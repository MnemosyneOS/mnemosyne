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
#
# 注意：轻量记录同时是「对外输出」的数据源 —— MCP 的 recall 投影直接读这份
# dict。所以这里少一个键，对应字段在工具返回里就会**静默变成 null/默认值**，
# 而且没有任何报错。历史上就踩过两次：
#   - verification（已补）：少了它，模型分不清哪条说法已被取代；
#   - superseded_by / version / flags（7.0.1 勘误补）：`superseded_by` 恒为 null，
#     尽管库里该列有值；`version` 恒为 1，`flags` 恒为 []。
# 三个字段都是小标量（None / int / 短列表），对内存的影响可忽略（大头的
# content 本来就在清单里），因此并入白名单而不是在输出层回查数据库。
_RECORD_KEYS = (
    "id", "content", "type", "entities", "tags", "confidence", "importance",
    "tier", "status", "project", "layer", "fact_type", "verification",
    "event_time", "created_at", "access_count", "last_accessed_at",
    "session_id", "embedding", "topic_tag", "meta",
    "version", "superseded_by", "flags",
)

# 与 _RECORD_KEYS 对应的 sqlite 列名白名单（type←mtype、meta←template_hash）。
# 轻量模式重建索引时按此列清单直接物化轻量记录，
# 跳过完整记录 dict 的双份驻留（100k 内存关键优化）。
# 新增字段时必须与 _RECORD_KEYS 同步，否则 SQL 少选一列、输出侧又变回 null。
_LIGHT_COLUMNS = (
    "id", "content", "mtype", "entities", "tags", "confidence", "importance",
    "tier", "status", "project", "layer", "fact_type", "verification",
    "event_time", "created_at", "access_count", "last_accessed_at",
    "session_id", "embedding", "topic_tag", "template_hash",
    "version", "superseded_by", "flags",
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
    """5-Way Fusion 检索引擎（v3.0 倒排索引加速版）。

    五路：BM25 关键词 + 随机投影向量 + 知识图谱 + 时间衰减 + 可信度加权
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
        # v7.0.2 (P2-2): 最近一次 retrieve 的质量指标（brain 侧累积成 recall_health）
        self.last_recall_metrics = {}
        # v7.0.2 (P0-2 修正)：上一次打分写在记录 dict 上的置信带引用。
        # 【为什么需要】`_cached_records` 里的 dict 是**跨调用共享**的（同一批对象
        # 每次检索都复用，这正是索引加速的关键）。而 `_relevance_band` 是"这一轮
        # 查询对这个记录"的判断，必须挂在 dict 上才能被 MCP 输出层读到（结果元组
        # 是三元素，cli.py/web_server.py 有精确解包，不能扩）。两者叠加就产生别名
        # 缺陷：调用方拿到结果 A，随后又发了一次**无关**查询 B，B 的打分把这批共享
        # dict 全部标成 low —— 调用方手里的 A 结果置信带被静默改写（实测复现：
        # 相关查询返回的 3 条全部变成 band=low、_relevance=0.0）。
        # 修法：每轮打分前清掉上一轮打过标记的记录，保证"标记只属于最近一次调用"。
        # 代价 O(k)（标记数 = 候选数上限），不进任何热循环。
        self._band_marked = []

    # ---- v7.0.2 (P0-2 修正)：置信带标记的跨调用隔离 ----

    def _clear_band_marks(self):
        """清掉上一轮写在共享记录 dict 上的置信带标记（O(上一轮候选数)）。"""
        marks = self._band_marked
        if not marks:
            return
        for _r in marks:
            try:
                _r.pop("_relevance_band", None)
                _r.pop("_relevance", None)
            except (AttributeError, TypeError):
                pass
        self._band_marked = []

    def _apply_bands(self, items, bands):
        """把缓存里的置信带重新贴回记录（查询缓存命中路径用）。

        【为什么缓存命中也要贴】缓存条目里的记录 dict 与 `_cached_records` 是同一批
        对象；在此期间任何一次打分都会先 `_clear_band_marks()` 把它们清空。若命中
        缓存时不重贴，调用方就会看到 `confidence_band=None`（等于能力静默消失）。
        """
        if not bands:
            return
        marked = []
        for it in items:
            rec = it[1] if isinstance(it, (tuple, list)) and len(it) > 1 else None
            if not isinstance(rec, dict):
                continue
            b = bands.get(rec.get("id"))
            if b:
                rec["_relevance_band"] = b
                marked.append(rec)
        self._band_marked = marked

    @staticmethod
    def _snapshot(items):
        """输出边界快照：把共享记录 dict 复制一份再交给调用方。

        【为什么必须复制】`_cached_records` 里的 dict 是索引的一部分，**跨调用共享**
        （这正是倒排索引能把检索压到 O(q·log n) 的原因）。而 MCP/CLI 的输出层是
        "拿到结果 → 稍后再读字段"，两者叠加就会出现**别名污染**：调用方持有结果 A，
        之后又发了一次无关查询 B，B 的打分把同一批共享 dict 改写成
        `band=low / _relevance=0.0` —— 调用方手里的 A 结果被静默篡改（实测复现）。
        复制一份（k≤20，微秒级）就把"索引对象"与"输出对象"彻底分开，
        顺带让"本次调用的结果"成为不可变快照。
        """
        out = []
        for it in items:
            try:
                s, r, *rest = it
            except (TypeError, ValueError):
                out.append(it)
                continue
            out.append((s, dict(r) if isinstance(r, dict) else r, *rest))
        return out

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
                 multi_hop=False, boost_recency=0.6, candidate_n=500, project=None,
                 tags=None, apply_floor=True):
        """5-Way Fusion 检索主入口。

        结构化过滤：
        - mtype: 单类型过滤（如 "preference"）
        - tag:   单标签过滤
        - tags:  多标签 OR 过滤（命中任一 tag 即保留；与 mtype 为 AND 关系）。
          兼容 tag 传单值/列表：tag 为列表时按 tags 语义处理。
        - apply_floor: 是否应用 v7.0.2 的**相关性下限**（默认 True）。
          【为什么需要这个开关】"下限"解决的是"**回答问题**时别把无关记忆当证据
          塞给模型"。但另有一类调用是"**定位目标**"—— forget / update / dedup
          要问"用户说的那条记忆是哪条"，此时判据完全不同：
            • 用户的描述天然含糊（"把那个 bge 维度的事忘掉"），语义距离本来就远；
            • 返回空 = 操作直接失败，而返回"最像的几条 + 分数"再由人确认
              （所有目标解析都走 dry_run 先行）才是正确姿势。
          同一个阈值套两种语义必然有一边是错的，所以把它做成显式参数而不是
          隐式全局行为，并写进查询缓存键避免两种结果互相污染。
        """
        self._ensure_index(store)
        records = self._cached_records  # ← 复用 _ensure_index 的缓存，消灭二次全量扫描

        # ---- 归一化 tag/tags（单值或列表均支持，多 tag 为 OR 语义）----
        tag_list = []
        for _val in (tag, tags):
            if _val is None:
                continue
            if isinstance(_val, (list, tuple)):
                tag_list.extend(t for t in _val if t)
            elif _val:
                tag_list.append(_val)
        tag_set = set(tag_list)

        # ---- v7.0.0: 查询 TTL 缓存（相同查询 10 秒内复用，避免重复计算）----
        # key 含 store 指纹与记录数：数据变化时自动失效，避免返回陈旧结果
        cache_key = (query, k, layer, mtype, tag, date_from, date_to, project,
                     use_vector, use_graph, multi_hop, boost_recency, candidate_n,
                     tuple(sorted(tag_set)), bool(apply_floor),
                     self._indexed_fingerprint, self._indexed_record_count)
        _cache_now = _utcnow_ts()
        _cached = self._query_cache.get(cache_key)
        if _cached is not None and (_cache_now - _cached[0]) < self._query_cache_ttl:
            _hit = list(_cached[1])  # 浅拷贝，避免调用方修改污染缓存
            # v7.0.2 (P0-2 修正)：重贴置信带（记录 dict 的标记可能已被后续打分清掉）
            self._apply_bands(_hit, _cached[2] if len(_cached) > 2 else None)
            self.last_recall_metrics = _recall_metrics(
                query, k, None, None, _hit, 0, cache_hit=True,
                sem_stats=(_cached[3] if len(_cached) > 3 else None))
            return self._snapshot(_hit)

        # 过滤已删除/已合并的记录（v7.0.0）
        records = [r for r in records if r.get("status") in (None, "active")]
        if project:
            records = [r for r in records if r.get("project") == project]
        if layer:
            records = [r for r in records if r.get("layer") == layer]
        if mtype:
            records = [r for r in records if r.get("type") == mtype]
        if tag_set:
            # 多标签 OR：记录的 tags 与 tag_set 有交集即保留
            records = [r for r in records
                       if set(r.get("tags") or []) & tag_set]
        if date_from or date_to:
            records = [r for r in records if _in_date_range(r, date_from, date_to)]

        if not records:
            self.last_recall_metrics = _recall_metrics(query, k, 0, 0, [], 0)
            return []

        # ---- Query Expansion (v7.0.0 Module 1) ----
        # Expand query with synonyms to improve recall on paraphrases
        expanded_queries = _expand_query_terms(query)

        # query token 驻留（与索引 token 同字符串对象，保证倒排/IDF 命中）
        q_tokens = [self._tok_intern.get(t, t) for t in _tokenize(query)]
        q_tf = _tf_vector(q_tokens)
        # v7.0.2 (P0-2)：词面兜底用的"查询内容词"（剥掉泛问词与单字噪声）
        q_content_tokens = {t for t in q_tokens if _is_content_token(t)}
        n = len(records)
        now = _utcnow_ts()

        # ---- Path6: tags 自动命中通道（v7.0.2 混合检索默认行为）----
        # 设计目标：即使调用方不显式传 tags，也按 query 里的实体词自动匹配
        # 全库 tags，命中记录直接进候选并给高分 boost，结构性保证"问荔枝大窑
        # 必中 tags=汽水/大窑"的记忆不被 Top-K 挤占。
        # 触发条件：query 非纯泛词（含 2+ CJK 实体字符或 1+ 英文词）且
        # 调用方未显式传 tag/tags（显式传了走过滤通道，不叠加自动通道避免双重收窄）。
        tag_boost = {}   # idx → boost 分数
        tag_hit_reasons = {}  # idx → 命中的 tag 描述
        if not tag_set:
            auto_tag_hits = _auto_tag_match(query, records, self._topic_index)
            for idx, hit_tags in auto_tag_hits.items():
                tag_boost[idx] = 0.30
                tag_hit_reasons[idx] = "tags:" + "/".join(hit_tags[:3])

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
                    # 无 FTS 命中时：保持候选池为空（score 全 0），
                    # 让下游 Path2a 向量通道 / Path3b 图通道自行补候选。
                    # 若 records 已经过 tag/mtype/project 过滤，全量回退会把
                    # 被过滤掉的记录重新放进候选，绕过结构化过滤——
                    # 因此绝不在该场景下全量回退。
                    _any_filter = bool(project or layer or mtype or tag_set
                                       or date_from or date_to)
                    if _any_filter:
                        candidate_indices = set()
                        bm25_scores = [0.0] * n
                    else:
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

            else:
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
                    # 无候选（query token 全部未命中倒排索引）。
                    # 若已启用结构化过滤，必须保持候选为空——全量回退会把
                    # 被 tag/mtype 过滤掉的记录重新放进候选，绕过过滤。
                    # 未过滤时回退全量扫描（保持旧行为）。
                    if project or layer or mtype or tag_set or date_from or date_to:
                        candidate_indices = set()
                    else:
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
                    # 混合检索第二路：向量通道透传结构化过滤（tags OR / mtype）。
                    # 仅当向量插件的 search 支持 tags/mtype kwargs 时传递
                    # （Qdrant 插件支持；内置随机投影 / numpy_vector 不支持，
                    # 此时靠 retrieve 入口的 Python 记录过滤兜底，行为不变）。
                    _search_kw = {}
                    if tag_set:
                        _search_kw["tags"] = list(tag_set)
                    if mtype:
                        _search_kw["mtype"] = mtype
                    # 能力探测：插件 search 接受 tags/mtype kwargs 才透传；
                    # 内置随机投影 / numpy_vector 的 search 不接受 → TypeError，
                    # 退化纯向量召回（行为不变，靠 retrieve 入口记录过滤兜底）。
                    _probe = None
                    if _search_kw:
                        try:
                            _probe = self.embed_engine.search(q_vec, top_k=candidate_n,
                                                             **_search_kw)
                        except TypeError:
                            _probe = None
                    if _probe is None:
                        _probe = self.embed_engine.search(q_vec, top_k=candidate_n)
                    for _mid, _sim in _probe:
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

        # ---- Path6 候选补入：tags 自动命中的记录必须进候选池，否则 boost 无效 ----
        if tag_boost:
            _existing = set(candidate_indices)
            for _idx in tag_boost:
                if _idx < n and _idx not in _existing:
                    candidate_indices.append(_idx)
                    _existing.add(_idx)

        # ---- Path3: Knowledge Graph（v3.1 增强：多跳扩展 + 图遍历boost）----
        # v7.0.2（P1-1）修复三处，使图通道真正参与打分（原先 0.10 权重长期空转）：
        #   ① 查询侧代词归一：`我/我的` → `用户`（记忆以第三人称书写）；
        #   ② 记录侧节点 = entities ∪ graph_edges 端点（含 qualifier），
        #      并用包含式匹配（`RTX` 与 `RTX 4090` 视为同一实体）；
        #   ③ 多跳改为在归一化后的邻接图上展开，而不是 `qe in adj` 的精确命中。
        # 邻居扩展结果一次性预计算，避免在候选循环里做 O(候选 × 邻居) 的重复匹配。
        graph_scores = [0.0] * n
        q_entities = set(_extract_entity_names(query))
        for _pron, _ent in _QUERY_PRONOUN_ENTITIES.items():
            if _pron in query:
                q_entities.add(_ent)
        graph_expanded_entities = set(q_entities)  # 扩展后的实体集合
        _rec_nodes_by_id = {}   # memory_id → 该记录在图里的节点集合（v7.0.2 D8）
        _hop1_nodes, _hop2_nodes = set(), set()
        if use_graph and self.graph_store and self.graph_store.exists:
            try:
                all_edges = self.graph_store.all_edges()
            except Exception as exc:
                logger.debug("图边读取失败：%s", exc)
                all_edges = []
            adj = {}
            for e in all_edges:
                frm, to = e.get('from', ''), e.get('to', '')
                if not frm or not to:
                    continue
                adj.setdefault(frm, set()).add(to)
                adj.setdefault(to, set()).add(frm)
                # v7.0.2 (D8b)：**限定词也是图节点**。
                # `用户的显卡是 RTX 4090` 抽出的边是
                # `用户 --is_a--> RTX 4090 (qualifier=显卡)` —— 属性名 `显卡` 只出现在
                # qualifier 里。若邻接表只用 from/to 建，`显卡` 就**不是**任何节点，
                # 用户问"我用的什么显卡"时它在图里根本不存在，图通道无法区分
                # `显卡`（精确）与 `备用显卡`（包含）。把 qualifier 作为连接
                # 「主体 ↔ 取值」的中间节点加入，属性反问才能被图通道看见。
                _ql = e.get("qualifier")
                if _ql:
                    _ql = str(_ql)
                    adj.setdefault(_ql, set()).add(frm)
                    adj.setdefault(_ql, set()).add(to)
                    adj.setdefault(frm, set()).add(_ql)
                    adj.setdefault(to, set()).add(_ql)
                # v7.0.2 (D8)：按 memory_id 建"这条记录在图里有哪些节点"的映射。
                # 【为什么要从边表取而不是从 record["entities"] 取】两条硬理由：
                #   ① `graph_edges` 不在 `_RECORD_KEYS` 轻量记录白名单里（它是
                #      dict 列表，物化到 100k 记录代价高、SQLite 也没有该列）——
                #      于是 `_record_graph_nodes()` 实际**只能看到 entities**，
                #      P1-1 声称的"含 qualifier 端点"从未真正生效；
                #   ② entities 是**中文滑窗切分**的产物（`[\u4e00-\u9fff]{2,6}`），
                #      同一句话切出什么碎片高度依赖字数对齐 —— 实测
                #      `用户的备用嵌入模型…` 恰好切出 `入模型` 从而匹配上查询，
                #      而 `用户的嵌入模型是 bge-m3` 切出的是 `用户的嵌入模`+`型`，
                #      反而**匹配不上**。这不是"信号弱"，是**抽签**：
                #      正确答案因为多了一个字而丢掉图通道的分。
                #   边表带 memory_id，天然给出"这条记录连了哪些节点"，精确且对称。
                _mid = e.get("memory_id")
                if _mid:
                    ns = _rec_nodes_by_id.setdefault(_mid, set())
                    ns.add(frm)
                    ns.add(to)
                    _ql = e.get("qualifier")
                    if _ql:
                        ns.add(str(_ql))

            # Step A: 查询实体（含代词归一）→ 归一化匹配到图节点 → 展开 2 跳
            # v7.0.2 (D8b)：播种方式改为"**扫图节点，问它是否出现在查询里**"，
            # 而不是"扫查询实体，问它是否长得像某个节点"：
            #   • 旧方向依赖 `_extract_entity_names` 的**中文滑窗**产物，而滑窗会
            #     把词切碎（`我用的什么显卡` → `我用的什么显`+`卡`，`显卡` 消失）；
            #   • 新方向是 `key in query`，只要图里有 `显卡` 这个节点，查询里出现
            #     `显卡` 就能对上一一 与分词无关，且天然对短查询更稳。
            # 代价同为 O(E)（本来就要遍历 all_edges 建邻接表），远优于
            # 旧的 O(|查询实体| × |图节点|) 嵌套包含判断。
            _seed_nodes = set()
            for _key in adj:
                if not _key:
                    continue
                if _key in query:
                    _seed_nodes.add(_key)
            _seed_nodes |= _match_adj_nodes(q_entities, adj)
            for s in _seed_nodes:
                _hop1_nodes |= adj.get(s, set())
            for h in _hop1_nodes:
                _hop2_nodes |= adj.get(h, set())
            _hop2_nodes -= _hop1_nodes
            graph_expanded_entities |= _seed_nodes | _hop1_nodes | _hop2_nodes

            for i in candidate_indices:
                rid = records[i].get("id")
                ns = _rec_nodes_by_id.get(rid)
                # 边表里没有这条记录（例如尚未回填的存量数据）→ 退化为实体串匹配，
                # 并**降权一半**：模糊信号不该与结构信号同权。
                _fuzzy = False
                if not ns:
                    ns = _record_graph_nodes(records[i])
                    _fuzzy = True
                if not ns:
                    continue
                _w = 0.5 if _fuzzy else 1.0
                # v7.0.2 (D8c)：**直接匹配只算"查询自己提到的节点"**
                # (`_seed_nodes ∪ q_entities`)，不再把 2 跳可达集合塞进来。
                # 【为什么】旧实现用 `graph_expanded_entities`（= 查询实体 ∪ 1 跳 ∪ 2 跳）
                # 做直接匹配，而 `用户` 是几乎每条记忆都连的**枢纽节点** ——
                # 于是 hop1/hop2 覆盖了全库节点，"直接匹配"退化成
                # "你是不是也连着这个库里的某样东西"，几乎所有记录都拿到 1~2 分，
                # 图通道彻底失去区分力（实测：8 条候选**全部**标成"实体图"）。
                # 跳达改为**独立的弱加分**，只在无直接匹配时才给。
                direct = _node_overlap(_seed_nodes | q_entities, ns)
                graph_scores[i] = direct * 0.5 * _w
                if direct == 0:
                    # 图路径连接：即使无直接重叠，1 跳 / 2 跳可达也加分
                    if _hop1_nodes and _node_overlap(_hop1_nodes, ns):
                        graph_scores[i] += 0.30 * _w
                    elif _hop2_nodes and _node_overlap(_hop2_nodes, ns):
                        graph_scores[i] += 0.15 * _w
        
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
            # ---- v7.0.2 (D10)：时间先验改用「记忆自身的时效」，不再用正文里抽的 event_time ----
            # 【旧行为】`et = event_time or created_at`，而 `event_time` 是
            # `_extract_event_time(content, ...)` 从**正文里抽出来的日期**。
            # 于是时间通道实际在惩罚"正文里带日期的记忆"：
            #   `用户的显卡是 RTX 4090，购于 2026-03-15`  → event_time=2026-03-15
            #        → 距今约 184 天 → 衰减 0.5^(184/90) ≈ 0.24
            #   `用户的备用显卡是 RTX 3060`（无日期）      → 回落到 created_at=今天 → 1.00
            # 结果：**记得越具体（连日子都写了）反而被扣分**，而且 time_weight=0.20
            # 是向量通道权重 0.55 的 1/3 —— 补偿一个 0.02 的余弦差绰绰有余。
            # 实测（真实 Qdrant + BGE-M3）：`我用的什么显卡` 的正确答案被"备用显卡"
            # 与"显示器"挤到 rank3，而它的语义相似度其实**更高**（0.7075 > 0.6402）。
            # 这是"越准确越排后面"的**系统性反向激励**，必须修。
            # 【新行为】默认用 `updated_at → created_at`（记忆**何时被写下**）；
            # 只有查询**显式**带时间意图（去年/以前/最近…）时，才把 event_time 纳入 ——
            # 那时"事件何时发生"确实才是用户要的东西，也是时间通道的本来用途。
            _recency = r.get("updated_at") or r.get("created_at") or ""
            base_temporal = _temporal_score(_recency, now) * boost_recency
            et = r.get("event_time") or _recency

            # v3.1: Time上下文适配
            if q_time_context == 'recent_update':
                # 偏好"最新"的记忆：越新分越高
                base_temporal *= 1.3
            elif q_time_context == 'past' and q_time_year:
                # 偏好特定年份附近（此处**应当**用 event_time：用户在问"那次是哪年"）
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
        # v7.0.2：语义背景统计（供 recall_health 判断绝对阈值是否仍适用于本库）。
        # 只统计真正跑过语义通道的候选（vec_scores 对无 embedding 的记录留 0）。
        _sem_vals = sorted(v for v in (vec_scores[i] for i in candidate_indices) if v > 0)
        _sem_stats = {
            "top1": round(_sem_vals[-1], 4) if _sem_vals else None,
            "background": round(_sem_vals[len(_sem_vals) // 2], 4) if _sem_vals else None,
        }
        # 语义后端（向量插件，具备 search 语义候选召回）时，向量为主导信号；
        # 随机投影核心保持关键词主导（阶段3 修复：原向量权重仅 0.15-0.20，
        # 高质量语义模型下改写对排不到前位，MRR/NDCG 不达标）
        _semantic_backend = hasattr(self.embed_engine, "search")
        # v7.0.2 (P0-2 修正)：清掉上一轮的置信带标记，避免"后一次查询改写前一次
        # 已返回结果的 band"（详见 __init__ 里 `_band_marked` 的说明）。
        self._clear_band_marks()
        scored = []
        for i in candidate_indices:
            r = records[i]

            n_bm25 = bm25_scores[i] / bm25_max if bm25_max > 0 else 0.0
            n_vec = vec_scores[i] / vec_max if vec_max > 0 else 0.0
            n_graph = graph_scores[i] / graph_max if graph_max > 0 else 0.0
            n_time = time_scores[i] / time_max if time_max > 0 else 0.0
            imp = (r.get("importance") or 3) / 5.0
            # ---- v7.0.2 (P0-1): access_boost 由「加性 + 上限 0.20」改为「乘性 + 上限 5%」----
            # 旧行为 `min(access_count, 10) * 0.02`：加性、上限 +0.20，与一个完整通道
            # 权重（0.10~0.55）同量级；而 access_count 由 `_touch_recalled()` 在每次召回后
            # 对"进入结果列表"的记录累加 —— 不问它是否真的是用户要的那条。
            # 实测（对照实验）：同一批记忆、同一句提问，仅因"先前问过 6 个无关问题"，
            # 干扰项分数 +0.12（= 6 × 0.02，与公式逐位吻合），把正确答案从 rank1 挤到 rank3。
            # 会话越长偏置越强 —— 这是"长会话越聊越偏"的直接机制，且上下文再大也救不了
            # （问题在打分层，不在上下文窗口）。
            # 新行为：乘性且封顶 +5%，数学上不可能越级反超；计数语义同步收窄，
            # 见 `brain._touch_recalled()`：只有"高分命中"才 +1。
            access_boost = 1.0 + 0.01 * min(int(r.get("access_count") or 0), 5)
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
            ) * (0.6 + 0.4 * imp) * access_boost + ngram_boost_score + syn_equiv
            # Path6: tags 自动命中通道（query 实体词命中记录 tags → 强 boost，
            # 结构性保证精准记忆不被 Top-K 挤占；即使 FTS5/向量都没排到前位也进）
            total += tag_boost.get(i, 0.0)

            # ---- v7.0.2 (P0-2): 相关性下限判定（未归一化信号，见 Part 6.4 注释）----
            # 通过条件（OR）：Path6 结构性命中 / 同义等价 / n-gram 字面重合 /
            #                原始 cosine ≥ REL_MIN_SEMANTIC / 词面内容词有交集。
            # 未通过者标 low，排序后统一剔除 —— 允许返回空结果：对 Agent 而言
            # "没找到相关记忆"比"给 5 条无关记忆"安全得多（旧行为是 100% 返回满 5 条，
            # 且无关查询 top1 可达 1.0200，高于相关查询的 0.6922）。
            raw_sem = vec_scores[i] if (use_vector and self.embed_engine) else 0.0
            band = "high" if (syn_equiv > 0 or raw_sem >= REL_HIGH_SEMANTIC
                              or ngram_sim >= REL_HIGH_SEMANTIC) else "medium"
            passes = (bool(i in tag_boost) or syn_equiv > 0
                      or ngram_sim >= REL_MIN_NGRAM or raw_sem >= REL_MIN_SEMANTIC)
            if not passes:
                # 词面兜底（零依赖）：标签词与查询同形、或内容内容词有交集。
                # 只在不通过前几项时才付分词成本（慢路径）。
                _rt = {t for t in _tokenize(r.get("content", "") or "")
                       if _is_content_token(t)}
                for _tg in (r.get("tags") or []):
                    _tg = str(_tg).strip().lower()
                    if _tg:
                        _rt.add(_tg)
                if q_content_tokens and (q_content_tokens & _rt):
                    passes = True
                    band = "medium"
                else:
                    band = "low"
            # 挂在记录 dict 上（而不是加长结果元组）：cli.py / web_server.py 里有
            # 精确的 `for score, rec, reasons in results` 三元组解包，扩元组会直接炸。
            # 写库走 `_MEMORIES_COLUMNS` 白名单，多余键不会被持久化。
            r["_relevance_band"] = band
            r["_relevance"] = round(raw_sem, 4)
            # 登记本轮标记，供下一次打分前清理（v7.0.2 P0-2 修正）。
            # 上限 5000：候选数不会超过这个量级（candidate_n 通常 20~200），
            # 设上限纯粹是防"有人把 candidate_n 调到 10 万"时标记表无限膨胀。
            if len(self._band_marked) < 5000:
                self._band_marked.append(r)

            reasons = []
            if n_bm25 > 0.3: reasons.append("关键词")
            if n_vec > 0.35: reasons.append("语义")
            if n_graph > 0: reasons.append("实体图")
            if n_time > 0.8: reasons.append("近期")
            if conf > 0.9: reasons.append("高可信")
            if ngram_sim > 0.5: reasons.append("N-gram相似")
            if syn_equiv > 0: reasons.append("同义改写")
            if r.get('id') in superseded_ids: reasons.append("已更新")
            if i in tag_hit_reasons: reasons.append(tag_hit_reasons[i])
            if band == "low": reasons.append("低于相关性下限")

            scored.append((total, r, reasons))

        scored.sort(key=lambda x: x[0], reverse=True)
        # ---- v7.0.2 (P0-2): 相关性下限过滤（P0-2 的核心，见上方判定段）----
        # 位置：排序之后。过滤器只剔除输出项，不动候选下标 —— records /
        # _doc_tf_cache / 倒排 posting 必须同序同长，候选阶段增删会让下标错位。
        _n_before_floor = len(scored)
        if apply_floor:
            scored = [it for it in scored if it[1].get("_relevance_band") != "low"]
        _floor_dropped = _n_before_floor - len(scored)
        # ---- v7.0.0-MCP：置信度归零 = 已撤回（"忘记"的语义），输出前硬过滤 ----
        # 为什么放在这个位置：候选池的构建、doc_tf / 倒排 / _conf_weights 都按下标
        # 与 _cached_records 对齐（records 与 _doc_tf_cache 必须同序同长），在候选阶段
        # 增删元素会让下标错位、检索串味。放在打分排序之后只剔除输出项，零对齐风险。
        # 为什么需要它：forget 走的是"置信度 0 + status=deleted"双保险，status 过滤
        # 已能兜住绝大多数情况；这条是给"调用方显式写入 confidence=0"这类撤回语义
        # 兜底，确保被撤回的记忆在任何情况下都不会被交回给调用方。
        scored = [it for it in scored
                  if (it[1].get("confidence")
                      if it[1].get("confidence") is not None else 0.7) > 0]
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
        # 缓存条目：第 3 位 = 本轮置信带快照（命中时重贴），
        # 第 4 位 = 本轮语义背景统计（命中时复用，保证指标字段恒定存在）
        _band_snapshot = {it[1].get("id"): it[1].get("_relevance_band")
                          for it in top
                          if isinstance(it[1], dict) and it[1].get("_relevance_band")}
        self._query_cache[cache_key] = (_cache_now, top, _band_snapshot, _sem_stats)

        # v7.0.2 (P2-2): 落本次召回的质量指标（P0-2 是否触发下限看这里）
        self.last_recall_metrics = _recall_metrics(
            query, k, len(records), len(candidate_indices), top, _floor_dropped,
            sem_stats=_sem_stats)
        # v7.0.2 (P0-2 修正)：输出边界快照 —— 交给调用方的是副本，不是索引对象
        return self._snapshot(top)

    def sync_record(self, memory_id, updates):
        """把字段更新同步回**共享检索索引记录**（v7.0.2）。

        为什么需要：`retrieve()` 现在返回记录副本（消除别名污染），于是
        `brain._touch_recalled()` 对返回值的就地修改（access_count 等）不再影响
        索引里的那份 —— 那会让"访问计数"在会话内不生效（要等索引重建）。
        这里按 id 定位共享记录并同步同一批字段，恢复"会话内即时生效 + 跨进程靠
        落库"的一致性。命中下标走 `_id_map`（O(1)）；退化路径只在记录数不大时
        线性扫描，避免百万级库上出现 O(N) 热点。
        """
        if not memory_id or not updates:
            return False
        recs = self._cached_records
        if not recs:
            return False
        idx = None
        try:
            if self._id_map:
                idx = self._id_map.get(memory_id)
        except Exception:
            idx = None
        if idx is None or idx >= len(recs) \
                or not isinstance(recs[idx], dict) \
                or recs[idx].get("id") != memory_id:
            if len(recs) > 5000:
                return False
            idx = None
            for i, r in enumerate(recs):
                if isinstance(r, dict) and r.get("id") == memory_id:
                    idx = i
                    break
            if idx is None:
                return False
        rec = recs[idx]
        try:
            rec.update(updates)
        except Exception:
            return False
        return True

    def _multi_hop_enhance(self, store, query, scored, k):
        """多跳推理：从第一跳结果中抽取实体，再做一次图扩展检索，合并结果。"""
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


# ============================================================================
# Part 6.4: 相关性判定基础设施（v7.0.2 P0-2 / P1-1 / P1-3）
# ============================================================================

# ---- P0-2：相关性下限 ----
# 【为什么不能用融合分做阈值】五路（BM25/向量/图/时间/可信度）每一路都做了
# `x / max(x)` 归一化，于是**最高分那条永远拿 1.0** —— 融合分因此没有绝对含义。
# 实测：无关查询「怎样给汽车换轮胎」top1 = 1.0200，**高于**相关查询
# 「我的显卡是什么」的 0.6922。任何"融合分 ≥ τ"的写法要么形同虚设、
# 要么先把正确答案滤掉。
# 【可行信号】未归一化的原始 cosine 有绝对含义。**阈值必须实测标定，别随手改**：
#
#   标定集 A（8 条记忆，7.0.2 初版）：相关 min 0.5222 / 无关 max 0.4064 → 取 0.45
#   标定集 B（24 条记忆，含同主题干扰项，7.0.2 终版复标）：
#       相关查询·**正确答案**原始 cosine：min 0.5708 / max 0.8440 / 均 0.7393
#       无关查询·top1      原始 cosine：min 0.3142 / max 0.5275
#       阈值扫描：
#         0.45 → 无关放过 **5** 条，相关保留 8/8   ← 初版取值，在干扰项密集时太松
#         0.50 → 无关放过 2 条，   相关保留 8/8
#         0.55 → 无关放过 **0** 条，相关保留 **8/8** ← 采用
#         0.60 → 无关放过 0 条，   相关保留 7/8（丢掉 0.5708 那条真答案）
#
# 为什么标定集 A 不够：A 的"无关"项与查询毫无话题交集；B 故意放了**同主题干扰项**
# （"备用显卡"/"病假天数"/"备用端口"），此时无关项能到 0.53 —— 0.45 会放 5 条进来。
# 这正是用户实际会遇到的情形（库里全是围绕自己的记忆），所以以 B 为准。
#
# 【已知边界】0.5708 与 0.5275 只差 0.043，说明该阈值对**语料分布**敏感：
# 若整库围绕同一主题（背景相似度整体偏高），绝对阈值会同时放过无关项。
# 因此 `last_recall_metrics` 额外暴露 `sem_background`（候选原始余弦中位数）
# 与 `sem_top1`，便于发现"整体偏高"的库后复标定。
# 未采用"相对背景"判据的原因：实测 相关(top1-中位) min 0.1557 与
# 无关(top1-中位) max 0.1458 仅差 0.010，单独使用更不可靠（会误杀相关项）。
REL_MIN_SEMANTIC = float(os.environ.get("MNEMOSYNE_REL_MIN_SEMANTIC", "0.55"))
# 字符 n-gram 相似度下限（0~1）：字面高度重合（改写 / 引用原句）视为强证据。
# 实测无关项全部 < 0.34，故它作为"向量弱但字面强"的兜底通道，不随语义阈值调整。
REL_MIN_NGRAM = float(os.environ.get("MNEMOSYNE_REL_MIN_NGRAM", "0.34"))
# 语义"高置信"档下限：正确答案实测均值 0.7393，0.60 稳在其下沿之下
REL_HIGH_SEMANTIC = float(os.environ.get("MNEMOSYNE_REL_HIGH_SEMANTIC", "0.60"))

# ---- P1-1：代词 → 实体归一 ----
# 记忆以第三人称书写（主语多为 `用户`），而提问是"我 / 我的"。
# 不做这层归一，图通道的查询侧永远找不到 `用户` 这个节点 → 0.10 权重空转。
_QUERY_PRONOUN_ENTITIES = {"我": "用户", "我的": "用户", "自己": "用户", "咱": "用户"}

# ---- P1-3：查询词 → 标签 同义映射 ----
# Path6 的实体词抽取是纯规则/词表命中，**查询词与标签不同形就完全失效**：
# 「我的显卡是什么」的标签是 `硬件`，两者不同形 → 拿不到 0.30 boost，
# 分数只有 0.6922，反被无关查询的 1.0200 反超。
_QUERY_TAG_SYNONYMS = {
    "显卡": ("硬件",), "gpu": ("硬件",), "显存": ("硬件",), "cpu": ("硬件",),
    "主板": ("硬件",), "硬盘": ("硬件",), "内存条": ("硬件",), "电脑": ("硬件",),
    "咖啡": ("咖啡", "饮品"), "卡布基诺": ("咖啡", "饮品"), "卡布奇诺": ("咖啡", "饮品"),
    "拿铁": ("咖啡", "饮品"), "汽水": ("汽水", "饮品"), "大窑": ("汽水", "饮品"),
    "乌龙茶": ("茶", "饮品"), "奶茶": ("茶", "饮品"), "茶": ("茶", "饮品"),
    "时区": ("环境",), "utc": ("环境",), "操作系统": ("环境",),
    "城市": ("地点",), "在哪": ("地点",), "住在": ("地点",), "工作地": ("地点",),
    "代号": ("项目",), "项目名": ("项目",), "仓库": ("项目",),
    "沟通": ("沟通",), "表达方式": ("沟通",), "说话": ("沟通",),
    # ---- v7.0.2 (D11)：偏好 / 意图类 ----
    # 【为什么必须补这一类】它是记忆系统被问得**最多**的一类问题，而且提问**天然是改写**：
    #   提问「我喜欢什么样的表达方式」
    #   记忆「用户偏好先把结论说清楚，再给依据。」（tags=['偏好','项目']）
    # 实测这一对的原始余弦只有 **0.4900**，而无关查询的 top1 可达 **0.5275** ——
    # 两条分布**重叠**，任何单一语义阈值都无法既保住它、又挡住无关项
    # （详见下方 REL_MIN_SEMANTIC 的标定表）。
    # 结论：这类问题不能只靠语义通道，必须由**结构化标签**兜住 ——
    # 这正是 Path6 的既有设计目的（"结构性保证精准记忆不被 Top-K 挤占"）：
    # 命中 tag 即视为结构性证据，直接通过相关性下限。
    "喜欢": ("偏好",), "我喜欢": ("偏好",), "偏好": ("偏好",), "讨厌": ("偏好",),
    "习惯": ("偏好",), "爱好": ("偏好",), "喜好": ("偏好",), "偏爱": ("偏好",),
    "中意": ("偏好",), "倾向": ("偏好",), "口味": ("偏好", "饮品"),
    "表达": ("沟通",), "语气": ("沟通",), "风格": ("沟通",), "措辞": ("沟通",),
    "身份": ("身份",), "名字": ("身份",), "称呼": ("身份",),
    "待办": ("待办",), "要做": ("待办",), "任务": ("待办",),
    "反思": ("反思",), "总结": ("反思",), "复盘": ("反思",),
    "教训": ("教训",), "踩坑": ("教训",),
}

# ---- P1-3：泛问词灰名单 ----
# 「什么 / 怎么 / 多少」这类泛问 2 字子串会被 `_query_entity_words` 当成实体词，
# 再拿去和全库 tags 做双向子串匹配 → 制造噪声命中，把无关项抬进 top-k。
_GENERIC_QUERY_WORDS = frozenset({
    "什么", "什么时", "怎么", "怎样", "如何", "为什么", "为啥", "哪个", "哪些",
    "哪里", "哪儿", "多少", "几个", "多久", "谁", "是不是", "有没有", "是否",
    "可以", "能不能", "要不要", "请问", "告诉", "知道", "记得", "时候",
    "我的", "我在", "我有", "我想", "我要", "这个", "那个", "一个",
})

# 图邻接表包含式匹配的规模上限（超过则只做精确匹配，保护 P99 延迟）
_GRAPH_ADJ_MATCH_LIMIT = 5000


def _recall_metrics(query, k, n_records, n_candidates, top, floor_dropped,
                    cache_hit=False, sem_stats=None):
    """装配一次 retrieve 的质量指标（v7.0.2 P2-2：召回质量监控的输入）。

    纯记账，无副作用。通道分布来自每条结果自带的 `reasons`，
    分档分布来自 P0-2 写在记录上的 `_relevance_band`。

    v7.0.2：新增 `sem_top1` / `sem_background`（候选原始余弦的 top1 与中位数）。
    用途：相关性下限是**绝对**阈值，对"整库同一主题"（背景相似度整体偏高）的库会失准；
    运维可通过这两个值判断是否需要复标定（top1 与 background 贴得很近 = 该库整体偏高）。
    """
    channels = {}
    bands = {}
    for it in top:
        try:
            for rsn in (it[2] or ()):
                channels[rsn] = channels.get(rsn, 0) + 1
            _b = str(it[1].get("_relevance_band"))
            bands[_b] = bands.get(_b, 0) + 1
        except (IndexError, TypeError, AttributeError):
            continue
    m = {
        "query_len": len(query or ""),
        "k": k,
        "records": n_records,
        "candidates": n_candidates,
        "returned": len(top),
        "top1": round(float(top[0][0]), 4) if top else None,
        "top1_band": (top[0][1].get("_relevance_band") if top else None),
        "empty": not top,
        "floor_dropped": floor_dropped,
        "bands": bands,
        "channels": channels,
        "cache_hit": cache_hit,
    }
    if sem_stats:
        m["sem_top1"] = sem_stats.get("top1")
        m["sem_background"] = sem_stats.get("background")
    return m


def _is_content_token(token):
    """词面兜底用的"内容词"判定：≥2 字符且不在泛问词灰名单里。"""
    if not token or len(token) < 2:
        return False
    t = str(token).strip().lower()
    if not t or t in _GENERIC_QUERY_WORDS:
        return False
    return True


def _record_graph_nodes(record):
    """记录在图里的节点集合：`entities` ∪ `graph_edges` 的 from/to/qualifier。

    v7.0.2（P1-1）：旧实现只看 `entities`，而边存的是"对象字符串"
    （`RTX 4090`），实体存的是分词碎片（`RTX`、`4090`），两边对不上 ——
    即使图里真有边，打分也用不上。把边的端点也当节点，再配 `_node_overlap`
    的包含式匹配，图通道才真正参与打分。
    """
    nodes = set()
    for e in (record.get("entities") or []):
        if e:
            nodes.add(str(e))
    for e in (record.get("graph_edges") or []):
        if isinstance(e, dict):
            for k in ("from", "to", "qualifier"):
                v = e.get(k)
                if v:
                    nodes.add(str(v))
        elif isinstance(e, (list, tuple)) and len(e) >= 2:
            nodes.add(str(e[0]))
            nodes.add(str(e[1]))
    return nodes


def _node_overlap(a_nodes, b_nodes):
    """**分级**节点匹配得分：精确命中记 1.0，包含式命中记 0.5。

    【为什么要分级（v7.0.2 D8）】旧实现把"精确命中"与"包含式命中"同等计 1 分，
    于是这两条记录在图通道上**得分完全相同**：
        `用户的显卡是 RTX 4090`   ← 限定词正是查询词 `显卡`（精确）
        `用户的备用显卡是 RTX 3060` ← 限定词是 `备用显卡`，只是**包含** `显卡`
    图通道因此无法区分"用户问的那个属性"与"带额外修饰的近邻属性"。
    实测（真实 Qdrant + BGE-M3）：`我用的什么显卡` 的正确答案一度被"备用显卡"
    挤到 rank3 —— 用户视角就是"它把备用件当成我在用的了"。

    【为什么还需要包含式】分词粒度差异确实存在（`RTX` vs `RTX 4090`、
    `嵌入模型` vs `用户的嵌入模型`），完全改精确匹配会漏配。故保留但降权。
    """
    score = 0.0
    for a in a_nodes:
        if not a:
            continue
        best = 0.0
        for b in b_nodes:
            if not b:
                continue
            if a == b:
                best = 1.0
                break
            if a in b or b in a:
                if best < 0.5:
                    best = 0.5
        score += best
    return score


def _match_adj_nodes(nodes, adj):
    """在邻接表键里找与 *nodes* 包含式匹配的节点名（P1-1 归一化 + 规模保护）。"""
    out = set()
    if not nodes or not adj:
        return out
    allow_fuzzy = len(adj) <= _GRAPH_ADJ_MATCH_LIMIT
    for n in nodes:
        if not n:
            continue
        if n in adj:
            out.add(n)
            continue
        if not allow_fuzzy:
            continue
        for key in adj:
            if n in key or key in n:
                out.add(key)
    return out


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
# Part 6.5: 自动 tags 命中通道（v7.0.2 混合检索默认行为）
# ============================================================================

# query 实体词长度下限（CJK 字符数）：过短（如单字"喝"）噪音大，不触发
_AUTO_TAG_MIN_CJK = 2
_AUTO_TAG_MAX_HITS = 30  # 单次查询最多补多少个 tags 命中候选（防全库误命中爆炸）


def _query_entity_words(query):
    """从 query 抽实体词用于 tags 自动匹配。

    策略（零分词依赖，纯规则）：
    1. CJK 连续片段 → 取其中的 2/3 字子串作为候选实体词
       （"荔枝味大窑汽水" → "大窑""汽水""荔枝"等子串，匹配 tags 里的
        "大窑"/"汽水"/"荔枝口味"）
    2. 英文/数字词 → 整词（"cappuccino" 匹配 tag "卡布基诺/cappuccino"）
    3. 同义词扩展（复用 _expand_query_terms 的语义组，覆盖"喜欢/爱/要"等）
    返回 (实体词 set, 实体词的 bigram set)。
    """
    words = set()
    bigrams = set()
    if not query:
        return words, bigrams
    low = (query or "").lower()

    # 英文/数字整词
    import re as _re
    for m in _re.findall(r"[a-z0-9]+", low):
        if len(m) >= 2 and m not in _GENERIC_QUERY_WORDS:
            words.add(m)

    # CJK 连续片段 → 2/3 字子串
    for m in _re.findall(r"[\u4e00-\u9fff]+", low):
        L = len(m)
        if L < _AUTO_TAG_MIN_CJK:
            continue
        for wlen in (2, 3):
            if wlen <= L:
                for i in range(L - wlen + 1):
                    sub = m[i:i + wlen]
                    # v7.0.2 (P1-3)：泛问词不参与实体词/bigram —— 否则「什么」「多少」
                    # 这类 2 字子串会被当成实体词去撞全库 tags，制造噪声命中，
                    # 把无关记忆抬进 top-k。
                    if sub in _GENERIC_QUERY_WORDS:
                        continue
                    words.add(sub)
                    if wlen == 2:
                        bigrams.add(sub)
    return words, bigrams


def _auto_tag_match(query, records, topic_index):
    """按 query 实体词自动匹配全库 tags，返回 {idx: [命中的 tags]}。

    用 topic_index（tag→idx 集合）反查，避免 O(N×tags) 全库扫描：
      1. query 实体词 W → 候选 tag 集合 C = {tag ∈ topic_index : tag 是 W 中某词子串，
         或 W 中某词是 tag 子串，或 tag 的 bigram 与 query bigram 重叠}
      2. 对 C 里每个 tag 取 topic_index[tag]（idx 集合），命中的 idx 记入结果
    限制：单查询最多补 _AUTO_TAG_MAX_HITS 个候选（按命中 tag 数降序截断）。
    """
    words, q_bigrams = _query_entity_words(query)
    if not words:
        return {}

    # ---- v7.0.2 (P1-3): 查询词 → 标签 同义映射 ----
    # 「我的显卡是什么」的标签是 `硬件`，字面完全不同形；没有这层映射，Path6
    # 对这类记忆**完全失效**（实测分数 0.6922，反被无关查询「怎样给汽车换轮胎」
    # 的 1.0200 反超）。把同义标签名并入实体词集合，让下面的"双向子串"判定
    # 能直接命中 topic_index 的 tag 键。
    _syn_extra = set()
    for _w in list(words):
        for _t in _QUERY_TAG_SYNONYMS.get(_w, ()):
            _syn_extra.add(str(_t).lower())
    if _syn_extra:
        words = words | _syn_extra

    # 1) 候选 tag 集合（子串双向 + bigram 重叠）
    cands = set()
    for tag in topic_index.keys():
        tl = str(tag).lower()
        if not tl:
            continue
        hit = False
        # 双向子串（"大窑" 命中 tag "大窑"；"汽水" 命中 tag "大窑汽水"；
        # "荔枝味" 命中 tag "荔枝口味" 需 bigram 重叠兜底）
        for w in words:
            wl = w.lower()
            if wl in tl or tl in wl:
                hit = True
                break
        if not hit and q_bigrams:
            # tag 的 bigram 与 query bigram 有重叠（覆盖"荔枝味"↔"荔枝口味"）
            tag_bigrams = {tl[i:i + 2] for i in range(max(1, len(tl) - 1))}
            if tag_bigrams & q_bigrams:
                hit = True
        if hit:
            cands.add(tag)

    if not cands:
        return {}

    # 2) 反查 idx（topic_index: tag → set(doc_idx)），按命中 tag 数排序截断
    hits = {}  # idx → [tag, ...]
    for tag in cands:
        for idx in (topic_index.get(tag) or ()):
            hits.setdefault(idx, []).append(tag)

    # 截断：命中 tag 多的优先（更精准），控制候选爆炸
    if len(hits) > _AUTO_TAG_MAX_HITS:
        ranked = sorted(hits.items(), key=lambda kv: len(kv[1]), reverse=True)
        hits = dict(ranked[:_AUTO_TAG_MAX_HITS])
    return hits


# ============================================================================
# Part 7: Cognitive Resolver（Cognitive Resolver v3.1）
# ============================================================================
# 四规则引擎模块，在检索Result和答案之间做认知加工。
# 不依赖 LLM，纯规则 + 检索上下文推理。


