#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — Provider base contracts
======================================

Five component categories are defined, mirroring the component taxonomy used by
the wider AI-memory ecosystem (LLM / Embedder / VectorStore / GraphStore /
Reranker).  Every adapter in this package is **optional**: the engine's core
never imports one at start-up, and a missing adapter degrades to the built-in
pure-stdlib implementation instead of raising.

Design rules (keep these invariant — they are what makes the zero-dependency
claim true):

1. An adapter module must be importable with only the Python standard library
   on ``sys.path``.  Third-party SDKs are imported *inside* the method that
   needs them, never at module import time.
2. Adapters that talk to an HTTP API use :mod:`urllib.request` directly rather
   than a vendor SDK.  That keeps them dependency-free *and* swappable, since
   the wire format is the stable interface — not the SDK version.
3. An adapter that genuinely cannot work without an SDK declares it in
   ``ProviderInfo.requires`` and raises :class:`ProviderUnavailable` when
   constructed without it.  Callers then fall back rather than crash.
"""

from __future__ import annotations

import abc
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ProviderError(RuntimeError):
    """Base class for every provider-layer failure."""


class ProviderUnavailable(ProviderError):
    """The requested provider cannot be built in this environment.

    Raised when the provider's optional dependency is absent, when its
    credentials are missing, or when its upstream endpoint is not configured.
    Carries an actionable ``hint`` so a caller can tell the user what to do
    instead of dumping a bare ImportError.
    """

    def __init__(self, provider: str, reason: str, hint: str = ""):
        self.provider = provider
        self.reason = reason
        self.hint = hint or (
            f"Install the optional extras for '{provider}' "
            f"(pip install mnemosyne-os[providers]) or switch to a "
            f"pure-standard-library provider."
        )
        super().__init__(f"provider '{provider}' unavailable: {reason}")


class UnknownProvider(ProviderError, KeyError):
    """The provider name is not registered in the requested category."""

    def __init__(self, category: str, name: str, available: List[str]):
        self.category = category
        self.name = name
        self.available = available
        super().__init__(
            f"unsupported {category} provider: {name!r}; "
            f"available: {', '.join(sorted(available)) or '(none)'}"
        )


# ---------------------------------------------------------------------------
# Provider metadata
# ---------------------------------------------------------------------------

@dataclass
class ProviderInfo:
    """Static description of one provider adapter.

    Attributes
    ----------
    name:
        The configuration key, exactly as a user writes it in a config block
        (e.g. ``"openai"``, ``"qdrant"``).
    category:
        One of ``llm`` / ``embedder`` / ``vector_store`` / ``graph_store`` /
        ``reranker``.
    factory:
        Callable taking the provider's ``config`` dict and returning an
        instance.
    requires:
        Third-party distributions (or external services) the adapter needs.
        Empty for pure-stdlib adapters — those are always available.
    transports:
        Free-form labels describing how the adapter reaches its upstream,
        e.g. ``("http",)``, ``("sdk",)``, ``("embedded",)``.
    default_model:
        Sensible default model name, when the category has one.
    env_keys:
        Environment variables consulted when the config omits credentials.
    notes:
        One-line human explanation surfaced by ``list_providers()``.
    """

    name: str
    category: str
    factory: Callable[..., Any]
    requires: tuple = ()
    transports: tuple = ()
    default_model: Optional[str] = None
    env_keys: tuple = ()
    notes: str = ""

    # -- introspection ------------------------------------------------------

    @property
    def available(self) -> bool:
        """True when every entry in :attr:`requires` can be imported."""
        if not self.requires:
            return True
        import importlib.util

        for dep in self.requires:
            # A requirement may be given as a distribution name; map the few
            # cases where the import name differs from the distribution name.
            mod = _DIST_TO_MODULE.get(dep, dep).replace("-", "_")
            try:
                if importlib.util.find_spec(mod) is None:
                    return False
            except (ImportError, ValueError, ModuleNotFoundError):
                return False
        return True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "requires": list(self.requires),
            "transports": list(self.transports),
            "default_model": self.default_model,
            "env_keys": list(self.env_keys),
            "notes": self.notes,
            "available": self.available,
        }


# Distribution name -> import name, for the handful that differ.
_DIST_TO_MODULE = {
    "sentence-transformers": "sentence_transformers",
    "huggingface-hub": "huggingface_hub",
    "python-dotenv": "dotenv",
    "opensearch-py": "opensearchpy",
    "azure-search-documents": "azure.search.documents",
    "google-cloud-aiplatform": "google.cloud.aiplatform",
    "boto3": "boto3",
    "fastembed": "fastembed",
    "kuzu": "kuzu",
    "neo4j": "neo4j",
    "pymongo": "pymongo",
    "psycopg2-binary": "psycopg2",
    "psycopg": "psycopg",
    "redis": "redis",
    "valkey": "valkey",
    "pymilvus": "pymilvus",
    "chromadb": "chromadb",
    "weaviate-client": "weaviate",
    "pinecone": "pinecone",
    "faiss-cpu": "faiss",
    "lancedb": "lancedb",
    "elasticsearch": "elasticsearch",
    "databricks-sdk": "databricks.sdk",
}


def _first_env(*names: str) -> Optional[str]:
    """Return the first non-empty environment variable among *names*."""
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


# ---------------------------------------------------------------------------
# Component contracts
# ---------------------------------------------------------------------------

class BaseProvider(abc.ABC):
    """Common plumbing shared by every adapter."""

    category: str = "provider"
    name: str = "base"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config: Dict[str, Any] = dict(config or {})

    # -- config helpers -----------------------------------------------------

    def cfg(self, key: str, default: Any = None) -> Any:
        """Read a config value, falling back to a category-specific env var.

        Lookup order is: explicit config -> ``MNEMOSYNE_<CATEGORY>_<KEY>`` ->
        ``<CATEGORY_UPPER>_<KEY>`` -> caller default.  Two env prefixes are
        tried so both a namespaced and a plain convention work.
        """
        if key in self.config and self.config[key] is not None:
            return self.config[key]
        cat = self.category.upper()
        key_up = key.upper()
        return _first_env(
            f"MNEMOSYNE_{cat}_{key_up}",
            f"{cat}_{key_up}",
        ) if default is None else (
            _first_env(f"MNEMOSYNE_{cat}_{key_up}", f"{cat}_{key_up}") or default
        )

    def require(self, *keys: str) -> None:
        """Raise :class:`ProviderUnavailable` unless every key resolves."""
        missing = [k for k in keys if not self.cfg(k)]
        if missing:
            raise ProviderUnavailable(
                self.name,
                f"missing configuration: {', '.join(missing)}",
                hint=(
                    f"Set {', '.join('MNEMOSYNE_%s_%s' % (self.category.upper(), k.upper()) for k in missing)} "
                    f"or pass them in the '{self.name}' config block."
                ),
            )

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<{type(self).__name__} name={self.name!r} category={self.category!r}>"


class LLMProvider(BaseProvider):
    """Text-completion contract.

    ``complete`` is the only method the memory engine requires.  Providers that
    can also emit schema-constrained JSON implement ``structured``; the default
    implementation asks for JSON in the prompt and repairs the common
    code-fence / prose-preamble wrapping problems.

    ``supports_vision`` / ``describe_image`` are optional.  They are declared here
    rather than discovered by ``hasattr`` so that a caller can decide *before*
    making a call whether an image turn can be described, instead of building a
    request only to have it rejected by the upstream API.
    """

    category = "llm"

    @property
    def supports_vision(self) -> bool:
        """Whether this adapter can accept image input.

        Derived from a module-level registry of vision-capable provider names
        rather than declared per class.  A per-class flag has to be remembered
        when a vendor is added, and forgetting it produces a silent capability
        loss rather than an error; a registry is a single place to look and is
        checked by :func:`mnemosyne.providers.vision.supports_vision`.
        """
        from .vision import supports_vision

        return supports_vision(self.name)

    @abc.abstractmethod
    def complete(self, messages: List[Dict[str, str]], **kwargs: Any) -> str:
        """Return the assistant text for *messages*.

        *messages* uses the OpenAI chat shape: a list of
        ``{"role": ..., "content": ...}`` dicts.
        """

    def describe_image(self, prompt: str, image_url: str, **kwargs: Any) -> str:
        """Return a text description of the image at *image_url*.

        Delegates to :mod:`mnemosyne.providers.vision`, which owns the per-vendor
        wire formats.  Raises :class:`ProviderUnavailable` when the provider has no
        vision support; the multimodal layer catches that and stores the image
        reference without a description instead of failing the write.
        """
        from .vision import describe

        return describe(self, prompt, image_url,
                        max_tokens=int(kwargs.get("max_tokens") or 400))

    def structured(self, messages: List[Dict[str, str]],
                   schema: Optional[Dict[str, Any]] = None,
                   **kwargs: Any) -> Any:
        """Return parsed JSON from the model, tolerating imperfect output."""
        from .json_utils import loads_lenient

        raw = self.complete(messages, **kwargs)
        return loads_lenient(raw)


class EmbedderProvider(BaseProvider):
    """Vector-embedding contract."""

    category = "embedder"

    #: Dimensionality of the vectors this embedder produces.  ``None`` until
    #: the first call, for providers that only learn it from the response.
    dims: Optional[int] = None

    @abc.abstractmethod
    def embed(self, texts: List[str]) -> List[List[float]]:
        """Embed a batch of texts, returning one vector per input."""

    def embed_one(self, text: str) -> List[float]:
        out = self.embed([text])
        return out[0] if out else []


class RerankerProvider(BaseProvider):
    """Relevance-rescoring contract."""

    category = "reranker"

    @abc.abstractmethod
    def rerank(self, query: str, documents: List[str],
               top_n: Optional[int] = None) -> List[Dict[str, Any]]:
        """Rescore *documents* against *query*.

        Returns a list of ``{"index": int, "score": float, ...}`` dicts sorted
        by descending relevance.  ``index`` refers to the position in the input
        *documents* list, so callers can map results back onto their own hits.
        """


class VectorStoreProvider(BaseProvider):
    """Vector index contract.

    The built-in store implements this without any third-party code by
    delegating to Mnemosyne's SQLite backend.  External adapters (Qdrant,
    Chroma, pgvector, ...) implement the same four operations.
    """

    category = "vector_store"

    @abc.abstractmethod
    def upsert(self, vectors: List[List[float]], payloads: List[Dict[str, Any]],
               ids: List[str]) -> None: ...

    @abc.abstractmethod
    def search(self, query_vector: List[float], top_k: int = 20,
               filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Return ``[{"id", "score", "payload"}, ...]`` sorted by similarity."""

    @abc.abstractmethod
    def delete(self, ids: List[str]) -> None: ...

    def get(self, vector_id: str) -> Optional[Dict[str, Any]]:
        """Fetch one point by id, or ``None``."""
        return None

    def list_all(self, filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Return every point matching *filters* (used by ``get_all``)."""
        return []

    def delete_by_filters(self, filters: Dict[str, Any]) -> int:
        """Delete every point matching *filters*; return the count removed."""
        removed = 0
        for point in self.list_all(filters):
            self.delete([point["id"]])
            removed += 1
        return removed

    def reset(self) -> None:
        """Drop every point this adapter owns.  No-op by default."""


class GraphStoreProvider(BaseProvider):
    """Entity-relation graph contract.

    Mnemosyne's native implementation is the SQLite triple store already used by
    the temporal knowledge graph, so the built-in graph store is always
    available.  External adapters are provided for teams that already run a
    dedicated graph database.
    """

    category = "graph_store"

    @abc.abstractmethod
    def add_edges(self, edges: List[Dict[str, Any]],
                  memory_id: Optional[str] = None) -> None:
        """Insert ``{"source", "relation", "target"}`` triples."""

    @abc.abstractmethod
    def neighbors(self, entity: str, max_depth: int = 2) -> Dict[str, Any]:
        """Return ``{"nodes": [...], "edges": [...]}`` around *entity*."""

    def delete_entity(self, entity: str) -> int:
        """Remove every edge touching *entity*; return the count removed."""
        return 0
