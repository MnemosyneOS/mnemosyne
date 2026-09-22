#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — Provider registry
================================

One import surface for all five component categories, and one place that knows
whether a given adapter can actually run *on this machine right now*.

The registry exists to make a specific promise checkable: **every provider is
optional**.  Nothing here imports a third-party module at import time, so
``import mnemosyne`` remains dependency-free no matter which adapters are
registered, and :func:`describe_providers` can be called on a bare install to
report exactly what is missing and what to install — a question that otherwise
only surfaces as an ImportError deep inside a request.

Typical use::

    from mnemosyne.providers import build_components

    comps = build_components({
        "llm":           {"provider": "ollama", "config": {"model": "llama3.2"}},
        "embedder":      {"provider": "ollama", "config": {"model": "nomic-embed-text"}},
        "vector_store":  {"provider": "qdrant", "config": {"url": "http://localhost:6333"}},
        "reranker":      {"provider": "cohere", "config": {"api_key": "..."}},
        "graph_store":   {"provider": "builtin"},
    }, brain=brain)

``build_components`` never raises for a *missing* component: an unavailable or
misconfigured adapter is reported in the returned ``degraded`` map and the
built-in equivalent is substituted, because a memory system that refuses to write
because a reranker key expired is worse than one that writes and says so.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import (BaseProvider, EmbedderProvider, GraphStoreProvider,
                   LLMProvider, ProviderError, ProviderInfo, ProviderUnavailable,
                   RerankerProvider, UnknownProvider, VectorStoreProvider)
from .embedders import (BuiltinEmbedder, get_embedder, list_embedder_providers)
from .graph_stores import BuiltinGraphStore, get_graph_store, list_graph_store_providers
from .llms import RuleBasedLLM, get_llm, list_llm_providers
from .rerankers import get_reranker, list_reranker_providers
from .vector_stores import (BuiltinVectorStore, describe_vector_stores,
                            get_vector_store, list_vector_store_providers)
from .vision import VISION_PROVIDERS, supports_vision

__all__ = [
    # contracts
    "BaseProvider", "LLMProvider", "EmbedderProvider", "VectorStoreProvider",
    "GraphStoreProvider", "RerankerProvider", "ProviderInfo",
    # errors
    "ProviderError", "ProviderUnavailable", "UnknownProvider",
    # builders
    "get_llm", "get_embedder", "get_reranker", "get_vector_store", "get_graph_store",
    "build_components", "list_providers", "describe_providers",
    # built-ins
    "BuiltinEmbedder", "BuiltinVectorStore", "BuiltinGraphStore", "RuleBasedLLM",
]

#: category -> (builder, lister).  Adding a category means adding one row here.
_CATEGORIES: Dict[str, Dict[str, Any]] = {
    "llm": {"build": get_llm, "list": list_llm_providers,
            "required": False, "fallback": "rules",
            "doc": "Fact extraction, update decisions and rerank scoring."},
    "embedder": {"build": get_embedder, "list": list_embedder_providers,
                 "required": False, "fallback": "builtin",
                 "doc": "Vector representation for semantic recall."},
    "vector_store": {"build": get_vector_store, "list": list_vector_store_providers,
                     "required": False, "fallback": "builtin",
                     "doc": "Where embeddings and their payloads live."},
    "graph_store": {"build": get_graph_store, "list": list_graph_store_providers,
                    "required": False, "fallback": "builtin",
                    "doc": "Entity-relation triples for multi-hop recall."},
    "reranker": {"build": get_reranker, "list": list_reranker_providers,
                 "required": False, "fallback": None,
                 "doc": "Optional second-stage relevance rescoring."},
}

CATEGORIES = tuple(_CATEGORIES)


def list_providers(category: Optional[str] = None) -> Dict[str, List[str]]:
    """Return ``{category: [provider names]}``, or one category's list."""
    if category:
        spec = _CATEGORIES.get(category)
        if spec is None:
            raise UnknownProvider("category", category, list(_CATEGORIES))
        return {category: spec["list"]()}
    return {cat: spec["list"]() for cat, spec in _CATEGORIES.items()}


def describe_providers() -> Dict[str, Any]:
    """Report every provider, its transport, and whether it is usable now.

    For each category this also names the fallback that will be substituted when
    the configured provider cannot be built — the fact a user actually needs when
    deciding whether a missing key is fatal.
    """
    out: Dict[str, Any] = {}
    for cat, spec in _CATEGORIES.items():
        entry: Dict[str, Any] = {
            "doc": spec["doc"],
            "fallback": spec["fallback"],
            "required": spec["required"],
            "providers": [],
        }
        if cat == "vector_store":
            # The vector store set is large and heterogeneous; its own
            # describer knows about embedded / http / sdk transports.
            entry["providers"] = describe_vector_stores()
        else:
            for name in spec["list"]():
                info = _provider_info(cat, name)
                entry["providers"].append(info)
        out[cat] = entry
    return out


def _provider_info(category: str, name: str) -> Dict[str, Any]:
    """Best-effort metadata for one named provider."""
    requires: tuple = ()
    transport = "http"
    default_model: Optional[str] = None

    if category == "llm":
        from . import llms

        cls = llms._REGISTRY.get(name)
        requires = getattr(cls, "requires", ())
        if cls is RuleBasedLLM:
            transport = "embedded"
        elif name in ("langchain", "aws_bedrock"):
            transport = "sdk"
        default_model = getattr(cls, "default_model", None)
    elif category == "embedder":
        from . import embedders

        cls = embedders._REGISTRY.get(name)
        requires = getattr(cls, "requires", ())
        if cls is BuiltinEmbedder or name == "hashing":
            transport = "embedded"
        elif name in ("fastembed", "langchain", "aws_bedrock"):
            transport = "sdk"
        default_model = getattr(cls, "default_model", None)
    elif category == "graph_store":
        from . import graph_stores

        cls = graph_stores._REGISTRY.get(name)
        requires = getattr(cls, "requires", ())
        if cls is BuiltinGraphStore:
            transport = "embedded"
        elif name == "kuzu":
            transport = "embedded"
    elif category == "reranker":
        from . import rerankers

        cls = rerankers._REGISTRY.get(name)
        requires = getattr(cls, "requires", ())
        if name == "sentence_transformer":
            transport = "sdk"
        elif name == "llm":
            transport = "local"

    import importlib.util

    available = True
    for dep in requires:
        mod = dep.replace("-", "_")
        try:
            if importlib.util.find_spec(mod) is None:
                available = False
                break
        except (ImportError, ValueError, ModuleNotFoundError):
            available = False
            break

    return {
        "name": name,
        "transport": transport,
        "requires": list(requires),
        "available": available,
        "default_model": default_model,
    }


def build_components(config: Optional[Dict[str, Any]] = None,
                     brain: Any = None) -> Dict[str, Any]:
    """Build every requested component, substituting a fallback on failure.

    Parameters
    ----------
    config:
        ``{category: {"provider": ..., "config": {...}}}``.  Omitted categories
        resolve to their built-in default.
    brain:
        The live ``MemoryBrain``, needed by the built-in vector and graph stores.

    Returns
    -------
    dict with keys:

    ``components``
        ``{category: instance}`` — always fully populated.
    ``degraded``
        ``{category: {"requested", "used", "reason", "hint"}}`` for every
        component whose configured provider could not be built.  Empty means
        everything the user asked for is running.
    """
    config = config or {}
    components: Dict[str, Any] = {}
    degraded: Dict[str, Any] = {}

    for cat, spec in _CATEGORIES.items():
        requested = config.get(cat) or {}
        want = str(requested.get("provider") or spec["fallback"] or "none").strip().lower()

        # A category with no fallback and no request is simply absent.
        if want in ("", "none", "off", "disabled"):
            components[cat] = None
            continue

        try:
            if cat == "vector_store":
                components[cat] = spec["build"](requested, brain=brain)
            elif cat == "graph_store":
                components[cat] = spec["build"](requested, brain=brain)
            else:
                components[cat] = spec["build"](requested)
            continue
        except Exception as e:  # noqa: BLE001 - any failure degrades, never raises
            # Deliberately broad. Catching only the provider layer's own error
            # types was not enough: a store whose constructor performs I/O raises a
            # transport error, so a transient outage at start-up would take the
            # whole engine down instead of falling back to the built-in store.
            # The invariant this protects is that `build_components` always
            # returns a usable component and reports anything it could not honour.
            reason = f"{type(e).__name__}: {e}"
            hint = getattr(e, "hint", "")

        fallback = spec["fallback"]
        if fallback is None:
            components[cat] = None
            degraded[cat] = {"requested": want, "used": None, "reason": reason,
                             "hint": hint}
            continue
        try:
            if cat == "vector_store":
                components[cat] = spec["build"]({"provider": fallback}, brain=brain)
            elif cat == "graph_store":
                components[cat] = spec["build"]({"provider": fallback}, brain=brain)
            else:
                components[cat] = spec["build"]({"provider": fallback})
        except Exception as e2:  # pragma: no cover - built-ins should not fail
            components[cat] = None
            reason = f"{reason}; fallback also failed: {e2}"
            fallback = None
        degraded[cat] = {"requested": want, "used": fallback, "reason": reason,
                         "hint": hint}

    return {"components": components, "degraded": degraded}


def provider_availability() -> Dict[str, bool]:
    """Quick ``{category: bool}`` map: is the configured-able default ready?

    Intended for health endpoints and the console's status page.
    """
    out: Dict[str, bool] = {}
    for cat, spec in _CATEGORIES.items():
        names = spec["list"]()
        out[cat] = bool(names)
    return out
