import collections
import hashlib
import json
import os
import re
import shutil
import time
from datetime import datetime
import importlib
import logging
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("mnemosyne.brain")

from .capsule import (build_capsule, capsule_cache_stats, capsule_selfcheck,
                      content_hash, display_ref, estimate_tokens, make_ref,
                      parse_ref,)
from .graph import (MemoryGraphStore, _cosine,)
from .models import (_auto_importance, _build_record, _default_confidence, _default_layer, _extract_event_time, _infer_fact_type, _infer_source_type, ConsolidationReport, DemoteReport,)
from .retrieval import (RetrievalEngine,)
from .storage import (MemoryStore,)
from .utils import (EmbeddingEngine, StatsTracker, _content_atoms, _extract_relationships, _now_iso, _stable_id, _tf_vector, _tokenize, _utcnow_ts, compress_text, _memory_value, _normalize_template_hash, _content_signature, _compute_pair_similarity, _unique_salt, _redact_sensitive_fields, encode_replaced_stats,)

# === Constants (defined in package __init__) ===
import os as _os_init
VERSION = "8.0.0"
INDEX_NAME = "index.jsonl"
GRAPH_NAME = "graph.jsonl"
META_NAME = "meta.json"
EMBEDDING_DIM = 128
PROJ_BUCKETS = 2048
DEFAULT_DIR = _os_init.path.join(_os_init.path.expanduser("~"), ".mnemosyne")
MEMORY_TYPES = {
    "semantic", "episodic", "procedural", "preference", "lesson",
    "identity", "reflection", "strategy", "todo", "note",
    "conversation", "fact", "event",
}
MEMORY_LAYERS = {"working", "episodic", "semantic", "procedural", "reflective"}
FACT_TYPES = {"fact", "opinion", "belief", "observation", "inference", "hypothesis"}
SOURCE_TYPES = {"user", "system", "inference", "web_search", "file", "agent_generated", "external"}
VERIFY_STATUS = {"unverified", "verified", "contradicted", "outdated", "superseded"}

# v7.0.2 (P0-1)：access_count 计数上限。与 retrieval 打分侧
# `access_boost = 1.0 + 0.01 * min(access_count, 5)` 对齐 —— 超过 5 已无边际影响，
# 继续累积只会让"老记忆"长期压过"当前相关记忆"。
_ACCESS_COUNT_CAP = 5

# v7.0.2 (P2-2)：召回质量指标的环形缓冲长度（进程内，供 recall_health 读取）
_RECALL_HEALTH_RING = 200


def _write_dedupe_config():
    """写入侧语义去重的开关与门限（v7.0.2 P2-3）。

    默认开启（`MNEMOSYNE_WRITE_DEDUPE=0` 可关闭），门限 0.95
    （`MNEMOSYNE_WRITE_DEDUPE_THRESHOLD` 可调，钳制在 [0.5, 1.0]）。
    """
    raw = str(os.environ.get("MNEMOSYNE_WRITE_DEDUPE", "1")).strip().lower()
    enabled = raw not in ("0", "false", "no", "off", "")
    try:
        thr = float(os.environ.get("MNEMOSYNE_WRITE_DEDUPE_THRESHOLD", "0.95"))
    except (TypeError, ValueError):
        thr = 0.95
    return enabled, max(0.5, min(thr, 1.0))


class MemoryBrain:
    """Memory Brain —— 协调所有认知模块的统一入口。

    子模块：
      - Extractor: 自动信息抽取
      - Embedder: 向量编码
      - Graph: Knowledge Graph
      - Consolidator: Memory Consolidation
      - Confidence: 可信度评估
      - Learner: 自学习循环
    """

    def __init__(self, base_dir: str = DEFAULT_DIR, enable_embeddings: bool = True,
                 enable_graph: bool = True, enable_stats: bool = True,
                 tokenizer_backend: str = "simple", tokenizer_model: Optional[str] = None,
                 actor: str = "local", plugins: Optional[List[str]] = None,
                 namespace: Optional[str] = None,
                 max_active_memories: Optional[int] = None,
                 store_backend: Optional[str] = None,
                 hot_cache_size: Optional[int] = 1000) -> None:
        self.base_dir = base_dir
        self.actor = actor
        self.namespace = namespace
        self.max_active_memories = max_active_memories
        # 存储后端选择：默认 SqliteBackend（WAL + FTS5 + 命名空间隔离 + 热层 LRU 缓存）；
        # 显式 store_backend == "jsonl" 时使用 JSONL 兼容后端（migrate 命令可迁移）。
        if store_backend == "jsonl":
            self.store = MemoryStore(base_dir)
            self.store_backend = "jsonl"
        else:
            from storage import SqliteBackend
            self.store = SqliteBackend(base_dir, namespace=namespace,
                                       hot_cache_size=hot_cache_size)
            self.store_backend = "sqlite"
        self.embed_engine = EmbeddingEngine() if enable_embeddings else None
        self.graph_store = MemoryGraphStore(base_dir) if enable_graph else None
        self.retrieval = RetrievalEngine(
            embed_engine=self.embed_engine,
            graph_store=self.graph_store,
        )
        self.enable_embeddings = enable_embeddings
        self.enable_graph = enable_graph
        self.stats_tracker = StatsTracker(base_dir,
                                          tokenizer_backend=tokenizer_backend,
                                          tokenizer_model=tokenizer_model) if enable_stats else None
        self._stats_auto = False
        self._show_stats = False
        self._auto_display_stats = False  # 默认不刷屏；需 stats_auto()/show_stats() 显式开启
        self.last_stats = None  # v7.0.0：最近一次 retain/recall 的统计快照（返回值类型统一后由此读取）
        self._input_price_per_million = 3.0
        self.semantic_hook = None  # 分层协同：BM25 不足时回调外部向量库（L2）
        self._template_index = None          # template_hash → record，加速版本追踪（惰性构建，O(1) 替代 O(N) 扫描）
        # v7.0.0: 公证器局部扫描索引（实体倒排 + 内容指纹），避免每次 retain 全量读盘/遍历
        self._entity_index = None            # entity → list[record]
        self._fingerprint_index = None       # template_hash/fingerprint → list[record]

        # v7.0.2 (P1-2)：向量后端写/删成败记账（插件熔断时会静默丢弃，必须可见化）
        self._vector_ops = {}
        # v7.0.2 (P2-2)：召回质量指标环形缓冲（进程内，供 recall_health 工具读取）
        self._recall_health = {"n": 0, "empty": 0, "floor_dropped": 0,
                               "returned_sum": 0, "top1_sum": 0.0, "top1_n": 0,
                               "bands": {}, "channels": {}, "recent": []}
        # v7.0.2 (P1-1)：存量数据的图边懒回填标记（每个进程只做一次）
        self._graph_backfilled = False

        # v7.0.0: MemoryNotary
        from .notary import MemoryNotary
        self.notary = MemoryNotary()

        # v7.0.0: Plugin loading
        self._plugins = {}
        self.plugins = self._plugins  # 兼容别名（test 直接访问 brain.plugins）
        self.vector_backend_plugin = None
        self.crypto_plugin = None
        self.reranker_plugin = None
        # v7.0.0-qdrant: 插件启用来源。CLI（cli.py:172）、MCP Server
        # （webui/mcp_server.py:84）与 WebUI 构建 MemoryBrain 时均未传 plugins，
        # 故在此统一支持环境变量 MNEMOSYNE_PLUGINS（逗号分隔，如
        # "qdrant_backend"），避免逐个改动各入口。未设置时 plugins 保持 None，
        # 行为与上游 7.0.0 完全一致。
        if plugins is None:
            _env_plugins = os.environ.get("MNEMOSYNE_PLUGINS", "")
            plugins = [p.strip() for p in _env_plugins.split(",") if p.strip()] or None
        if plugins:
            self._load_plugins(plugins)

        # v7.0.0: 外部 provider 与路由
        self.external_provider = None
        self._external_providers = []
        self._external_write = True
        self._external_read = "hybrid"

        # v7.0.0: 用户画像 / 会话 / 账本 / 快照
        try:
            from profiles.user_profile import UserProfile
            self.profile_manager = UserProfile(os.path.join(base_dir, "profiles.db"))
        except Exception:
            self.profile_manager = None
        try:
            from storage.session_store import SessionStore
            self.session_store = SessionStore(os.path.join(base_dir, "sessions.db"))
        except Exception:
            self.session_store = None
        try:
            from storage.ledger import MemoryLedger
            self.ledger = MemoryLedger(base_dir=base_dir)
        except Exception:
            self.ledger = None
        try:
            from context.snapshot_builder import SnapshotBuilder
            self.snapshot_builder = SnapshotBuilder(self)
        except Exception:
            self.snapshot_builder = None

    def set_model_price(self, input_per_million: float) -> None:
        """设置大模型输入单价（元/百万Token），默认 DeepSeek ¥3。
        
        常用参考：
          GPT-4o       ¥70/百万    Claude 3.5   ¥20/百万
          文心一言 4.0   ¥12/百万    Qwen-Max     ¥3.5/百万
          通义千问 Turbo ¥0.8/百万
        """
        self._input_price_per_million = float(input_per_million)

    # ---- v7.0.0: Plugin loading ----
    
    def _load_plugins(self, plugin_names, *args, **kwargs):
        """Load plugins by name. Each plugin is a string like 'crypto' or 'numpy_vector'.

        兼容旧调用：允许额外位置参数（历史上签名曾有 5 个形参），统一忽略。
        """
        import importlib
        for name in (plugin_names or []):
            if not isinstance(name, str):
                continue
            try:
                mod = importlib.import_module(f"mnemosyne_plugins.{name}.plugin")
                if hasattr(mod, 'register'):
                    inst = mod.register(self)
                elif hasattr(mod, 'get_plugin_class'):
                    cls = mod.get_plugin_class()
                    inst = cls(self)
                else:
                    continue
                self._plugins[name] = inst
                # 绑定官方插件快捷属性
                if name in ("numpy_vector", "qdrant_backend"):
                    self.vector_backend_plugin = inst
                    # 将向量插件接入检索向量路径：替代默认随机投影 EmbeddingEngine，
                    # 使 retain()/recall() 实际使用插件编码（模型或哈希回退）。
                    # 此前插件虽加载但从未接线，导致 plugins=["numpy_vector"]
                    # 对检索质量零影响（阶段3 发现并修复）。
                    if getattr(inst, "available", False):
                        self.embed_engine = inst
                        self.enable_embeddings = True
                        if self.retrieval is not None:
                            self.retrieval.embed_engine = inst
                elif name == "crypto":
                    self.crypto_plugin = inst
                elif name == "reranker":
                    self.reranker_plugin = inst
            except Exception as e:
                print(f"Warning: failed to load plugin '{name}': {e}")

    def get_plugin(self, name):
        """Get a loaded plugin by name."""
        return self._plugins.get(name)

    def list_plugins(self):
        """List loaded plugins as dicts (compatible with plugin_sdk.PluginInfo)."""
        out = []
        for name, inst in self._plugins.items():
            info = {"name": name, "enabled": True}
            if hasattr(inst, "version"):
                info["version"] = inst.version
            out.append(info)
        return out

    # ---- v7.0.0: Audit trail ----
    
    def audit(self, memory_id):
        """Return the complete audit trail for a memory."""
        return self.store.audit(memory_id)

    # ---- v7.0.0: retain_detailed ----
    
    def retain_detailed(self, content, mtype="semantic", fast=False, project=None, **kwargs):
        """Write a memory with Notary assessment. Returns the full record dict."""
        mid = self.retain(content, mtype=mtype, fast=fast, project=project, **kwargs)
        return self.store.find_by_id(mid)

    def _retain_core(self, content, mtype="semantic", **kwargs):
        """底层写入原语：返回 memory_id。供 importer / crypto 插件等内部调用。

        与 retain() 的区别：不额外叠加外部 provider 双写等高层行为，
        但保留完整的 record 构建、公证评估、可信度历史与账本记录。
        """
        fast = kwargs.pop("fast", False)
        return self.retain(content, mtype=mtype, fast=fast, **kwargs)

    # ---- v7.0.0: forget with evict ----

    def forget(self, memory_id: str, evict: bool = False,
               reason: str = "user_forget") -> bool:
        """遗忘一条记忆。默认软遗忘：confidence 归零 + status=deleted。

        软遗忘（evict=False，默认）满足"把指定记忆置信度降为 0"，同时把
        status 置为 deleted，让检索层（sqlite FTS 的 status 过滤 + retrieval
        的 status 过滤）彻底不再召回它。审计链与可信度轨迹全部保留，
        因此该操作**可回溯、可复位**，不是不可逆破坏。
        硬删除（evict=True）才会物理移除记忆行及其实体/边。

        v7.0.0-MCP 修复：
          - 原实现只改 status（走 all_records()+rewrite() 全库重写），既没有把
            confidence 归零，也没有写 confidence_history，且在全量重写上代价高。
            现在改为 update_by_id 单行更新（sqlite 后端 O(1)）。
          - 原实现不失效检索缓存：sqlite 走 WAL，UPDATE 未必立刻改主库 mtime，
            而检索缓存以 mtime+size 为失效判据 —— 结果是"已遗忘"的记忆仍留在
            _cached_records 里继续被召回。现在显式置空指纹。
        """
        rec = self.store.find_by_id(memory_id)
        if rec is None:
            return False
        prev_conf = rec.get("confidence")
        preview = (rec.get("content") or "")[:50]
        if evict:
            if hasattr(self.store, "delete"):
                self.store.delete(memory_id)
            else:
                self.store.rewrite([r for r in self.store.all_records()
                                    if r.get("id") != memory_id])
        else:
            self.store.update_by_id(memory_id, {
                "confidence": 0.0,
                "status": "deleted",
                "deleted_at": _now_iso(),
            })
        # 同步向量索引：删除该记忆在向量后端里的点。
        # 根因（实测）：软遗忘只改 status/confidence、硬删除只删 SQLite 行，
        # 两条路径都不触碰向量后端 —— 于是"已遗忘"的记忆仍在 ANN 候选池里占位；
        # 硬删除后 SQLite 行已不存在，检索层的 status 过滤也无从判断，
        # 该点成为**永久孤儿**（dsh 命名空间实测 33 点中有 3 个这样的孤儿），
        # 直接稀释语义候选池、拉低召回精度。
        # 软遗忘同样回收：该记忆本就被输出侧过滤掉，留下向量只有占位成本，
        # 没有任何召回收益；可回溯性由行/审计链/可信度轨迹保留，向量是可重建的派生索引。
        try:
            if hasattr(self.embed_engine, "remove"):
                self._note_vector_op("remove", self.embed_engine.remove(memory_id))
        except Exception as exc:
            self._note_vector_op("remove", False, exc)
        try:
            self.store.audit_log({
                "ts": _now_iso(),
                "actor": self.actor,
                "action": "forget_evict" if evict else "forget",
                "target_id": memory_id,
                "details": {"reason": reason, "evict": bool(evict),
                            "confidence_before": prev_conf,
                            "confidence_after": 0.0,
                            "content_preview": preview},
            })
        except Exception as exc:
            logger.debug("可选功能降级，忽略异常：%s", exc)
        try:
            self.store.add_confidence_history(memory_id, {
                "ts": _now_iso(),
                "confidence": 0.0,
                "reason": reason,
                "delta": -(float(prev_conf)
                           if isinstance(prev_conf, (int, float)) else 0.0),
                "flags": [],
            })
        except Exception as exc:
            logger.debug("可选功能降级，忽略异常：%s", exc)
        if self.ledger is not None:
            try:
                self.ledger.append("forget", memory_id=memory_id,
                                   data_summary={"evict": bool(evict),
                                                 "reason": reason})
            except Exception as exc:
                logger.debug("可选功能降级，忽略异常：%s", exc)
        # 更新 template 索引里的 status（否则同内容再次 retain 会被判为旧版本未删）
        if self._template_index:
            for _h, _ent in list(self._template_index.items()):
                if _ent and _ent[0] == memory_id:
                    self._template_index[_h] = (_ent[0], _ent[1],
                                                "deleted", _ent[3])
        self._invalidate_retrieval_index()
        return True

    # ---- v7.0.0-MCP: 记忆生命周期（召回缓存失效 / 更新-更正 / 按语义遗忘） ----

    def _invalidate_retrieval_index(self):
        """强制下一次 recall 重建检索索引。

        为什么必须显式做：检索缓存的失效判据是 store 文件指纹（mtime+size，
        见 retrieval._store_fingerprint），而 sqlite 后端跑在 WAL 模式下，
        UPDATE/DELETE 先落 -wal，主库 mtime 可能不变 —— 只靠指纹会漏判，
        导致被遗忘或被取代的记忆继续留在 _cached_records 里被召回。
        """
        try:
            self.retrieval._indexed_fingerprint = None
            self.retrieval._query_cache.clear()
        except Exception as exc:
            logger.debug("检索索引失效失败：%s", exc)

    def _mark_superseded(self, old_id: str, new_id: Optional[str] = None) -> bool:
        """把 old_id 标记为已被取代（verification=superseded，保留全部历史）。

        检索层对 superseded 已有降权（可信度 ×0.3、时序 ×0.3），
        所以这是"更新"的正确落点：旧说法不再主导召回，但没被销毁。
        """
        old = self.store.find_by_id(old_id)
        if old is None:
            return False
        updates = {"verification": "superseded"}
        if new_id:
            updates["superseded_by"] = new_id
        ok = self.store.update_by_id(old_id, updates)
        if not ok:
            return False
        try:
            self.store.audit_log({
                "ts": _now_iso(),
                "actor": self.actor,
                "action": "supersede",
                "target_id": old_id,
                "details": {"superseded_by": new_id,
                            "content_preview": (old.get("content") or "")[:50]},
            })
        except Exception as exc:
            logger.debug("可选功能降级，忽略异常：%s", exc)
        self._invalidate_retrieval_index()
        return True

    def correct(self, old_memory_id: str, new_content: str, **kwargs: Any) -> Optional[str]:
        """更正一条记忆：写入更正内容 + 把旧记忆标记为 superseded。

        对应规则里的"用户纠正先前说法"：先 recall 拿到旧记忆 id，
        再调用本方法（或等价的 retain(..., supersedes=旧id)）。
        返回新记忆 id；旧记忆不存在时返回 None。
        """
        old = self.store.find_by_id(old_memory_id)
        if old is None:
            return None
        kw = dict(kwargs)
        kw.pop("fast", None)          # _build_record 不接受 fast
        kw.pop("supersedes", None)    # 由本方法统一注入
        return self.retain(new_content, mtype=old.get("type", "semantic"),
                           supersedes=old_memory_id, **kw)

    def resolve_targets(self, query: Optional[str] = None,
                        memory_id: Optional[str] = None, k: int = 3):
        """解析"要操作哪条记忆"：给 memory_id 直接取；否则用 recall 按相关性找。

        返回 [(score|None, record), ...]。供 forget_targets 复用。

        v7.0.2：这里**显式关闭相关性下限**（`apply_floor=False`）。
        理由：本方法的语义是"**定位目标**"而不是"回答问题"——
        用户描述天然含糊（"把那个 bge 维度的事忘掉"），若套用回答问题的下限，
        返回空就等于"无法遗忘"，是功能性的破坏。安全性不靠阈值，而靠
        ① 所有调用方先走 dry_run 把候选报给用户确认；② 报告里带 score 与
        相关性置信带，让人自己判断像不像。详见 `retrieval.retrieve` 的
        `apply_floor` 说明。
        """
        if memory_id:
            rec = self.store.find_by_id(memory_id)
            return [(None, rec)] if rec else []
        if not query:
            return []
        out = []
        for item in self.recall(query, k=int(k or 3), apply_floor=False):
            try:
                out.append((round(float(item[0]), 4), item[1]))
            except (TypeError, ValueError, IndexError):
                out.append((None, item))
        return [t for t in out if isinstance(t[1], dict)]

    def forget_targets(self, memory_id: Optional[str] = None,
                       query: Optional[str] = None, k: int = 3,
                       dry_run: bool = False, evict: bool = False,
                       reason: str = "user_forget") -> Dict[str, Any]:
        """遗忘入口（MCP / CLI 共用）：支持 memory_id 或自然语言 query。

        dry_run=True 只解析候选、不做任何修改 —— Agent 应先 dry_run 把候选
        报给用户确认，再实际执行，避免"忘记"误伤。
        """
        targets = self.resolve_targets(query=query, memory_id=memory_id, k=k)
        resolved = []
        for score, rec in targets:
            resolved.append({
                "memory_id": rec.get("id"),
                "content": (rec.get("content") or "")[:120],
                "mtype": rec.get("type"),
                "confidence_before": rec.get("confidence"),
                "status_before": rec.get("status", "active"),
                "score": score,
                # v7.0.2：目标解析不套相关性下限（见 resolve_targets 说明），
                # 因此必须把"像不像"的判据交回调用方 —— 置信带 + 相关性原值。
                "confidence_band": rec.get("_relevance_band"),
                "semantic_similarity": rec.get("_relevance"),
            })
        report = {"dry_run": bool(dry_run), "targets": resolved,
                  "count": len(resolved),
                  "target_resolution": ("by_query(no_relevance_floor)"
                                        if query and not memory_id
                                        else "by_memory_id")}
        if dry_run:
            return report
        forgotten, failed = [], []
        for t in resolved:
            try:
                if self.forget(t["memory_id"], evict=evict, reason=reason):
                    forgotten.append(t)
                else:
                    failed.append(t)
            except Exception as exc:
                t["error"] = str(exc)
                failed.append(t)
        report["forgotten"] = forgotten
        report["failed"] = failed
        report["count"] = len(forgotten)
        report["evict"] = bool(evict)
        return report

    # ---- v7.0.0: close ----
    
    def close(self) -> None:
        """Clean up resources."""
        if self.store:
            try:
                self.store.close()
            except Exception as exc:
                logger.debug("可选功能降级，忽略异常：%s", exc)
        if self.graph_store:
            try:
                self.graph_store.close()
            except Exception as exc:
                logger.debug("可选功能降级，忽略异常：%s", exc)
        if getattr(self, "ledger", None) is not None:
            try:
                self.ledger.close()
            except Exception as exc:
                logger.debug("可选功能降级，忽略异常：%s", exc)
        if getattr(self, "session_store", None) is not None:
            try:
                self.session_store.close()
            except Exception as exc:
                logger.debug("可选功能降级，忽略异常：%s", exc)
        if getattr(self, "profile_manager", None) is not None:
            try:
                self.profile_manager.close()
            except Exception as exc:
                logger.debug("可选功能降级，忽略异常：%s", exc)

    # ---- 上下文管理器：支持 with MemoryBrain(...) as brain: ----

    def __enter__(self) -> "MemoryBrain":
        """进入 with 语句时确保存储初始化完成。"""
        self.ensure_init()
        return self

    def __exit__(self, exc_type: Optional[type], exc_value: Optional[BaseException],
                 traceback: Optional[Any]) -> bool:
        """退出 with 语句时释放资源（不吞异常）。"""
        self.close()
        return False

    # ---- v7.0.0: _rec_content helper ----
    
    def _rec_content(self, record):
        """Extract content from a record (handles both dict and tuple forms)."""
        if isinstance(record, dict):
            return record
        if isinstance(record, (list, tuple)):
            # (score, record, reasons) tuple
            if len(record) > 1 and isinstance(record[1], dict):
                return record[1]
            return record[0] if isinstance(record[0], dict) else {}
        return {}

    # ---- v7.0.0: budget_tokens in recall ----
    
    def _budget_recall(self, query: str, budget_tokens: int, k: int = 5,
                       **kwargs: Any) -> Tuple[List[Any], Dict[str, Any]]:
        """Budget-constrained recall. Returns (results, cost_report).

        v7.0.2（P2-1）起：预算**真正参与取舍**（prefix packing，见下方说明），
        并把 `budget_limit` / `budget_used` / `dropped_count` / `truncated`
        一并回传，让调用方能区分"结果就是这么少"与"预算被砍掉了"。
        """
        import time as _bt
        _bt0 = _bt.time()
        # v7.0.2 (P1-1) 收尾：与 recall() 同一处理 —— 预算路径也必须能看到存量图边，
        # 否则同一个库"普通召回有图谱增强、预算召回没有"，两条路径结果不一致。
        if not self._graph_backfilled:
            try:
                self._backfill_graph_edges()
            except Exception as exc:
                logger.debug("图边回填失败（忽略，不影响检索）：%s", exc)
                self._graph_backfilled = True

        # Token counter
        def _count_tokens(text):
            """预算口径的 token 计数（v7.0.2 修正）。

            【为什么必须改】原实现无分词器时用「4 字符 ≈ 1 token」—— 那是**英文**
            经验值。中文一个字通常就是 1 个 token，于是 `len(text)//4` 把中文上下文
            体积低估到 **1/3~1/4**：调用方写 `budget_tokens=40`（本意是"只给我
            几十个 token"），实际被塞进 150+ token 的中文原文 —— 预算形同虚设，
            `capsule` 的分层降级路径**永远触发不到**，"极致短上下文"就只是口号。
            实测：一条 66 字符的中文记忆按旧口径算 16 token，于是"40 token 预算"
            把它整条原文返回了。
            新口径：优先用真实分词器（tiktoken/transformers，显式配置时）；
            否则用 `capsule.estimate_tokens` —— CJK 逐字计 1、拉丁按词计 1，
            宁可**略高估**也不乐观低估（保守估算最多让上下文小一点，绝不爆窗）。
            """
            if self.stats_tracker and hasattr(self.stats_tracker, '_tokenizer'):
                _tk = getattr(self.stats_tracker, "_tokenizer", None)
                if _tk is not None:
                    try:
                        return len(_tk.encode(text))
                    except Exception as exc:
                        logger.debug("可选功能降级，忽略异常：%s", exc)
            return estimate_tokens(text)

        # Get candidates
        candidates = self.retrieval.retrieve(self.store, query, k=20, **kwargs)
        if not candidates:
            return [], {
                "selected": [],
                "selected_count": 0,
                "tokens_consumed": 0,
                "tokens_saved": 0,
                "top_k_tokens": 0,
                "budget_tokens": budget_tokens,
                "budget_limit": budget_tokens,
                "budget_used": 0,
                "dropped_count": 0,
                "truncated": False,
                "query_tokens": _count_tokens(query),
                "marginal_values": [],
            }

        # Greedy selection by marginal value (score × confidence × 边际信息量)
        selected = []
        selected_contents = []
        tokens_consumed = 0
        marginal_values = []

        def _bigram_overlap(a, b):
            """两条文本的字符 bigram 重叠度（Jaccard，0~1）。"""
            if not a or not b:
                return 0.0
            sa = {a[i:i + 2] for i in range(len(a) - 1)}
            sb = {b[i:i + 2] for i in range(len(b) - 1)}
            if not sa or not sb:
                return 0.0
            return len(sa & sb) / len(sa | sb)

        # Sort candidates by score * confidence
        # 注意：`rest`（每条结果自带的 reasons）必须**随条目一起带走**。
        # 旧实现把它漏在循环外，`selected.append((score, record, *rest))` 用的是
        # 上一个 for 循环泄漏的 `rest` —— 于是预算召回返回的每一条都带着
        # **最后一个候选**的 reasons（一个真实但极难发现的元数据错挂）。
        scored = []
        for score, record, *rest in candidates:
            conf = record.get("confidence", 0.7) if isinstance(record, dict) else 0.7
            content = record.get("content", "") if isinstance(record, dict) else ""
            tok_count = _count_tokens(content)
            marginal = score * conf
            scored.append((marginal, score, record, content, tok_count, rest))

        scored.sort(key=lambda x: x[0], reverse=True)

        # ---- v7.0.2 (P2-1 + AIC)：严格「按分数降序逐条装箱」，装不下就**降级** ----
        # 旧实现（≤7.0.1）遇到"下一条放不下"时用 `continue` 跳过 → 预算值不参与
        # 取舍（实测 120/60/30 产出完全相同，参数形同虚设）。
        # 7.0.2 起分两步：
        #   ① 放得下 → 给**原文**（无损，最不可能出错）；
        #   ② 放不下 → 不丢弃，而是压成**记忆胶囊**（指针 + 事实 + 原子，见
        #      capsule.py）。胶囊保证"原子守恒 + 可逆 + 零幻觉"，于是"预算不够"
        #      变成"精度分层"而不是"信息消失"。
        #   ③ 连最小胶囊（指针+原子）都装不下 → 进 `precision_index`：
        #      以**引用索引**形式随结果回传，**不计入正文预算**，由调用方决定
        #      是否 expand。这样"严格不超预算"与"事实不丢"同时成立。
        truncated = False
        capsules = []
        precision_index = []
        for marginal, score, record, content, tok_count, rest in scored:
            if tokens_consumed + tok_count <= budget_tokens:
                # ① 原文装得下：无损直通
                redundancy = max((_bigram_overlap(content, c) for c in selected_contents),
                                 default=0.0)
                marginal_adjusted = marginal * (1.0 - redundancy)
                selected.append((score, record, *rest))
                selected_contents.append(content)
                tokens_consumed += tok_count
                marginal_values.append(round(marginal_adjusted, 4))
                if len(selected) >= 20:  # Cap at top-20
                    break
                continue

            # ② 放不下 → 胶囊降级（不是丢弃）
            _remain = int(budget_tokens) - tokens_consumed
            try:
                cap = self._capsule_of(record, _remain, token_counter=_count_tokens)
            except Exception as exc:
                logger.debug("胶囊构建失败，转为索引项：%s", exc)
                cap = None
            if cap is not None and not cap["over_budget"] \
                    and cap["level"] != "full" and len(selected) < 20:
                _mini = self._capsule_record(record, cap)
                redundancy = max((_bigram_overlap(cap["text"], c)
                                  for c in selected_contents), default=0.0)
                selected.append((score, _mini, *rest))
                selected_contents.append(cap["text"])
                tokens_consumed += cap["tokens"]
                marginal_values.append(round(marginal * (1.0 - redundancy), 4))
                capsules.append({"ref": cap["ref"], "display_ref": cap["display_ref"],
                                 "level": cap["level"], "tokens": cap["tokens"],
                                 "lossless": False})
                truncated = True
                continue

            # ③ 连最小胶囊都装不下 → 只回传索引（不占正文预算）
            if cap is None:
                _min_tokens = None
            else:
                _min_tokens = cap.get("min_tokens")
            precision_index.append({
                "ref": (cap or {}).get("ref") or make_ref(record.get("id") or "", content),
                "display_ref": (cap or {}).get("display_ref"),
                "level": (cap or {}).get("level"),
                "tokens": (cap or {}).get("tokens"),
                "min_tokens": _min_tokens,
                "original_tokens": tok_count,
                "atoms": (cap or {}).get("atoms") or [],
                "preview": (content or "")[:40],
                "over_budget": True,
            })
            truncated = True
            if len(precision_index) >= 20:
                break

        # 预算连一条完整条目都放不下 → 至少给**首条的最小胶囊**并显式标注。
        # 为什么不是静默返回空：调用方已明确"用预算换上下文"，给出**标注过**的
        # 首条远好于给空结果 —— 静默返回空会让 Agent 误以为"没有相关记忆"。
        # 注意这里用胶囊而非原文：原文必然超预算，而胶囊只保留事实与指针。
        budget_respected = True
        if not selected and scored:
            _m, _s, _rec, _content, _tok, _rest = scored[0]
            try:
                _cap = self._capsule_of(_rec, max(1, int(budget_tokens)),
                                        token_counter=_count_tokens)
            except Exception:
                _cap = None
            if _cap is not None:
                selected.append((_s, self._capsule_record(_rec, _cap), *_rest))
                selected_contents.append(_cap["text"])
                tokens_consumed = _cap["tokens"]
                budget_respected = not _cap["over_budget"]
                capsules.append({"ref": _cap["ref"], "display_ref": _cap["display_ref"],
                                 "level": _cap["level"], "tokens": _cap["tokens"],
                                 "lossless": _cap["lossless"]})
                marginal_values.append(round(_m, 4))
            else:
                selected.append((_s, _rec, *_rest))
                selected_contents.append(_content)
                tokens_consumed = min(_tok, max(1, int(budget_tokens)))
                budget_respected = tokens_consumed <= int(budget_tokens)
                marginal_values.append(round(_m, 4))
            truncated = True

        # 计算 top_k_tokens：无预算时本应送入的全部候选 token 数。
        # _budget_recall 的候选池为 top-20（retrieve k=20），无预算即全部送入，
        # 因此基线 = 全部候选 token 之和；预算选择是其子集，故恒有
        # top_k_tokens >= tokens_consumed（token 经济学：预算只会省，不会多花）。
        top_k_tokens = sum(_count_tokens(r.get("content", "") if isinstance(r, dict) else "")
                           for _, r, *_ in candidates)
        _index_tokens = sum(int(p.get("tokens") or 0) for p in precision_index)

        cost_report = {
            "selected": [s[0] for s in selected],
            "selected_count": len(selected),
            "tokens_consumed": tokens_consumed,
            "tokens_saved": max(0, top_k_tokens - tokens_consumed),
            "top_k_tokens": top_k_tokens,
            "budget_tokens": budget_tokens,
            # v7.0.2 (P2-1)：把预算账目显式回传，让 Agent 自己判断"够不够用、
            # 要不要追问"，而不是拿到一个长度不明、来路不明的上下文块。
            "budget_used": tokens_consumed,
            "budget_limit": budget_tokens,
            "budget_respected": budget_respected,
            "dropped_count": max(0, len(scored) - len(selected)),
            "truncated": truncated,
            "query_tokens": _count_tokens(query),
            "marginal_values": marginal_values,
            # v7.0.2 (AIC)：降级与索引账目
            "capsule_count": len(capsules),
            "capsules": capsules,
            "indexed_count": len(precision_index),
            "index_tokens": _index_tokens,
            "precision_index": precision_index,
            "capsule_cache": capsule_cache_stats(),
        }

        # v7.0.2 (P2-2)：预算路径同样进质量指标，否则 recall_health 只统计到
        # "非预算"那一半调用，指标面失真（用 budget_tokens 的 Agent 恰好是最需要
        # 看"截断率/空结果率"的那批）。
        try:
            self._accumulate_recall_health(
                getattr(self.retrieval, "last_recall_metrics", None),
                latency_ms=(_bt.time() - _bt0) * 1000)
        except Exception as exc:
            logger.debug("召回指标累积失败（忽略）：%s", exc)

        return selected, cost_report

    def show_stats(self, on=True):
        """开启后，每次 retain/recall 自动打印统计行到终端。
        App 会在输出内容下方直接看到统计数据。"""
        self._show_stats = on

    def _stats_line(self, action, detail):
        """生成一行紧凑统计。只显示 Token 数——价格取决于大模型缓存命中率，Mnemosyne 不猜测。"""
        s = self.stats_tracker.summary() if self.stats_tracker else {}
        saved = s.get("estimated_tokens_saved", 0)
        print(f"[Mnemosyne] {action} | 写入{s.get('today_retain','?')} 检索{s.get('today_recall','?')} | "
              f"命中率{s.get('today_hit_rate',0):.0%} | 拦截未送入 LLM ≈{saved}Token | {detail}")

    def ensure_init(self):
        self.store.ensure_init()
        if self.graph_store:
            self.graph_store.ensure_init()

    # ---- 记忆writes （增强版自动抽取） ----

    @staticmethod
    def _normalize_for_hash(text):
        """归一化文本用于 template_hash 计算——去空格/标点/大小写"""
        import re
        return re.sub(r"[\s，。！？、；：\"\"''（）《》\[\]{}]", '', text).lower()[:200]

    def should_remember(self, content, mtype="semantic", **kwargs):
        """规则判断是否值得记忆（零 LLM）。默认规则，可被调用方覆盖。

规则：太短不记；含记忆意图词/实体/关键信息/重要信号词则记。返回 True/False。"""
        import re
        text = (content or "").strip()
        if not text:
            return False
        if len(text) < 6:
            return False
        if any(w in text for w in ("记住", "记下", "别忘了", "记录", "备注", "remember")):
            return True
        if re.search(r"\d{4}|\d{1,3}[%元万亿]|[A-Za-z0-9._%+-]+@|https?://", text):
            return True
        if any(w in text for w in ("重要", "必须", "决策", "偏好", "喜欢", "讨厌", "核心", "关键", "密码", "账号", "地址", "电话")):
            return True
        if re.search(r"[胡王李张刘陈杨黄赵周吴徐孙马朱郭何罗高林郑][\u4e00-\u9fff]{1,2}", text):
            return True
        return False

    def _ensure_template_index(self):
        """惰性构建 template_hash → 轻量元组索引，加速版本追踪（一次性 O(N)，替代每次 retain 的 O(N) 扫描）。

        条目为 (id, version, status, superseded_by) 元组——比 dict 省 ~70% 内存；
        完整记录按需经 find_by_id 读取，避免 100k 规模下整份记录的双份驻留。
        """
        if self._template_index is None:
            self._template_index = {}
            for r in self.store.all_records():
                th = (r.get("meta") or {}).get("template_hash")
                if th:
                    self._template_index[th] = (
                        r.get("id"), r.get("version", 1),
                        r.get("status", "active"), r.get("superseded_by"),
                    )
        return self._template_index

    # ---- v7.0.0: 公证器局部扫描索引（实体倒排 + 内容指纹）----

    @staticmethod
    def _slim_notary_entry(r):
        """公证器索引的轻量条目（仅 notary.assess 所需字段，省内存）。"""
        meta = (r.get("meta") or {})
        return {
            "id": r.get("id"),
            "content": r.get("content", ""),
            "entities": r.get("entities") or [],
            "fact_type": r.get("fact_type", "fact"),
            "meta": {"template_hash": meta.get("template_hash")},
            "content_hash": meta.get("template_hash") or r.get("content_hash"),
        }

    def _index_record(self, r):
        """将一条记录增量写入实体倒排索引与内容指纹索引（轻量条目）。"""
        if self._entity_index is None:
            self._ensure_notary_index()
        slim = self._slim_notary_entry(r)
        for e in slim["entities"]:
            self._entity_index.setdefault(e, []).append(slim)
        th = slim["meta"]["template_hash"] or slim["content_hash"]
        if th:
            self._fingerprint_index.setdefault(th, []).append(slim)

    def _ensure_notary_index(self):
        """惰性构建公证器局部扫描索引（一次性 O(N)，之后增量 O(1)）。"""
        if self._entity_index is None:
            self._entity_index = {}
            self._fingerprint_index = {}
            for r in self.store.all_records():
                self._index_record(r)
        return self._entity_index, self._fingerprint_index

    def _notary_candidates(self, record, content, max_scan=1000, per_entity=20):
        """计算与当前记录相关的局部候选集：共享实体的记录 + 相同内容指纹的记录。

        替代原 notary.assess 传入全量 all_records 的 O(N) 全量扫描，
        改为基于实体倒排 + 指纹索引的局部扫描。

        v7.0.0 性能上限：高频实体（如「记忆」「项目」）的倒排列表会随库增长，
        每实体只取最近 per_entity 条、总候选封顶 max_scan，避免 O(N) 退化。
        """
        self._ensure_notary_index()
        entities = record.get("entities") or []
        th = (record.get("meta") or {}).get("template_hash")
        if th is None:
            th = hashlib.sha256(
                MemoryBrain._normalize_for_hash(content).encode("utf-8", errors="surrogatepass")
            ).hexdigest()[:16]
        candidates = []
        seen = set()
        # 1) 指纹候选（精确重复检测，全部纳入）
        for r in self._fingerprint_index.get(th, []):
            rid = r.get("id")
            if rid not in seen:
                seen.add(rid)
                candidates.append(r)
        # 2) 实体候选：每个实体只取最近 per_entity 条（列表按写入序追加，尾部最新）
        for e in entities:
            postings = self._entity_index.get(e, [])
            for r in postings[-per_entity:]:
                rid = r.get("id")
                if rid not in seen:
                    seen.add(rid)
                    candidates.append(r)
                    if len(candidates) >= max_scan:
                        return candidates
        return candidates

    def retain(self, content: str, mtype: str = "semantic", fast: bool = False,
               project: Optional[str] = None, **kwargs: Any) -> str:
        """写入一条记忆。project 可选项目名用于多项目隔离。
fast=True 跳过实体详抽/图谱边/冲突检测（批量快数倍）；**向量索引仍然保留**，
因为语义检索依赖它（见本函数 fast 分支的说明）。
时序版本追踪：相同 template_hash 自动递增 version。
返回值类型稳定为 str（memory_id）。"""
        import hashlib
        # v7.0.0-MCP：取出调用方显式意图，避免被下游覆盖。
        # confidence —— Notary 评估会无条件改写 record["confidence"]（见下方
        #   assessment 段），因此先取出，评估后再还原为调用方的值，并把 Notary
        #   的原始判断留档在 meta["notary_confidence"]（不丢信息）。
        # supersedes —— 更正路径：本记忆写入后，把被取代的旧记忆标为 superseded。
        # tags 无需特别处理，_build_record 直接接收。
        _explicit_confidence = kwargs.pop("confidence", None)
        _supersedes_id = kwargs.pop("supersedes", None)
        # v7.0.0 阶段4：字段级脱敏——密码/邮箱/卡号/密钥等敏感值先改写为掩码，
        # 再参与实体抽取、嵌入、指纹与落盘；公证器注入检测仍使用原文，以保留
        # 敏感字段的告警能力。脱敏汇总写入 record["meta"]["redactions"]。
        original_content = content
        content, redactions = _redact_sensitive_fields(content)
        # BuildRecord
        if fast:
            # 快速Path：Build最小Record（skip_detailed 跳过实体详情的 dict 抽取，
            # 保留轻量实体名以支撑图谱/公证器；内存与 CPU 关键优化）
            record = _build_record(content, mtype=mtype, skip_detailed=True, **kwargs)
            if not record.get("event_time"):
                record["event_time"] = _extract_event_time(content, record["created_at"])
            # skips：entities_detailed(重) + graph_edges + conflict_detection
            # 但保留 entities（轻量实体名，_build_record 已抽取），以支撑图谱实体关系网络
            record["entities_detailed"] = []
            record["graph_edges"] = []
            # 向量索引：快速Path 必须保留，否则语义检索对这批记忆完全失效。
            # 依据：语义后端下 retrieval.py 的 vec_weight 是主导信号（0.55），而
            # n_vec 只在 record["embedding"] 非空时才计算（retrieval.py:494）——
            # 只写向量后端而留空 embedding，候选即使被 Path2a 召回也会以 n_vec=0
            # 排在末尾，等于白召回。故这里一次 encode，同时喂给 SQLite 与向量后端。
            # （此前此处固定置 None 且从不调用 add()，导致 MCP 写入的记忆全部丢失
            # 语义索引；这是本项目阶段四发现并修复的缺陷 F1。）
            record["embedding"] = None
            if self.embed_engine and self.enable_embeddings:
                try:
                    record["embedding"] = self.embed_engine.encode(content)
                except Exception as exc:
                    record["embedding"] = None
                    logger.debug("可选功能降级，忽略异常：%s", exc)
                if record.get("embedding") and hasattr(self.embed_engine, "add"):
                    try:
                        self._note_vector_op("add", self.embed_engine.add(
                            record["id"], record["embedding"],
                            tags=record.get("tags"), mtype=record.get("type")))
                    except Exception as exc:
                        self._note_vector_op("add", False, exc)
        else:
            record = _build_record(content, mtype=mtype, **kwargs)
            if not record.get("event_time"):
                record["event_time"] = _extract_event_time(content, record["created_at"])
            if self.enable_graph and record.get("entities_detailed"):
                rels = _extract_relationships(record["entities_detailed"], content)
                record["graph_edges"] = rels
                if self.graph_store:
                    self.graph_store.add_edges(rels, memory_id=record["id"])
                # sqlite 后端：边同时写入其 edges 表
                if hasattr(self.store, "add_edges") and self.store is not self.graph_store:
                    try:
                        self.store.add_edges(rels, memory_id=record["id"])
                    except Exception as exc:
                        logger.debug("可选功能降级，忽略异常：%s", exc)
            if self.embed_engine and self.enable_embeddings:
                record["embedding"] = self.embed_engine.encode(content)
                # 向量后端插件：同步写入其向量索引，供 retrieve() 的 search()
                # 语义候选召回（阶段3 修复：此前插件向量索引从未被填充）
                if record.get("embedding") and hasattr(self.embed_engine, "add"):
                    try:
                        self._note_vector_op("add", self.embed_engine.add(
                            record["id"], record["embedding"],
                            tags=record.get("tags"), mtype=record.get("type")))
                    except Exception as exc:
                        self._note_vector_op("add", False, exc)
            conflicts = self._detect_conflicts_at_write(record)
            if conflicts:
                record["meta"]["write_conflicts"] = conflicts
        
        # --- v7.0.0: Notary assessment（局部扫描：实体倒排 + 指纹索引，替代全量 all_records）---
        try:
            # 注入/凭据检测用原文（脱敏后内容已无凭据模式），指纹/交叉印证也用原文。
            local_candidates = self._notary_candidates(record, original_content)
            assessment = self.notary.assess(record, original_content, local_candidates)
            record["confidence"] = assessment["confidence"]
            record["flags"] = assessment["flags"]
            # v7.0.0: notary_evidence（供安全报告 / MCP 未授权检测用）
            is_susp, inj_score, inj_flags = self.notary.check_injection(original_content)
            record["notary_evidence"] = {
                "injection": {"flags": inj_flags, "score": inj_score, "suspicious": is_susp},
                "duplicate_ids": assessment.get("duplicate_ids", []),
                "fingerprint": assessment.get("fingerprint", ""),
            }
        except Exception:
            record.setdefault("confidence", 0.7)
            record.setdefault("flags", [])
        # v7.0.0-MCP：调用方显式 confidence 优先于 Notary 启发式评估。
        # 依据：Notary 的 confidence 是对"内容可信度"的启发式打分，永远落在
        # 0.7 左右；而规则要求"写入时必须设定 confidence"，那是用户/Agent 对
        # 这条记忆的确定性判断，语义更强，不能被覆盖（原实现是静默丢弃）。
        # Notary 的原始值不丢：留档到 meta["notary_confidence"]。
        if _explicit_confidence is not None:
            try:
                _conf = max(0.0, min(1.0, float(_explicit_confidence)))
            except (TypeError, ValueError):
                _conf = None
            if _conf is not None:
                record.setdefault("meta", {})
                record["meta"]["notary_confidence"] = record.get("confidence")
                record["confidence"] = _conf
        # v7.0.0 阶段4：脱敏汇总写入 meta，并追加 redacted:* 告警 flags 供审计追溯。
        if redactions:
            record.setdefault("meta", {})
            record["meta"]["redactions"] = redactions
            record.setdefault("flags", [])
            for r in redactions:
                flag = "redacted:" + r["type"]
                if flag not in record["flags"]:
                    record["flags"].append(flag)
        
        # --- project 隔离 ---
        if project:
            record["project"] = project
        
        # --- 时序版本追踪（v5.2: O(1) 索引查找替代 O(N) 全量扫描）---
        did_rewrite = False
        record["version"] = 1
        record.setdefault("meta", {})
        record["meta"]["template_hash"] = hashlib.sha256(
            MemoryBrain._normalize_for_hash(content).encode("utf-8", errors="surrogatepass")
        ).hexdigest()[:16]
        old = self._ensure_template_index().get(record["meta"]["template_hash"])
        if old and old[2] != "deleted":
            # 轻量元组索引命中 → 按需取完整旧记录（supersede 是低频路径）
            full_old = self.store.find_by_id(old[0])
            if full_old is not None:
                full_old["superseded_by"] = record["id"]
                record["version"] = int(old[1]) + 1
                record["supersedes"] = old[0]
                self.store.rewrite(
                    [r for r in self.store.all_records() if r["id"] != old[0]]
                    + [full_old])
                did_rewrite = True

        # ---- v7.0.2 (P2-3): 写入侧语义去重（近似重复 → 并入既有记忆）----
        # 位置：紧跟"精确重复（template_hash 相同）走版本递增"之后、落盘之前。
        # 精确重复已被上一步吃掉，这里处理的是**近似重复**（0.95 ≤ sim < 1.0）：
        # 同一条偏好被反复重述会堆成多条高度相似的 active 记忆（实测 dedup 报出
        # 5 对 0.98~0.986 的重复），它们白占 ANN 候选位、让 Top-K 被同一事实的多个
        # 变体塞满 —— 事前收口比事后 dedup 更省也更准。
        # 边界：显式 `supersedes=` 的更正路径**不参与**（用户明确要求版本链）。
        if _supersedes_id is None and not did_rewrite:
            _dd_on, _dd_thr = _write_dedupe_config()
            if _dd_on:
                try:
                    _dup, _dup_sim = self._find_near_duplicate(record, content, _dd_thr)
                except Exception as exc:
                    _dup, _dup_sim = None, 0.0
                    logger.debug("写入侧去重：检测失败，按新增处理：%s", exc)
                if _dup is not None:
                    _merged_id = self._merge_near_duplicate(_dup, record, content, _dup_sim)
                    if _merged_id:
                        return _merged_id
                    # 合并落库失败 → 继续走正常新增路径（宁可重复，绝不丢数据）
        
        # v7.0.0: crypto 加密（content 字段静态加密，密钥缺失时静默跳过）
        if getattr(self, "crypto_plugin", None) is not None and getattr(self.crypto_plugin, "available", False):
            try:
                record["content"] = self.crypto_plugin.encrypt("content", record["content"])
            except Exception as exc:
                logger.debug("可选功能降级，忽略异常：%s", exc)
        # writes 
        self.store.append(record)
        # v7.0.0: 增量更新公证器局部扫描索引（实体倒排 + 指纹）
        self._index_record(record)
        # v7.0.0: Audit log
        self.store.audit_log({
            "ts": _now_iso(),
            "actor": self.actor,
            "action": "retain",
            "target_id": record.get("id", ""),
            "details": {"content_preview": content[:50]},
        })
        # v7.0.0: 可信度历史（notary assessment 记录）
        try:
            self.store.add_confidence_history(record.get("id", ""), {
                "ts": _now_iso(),
                "confidence": record.get("confidence", 0.7),
                "reason": "notary_assess",
                "flags": record.get("flags", []),
            })
        except Exception as exc:
            logger.debug("可选功能降级，忽略异常：%s", exc)
        # v7.0.0: 账本追加（内存账本，独立于后端）
        if self.ledger is not None:
            try:
                self.ledger.append("retain", memory_id=record.get("id", ""),
                                   data_summary={"content_preview": content[:50]})
            except Exception as exc:
                logger.debug("可选功能降级，忽略异常：%s", exc)
        # v7.0.0: 外部 provider 双写（写路由）
        if self.external_provider and self._external_write:
            try:
                self.external_provider.retain(content, mtype=mtype, **kwargs)
            except Exception as exc:
                logger.debug("可选功能降级，忽略异常：%s", exc)
        # v5.2 增量索引：append 后增量更新倒排（版本追踪 rewrite 时跳过，下次全量重建保一致）
        if not did_rewrite:
            self.retrieval.incremental_add(self.store, record)
        # 更新 template 索引指向最新版本（O(1)）
        # 更新 template 索引指向最新版本（O(1)，存轻量元组）
        self._template_index[record["meta"]["template_hash"]] = (
            record["id"], record.get("version", 1),
            record.get("status", "active"), record.get("superseded_by"),
        )
        if self.stats_tracker:
            self.stats_tracker.track_retain(len(content), content=content)
            self.last_stats = self.stats_tracker.summary()
        if self._show_stats and self.stats_tracker:
            self._stats_line("写入", f"+{len(content)}字符")
        # v7.0.0-MCP：更正路径——把被本次写入取代的旧记忆标记为 superseded。
        # 放在 append 之后：新记忆已落库，superseded_by 指向的 id 一定存在。
        if _supersedes_id:
            try:
                self._mark_superseded(_supersedes_id, record["id"])
            except Exception as exc:
                logger.debug("标记 superseded 失败：%s", exc)
        # v7.0.0：返回值类型统一稳定为 str（不再因 _stats_auto 条件返回 tuple）
        return record["id"]

    def retain_batch(self, items: List[Any], fast: bool = False) -> List[Dict[str, Any]]:
        """批量写入。items: [(content, mtype, kwargs), ...]
        fast=True: 跳过实体详抽/图谱构建；**向量索引仍然保留**（语义检索依赖它）"""
        records = []
        for item in items:
            content, mtype = item[0], item[1] if len(item) > 1 else "semantic"
            kwargs = item[2] if len(item) > 2 else {}
            rec = _build_record(content, mtype=mtype, **kwargs)
            if fast:
                rec["entities"] = []
                rec["entities_detailed"] = []
                rec["graph_edges"] = []
            # 向量索引：批量路径同样必须保留（语义检索依赖它）。此处同时修掉一个
            # 既有缺陷——旧代码的 fast=False 分支只把 embedding 写进 record，从不
            # 调用向量后端的 add()，因此经 retain_batch 写入的记忆从未进入向量索引
            # （MCP 的 retain_batch 与 Python SDK 的 retain_batch 都受影响）。
            rec["embedding"] = None
            if self.embed_engine and self.enable_embeddings:
                try:
                    rec["embedding"] = self.embed_engine.encode(content)
                except Exception as exc:
                    rec["embedding"] = None
                    logger.debug("可选功能降级，忽略异常：%s", exc)
                if rec.get("embedding") and hasattr(self.embed_engine, "add"):
                    try:
                        self.embed_engine.add(rec["id"], rec["embedding"],
                                              tags=rec.get("tags"),
                                              mtype=rec.get("type"))
                    except Exception as exc:
                        logger.debug("可选功能降级，忽略异常：%s", exc)
            records.append(rec)
        self.store.append_batch(records)
        # v7.0.2: Audit log（对齐单条 retain 的审计链）。
        # 根因：retain_batch 此前从不写 audit_log / confidence_history，
        # 导致经批量路径写入的记忆在 audit() 查询里 0 条记录——
        # "用户明确让记、说记住了，但审计链查无此记"的根因即此。
        # 对齐单条 retain（行 865 / 874）：写入即留审计链 + 可信度轨迹。
        for rec in records:
            _rid = rec.get("id", "")
            _content = rec.get("content", "") or ""
            try:
                self.store.audit_log({
                    "ts": _now_iso(),
                    "actor": self.actor,
                    "action": "retain_batch",
                    "target_id": _rid,
                    "details": {"content_preview": _content[:50],
                                "mtype": rec.get("type", "semantic"),
                                "tags": rec.get("tags", [])},
                })
                self.store.add_confidence_history(_rid, {
                    "ts": _now_iso(),
                    "confidence": rec.get("confidence", 0.7),
                    "reason": "retain_batch",
                })
                self._index_record(rec)
            except Exception as exc:
                logger.debug("可选功能降级，忽略异常：%s", exc)
        return records

    def _detect_conflicts_at_write(self, new_record):
        """writes 时检测与已有记忆的冲突。"""
        records = self.store.all_records()
        conflicts = []
        new_ents = set(new_record.get("entities") or [])
        if not new_ents:
            return []
        for r in records:
            if r.get("status") == "deleted" or r.get("_corrupt"):
                continue
            r_ents = set(r.get("entities") or [])
            common = new_ents & r_ents
            if common and r.get("id") != new_record.get("id"):
                # 比较事实Type是否矛盾
                if (r.get("fact_type") == "fact" and new_record.get("fact_type") == "fact"
                        and r.get("content", "")[:30] != new_record.get("content", "")[:30]):
                    similarity = _cosine(
                        _tf_vector(_tokenize(r.get("content", ""))),
                        _tf_vector(_tokenize(new_record.get("content", ""))),
                    )
                    if similarity > 0.3:
                        conflicts.append({
                            "conflicting_id": r["id"],
                            "shared_entities": list(common)[:5],
                            "existing_content": r.get("content", "")[:80],
                        })
        return conflicts[:5]

    # ---- 记忆检索 ----

    def recall(self, query: str, k: int = 5, project: Optional[str] = None,
               compress: bool = False, compress_level: int = 2,
               budget_tokens: Optional[int] = None,
               mtype: Optional[str] = None,
               tag: Optional[str] = None,
               tags: Optional[list] = None,
               **kwargs: Any) -> Union[List[Any], Tuple[List[Any], Dict[str, Any]]]:
        """检索记忆。project: 可选，只检索该项目下的记忆。自动记录命中率和延迟。

        结构化过滤（混合检索）：
        - mtype: 按记忆类型精确过滤（如 "preference"）
        - tag:   按单标签过滤
        - tags:  按多标签 OR 过滤（命中任一 tag 即保留；与 mtype 为 AND 关系）
        向量语义通道（Qdrant 插件）会把 mtype/tags 作为 payload filter 透传；
        SQLite/FTS5 通道在 retrieve 入口做记录级过滤。

        返回值：默认稳定为 list（(score, record, reasons) 列表）；
        budget_tokens 设置时显式返回 (results, cost_report) 元组。
        """
        import time
        t0 = time.time()

        # v7.0.2 (P1-1) 收尾：存量数据的图边懒回填。
        # 为什么放在 recall 入口：图通道（5 路融合中权重 0.10）依赖 graph.jsonl /
        # edges 表里的边，而 7.0.1 之前的所有写入都**没有产生任何边**（抽取器打不出
        # 三元组）。只修抽取器只会让"新写入"有图，老记忆仍然查不到 —— 用户视角就是
        # "明明记过，图谱里却没有"。回填本身是幂等的（进程内标志 + 目录内标记文件），
        # 且失败/无图可补都只是 no-op，绝不影响 recall 主路径。
        if not self._graph_backfilled:
            try:
                self._backfill_graph_edges()
            except Exception as exc:
                logger.debug("图边回填失败（忽略，不影响检索）：%s", exc)
                self._graph_backfilled = True
        
        # v7.0.0: Budget-constrained recall
        if budget_tokens is not None:
            return self._budget_recall(query, budget_tokens, k=k, project=project,
                                       mtype=mtype, tag=tag, tags=tags, **kwargs)
        
        results = self.retrieval.retrieve(self.store, query, k=k,
                                           mtype=mtype, tag=tag, tags=tags,
                                           **kwargs)
        # v7.0.0: crypto 解密（读取时还原 content 字段）
        if getattr(self, "crypto_plugin", None) is not None and getattr(self.crypto_plugin, "available", False):
            _dec = []
            for _item in results:
                if isinstance(_item, (list, tuple)) and len(_item) >= 2 and isinstance(_item[1], dict):
                    try:
                        _item[1]["content"] = self.crypto_plugin.decrypt(
                            "content", _item[1].get("content", ""))
                    except Exception as exc:
                        logger.debug("可选功能降级，忽略异常：%s", exc)
                _dec.append(_item)
            results = _dec
        # v7.0.0: 外部 provider 路由（local / external / hybrid）
        if self.external_provider and self._external_read in ("external", "hybrid"):
            try:
                raw = self.external_provider.recall(query, k=k)
            except Exception:
                raw = []
            ext_results = []
            for item in raw or []:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    score = item[0]
                    if isinstance(item[1], dict):
                        rec = item[1]
                    else:
                        rec = {"content": item[1], "external": True}
                    ext_results.append((score, rec))
                else:
                    ext_results.append((0.5, {"content": str(item), "external": True}))
            if self._external_read == "external":
                results = ext_results
            else:  # hybrid：合并（去重）
                seen_ids = {r[1].get("id") if isinstance(r, (list, tuple)) and len(r) > 1 and isinstance(r[1], dict) else None
                            for r in results}
                for e in ext_results:
                    eid = e[1].get("id") if isinstance(e[1], dict) else None
                    if eid is None or eid not in seen_ids:
                        results = list(results) + [e]
        # --- project 过滤 ---
        if project:
            results = [(s, r, x) for s, r, *x in [(r_[0], r_[1], r_[2] if len(r_) > 2 else None) for r_ in results] 
                       if r.get("project") == project]
        hit = len(results) > 0
        if results:
            self._touch_recalled(results)
        # v7.0.2 (P2-2)：累积召回质量指标（recall_health 只读工具的数据来源）
        self._accumulate_recall_health(
            getattr(self.retrieval, "last_recall_metrics", None),
            latency_ms=(time.time() - t0) * 1000)
        # --- 分层协同：BM25 不足 K 个时，回调外部向量库（L2）补充 ---
        if self.semantic_hook and len(results) < k:
            try:
                extra = self.semantic_hook(query, k - len(results))
                if extra:
                    results = list(results) + list(extra)
            except Exception as exc:
                logger.debug("可选功能降级，忽略异常：%s", exc)
        recalled_chars = sum(len(r[1].get("content", "")) if len(r) > 1 else 0 for r in results)
        latency_ms = (time.time() - t0) * 1000
        if self.stats_tracker:
            # v5.2: 用缓存 O(1) 替代 O(N) 全量扫描（百万级关键）
            potential_chars = self.retrieval._total_chars
            recalled_text = " ".join(r[1].get("content", "") if len(r) > 1 else "" for r in results)
            potential_text = " ".join(r.get("content", "") for r in self.retrieval._cached_records[:50]
                                      if not r.get("_corrupt") and r.get("status", "active") != "deleted")
            self.stats_tracker.track_recall(hit, recalled_chars, latency_ms, potential_chars,
                                            recalled_text=recalled_text, potential_text=potential_text)
            self.last_stats = self.stats_tracker.summary()
            if self._auto_display_stats:
                self.stats_tracker.print_summary()
        # 方向2: token 级压缩（召回后压缩，节省送入 LLM 的输入 Token，不影响检索）
        if compress and results:
            _compressed = []
            for _item in results:
                _s, _r = _item[0], _item[1]
                if isinstance(_r, dict) and _r.get("content"):
                    _r2 = dict(_r)
                    _r2["content"] = compress_text(_r["content"], compress_level)
                    _compressed.append((_s, _r2, *_item[2:]))
                else:
                    _compressed.append(_item)
            results = _compressed
        # v7.0.0：返回值类型统一稳定为 list（不再因 _stats_auto 条件返回 tuple）；
        # 统计快照通过 brain.last_stats 读取。
        return results

    def query_topic(self, topic, k=10):
        """按主题检索（方向3: 主题索引 O(1) 查找，替代 O(N) 全量扫描）。

topic: 主题词/标签，如 "商业化"、"技术架构"。
返回匹配的记录列表（按重要性排序）。"""
        self.retrieval._ensure_index(self.store)
        records = self.retrieval._cached_records
        idxs = self.retrieval._topic_index.get(topic, set())
        hits = [records[i] for i in idxs if i < len(records)]
        # 精确匹配不足 k 时，回退 topic_tag 子串匹配（兼容旧行为）
        if len(hits) < k:
            seen = {r.get('id') for r in hits}
            for r in records:
                if r.get('id') in seen:
                    continue
                topic_tag = r.get('topic_tag') or ''
                if topic in topic_tag:
                    hits.append(r)
                    seen.add(r.get('id'))
        hits.sort(key=lambda r: r.get("importance") or 0, reverse=True)
        return hits[:k]

    def preview(self, query, k=5, **kwargs):
        """预览本轮将送入大模型的 prompt 文本——不做任何调用，只展示。
        
        用途：让用户直接看到 Mnemosyne 过滤后实际送入 LLM 的内容，
              自己验证节省了多少 Token，不依赖引擎的 stats 统计。
        """
        results = self.retrieval.retrieve(self.store, query, k=k, **kwargs)
        lines = []
        for item in results:
            rec = item[1] if len(item) > 1 else item
            content = rec.get("content", "") if isinstance(rec, dict) else str(rec)
            lines.append(content)

        full_text = "\n---\n".join(lines)
        all_chars = sum(len(r.get("content", "")) for r in self.store.all_records()
                        if not r.get("_corrupt") and r.get("status", "active") != "deleted")
        preview_chars = len(full_text)
        saved_chars = all_chars - preview_chars

        # 估算 Token（~4 chars/token）
        all_tokens = all_chars // 4
        preview_tokens = preview_chars // 4
        saved_tokens = saved_chars // 4

        # 按 DeepSeek-V4-Pro 输入原价 ¥3/百万 Token 估算
        cost_without = all_tokens * 3 / 1_000_000
        cost_with = preview_tokens * 3 / 1_000_000

        return {
            "preview_text": full_text,
            "preview_chars": preview_chars,
            "all_memory_chars": all_chars,
            "all_memory_tokens_est": all_tokens,
            "preview_tokens_est": preview_tokens,
            "saved_tokens_est": saved_tokens,
            "saved_ratio": f"{saved_chars / max(all_chars, 1) * 100:.1f}%",
            "cost_without_mnemosyne_est": f"¥{cost_without:.4f}",
            "cost_with_mnemosyne_est": f"¥{cost_with:.4f}",
            "cost_saved_this_round_est": f"¥{cost_without - cost_with:.4f}",
        }

    # ---- Memory Reflection（增强版：认知级反思） ----

    def reflect(self, question=None, deep=False):
        """增强反思：计数 + 冲突 + 趋势 + 认知模式发现。"""
        records = [r for r in self.store.all_records()
                   if not r.get("_corrupt") and r.get("status") != "deleted"]
        insights = {
            "total": len(records),
            "by_type": dict(collections.Counter(r.get("type", "?") for r in records)),
            "by_layer": dict(collections.Counter(r.get("layer", "?") for r in records)),
            "by_fact_type": dict(collections.Counter(r.get("fact_type", "fact") for r in records)),
            "by_verification": dict(collections.Counter(r.get("verification", "unverified") for r in records)),
            "question": question,
        }

        # 高频实体
        ent_counter = collections.Counter()
        for r in records:
            for e in (r.get("entities") or []):
                ent_counter[e] += 1
        insights["top_entities"] = [{"entity": e, "count": c}
                                     for e, c in ent_counter.most_common(15) if c >= 2]

        # 冲突检测（基于实体+事实Type矛盾）
        conflicts = []
        ent_map = collections.defaultdict(list)
        for r in records:
            for e in (r.get("entities") or []):
                ent_map[e].append(r)
        for e, rs in ent_map.items():
            facts = [r for r in rs if r.get("fact_type") == "fact"]
            if len(facts) >= 2:
                contents = {r.get("content", "")[:60] for r in facts}
                if len(contents) >= 2:
                    conflicts.append({
                        "entity": e,
                        "type": "fact_conflict",
                        "memory_ids": [r["id"] for r in facts[:3]],
                        "summaries": list(contents)[:3],
                    })
        insights["conflicts"] = conflicts[:15]

        # 趋势：Time线密度
        monthly = collections.Counter()
        for r in records:
            monthly[r.get("created_at", "")[:7]] += 1
        insights["monthly_density"] = dict(sorted(monthly.items())[-12:])

        # 深度反思：认知模式Found 
        if deep and len(records) >= 10:
            insights["cognitive_patterns"] = self._discover_patterns(records)

        # 可信度分布
        confs = [r.get("confidence", 0.7) for r in records]
        if confs:
            insights["confidence_stats"] = {
                "mean": round(sum(confs) / len(confs), 3),
                "min": round(min(confs), 3),
                "max": round(max(confs), 3),
                "low_confidence_count": sum(1 for c in confs if c < 0.5),
            }

        return insights

    def _discover_patterns(self, records):
        """从记忆中自动发现行为/认知模式。"""
        patterns = []

        # 偏好聚合：从preference和reflectiveType提取
        prefs = [r for r in records if r.get("type") in ("preference", "reflective")]
        if prefs:
            freq_words = collections.Counter()
            for r in prefs:
                for tok in _tokenize(r.get("content", "")):
                    if len(tok) >= 2:
                        freq_words[tok] += 1
            patterns.append({
                "type": "preference_cluster",
                "frequent_themes": [w for w, c in freq_words.most_common(10) if c >= 2],
                "preference_count": len(prefs),
            })

        # 教训聚合
        lessons = [r for r in records if r.get("type") in ("procedural", "lesson")]
        if len(lessons) >= 3:
            patterns.append({
                "type": "lesson_summary",
                "total_lessons": len(lessons),
                "recent_lessons": [r.get("content", "")[:60] for r in lessons[-5:]],
            })

        # 身份信息一致性
        identity_recs = [r for r in records if r.get("type") == "identity"]
        if identity_recs:
            patterns.append({
                "type": "identity_profile",
                "count": len(identity_recs),
                "latest": identity_recs[-1].get("content", "")[:100] if identity_recs else "",
            })

        return patterns

    # ---- Memory Consolidation（Consolidation Engine） ----

    def consolidate(self, dry_run: bool = False, min_similarity: float = 0.6,
                    max_group: int = 5, generate_summary: bool = True) -> ConsolidationReport:
        """Memory Consolidation引擎：按同义归一化签名聚类，合并高度相关记忆。

        返回 ConsolidationReport。原记忆标记为 status="consolidated"，
        合并产物保留 status="active" 并携带 merged_ids。
        generate_summary=False 时仍执行合并，仅简化合并产物内容。
        """
        records = [r for r in self.store.all_records()
                   if not r.get("_corrupt") and r.get("status") in (None, "active")
                   and not r.get("consolidated_at")]
        if len(records) < 2:
            return ConsolidationReport(dry_run=dry_run)

        # 同义归一化签名聚类
        sig_groups = collections.defaultdict(list)
        for r in records:
            sig_groups[_content_signature(r.get("content", ""))].append(r)

        # 相似度编码（同义归一化后的 TF 向量）
        enc = {r["id"]: _tf_vector(_tokenize(_normalize_template_hash(r.get("content", ""))))
               for r in records}

        planned_groups = []
        for sig, group in sig_groups.items():
            if len(group) < 2:
                continue
            # 大组按 max_group 分块，逐块合并（避免 max_group 截断导致残留过多活跃记忆）
            for start in range(0, len(group), max(1, max_group)):
                chunk = group[start:start + max_group]
                if len(chunk) < 2:
                    continue
                sims = []
                for i in range(len(chunk)):
                    for j in range(i + 1, len(chunk)):
                        sims.append(_compute_pair_similarity(
                            chunk[i], chunk[j], enc,
                            embed_engine=self.embed_engine, store=self.store))
                avg_sim = sum(sims) / max(len(sims), 1)
                if avg_sim >= min_similarity:
                    planned_groups.append({
                        "ids": [r["id"] for r in chunk],
                        "avg_similarity": round(avg_sim, 3),
                        "size": len(chunk),
                    })

        report = ConsolidationReport(dry_run=dry_run, merges_planned=len(planned_groups))

        if not dry_run:
            executed = 0
            for cg in planned_groups:
                group_recs = [self.store.find_by_id(gid) for gid in cg["ids"]]
                group_recs = [r for r in group_recs if r]
                if len(group_recs) < 2:
                    continue
                # generate_summary=False 时仍合并，但合并产物内容简化为首条代表
                # （阶段3 修复：此前该参数仅存在于签名，未实际生效）
                if generate_summary:
                    summary_content = "；".join(r.get("content", "")[:80] for r in group_recs[:3])
                else:
                    summary_content = group_recs[0].get("content", "")[:80]
                common_entities = list(set(
                    e for r in group_recs for e in (r.get("entities") or [])[:5]))[:10]
                common_tags = list(set(t for r in group_recs for t in (r.get("tags") or [])))[:5]
                avg_importance = int(sum(r.get("importance", 3) for r in group_recs) / len(group_recs))
                avg_confidence = sum(r.get("confidence", 0.7) for r in group_recs) / len(group_recs)

                consolidated_rec = _build_record(
                    content=f"[Memory Consolidation] {summary_content[:200]}",
                    mtype=group_recs[0].get("type", "semantic"),
                    tags=common_tags + ["consolidated"],
                    importance=avg_importance,
                    confidence=round(avg_confidence, 2),
                    fact_type="inference",
                    source_type="agent_generated",
                    verification="unverified",
                )
                consolidated_rec["entities"] = common_entities
                consolidated_rec["merged_ids"] = cg["ids"]
                consolidated_rec["consolidated_from"] = cg["ids"]
                consolidated_rec["consolidated_at"] = _now_iso()
                consolidated_rec["id"] = _stable_id(summary_content, _unique_salt())
                if self.embed_engine and self.enable_embeddings:
                    consolidated_rec["embedding"] = self.embed_engine.encode(summary_content)

                self.store.append(consolidated_rec)
                # 同步向量索引：合并产物必须进向量后端。
                # 根因（实测）：上一行 if 已 encode 出 embedding 并随 append 落进
                # SQLite，但**从未调用向量后端的 add()** —— 于是合并产物对语义召回
                # 完全不可见（dsh 命名空间实测：2 条 [Memory Consolidation] 记录在
                # SQLite 有 4096B 向量 blob，却在 Qdrant 里没有任何点）。
                # 后果是把多条记忆合并成 1 条 active 记录，反而丢掉了 0.55 权重的
                # 语义通道，等于"越压缩越召回不到"。与 retain() 的写法保持一致。
                if consolidated_rec.get("embedding") and hasattr(self.embed_engine, "add"):
                    try:
                        self.embed_engine.add(
                            consolidated_rec["id"], consolidated_rec["embedding"],
                            tags=consolidated_rec.get("tags"),
                            mtype=consolidated_rec.get("type"))
                    except Exception as exc:
                        logger.debug("可选功能降级，忽略异常：%s", exc)
                for r in group_recs:
                    self.store.update_by_id(r["id"], {
                        "status": "consolidated",
                        "consolidated_at": _now_iso(),
                        "parent_id": consolidated_rec["id"],
                    })
                if self.ledger is not None:
                    try:
                        self.ledger.append("consolidate", memory_id=consolidated_rec["id"],
                                           data_summary={"merged": len(group_recs)})
                    except Exception as exc:
                        logger.debug("可选功能降级，忽略异常：%s", exc)
                executed += 1
            report.merges_executed = executed
            report.consolidated = sum(
                1 for r in self.store.all_records() if r.get("status") == "consolidated")
        report.groups = planned_groups[:10]
        return report

    # ---- 自学习循环 ----

    def self_learn(self, lookback_days=30):
        """自学习循环：分析近期交互，提炼可复用的行为为策略。

        输出策略记忆（strategyType），下次 Agent 可直接参考。
        """
        records = [r for r in self.store.all_records()
                   if not r.get("_corrupt") and r.get("status") != "deleted"]
        now = _utcnow_ts()
        recent = [
            r for r in records
            if now - _parse_ts(r.get("created_at", "")) < lookback_days * 86400
        ]
        if len(recent) < 5:
            return {"learned": 0, "strategies": [], "reason": "not_enough_data"}

        learnings = []

        # 1. 从纠正中学习（procedural/lesson Type）
        corrections = [r for r in recent if r.get("type") in ("procedural", "lesson")]
        if corrections:
            common = collections.Counter(
                t for r in corrections for t in _tokenize(r.get("content", "")) if len(t) >= 2
            )
            top_themes = [w for w, _ in common.most_common(8) if common[w] >= 2]
            if top_themes:
                strategy = _build_record(
                    content=f"常见问题模式：{', '.join(top_themes[:5])}。建议优先检查这些领域避免重复错误。",
                    mtype="strategy",
                    importance=4,
                    confidence=0.55,
                    source_type="agent_generated",
                    fact_type="inference",
                    tags=["self_learned", "strategy"],
                )
                learnings.append(("strategy", strategy))

        # 2. 从偏好一致性学习
        prefs = [r for r in recent if r.get("type") == "preference"]
        if len(prefs) >= 3:
            summary = "用户核心偏好模式："
            for p in prefs[-5:]:
                summary += f"「{p.get('content', '')[:40]}」; "
            strategy = _build_record(
                content=summary[:300],
                mtype="strategy",
                importance=4,
                confidence=0.6,
                source_type="agent_generated",
                fact_type="inference",
                tags=["self_learned", "preference"],
            )
            learnings.append(("strategy", strategy))

        # 3. 冲突parses 学习
        reflect_data = self.reflect()
        unresolved_conflicts = reflect_data.get("conflicts", [])
        if unresolved_conflicts:
            conflict_entities = [c["entity"] for c in unresolved_conflicts[:3]]
            strategy = _build_record(
                content=f"未解决的认知冲突涉及：{', '.join(conflict_entities)}。建议用户澄清或标记最新信息为准。",
                mtype="strategy",
                importance=3,
                confidence=0.45,
                source_type="agent_generated",
                fact_type="hypothesis",
                tags=["self_learned", "conflict"],
            )
            learnings.append(("strategy", strategy))

        # writes 学习Result
        strategy_count = 0
        for ltype, rec in learnings:
            if self.embed_engine and self.enable_embeddings:
                rec["embedding"] = self.embed_engine.encode(rec.get("content", ""))
            self.store.append(rec)
            strategy_count += 1

        return {
            "learned": strategy_count,
            "strategies": [{"content": rec.get("content", "")[:100], "tags": rec.get("tags", [])}
                           for _, rec in learnings],
        }

    # ---- 维护操作 ----

    def dedup(self, dry_run=False):
        """去重（增强版：指纹+相似度检测）。"""
        return _dedup(self.store, dry_run=dry_run, embed_engine=self.embed_engine)

    def expire(self):
        return _expire_old(self.store)

    def _note_vector_op(self, op, ok, exc=None):
        """向量后端写/删的成败记账（v7.0.2 P1-2）。

        【为什么需要】插件侧熔断器在 Qdrant 异常后会**直接 return**，
        之后 30 秒内（`MNEMOSYNE_QDRANT_BREAKER`）的 `add()` / `remove()`
        全部被**静默丢弃**，调用方无法区分"成功"与"丢弃"；而 `doctor` 过去
        不暴露插件统计 —— 结果是一次瞬时抖动会让一批记忆永久失去语义索引，
        且**无人知道**。这里把每次成败记账，并让"丢索引"变成可见告警。
        """
        key = "vec_%s_ok" % op if ok else "vec_%s_fail" % op
        self._vector_ops[key] = self._vector_ops.get(key, 0) + 1
        if not ok:
            det = self._vector_ops.setdefault("vec_fail_detail", [])
            if len(det) < 20:
                det.append({"op": op, "at": _now_iso(),
                            "error": (str(exc)[:160] if exc else None)})
            n = self._vector_ops[key]
            if n <= 5 or n % 50 == 0:
                logger.warning("向量后端 %s 未成功（第 %d 次）：该记忆的语义索引可能缺失，"
                               "权威数据已安全落库；请检查 Qdrant / 嵌入服务",
                               op, n)
        return ok

    def _backfill_graph_edges(self, limit: int = 5000, force: bool = False):
        """存量数据的图边懒回填（v7.0.2 P1-1 收尾）。

        【为什么需要】P1-1 修好了抽取器（`A的B是C` → `用户 --is_a--> RTX 4090
        (qualifier=显卡)`），但那只对**新写入**生效。7.0.1 及以前入库的记忆，
        其 `graph_edges` 必然为空 —— 因为当时的抽取器连一条三元组都打不出来
        （`graph_query("用户")` 实测恒为空列表）。于是"修好代码"之后用户仍会看到
        "我明明记过，图谱里却没有"，这是典型的"改了但没生效"。

        【做法】一次性扫描活跃记录，对**尚未在图里出现过**的（按 memory_id 判定）
        用当前抽取器重算三元组并写入 graph.jsonl 与 SQLite edges 表。
        幂等保证：
          • 进程内标志 `self._graph_backfilled`
          • 数据目录内的标记文件 `.graph_edges_backfilled_v702`
            （跨进程也不重复做；`force=True` 可强制重跑）
        安全边界：只**新增**边，绝不修改/删除任何记忆记录；单条抽取异常即跳过；
        `limit` 限制单次回填的边数上限，避免首启卡顿（剩余部分下次调用继续）。
        返回统计字典，便于 doctor / 诊断脚本读取。
        """
        if self.graph_store is None or not self.enable_graph:
            self._graph_backfilled = True
            return {"skipped": "graph_disabled"}
        marker = os.path.join(self.base_dir, ".graph_edges_backfilled_v702b")
        if not force:
            if self._graph_backfilled:
                return {"skipped": "done_in_process"}
            if os.path.exists(marker):
                self._graph_backfilled = True
                return {"skipped": "marker_present"}

        # 0) v7.0.2（D9）修存量脏边：7.0.1 及以前写入的边 `memory_id` 全为 null。
        #    必须先清掉，否则唯一索引 (from,to,relation,qualifier) 会让下面重建的
        #    "带正确 memory_id 的同一条边"被 INSERT OR IGNORE 静默忽略 ——
        #    脏数据把正确数据挡在门外，且不报错。边是派生索引，删除安全。
        purged = 0
        try:
            purged += int(self.graph_store.purge_edges_without_memory_id() or 0)
        except Exception as exc:
            logger.debug("图边回填：清理 graph.jsonl 脏边失败：%s", exc)
        if hasattr(self.store, "purge_edges_without_memory_id") \
                and self.store is not self.graph_store:
            try:
                purged += int(self.store.purge_edges_without_memory_id() or 0)
            except Exception as exc:
                logger.debug("图边回填：清理 edges 表脏边失败：%s", exc)
        if purged:
            logger.info("图边回填：清理了 %d 条 memory_id 为空的旧边（7.0.1 遗留）",
                        purged)

        # 1) 已有边的 memory_id 集合（判定"这条记忆是否已被图覆盖"）
        try:
            existing = {e.get("memory_id") for e in self.graph_store.iter_edges()
                        if e.get("memory_id")}
        except Exception as exc:
            logger.debug("图边回填：读取既有边失败，跳过本次：%s", exc)
            self._graph_backfilled = True
            return {"skipped": "read_edges_failed"}

        # 2) 找出缺口并重算三元组
        new_edges = []
        scanned = 0
        covered = 0
        try:
            records = self.store.all_records()
        except Exception as exc:
            logger.debug("图边回填：读取记录失败，跳过本次：%s", exc)
            self._graph_backfilled = True
            return {"skipped": "read_records_failed"}
        for r in records:
            if len(new_edges) >= limit:
                break
            if not isinstance(r, dict):
                continue
            if r.get("status") == "deleted" or r.get("_corrupt"):
                continue
            rid = r.get("id")
            if not rid:
                continue
            scanned += 1
            if rid in existing:
                covered += 1
                continue
            content = r.get("content") or ""
            if not content:
                continue
            try:
                rels = _extract_relationships(r.get("entities_detailed") or [],
                                              content)
            except Exception:
                continue
            for rel in rels:
                if len(new_edges) >= limit:
                    break
                e = dict(rel)
                e["memory_id"] = rid
                new_edges.append(e)

        # 3) 分批写入（图存储 + SQLite edges 表，与 retain 路径保持一致）
        written = 0
        for i in range(0, len(new_edges), 500):
            chunk = new_edges[i:i + 500]
            try:
                self.graph_store.add_edges(chunk)
                written += len(chunk)
            except Exception as exc:
                logger.debug("图边回填：写入 graph.jsonl 失败：%s", exc)
            if hasattr(self.store, "add_edges") and self.store is not self.graph_store:
                try:
                    self.store.add_edges(chunk)
                except Exception as exc:
                    logger.debug("图边回填：写入 edges 表失败：%s", exc)

        # 4) 边变了 → 检索侧的图通道缓存必须失效
        if written:
            try:
                self._invalidate_retrieval_index()
            except Exception:
                pass
        # 5) 落标记（写失败不影响本次结果，下次重做即可）
        try:
            with open(marker, "w", encoding="utf-8") as f:
                f.write(json.dumps({"at": _now_iso(), "edges": written,
                                    "scanned": scanned, "covered": covered,
                                    "purged_stale": purged},
                                   ensure_ascii=False))
        except Exception as exc:
            logger.debug("图边回填：标记写入失败：%s", exc)
        self._graph_backfilled = True
        if written:
            logger.info("图边回填完成：扫描 %d 条记录，补齐 %d 条边"
                        "（原已覆盖 %d 条，清理旧边 %d 条）",
                        scanned, written, covered, purged)
        return {"scanned": scanned, "covered": covered, "edges_written": written,
                "purged_stale": purged}

    # ---- v7.0.2 (AIC)：记忆胶囊与精确展开 ----

    def _capsule_of(self, record, budget_tokens, token_counter=None):
        """构建一条记忆的胶囊（内层封装，统一 token 计数口径）。"""
        return build_capsule(record, budget_tokens, token_counter=token_counter,
                             use_cache=True)

    @staticmethod
    def _capsule_record(record, cap):
        """把胶囊渲染成一条"可返回的记录"（不修改原记录对象）。

        为什么复制而不是原地改：`_cached_records` 里的 dict 是跨调用共享的索引
        对象，原地改写 content 会污染整个检索索引（原文被胶囊覆盖）—— 那将是
        灾难性的。复制一份只用于本次输出。
        """
        mini = dict(record)
        mini["content"] = cap["text"]
        mini["_capsule"] = {
            "ref": cap["ref"], "display_ref": cap["display_ref"],
            "level": cap["level"], "tokens": cap["tokens"],
            "min_tokens": cap.get("min_tokens"),
            "original_tokens": cap["original_tokens"], "ratio": cap["ratio"],
            "lossless": cap["lossless"], "atoms": cap["atoms"],
        }
        return mini

    def _load_record_for_ref(self, pr):
        """按指针取记录：先精确命中 id，再退化为前缀匹配（短指针）。"""
        rid = pr.get("id") or ""
        rec = None
        try:
            rec = self.store.find_by_id(rid)
        except Exception:
            rec = None
        if rec is not None:
            return rec
        try:
            for r in self.store.all_records():
                if str(r.get("id") or "").startswith(rid):
                    return r
        except Exception as exc:
            logger.debug("按指针前缀查找失败：%s", exc)
        return None

    def expand(self, ref, verify=True):
        """按指针取回**原文**（极限上下文精确压缩的逆运算）。

        这是"极短上下文也能 100% 精准"的最后一环：胶囊把上下文压到几十个
        token，里面只有指针 + 事实 + 原子；模型一旦需要原文（引用、复述、
        核对细节），用 `expand(ref)` 逐字取回 —— 因此压缩**从不丢信息**，
        只是把它分层存放。

        `verify=True` 时校验内容哈希：若库里的正文与指针声明的不一致
        （记录被改写 / id 复用 / 指针抄错），返回 ``verified=False`` 并给出
        实际哈希，**不静默交付可疑内容**。
        """
        pr = parse_ref(ref) if isinstance(ref, str) else None
        if pr is None:
            return {"error": "指针格式非法（应形如 m:<id>#<hash>[@级别]）",
                    "ref": ref, "verified": False}
        rec = self._load_record_for_ref(pr)
        if rec is None:
            return {"error": "指针指向的记忆不存在（可能已被物理删除）",
                    "ref": ref, "verified": False}
        content = rec.get("content") or ""
        if getattr(self, "crypto_plugin", None) is not None \
                and getattr(self.crypto_plugin, "available", False):
            try:
                content = self.crypto_plugin.decrypt("content", content)
            except Exception as exc:
                logger.debug("可选功能降级，忽略异常：%s", exc)
        actual = content_hash(content, 16)
        want = (pr.get("hash") or "")
        verified = True
        if verify and want:
            verified = actual.startswith(want.lower())
        return {
            "ref": make_ref(rec.get("id") or "", content, pr.get("level")),
            "memory_id": rec.get("id"),
            "level": pr.get("level"),
            "lossless": True,
            "content": content,
            "content_chars": len(content),
            "content_tokens": estimate_tokens(content),
            "content_hash": actual,
            "verified": bool(verified),
            "verification_note": (None if verified else
                                  "内容哈希与指针不符：该记忆可能已被改写，"
                                  "请以本字段返回的正文为准并重新取指针"),
            "type": rec.get("type") or rec.get("mtype") or "semantic",
            "tags": rec.get("tags") or [],
            "created_at": rec.get("created_at") or "",
            "verification": rec.get("verification", "unverified"),
            "superseded_by": rec.get("superseded_by"),
            "status": rec.get("status", "active"),
        }

    def capsule(self, memory_id_or_ref, budget_tokens=None):
        """取单条记忆的胶囊（按 id 或指针）。

        用途：Agent 只需"这一条的关键事实"而不要全文时，直接取胶囊 ——
        比 `recall` 更省（无需检索），比 `expand` 更省 token，
        且**原子守恒**（数字/日期/型号/URL 全在）。
        """
        if budget_tokens is None:
            budget_tokens = 60
        # 参数既可以是完整/短指针（m:<id>[#hash][@level]），也可以直接给 memory_id
        pr = parse_ref(memory_id_or_ref) if isinstance(memory_id_or_ref, str) else None
        rec = self._load_record_for_ref(pr) if pr else None
        if rec is None and isinstance(memory_id_or_ref, str):
            # 退化为"直接按 id 取"（也支持 id 前缀，方便手工调用）
            rec = self._load_record_for_ref({"id": memory_id_or_ref})
        if rec is None:
            return {"error": "记忆不存在（memory_id 或 ref 未命中）",
                    "memory_id": memory_id_or_ref}
        content = rec.get("content") or ""
        cap = self._capsule_of(rec, int(budget_tokens))
        ok, problems = capsule_selfcheck(cap, content)
        return {
            "ref": cap["ref"], "display_ref": cap["display_ref"],
            "memory_id": cap["memory_id"], "level": cap["level"],
            "text": cap["text"], "atoms": cap["atoms"], "facts": cap["facts"],
            "tokens": cap["tokens"], "min_tokens": cap["min_tokens"],
            "original_tokens": cap["original_tokens"], "ratio": cap["ratio"],
            "lossless": cap["lossless"], "over_budget": cap["over_budget"],
            "selfcheck_ok": ok, "selfcheck_problems": problems,
            "note": ("胶囊只含指针/事实/原子，原子（数字·日期·金额·型号·URL 等）"
                     "在任何层级都完整保留；需要原文请用 expand(ref)。"),
        }

    def _find_near_duplicate(self, record, content, threshold):
        """找与 *content* 高度相似的既有活跃记忆（v7.0.2 P2-3 写入侧语义去重）。

        候选池用向量后端的 ANN 检索（O(log n)），再用同维度 cosine 精判 ——
        只有真正 ≥ threshold、**type 一致**、且**内容原子完全相同**才算近似重复。

        【为什么必须加"原子相同"这一道】纯相似度判重会**改错事实**：
        "用户的年假是 5 天" 与 "用户的年假是 15 天" 只差一个字符，cosine 实测
        0.95+，只看相似度就会把后者并进前者 —— 结果是**新事实被旧事实吃掉**，
        而用户与审计都看不出发生了什么。这是"记忆系统越用越不准"的最恶劣形态。
        因此凡"改一个字就是另一个事实"的片段（数字/日期/金额/型号/URL/邮箱/
        引号原话，见 `utils._content_atoms`）不同者，**相似度再高也绝不合并**。
        方向取舍：宁可漏合并（多留一条近重复）也绝不误合并（改写事实）。

        向量后端不可用时退化为最近写入的若干条 + 词频相似度（纯本地）。
        """
        pool = []
        vec = record.get("embedding")
        if vec and hasattr(self.embed_engine, "search"):
            try:
                for mid, _sim in self.embed_engine.search(vec, top_k=10):
                    if mid:
                        pool.append(mid)
            except Exception as exc:
                logger.debug("写入侧去重：向量候选获取失败：%s", exc)
        if not pool:
            try:
                pool = [r.get("id") for r in self.store.all_records()
                        if r.get("status") != "deleted"][-40:]
            except Exception as exc:
                logger.debug("写入侧去重：退化候选获取失败：%s", exc)
                pool = []
        new_atoms = _content_atoms(content)
        best, best_sim = None, 0.0
        for mid in pool:
            if not mid or mid == record.get("id"):
                continue
            try:
                r = self.store.find_by_id(mid)
            except Exception:
                r = None
            if not r or r.get("status") == "deleted" or r.get("_corrupt"):
                continue
            if (r.get("type") or "semantic") != (record.get("type") or "semantic"):
                continue
            # 原子守恒硬约束：原子多重集不同 → 不是重复，是**另一个事实**。
            if _content_atoms(r.get("content") or "") != new_atoms:
                continue
            try:
                if vec and r.get("embedding") is not None \
                        and self.embed_engine is not None:
                    sim = float(self.embed_engine.similarity(vec, r["embedding"]))
                else:
                    sim = float(_compute_pair_similarity(
                        {"content": content, "id": record.get("id")}, r, {},
                        embed_engine=None))
            except Exception:
                continue
            if sim > best_sim:
                best, best_sim = r, sim
        if best is not None and best_sim >= threshold:
            return best, round(best_sim, 4)
        return None, 0.0

    def _merge_near_duplicate(self, old, new_record, content, sim):
        """把近似重复的新写入**并入**既有记忆，而不是新增一条（v7.0.2 P2-3）。

        为什么在写入侧收口：同一条偏好被反复重述（"我喜欢大窑汽水" /
        "我爱喝大窑汽水"）会在库里堆成多条高度相似的活跃记忆 —— 实测 `dedup`
        能报出 5 对 0.98~0.986 的重复。它们白占 ANN 候选位，并让 Top-K 被同一
        事实的多个变体塞满（"越写越糊"）。事前收口比事后 dedup 更省，也更准。
        合并规则：内容以最新表述为准、tags 取并集、confidence/importance 取高者、
        version+1；向量随内容重算并**覆盖同一个点**（id 不变，无需删点）。
        返回既有记忆的 id（库内 active 条目不增加）。
        """
        import hashlib as _hl
        merged = dict(old)
        merged["content"] = content
        merged["tags"] = list(dict.fromkeys(
            (old.get("tags") or []) + (new_record.get("tags") or [])))
        try:
            merged["confidence"] = max(float(old.get("confidence") or 0.7),
                                       float(new_record.get("confidence") or 0.7))
        except (TypeError, ValueError):
            pass
        try:
            merged["importance"] = max(int(old.get("importance") or 3),
                                       int(new_record.get("importance") or 3))
        except (TypeError, ValueError):
            pass
        merged["version"] = int(old.get("version", 1)) + 1
        merged["updated_at"] = _now_iso()
        if self.embed_engine is not None and self.enable_embeddings:
            try:
                _emb = self.embed_engine.encode(content)
                if _emb:
                    merged["embedding"] = _emb
            except Exception as exc:
                logger.debug("写入侧去重：重算向量失败：%s", exc)
        merged.setdefault("meta", {})
        if isinstance(merged["meta"], dict):
            merged["meta"]["merge_dedup"] = {"similarity": sim, "at": _now_iso()}
        merged["meta"]["template_hash"] = _hl.sha256(
            MemoryBrain._normalize_for_hash(content)
            .encode("utf-8", errors="surrogatepass")).hexdigest()[:16]
        try:
            self.store.rewrite([r for r in self.store.all_records()
                                if r.get("id") != merged["id"]] + [merged])
        except Exception as exc:
            logger.warning("写入侧去重：合并落库失败，回退为新增：%s", exc)
            return None
        if merged.get("embedding") and hasattr(self.embed_engine, "add"):
            try:
                self._note_vector_op("add", self.embed_engine.add(
                    merged["id"], merged["embedding"],
                    tags=merged.get("tags"), mtype=merged.get("type")))
            except Exception as exc:
                self._note_vector_op("add", False, exc)
        try:
            self.store.audit_log({
                "ts": _now_iso(), "actor": self.actor,
                "action": "retain_merge_dedup",
                "target_id": merged["id"],
                "details": {"similarity": sim, "content_preview": content[:50]},
            })
            self.store.add_confidence_history(merged["id"], {
                "ts": _now_iso(), "confidence": merged.get("confidence", 0.7),
                "reason": "write_dedup_merge", "flags": merged.get("flags", []),
            })
        except Exception as exc:
            logger.debug("写入侧去重：审计写入失败：%s", exc)
        if self.ledger is not None:
            try:
                self.ledger.append("retain_dedup", memory_id=merged["id"],
                                   data_summary={"similarity": sim,
                                                 "content_preview": content[:50]})
            except Exception as exc:
                logger.debug("写入侧去重：账本追加失败：%s", exc)
        self._invalidate_retrieval_index()
        self._template_index = None  # 强制下次重建（内容已变）
        if self.stats_tracker:
            self.stats_tracker.track_retain(len(content), content=content)
            self.last_stats = self.stats_tracker.summary()
        return merged["id"]

    def _touch_recalled(self, results):
        """更新命中记录的访问计数。

        v7.0.0 优化：只遍历命中结果（O(k)），不再全量扫描 O(N)——
        100k 规模下这是每次 recall 的关键热点。sqlite 后端把计数落库，
        并同步检索索引指纹，避免下一次 recall 触发全量重建。

        v7.0.2（P0-1）：**计数语义收窄**。旧语义是"只要进入结果列表就算命中"，
        于是与被查内容无关、只是被顺带列出的记忆也会累积 access_count，
        再经 retrieval 的 access_boost 反哺排名 → 正反馈回路：
        会话越长偏置越强，实测能把正确答案从 rank1 挤到 rank3。
        新语义：仅当该条融合分落入 top1 的 90% 以内（即真被排到前位、
        可能是用户要的那条）才 +1；计数上限 5，与打分侧
        `min(access_count, 5)` 对齐（超过 5 已无边际影响）。
        """
        now = _now_iso()
        _scores = [float(r[0]) for r in results
                   if isinstance(r, (tuple, list)) and r
                   and isinstance(r[0], (int, float))]
        _gate = (max(_scores) * 0.90) if _scores else None
        touched = {}
        for r in results:
            rec = r[1] if isinstance(r, (tuple, list)) and len(r) > 1 else r
            if not (isinstance(rec, dict) and rec.get("id")):
                continue
            if (_gate is not None and isinstance(r, (tuple, list)) and r
                    and isinstance(r[0], (int, float)) and float(r[0]) < _gate):
                continue  # 尾部搭便车的记录不计数（原先会）
            rid = rec["id"]
            rec["access_count"] = min((rec.get("access_count") or 0) + 1,
                                      _ACCESS_COUNT_CAP)
            rec["last_accessed_at"] = now
            touched[rid] = {"access_count": rec["access_count"],
                            "last_accessed_at": now}
        if not touched:
            return
        if self.store_backend == "sqlite" and hasattr(self.store, "update_by_id"):
            for rid, upd in touched.items():
                try:
                    self.store.update_by_id(rid, upd)
                except Exception as exc:
                    logger.debug("访问计数落库失败：%s", exc)
            # v7.0.2：把同一批更新同步回**共享检索索引记录**。
            # 为什么现在必须显式同步：`retrieve()` 改为返回记录**副本**（消除跨调用
            # 别名污染，见 `retrieval._snapshot`），于是上面就地改的是副本；不同步
            # 的话索引里那份 access_count 要等下次索引重建才更新，等于"访问计数在
            # 会话内不生效"。这里 O(1) 补齐，语义与落库一致。
            for rid, upd in touched.items():
                try:
                    self.retrieval.sync_record(rid, upd)
                except Exception as exc:
                    logger.debug("访问计数同步索引失败：%s", exc)
            # 同步索引指纹：访问计数 UPDATE 会改变 db mtime，避免下次检索全量重建
            try:
                self.retrieval._indexed_fingerprint = self.retrieval._store_fingerprint(self.store)
            except Exception:
                pass

    # ---- v7.0.2 (P2-2): 召回质量监控 ----

    def _accumulate_recall_health(self, metrics, latency_ms=None):
        """累积召回质量指标（v7.0.2 P2-2）。

        为什么需要：7.0.1 的"越聊越偏"（access_boost 正反馈）是靠**人工对照实验**
        才挖出来的 —— 没有指标面，这类"随会话时长退化"的质量问题只能靠运气发现。
        这里把每轮 recall 的返回条数 / top1 分数 / 是否触发相关性下限 / 命中通道
        分布 / 延迟落进环形缓冲，`recall_health` 只读工具即可直接读出趋势。
        """
        if not metrics:
            return
        h = self._recall_health
        h["n"] += 1
        if metrics.get("empty"):
            h["empty"] += 1
        h["floor_dropped"] += int(metrics.get("floor_dropped") or 0)
        h["returned_sum"] += int(metrics.get("returned") or 0)
        if metrics.get("top1") is not None:
            h["top1_sum"] += float(metrics["top1"])
            h["top1_n"] += 1
        for k_, v_ in (metrics.get("bands") or {}).items():
            h["bands"][str(k_)] = h["bands"].get(str(k_), 0) + int(v_)
        for k_, v_ in (metrics.get("channels") or {}).items():
            h["channels"][str(k_)] = h["channels"].get(str(k_), 0) + int(v_)
        ring = h["recent"]
        ring.append({
            "at": _now_iso(),
            "returned": metrics.get("returned"),
            "top1": metrics.get("top1"),
            "band": metrics.get("top1_band"),
            "floor_dropped": metrics.get("floor_dropped"),
            "empty": metrics.get("empty"),
            "cache_hit": metrics.get("cache_hit"),
            "latency_ms": (round(float(latency_ms), 2)
                           if latency_ms is not None else None),
        })
        if len(ring) > _RECALL_HEALTH_RING:
            del ring[:len(ring) - _RECALL_HEALTH_RING]

    def recall_health(self):
        """召回质量只读指标（v7.0.2 P2-2）。

        输出：调用次数 / 空结果率 / 触发相关性下限被剔除的条数 / 平均返回条数 /
        平均 top1 分 / 置信带分布 / 通道命中分布 / 延迟 p50·max /
        向量后端写删成败（P1-2）/ 非法字符替换计数（P2-4）/ 最近 20 轮明细。
        用途：跑一段真实会话后看"precision / 空结果率 / top1 分"是否随时间退化。
        """
        h = self._recall_health
        n = h["n"]
        lat = sorted(x["latency_ms"] for x in h["recent"]
                     if x.get("latency_ms") is not None)
        try:
            from .retrieval import (REL_HIGH_SEMANTIC, REL_MIN_NGRAM,
                                    REL_MIN_SEMANTIC)
            thresholds = {"rel_min_semantic": REL_MIN_SEMANTIC,
                          "rel_min_ngram": REL_MIN_NGRAM,
                          "rel_high_semantic": REL_HIGH_SEMANTIC}
        except Exception:
            thresholds = {}
        return {
            "namespace": self.namespace,
            "recall_calls": n,
            "empty_results": h["empty"],
            "empty_rate": round(h["empty"] / n, 4) if n else 0.0,
            "floor_dropped_items": h["floor_dropped"],
            "avg_returned": round(h["returned_sum"] / n, 3) if n else 0.0,
            "avg_top1": round(h["top1_sum"] / h["top1_n"], 4) if h["top1_n"] else None,
            "bands": dict(h["bands"]),
            "channels": dict(h["channels"]),
            "latency_ms": {"n": len(lat),
                           "p50": lat[len(lat) // 2] if lat else None,
                           "max": lat[-1] if lat else None},
            "vector_backend_ops": dict(self._vector_ops),
            "encoding_replacements": encode_replaced_stats(),
            # v7.0.2 (AIC)：胶囊缓存命中率 —— 直接反映"上下文压缩是否稳定"
            # （命中率越高，同一记忆在同一预算下产出的胶囊越一致，
            #  上游 LLM 的前缀/KV 缓存越容易复用）。
            "capsule_cache": capsule_cache_stats(),
            "graph_edges": (None if self.graph_store is None else
                            len(self.graph_store.all_edges())),
            "thresholds": thresholds,
            "recent": list(h["recent"][-20:]),
        }

    def overwrite(self, memory_id: str, new_content: str, **kwargs: Any) -> Optional[str]:
        """覆盖某条记忆：软删旧版 + 追加新版（版本控制，保留历史）。返回新记录 id，失败返回 None。"""
        records = self.store.all_records()
        old = None
        for r in records:
            if r.get("id") == memory_id:
                old = r
                break
        if not old:
            return None
        kwargs.pop("fast", None)  # _build_record 不接受 fast，这里统一走完整构建
        old["status"] = "deleted"
        old["deleted_at"] = _now_iso()
        new_rec = _build_record(new_content, mtype=old.get("type", "semantic"), **kwargs)
        new_rec["version"] = int(old.get("version", 1)) + 1
        new_rec["parent_id"] = memory_id
        new_rec["supersedes"] = memory_id
        old["superseded_by"] = new_rec["id"]
        # 向量：overwrite 经 _build_record 构建（该函数本身不编码），必须显式补上，
        # 否则新版记忆落库时 embedding 恒为 None —— 语义通道（权重 0.55）永久缺失。
        if self.embed_engine is not None and self.enable_embeddings:
            try:
                new_rec["embedding"] = self.embed_engine.encode(new_content)
            except Exception as exc:
                new_rec["embedding"] = None
                logger.debug("可选功能降级，忽略异常：%s", exc)
        records.append(new_rec)
        self.store.rewrite(records)
        # 同步向量索引：删旧点 + 写新点。与 forget/_dedup 同一根因 ——
        # overwrite 此前只改 SQLite，新版不 add、旧版点不回收。
        try:
            if new_rec.get("embedding") and hasattr(self.embed_engine, "add"):
                self._note_vector_op("add", self.embed_engine.add(
                    new_rec["id"], new_rec["embedding"],
                    tags=new_rec.get("tags"), mtype=new_rec.get("type")))
            if hasattr(self.embed_engine, "remove"):
                self._note_vector_op("remove", self.embed_engine.remove(memory_id))
        except Exception as exc:
            self._note_vector_op("upsert", False, exc)
        self._invalidate_retrieval_index()
        return new_rec["id"]

    def evict_lru(self, max_records: Optional[int] = None,
                  older_than_days: Optional[int] = None) -> int:
        """LRU 淘汰：软删最久未访问的记录。

max_records: 记忆上限，超过就淘汰最久未访问的（保留最近 max_records 条）。
older_than_days: 超过 N 天未访问即淘汰。
返回淘汰数量。"""
        active = [r for r in self.store.all_records() if r.get("status", "active") != "deleted"]
        if not active:
            return 0
        evicted = 0
        # 1) older_than_days：超过 N 天未访问 → 淘汰
        if older_than_days is not None:
            cutoff = _utcnow_ts() - older_than_days * 86400
            for r in active:
                la = r.get("last_accessed_at")
                if not la:
                    continue
                try:
                    ts = datetime.fromisoformat(la).timestamp()
                except Exception:
                    continue
                if ts < cutoff and r.get("status", "active") != "deleted":
                    r["status"] = "deleted"
                    r["deleted_at"] = _now_iso()
                    evicted += 1
        # 2) max_records：超过上限 → 淘汰最久未访问的
        active2 = [r for r in self.store.all_records() if r.get("status", "active") != "deleted"]
        if max_records is not None and len(active2) > max_records:
            active2.sort(key=lambda r: r.get("last_accessed_at") or r.get("created_at") or "")
            for r in active2[:len(active2) - max_records]:
                r["status"] = "deleted"
                r["deleted_at"] = _now_iso()
                evicted += 1
        if evicted:
            self.store.rewrite(self.store.all_records())
            self.store._invalidate_cache()
        return evicted

    def repair(self, dry_run=False):
        return _repair(self.store, dry_run=dry_run)

    # ---- 图Query ----

    def graph_query(self, entity: str, depth: int = 2) -> Dict[str, Any]:
        """返回 {query, nodes, edges} 格式的图谱查询结果。

        v7.0.2（P1-1）：
          • 入口先做一次存量图边懒回填 —— 否则 7.0.1 及以前入库的记忆在这里
            永远查不到（这是"改好了但用户看不到效果"的典型场景）。
          • edges 带出 `qualifier`（`A的B是C` 的属性限定词，如"显卡"），
            使"用户的显卡是什么"这类按属性反问可被回答。
        """
        # 存量数据回填（幂等；失败不影响查询本身）
        if not self._graph_backfilled:
            try:
                self._backfill_graph_edges()
            except Exception as exc:
                logger.debug("图边回填失败（忽略，不影响查询）：%s", exc)
                self._graph_backfilled = True
        # 优先走后端原生 graph_query（SqliteBackend 有）
        if self.store is not self.graph_store and hasattr(self.store, "graph_query"):
            try:
                return self.store.graph_query(entity, max_depth=depth)
            except Exception as exc:
                logger.debug("可选功能降级，忽略异常：%s", exc)
        if not self.graph_store:
            return {"query": entity, "nodes": [entity], "edges": []}
        nodes = {entity}
        edges = []
        try:
            all_edges = self.graph_store.all_edges() if hasattr(self.graph_store, "all_edges") else []
        except Exception:
            all_edges = []
        for e in all_edges:
            if e.get("from") == entity or e.get("to") == entity:
                _edge = {
                    "from": e.get("from"),
                    "relation": e.get("relation", "related_to"),
                    "to": e.get("to"),
                    "strength": e.get("strength"),
                }
                if e.get("qualifier"):
                    _edge["qualifier"] = e["qualifier"]
                edges.append(_edge)
                nodes.add(e.get("from"))
                nodes.add(e.get("to"))
        return {"query": entity, "nodes": sorted(nodes), "edges": edges}

    # ---- 运行统计 ----

    def stats(self):
        """返回运行统计字典。"""
        if not self.stats_tracker:
            return {"error": "统计未启用，请用 MemoryBrain(base_dir=..., enable_stats=True)"}
        return self.stats_tracker.summary()

    def memory_repair(self):
        """扫描并自动修复损坏的记忆数据。返回 (removed, kept)。"""
        return self.store.repair()

    def doctor(self) -> Dict[str, Any]:
        """健康检查——扫描记忆库完整性、记录数、磁盘空间。

        v7.0.2 新增三个诊断段（均为**增量**，旧键一个不改，向后兼容）：
          • `vector_ops`   —— 向量后端写/删成败记账 + 失败明细（P1-2）
          • `vector_backend` —— 插件自述健康度（Qdrant 可达性 / 点数 /
             已知点 id 数 / 熔断器剩余时间 / 各原因丢弃计数）（P1-2）
          • `graph`        —— 图边总数与回填状态（P1-1）
        为什么必须进 doctor：7.0.1 的 doctor 只看"记录数/损坏数/磁盘"，
        于是**"记忆都在、但语义索引丢了"、"图通道空转"这类故障完全隐形** ——
        doctor 报 healthy，用户却觉得"召回不准"。
        返回 dict: {status, total_records, active_records, corrupt_records,
        disk_free_mb, recommendation, vector_ops, vector_backend, graph}
        """
        import os
        records = self.store.all_records()
        active = [r for r in records if r.get("status", "active") != "deleted"]
        corrupt = [r for r in records if r.get("_corrupt")]
        try:
            # 跨平台磁盘余量：shutil.disk_usage 同时支持 Windows / Linux / macOS
            # （os.statvfs 在 Windows 上不存在，原实现静默返回 -1）
            du = shutil.disk_usage(self.base_dir)
            disk_mb = du.free // (1024 * 1024)
        except OSError as exc:
            logger.debug("doctor() 磁盘余量获取失败：%s", exc)
            disk_mb = -1

        # --- v7.0.2 (P1-2)：向量后端可观测性 ---
        _vb = None
        try:
            _eng = self.embed_engine
            if _eng is not None and hasattr(_eng, "health"):
                _vb = _eng.health()
        except Exception as exc:
            _vb = {"error": str(exc)[:200]}
        # --- v7.0.2 (P1-1)：图边统计 ---
        _graph = {"enabled": bool(self.enable_graph and self.graph_store is not None),
                  "backfilled": bool(self._graph_backfilled)}
        try:
            if self.graph_store is not None:
                _edges = self.graph_store.all_edges()
                _graph["edges"] = len(_edges)
                _graph["entities"] = len({e.get("from") for e in _edges}
                                         | {e.get("to") for e in _edges})
                _graph["with_qualifier"] = sum(
                    1 for e in _edges if e.get("qualifier"))
        except Exception as exc:
            _graph["error"] = str(exc)[:200]

        diag = {
            "status": "healthy" if not corrupt else "needs_repair",
            "total_records": len(records),
            "active_records": len(active),
            "corrupt_records": len(corrupt),
            "deleted_records": len(records) - len(active),
            "brain_dir": self.base_dir,
            "disk_free_mb": disk_mb,
            "recommendation": "Run brain.memory_repair()" if corrupt else "No issues found",
            "vector_ops": dict(self._vector_ops),
            "vector_backend": _vb,
            "graph": _graph,
        }
        # 向量写失败是"数据未丢但检索会变差"的隐性故障 —— 只提示、不改 status，
        # 因为它不影响权威数据的完整性判定（doctor 的 status 语义保持稳定）。
        _vfail = sum(v for k, v in self._vector_ops.items()
                     if k.startswith("vec_") and k.endswith("_fail"))
        if _vfail:
            diag["recommendation"] = (
                "向量后端有 %d 次写/删未成功（语义召回会退化，权威数据完好）；"
                "请检查 Qdrant 与嵌入服务，必要时重跑 backfill。%s"
                % (_vfail, diag["recommendation"]))
        return diag

    def temporal_query(self, entity=None, limit=20):
        """时序查询——返回按时间排序的记录版本链。
entity: 可选实体名过滤; limit: 最大返回数。
返回: [{id, content, version, supersedes, superseded_by, created_at}]"""
        records = self.store.all_records()
        if entity:
            records = [r for r in records if entity.lower() in r.get("content", "").lower()]
        # 按 created_at 升序，version 降序（最新版本在前）
        records.sort(key=lambda r: (r.get("created_at", ""), -(r.get("version", 1))), reverse=True)
        return [
            {
                "id": r["id"],
                "content": r.get("content", "")[:200],
                "version": r.get("version", 1),
                "supersedes": r.get("supersedes"),
                "superseded_by": r.get("superseded_by"),
                "created_at": r.get("created_at", "")
            }
            for r in records[:limit]
        ]

    def list_projects(self):
        """列出所有项目名"""
        projects = set()
        for r in self.store.all_records():
            p = r.get("project")
            if p:
                projects.add(p)
        return sorted(projects)

    def stats_print(self):
        """打印运行统计到控制台。"""
        if not self.stats_tracker:
            print("统计未启用。")
        else:
            self.stats_tracker.print_summary()

    def stats_auto(self, on=True):
        """【兼容保留】开启自动统计展示。

        v7.0.0 起 retain()/recall() 返回值类型统一稳定：
          retain() 始终返回 str（memory_id），recall() 始终返回 list。
        统计信息不再改变返回值类型，改为写入 brain.last_stats；
        如需每次操作后打印统计，可配合 show_stats() 或本方法开启自动展示。
        """
        self._auto_display_stats = on
        self._stats_auto = on  # 保留旧属性，避免外部读取报 AttributeError

    def graph_path(self, from_e, to_e, max_depth=3):
        if not self.graph_store:
            return {"error": "图存储未启用"}
        return self.graph_store.search_path(from_e, to_e, max_depth)

    # ---- exports /imports  ----

    def export(self, fmt="json", out_path=None):
        return _export(self.store, fmt=fmt, out_path=out_path)

    def import_file(self, path):
        return _import_file(self.store, path)

    # ---- searches 记忆 ----

    def search_capture(self, query, results_text, urls=None, title=None):
        return _capture_search(self.store, query, results_text, urls=urls, title=title)

    def should_research(self, query, max_age_days=7):
        return _should_research(self.store, query, max_age_days=max_age_days)

    # =========================================================================
    # v7.0.0: 外部 provider / 路由 / 用户画像 / 多租户 / 容量 / 账本 / 会话 / 矛盾 / 快照 / 交换协议
    # =========================================================================

    def add_external_provider(self, provider):
        """注册一个外部记忆 provider（实现 retain/recall/forget/status 接口）。"""
        self.external_provider = provider
        if provider not in self._external_providers:
            self._external_providers.append(provider)

    def set_external_routing(self, write=True, read="hybrid"):
        """配置外部 provider 路由：write（是否双写）、read（local/external/hybrid）。"""
        self._external_write = bool(write)
        self._external_read = read

    def set_profile(self, key: str, value: Any) -> Optional[bool]:
        if self.profile_manager is None:
            return None
        return self.profile_manager.set_profile(key, value)

    def get_profile(self, key: str) -> Any:
        if self.profile_manager is None:
            return None
        return self.profile_manager.get_profile(key)

    def delete_profile(self, key):
        if self.profile_manager is None:
            return False
        return self.profile_manager.delete_profile(key)

    def get_all_profiles(self):
        if self.profile_manager is None:
            return {}
        return self.profile_manager.get_all_profiles()

    def clone_namespace(self, source_ns: str, target_ns: str) -> Dict[str, Any]:
        """克隆命名空间（复制 source 到 target），返回 {source, target, cloned_records}。"""
        from storage import SqliteBackend
        src_backend = SqliteBackend(self.base_dir, namespace=source_ns)
        try:
            db_path = getattr(src_backend, "db_path", None)
            if not (db_path and os.path.exists(db_path)):
                raise FileNotFoundError(f"Namespace '{source_ns}' not found")
            src_backend.ensure_init()
            records = src_backend.all_records()
            tgt_backend = SqliteBackend(self.base_dir, namespace=target_ns)
            tgt_backend.ensure_init()
            cloned = 0
            for r in records:
                r2 = dict(r)
                tgt_backend.append(r2)
                cloned += 1
            tgt_backend.close()
        finally:
            src_backend.close()
        return {"source": source_ns, "target": target_ns, "cloned_records": cloned}

    def _active_count(self):
        """当前活跃记忆数量。"""
        return sum(1 for r in self.store.all_records() if r.get("status") in (None, "active"))

    def _check_capacity(self):
        """容量检查：活跃数超过 max_active_memories 时告警。"""
        active = self._active_count()
        alert = False
        if self.max_active_memories and active > self.max_active_memories:
            alert = True
        return {"alert": alert, "active_count": active,
                "max_active_memories": self.max_active_memories}

    def _status_info(self):
        """返回增强状态信息（容量/分层/账本/审计/存储）。"""
        records = self.store.all_records()
        total = len(records)
        active = sum(1 for r in records if r.get("status") in (None, "active"))
        deleted = total - active
        max_active = self.max_active_memories
        limit = max_active if max_active else total
        percentage = round(active / max(limit, 1) * 100, 1) if limit else 0.0
        alert = bool(max_active and active > max_active)
        tier_counts = collections.Counter(r.get("tier", "hot") for r in records)
        ledger_info = {"latest_hash": "", "valid": True, "total": 0}
        if self.ledger is not None:
            try:
                v = self.ledger.verify_chain()
                ledger_info = {"latest_hash": "", "valid": bool(v.get("valid", True)),
                               "total": v.get("total", 0)}
            except Exception as exc:
                logger.debug("可选功能降级，忽略异常：%s", exc)
        storage_usage = None
        try:
            du = shutil.disk_usage(self.base_dir)
            storage_usage = {"total_mb": round(du.total / 1e6, 2),
                             "used_mb": round(du.used / 1e6, 2),
                             "free_mb": round(du.free / 1e6, 2)}
        except Exception as exc:
            logger.debug("可选功能降级，忽略异常：%s", exc)
        capacity = {"active_count": active, "max_active_memories": max_active,
                    "percentage": percentage}
        return {
            "namespace": self.namespace or "default",
            "backend": "sqlite" if hasattr(self.store, "conn") else "jsonl",
            "active_count": active,
            "total_memories": total,
            "deleted_count": deleted,
            "max_active_memories": max_active,
            "limit": limit,
            "percentage": percentage,
            "alert": alert,
            "counts_per_tier": dict(tier_counts),
            "capacity": capacity,
            "ledger": ledger_info,
            "audit_log": [],
            "storage_usage": storage_usage,
        }

    def ledger_audit(self, memory_id):
        """返回某条记忆的账本审计链。"""
        if self.ledger is None:
            return []
        return self.ledger.audit(memory_id)

    def _ledger_append(self, action, memory_id=None, data_summary=None):
        """追加一条账本记录（web_server 等内部调用）。无账本时静默跳过。"""
        if self.ledger is None:
            return None
        try:
            return self.ledger.append(action, memory_id=memory_id, data_summary=data_summary)
        except Exception:
            return None

    def verify_integrity(self) -> Dict[str, Any]:
        """校验账本链完整性，返回 {valid, total, first_broken_at}。"""
        if self.ledger is None:
            return {"valid": True, "total": 0, "first_broken_at": None}
        res = self.ledger.verify_chain()
        return {"valid": bool(res.get("valid", False)),
                "total": res.get("total", 0),
                "first_broken_at": res.get("first_broken_at")}

    def add_conversation_turn(self, session_id: str, role: str, content: str,
                              metadata: Optional[Dict[str, Any]] = None) -> Optional[int]:
        """追加一条会话轮次。"""
        if self.session_store is None:
            return None
        return self.session_store.append(session_id, role, content, metadata=metadata)

    def search_conversations(self, query: str, session_id: Optional[str] = None,
                             k: int = 10) -> List[Any]:
        """按关键词检索会话。"""
        if self.session_store is None:
            return []
        return self.session_store.search(query, session_id=session_id, k=k)

    def _import_conversation(self, conversation, session_id=None):
        """导入一段对话（轮次列表），抽取并写入记忆，返回统计。"""
        from session.importer import import_conversation
        return import_conversation(self, conversation, session_id=session_id)

    def find_contradictions(self, entity: Optional[str] = None,
                            min_similarity: float = 0.3) -> List[Any]:
        """检测记忆间的矛盾事实。"""
        from security.contradiction import find_contradictions as _fc
        return _fc(self, entity=entity, min_similarity=min_similarity)

    def build_context_prompt(self, query=None, max_chars=2000):
        """构建冻结的上下文快照提示词。"""
        if self.snapshot_builder is None:
            return ""
        return self.snapshot_builder.build_context_prompt(query=query, max_chars=max_chars)

    def export_memories(self, export_dir: str, namespace: str = "default") -> Dict[str, Any]:
        """导出记忆为 JSONL + manifest，返回 {record_count, exported_path, manifest_path}。"""
        os.makedirs(export_dir, exist_ok=True)
        records = [r for r in self.store.all_records() if r.get("status") in (None, "active")]
        jsonl_path = os.path.join(export_dir, "memories.jsonl")
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        ledger_hash = ""
        if self.ledger is not None:
            try:
                v = self.ledger.verify_chain()
                ledger_hash = f"valid:{v.get('valid')}:{v.get('total', 0)}"
            except Exception as exc:
                logger.debug("可选功能降级，忽略异常：%s", exc)
        manifest = {
            "schema_version": "1.0",
            "export_timestamp": _now_iso(),
            "namespace": namespace,
            "latest_ledger_hash": ledger_hash,
            "record_count": len(records),
            "format": "jsonl",
        }
        manifest_path = os.path.join(export_dir, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        return {"record_count": len(records), "exported_path": jsonl_path,
                "manifest_path": manifest_path}

    def import_memories(self, import_dir: str, namespace: str = "default") -> Dict[str, Any]:
        """从 JSONL 导入记忆，返回 {imported, namespace}。"""
        jsonl_path = os.path.join(import_dir, "memories.jsonl")
        if not os.path.isfile(jsonl_path):
            raise FileNotFoundError(f"No memories.jsonl in {import_dir}")
        imported = 0
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                content = rec.get("content", "")
                if not content:
                    continue
                try:
                    self.retain(content, fast=True)
                    imported += 1
                except Exception:
                    continue
        if self.ledger is not None:
            try:
                self.ledger.append("import", data_summary={"imported": imported,
                                                           "namespace": namespace})
            except Exception as exc:
                logger.debug("可选功能降级，忽略异常：%s", exc)
        return {"imported": imported, "namespace": namespace}

    def claim(self, import_dir, namespace="default"):
        """声明/认领外部导出目录（import_memories 的别名）。"""
        return self.import_memories(import_dir, namespace=namespace)

    def demote_cycle(self, budget_bytes: int = 0) -> DemoteReport:
        """遗忘经济学：将低价值记忆迁移到 warm/cold 层（迁移而非删除）。

        流程：
          1. 按价值模型（importance×2 + access_count×0.05 + confidence×3
             + recency×2 + hit_rate）升序排序；
          2. 降级最低价值的 20%（至少 1 条）；budget_bytes>0 时持续降级直到
             预计活跃数据量 ≤ budget_bytes；
          3. hot→warm 仅改 tier；warm→cold 迁移到冷层 gzip 归档 + 布隆索引
             （sqlite 后端），JSONL 后端软删归档；
          4. 每次迁移事件写入哈希账本（demote，from_tier → to_tier）。
        """
        records = [r for r in self.store.all_records()
                   if r.get("status") in (None, "active")]
        if not records:
            return DemoteReport()
        ranked = sorted(records, key=lambda r: _memory_value(r))

        def _est_bytes(rec):
            try:
                return len(json.dumps(rec, ensure_ascii=False))
            except (TypeError, ValueError):
                return 512

        # 降级最低价值的 20%（至少 1 条）
        n_demote = max(1, int(len(ranked) * 0.2))
        if budget_bytes > 0:
            # 预算约束：追加降级直到降级后的活跃字节数 ≤ budget_bytes
            active_bytes = sum(_est_bytes(r) for r in ranked)
            while n_demote < len(ranked):
                remaining = active_bytes - sum(_est_bytes(r) for r in ranked[:n_demote])
                if remaining <= budget_bytes:
                    break
                n_demote += 1

        demoted_ids = [r["id"] for r in ranked[:n_demote]]
        migrations = 0
        cold_migrated = 0
        for mid in demoted_ids:
            rec = self.store.find_by_id(mid)
            if not rec:
                continue
            tier = rec.get("tier") or "hot"
            if tier == "cold":
                continue  # 已在冷层
            new_tier = "cold" if tier == "warm" else "warm"
            if new_tier == "cold" and hasattr(self.store, "archive_cold"):
                # 冷层：gzip 归档 + 布隆过滤器索引（主表移除，可恢复）
                n = self.store.archive_cold([mid])
                if n > 0:
                    cold_migrated += 1
                    migrations += 1
            elif new_tier == "cold":
                # JSONL 兼容后端：软删归档（archive 移动到 gzip）
                self.store.update_by_id(mid, {"status": "deleted",
                                              "deleted_at": _now_iso()})
                try:
                    self.store.archive()
                except Exception as exc:
                    logger.debug("JSONL 冷层归档失败：%s", exc)
                migrations += 1
            else:
                self.store.update_by_id(mid, {"tier": new_tier})
                migrations += 1
            # 迁移事件记入账本（迁移而非删除）
            if self.ledger is not None:
                try:
                    self.ledger.append("demote", memory_id=mid, data_summary={
                        "from_tier": tier, "to_tier": new_tier,
                    })
                except Exception as exc:
                    logger.debug("demote 账本记录失败：%s", exc)
        return DemoteReport(migrations_count=migrations,
                            demoted=demoted_ids,
                            cold_migrated=cold_migrated,
                            budget_bytes=budget_bytes, current_bytes=0)


# ============================================================================
# Part 8: 维护操作（增强版）
# ============================================================================

def _parse_ts(iso_str):
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0


def _dedup(store, dry_run=False, embed_engine=None):
    records = store.all_records()
    seen = {}
    merged = 0
    dropped = []
    similar_pairs = []
    out = []
    for r in records:
        if r.get("_corrupt"):
            out.append(r)
            continue
        fp = _stable_id(r.get("content", ""))
        if fp in seen:
            merged += 1
            dropped.append(r.get("id"))
            if not dry_run:
                continue
        seen[fp] = r
        out.append(r)
    if len(out) <= 300:
        # 每条内容只编码一次（O(n) 次 HTTP），而不是每个 pair 现编两次
        # （O(n²) 次）。300 条上限下后者最坏 ≈9 万次调用，足以让 dedup 假死。
        vecs = {}
        if embed_engine is not None:
            for r in out:
                try:
                    vecs[r["id"]] = embed_engine.encode(r.get("content", ""))
                except Exception:
                    pass
        for i in range(len(out)):
            for j in range(i + 1, len(out)):
                vi, vj = vecs.get(out[i]["id"]), vecs.get(out[j]["id"])
                if vi and vj:
                    sim = embed_engine.similarity(vi, vj)
                else:
                    sim = _cosine(
                        _tf_vector(_tokenize(out[i].get("content", ""))),
                        _tf_vector(_tokenize(out[j].get("content", ""))),
                    )
                if sim > 0.85 and out[i].get("content") != out[j].get("content"):
                    similar_pairs.append((out[i]["id"], out[j]["id"], round(sim, 3)))
    if not dry_run and merged > 0:
        store.rewrite(out)
        # 同步向量索引：回收被去重掉的记录的点。
        # 根因：store.rewrite(out) 只重建 SQLite，向量点原样留存 —— 与 forget
        # 的硬删除同一后果：SQLite 行已消失，检索层无从过滤，成为永久孤儿点，
        # 每去重一批就多污染一点 ANN 候选池（去重本该提升精度，实际反噬）。
        if embed_engine is not None and hasattr(embed_engine, "remove"):
            _rm_fail = 0
            for mid in dropped:
                if not mid:
                    continue
                try:
                    if embed_engine.remove(mid) is False:
                        _rm_fail += 1
                except Exception as exc:
                    _rm_fail += 1
                    if _rm_fail <= 3:
                        logger.debug("去重回收向量点失败：%s", exc)
            if _rm_fail:
                # v7.0.2 (P1-2)：失败必须可见 —— 否则"去重"会静默留下孤儿点，
                # 反而稀释 ANN 候选池（这与去重的初衷完全相反）。
                logger.warning("去重：%d/%d 个被去重记录的向量点未成功回收，"
                               "将成为孤儿点并稀释语义候选池", _rm_fail, len(dropped))
    return {"merged": merged, "similar_pairs": similar_pairs[:20], "dry_run": dry_run}


def _forget(store, memory_id):
    records = store.all_records()
    found = False
    for r in records:
        if r.get("id") == memory_id:
            found = True
            r["status"] = "deleted"
            r["deleted_at"] = _now_iso()
    if found:
        store.rewrite(records)
    return found


def _expire_old(store):
    records = store.all_records()
    changed = False
    for r in records:
        if r.get("expires_at") and r.get("status") != "deleted":
            try:
                exp = datetime.fromisoformat(r["expires_at"]).timestamp()
                if _utcnow_ts() > exp:
                    r["status"] = "deleted"
                    r["deleted_at"] = _now_iso()
                    changed = True
            except Exception as exc:
                logger.debug("可选功能降级，忽略异常：%s", exc)
    if changed:
        store.rewrite(records)
    return changed


def _repair(store, dry_run=False):
    store.ensure_init()
    if not os.path.exists(store.index_path):
        return {"ok": True, "corrupt": 0, "kept": 0, "backup": None, "dry_run": dry_run}
    corrupt_lines = []
    valid_records = []
    with open(store.index_path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                json.loads(line)
                valid_records.append(line)
            except json.JSONDecodeError:
                corrupt_lines.append(line_no)
    if not corrupt_lines:
        return {"ok": True, "corrupt": 0, "kept": len(valid_records), "backup": None, "dry_run": dry_run}
    if dry_run:
        return {"ok": True, "corrupt": len(corrupt_lines), "kept": len(valid_records),
                "backup": None, "lines": corrupt_lines[:50], "dry_run": True}
    backup_path = store.index_path + f".bak-{int(time.time())}"
    shutil.copy2(store.index_path, backup_path)
    tmp = store.index_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for line in valid_records:
            f.write(line + "\n")
    os.replace(tmp, store.index_path)
    meta = store.read_meta() or {}
    meta["count"] = len(valid_records)
    meta["repaired_at"] = _now_iso()
    store._write_meta(meta)
    return {"ok": True, "corrupt": len(corrupt_lines), "kept": len(valid_records),
            "backup": backup_path, "lines": corrupt_lines[:50], "dry_run": False}


# ============================================================================
# Part 9: searches 记忆操作
# ============================================================================

def _capture_search(store, query, results_text, urls=None, title=None):
    existing = [r for r in store.all_records()
                if r.get("type") == "web"
                and r.get("meta", {}).get("search_query") == query
                and r.get("status") != "deleted"]
    snippet = results_text.strip()[:500]
    source = {"kind": "web_search", "query": query, "urls": urls or [], "title": title}
    if existing:
        rec = existing[0]
        rec["content"] = snippet if snippet else rec["content"]
        rec["meta"]["capture_count"] = rec.get("meta", {}).get("capture_count", 1) + 1
        rec["updated_at"] = _now_iso()
        store.rewrite([r if r["id"] != rec["id"] else rec for r in store.all_records()])
        return {"updated": True, "id": rec["id"], "capture_count": rec["meta"]["capture_count"]}
    record = _build_record(
        content=snippet, mtype="web",
        tags=["web", "search"] + ([query[:20]] if query else []),
        source=source, importance=2,
        context=f"联网搜索沉淀：{query}",
        meta={"search_query": query, "capture_count": 1, "raw_urls": urls or []},
    )
    store.append(record)
    return {"updated": False, "id": record["id"], "capture_count": 1}


def _should_research(store, query, max_age_days=7):
    records = [r for r in store.all_records()
               if r.get("type") == "web" and r.get("status") != "deleted"]
    for r in records:
        q = r.get("meta", {}).get("search_query") or ""
        if q and (q == query or q in query or query in q):
            try:
                created = datetime.fromisoformat(r.get("created_at", "")).timestamp()
                age_days = (_utcnow_ts() - created) / 86400.0
            except Exception:
                age_days = 999
            return {
                "found": True, "memory_id": r["id"],
                "age_days": round(age_days, 1),
                "fresh": age_days <= max_age_days,
                "content": r.get("content", "")[:200],
                "urls": r.get("source", {}).get("urls", []) if r.get("source") else [],
            }
    return {"found": False}


# ============================================================================
# Part 10: exports /imports 
# ============================================================================

def _export(store, fmt="json", out_path=None):
    records = [r for r in store.all_records() if not r.get("_corrupt")]
    if fmt == "json":
        payload = {
            "schema": "mnemosyne-v2", "exported_at": _now_iso(),
            "count": len(records), "version": VERSION, "memories": records,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        suffix = ".json"
    else:
        lines = ["# Mnemosyne v8.0.0 记忆库导出 ", "", f"导出时间：{_now_iso()}    共 {len(records)} 条", ""]
        for r in records:
            lines.append(f"## [{r.get('type')}] [{r.get('fact_type', 'fact')}] {r.get('created_at', '')}")
            lines.append("")
            lines.append(r.get("content", ""))
            lines.append("")
            meta_line = f"- 标签：{', '.join(r.get('tags') or [])} | 可信度：{r.get('confidence', '?')} | 来源：{r.get('source_type', '?')}"
            lines.append(meta_line)
            lines.append("")
        text = "\n".join(lines)
        suffix = ".md"
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
        return out_path
    return text


def _import_file(store, path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    count = 0
    for m in data.get("memories", []):
        if not m.get("content"):
            continue
        m = dict(m)
        for k in ("_score", "_hit_reasons", "id"):
            m.pop(k, None)
        store.append(m)
        count += 1
    return count


# ============================================================================
# Part 11: Hindsight 对标评测（内置 Benchmark）
# ============================================================================

def _hindsights_bench(brain, test_count=200):
    """流水线自测：写入 / 检索 / 反思 / 巩固 / 自学习 / 图查询 6 项实测。

    仅输出本机实测指标（延迟、数量），不做任何打分自评。
    """
    print("=" * 64)
    print("  Mnemosyne v8.0.0 — 流水线自测（实测指标，不打分）")
    print("=" * 64)
    brain.ensure_init()

    results = {}

    # ---- 1. writes 机制Test ----
    print("\n[1/6] 写入机制测试...")
    test_items = [
        ("Alice 是 Acme 公司的首席工程师，负责 AI 平台架构设计。", "semantic"),
        ("Alex prefers conclusions first; answers must be brief.", "preference"),
        ("2026-08-07 完成了劳动仲裁一审起诉材料的提交至横琴法院。", "episodic"),
        ("教训：hermes config set 对含点的嵌套 key 会拆错，必须用 Python 直接改 config.yaml。", "procedural"),
        ("Hindsight 是开源 Agent 记忆系统，supports  retain/recall/reflect 三种核心操作。", "semantic"),
        ("根据过去 20 次交互，用户多次要求减少废话，偏好直接给结果。", "observation"),
        ("我认为未来 AI 记忆系统应当采用 Human-in-the-loop 模式。", "opinion"),
        ("经过分析，用户是结果导向型人格，建议先给结论再展开。", "belief"),
        ("公司政策A在2026年1月废止，政策B于2026年3月生效。", "semantic"),
        ("关键决策：选择零依赖纯Python实现而非依赖PostgreSQL+pgvector。", "semantic"),
    ]
    t0 = time.time()
    count = 0
    for i in range(test_count // len(test_items)):
        for content, mtype in test_items:
            brain.retain(content, mtype=mtype)
            count += 1
    write_ms = (time.time() - t0) * 1000 / max(count, 1)
    results["write_latency_ms"] = round(write_ms, 1)
    results["write_count"] = count
    print(f"  writes  {count} 条，平均 {write_ms:.1f}ms/条")

    # ---- 2. 检索Test ----
    print("\n[2/6] 检索能力测试...")
    queries = [
        ("Alice 在哪里工作？", "semantic"),
        ("Alex's answer preference", "preference"),
        ("劳动仲裁 横琴法院", "episodic"),
        ("hermes Config 教训", "procedural"),
        ("AI 记忆系统 架构", "semantic"),
        ("公司 政策 废止 生效", "semantic"),
        ("用户 人格 行为模式", "belief"),
    ]
    recall_times = []
    for q, _ in queries:
        t1 = time.time()
        hits = brain.recall(q, k=3)
        recall_times.append((time.time() - t1) * 1000)
    results["recall_latency_ms_avg"] = round(sum(recall_times) / len(recall_times), 1)
    results["recall_queries"] = len(queries)
    print(f"  平均检索延迟：{results['recall_latency_ms_avg']}ms")

    # ---- 3. 反思Test ----
    print("\n[3/6] 反思能力测试...")
    t1 = time.time()
    ref = brain.reflect(deep=True)
    ref_time = (time.time() - t1) * 1000
    results["reflect_latency_ms"] = round(ref_time, 1)
    results["reflect_conflicts"] = len(ref.get("conflicts", []))
    results["reflect_entities"] = len(ref.get("top_entities", []))
    results["reflect_cognitive_patterns"] = len(ref.get("cognitive_patterns", []))
    print(f"  Found 冲突 {results['reflect_conflicts']} ，实体 {results['reflect_entities']} ，"
          f"认知模式 {results['reflect_cognitive_patterns']} ")

    # ---- 4. Compress/巩固Test ----
    print("\n[4/6] Memory ConsolidationTest...")
    t1 = time.time()
    cons = brain.consolidate(min_similarity=0.4)
    cons_time = (time.time() - t1) * 1000
    results["consolidate_latency_ms"] = round(cons_time, 1)
    results["consolidate_groups"] = cons.get("consolidated", 0)
    print(f"  巩固 {results['consolidate_groups']} 组记忆")

    # ---- 5. 自学习Test ----
    print("\n[5/6] 自学习循环测试...")
    t1 = time.time()
    learn = brain.self_learn(lookback_days=365)
    learn_time = (time.time() - t1) * 1000
    results["self_learn_latency_ms"] = round(learn_time, 1)
    results["self_learn_strategies"] = learn.get("learned", 0)
    print(f"  generates  {results['self_learn_strategies']} 条策略")

    # ---- 6. 图QueryTest ----
    print("\n[6/6] Knowledge GraphTest...")
    if brain.graph_store:
        t1 = time.time()
        neighbors = brain.graph_query("Alice", depth=2)
        graph_time = (time.time() - t1) * 1000
        results["graph_latency_ms"] = round(graph_time, 1)
        results["graph_query_ok"] = "depth_0" in neighbors
        print(f"  图查询延迟：{graph_time:.1f}ms，结果正常：{results['graph_query_ok']}")
    else:
        results["graph_query_ok"] = False
        print("  图未启用")

    # ---- 实测汇总（不打分自评；大样本基准请用 benchmarks/benchmark.py）----
    print("\n" + "=" * 64)
    print("  实测汇总")
    print("=" * 64)
    for key, label in [
        ("write_latency_ms", "平均写入延迟(ms/条)"),
        ("recall_latency_ms_avg", "平均检索延迟(ms/次)"),
        ("reflect_latency_ms", "反思耗时(ms)"),
        ("consolidate_latency_ms", "巩固耗时(ms)"),
        ("self_learn_latency_ms", "自学习耗时(ms)"),
        ("graph_latency_ms", "图查询耗时(ms)"),
    ]:
        if key in results:
            print(f"  {label:<18}: {results[key]}")
    print(f"  写入条数: {results.get('write_count')} | 检索查询数: {results.get('recall_queries')} | "
          f"冲突数: {results.get('reflect_conflicts')} | 巩固组数: {results.get('consolidate_groups')} | "
          f"自学习策略数: {results.get('self_learn_strategies')}")
    return results


# ============================================================================
# Part 12: 基准Test
# ============================================================================

def _benchmark(brain, count=2000):
    print("\U0001f9ea Mnemosyne v8.0.0 性能基准测试")
    print("=" * 56)
    brain.ensure_init()

    t0 = time.time()
    for i in range(count):
        rec = _build_record(
            f"benchmark memory {i}: 项目 {i % 50} 的关键决策是选择模块化架构，负责人 Alice，Date 2026-08-07。",
            mtype="semantic", tags=["benchmark", f"proj{i % 50}"], importance=(i % 5) + 1,
        )
        brain.store.append(rec)
    write_elapsed = time.time() - t0

    total = len(brain.store.all_records())
    queries = ["模块化架构 决策", "Alice 负责人", "benchmark memory 17"]
    latencies = {}
    for q in queries:
        t1 = time.time()
        brain.recall(q, k=5)
        latencies[q] = (time.time() - t1) * 1000

    print(f"writes ：{count} 条用时 {write_elapsed:.2f}s（约 {count / max(write_elapsed, 1e-6):.0f} 条/秒）")
    print(f"当前库总量：{total} 条")
    print("-" * 56)
    print("检索延迟（5-Way Fusion）：")
    for q, ms in latencies.items():
        print(f"  [{q}] -> {ms:.1f} ms")
    print("-" * 56)
    print("注：本命令为小样本快测；10k/100k 规模权威基准请运行 benchmarks/benchmark.py")
    print("=" * 56)
    return {
        "write_count": count, "write_seconds": round(write_elapsed, 2),
        "write_per_sec": round(count / max(write_elapsed, 1e-6), 1),
        "total": total,
        "latency_ms": {q: round(ms, 1) for q, ms in latencies.items()},
    }


# ============================================================================
# Part 13: 演示
# ============================================================================

def _demo(brain):
    print("\U0001f9ea Mnemosyne v8.0.0 演示模式")
    print("=" * 50)
    brain.ensure_init()

    demo_items = [
        ("Alice 是 Acme 公司的首席工程师，负责 AI 平台架构。", "semantic"),
        ("Alex prefers conclusions first; answers must be brief.", "preference"),
        ("2026-08-07 完成了劳动仲裁一审起诉材料的提交。", "episodic"),
        ("Hindsight 是开源 Agent 记忆系统，supports  retain/recall/reflect。", "semantic"),
        ("我认为人 AI 记忆系统应该优先本地化、零依赖。", "belief"),
    ]
    for content, mtype in demo_items:
        # retain() 返回 str（memory_id）；此处需要完整记录，使用 retain_detailed
        rec = brain.retain_detailed(content, mtype=mtype)
        print(f"  \u2713 retain: [{mtype}/{rec.get('fact_type', '?')}] {content[:40]}... "
              f"(confidence={rec.get('confidence', '?')}, importance={rec.get('importance', '?')})")

    print("-" * 50)
    hits = brain.recall("Alice 在哪里工作？", k=3)
    print("\U0001f9e0 recall 'Alice 在哪里工作？':")
    for score, rec, reasons in hits:
        print(f"  -> [{rec.get('fact_type', '?')}] {rec['content'][:50]}  "
              f"(score={score:.3f}, {reasons})")

    print("-" * 50)
    print("  \u2705 演示通过：v7.0.2 引擎可用。")


# ============================================================================
# Part 14: CLI
# ============================================================================

