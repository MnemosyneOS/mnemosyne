#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — ``Memory``: the drop-in memory client
====================================================

The class an application actually holds.  Its surface is the union of two sets:

**The compatible surface** — ``add`` / ``get`` / ``get_all`` / ``search`` /
``update`` / ``delete`` / ``delete_all`` / ``history`` / ``reset`` / ``close`` /
``from_config`` / ``chat``, with the same argument names, defaults, keyword-only
markers, return envelopes and error codes. Code written against the reference SDK
runs unchanged.

**The engine's native surface** — ``capsule`` / ``expand`` / ``verify_integrity``
/ ``temporal_query`` / ``graph_search`` / ``describe``. These are additions, so
they can never conflict with the compatible surface, and they are the reason to
use this implementation rather than the reference one.

Four behaviours are worth calling out because they are choices, not accidents:

**Scoping is physical.** Each distinct ``(user_id, agent_id, run_id, app_id)``
combination gets its own directory and SQLite file, not a filter over shared rows.
One tenant cannot read another's memories even with a malformed filter, and
deleting a tenant is an ``rm -rf`` rather than a scan.

**Degradation is reported, never silent.** A provider that cannot be built is
replaced by its built-in equivalent and recorded in ``describe()["degraded"]``.
Construction does not raise unless ``strict_providers`` is set, because refusing
to start over a lapsed API key is worse than starting with reduced recall — but
the reduction is always discoverable.

**Extraction has an offline path.** With no LLM configured, ``add()`` still works
and still recalls; it just extracts with pattern rules instead of a model. A
memory system that stops remembering when a key expires is not a memory system.

**Writes are idempotent per content hash.** Re-adding the same fact is detected
and reported as a duplicate rather than stored twice, which is what keeps an
agent that re-reads its own context from doubling its store every turn.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..providers import build_components
from ..providers.base import ProviderUnavailable
from .config import MemoryConfig
from .errors import (ENTITY_KEYS, ApiConflictError, ApiNotFoundError,
                     ApiProviderError, ApiValidationError, NOT_FOUND_001,
                     VALIDATION_007, reject_top_level_entity_kwargs,
                     validate_entity_ids, validate_messages, validate_threshold)
from .events import EventLog, EventStatus
from .extract import FactExtractor
from .filters import combine, compile_filter, normalize_filters, split_scope
from .multimodal import (MultimodalBatch, attach_to_metadata, describe_image,
                         image_fact_text, parse_messages)
from .projection import (derive_namespace, is_immutable, item_id, scope_summary,
                         to_memory_item, to_memory_items, to_record_updates)

__all__ = ["Memory", "MemoryBrainContext"]

_UNSET = "__unset__"

#: How many existing records an ``add()`` scans for an exact duplicate. Past this
#: the add proceeds and duplicate suppression is left to the engine's
#: template-hash versioning. Configurable per instance as ``dedup_scan_limit``
#: (0 disables the scan entirely).
_DEDUP_SCAN_LIMIT = 20000

#: Memory types the engine understands, mapped from a caller's free-text
#: ``memory_type``.  Unknown values fall back to ``semantic`` rather than being
#: rejected, so a caller using its own vocabulary still works.
_TYPE_ALIASES = {
    "semantic": "semantic", "episodic": "episodic", "procedural": "procedural",
    "preference": "preference", "identity": "identity", "lesson": "lesson",
    "strategy": "strategy", "reflective": "reflective", "reflection": "reflective",
    "conversation": "episodic", "fact": "semantic", "event": "episodic",
    "note": "semantic", "todo": "todo",
}


class _ProviderEmbeddingAdapter:
    """Adapts an :class:`EmbedderProvider` to the engine's ``embed_engine`` slot.

    The engine calls ``encode`` / ``encode_batch`` / ``similarity`` and, when a
    vector backend is attached, ``add`` / ``remove``.  Forwarding those to a
    provider is what lets a configured remote embedder drive recall without the
    engine having to know which vendor is behind it.

    ``add`` / ``remove`` intentionally return ``True`` without doing anything when
    the provider has no index of its own: the provider produces *vectors*, and
    Mnemosyne's own store is what persists them.  Reporting a spurious failure
    there would trip the engine's circuit breaker and disable semantic recall
    altogether.
    """

    def __init__(self, provider: Any):
        self.provider = provider
        self.dim = getattr(provider, "dims", None) or 128

    def encode(self, text_or_tokens: Any) -> List[float]:
        if isinstance(text_or_tokens, str):
            return self.provider.embed_one(text_or_tokens)
        return self.provider.embed_one(" ".join(str(t) for t in text_or_tokens))

    def encode_batch(self, texts: Iterable[str]) -> List[List[float]]:
        return self.provider.embed(list(texts))

    def similarity(self, a: List[float], b: List[float]) -> float:
        fn = getattr(self.provider, "similarity", None)
        if callable(fn):
            return float(fn(a, b))
        if not a or not b:
            return 0.0
        import math

        dot = na = nb = 0.0
        for x, y in zip(a, b):
            dot += x * y
            na += x * x
            nb += y * y
        return max(0.0, dot / math.sqrt(na * nb)) if na and nb else 0.0

    def add(self, memory_id: str, vector: List[float], **kwargs: Any) -> bool:
        return True

    def remove(self, memory_id: str, **kwargs: Any) -> bool:
        return True


class MemoryBrainContext:
    """Everything bound to one scope: its store, its providers, its event log.

    One instance per distinct entity scope, cached on the parent ``Memory``.
    Holding the provider instances per scope (rather than globally) matters for
    the built-in vector and graph stores, which are thin views onto *that scope's*
    SQLite file — a shared instance would silently read the wrong tenant.
    """

    def __init__(self, memory: "Memory", scope: Dict[str, Any]):
        self.scope = dict(scope or {})
        self.namespace = memory._namespace_for(self.scope)
        self.brain = memory._build_brain(self.namespace)

        cfg = memory.config
        self.embedder = memory._resolve_embedder()
        if self.embedder is not None:
            self.brain.embed_engine = _ProviderEmbeddingAdapter(self.embedder)
            self.brain.enable_embeddings = True

        self.vector_store = memory._resolve_vector_store(self.brain)
        self.graph_store = memory._resolve_graph_store(self.brain)
        self.reranker = memory._shared_reranker
        self.llm = memory._shared_llm

        self.extractor = FactExtractor(
            llm=self.llm,
            instructions=cfg.resolved_instructions(),
            categories=cfg.categories_catalog,
        )
        self.events = EventLog(cfg.brain_dir, self.namespace)
        self._lock = threading.RLock()


class Memory:
    """The memory client with Mnemosyne's engine underneath.

    ``Memory()`` with no arguments is fully functional and completely offline: the
    built-in embedder, the built-in vector store and the rule-based extractor all
    ship in the standard library.  Every provider named in a config is an
    *upgrade*, never a prerequisite.
    """

    # ------------------------------------------------------------------ init

    def __init__(self, config: Optional[MemoryConfig] = None):
        self.config = config if isinstance(config, MemoryConfig) else \
            MemoryConfig.from_dict(config.to_dict() if isinstance(config, dict) else None)

        self._contexts: Dict[str, MemoryBrainContext] = {}
        self._contexts_lock = threading.RLock()

        # Shared components are built once and reused across scopes; only the
        # built-in store views are per-scope.
        built = build_components(
            {
                "llm": self.config.llm.to_dict(),
                "embedder": self.config.embedder.to_dict(),
                "vector_store": self.config.vector_store.to_dict(),
                "graph_store": self.config.graph_store.to_dict(),
                "reranker": self.config.reranker.to_dict(),
            },
            brain=None,
        )
        self._components = built["components"]
        self.degraded: Dict[str, Any] = dict(built["degraded"])
        self.config.degraded = dict(self.degraded)

        if self.degraded and self.config.strict_providers:
            first = next(iter(self.degraded.values()))
            raise ApiProviderError(
                f"strict_providers is set and {len(self.degraded)} component(s) "
                f"could not be built; first failure: {first['reason']}",
                provider=first.get("requested", ""), hint=first.get("hint", ""),
                detail={"degraded": self.degraded},
            )

        self._shared_llm = self._components.get("llm")
        self._shared_reranker = self._components.get("reranker")
        self._shared_embedder = self._components.get("embedder")
        self._vector_spec = self.config.vector_store.to_dict()
        self._graph_spec = self.config.graph_store.to_dict()

        os.makedirs(self.config.brain_dir, exist_ok=True)

    @classmethod
    def from_config(cls, config_dict: Optional[Dict[str, Any]] = None) -> "Memory":
        """Build a ``Memory`` from a plain mapping — the documented entry point."""
        return cls(MemoryConfig.from_dict(config_dict))

    # ------------------------------------------------------- internal wiring

    def _namespace_for(self, scope: Optional[Dict[str, Any]]) -> str:
        if self.config.namespace:
            return str(self.config.namespace)
        return derive_namespace(scope)

    def _build_brain(self, namespace: str):
        from ..brain import MemoryBrain

        brain = MemoryBrain(
            self.config.brain_dir,
            namespace=namespace,
            enable_embeddings=True,
            enable_graph=bool(self.config.enable_graph),
        )
        brain.ensure_init()
        return brain

    def _resolve_embedder(self) -> Any:
        return self._shared_embedder

    def _resolve_vector_store(self, brain: Any) -> Any:
        spec = dict(self._vector_spec)
        provider = (spec.get("provider") or "builtin").lower()
        if provider in ("", "builtin", "none", "off", "memory"):
            from ..providers.vector_stores import (BuiltinVectorStore,
                                                   InMemoryVectorStore)

            if provider == "memory":
                return InMemoryVectorStore(spec.get("config") or {})
            return BuiltinVectorStore({"brain": brain})
        inst = self._components.get("vector_store")
        if inst is not None:
            return inst
        return BuiltinVectorStore({"brain": brain})

    def _resolve_graph_store(self, brain: Any) -> Any:
        spec = dict(self._graph_spec)
        provider = (spec.get("provider") or "builtin").lower()
        if provider in ("", "builtin", "none", "off"):
            from ..providers.graph_stores import BuiltinGraphStore

            return BuiltinGraphStore({"brain": brain})
        inst = self._components.get("graph_store")
        if inst is not None:
            return inst
        from ..providers.graph_stores import BuiltinGraphStore

        return BuiltinGraphStore({"brain": brain})

    def _ctx(self, scope: Optional[Dict[str, Any]]) -> MemoryBrainContext:
        """Return (building if needed) the context for *scope*."""
        key = self._namespace_for(scope)
        with self._contexts_lock:
            ctx = self._contexts.get(key)
            if ctx is None:
                ctx = MemoryBrainContext(self, scope or {})
                self._contexts[key] = ctx
            return ctx

    def _scoped(self, scope: Dict[str, Any],
                filters: Optional[Dict[str, Any]] = None
                ) -> Tuple[MemoryBrainContext, Dict[str, Any]]:
        """Split *filters* into (context, metadata-only filter).

        Every read path needs the same two things: which scope to open, and what
        is left to filter on once the scope keys have been consumed.  Doing it in
        one place stops a method from forgetting to strip the scope keys and then
        filtering on them a second time — which would return nothing, because the
        scope lives in the directory name, not in each record.
        """
        scope_from_filter, rest = split_scope(filters)
        merged = {**scope, **scope_from_filter}
        return self._ctx(merged), rest

    # ------------------------------------------------------------ properties

    @property
    def project(self) -> Optional[str]:
        """The default project tag, or ``None``.

        Present because the reference exposes it; here it maps onto the engine's
        project field, which partitions memories *within* a scope.
        """
        return self.config.__dict__.get("project")

    @property
    def entity_store(self) -> Dict[str, Any]:
        """Every entity scope this instance has opened, with memory counts."""
        out: Dict[str, Any] = {}
        for key, ctx in list(self._contexts.items()):
            try:
                count = ctx.brain.store.count()
            except Exception:  # pragma: no cover - store always present
                count = None
            out[key] = {"scope": ctx.scope, "namespace": ctx.namespace,
                        "count": count}
        return out

    # -------------------------------------------------------------- write API

    def add(self, messages: Any, *,
            user_id: Optional[str] = None,
            agent_id: Optional[str] = None,
            run_id: Optional[str] = None,
            app_id: Optional[str] = None,
            metadata: Optional[Dict[str, Any]] = None,
            timestamp: Optional[Any] = None,
            expiration_date: Optional[Any] = None,
            infer: bool = True,
            memory_type: Optional[str] = None,
            prompt: Optional[str] = None,
            immutable: bool = False,
            custom_categories: Optional[List[Any]] = None,
            custom_instructions: Optional[str] = None,
            includes: Optional[str] = None,
            excludes: Optional[str] = None,
            observation_date: Optional[str] = None,
            timezone: Optional[str] = None,
            temporal_reasoning: bool = False,
            async_mode: bool = False) -> Dict[str, Any]:
        """Extract and store memories from *messages*.

        Returns ``{"results": [{"id", "memory", "event", "categories", ...}]}``.
        With ``async_mode`` the work is handed to a worker thread and the return
        value is ``{"event_id", "status": "PENDING"}`` instead — poll it with
        :meth:`get_event_status`.

        ``timestamp`` backdates ``created_at``, which is what makes importing a
        transcript produce a correctly ordered history rather than a pile of
        memories all stamped "now".
        """
        # Attachments are parsed *before* validation, because validation flattens
        # multi-part content into a placeholder string — the structured reference a
        # caller may need back would be gone by the time anything could act on it.
        raw_messages = list(messages) if isinstance(messages, (list, tuple)) \
            else [messages]
        batch = parse_messages(raw_messages)
        turns = validate_messages(batch.text_messages or raw_messages)
        scope = validate_entity_ids({"user_id": user_id, "agent_id": agent_id,
                                     "run_id": run_id, "app_id": app_id})
        if not scope:
            # An unscoped write is legal but almost always a bug: it lands in the
            # shared default namespace where every other unscoped caller can see
            # it. Allowed, because the reference allows it and because a
            # single-user deployment genuinely wants one bucket.
            scope = {}

        meta = dict(metadata or {})
        for key, val in scope.items():
            meta.setdefault(key, val)
        if custom_categories:
            meta["categories_catalog"] = [
                (c.get("name") if isinstance(c, dict) else str(c))
                for c in custom_categories
            ]

        ctx = self._ctx(scope)

        if async_mode:
            evt = ctx.events.record("add", scope=scope,
                                    detail={"messages": len(turns),
                                            "attachments": len(batch.images)
                                            + len(batch.audio),
                                            "infer": bool(infer)})
            self._run_async(evt["event_id"], ctx, turns,
                            scope=scope, meta=meta, infer=infer,
                            memory_type=memory_type, prompt=prompt,
                            timestamp=timestamp, expiration_date=expiration_date,
                            immutable=immutable, includes=includes,
                            excludes=excludes, observation_date=observation_date,
                            timezone=timezone,
                            temporal_reasoning=temporal_reasoning,
                            custom_instructions=custom_instructions,
                            batch=batch)
            return {"event_id": evt["event_id"], "status": evt["status"]}

        return {"results": self._add_sync(
            ctx, turns, scope=scope, meta=meta, infer=infer,
            memory_type=memory_type, prompt=prompt, timestamp=timestamp,
            expiration_date=expiration_date, immutable=immutable,
            includes=includes, excludes=excludes,
            observation_date=observation_date, timezone=timezone,
            temporal_reasoning=temporal_reasoning,
            custom_instructions=custom_instructions,
            batch=batch,
        )}

    def _run_async(self, event_id: str, ctx: MemoryBrainContext,
                   turns: List[Dict[str, str]], **kwargs: Any) -> None:
        """Execute an accepted write on a worker thread."""
        def worker() -> None:
            try:
                ctx.events.update(event_id, status=EventStatus.RUNNING)
                results = self._add_sync(ctx, turns, **kwargs)
                ctx.events.update(event_id, status=EventStatus.SUCCEEDED,
                                  detail={"added": len(results)})
            except Exception as e:  # noqa: BLE001 - reported through the event
                ctx.events.update(event_id, error=f"{type(e).__name__}: {e}")

        thread = threading.Thread(target=worker, name=f"mnemosyne-add-{event_id}",
                                  daemon=True)
        thread.start()

    def _add_sync(self, ctx: MemoryBrainContext,
                  turns: List[Dict[str, str]], *,
                  scope: Dict[str, Any], meta: Dict[str, Any], infer: bool,
                  memory_type: Optional[str], prompt: Optional[str],
                  timestamp: Optional[Any], expiration_date: Optional[Any],
                  immutable: bool, includes: Optional[str], excludes: Optional[str],
                  observation_date: Optional[str], timezone: Optional[str],
                  temporal_reasoning: bool,
                  custom_instructions: Optional[str],
                  batch: Optional[MultimodalBatch] = None
                  ) -> List[Dict[str, Any]]:
        """The synchronous write path shared by ``add()`` and the async worker.

        Attachments are written as their own memories once the text facts are
        stored.  One memory per attachment, rather than an attachment list copied
        onto every text fact, because a copied list makes the same image appear to
        belong to unrelated statements and makes "which memory owns this image"
        unanswerable.
        """
        extractor = ctx.extractor
        if custom_instructions:
            extractor = FactExtractor(
                llm=ctx.llm,
                instructions=self.config.resolved_instructions(custom_instructions),
                categories=self.config.categories_catalog,
            )

        if infer:
            facts = extractor.extract(
                turns, memory_type=memory_type, prompt=prompt,
                observation_date=observation_date or _today(),
            )
            if includes:
                facts = [f for f in facts if _mentions(f.fact, includes)]
            if excludes:
                facts = [f for f in facts if not _mentions(f.fact, excludes)]
        else:
            facts = extractor.extract_verbatim(turns)

        results: List[Dict[str, Any]] = []
        for fact in facts:
            record_meta = dict(meta)
            record_meta["categories"] = [fact.category]
            if fact.speaker == "agent":
                record_meta["speaker"] = "agent"
            if immutable:
                record_meta["immutable"] = True
            if expiration_date:
                record_meta["expiration_date"] = str(expiration_date)

            record_meta.setdefault("source", "compat.add")
            if observation_date:
                record_meta["observation_date"] = str(observation_date)
            if timezone:
                record_meta["timezone"] = str(timezone)
            if temporal_reasoning:
                record_meta["temporal_reasoning"] = True

            mtype = _map_type(memory_type, fact.category)
            kwargs: Dict[str, Any] = {
                "tags": _tags_for(fact),
                "confidence": fact.confidence,
                "meta": record_meta,
                "source_type": "agent_generated" if fact.speaker == "agent" else "user",
            }
            if timestamp is not None:
                kwargs["_backdate"] = timestamp

            try:
                mid = self._write_one(ctx, fact.fact, mtype, kwargs,
                                      expiration_date=expiration_date,
                                      timestamp=timestamp, immutable=immutable)
            except Exception as e:  # noqa: BLE001 - one bad fact must not lose the batch
                results.append({"id": None, "memory": fact.fact, "event": "ERROR",
                                "error": f"{type(e).__name__}: {e}"})
                continue

            if mid is None:
                # Recognised as an exact duplicate of an existing memory.
                results.append({"id": None, "memory": fact.fact,
                                "event": "DUPLICATE"})
                continue

            self._link_entities(ctx, mid, fact.entities)
            results.append({
                "id": mid,
                "memory": fact.fact,
                "event": "ADD",
                "categories": [fact.category],
                "confidence": fact.confidence,
            })

        if batch is not None and batch.has_attachments:
            results.extend(self._write_attachments(
                ctx, batch, scope=scope, meta=meta, timestamp=timestamp,
                expiration_date=expiration_date, immutable=immutable,
            ))
        return results

    def _write_attachments(self, ctx: MemoryBrainContext, batch: MultimodalBatch,
                           *, scope: Dict[str, Any], meta: Dict[str, Any],
                           timestamp: Optional[Any],
                           expiration_date: Optional[Any],
                           immutable: bool) -> List[Dict[str, Any]]:
        """Store one memory per attachment.

        With a vision-capable model configured the memory text is the model's
        description, so the image becomes recallable by meaning; without one the
        text is the reference, so nothing is lost and a later pass can enrich it.
        Description failures are swallowed by :func:`describe_image` — an
        unreachable URL or a refusing model degrades the memory rather than
        failing the write.
        """
        describe = bool(getattr(self.config, "describe_images", True))
        vision_prompt = getattr(self.config, "vision_prompt", None)
        out: List[Dict[str, Any]] = []

        for ref in list(batch.images) + list(batch.audio):
            description = describe_image(ctx.llm, ref, vision_prompt) if describe else None
            text = image_fact_text(ref, description)

            record_meta = dict(meta)
            attach_to_metadata(record_meta, [ref])
            record_meta["categories"] = ["attachments"]
            if description is None:
                # Recorded so a caller can find the memories that still need a
                # vision pass, instead of guessing which ones lacked one.
                record_meta["description_missing"] = True

            kwargs: Dict[str, Any] = {
                "tags": ["compat", "attachment", ref.kind],
                "confidence": 0.9 if description else 0.5,
                "meta": record_meta,
                "source_type": "user",
            }
            try:
                mid = self._write_one(ctx, text, "episodic", kwargs,
                                      expiration_date=expiration_date,
                                      timestamp=timestamp, immutable=immutable)
            except Exception as e:  # noqa: BLE001 - one bad attachment is not fatal
                out.append({"id": None, "memory": text, "event": "ERROR",
                            "error": f"{type(e).__name__}: {e}",
                            "modality": ref.kind})
                continue
            if mid is None:
                out.append({"id": None, "memory": text, "event": "DUPLICATE",
                            "modality": ref.kind})
                continue
            out.append({
                "id": mid,
                "memory": text,
                "event": "ADD",
                "modality": ref.kind,
                "attachment": ref.to_metadata(),
                "described": description is not None,
                "categories": ["attachments"],
            })
        return out

    def _write_one(self, ctx: MemoryBrainContext, content: str, mtype: str,
                   kwargs: Dict[str, Any], *, expiration_date: Optional[Any],
                   timestamp: Optional[Any], immutable: bool) -> Optional[str]:
        """Persist one extracted fact; ``None`` when it is a duplicate.

        The dedup key is the engine's content signature, which normalises
        whitespace, case and template verbs.  Without it an agent that re-reads
        its own context each turn would store the same fact on every turn — the
        single most common way a memory store becomes useless.
        """
        backdate = kwargs.pop("_backdate", None)
        record_meta = kwargs.get("meta") or {}

        signature = _signature(content)
        limit = int(getattr(self.config, "dedup_scan_limit", _DEDUP_SCAN_LIMIT) or 0)
        if limit > 0:
            # Exact-duplicate suppression. Scanned rather than indexed because the
            # engine's primary key is a salted hash of the content, so it is not
            # addressable by content. The scan is bounded: past the limit, re-add
            # detection falls back to the engine's template-hash versioning, which
            # is what the store does natively. The alternative — an unbounded scan
            # on every write — degrades a 100k-record store to roughly a second per
            # add, and a memory system that gets slower the more you remember is
            # worse than one that occasionally stores a duplicate.
            for seen, existing in enumerate(ctx.brain.store.iter_records()):
                if seen >= limit:
                    break
                if existing.get("status") == "deleted":
                    continue
                if _signature(existing.get("content") or "") == signature:
                    return None

        mid = ctx.brain.retain(content, mtype=mtype, fast=False, **kwargs)

        updates: Dict[str, Any] = {}
        if timestamp is not None:
            iso = _to_iso(timestamp)
            if iso:
                updates["created_at"] = iso
                updates["knowledge_time"] = iso
        if expiration_date:
            # `expires_at` is the real column; `expiration_date` is the compatible
            # spelling and travels in meta, so only the column is written here.
            updates["expires_at"] = str(expiration_date)
        # Immutability is carried in meta (set by the caller) — writing it as a
        # top-level key would be dropped, because the store has no such column.
        if updates:
            ctx.brain.store.update_by_id(mid, updates)
        return mid

    def _link_entities(self, ctx: MemoryBrainContext, memory_id: str,
                       entities: List[str]) -> None:
        """Write entity edges for a memory, tolerating a disabled graph.

        Entity linking is the third retrieval signal, so it is attempted even on
        the fast path — but a failure here must never fail the write, because the
        memory itself is already durable by this point.
        """
        if not entities or not self.config.enable_graph:
            return
        edges = [{"source": entities[0], "relation": "mentions", "target": e}
                 for e in entities[1:]] if len(entities) > 1 else []
        if not edges:
            return
        try:
            ctx.brain.store.add_edges(edges, memory_id=memory_id)
            ctx.brain.store.update_by_id(memory_id, {"graph_edges": edges})
        except Exception:  # pragma: no cover - graph is best-effort
            pass

    # --------------------------------------------------------------- read API

    def get(self, memory_id: str) -> Dict[str, Any]:
        """Fetch one memory by id.

        Searches already-open contexts first and then every namespace directory on
        disk.  The disk pass is what makes this work from a fresh process for a
        memory written by another one — an in-memory-only lookup would make every
        restart look like total data loss, which is the single most alarming way a
        memory system can fail.
        """
        _, rec = self._locate(memory_id)
        if rec.get("status") == "deleted":
            raise ApiNotFoundError(
                f"memory {memory_id!r} was deleted",
                code=NOT_FOUND_001, detail={"memory_id": memory_id,
                                            "status": "deleted"})
        return to_memory_item(rec)

    def get_all(self, *, filters: Optional[Dict[str, Any]] = None,
                top_k: int = 20, show_expired: bool = False,
                **kwargs: Any) -> Dict[str, Any]:
        """List memories matching *filters*, newest first.

        Entity ids go inside ``filters``; passing them as keyword arguments raises
        ``VALIDATION_007``, matching the reference contract.
        """
        reject_top_level_entity_kwargs(kwargs, "get_all")
        ctx, rest = self._scoped({}, filters)
        predicate = compile_filter(combine(rest, _freshness_filter(show_expired)))

        rows: List[Dict[str, Any]] = []
        for rec in ctx.brain.store.iter_records():
            if rec.get("status") == "deleted":
                continue
            if predicate(rec):
                rows.append(rec)
        rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        if top_k:
            rows = rows[: int(top_k)]
        return {"results": [to_memory_item(r) for r in rows]}

    def search(self, query: str, *,
               top_k: int = 20,
               filters: Optional[Dict[str, Any]] = None,
               threshold: float = 0.1,
               rerank: bool = False,
               explain: bool = False,
               reference_date: Optional[Any] = None,
               show_expired: bool = False,
               **kwargs: Any) -> Dict[str, Any]:
        """Retrieve memories relevant to *query*.

        ``threshold`` filters low-relevance hits and is validated to ``[0, 1]``;
        pass ``0.0`` to disable filtering.  ``rerank=True`` runs the configured
        reranker as a second stage.  ``explain=True`` adds a ``reason`` field
        naming the signals that matched, which is what makes a surprising result
        debuggable instead of mysterious.
        """
        reject_top_level_entity_kwargs(kwargs, "search")
        thr = validate_threshold(threshold)
        ctx, rest = self._scoped({}, filters)
        predicate = compile_filter(combine(rest, _freshness_filter(show_expired)))

        effective_query = query
        if reference_date:
            effective_query = f"{query} (reference date: {reference_date})"

        raw = ctx.brain.recall(effective_query, k=max(int(top_k) * 3, 20))
        if isinstance(raw, tuple):
            raw = raw[0]

        hits: List[Tuple[float, Dict[str, Any], List[str]]] = []
        for entry in raw or []:
            score, rec, reasons = _unpack_hit(entry)
            if rec is None or rec.get("status") == "deleted":
                continue
            if not predicate(rec):
                continue
            hits.append((score, rec, reasons))

        hits = _normalise_scores(hits)
        if thr > 0.0:
            hits = [h for h in hits if h[0] >= thr]

        if not hits:
            # Lexical fallback. The fused retriever returns *nothing* on a query
            # with zero token overlap — the built-in embedder is a Johnson-
            # Lindenstrauss projection of TF-IDF, which is deterministic and free
            # but has no cross-word generalisation. That is exactly the capability
            # a configured embedding provider buys, so the miss is working as
            # designed; what is not acceptable is that it is indistinguishable
            # from "no such memory exists".
            #
            # The fallback is narrow on purpose: it runs only when the primary
            # pass is empty, requires at least one shared token, and tags its
            # results so `explain=True` reveals that they are token matches rather
            # than semantic ones.
            hits = self._lexical_fallback(ctx, query, predicate, int(top_k))

        if rerank and ctx.reranker is not None and hits:
            hits = self._apply_rerank(ctx.reranker, query, hits)

        hits = hits[: int(top_k)]
        results: List[Dict[str, Any]] = []
        for score, rec, reasons in hits:
            item = to_memory_item(rec, score=score)
            if explain:
                item["reason"] = ("+" .join(reasons) if reasons
                                  else "similarity")
                item["verification"] = rec.get("verification", "unverified")
            results.append(item)
        return {"results": results}

    def _apply_rerank(self, reranker: Any, query: str,
                      hits: List[Tuple[float, Dict[str, Any], List[str]]]
                      ) -> List[Tuple[float, Dict[str, Any], List[str]]]:
        """Reorder *hits* with the configured reranker.

        The reranked score replaces the retrieval score rather than blending with
        it: two scores on different scales averaged together produce a number that
        means nothing and makes the result set inexplicable.  Candidates the
        reranker omits keep their original score, so reranking can only reorder,
        never truncate.
        """
        try:
            ranked = reranker.rerank(query, [r.get("content") or "" for _, r, _ in hits])
        except Exception:  # noqa: BLE001 - a reranker outage must not break search
            return hits
        scores = {int(r["index"]): float(r.get("score") or 0.0)
                  for r in ranked if isinstance(r, dict) and "index" in r}
        if not scores:
            return hits
        out = []
        for i, (score, rec, reasons) in enumerate(hits):
            out.append((scores.get(i, float(score)), rec, reasons + ["rerank"]))
        out.sort(key=lambda h: h[0], reverse=True)
        return out

    def _lexical_fallback(self, ctx: MemoryBrainContext, query: str,
                          predicate: Any, top_k: int
                          ) -> List[Tuple[float, Dict[str, Any], List[str]]]:
        """Token-overlap search used only when the fused retriever found nothing.

        Deliberately narrow. It runs only on an empty primary result, so it can
        never dilute a good result set; it requires at least one shared token, so
        it cannot return a wholly unrelated memory; and it tags every hit so
        ``explain=True`` discloses that the match was lexical. The score is the
        fraction of query tokens found, which is a coverage measure a caller can
        reason about rather than an opaque similarity.
        """
        q_tokens = _tokens_for(query)
        if not q_tokens:
            return []
        scored: List[Tuple[float, Dict[str, Any], List[str]]] = []
        for rec in ctx.brain.store.iter_records():
            if rec.get("status") == "deleted":
                continue
            if not predicate(rec):
                continue
            surface = " ".join([str(rec.get("content") or ""),
                                " ".join(str(e) for e in (rec.get("entities") or []))])
            shared = q_tokens & _tokens_for(surface)
            if not shared:
                continue
            scored.append((len(shared) / float(len(q_tokens)), rec,
                           ["lexical_fallback"]))
        scored.sort(key=lambda h: h[0], reverse=True)
        return scored[:top_k] if top_k else scored

    def update(self, memory_id: str, text: Optional[str] = None,
               metadata: Optional[Dict[str, Any]] = None,
               expiration_date: Any = _UNSET,
               data: Optional[str] = None) -> Dict[str, Any]:
        """Patch one memory.

        Only the supplied fields change.  An immutable memory may still have its
        metadata corrected but not its text — refusing the metadata edit too would
        make a typo in the original un-fixable, which is the opposite of what
        immutability is for (protecting the *claim*, not the bookkeeping).
        """
        if text is None and data is not None:
            text = data
        ctx, rec = self._locate(memory_id)
        updates = to_record_updates(text=text, metadata=metadata,
                                    expiration_date=expiration_date)
        if not updates:
            raise ApiValidationError(
                "update() requires at least one of text, metadata or expiration_date")

        if updates.get("content") and is_immutable(rec):
            raise ApiConflictError(
                f"memory {memory_id!r} is immutable; its text cannot be "
                f"changed (metadata may still be updated)")

        if updates.get("content"):
            updates["content_hash"] = _signature(updates["content"])
            if ctx.embedder is not None or ctx.brain.embed_engine:
                try:
                    engine = ctx.brain.embed_engine
                    if engine is not None:
                        updates["embedding"] = engine.encode(updates["content"]) \
                            if hasattr(engine, "encode") else None
                except Exception:  # pragma: no cover
                    pass

        patch = updates.pop("meta_patch", None)
        updates["updated_at"] = _now()
        ctx.brain.store.update_by_id(memory_id, updates)
        if patch:
            fresh = ctx.brain.store.find_by_id(memory_id) or {}
            merged = dict(fresh.get("meta") or {})
            merged.update(patch)
            ctx.brain.store.update_by_id(memory_id, {"meta": merged})
        return {"message": "Memory updated successfully!"}

    def delete(self, memory_id: str) -> Dict[str, Any]:
        """Soft-delete one memory.

        Soft, not physical: the ledger entry and the confidence trajectory stay,
        so "this was known and then retracted" remains answerable.  ``forget``
        with ``evict`` is the physical path, deliberately not the default here.
        """
        ctx, _ = self._locate(memory_id)
        try:
            ok = ctx.brain.forget(memory_id)
        except Exception:
            ok = False
        if not ok:
            ctx.brain.store.update_by_id(memory_id, {"status": "deleted",
                                                     "confidence": 0.0})
        return {"message": "Memory deleted successfully!"}

    def delete_all(self, user_id: Optional[str] = None,
                   agent_id: Optional[str] = None,
                   run_id: Optional[str] = None,
                   app_id: Optional[str] = None,
                   **kwargs: Any) -> Dict[str, Any]:
        """Delete every memory in a scope.

        Entity ids are top-level here (unlike ``search``), matching the reference.
        At least one is required: a call with no scope would delete the shared
        default namespace, and a method named ``delete_all`` should never be the
        thing that empties a store by accident.
        """
        scope = validate_entity_ids({"user_id": user_id, "agent_id": agent_id,
                                     "run_id": run_id, "app_id": app_id},
                                    allow_all_empty=False)
        ctx = self._ctx(scope)
        removed = 0
        for rec in list(ctx.brain.store.iter_records()):
            try:
                if ctx.brain.forget(item_id(rec)):
                    removed += 1
                    continue
            except Exception:  # pragma: no cover
                pass
            ctx.brain.store.update_by_id(item_id(rec),
                                         {"status": "deleted", "confidence": 0.0})
            removed += 1
        # A scope with no memories is still a scope; drop its context so a later
        # write re-reads from disk instead of trusting a stale in-memory view.
        with self._contexts_lock:
            self._contexts.pop(ctx.namespace, None)
        return {"message": "All relevant memories deleted", "deleted": removed}

    def history(self, memory_id: str) -> Dict[str, Any]:
        """Return a memory's change history, oldest first.

        Built from the engine's hash-chained ledger, which is append-only — so
        unlike a table that is rewritten on each edit, this history cannot be
        altered by a later write, and a missing entry means the change genuinely
        did not happen.
        """
        ctx, rec = self._locate(memory_id)
        entries: List[Dict[str, Any]] = []
        try:
            trail = ctx.brain.ledger_audit(memory_id) or []
        except Exception:  # pragma: no cover
            trail = []
        for e in trail:
            entries.append({
                "id": f"{memory_id}#{e.get('seq')}",
                "memory_id": memory_id,
                "event": _ledger_event(e.get("action")),
                "created_at": e.get("timestamp"),
                "updated_at": e.get("timestamp"),
                "old_memory": None,
                "new_memory": (e.get("data_summary") or {}).get("content")
                              if isinstance(e.get("data_summary"), dict) else None,
                "is_deleted": e.get("action") in ("forget", "delete"),
            })
        if not entries:
            entries.append({
                "id": memory_id, "memory_id": memory_id, "event": "ADD",
                "created_at": rec.get("created_at"),
                "updated_at": rec.get("updated_at") or rec.get("created_at"),
                "old_memory": None, "new_memory": rec.get("content"),
                "is_deleted": rec.get("status") == "deleted",
            })
        return {"results": entries}

    def reset(self) -> None:
        """Delete every memory this instance can see.

        Scoped to the contexts already opened, so ``reset()`` on a
        per-user instance does not reach into another user's store — the reason
        it is not implemented as a filesystem walk.
        """
        with self._contexts_lock:
            contexts = list(self._contexts.values())
            self._contexts.clear()
        for ctx in contexts:
            for rec in list(ctx.brain.store.iter_records()):
                try:
                    ctx.brain.forget(item_id(rec), evict=True)
                except Exception:  # pragma: no cover
                    try:
                        ctx.brain.store.delete(item_id(rec))
                    except Exception:
                        pass
            try:
                ctx.events.clear()
            except Exception:  # pragma: no cover
                pass
            try:
                ctx.brain.close()
            except Exception:  # pragma: no cover
                pass

    def close(self) -> None:
        """Release every store handle held by this instance."""
        with self._contexts_lock:
            contexts = list(self._contexts.values())
            self._contexts.clear()
        for ctx in contexts:
            try:
                ctx.brain.close()
            except Exception:  # pragma: no cover
                pass

    def chat(self, query: str) -> None:
        """Not implemented — kept for signature compatibility.

        The reference raises here too.  A memory layer that also answers the
        question stops being a memory layer: it acquires its own model, its own
        prompt and its own opinions about the user's data, all of which belong to
        the application.
        """
        raise NotImplementedError("Chat function not implemented yet.")

    # ------------------------------------------------------- graph memory API

    def graph_add(self, data: str, *, filters: Optional[Dict[str, Any]] = None,
                  user_id: Optional[str] = None,
                  agent_id: Optional[str] = None,
                  run_id: Optional[str] = None,
                  metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Extract entities and relations from *data* into the graph.

        The reference moved graph memory out of its self-hosted SDK and into its
        hosted product. Here it is native and always on, so this method is a thin
        door onto behaviour the write path already performs — which is why it can
        accept plain text rather than requiring a graph-shaped input.
        """
        scope = validate_entity_ids(dict(filters or {}, user_id=user_id,
                                         agent_id=agent_id, run_id=run_id))
        ctx = self._ctx(scope)
        entities = _entities_from(ctx, data)
        edges = _relations_from(data, entities)
        if edges:
            ctx.brain.store.add_edges(edges)
            try:
                ctx.graph_store.add_edges(edges)
            except Exception:  # pragma: no cover
                pass
        return {"results": [{"source": e["source"], "relationship": e["relation"],
                             "target": e["target"]} for e in edges],
                "entities": entities}

    def graph_search(self, query: str, *,
                     filters: Optional[Dict[str, Any]] = None,
                     top_k: int = 20,
                     user_id: Optional[str] = None,
                     agent_id: Optional[str] = None,
                     run_id: Optional[str] = None) -> Dict[str, Any]:
        """Find memories whose entity graph is near *query*'s entities."""
        scope = validate_entity_ids(dict(filters or {}, user_id=user_id,
                                         agent_id=agent_id, run_id=run_id))
        ctx = self._ctx(scope)
        entities = _entities_from(ctx, query) or [query.strip()]
        seen: Dict[str, Dict[str, Any]] = {}
        for entity in entities[:8]:
            try:
                result = ctx.graph_store.neighbors(entity, max_depth=2)
            except Exception:  # pragma: no cover
                continue
            for edge in (result or {}).get("edges") or []:
                for node in (edge.get("from"), edge.get("to")):
                    if not node:
                        continue
                    for rec in ctx.brain.store.get_by_entity(str(node), k=top_k) or []:
                        rid = item_id(rec)
                        if rid and rid not in seen:
                            seen[rid] = rec
        rows = list(seen.values())[: int(top_k)]
        return {"results": [to_memory_item(r) for r in rows],
                "entities": sorted(seen.keys())}

    def graph_get_all(self, *, filters: Optional[Dict[str, Any]] = None,
                      user_id: Optional[str] = None,
                      agent_id: Optional[str] = None,
                      run_id: Optional[str] = None) -> Dict[str, Any]:
        """Return the whole relation set for a scope."""
        scope = validate_entity_ids(dict(filters or {}, user_id=user_id,
                                         agent_id=agent_id, run_id=run_id))
        ctx = self._ctx(scope)
        try:
            edges = ctx.brain.store.all_edges()
        except Exception:  # pragma: no cover
            edges = []
        return {"results": [
            {"source": e.get("source") or e.get("from"),
             "relationship": e.get("relation"),
             "target": e.get("target") or e.get("to"),
             "memory_id": e.get("memory_id")} for e in edges]}

    def graph_delete_all(self, *, filters: Optional[Dict[str, Any]] = None,
                         user_id: Optional[str] = None,
                         agent_id: Optional[str] = None,
                         run_id: Optional[str] = None) -> Dict[str, Any]:
        """Delete every memory in a scope together with its graph edges."""
        return self.delete_all(user_id=user_id, agent_id=agent_id, run_id=run_id)

    # -------------------------------------------------------------- entities

    def list_entities(self, *, user_id: Optional[str] = None,
                      agent_id: Optional[str] = None,
                      run_id: Optional[str] = None,
                      app_id: Optional[str] = None,
                      limit: int = 100) -> Dict[str, Any]:
        """Enumerate the entity scopes present under this store."""
        root = os.path.join(self.config.brain_dir, "data", "namespaces")
        found: List[Dict[str, Any]] = []
        if os.path.isdir(root):
            for name in sorted(os.listdir(root))[: int(limit)]:
                full = os.path.join(root, name)
                if not os.path.isdir(full):
                    continue
                found.append({"namespace": name,
                              "db": os.path.join(full, "memory.db"),
                              "count": _count_db(os.path.join(full, "memory.db"))})
        for key, ctx in list(self._contexts.items()):
            if not any(f["namespace"] == key for f in found):
                found.append({"namespace": key, "scope": ctx.scope,
                              "count": _safe_count(ctx)})
        return {"results": found, "count": len(found)}

    def delete_entities(self, *, user_id: Optional[str] = None,
                        agent_id: Optional[str] = None,
                        run_id: Optional[str] = None,
                        app_id: Optional[str] = None) -> Dict[str, Any]:
        """Delete a scope and cascade to its memories."""
        scope = validate_entity_ids({"user_id": user_id, "agent_id": agent_id,
                                     "run_id": run_id, "app_id": app_id},
                                    allow_all_empty=False)
        out = self.delete_all(**scope)
        return {"message": "Entities deleted successfully",
                "deleted": out.get("deleted", 0), "scope": scope}

    # ---------------------------------------------------------------- events

    def list_events(self, *, status: Optional[str] = None, limit: int = 50,
                    operation: Optional[str] = None,
                    filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """List accepted operations, newest first.

        Unions every context this instance has opened; because event logs are
        per-scope files, an operation run for a scope never opened in this process
        is invisible here — which is the honest answer, and why the REST layer
        reports a scope explicitly.
        """
        rows: List[Dict[str, Any]] = []
        for ctx in self._iter_contexts():
            rows.extend(ctx.events.list(status=status, limit=limit,
                                        operation=operation))
        rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        return {"results": rows[: int(limit)], "count": len(rows)}

    def get_event_status(self, event_id: str) -> Dict[str, Any]:
        """Check the status of an accepted operation."""
        for ctx in self._iter_contexts():
            evt = ctx.events.get(event_id)
            if evt:
                return ({"event_id": evt["event_id"], "status": evt["status"],
                         "operation": evt.get("operation"),
                         "detail": evt.get("detail") or {},
                         "error": evt.get("error"),
                         "created_at": evt.get("created_at"),
                         "updated_at": evt.get("updated_at")})
        raise ApiNotFoundError(f"event {event_id!r} not found",
                                detail={"event_id": event_id})

    # ----------------------------------------------------- native extensions

    def capsule(self, memory_id: str, budget_tokens: Optional[int] = None) -> Dict[str, Any]:
        """Compress one memory into a lossless, expandable pointer capsule.

        An addition with no counterpart in the reference: the memory is reduced to
        a stable pointer plus structured facts plus content atoms (numbers, dates,
        amounts, model names, URLs), and any atom survives every compression level,
        so a tight context budget still answers "how much" and "when".
        """
        ctx, _ = self._locate(memory_id)
        return ctx.brain.capsule(memory_id, budget_tokens=budget_tokens)

    def expand(self, ref: str, verify: bool = True) -> Dict[str, Any]:
        """Recover a capsule's original text byte-for-byte, checking its hash.

        The engine historically named this field ``content`` while ``capsule()``
        names the same text ``text``.  Both keys are returned here so a caller can
        round-trip ``cap["text"]`` → ``expand()`` → ``out["text"]`` without having
        to know which of the two the underlying layer chose.  The original key is
        kept as well, so nothing that already reads ``content`` breaks.
        """
        for ctx in self._iter_contexts():
            try:
                out = ctx.brain.expand(ref, verify=verify)
            except Exception:  # pragma: no cover - wrong context, keep looking
                continue
            if out:
                return _normalise_expand(out)
        raise ApiNotFoundError(f"capsule ref {ref!r} not found",
                                detail={"ref": ref})

    def temporal_query(self, entity: Optional[str] = None,
                       limit: int = 20) -> Dict[str, Any]:
        """Return the version chain of an entity, oldest first."""
        for ctx in self._iter_contexts():
            try:
                rows = ctx.brain.temporal_query(entity=entity, limit=limit)
                if rows:
                    return {"results": rows}
            except Exception:  # pragma: no cover
                continue
        return {"results": []}

    def verify_integrity(self) -> Dict[str, Any]:
        """Verify the hash-chained ledger of every open context."""
        out: Dict[str, Any] = {}
        for ctx in self._iter_contexts():
            try:
                out[ctx.namespace] = ctx.brain.verify_integrity()
            except Exception as e:  # pragma: no cover
                out[ctx.namespace] = {"valid": False, "error": str(e)}
        return out

    def describe(self) -> Dict[str, Any]:
        """Report the live configuration, including anything degraded.

        The single most useful call when retrieval quality is unexpectedly poor:
        it answers "am I actually running the embedder I think I am".
        """
        return {
            "version": _version(),
            "config": self.config.to_dict(),
            "degraded": self.degraded,
            "contexts": [
                {"namespace": c.namespace, "scope": c.scope,
                 "embedder": type(c.embedder).__name__ if c.embedder else "builtin",
                 "vector_store": type(c.vector_store).__name__,
                 "graph_store": type(c.graph_store).__name__,
                 "reranker": type(c.reranker).__name__ if c.reranker else None,
                 "llm": type(c.llm).__name__ if c.llm else "RuleBasedLLM"}
                for c in self._iter_contexts()
            ],
        }

    # ------------------------------------------------------------- internals

    def _iter_contexts(self) -> List[MemoryBrainContext]:
        with self._contexts_lock:
            return list(self._contexts.values())

    def _locate(self, memory_id: str) -> Tuple[MemoryBrainContext, Dict[str, Any]]:
        """Find which context owns *memory_id*.

        Searches already-open contexts first, then every namespace directory on
        disk.  The disk pass is what makes ``get()`` work on a fresh process for a
        memory written by another one — a lookup that only consulted in-memory
        state would make every restart look like data loss.
        """
        for ctx in self._iter_contexts():
            rec = ctx.brain.store.find_by_id(memory_id)
            if rec:
                return ctx, rec

        root = os.path.join(self.config.brain_dir, "data", "namespaces")
        if os.path.isdir(root):
            for name in sorted(os.listdir(root)):
                db = os.path.join(root, name, "memory.db")
                if not os.path.isfile(db):
                    continue
                try:
                    ctx = self._open_namespace(name)
                except Exception:  # pragma: no cover
                    continue
                rec = ctx.brain.store.find_by_id(memory_id)
                if rec:
                    return ctx, rec
        raise ApiNotFoundError(f"memory {memory_id!r} not found",
                                code=NOT_FOUND_001, detail={"memory_id": memory_id})

    def _open_namespace(self, namespace: str) -> MemoryBrainContext:
        with self._contexts_lock:
            ctx = self._contexts.get(namespace)
            if ctx is None:
                ctx = MemoryBrainContext(self, {})
                ctx.namespace = namespace
                ctx.brain = self._build_brain(namespace)
                ctx.embedder = self._resolve_embedder()
                if ctx.embedder is not None:
                    ctx.brain.embed_engine = _ProviderEmbeddingAdapter(ctx.embedder)
                ctx.vector_store = self._resolve_vector_store(ctx.brain)
                ctx.graph_store = self._resolve_graph_store(ctx.brain)
                ctx.reranker = self._shared_reranker
                ctx.llm = self._shared_llm
                ctx.extractor = FactExtractor(
                    llm=ctx.llm,
                    instructions=self.config.resolved_instructions(),
                    categories=self.config.categories_catalog,
                )
                ctx.events = EventLog(self.config.brain_dir, namespace)
                self._contexts[namespace] = ctx
            return ctx


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------

def _freshness_filter(show_expired: bool) -> Dict[str, Any]:
    """Exclude deleted rows, and expired ones unless explicitly requested."""
    base: Dict[str, Any] = {"status": {"ne": "deleted"}}
    if show_expired:
        return base
    return {"and": [base, {"or": [
        {"expires_at": {"eq": None}},
        {"expires_at": {"gte": _today()}},
    ]}]}


def _unpack_hit(entry: Any) -> Tuple[float, Optional[Dict[str, Any]], List[str]]:
    if isinstance(entry, dict):
        return float(entry.get("score") or 0.0), entry, []
    if isinstance(entry, (list, tuple)):
        if len(entry) >= 3 and isinstance(entry[0], (int, float)):
            return float(entry[0]), entry[1], list(entry[2] or [])
        if len(entry) == 2:
            a, b = entry
            if isinstance(a, (int, float)):
                return float(a), b, []
            if isinstance(b, (int, float)):
                return float(b), a, []
    return 0.0, None, []


def _normalise_scores(hits: List[Tuple[float, Dict[str, Any], List[str]]]
                      ) -> List[Tuple[float, Dict[str, Any], List[str]]]:
    """Rescale scores so the best hit is 1.0 and keep the ordering.

    The engine's fused score is not bounded, while the compatible contract
    documents ``score`` as a similarity in ``[0, 1]`` and callers compare it to
    ``threshold``.  Without this rescale a threshold of 0.1 would either admit
    everything or nothing depending on corpus size — the number would be
    meaningless, which is worse than being absent.
    """
    if not hits:
        return []
    hits = sorted(hits, key=lambda h: h[0], reverse=True)
    top = hits[0][0]
    if top <= 0:
        return [(0.0, rec, reasons) for _, rec, reasons in hits]
    out = []
    for score, rec, reasons in hits:
        out.append((max(0.0, min(1.0, float(score) / float(top))), rec, reasons))
    return out


def _normalise_expand(out: Dict[str, Any]) -> Dict[str, Any]:
    """Give an ``expand()`` result both ``text`` and ``content`` keys.

    The two spellings come from two different layers of the engine, and a caller
    that guessed wrong would see an empty string rather than an error — the kind
    of failure that looks like data loss.  Populating both removes the guess.
    """
    if not isinstance(out, dict):
        return {"text": str(out), "content": str(out)}
    text = out.get("text")
    if text is None:
        text = out.get("content")
    if text is None:
        text = ""
    return {**out, "text": text, "content": out.get("content", text)}


def _map_type(memory_type: Optional[str], category: str) -> str:
    if memory_type:
        return _TYPE_ALIASES.get(str(memory_type).lower().strip(), "semantic")
    return {
        "preferences": "preference", "personal_details": "identity",
        "professional_details": "identity", "goals": "strategy",
        "relationships": "identity", "dates_and_events": "episodic",
        "technical_interests": "semantic", "health": "identity",
        "locations": "semantic", "miscellaneous": "semantic",
    }.get(category, "semantic")


def _tags_for(fact: Any) -> List[str]:
    tags = ["compat"]
    if fact.category:
        tags.append(str(fact.category))
    if fact.speaker == "agent":
        tags.append("agent_generated")
    return tags


#: Tokens too common to carry retrieval signal.  Kept short and English+Chinese
#: because the fallback must not import a language-specific stopword table —
#: it runs on the zero-dependency path by definition.
_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "has", "have", "had", "of", "in", "on", "at", "to",
    "for", "with", "and", "or", "but", "if", "then", "than", "so", "as",
    "what", "which", "who", "whom", "whose", "when", "where", "why", "how",
    "i", "me", "my", "we", "our", "you", "your", "it", "its", "he", "she",
    "they", "them", "his", "her", "their", "that", "this", "these", "those",
    "about", "from", "by", "not", "no", "yes", "can", "could", "should",
    "would", "will", "shall", "may", "might", "must",
    "的", "了", "是", "在", "我", "你", "他", "她", "它", "们", "和", "与",
    "有", "就", "都", "而", "及", "或", "把", "被", "给", "让", "对", "为",
})


def _tokens_for(text: str) -> set:
    """Tokenise *text* into a signal-bearing token set.

    Uses the engine's own tokeniser (which handles CJK bigrams as well as Latin
    words) and then drops stopwords and one-character Latin tokens.  Returning a
    *set* rather than a list is what makes the coverage ratio in
    :meth:`Memory._lexical_fallback` meaningful: repeated words must not inflate
    the score.
    """
    if not text:
        return set()
    try:
        from ..utils import _tokenize

        raw = _tokenize(str(text))
    except Exception:  # pragma: no cover - tokeniser always present in-tree
        import re

        raw = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", str(text))
    out = set()
    for token in raw or []:
        t = str(token).strip().lower()
        if not t or t in _STOPWORDS:
            continue
        if len(t) < 2 and t.isascii():
            continue
        out.add(t)
    return out


def _signature(text: str) -> str:
    try:
        from ..utils import _content_signature

        return _content_signature(text or "")
    except Exception:  # pragma: no cover
        import hashlib
        import re

        norm = re.sub(r"\s+", " ", (text or "").strip().lower())
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:32]


def _to_iso(value: Any) -> Optional[str]:
    """Convert an epoch number, datetime or ISO string into the engine's format."""
    import datetime

    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.replace(microsecond=0).isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat() + "T00:00:00"
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:  # milliseconds
            ts /= 1000.0
        return datetime.datetime.fromtimestamp(ts).replace(microsecond=0).isoformat()
    text = str(value).strip().replace("Z", "")
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(text[:26], fmt).replace(
                microsecond=0).isoformat()
        except ValueError:
            continue
    return text


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _mentions(text: str, needle: str) -> bool:
    return str(needle or "").lower() in str(text or "").lower()


def _entities_from(ctx: MemoryBrainContext, text: str) -> List[str]:
    from .extract import extract_entities

    return extract_entities(text or "")


def _relations_from(text: str, entities: List[str]) -> List[Dict[str, Any]]:
    """Derive candidate triples from *text* using the engine's extractor."""
    try:
        from ..utils import _extract_entities, _extract_relationships

        detailed = _extract_entities(text or "")
        rels = _extract_relationships(detailed, text or "")
        if rels:
            return [{"source": r.get("source") or r.get("from"),
                     "relation": r.get("relation") or r.get("predicate"),
                     "target": r.get("target") or r.get("to")}
                    for r in rels if r.get("source") or r.get("from")]
    except Exception:  # pragma: no cover
        pass
    span = re_span(text)
    return span


def re_span(text: str) -> List[Dict[str, Any]]:
    """Minimal fallback: link the first entity to each other entity."""
    import re

    names = re.findall(r"\b[A-Z][A-Za-z0-9+#.\-]{1,30}\b", text or "")
    blocked = {"I", "The", "A", "An", "And", "But", "If", "It", "We", "You"}
    names = [n for n in names if n not in blocked][:8]
    if len(names) < 2:
        return []
    return [{"source": names[0], "relation": "related_to", "target": n}
            for n in names[1:]]


def _ledger_event(action: Optional[str]) -> str:
    return {
        "retain": "ADD", "add": "ADD", "update": "UPDATE", "overwrite": "UPDATE",
        "correct": "UPDATE", "forget": "DELETE", "delete": "DELETE",
        "consolidate": "UPDATE",
    }.get(str(action or "").lower(), "UPDATE")


def _count_db(path: str) -> Optional[int]:
    import sqlite3

    if not os.path.isfile(path):
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
            return int(row[0]) if row else None
        finally:
            conn.close()
    except Exception:  # pragma: no cover - schema or lock differences
        return None


def _safe_count(ctx: MemoryBrainContext) -> Optional[int]:
    try:
        return ctx.brain.store.count()
    except Exception:  # pragma: no cover
        return None


def _version() -> str:
    try:
        from ... import __version__

        return __version__
    except Exception:  # pragma: no cover
        return "unknown"
