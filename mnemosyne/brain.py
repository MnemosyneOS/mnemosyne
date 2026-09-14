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

from .graph import (MemoryGraphStore, _cosine,)
from .models import (_auto_importance, _build_record, _default_confidence, _default_layer, _extract_event_time, _infer_fact_type, _infer_source_type, ConsolidationReport, DemoteReport,)
from .retrieval import (RetrievalEngine,)
from .storage import (MemoryStore,)
from .utils import (EmbeddingEngine, StatsTracker, _extract_relationships, _now_iso, _stable_id, _tf_vector, _tokenize, _utcnow_ts, compress_text, _memory_value, _normalize_template_hash, _content_signature, _compute_pair_similarity, _unique_salt, _redact_sensitive_fields,)

# === Constants (defined in package __init__) ===
import os as _os_init
VERSION = "7.0.1"
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
        """
        if memory_id:
            rec = self.store.find_by_id(memory_id)
            return [(None, rec)] if rec else []
        if not query:
            return []
        out = []
        for item in self.recall(query, k=int(k or 3)):
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
            })
        report = {"dry_run": bool(dry_run), "targets": resolved,
                  "count": len(resolved)}
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
        """Budget-constrained recall. Returns (results, cost_report)."""
        # Token counter
        def _count_tokens(text):
            if self.stats_tracker and hasattr(self.stats_tracker, '_tokenizer'):
                try:
                    return len(self.stats_tracker._tokenizer.encode(text))
                except Exception as exc:
                    logger.debug("可选功能降级，忽略异常：%s", exc)
            return max(1, len(text) // 4)

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
        scored = []
        for score, record, *rest in candidates:
            conf = record.get("confidence", 0.7) if isinstance(record, dict) else 0.7
            content = record.get("content", "") if isinstance(record, dict) else ""
            tok_count = _count_tokens(content)
            marginal = score * conf
            scored.append((marginal, score, record, content, tok_count))

        scored.sort(key=lambda x: x[0], reverse=True)

        for marginal, score, record, content, tok_count in scored:
            if tokens_consumed + tok_count > budget_tokens:
                continue
            # 边际信息量：与已选内容的重叠越大，边际价值越低（冗余惩罚，避免
            # 预算被近重复记忆占满；首条无已选内容，惩罚为 0）。
            redundancy = max((_bigram_overlap(content, c) for c in selected_contents),
                             default=0.0)
            marginal_adjusted = marginal * (1.0 - redundancy)
            selected.append((score, record, *rest))
            selected_contents.append(content)
            tokens_consumed += tok_count
            marginal_values.append(round(marginal_adjusted, 4))
            if len(selected) >= 20:  # Cap at top-20
                break
        
        # 计算 top_k_tokens：无预算时本应送入的全部候选 token 数。
        # _budget_recall 的候选池为 top-20（retrieve k=20），无预算即全部送入，
        # 因此基线 = 全部候选 token 之和；预算选择是其子集，故恒有
        # top_k_tokens >= tokens_consumed（token 经济学：预算只会省，不会多花）。
        top_k_tokens = sum(_count_tokens(r.get("content", "") if isinstance(r, dict) else "")
                           for _, r, *_ in candidates)
        
        cost_report = {
            "selected": [s[0] for s in selected],
            "selected_count": len(selected),
            "tokens_consumed": tokens_consumed,
            "tokens_saved": max(0, top_k_tokens - tokens_consumed),
            "top_k_tokens": top_k_tokens,
            "budget_tokens": budget_tokens,
            "query_tokens": _count_tokens(query),
            "marginal_values": marginal_values,
        }
        
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
              f"命中率{s.get('today_hit_rate',0):.0%} | 拦截未送入LLM≈{saved}Token | {detail}")

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
                MemoryBrain._normalize_for_hash(content).encode("utf-8")
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
                        self.embed_engine.add(record["id"], record["embedding"])
                    except Exception as exc:
                        logger.debug("可选功能降级，忽略异常：%s", exc)
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
                        self.embed_engine.add(record["id"], record["embedding"])
                    except Exception as exc:
                        logger.debug("可选功能降级，忽略异常：%s", exc)
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
            MemoryBrain._normalize_for_hash(content).encode("utf-8")
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
        """批量writes 。items: [(content, mtype, kwargs), ...]
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
                        self.embed_engine.add(rec["id"], rec["embedding"])
                    except Exception as exc:
                        logger.debug("可选功能降级，忽略异常：%s", exc)
            records.append(rec)
        self.store.append_batch(records)
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
               **kwargs: Any) -> Union[List[Any], Tuple[List[Any], Dict[str, Any]]]:
        """检索记忆。project: 可选，只检索该项目下的记忆。自动记录命中率和延迟。

        返回值：默认稳定为 list（(score, record, reasons) 列表）；
        budget_tokens 设置时显式返回 (results, cost_report) 元组。
        """
        import time
        t0 = time.time()
        
        # v7.0.0: Budget-constrained recall
        if budget_tokens is not None:
            return self._budget_recall(query, budget_tokens, k=k, project=project, **kwargs)
        
        results = self.retrieval.retrieve(self.store, query, k=k, **kwargs)
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
        """增强反思：counts  + 冲突 + 趋势 + 认知模式Found 。"""
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
        """从记忆中自动Found line为/认知模式。"""
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
        """自学习循环：analyzes 近期交互，提炼可复用的line为策略。

        Output策略记忆（strategyType），下 timesAgent可直接参考。
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
                    content=f"常见问题模式：{', '.join(top_themes[:5])}。建议优先checks 这些领域避免重复错误。",
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

    def _touch_recalled(self, results):
        """更新命中记录的访问计数。

        v7.0.0 优化：只遍历命中结果（O(k)），不再全量扫描 O(N)——
        100k 规模下这是每次 recall 的关键热点。sqlite 后端把计数落库，
        并同步检索索引指纹，避免下一次 recall 触发全量重建。
        """
        now = _now_iso()
        touched = {}
        for r in results:
            rec = r[1] if isinstance(r, (tuple, list)) and len(r) > 1 else r
            if isinstance(rec, dict) and rec.get("id"):
                rid = rec["id"]
                rec["access_count"] = (rec.get("access_count") or 0) + 1
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
            # 同步索引指纹：访问计数 UPDATE 会改变 db mtime，避免下次检索全量重建
            try:
                self.retrieval._indexed_fingerprint = self.retrieval._store_fingerprint(self.store)
            except Exception:
                pass

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
        records.append(new_rec)
        self.store.rewrite(records)
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
        """返回 {query, nodes, edges} 格式的图谱查询结果。"""
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
                edges.append({
                    "from": e.get("from"),
                    "relation": e.get("relation", "related_to"),
                    "to": e.get("to"),
                    "strength": e.get("strength"),
                })
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
返回 dict: {status, total_records, active_records, corrupt_records, disk_free_mb, recommendation}"""
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
        return {
            "status": "healthy" if not corrupt else "needs_repair",
            "total_records": len(records),
            "active_records": len(active),
            "corrupt_records": len(corrupt),
            "deleted_records": len(records) - len(active),
            "brain_dir": self.base_dir,
            "disk_free_mb": disk_mb,
            "recommendation": "Run brain.memory_repair()" if corrupt else "No issues found"
        }

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
    similar_pairs = []
    out = []
    for r in records:
        if r.get("_corrupt"):
            out.append(r)
            continue
        fp = _stable_id(r.get("content", ""))
        if fp in seen:
            merged += 1
            if not dry_run:
                continue
        seen[fp] = r
        out.append(r)
    if len(out) <= 300:
        for i in range(len(out)):
            for j in range(i + 1, len(out)):
                if embed_engine:
                    sim = embed_engine.similarity(
                        embed_engine.encode(out[i].get("content", "")),
                        embed_engine.encode(out[j].get("content", "")),
                    )
                else:
                    sim = _cosine(
                        _tf_vector(_tokenize(out[i].get("content", ""))),
                        _tf_vector(_tokenize(out[j].get("content", ""))),
                    )
                if sim > 0.85 and out[i].get("content") != out[j].get("content"):
                    similar_pairs.append((out[i]["id"], out[j]["id"], round(sim, 3)))
    if not dry_run and merged > 0:
        store.rewrite(out)
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
        context=f"联网searches 沉淀：{query}",
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
        lines = ["# Mnemosyne v7.0.1 记忆库exports ", "", f"exports Time：{_now_iso()}    共 {len(records)} 条", ""]
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
    print("  Mnemosyne v7.0.1 — 流水线自测（实测指标，不打分）")
    print("=" * 64)
    brain.ensure_init()

    results = {}

    # ---- 1. writes 机制Test ----
    print("\n[1/6] writes 机制Test...")
    test_items = [
        ("Alice 是 Acme 公司的首席工程师，负责 AI 平台架构设计。", "semantic"),
        ("产品负责人哥偏好结论先line的回答风格，回答必须简短。", "preference"),
        ("2026-08-07 完成了劳动仲裁一审起诉材料的commits 至横琴法院。", "episodic"),
        ("教训：hermes config set 对含点的嵌套 key 会拆错，必须用 Python 直接改 config.yaml。", "procedural"),
        ("Hindsight 是开源 Agent 记忆系统，supports  retain/recall/reflect 三种核心操作。", "semantic"),
        ("based on 过去20 times交互，用户多 times要求减少废话，偏好直接给Result。", "observation"),
        ("我认为未来 AI 记忆系统应当采用 Human-in-the-loop 模式。", "opinion"),
        ("经过analyzes ，用户是Result导向型人格，建议先给结论再展开。", "belief"),
        ("公司政策A在2026年1月废止，政策B于2026年3月生效。", "semantic"),
        ("关Key决策：选择零依赖纯Python实现而非依赖PostgreSQL+pgvector。", "semantic"),
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
    print("\n[2/6] 检索能力Test...")
    queries = [
        ("Alice 在哪里Work？", "semantic"),
        ("产品负责人哥的回答偏好", "preference"),
        ("劳动仲裁 横琴法院", "episodic"),
        ("hermes Config 教训", "procedural"),
        ("AI 记忆系统 架构", "semantic"),
        ("公司 政策 废止 生效", "semantic"),
        ("用户 人格 line为模式", "belief"),
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
    print("\n[3/6] 反思能力Test...")
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
    print("\n[5/6] 自学习循环Test...")
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
        print(f"  图Query延迟：{graph_time:.1f}ms，Result正常：{results['graph_query_ok']}")
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
    print("\U0001f9ea Mnemosyne v7.0.1 性能基准Test")
    print("=" * 56)
    brain.ensure_init()

    t0 = time.time()
    for i in range(count):
        rec = _build_record(
            f"benchmark memory {i}: 项目 {i % 50} 的关Key决策是选择模块化架构，负责人 Alice，Date 2026-08-07。",
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
    print("\U0001f9ea Mnemosyne v7.0.1 演示模式")
    print("=" * 50)
    brain.ensure_init()

    demo_items = [
        ("Alice 是 Acme 公司的首席工程师，负责 AI 平台架构。", "semantic"),
        ("产品负责人哥偏好结论先line的回答风格，回答必须简短。", "preference"),
        ("2026-08-07 完成了劳动仲裁一审起诉材料的commits 。", "episodic"),
        ("Hindsight 是开源 Agent 记忆系统，supports  retain/recall/reflect。", "semantic"),
        ("我认为人 AI 记忆系统应该优先本地化、零依赖。", "belief"),
    ]
    for content, mtype in demo_items:
        # retain() 返回 str（memory_id）；此处需要完整记录，使用 retain_detailed
        rec = brain.retain_detailed(content, mtype=mtype)
        print(f"  \u2713 retain: [{mtype}/{rec.get('fact_type', '?')}] {content[:40]}... "
              f"(confidence={rec.get('confidence', '?')}, importance={rec.get('importance', '?')})")

    print("-" * 50)
    hits = brain.recall("Alice 在哪里Work？", k=3)
    print("\U0001f9e0 recall 'Alice 在哪里Work？':")
    for score, rec, reasons in hits:
        print(f"  -> [{rec.get('fact_type', '?')}] {rec['content'][:50]}  "
              f"(score={score:.3f}, {reasons})")

    print("-" * 50)
    print("  \u2705 演示via ：v7.0.1 引擎可用。")


# ============================================================================
# Part 14: CLI
# ============================================================================

