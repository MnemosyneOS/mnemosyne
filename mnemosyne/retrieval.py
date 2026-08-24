import json
import os
import re
import heapq
import logging
from array import array

logger = logging.getLogger("mnemosyne.retrieval")

from .graph import (_bm25_score, _confidence_weight, _cosine, _idf, _temporal_score,)
from .utils import (_extract_entity_names, _tf_vector, _tokenize, _utcnow_ts,)
INDEX_NAME = "index.jsonl"
GRAPH_NAME = "graph.jsonl"
META_NAME = "meta.json"
EMBEDDING_DIM = 128
PROJ_BUCKETS = 2048
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".mnemosyne")


def _intersect_sorted(a, b):
    """两个升序 posting（list/array）的交集（归并，O(len(a)+len(b))）。"""
    out = []
    i = j = 0
    la, lb = len(a), len(b)
    while i < la and j < lb:
        if a[i] == b[j]:
            out.append(a[i])
            i += 1
            j += 1
        elif a[i] < b[j]:
            i += 1
        else:
            j += 1
    return out


def _intern(table, token):
    """token 字符串驻留：相同 token 只保留一份字符串对象。"""
    return table.setdefault(token, token)


# 检索融合所需的记录字段（轻量记录缓存只保留这些键，省 ~40% 记录内存）
_RECORD_KEYS = (
    "id", "content", "type", "entities", "tags", "confidence", "importance",
    "tier", "status", "project", "layer", "fact_type", "verification",
    "event_time", "created_at", "access_count", "last_accessed_at",
    "session_id", "embedding", "topic_tag", "meta",
)

# 与 _RECORD_KEYS 对应的 sqlite 列名白名单（type←mtype、meta←template_hash）。
# 轻量模式重建索引时按此列清单直接物化轻量记录，
# 跳过完整记录 dict 的双份驻留（100k 内存关键优化）。
_LIGHT_COLUMNS = (
    "id", "content", "mtype", "entities", "tags", "confidence", "importance",
    "tier", "status", "project", "layer", "fact_type", "verification",
    "event_time", "created_at", "access_count", "last_accessed_at",
    "session_id", "embedding", "topic_tag", "template_hash",
)


def _slim_record(r):
    """构造轻量记录（仅保留检索融合所需字段；消费方 .get() 语义一致）。"""
    slim = {}
    for k in _RECORD_KEYS:
        if k in r:
            slim[k] = r[k]
    slim.setdefault("meta", {})
    return slim


class RetrievalEngine:
    """5-Way Fusion检Index擎（v3.0 Inverted Index加速版）。

    五路：BM25关Key词 + 随机投影向量 + Knowledge Graph + Time衰减 + 可信度加权
    v3.0 新增：Inverted IndexCache，BM25 检索从 O(n) 降至 O(q·log(n))。
    v7.0.0 内存优化：token 字符串全局驻留（intern）+ posting 用 array('I') 紧凑存储，
    100k 规模索引内存从 ~800MB 降至 ~50MB。
    """

    def __init__(self, embed_engine=None, graph_store=None):
        self.embed_engine = embed_engine  # 允许 None 以禁用向量路径（勿用 or 兜底重建）
        self.graph_store = graph_store
        # v3.0: Inverted IndexCache
        self._inverted_index = {}       # token → array('I')（升序 doc_idx postings）
        self._doc_tf_cache = []         # 预computes 的 TF 向量（避免per  times检索重新分词）
        self._tok_intern = {}           # token 字符串驻留表（内存优化：相同 token 只存一份）
        self._indexed_record_count = 0  # 上 timesIndex时的Record数（active）
        self._indexed_store_path = None # 上 timesIndex的记忆库Path
        self._cached_records = []       # 缓存过滤后的 records，避免 retrieve 二次全量扫描
        # v5.2 量级扩展：倒排持久化 + IDF/avg_len 增量缓存
        self._indexed_fingerprint = None  # (mtime_ns, size) 文件指纹，检测 index.jsonl 变化
        self._idf_cache = None          # 缓存的 IDF 字典（避免每次检索 O(N) 重算）
        self._avg_len_cache = 0.0       # 缓存的平均文档长度
        self._inv_dirty = False         # 倒排是否脏（需持久化）
        self._superseded_ids = set()    # 缓存 superseded 记录 id（避免每次检索 O(N) 扫描）
        self._conf_weights = []         # 缓存可信度权重（避免每次检索 O(N) 重算）
        self._total_chars = 0           # 缓存 active 记录总字符数（避免 recall 每次 O(N) 重算）
        self._topic_index = {}          # 主题索引：tag/topic_tag → set(doc_idx)，加速 query_topic（方向3）
        self._id_map = {}               # id → doc_idx 映射（FTS5 候选通道复用，避免每次查询 O(N) 建表）
        self._light_index = False       # 轻量索引模式（sqlite+FTS5 后端：候选由 FTS5 提供，
                                        # 不构建内存倒排/doc_tf，100k 规模省 ~200MB）
        # v7.0.0: 查询 TTL 缓存（相同查询 10 秒内复用，减少重复计算）
        self._query_cache = {}          # cache_key → (timestamp, result_list)
        self._query_cache_ttl = 10.0    # 秒

    def _ensure_index(self, store):
        """增量更新检索索引：
        1) 文件指纹快速判断 → 无变化时 O(1) 返回，不读盘不重建
        2) 轻量模式（sqlite+FTS5 后端）：只缓存 records/置信度/主题索引，
           BM25 候选由 FTS5 提供，不构建内存倒排与 doc_tf（内存省 ~200MB@100k）
        3) 完整模式（JSONL 后端）：持久化倒排加载 + 全量重建兜底"""
        self._light_index = bool(getattr(store, "search", None)
                                 and callable(getattr(store, "search")))
        fp = self._store_fingerprint(store)
        # 快速路径：文件指纹未变 → 索引最新
        if fp is not None and fp == self._indexed_fingerprint and self._indexed_store_path == store.index_path:
            return  # O(1) 命中，不读盘不重建
        if not self._light_index:
            # 尝试持久化加载（仅完整模式）
            if self._try_load_index(store, fp):
                return
        # 重建兜底
        self._rebuild_index(store, fp)

    def _store_fingerprint(self, store):
        """返回 index.jsonl 的文件指纹 (mtime_ns, size)，用于检测变化。"""
        try:
            st = os.stat(store.index_path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _rebuild_index(self, store, fp=None):
        """重建索引缓存。

        轻量模式（sqlite+FTS5）：只缓存 records/置信度/主题索引/id 映射；
        完整模式（JSONL）：额外构建倒排 + doc_tf 并持久化。
        """
        records = [r for r in store.all_records()
                   if not r.get("_corrupt") and r.get("status", "active") != "deleted"]
        if self._light_index:
            # 轻量记录缓存：直接按列白名单从存储层物化（省 ~40% 记录内存），
            # 不再先物化完整记录再 slim（消灭双份驻留瞬态，100k 内存关键）
            records = [
                r for r in store.all_records(keys=_LIGHT_COLUMNS)
                if not r.get("_corrupt") and r.get("status", "active") != "deleted"
            ]
        self._inverted_index.clear()
        self._doc_tf_cache = []
        self._cached_records = records  # ← 缓存过滤后 records，retrieve() 直接复用
        self._idf_cache = None
        self._avg_len_cache = 0.0
        self._superseded_ids = {r['id'] for r in records if r.get('verification') in ('superseded', 'outdated')}
        self._conf_weights = [_confidence_weight(r) for r in records]
        self._total_chars = sum(len(r.get('content', '')) for r in records)
        self._id_map = {r['id']: idx for idx, r in enumerate(records)}
        # 构建主题索引（方向3: 加速 query_topic，O(1) 替代 O(N) 全量扫描）
        self._topic_index = {}
        for idx, r in enumerate(records):
            for tag in (r.get('tags') or []):
                self._topic_index.setdefault(tag, set()).add(idx)
            topic_tag = r.get('topic_tag') or ''
            if topic_tag:
                self._topic_index.setdefault(topic_tag, set()).add(idx)
        if self._light_index:
            # 轻量模式：不构建内存倒排/doc_tf（FTS5 提供 BM25 候选与排序）
            self._indexed_record_count = len(records)
            self._indexed_fingerprint = fp if fp is not None else self._store_fingerprint(store)
            self._indexed_store_path = store.index_path
            self._inv_dirty = False
            return
        for idx, r in enumerate(records):
            tokens = [_intern(self._tok_intern, t) for t in _tokenize(r.get("content", ""))]
            self._doc_tf_cache.append(_tf_vector(tokens))
            for tok in set(tokens):
                if tok not in self._inverted_index:
                    self._inverted_index[tok] = []
                self._inverted_index[tok].append(idx)
        # posting 列表 → 紧凑 array('I')（释放 Python int 对象，内存降 ~10 倍）
        self._inverted_index = {tok: array('I', docs)
                                for tok, docs in self._inverted_index.items()}
        self._indexed_record_count = len(records)
        self._indexed_fingerprint = fp if fp is not None else self._store_fingerprint(store)
        self._indexed_store_path = store.index_path
        self._inv_dirty = True
        self._persist_index(store)

    def incremental_add(self, store, record):
        """增量添加一条记录到检索缓存（retain 后调用，避免全量重建）。

        前提：调用前索引已是最新（_ensure_index 已建立且无 rewrite 干扰）。
        若索引未建立或 store 已变化，回退全量重建保证正确性。
        """
        if record.get("_corrupt") or record.get("status", "active") == "deleted":
            return
        # 与 _ensure_index 一致地判定轻量模式（在首次 retain 即生效，
        # 避免先建了完整倒排再切换）
        self._light_index = bool(getattr(store, "search", None)
                                 and callable(getattr(store, "search")))
        # 索引尚未建立（首次 retain）→ 全量重建（含这条新记录）
        if self._indexed_store_path != store.index_path or not self._cached_records:
            self._rebuild_index(store)
            return
        doc_idx = len(self._cached_records)
        cached_record = _slim_record(record) if self._light_index else record
        self._cached_records.append(cached_record)
        self._id_map[record['id']] = doc_idx
        if record.get('verification') in ('superseded', 'outdated'):
            self._superseded_ids.add(record['id'])
        self._conf_weights.append(_confidence_weight(record))
        self._total_chars += len(record.get('content', ''))
        for tag in (record.get('tags') or []):
            self._topic_index.setdefault(tag, set()).add(doc_idx)
        topic_tag = record.get('topic_tag') or ''
        if topic_tag:
            self._topic_index.setdefault(topic_tag, set()).add(doc_idx)
        self._indexed_record_count = doc_idx + 1
        if self._light_index:
            # 轻量模式：无倒排/doc_tf 可更新
            self._indexed_fingerprint = self._store_fingerprint(store)
            return
        tokens = [_intern(self._tok_intern, t) for t in _tokenize(record.get("content", ""))]
        tf = _tf_vector(tokens)
        self._doc_tf_cache.append(tf)
        for tok in set(tokens):
            if tok not in self._inverted_index:
                self._inverted_index[tok] = array('I', [doc_idx])
            else:
                self._inverted_index[tok].append(doc_idx)
        self._indexed_fingerprint = self._store_fingerprint(store)
        self._idf_cache = None  # IDF 变了，失效
        self._avg_len_cache = 0.0
        self._inv_dirty = True  # 增量更新不立即持久化（避免每次序列化整个倒排）；全量重建时才持久化

    def _inv_path(self, store):
        """倒排持久化文件路径。"""
        return store.index_path + ".inv.json"

    def _persist_index(self, store):
        """持久化倒排索引 + TF cache，避免重启后全量重建（省 tokenize CPU）。"""
        try:
            data = {
                "v": 3,
                "fingerprint": self._indexed_fingerprint,
                "active_count": self._indexed_record_count,
                "index_path": store.index_path,
                "inverted_index": {tok: list(docs)
                                   for tok, docs in self._inverted_index.items()},
                "doc_tf_cache": self._doc_tf_cache,
            }
            tmp = self._inv_path(store) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self._inv_path(store))
            self._inv_dirty = False
        except OSError:
            pass  # 持久化失败不影响内存索引正确性

    def _try_load_index(self, store, fp):
        """从持久化倒排加载。成功返回 True（省 tokenize），失败返回 False。"""
        try:
            with open(self._inv_path(store), "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return False
        if data.get("v") not in (2, 3):
            return False
        if data.get("index_path") != store.index_path:
            return False
        if fp is not None and tuple(data.get("fingerprint") or ()) != fp:
            return False  # 文件已变化，持久化过期
        self._inverted_index = {tok: array('I', docs)
                                for tok, docs in data["inverted_index"].items()}
        self._doc_tf_cache = data.get("doc_tf_cache", [])
        # 恢复 token 驻留表（doc_tf 中重复的 token 字符串去重，内存优化）
        self._tok_intern = {}
        for tf in self._doc_tf_cache:
            for t in tf:
                self._tok_intern.setdefault(t, t)
        self._cached_records = [r for r in store.all_records()
                                if not r.get("_corrupt") and r.get("status", "active") != "deleted"]
        self._indexed_record_count = data.get("active_count", len(self._cached_records))
        self._indexed_fingerprint = fp if fp is not None else tuple(data.get("fingerprint") or ())
        self._indexed_store_path = store.index_path
        self._id_map = {r['id']: idx for idx, r in enumerate(self._cached_records)}
        self._idf_cache = None
        self._avg_len_cache = 0.0
        self._superseded_ids = {r['id'] for r in self._cached_records if r.get('verification') in ('superseded', 'outdated')}
        self._conf_weights = [_confidence_weight(r) for r in self._cached_records]
        self._total_chars = sum(len(r.get('content', '')) for r in self._cached_records)
        self._topic_index = {}
        for idx, r in enumerate(self._cached_records):
            for tag in (r.get('tags') or []):
                self._topic_index.setdefault(tag, set()).add(idx)
            topic_tag = r.get('topic_tag') or ''
            if topic_tag:
                self._topic_index.setdefault(topic_tag, set()).add(idx)
        self._inv_dirty = False
        return True

    def retrieve(self, store, query, k=5, layer=None, mtype=None, tag=None,
                 date_from=None, date_to=None, use_vector=True, use_graph=True,
                 multi_hop=False, boost_recency=0.6, candidate_n=500, project=None):
        """5-Way Fusion检索主入口。"""
        self._ensure_index(store)
        records = self._cached_records  # ← 复用 _ensure_index 的缓存，消灭二次全量扫描

        # ---- v7.0.0: 查询 TTL 缓存（相同查询 10 秒内复用，避免重复计算）----
        # key 含 store 指纹与记录数：数据变化时自动失效，避免返回陈旧结果
        cache_key = (query, k, layer, mtype, tag, date_from, date_to, project,
                     use_vector, use_graph, multi_hop, boost_recency, candidate_n,
                     self._indexed_fingerprint, self._indexed_record_count)
        _cache_now = _utcnow_ts()
        _cached = self._query_cache.get(cache_key)
        if _cached is not None and (_cache_now - _cached[0]) < self._query_cache_ttl:
            return list(_cached[1])  # 浅拷贝，避免调用方修改污染缓存

        # 过滤已删除/已合并的记录（v7.0.0）
        records = [r for r in records if r.get("status") in (None, "active")]
        if project:
            records = [r for r in records if r.get("project") == project]
        if layer:
            records = [r for r in records if r.get("layer") == layer]
        if mtype:
            records = [r for r in records if r.get("type") == mtype]
        if tag:
            records = [r for r in records if tag in (r.get("tags") or [])]
        if date_from or date_to:
            records = [r for r in records if _in_date_range(r, date_from, date_to)]

        if not records:
            return []

        # ---- Query Expansion (v7.0.0 Module 1) ----
        # Expand query with synonyms to improve recall on paraphrases
        expanded_queries = _expand_query_terms(query)

        # query token 驻留（与索引 token 同字符串对象，保证倒排/IDF 命中）
        q_tokens = [self._tok_intern.get(t, t) for t in _tokenize(query)]
        q_tf = _tf_vector(q_tokens)
        n = len(records)
        now = _utcnow_ts()

        # ---- Path1: BM25 关键词 ----
        # 轻量模式（sqlite+FTS5）：候选与 BM25 排序均由 FTS5 提供（bm25() 排序 + 排名分数）；
        # 完整模式（JSONL）：内存倒排 + doc_tf 计算 BM25。
        bm25_scores = [0.0] * n
        candidate_indices = set()

        if self._light_index:
            # ---- 轻量模式：FTS5 bm25 取 top candidate_n 候选 → 排名即 BM25 信号 ----
            try:
                # 候选获取只用 (id, rank)，不反序列化完整记录（100k P50 关键）
                search_fn = getattr(store, "search_ids", None) or store.search
                fts_rows = search_fn(query, k=candidate_n,
                                     project=project, mtype=mtype)
                if len(fts_rows) < k:
                    # AND 查询过严 → OR 模式补充召回（同义/部分匹配）
                    try:
                        extra_rows = search_fn(query, k=candidate_n,
                                               project=project, mtype=mtype,
                                               or_mode=True)
                    except Exception:
                        extra_rows = []
                    seen_rids = {rid for rid, _ in fts_rows}
                    for rid, rank in extra_rows:
                        if rid not in seen_rids:
                            fts_rows.append((rid, rank))
                            seen_rids.add(rid)
                if project or layer or mtype or tag or date_from or date_to:
                    id_to_idx = {r["id"]: i for i, r in enumerate(records)}
                else:
                    id_to_idx = self._id_map  # O(1) 复用全量 id→idx 映射
                for rid, rank in fts_rows:
                    i = id_to_idx.get(rid)
                    if i is None:
                        continue
                    bm25_scores[i] = -rank if rank is not None else 0.0
                    candidate_indices.add(i)
                if not candidate_indices:
                    # 与完整模式一致的回退：候选不足时全量候选（如加密内容场景，
                    # FTS 无法命中密文，交由融合路径按其他信号排序）
                    candidate_indices = set(range(n))
            except Exception as exc:
                logger.debug("FTS5 候选获取失败，回退全量候选：%s", exc)
                candidate_indices = set(range(n))
        else:
            # ---- 完整模式（JSONL）：内存倒排索引 BM25 ----
            # _ensure_index 已在 retrieve 入口调用，此处直接用缓存
            doc_tfs = self._doc_tf_cache if self._doc_tf_cache else \
                      [_tf_vector([self._tok_intern.get(t, t)
                                   for t in _tokenize(r.get("content", ""))])
                       for r in records]
            # v5.2: IDF / avg_len 缓存（避免每次检索 O(N) 重算，百万级关键）
            if self._idf_cache is None:
                self._avg_len_cache = sum(sum(t.values()) for t in doc_tfs) / max(len(doc_tfs), 1)
                self._idf_cache = _idf(doc_tfs)
            avg_len = self._avg_len_cache
            idf_dict = self._idf_cache

            # v5.2: 用 Inverted Index 加速 BM25（交集优先：多词 query 大幅减少候选）
            # v7.0.0: Use expanded queries to build a larger candidate set
            for alt_query in expanded_queries[:5]:  # Limit expansion to avoid explosion
                alt_tokens = [self._tok_intern.get(t, t) for t in _tokenize(alt_query)]
                alt_tf = _tf_vector(alt_tokens)

                intersection = None
                for q_tok in alt_tf:
                    postings = self._inverted_index.get(q_tok)
                    if postings is None:
                        continue
                    if intersection is None:
                        intersection = list(postings)
                    else:
                        intersection = _intersect_sorted(intersection, postings)
                        if not intersection:
                            break

                if intersection is not None:
                    candidate_indices.update(intersection)
                else:
                    for q_tok in alt_tf:
                        if q_tok in self._inverted_index:
                            candidate_indices.update(self._inverted_index[q_tok])

            if candidate_indices:
                candidate_indices = {i for i in candidate_indices if i < n}
                # v5.2: 候选过大时（常见词 query），用 IDF 最高的词截断，避免 O(N) 扫描
                if len(candidate_indices) > candidate_n * 10:
                    rarest = max(q_tf.keys(), key=lambda t: idf_dict.get(t, 0))
                    rare_postings = self._inverted_index.get(rarest, array('I'))
                    candidate_indices = {i for i in rare_postings if i < n}
                    # 进一步截断：rarest 词 postings 仍过大时，按该词 TF 取 top candidate_n*2，
                    # 确保 BM25 计算仅在有限候选中进行（避免 10k+ 候选的 BM25 计算，100k 宽泛查询关键）
                    if len(candidate_indices) > candidate_n * 2:
                        candidate_indices = set(
                            sorted(candidate_indices,
                                   key=lambda i: doc_tfs[i].get(rarest, 0),
                                   reverse=True)[:candidate_n * 2]
                        )
                for i in candidate_indices:
                    # Score with original query TF (primary) + expanded queries (secondary)
                    primary_score = _bm25_score(q_tf, doc_tfs[i], idf_dict, avg_len)
                    bm25_scores[i] = primary_score
                    # Add small boost from expanded queries
                    for alt_query in expanded_queries[1:3]:  # Only first 2 expansions
                        alt_tokens = [self._tok_intern.get(t, t)
                                      for t in _tokenize(alt_query)]
                        alt_tf = _tf_vector(alt_tokens)
                        alt_score = _bm25_score(alt_tf, doc_tfs[i], idf_dict, avg_len)
                        if alt_score > primary_score * 0.5:
                            bm25_scores[i] = max(bm25_scores[i], primary_score + alt_score * 0.3)
            else:
                # 回退：全量扫描
                bm25_scores = [_bm25_score(q_tf, t, idf_dict, avg_len) for t in doc_tfs]
                candidate_indices = set(range(n))

        # ---- 粗筛候选（v5.2: heapq 取 top-K，避免全量排序 O(candidate log candidate)）----
        if isinstance(candidate_indices, set):
            if len(candidate_indices) > candidate_n:
                candidate_indices = heapq.nlargest(candidate_n, candidate_indices, key=lambda i: bm25_scores[i])
            else:
                candidate_indices = sorted(candidate_indices, key=lambda i: bm25_scores[i], reverse=True)
        elif n > candidate_n:
            candidate_indices = heapq.nlargest(candidate_n, range(n), key=lambda i: bm25_scores[i])
        else:
            candidate_indices = list(range(n))

        # ---- Path2: 向量语义（随机投影嵌入 / 向量插件） ----
        # P99 优化：仅对已有 embedding 的候选计算向量分数（避免 embedding=None 时
        # 对每个候选重算全文 TF → 10M 规模候选爆炸 P99 67s 的根因）
        vec_scores = [0.0] * n
        if use_vector and self.embed_engine:
            # 传入原始 query 文本（而非 bigram token 列表）：零依赖 EmbeddingEngine
            # 对字符串与 token 列表结果一致；模型类向量插件（numpy_vector）需要
            # 原始文本才能得到正确语义编码（阶段3 发现并修复）。
            q_vec = self.embed_engine.encode(query)
            # Path2a: 向量后端语义候选召回（补充 FTS 关键词遗漏的语义等价记忆）
            # —— 阶段3 修复：此前向量仅对 FTS top-500 候选精排，语义等价但关键词
            # 不同的记忆不在候选池内，向量插件对 Recall@5 零提升。
            if hasattr(self.embed_engine, "search"):
                try:
                    id_to_idx_full = {r["id"]: i for i, r in enumerate(records)}
                    existing = set(candidate_indices)
                    for _mid, _sim in self.embed_engine.search(q_vec, top_k=candidate_n):
                        _i = id_to_idx_full.get(_mid)
                        if _i is not None and _i not in existing:
                            candidate_indices.append(_i)
                            existing.add(_i)
                except Exception as exc:
                    logger.debug("向量后端语义候选召回失败：%s", exc)
            for i in candidate_indices:
                rec = records[i]
                if rec.get("embedding"):
                    vec_scores[i] = self.embed_engine.similarity(q_vec, rec["embedding"])

        # ---- Path3: Knowledge Graph（v3.1 增强：多跳扩展 + 图遍历boost）----
        graph_scores = [0.0] * n
        q_entities = set(_extract_entity_names(query))
        graph_expanded_entities = set(q_entities)  # 扩展后的实体集合
        if use_graph and self.graph_store and self.graph_store.exists:
            # Step A: 从 query 实体出发做图扩展（2跳），finds 关联实体
            for qe in list(q_entities)[:10]:
                try:
                    neighbors = self.graph_store.get_neighbors(qe, max_depth=2)
                    for depth_key in ['depth_1', 'depth_2']:
                        for nb in neighbors.get(depth_key, []):
                            graph_expanded_entities.add(nb)
                except Exception as exc:
                    logger.debug("图邻居扩展失败：%s", exc)
            
            # Step B: 用扩展后的实体集合重新computes 图谱分数
            all_edges = self.graph_store.all_edges()
            adj = {}
            for e in all_edges:
                frm, to = e.get('from',''), e.get('to','')
                if frm not in adj: adj[frm] = set()
                if to not in adj: adj[to] = set()
                adj[frm].add(to); adj[to].add(frm)
            
            for i in candidate_indices:
                r = records[i]
                r_ent = set(r.get("entities") or [])
                # 直接实体重叠
                direct = len(graph_expanded_entities & r_ent)
                graph_scores[i] = direct * 0.5
                # 图Path连接（核心新增：即使无直接重叠，走图Path也能加分）
                if direct == 0 and q_entities:
                    for qe in q_entities:
                        for re_ent in r_ent:
                            if qe in adj and re_ent in adj.get(qe, set()):
                                graph_scores[i] += 0.3  # 1跳邻居 +0.3
                            elif qe in adj:
                                for mid in adj[qe]:
                                    if re_ent in adj.get(mid, set()):
                                        graph_scores[i] += 0.15  # 2跳 +0.15
                                        break
        
        # ---- Path3b: 候选池图扩展（v3.1 新增）----
        # 把图关联但BM25低分的Record也加入候选池
        if use_graph and self.graph_store and self.graph_store.exists and graph_expanded_entities:
            extra_candidates = set()
            for i in range(n):
                if i in candidate_indices: continue
                r = records[i]
                r_ent = set(r.get("entities") or [])
                if graph_expanded_entities & r_ent:
                    extra_candidates.add(i)
            # merges 额外候选（最多追加50）
            candidate_indices = list(candidate_indices) + list(extra_candidates)[:50]
        
        # ---- Path4: Time检索（v3.1 增强：Time上下文感知 + 冲突降权）----
        time_scores = [0.0] * n
        # 从Query中提取Time上下文
        q_time_pattern = re.search(r'(\d{4})年|(\d{4})[/-]|去年|今年|现在|最近|之前|以后|之前说过|后来|先是|后来改成|原来|以前|updates |现在在|搬到|换', query)
        q_time_context = 'recent'  # default: prefer recent
        q_time_year = None
        if q_time_pattern:
            matched = q_time_pattern.group(0)
            if re.search(r'去年', matched): q_time_context = 'past'
            elif re.search(r'现在|今年|搬到|换|现在在', matched): q_time_context = 'recent_update'
            elif re.search(r'之前|原来|以前', matched): q_time_context = 'past'
            elif re.search(r'后来|updates ', matched): q_time_context = 'recent_update'
            year_match = re.search(r'(\d{4})', matched)
            if year_match: q_time_year = int(year_match.group(1))
        
        # 检测被 superseded 的Record（v5.2: 用缓存 O(1) 替代 O(N) 扫描）
        unfiltered = (records is self._cached_records)
        if unfiltered:
            superseded_ids = self._superseded_ids
        else:
            superseded_ids = {r['id'] for r in records if r.get('verification') in ('superseded', 'outdated')}
        
        for i in candidate_indices:
            r = records[i]
            et = r.get("event_time") or r.get("created_at", "")
            base_temporal = _temporal_score(et, now) * boost_recency
            
            # v3.1: Time上下文适配
            if q_time_context == 'recent_update':
                # 偏好"最新"的记忆：越新分越高
                base_temporal *= 1.3
            elif q_time_context == 'past' and q_time_year:
                # 偏好特定年份附近
                try:
                    et_year = int(et[:4]) if et and len(et) >= 4 else None
                    if et_year and abs(et_year - q_time_year) <= 1:
                        base_temporal *= 2.0  # 年份matches 大幅加权
                except (ValueError, TypeError) as exc:
                    logger.debug("时间年份解析失败：%s", exc)
            
            # v3.1: superseded Record降权（Memory Consolidation）
            if r.get('id') in superseded_ids:
                base_temporal *= 0.3
            
            time_scores[i] = base_temporal
        
        # ---- Path5: 可信度加权（v5.2: 缓存 O(1) 替代 O(N) 扫描）----
        if unfiltered:
            conf_weights = self._conf_weights
        else:
            conf_weights = [(_confidence_weight(r) * (0.3 if r.get('id') in superseded_ids else 1.0)) for r in records]
        
        # ---- 五路加权融合（v5.2: 预计算 max，消除 _norm 的 O(candidate×N) 热点）----
        try:
            from .utils import _normalize_template_hash as _norm_hash
        except Exception:
            _norm_hash = lambda x: x
        bm25_max = max(bm25_scores) if bm25_scores else 0.0
        vec_max = max(vec_scores) if vec_scores else 0.0
        graph_max = max(graph_scores) if graph_scores else 0.0
        time_max = max(time_scores) if time_scores else 0.0
        # 语义后端（向量插件，具备 search 语义候选召回）时，向量为主导信号；
        # 随机投影核心保持关键词主导（阶段3 修复：原向量权重仅 0.15-0.20，
        # 高质量语义模型下改写对排不到前位，MRR/NDCG 不达标）
        _semantic_backend = hasattr(self.embed_engine, "search")
        scored = []
        for i in candidate_indices:
            r = records[i]

            n_bm25 = bm25_scores[i] / bm25_max if bm25_max > 0 else 0.0
            n_vec = vec_scores[i] / vec_max if vec_max > 0 else 0.0
            n_graph = graph_scores[i] / graph_max if graph_max > 0 else 0.0
            n_time = time_scores[i] / time_max if time_max > 0 else 0.0
            imp = (r.get("importance") or 3) / 5.0
            access_boost = min(r.get("access_count") or 0, 10) * 0.02
            conf = conf_weights[i]
            if unfiltered and r.get('id') in superseded_ids:
                conf *= 0.3  # superseded 惩罚（缓存 conf 不含此惩罚）

            # v7.0.0: N-gram similarity boost (Module 1)
            # Character-level bigram/trigram overlap catches paraphrases
            ngram_sim = _ngram_boost(query, r)
            ngram_boost_score = ngram_sim * 0.15  # 15% weight for n-gram signal

            # v7.0.0: 同义等价强 boost（归一化后相同 → 改写对，应排在变体干扰项之前）
            syn_equiv = 0.0
            try:
                if _norm_hash(query) == _norm_hash(r.get("content", "")):
                    syn_equiv = 0.5
            except Exception:
                pass

            # v3.1: 图权重提升 + Time感知；语义后端下向量主导，随机投影核心关键词主导
            if _semantic_backend:
                graph_weight = 0.10
                bm25_weight = 0.12
                vec_weight = 0.55 if n_vec > 0.5 else 0.40
            else:
                graph_weight = 0.25 if n_graph > 0.3 else 0.20  # ↑ from 0.15
                bm25_weight = 0.25 if n_bm25 > 0.3 else 0.20    # ↓ from 0.30
                vec_weight = 0.20 if n_vec > 0.3 else 0.15     # ↓ from 0.25
            time_weight = 0.20                              # ↑ from 0.15
            conf_weight = 0.10                              # ↓ from 0.15

            total = (
                n_bm25 * bm25_weight +
                n_vec * vec_weight +
                n_graph * graph_weight +
                n_time * time_weight +
                conf * conf_weight
            ) * (0.6 + 0.4 * imp) + access_boost + ngram_boost_score + syn_equiv

            reasons = []
            if n_bm25 > 0.3: reasons.append("关键词")
            if n_vec > 0.35: reasons.append("语义")
            if n_graph > 0: reasons.append("实体图")
            if n_time > 0.8: reasons.append("近期")
            if conf > 0.9: reasons.append("高可信")
            if ngram_sim > 0.5: reasons.append("N-gram相似")
            if syn_equiv > 0: reasons.append("同义改写")
            if r.get('id') in superseded_ids: reasons.append("已更新")

            scored.append((total, r, reasons))

        scored.sort(key=lambda x: x[0], reverse=True)
        # ---- session 多样性重排（P99优化后新增，提升多证据召回）----
        # 根因：LongMemEval 多答案 session 时，单 session 占满 top-k，其他答案 session 被挤出
        # 方案：top-k 内单 session 最多 max_per_session 条，不足 k 时用后续高分候补
        max_per_session = 2
        top = []
        seen_sessions = {}
        for item in scored:
            rec = item[1]
            sid = rec.get("source") or rec.get("session_id") or rec.get("id", "").split("_")[0]
            if len(top) >= k:
                break
            cnt = seen_sessions.get(sid, 0)
            if cnt < max_per_session:
                top.append(item)
                seen_sessions[sid] = cnt + 1
        # 如果多样性筛选后不足 k（极端情况：所有高分都来自少数 session 且已满），补足
        if len(top) < k:
            used = {item[1].get("source") or item[1].get("session_id") or item[1].get("id", "").split("_")[0] for item in top}
            for item in scored:
                if len(top) >= k:
                    break
                rec = item[1]
                sid = rec.get("source") or rec.get("session_id") or rec.get("id", "").split("_")[0]
                if item not in top and (sid not in used or sum(1 for t in top if (t[1].get("source") or t[1].get("session_id") or t[1].get("id", "").split("_")[0]) == sid) < max_per_session):
                    top.append(item)

        # ---- 多跳推理增强 ----
        if multi_hop and len(top) > 0:
            top = self._multi_hop_enhance(store, query, top, k)

        # ---- v7.0.0: 写入查询缓存（限量，防止无限增长）----
        if len(self._query_cache) > 512:
            self._query_cache = {kk: vv for kk, vv in self._query_cache.items()
                                 if (_cache_now - vv[0]) < self._query_cache_ttl}
        self._query_cache[cache_key] = (_cache_now, top)

        return top

    def _multi_hop_enhance(self, store, query, scored, k):
        """多跳推理：从第一跳Result中抽取实体，再做一 times图扩展检索，merges Result。"""
        first_entities = set()
        for _, rec, _ in scored[:3]:
            for e in (rec.get("entities") or [])[:5]:
                first_entities.add(e)

        if not first_entities:
            return scored

        # 从全库中找与first_entities有实体共现但BM25不高的Record
        all_recs = [r for r in store.all_records()
                    if not r.get("_corrupt") and r.get("status") != "deleted"]
        scored_ids = {s[1]["id"] for s in scored}

        hop2 = []
        for r in all_recs:
            if r["id"] in scored_ids:
                continue
            r_ent = set(r.get("entities") or [])
            overlap = len(first_entities & r_ent)
            if overlap > 0:
                hop_score = overlap * 0.3 * (_confidence_weight(r)) * (r.get("importance", 3) / 5.0)
                hop2.append((hop_score, r, ["多跳推理"]))

        hop2.sort(key=lambda x: x[0], reverse=True)
        # merges ：原有 top-k 的80%位置 + hop2 的top-2
        combined = scored[:max(k - 2, k // 2)] + hop2[:2]
        combined.sort(key=lambda x: x[0], reverse=True)
        return combined[:k]


def _in_date_range(r, date_from, date_to):
    ts = r.get("created_at", "")
    if date_from and ts < date_from:
        return False
    if date_to and ts > date_to:
        return False
    return True


# ============================================================================
# Query Expansion & N-gram Similarity (v7.0.0 Module 1)
# ============================================================================

# Synonym dictionary for zero-dependency query expansion
_SYNONYM_GROUPS = [
    # Establishment / founding
    ["成立", "创建", "建立", "创立", "开创", "发起"],
    # Closure
    ["倒闭", "破产", "关闭", "解散", "歇业"],
    # Weather
    ["天气", "气候", "气温", "温度"],
    # Product / company
    ["产品", "商品", "货物"],
    ["公司", "企业", "厂商", "机构"],
    # Sentiment
    ["很好", "不错", "非常好", "棒", "优秀"],
    ["喜欢", "爱", "喜好", "喜爱"],
    ["讨厌", "憎恶", "厌恶", "不喜欢"],
    # Common verbs
    ["查找", "搜索", "寻找", "检索"],
    ["介绍", "描述", "讲述", "概述"],
    ["发布", "推出", "上市"],
    ["拥有", "具有", "持有"],
    ["发现", "查出", "找到"],
    # Common nouns
    ["信息", "消息", "讯息", "资讯"],
    ["新闻", "消息", "资讯", "报道"],
    ["行星", "星球", "天体"],
    ["八大", "八颗", "八个"],
    ["描述", "描绘", "形容"],
    ["研究", "探索", "调查"],
    ["来源", "来自", "起源"],
    ["迁移", "移动", "迁徙"],
    ["感染", "传染", "传播"],
    # Structural
    ["首都", "京城"],
    ["分支", "子类", "领域"],
    ["周期", "时期", "阶段"],
    ["化学式", "分子式"],
    ["行为", "表现", "特征"],
    ["提出", "发现", "创立"],
]

# Build reverse lookup: term -> set of synonyms
_SYNONYM_LOOKUP = {}
for _group in _SYNONYM_GROUPS:
    for _term in _group:
        _SYNONYM_LOOKUP.setdefault(_term, set()).update(_group)


def _expand_query_terms(query):
    """Expand a query string into a set of additional search terms using synonyms.
    
    Returns a list of alternative query strings (including the original).
    """
    expanded = [query]
    for _term, _syns in _SYNONYM_LOOKUP.items():
        if _term in query:
            for _syn in _syns:
                if _syn != _term and _syn not in query:
                    alt = query.replace(_term, _syn)
                    if alt not in expanded:
                        expanded.append(alt)
    return expanded


def _bigram_similarity(text1, text2):
    """Compute character bigram similarity between two strings (0-1).
    
    Uses Jaccard similarity on character bigrams, which works well
    for Chinese text without word segmentation.
    """
    if not text1 or not text2:
        return 0.0
    
    def _bigrams(text):
        text = text.lower().strip()
        return set(text[i:i+2] for i in range(len(text) - 1))
    
    b1, b2 = _bigrams(text1), _bigrams(text2)
    if not b1 or not b2:
        return 0.0
    
    intersection = len(b1 & b2)
    union = len(b1 | b2)
    return intersection / union if union > 0 else 0.0


def _compute_pair_similarity(record_a, record_b):
    """Compute similarity between two records using multiple signals.
    
    Combines:
    1. Character bigram Jaccard similarity (primary signal for CJK)
    2. Token overlap ratio
    3. Synonym-aware matching
    """
    content_a = record_a.get("content", "")
    content_b = record_b.get("content", "")
    
    # Signal 1: Bigram Jaccard
    bigram_sim = _bigram_similarity(content_a, content_b)
    
    # Signal 2: Token overlap
    tokens_a = set(_tokenize(content_a))
    tokens_b = set(_tokenize(content_b))
    if tokens_a and tokens_b:
        token_sim = len(tokens_a & tokens_b) / min(len(tokens_a), len(tokens_b))
    else:
        token_sim = 0.0
    
    # Signal 3: Synonym-aware matching
    # Check if tokens from A match synonyms of tokens from B
    syn_matches = 0
    for tok_a in tokens_a:
        if tok_a in tokens_b:
            continue
        syns = _SYNONYM_LOOKUP.get(tok_a, set())
        if syns & tokens_b:
            syn_matches += 1
    if tokens_a:
        syn_sim = syn_matches / len(tokens_a)
    else:
        syn_sim = 0.0
    
    # Weighted combination
    similarity = 0.5 * bigram_sim + 0.3 * token_sim + 0.2 * syn_sim
    return min(similarity, 1.0)


def _ngram_boost(query, record, max_grams=50):
    """Compute character n-gram (bigram + trigram) similarity boost.
    
    Returns a score in [0, 1] based on character-level n-gram overlap
    between the query and the record content. This catches paraphrases
    that share character sequences even when word-level matching fails.
    """
    content = record.get("content", "")
    if not content:
        return 0.0
    
    q_lower = query.lower()
    c_lower = content.lower()
    
    # Character bigrams
    q_bi = set(q_lower[i:i+2] for i in range(len(q_lower) - 1))
    c_bi = set(c_lower[i:i+2] for i in range(len(c_lower) - 1))
    
    # Character trigrams
    q_tri = set(q_lower[i:i+3] for i in range(len(q_lower) - 2))
    c_tri = set(c_lower[i:i+3] for i in range(len(c_lower) - 2))
    
    bi_overlap = len(q_bi & c_bi) / len(q_bi) if q_bi else 0.0
    tri_overlap = len(q_tri & c_tri) / len(q_tri) if q_tri else 0.0
    
    return 0.6 * bi_overlap + 0.4 * tri_overlap


# ============================================================================
# Part 7: Cognitive Resolver（Cognitive Resolver v3.1）
# ============================================================================
# 四规则引擎模块，在检索Result和答案之间做认知加工。
# 不依赖 LLM，纯规则 + 检索上下文推理。


