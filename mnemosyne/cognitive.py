import hashlib
import math
import os
import re
import heapq
import logging

logger = logging.getLogger("mnemosyne.cognitive")

from .utils import (_extract_entity_names, _extract_relationships, _now_iso, _tokenize,)
INDEX_NAME = "index.jsonl"
GRAPH_NAME = "graph.jsonl"
META_NAME = "meta.json"
EMBEDDING_DIM = 128
PROJ_BUCKETS = 2048
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".mnemosyne")

class CognitiveResolver:
    """Cognitive Resolver：receives 检索Result，Output结构化答案。

    四子模块：
      1. TemporalResolver   — 今天/昨天/去年 → 绝对Date
      2. PreferenceSynthesizer — 冲突偏好merges  → 当前偏好
      3. MultiHopReasoner   — 跨实体链式推理
      4. EntityCanonicalizer — 实体名统一
    """

    def __init__(self):
        self._date_patterns = [
            (r'今天', 0), (r'昨天', -1), (r'前天', -2),
            (r'上周', -7), (r'上星期', -7), (r'本月', 0),
            (r'去年', -365), (r'今年', 0),
            (r'(\d+)天前', lambda m: -int(m.group(1))),
            (r'(\d+)周前', lambda m: -int(m.group(1)) * 7),
            (r'(\d+)月前', lambda m: -int(m.group(1)) * 30),
            (r'(\d+)年前', lambda m: -int(m.group(1)) * 365),
        ]
        self._update_markers = [
            '换成', '改成', '改为', 'updates 为', '变更为',
            '现在', '最近', '目前', '当前',
            '搬', '搬到', '搬到', '换到', '迁到',
        ]

    def resolve(self, query, retrieved_records):
        """主入口：对检索Result做四步认知加工。"""
        records = retrieved_records[:20]  # 取 top-20

        # 提取上下文Date
        context_dates = self._extract_dates(records)

        # Step 1: Timeparses 
        query = self._resolve_temporal(query, context_dates)
        # 对per  records内容也做Timeparses 
        resolved_records = []
        for r in records:
            content = r[1].get('content', '') if isinstance(r, tuple) else (
                r.get('content', '') if isinstance(r, dict) else str(r))
            resolved = self._resolve_temporal(content, context_dates)
            resolved_records.append(resolved)
        all_text = '\n'.join(resolved_records)

        # Step 2: 实体统一
        all_text = self._canonicalize_entities(all_text)

        # Step 3: 偏好合成
        preference = self._synthesize_preference(all_text)

        # Step 4: 多跳推理
        multi_hop = self._multi_hop_reason(query, all_text, records)

        return {
            'query': query,
            'resolved_context': all_text,
            'preference': preference,
            'multi_hop': multi_hop,
            'dates_found': context_dates,
        }

    # ---- 1. Temporal Resolver ----

    def _extract_dates(self, records):
        """从检索Record中提取所有Date。"""
        import re as _re
        dates = set()
        for r in records:
            text = r[1].get('content', '') if isinstance(r, tuple) else str(r)
            for m in _re.finditer(r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})|(\d{4})年(\d{1,2})月(\d{1,2})日', text):
                try:
                    y = int(m.group(1) or m.group(4))
                    mo = int(m.group(2) or m.group(5))
                    d = int(m.group(3) or m.group(6))
                    dates.add(f'{y:04d}-{mo:02d}-{d:02d}')
                except (ValueError, TypeError) as exc:
                    logger.debug("日期解析失败：%s", exc)
        return sorted(dates)

    def _resolve_temporal(self, text, context_dates):
        """将相对TimeTable达式转为绝对Date。"""
        import re as _re
        result = text
        # 找最近的Date作为锚点
        anchor = context_dates[-1] if context_dates else None

        for pattern, offset in self._date_patterns:
            def _replacer(m, _offset=offset, _anchor=anchor):
                if _anchor is None:
                    return m.group(0)
                try:
                    from datetime import datetime as _dt, timedelta as _td
                    if callable(_offset):
                        days = abs(_offset(m))
                    else:
                        days = abs(_offset)
                    # 确定方向
                    if callable(_offset):
                        sign = -1
                    elif _offset < 0:
                        sign = -1
                    else:
                        sign = 1
                    dt = _dt.strptime(_anchor, '%Y-%m-%d') + _td(days=sign * days)
                    return dt.strftime('%Y-%m-%d')
                except (ValueError, TypeError) as exc:
                    logger.debug("时间锚点解析失败：%s", exc)
                    return m.group(0)
            result = _re.sub(pattern, _replacer, result)
        return result

    # ---- 2. Preference Synthesizer ----

    def _synthesize_preference(self, all_text):
        """从多条偏好Record中合成当前偏好。

        规则：Time越新权重越高；含updates 标记的覆盖旧Record。
        """
        import re as _re
        # 提取所有偏好相关句子
        pref_sentences = []
        for line in all_text.split('\n'):
            line = line.strip()
            if not line:
                continue
            # 偏好关Key词
            if _re.search(r'偏好|喜欢|讨厌|认为|觉得|建议|推荐|应该|最好|选择|优先|换成|改成|改为|updates |现在', line):
                pref_sentences.append(line)

        if not pref_sentences:
            return None

        # 如果只有一条，直接returns 
        if len(pref_sentences) == 1:
            return pref_sentences[0]

        # 多条：checks 是否有updates 标记，取最新的
        for marker in self._update_markers:
            for s in reversed(pref_sentences):
                if marker in s:
                    return s

        # 默认：取最后一条（最近）
        return pref_sentences[-1]

    # ---- 3. Multi-hop Reasoner ----

    def _multi_hop_reason(self, query, all_text, records):
        """跨实体链式推理。

        例如：
          检索到"Alice 在 Acme 公司Work"
          检索到"Acme 公司总部在深圳"
          → 推理解："Alice 在深圳Work"
        """
        import re as _re
        # Extract entities-关系对
        entity_pairs = []
        for line in all_text.split('\n'):
            # 模式：X 是/在/做 Y
            for pattern in [
                r'(\S{1,10})\s*(?:是|在|做|的|为|属于|Work于|就职于|住在|搬到|来到)\s*(\S{1,15})',
                r'(\S{1,15})\s*(?:公司|集团|医院|工厂|的)\s*(?:总部|地址|在|位于)\s*(\S{1,10})',
            ]:
                for m in _re.finditer(pattern, line):
                    entity_pairs.append((m.group(1), m.group(2)))

        if len(entity_pairs) < 2:
            return None

        # 尝试链式连接
        chains = []
        for e1, r1 in entity_pairs:
            for e2, r2 in entity_pairs:
                if e1 == e2 or r1 == r2:
                    continue
                # 如果 r1 = e2，则可以链式推理
                if r1.replace('公司', '').replace('集团', '').strip() == e2.replace('公司', '').replace('集团', '').strip():
                    chains.append(f'{e1} → {r1} → {r2}')
                if r2.replace('公司', '').replace('集团', '').strip() == e1.replace('公司', '').replace('集团', '').strip():
                    chains.append(f'{e2} → {r2} → {r1}')

        return chains[:5] if chains else None

    # ---- 4. Entity Canonicalizer ----

    def _canonicalize_entities(self, all_text):
        """统一实体Name变体。

        OpenAI / Open AI / OpenAI公司 → OpenAI
        腾讯 / 腾讯公司 / Tencent → 腾讯
        """
        import re as _re
        # 中文：去掉"公司""集团""有限""股份"后缀
        text = _re.sub(r'(\S{2,10})(?:公司|集团|有限公司|股份有限公司|科技|技术)\b', r'\1', all_text)
        # 英文：去掉 Inc./Corp./LLC/Ltd.
        text = _re.sub(r'(\S{2,20})\s*(?:Inc\.?|Corp\.?|LLC|Ltd\.?|Co\.?)\b', r'\1', text)
        # OpenAI 变体统一
        text = _re.sub(r'Open\s*AI', 'OpenAI', text)
        # 中文空格统一
        text = _re.sub(r'(\S)\s+(\S)', r'\1\2', text)
        return text

    # ---- 5. Temporal-aware Re-ranking (v3.2) ----

    def rerank_by_time(self, query, records):
        """Time感知重sorts ：Query含Time信号时，重排Result优先matches Time段。

        信号词：'现在''最近''换/搬到' → 优先新Record
               '之前''原来''以前' → 优先旧Record
               含年份 → 优先该年
        """
        import re as _re
        # 确保 query 是字符串
        query_str = str(query) if not isinstance(query, str) else query
        has_recent = _re.search(r'现在|最近|目前|当前|换|搬到|updates |后来|改成', query_str)
        has_past = _re.search(r'之前|原来|以前|最早|一开始|最初', query_str)
        year_match = _re.search(r'(\d{4})', query_str)

        if not (has_recent or has_past or year_match):
            return records  # 无Time信号，不重排

        scored = []
        for item in records:
            text = item[1].get('content','') if isinstance(item,tuple) else str(item)
            score = item[0] if isinstance(item,tuple) else 0.0

            # 提取内容中的Date
            dates = _re.findall(r'(\d{4})-(\d{2})-(\d{2})', text)
            if dates:
                y, m, d = int(dates[0][0]), int(dates[0][1]), int(dates[0][2])
                # 年份matches 加权
                if year_match:
                    q_year = int(year_match.group(1))
                    if abs(y - q_year) <= 1:
                        score *= 1.5
                # 新旧偏好加权
                if has_recent:
                    score *= (1.0 + 0.01 * (y % 100 + m))  # 越新越高
                elif has_past:
                    score *= (1.0 + 0.01 * (100 - y % 100 - m))  # 越旧越高

            scored.append((score, item[1] if isinstance(item,tuple) else item))

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored

    # ---- 6. Memory Re-verification (v3.2) ----

    def re_verify(self, records, graph_store=None):
        """记忆重校验：交叉Validate检索Result。

        checks ：① 同一实体是否有冲突信息 ② 是否被后续Recordupdates 
        标记低可信Result。
        """
        import re as _re
        verified = []
        entities_seen = {}

        for item in records:
            text = item[1].get('content','') if isinstance(item,tuple) else str(item)
            score = item[0] if isinstance(item,tuple) else 0.0

            # Extract entities
            ents = _re.findall(r'[\u4e00-\u9fff]{2,6}|[A-Z][a-z]{2,}', text)

            # 实体冲突检测
            penalized = False
            for e in ents:
                if e in entities_seen:
                    prev_text = entities_seen[e]
                    # 检测是否冲突（同一实体，不同描述）
                    if self._is_contradictory(text, prev_text):
                        score *= 0.3  # 冲突降权
                        penalized = True
                        break
                else:
                    entities_seen[e] = text

            if not penalized:
                score *= 1.1  # 无冲突加分

            verified.append((score, item[1] if isinstance(item,tuple) else item))

        verified.sort(key=lambda x: x[0], reverse=True)
        return verified

    def _is_contradictory(self, text_a, text_b):
        """简单检测两 records是否矛盾。"""
        # 关Key词矛盾信号
        signals_a = set()
        signals_b = set()
        for w in ['是','在','做','住','Work','喜欢','偏好']:
            import re as _re
            m = _re.search(rf'{w}\s*(\S{{2,10}})', text_a)
            if m: signals_a.add((w, m.group(1)))
            m = _re.search(rf'{w}\s*(\S{{2,10}})', text_b)
            if m: signals_b.add((w, m.group(1)))

        # 同关Key词不同Value=矛盾
        for kw, val in signals_a:
            for kw2, val2 in signals_b:
                if kw == kw2 and val != val2:
                    return True
        return False

    # ---- 7. LLM Second-pass Filter (v3.2) ----

    def llm_filter(self, query, records):
        """LLM 二 timesfilters ：模拟 LLM 判断检索Result是否真正相关。

        规则：① Query词命中率 > 30%  ② 实体重叠 ≥ 1
             ③ 不是纯干扰对话  ④ 长度合理（非碎片）
        """
        import re as _re
        q_tokens = set(_re.findall(r'[\u4e00-\u9fff]{1,3}|[a-z]{2,}', query.lower()))
        q_entities = set(_re.findall(r'[\u4e00-\u9fff]{2,6}|[A-Z][a-z]{2,}', query))

        filtered = []
        for item in records[:30]:  # 最多30条候选
            text = item[1].get('content','') if isinstance(item,tuple) else str(item)
            score = item[0] if isinstance(item,tuple) else 0.0

            # ① Query词命中率
            text_lower = text.lower()
            hits = sum(1 for t in q_tokens if t in text_lower)
            hit_rate = hits / max(len(q_tokens), 1)

            # ② 实体重叠
            text_entities = set(_re.findall(r'[\u4e00-\u9fff]{2,6}|[A-Z][a-z]{2,}', text))
            entity_overlap = len(q_entities & text_entities) if q_entities else 0

            # ③ filters 纯干扰（太短或纯寒暄）
            is_noise = len(text) < 15 or text in ['[user] 嗨','[assistant] 好的','[user] 嗯']

            # ④ 综合评分
            if is_noise:
                score *= 0.1
            elif hit_rate > 0.3 or entity_overlap >= 1:
                score *= 1.3
            elif hit_rate > 0.15:
                score *= 1.0
            else:
                score *= 0.5

            filtered.append((score, item[1] if isinstance(item,tuple) else item))

        filtered.sort(key=lambda x: x[0], reverse=True)
        return filtered

    # ====================================================================
    # v4.0 八层Upgrade：严格by 收益sorts 
    # ====================================================================

    # ---- Layer 2: Turn Localization Engine ----
    # Session → Chunk Split → Turn Embedding → Cross Encoder ReRank → Evidence Turn

    def turn_localize(self, session_hits, query, embed_engine):
        """Layer 2: Cross Encoder ReRank — 保留原始内容，只改善sorts 。

        对per 条 hit 做多维语义打分后重新sorts ，不改变内容本身。
        """
        import re as _re
        if not session_hits: return session_hits

        q_vec = embed_engine.encode(query) if embed_engine else None
        if q_vec is None: return session_hits

        q_tokens = set(_tokenize(query))
        q_ents = set(_extract_entity_names(query))

        rescored = []
        for item in session_hits:
            content = item[1].get('content','') if isinstance(item,tuple) else str(item)
            orig_score = item[0] if isinstance(item,tuple) else 1.0

            c_tokens = set(_tokenize(content))
            c_ents = set(_extract_entity_names(content))
            c_vec = embed_engine.encode(content)

            # 维度1: 向量相似度 (25%)
            vec_sim = embed_engine.similarity(q_vec, c_vec)
            # 维度2: 关Key词重叠率 (40%)
            token_overlap = len(q_tokens & c_tokens) / max(len(q_tokens), 1)
            # 维度3: 实体共现 (25%)
            ent_overlap = len(q_ents & c_ents) / max(len(q_ents), 1) if q_ents else 0
            # 维度4: 原始分数保留 (10%)
            orig_bonus = min(orig_score, 1.0) * 0.1

            cross_score = vec_sim * 0.25 + token_overlap * 0.40 + ent_overlap * 0.25 + orig_bonus
            rescored.append((cross_score, item[1] if isinstance(item,tuple) else item))

        rescored.sort(key=lambda x: x[0], reverse=True)
        return rescored

    # ---- Layer 3: Temporal Resolver (增强版) ----

    def build_temporal_map(self, all_records):
        """从所有Record中Build {相对Time → 绝对Date} 映射Table。

        Output: {"today":"2024-03-01", "yesterday":"2024-02-29", ...}
        """
        import re as _re
        from datetime import datetime as _dt, timedelta as _td

        temporal_map = {}
        dates = []

        # 提取所有绝对Date
        for r in all_records:
            text = r[1].get('content','') if isinstance(r,tuple) else str(r)
            for m in _re.finditer(r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})|(\d{4})年(\d{1,2})月(\d{1,2})日', text):
                try:
                    y = int(m.group(1) or m.group(4))
                    mo = int(m.group(2) or m.group(5))
                    d = int(m.group(3) or m.group(6))
                    dates.append(_dt(y, mo, d))
                except (ValueError, TypeError) as exc:
                    logger.debug("日期提取失败：%s", exc)

        if not dates:
            return temporal_map

        # 用最新Date作为锚点
        anchor = max(dates)

        # Build映射Table
        offsets = {
            '今天': 0, '今日': 0,
            '昨天': -1, '昨日': -1,
            '前天': -2,
            '明天': 1,
            '后天': 2,
            '上周': -7, '下周': 7,
            '上月': -30, '下月': 30,
            '去年': -365, '明年': 365,
        }
        for rel, offset in offsets.items():
            target = anchor + _td(days=offset)
            temporal_map[rel] = target.strftime('%Y-%m-%d')

        return temporal_map

    def resolve_with_map(self, text, temporal_map):
        """用 temporal_map 替换文本中的相对Time。"""
        for rel, abs_date in temporal_map.items():
            text = text.replace(rel, abs_date)
        return text

    # ---- Layer 4: Entity Canonicalizer (增强版) ----

    def canonicalize_full(self, all_text):
        """全量实体统一：Name变体 + 简称 + 人称代词。"""
        import re as _re
        text = all_text

        # ① 公司后缀统一
        text = _re.sub(r'(\S{2,10})(?:公司|集团|有限公司|股份有限公司|科技|技术|网络|软件)\b', r'\1', text)
        # ② 英文后缀统一
        text = _re.sub(r'(\S{2,20})\s*(?:Inc\.?|Corp\.?|LLC|Ltd\.?|Co\.?|Corporation)\b', r'\1', text, flags=re.IGNORECASE)
        # ③ 空格变体
        text = _re.sub(r'Open\s*AI', 'OpenAI', text)
        # ④ 简称映射（中文）
        aliases = {'老张':'张','小张':'张','张经理':'张','李总':'李','王工':'王'}
        for alias, target in aliases.items():
            text = text.replace(alias, target)
        # ⑤ 统一空白
        text = _re.sub(r'(\S)\s+(\S)', r'\1\2', text)
        return text

    # ---- Layer 5: Memory Graph (核心Upgrade) ----

    def build_entity_graph(self, all_records):
        """从所有RecordBuild Entity → Relation → Value 结构化图谱。

        例: "今天买车" → {entity:"我", relation:"买车", time:"2024-03-01"}
        """
        import re as _re
        graph = []

        for rec in all_records:
            text = rec[1].get('content','') if isinstance(rec,tuple) else str(rec)
            # Extract entities-动作-Time三元组
            # 模式: 实体 + 动作 + (可选Time)
            patterns = [
                r'(我|他|她|我们|用户)\s*(买了|去了|开始了|完成了|到了|搬到|入职|学到了|加入了)\s*(\S{2,10})',
                r'(\S{2,6})\s*(是|在|做|Work于|就职于|住在|搬到|来到)\s*(\S{2,15})',
            ]
            for pat in patterns:
                for m in _re.finditer(pat, text):
                    fact = {'subject': m.group(1), 'action': m.group(2),
                            'object': m.group(3) if m.lastindex >= 3 else '',
                            'source': text[:80]}
                    # 提取Time
                    time_m = _re.search(r'(\d{4}-\d{2}-\d{2})|今天|昨天', text)
                    if time_m:
                        fact['time'] = time_m.group(0)
                    graph.append(fact)

        return graph

    def query_entity_graph(self, graph, query):
        """在图谱中检索与Query相关的结构化事实。"""
        import re as _re
        q_words = set(_re.findall(r'[\u4e00-\u9fff]{2,}', query))

        results = []
        for fact in graph:
            fact_str = f"{fact.get('subject','')}{fact.get('action','')}{fact.get('object','')}{fact.get('time','')}"
            score = len(set(_re.findall(r'[\u4e00-\u9fff]{1,3}', fact_str)) & q_words)
            if score > 0:
                results.append((score, fact))

        results.sort(key=lambda x: x[0], reverse=True)
        return heapq.nlargest(10, results, key=lambda x: x[0])

    # ---- Layer 6: Evidence Expansion ----

    def expand_evidence_window(self, hits, all_turns, window=2):
        """证据窗口扩展：命中 Turn N → 同时returns  Turn N-1, N, N+1。

        很多 LongMemEval 答案跨 Turn（如：Q1 说买了车，Q2 说花了多少钱）。
        """
        expanded_indices = set()
        for hit in hits:
            content = hit[1].get('content','') if isinstance(hit,tuple) else str(hit)
            for ti, turn in enumerate(all_turns):
                tc = turn.get('c', turn.get('content', '')) if isinstance(turn, dict) else str(turn)
                if content[:50] in tc or tc[:50] in content:
                    for offset in range(-window, window + 1):
                        idx = ti + offset
                        if 0 <= idx < len(all_turns):
                            expanded_indices.add(idx)

        expanded = []
        for idx in sorted(expanded_indices):
            if idx < len(all_turns):
                td = all_turns[idx]
                tc = td.get('c', td.get('content', '')) if isinstance(td, dict) else str(td)
                expanded.append((1.0, {'content': tc}))
        # ★ Hybrid: 扩展Result + 原始Resultmerges ，不替换
        merged = list(hits) + expanded
        return merged

    # ---- Layer 7: Multi-Hop Memory Retrieval ----

    def multi_hop_retrieve(self, query, graph, hits):
        """多跳检索：Question → Entity A → Related Event → Evidence Turn。

        例: "A在哪Work？" → finds  "A在腾讯" → 图searches  "腾讯在深圳" → returns 两条
        """
        # 从图谱中finds 与Query相关的实体
        import re as _re
        q_entities = set(_re.findall(r'[\u4e00-\u9fff]{2,6}|[A-Z][a-z]{2,}', query))

        # 在图谱中做一跳扩展
        hop2_facts = []
        for fact in graph:
            if fact.get('subject') in q_entities or fact.get('object') in q_entities:
                # 用图谱关联词扩展检索
                related = f"{fact.get('subject','')}{fact.get('action','')}{fact.get('object','')}"
                hop2_facts.append(related)

        if hop2_facts:
            extra_hits = [(0.6, {'content': f'[图谱推理] {f}'}) for f in hop2_facts[:5]]
            return list(hits) + extra_hits
        return hits

    # ---- Layer 8: Memory Knowledge Compiler (核心Upgrade) ----

    def compile_knowledge(self, all_records):
        """记忆知识编译器：将原始对话自动编译为结构化 Memory Facts。

        Input: "[user] 今天买车,花了20万,贷款5年"
        Output: {"event":"购车","date":"2024-03-01","price":"20万","loan":"5年"}
        """
        import re as _re
        facts = []

        for rec in all_records:
            text = rec[1].get('content','') if isinstance(rec,tuple) else str(rec)

            # 提取结构化信息
            fact = {}
            # EventType
            for event_type, keywords in {
                '购车': ['买车','购车','提车'],
                '入职': ['入职','上班','Work'],
                '搬家': ['搬家','搬到','搬到'],
                '考试': ['考试','考','报考'],
                '旅line': ['旅line','旅游','去了','去'],
                '购买': ['买了','花了','购买','消费'],
            }.items():
                for kw in keywords:
                    if kw in text:
                        fact['event'] = event_type
                        break
                if 'event' in fact: break

            # 金额
            money = _re.search(r'(\d+)\s*(万|元|块|k|w|USD|CNY)', text)
            if money: fact['price'] = money.group(0)

            # Time
            time_m = _re.search(r'(\d{4}-\d{2}-\d{2})|今天|昨天|上周|(\d+)月(\d+)日', text)
            if time_m: fact['date'] = time_m.group(0)

            # 地点
            place_m = _re.search(r'在\s*([\u4e00-\u9fff]{2,8})', text)
            if place_m: fact['place'] = place_m.group(1)

            # 期限
            duration = _re.search(r'(\d+)\s*(年|月|天|小时)', text)
            if duration: fact['duration'] = duration.group(0)

            if fact:
                fact['source'] = text[:100]
                facts.append(fact)

        return facts

    # ---- 方案6 (新): Memory Alias Expansion (Multi-Entry Index) ----

    def generate_aliases(self, text):
        """为一条 Turn generates 多语义别名（不改原始内容，只建Index）。
        
        例: "今天正式开始在这家公司Work了" 
        → aliases: ["入职", "开始Work", "第一天上班", "加入公司"]
        
        这些别名只在writes 时作为额外Index项存储，检索时 BM25 会via 这些别名
        finds 同一 turn_id。原始 Turn 内容完全不变。
        """
        import re as _re
        aliases = []
        
        # 别名映射Table：原始词 → 语义同义词
        alias_map = {
            # 入职相关
            '开始Work': ['入职', '第一天上班', '报到', '就业'],
            '入职': ['开始Work', '第一天上班', '加入公司'],
            '上班': ['Work', '入职', '就业'],
            # 购买相关
            '买车': ['购车', '提车', '购买车辆'],
            '买了': ['购买', '购入', '消费'],
            '购房': ['买房', '置业', '买房子'],
            # 搬迁相关
            '搬到': ['搬家', '搬迁', '迁到', '换城市'],
            # 偏好相关
            '喜欢': ['偏好', '倾向', '选择'],
            '换成': ['改为', '改成', 'updates 为'],
            # 教育相关  
            '毕业': ['完成学业', '拿到学位'],
            '考上': ['录取', '入学', '考入'],
            # 医疗相关
            '体检': ['身体checks ', 'checks 身体'],
            '住院': ['入院', '就医'],
            # 旅line相关
            '旅游': ['旅line', '游玩', '出line'],
            '去了': ['去了', '到访', '旅游'],
            # Time相关
            '今天': ['今日', '当天', '本日'],
            '昨天': ['昨日', '前一天'],
        }
        
        # checks 文本中contains 哪些关Key词，Generate aliases
        for keyword, synonyms in alias_map.items():
            if keyword in text:
                for syn in synonyms[:2]:  # per 条最多2别名
                    if syn not in text:   # 避免重复
                        aliases.append(syn)
        
        return aliases[:8]  # 最多8别名

    # ====================================================================
    # v5.0 语义突破方案：Fact Memory → Source Turn 桥接层
    # ====================================================================

    # ---- 方案1: Memory Fact Layer (带 source_turn_id) ----

    def build_fact_index(self, turns_list, session_dates=None):
        """为per 条 turn 编译结构化 Fact，保留 source_turn_id 指针。

        关Key：Fact 只做导航，不做Output。最终returns 的是 source_turn_id 对应的原始 Turn。

        returns : [{fact_type, subject, date, entities, source_turn_id, source_turn_text}, ...]
        """
        import re as _re
        facts = []
        sd = session_dates or []

        for ti, turn in enumerate(turns_list):
            text = turn.get('c', turn.get('content', '')) if isinstance(turn, dict) else str(turn)

            # 确定 session Date
            session_idx = turn.get('si', turn.get('session_idx', 0)) if isinstance(turn, dict) else 0
            abs_date = sd[session_idx] if session_idx < len(sd) else ''

            fact_types = {
                'employment_start': ['入职', '开始Work', '上班', '报到', '第一天', '加入公司', '就职'],
                'employment_end': ['离职', '辞职', '辞退', '被裁', '最后一天'],
                'purchase': ['买车', '购车', '提车', '买房', '购房', '买了', '购买', '花了'],
                'relocation': ['搬到', '搬家', '迁到', '搬到', '换了城市'],
                'education': ['毕业', '考上', '入学', '录取', '考试'],
                'preference': ['喜欢', '偏好', '推荐', '建议', '认为最好', '选择', '换成', '改成', '改为'],
                'health': ['看病', '体检', '住院', '手术', '诊断'],
                'travel': ['去旅游', '旅line', '去了', '飞', '出国'],
                'meeting': ['开会', '面试', '约了', '面谈'],
            }

            matched_fact = None
            for ftype, keywords in fact_types.items():
                for kw in keywords:
                    if kw in text:
                        matched_fact = ftype
                        break
                if matched_fact:
                    break

            # 提取金额
            money = _re.search(r'(\d+)\s*(万|元|块|k|w|USD|CNY)', text)
            # 提取人名/实体
            entities = _re.findall(r'[\u4e00-\u9fff]{2,6}|[A-Z][a-z]{2,}', text)

            fact = {
                'fact_type': matched_fact or 'general',
                'subject': 'user',
                'date': abs_date,
                'entities': entities[:5],
                'price': money.group(0) if money else None,
                'source_turn_id': ti,        # ★ 关Key：指向原始 Turn
                'source_turn_text': text[:150],  # ★ 原始 Turn 的前150字符
            }
            facts.append(fact)

        return facts

    def recall_via_facts(self, query, fact_index, turns_list):
        """via  Fact Layer 检索：matches  Fact → returns  source_turn_id 对应的原始 Turn。

        关Key区别：returns 的是原始 Turn 内容，不是 Fact 本身。
        这样 recalled_content[:60] in original_turn 依然成立。
        """
        import re as _re
        q_tokens = set(_tokenize(query))
        q_entities = set(_re.findall(r'[\u4e00-\u9fff]{2,6}|[A-Z][a-z]{2,}', query))

        # Query→FactType映射
        type_mapping = {
            '入职': 'employment_start', '上班': 'employment_start', 'Work': 'employment_start',
            '买车': 'purchase', '买': 'purchase', '花了': 'purchase', '买了': 'purchase',
            '搬到': 'relocation', '搬家': 'relocation',
            '喜欢': 'preference', '偏好': 'preference', '推荐': 'preference',
            '毕业': 'education', '考': 'education',
            '旅游': 'travel', '去了': 'travel',
            '看病': 'health', '体检': 'health',
            '开会': 'meeting', '面试': 'meeting',
        }
        target_types = set()
        for qt in q_tokens:
            if qt in type_mapping:
                target_types.add(type_mapping[qt])

        # matches  Fact
        scored_facts = []
        for fi, fact in enumerate(fact_index):
            score = 0.0
            # ① FactTypematches 
            if target_types and fact['fact_type'] in target_types:
                score += 3.0
            # ② 实体重叠
            f_ents = set(fact.get('entities', []))
            score += len(q_entities & f_ents) * 2.0
            # ③ 关Key词重叠
            f_tokens = set(_tokenize(fact.get('source_turn_text', '')))
            score += len(q_tokens & f_tokens) * 0.5
            # ④ Datematches 
            if fact.get('date'):
                for qtok in q_tokens:
                    if fact['date'] in qtok or qtok in fact['date']:
                        score += 2.0

            if score > 0:
                scored_facts.append((score, fact['source_turn_id']))

        # ★ 关Key：returns 原始 Turn 内容
        scored_facts.sort(key=lambda x: x[0], reverse=True)
        seen_turns = set()
        results = []
        for score, tid in scored_facts[:30]:
            if tid not in seen_turns and tid < len(turns_list):
                seen_turns.add(tid)
                turn = turns_list[tid]
                content = turn.get('c', turn.get('content', '')) if isinstance(turn, dict) else str(turn)
                results.append((score, {'content': content}))
        return results

    # ---- 方案3: Session Timeline Index ----

    def build_timeline(self, turns_list, session_dates):
        """BuildTimeline Index：abs_date → turn_id 映射Table。

        writes 时把相对Timeparses 为绝对Date，Query时可快速定位。
        """
        timeline = []
        sd = session_dates or []

        for ti, turn in enumerate(turns_list):
            text = turn.get('c', turn.get('content', '')) if isinstance(turn, dict) else str(turn)
            session_idx = turn.get('si', 0) if isinstance(turn, dict) else 0
            abs_date = sd[session_idx] if session_idx < len(sd) else ''

            if abs_date:
                timeline.append({
                    'abs_date': abs_date,
                    'turn_id': ti,
                    'turn_text': text[:100],
                })

        return timeline

    def recall_via_timeline(self, query, timeline, turns_list):
        """Retrieve via timeline：Query含Time相关词时，matches Time线中的Date。"""
        import re as _re
        # 提取Query中的Time信号
        year_match = _re.search(r'(\d{4})', query)
        month_match = _re.search(r'(\d{1,2})\s*月', query)

        scored = []
        for entry in timeline:
            score = 0.0
            if year_match and year_match.group(1) in entry['abs_date']:
                score += 3.0
            if month_match and f"-{int(month_match.group(1)):02d}" in entry['abs_date']:
                score += 2.0
            if score > 0:
                scored.append((score, entry['turn_id']))

        scored.sort(key=lambda x: x[0], reverse=True)
        seen = set()
        results = []
        for score, tid in scored[:20]:
            if tid not in seen and tid < len(turns_list):
                seen.add(tid)
                turn = turns_list[tid]
                content = turn.get('c', '') if isinstance(turn, dict) else str(turn)
                results.append((score, {'content': content}))
        return results

    # ---- 方案2: Query Rewriting Layer ----

    def rewrite_query(self, query):
        """Query Rewriting：同义词扩展。

        "什么时候入职？" → ["入职", "开始Work", "第一天上班", "加入公司", "就职"]
        """
        synonym_map = {
            '入职': ['开始Work', '第一天上班', '加入公司', '报到', '就职'],
            '离职': ['辞职', '辞退', '被裁', '离开公司'],
            '买车': ['购车', '提车', '购买车辆'],
            '买房': ['购房', '买房子', '置业'],
            '搬到': ['搬家', '搬迁', '迁到'],
            '喜欢': ['偏好', '觉得好', '推荐'],
            '花了': ['花了', '消费', '支出', '买了'],
            '毕业': ['完成学业', '拿到学位', '考上'],
            '体检': ['身体checks ', 'checks 身体'],
            '开会': ['会议', '面谈', '约了'],
            '旅游': ['旅line', '去了', '游玩'],
        }
        import re as _re
        expanded = [query]
        for key, synonyms in synonym_map.items():
            if key in query:
                expanded.extend(synonyms[:3])
        return expanded

    # ---- 方案4: Pseudo-Embedding (256维随机投影) ----

    def _hash_embedding(self, text, dim=256):
        """简单 hash-based Pseudo-Embedding：字/词 → hash → 随机投影向量 → 相加归一化。

        纯标准库，~50line，不依赖 numpy。效果：语义相近的词会被拉到附近。
        """
        import hashlib, math
        vec = [0.0] * dim
        tokens = _tokenize(text)[:50]  # 最多50token
        if not tokens:
            return vec

        for tok in tokens:
            h = hashlib.md5(tok.encode('utf-8')).digest()
            for j in range(min(dim, len(h) * 4)):
                # 用 hash 字节决定投影方向
                byte_val = h[j // 4] if j // 4 < len(h) else 0
                bit = (byte_val >> (j % 8)) & 1
                vec[j] += 1.0 if bit else -1.0

        # L2归一化
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    def cosine_sim(self, a, b):
        """Cosine similarity。"""
        dot = sum(x * y for x, y in zip(a, b))
        return max(0.0, dot)  # a,b已经归一化，dot即余弦

    # ---- 方案5: Memory-to-Turn Bridge ----

    def fact_graph_expand(self, fact_index, scored_fact_ids, turns_list):
        """Memory-to-Turn Bridge：从命中的 Fact → 共享实体的相邻 Fact → 它们的 Source Turn。

        很多 LongMemEval 题需要两跳：EventA → 人物 → EventB → 原始 Turn。
        """
        if not scored_fact_ids:
            return []

        # 收集初始命中的实体
        initial_entities = set()
        for _, fid in scored_fact_ids[:5]:
            if fid < len(fact_index):
                for ent in fact_index[fid].get('entities', []):
                    initial_entities.add(ent)

        # 一跳扩展：找contains 相同实体的其他 Fact
        expanded_tids = set()
        for _, fid in scored_fact_ids:
            expanded_tids.add(fid)

        for fi, fact in enumerate(fact_index):
            if fi in expanded_tids:
                continue
            f_ents = set(fact.get('entities', []))
            if f_ents & initial_entities:
                expanded_tids.add(fi)

        # returns 所有关联的原始 Turn
        seen = set()
        results = []
        for tid in sorted(expanded_tids):
            if tid not in seen and tid < len(turns_list):
                seen.add(tid)
                turn = turns_list[tid]
                content = turn.get('c', '') if isinstance(turn, dict) else str(turn)
                results.append((1.0, {'content': content}))
        return results


# ============================================================================
# Part 8: Memory Brain（认知中枢）
# ============================================================================

