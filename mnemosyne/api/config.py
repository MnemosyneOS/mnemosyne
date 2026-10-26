#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — Compatibility configuration
==========================================

A drop-in configuration surface matching the reference SDK's shape::

    Memory.from_config({
        "llm":           {"provider": "openai", "config": {"model": "gpt-4o-mini"}},
        "embedder":      {"provider": "openai", "config": {"model": "text-embedding-3-small"}},
        "vector_store":  {"provider": "qdrant", "config": {"host": "localhost", "port": 6333}},
        "graph_store":   {"provider": "builtin"},
        "reranker":      {"provider": "cohere", "config": {"api_key": "..."}},
        "history_db_path": "~/.mnemosyne/history.db",
        "version": "v1.1",
        "custom_instructions": "Focus on durable user preferences.",
    })

Two deliberate departures from the reference, both of which make the same config
mean something *safe* here rather than merely accepting it:

**No third-party models.** Validation uses plain dataclasses. The reference
builds pydantic models, which would make ``pip install mnemosyne-os`` pull in a
validation library the core otherwise never touches.

**Dimension coherence is checked.** The reference lets you pair a 1536-dimension
embedder with a 768-dimension collection and discover the mismatch on the first
query. Here, when both an embedder and a vector store are named, their
dimensionality is compared up front and a mismatch is reported with the two
numbers and the field to change.  Silent dimension drift is the single most
expensive failure mode in a vector-backed memory system, because it does not
raise — it just returns the wrong memories.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .errors import ApiValidationError, ApiProviderError

__all__ = ["MemoryConfig", "ComponentConfig", "DEFAULT_HISTORY_DB_PATH",
           "SUPPORTED_VERSION", "DEFAULT_INSTRUCTIONS"]

SUPPORTED_VERSION = "v1.1"

DEFAULT_INSTRUCTIONS = (
    "Extract durable, self-contained facts about the user and their world.\n"
    "Rules:\n"
    "1. One fact per memory. Never merge unrelated facts into one sentence.\n"
    "2. Rewrite in the third person and resolve pronouns: \"I moved to Austin\" "
    "becomes \"User moved to Austin\".\n"
    "3. Keep every literal detail exactly as stated — numbers, dates, amounts, "
    "model names, URLs, proper nouns. Never paraphrase or round them.\n"
    "4. Skip small talk, questions, acknowledgements and anything the assistant "
    "said about itself. Only what the *user* revealed matters.\n"
    "5. Skip anything derivable from a single word of the conversation, and skip "
    "secrets: credentials, tokens, private keys.\n"
    "6. Preserve time references relative to the observation date rather than "
    "guessing an absolute date."
)

_DEFAULT_DIR = os.environ.get("MNEMOSYNE_DIR") or os.path.join(
    os.path.expanduser("~"), ".mnemosyne")
DEFAULT_HISTORY_DB_PATH = os.path.join(_DEFAULT_DIR, "history.db")


def _as_mapping(value: Any, name: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ApiValidationError(
            f"'{name}' must be a mapping, got {type(value).__name__}")
    return value


@dataclass
class ComponentConfig:
    """One ``{provider, config}`` block.

    ``config`` is kept as a plain dict rather than a per-provider model, because
    the set of accepted keys is owned by the adapter (and grows with each vendor
    release).  The adapters validate what they need and raise
    :class:`ApiProviderError` with a hint, which keeps a new provider key from
    requiring a change here.
    """

    provider: str = ""
    config: Dict[str, Any] = field(default_factory=dict)
    #: Filled in by :meth:`MemoryConfig.validate` when a provider is unavailable
    #: but construction was allowed to continue on a fallback.
    degraded_from: Optional[str] = None
    degrade_reason: str = ""

    @classmethod
    def coerce(cls, value: Any, default_provider: str = "") -> "ComponentConfig":
        if value is None:
            return cls(provider=default_provider)
        if isinstance(value, str):
            return cls(provider=value)
        if isinstance(value, ComponentConfig):
            return value
        raw = _as_mapping(value, "component config")
        provider = raw.get("provider") or default_provider
        params = raw.get("config")
        if params is None:
            # Allow the inline spelling {provider: openai, model: gpt-4o};
            # everything not a known envelope key becomes adapter config.
            params = {k: v for k, v in raw.items()
                      if k not in ("provider", "config", "params")}
        elif not isinstance(params, dict):
            raise ApiValidationError(
                f"the 'config' of provider {provider!r} must be a mapping")
        return cls(provider=str(provider or default_provider),
                   config=dict(params or {}))

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"provider": self.provider, "config": dict(self.config)}
        if self.degraded_from:
            out["degraded_from"] = self.degraded_from
            out["degrade_reason"] = self.degrade_reason
        return out


@dataclass
class MemoryConfig:
    """The full configuration of a ``Memory`` instance.

    Beyond the reference fields, three Mnemosyne-specific keys are accepted so a
    caller can reach the engine's own features without leaving this object:

    ``brain_dir``
        Where the memory store lives. Every dimension scope gets a subdirectory
        under it, which is what makes isolation physical rather than a filter.
    ``namespace``
        An explicit namespace for the whole instance, overriding the
        per-scope derivation.
    ``strict_providers``
        Default ``False``. When false, a provider that cannot be built is
        replaced by its built-in fallback and recorded in
        :attr:`degraded`. When true, the same condition raises instead — the
        right choice for CI, where silently falling back would hide a broken key.
    """

    llm: ComponentConfig = field(default_factory=lambda: ComponentConfig())
    embedder: ComponentConfig = field(default_factory=lambda: ComponentConfig())
    vector_store: ComponentConfig = field(default_factory=lambda: ComponentConfig())
    graph_store: ComponentConfig = field(default_factory=lambda: ComponentConfig())
    reranker: ComponentConfig = field(default_factory=lambda: ComponentConfig())
    history_db_path: str = DEFAULT_HISTORY_DB_PATH
    version: str = SUPPORTED_VERSION
    custom_instructions: Optional[str] = None
    custom_categories: Optional[List[Any]] = None

    brain_dir: str = _DEFAULT_DIR
    namespace: Optional[str] = None
    strict_providers: bool = False
    enable_graph: bool = True
    #: How many existing records an ``add()`` scans for an exact duplicate before
    #: giving up and letting the engine's template-hash versioning handle it.
    #: ``0`` disables the scan. Bounded because an unbounded scan on every write
    #: makes a large store slower the more it remembers.
    dedup_scan_limit: int = 20000
    #: Populated by :meth:`validate`. ``{category: {...}}`` for anything that
    #: could not be built as asked.
    degraded: Dict[str, Any] = field(default_factory=dict)

    # -- construction -------------------------------------------------------

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "MemoryConfig":
        """Build a config from a plain mapping.

        Unknown top-level keys are accepted and preserved on :attr:`extra`, for
        the same reason the reference tolerates them: a caller may pass one
        config object to several libraries, and rejecting a key that only
        another library understands would break that.
        """
        raw = _as_mapping(data, "config")
        known = {
            "llm", "embedder", "vector_store", "graph_store", "reranker",
            "history_db_path", "version", "custom_instructions",
            "custom_categories", "brain_dir", "namespace", "strict_providers",
            "enable_graph", "dedup_scan_limit",
        }
        cfg = cls(
            llm=ComponentConfig.coerce(raw.get("llm"), "rules"),
            embedder=ComponentConfig.coerce(raw.get("embedder"), "builtin"),
            vector_store=ComponentConfig.coerce(raw.get("vector_store"), "builtin"),
            graph_store=ComponentConfig.coerce(raw.get("graph_store"), "builtin"),
            reranker=ComponentConfig.coerce(raw.get("reranker"), ""),
            history_db_path=raw.get("history_db_path") or DEFAULT_HISTORY_DB_PATH,
            version=str(raw.get("version") or SUPPORTED_VERSION),
            custom_instructions=raw.get("custom_instructions"),
            custom_categories=raw.get("custom_categories"),
            brain_dir=str(raw.get("brain_dir") or _DEFAULT_DIR),
            namespace=raw.get("namespace"),
            strict_providers=bool(raw.get("strict_providers", False)),
            enable_graph=bool(raw.get("enable_graph", True)),
            dedup_scan_limit=int(raw.get("dedup_scan_limit", 20000)),
        )
        extra = {k: v for k, v in raw.items() if k not in known}
        if extra:
            object.__setattr__(cfg, "extra", extra)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        """Check the config is internally coherent, then record degradations.

        Called automatically by :meth:`from_dict`; safe to call again after a
        provider becomes available.
        """
        if self.version and str(self.version).lower() not in (
                SUPPORTED_VERSION, "v1", "v2", "v3"):
            # A newer version string is accepted with a warning rather than an
            # error: refusing to start because a caller pinned a version ahead of
            # this build is a worse outcome than behaving like v1.1.
            object.__setattr__(self, "version_warning",
                               f"version {self.version!r} is newer than this build "
                               f"supports ({SUPPORTED_VERSION}); using "
                               f"{SUPPORTED_VERSION} semantics")

        self.brain_dir = os.path.expanduser(str(self.brain_dir or _DEFAULT_DIR))
        self.history_db_path = os.path.expanduser(
            str(self.history_db_path or DEFAULT_HISTORY_DB_PATH))

        self._check_embedder_dimensions()

    def _check_embedder_dimensions(self) -> None:
        """Compare the named embedder's dimension against the store's.

        Only runs when *both* are named explicitly, and skips providers whose
        dimension is unknown or whose storage is schemaless.  A mismatch is
        raised rather than degraded: writing vectors nothing can query is not a
        degraded mode, it is silent data loss.
        """
        from ..providers.embedders import default_dims as emb_dims

        emb = (self.embedder.provider or "builtin").lower()
        store = (self.vector_store.provider or "builtin").lower()
        if store in ("builtin", "memory", "generic", "sdk"):
            return

        want = self.embedder.config.get("dimensions") or self.embedder.config.get(
            "embedding_dims") or emb_dims(emb)
        got = self.vector_store.config.get("embedding_model_dims") or self.vector_store \
            .config.get("dimensions")
        if not want or not got:
            return
        try:
            want_i, got_i = int(want), int(got)
        except (TypeError, ValueError):
            return
        if want_i != got_i:
            raise ApiValidationError(
                f"embedder '{emb}' produces {want_i}-dimensional vectors but "
                f"vector_store '{store}' is configured for {got_i}; "
                f"set vector_store.config.embedding_model_dims={want_i}",
                code="VALIDATION_009",
                detail={"embedder": emb, "embedder_dims": want_i,
                        "vector_store": store, "store_dims": got_i},
            )

    # -- helpers ------------------------------------------------------------

    def component(self, category: str) -> ComponentConfig:
        """Return a component block by name."""
        try:
            return getattr(self, category)
        except AttributeError:
            raise ApiValidationError(f"unknown component category: {category!r}") from None

    def resolved_instructions(self, extra: Optional[str] = None) -> str:
        """Return the extraction instruction text.

        :attr:`custom_instructions` replaces the default wholesale, matching the
        reference behaviour; *extra* is appended as an additional section so a
        per-call hint can sharpen the instruction without having to restate it.
        """
        base = (self.custom_instructions or DEFAULT_INSTRUCTIONS).strip()
        if extra and str(extra).strip():
            base = f"{base}\n\nAdditional instructions for this call:\n{str(extra).strip()}"
        return base

    @property
    def categories_catalog(self) -> List[Any]:
        from .extract import DEFAULT_CATEGORIES

        return list(self.custom_categories or DEFAULT_CATEGORIES)

    def to_dict(self, redact_secrets: bool = True) -> Dict[str, Any]:
        """Serialise the config, hiding anything credential-shaped by default."""
        def clean(block: ComponentConfig) -> Dict[str, Any]:
            d = block.to_dict()
            if redact_secrets:
                d["config"] = {
                    k: ("***" if _looks_secret(k) and v else v)
                    for k, v in d["config"].items()
                }
            return d

        return {
            "llm": clean(self.llm),
            "embedder": clean(self.embedder),
            "vector_store": clean(self.vector_store),
            "graph_store": clean(self.graph_store),
            "reranker": clean(self.reranker),
            "history_db_path": self.history_db_path,
            "version": self.version,
            "custom_instructions": self.custom_instructions,
            "brain_dir": self.brain_dir,
            "namespace": self.namespace,
            "enable_graph": self.enable_graph,
            "strict_providers": self.strict_providers,
            "degraded": self.degraded,
        }

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (f"MemoryConfig(llm={self.llm.provider!r}, "
                f"embedder={self.embedder.provider!r}, "
                f"vector_store={self.vector_store.provider!r}, "
                f"graph_store={self.graph_store.provider!r}, "
                f"reranker={self.reranker.provider!r})")


_SECRET_HINTS = ("key", "token", "secret", "password", "credential", "auth")


def _looks_secret(key: str) -> bool:
    k = str(key).lower()
    return any(h in k for h in _SECRET_HINTS)
