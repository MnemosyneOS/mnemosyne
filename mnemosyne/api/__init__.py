#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — drop-in compatibility surface
============================================

Import as a substitute for the reference SDK::

    from mnemosyne.api import Memory

    m = Memory()
    m.add("I prefer dark mode and vim keybindings", user_id="alice")
    hits = m.search("what does alice prefer?", filters={"user_id": "alice"})

Everything exported here is either an exact match for the reference API or a
documented Mnemosyne addition.  The additions are grouped separately and named
so they cannot collide:

``Memory`` / ``AsyncMemory`` / ``MemoryClient``
    The three client classes. Same methods, same signatures, same error codes.
``MemoryConfig`` / ``ComponentConfig``
    Configuration, via plain dataclasses rather than a validation library, so
    importing this module still pulls in nothing outside the standard library.
``MemoryItem``
    The projected memory shape — ``id`` / ``memory`` / ``metadata`` / ``score`` /
    ``created_at`` / ``updated_at`` — plus ``categories`` when present.
``*Options``
    Typed option objects for the client.
``*Error``
    The exception hierarchy, with ``code`` attributes carrying the stable error
    identifiers callers branch on.

Additions (no counterpart in the reference):

``describe()``
    Live configuration *including what was degraded*, which is the question that
    actually matters when recall is worse than expected.
``capsule()`` / ``expand()``
    Lossless hierarchical compression with byte-exact recovery.
``verify_integrity()``
    Hash-chained ledger verification.
``temporal_query()``
    Version chains for an entity.

The module also exposes ``PROVIDERS``, a mapping of every registered provider per
category, and ``available_providers()``, which reports what can run on this
machine right now.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .async_memory import AsyncMemory
from .client import DEFAULT_BASE_URL, MemoryClient
from .config import (DEFAULT_HISTORY_DB_PATH, DEFAULT_INSTRUCTIONS,
                     SUPPORTED_VERSION, ComponentConfig, MemoryConfig)
from .errors import (ENTITY_KEYS, ApiConflictError, ApiError,
                     ApiNotFoundError, ApiProviderError,
                     ApiValidationError)
from .events import EventStatus
from .extract import DEFAULT_CATEGORIES, ExtractionResult, FactExtractor
from .filters import compile_filter, normalize_filters, split_scope
from .memory import Memory
from .options import (AddMemoryOptions, DeleteAllOptions, GetAllMemoryOptions,
                      ListEventsOptions, SearchMemoryOptions, UpdateMemoryOptions)
from .projection import derive_namespace, to_memory_item

from ..providers import build_components, describe_providers, list_providers

__version__ = "8.0.0"

#: The projected memory shape, documented as a tuple of its guaranteed fields.
MEMORY_ITEM_FIELDS = ("id", "memory", "metadata", "score", "created_at", "updated_at")

#: Every registered provider, per category.
PROVIDERS: Dict[str, List[str]] = list_providers()


def available_providers() -> Dict[str, Any]:
    """Report which providers can actually run here, and what is missing.

    The zero-dependency claim is only meaningful if it is checkable, so this
    function answers "what works on this machine" by probing for the optional
    packages each adapter needs, rather than printing a static feature list.

    Example::

        >>> from mnemosyne.api import available_providers
        >>> info = available_providers()
        >>> info["llm"]["providers"][0]["available"]
        True
    """
    return describe_providers()


def build(config: Dict[str, Any] | None = None,
          brain: Any = None) -> Dict[str, Any]:
    """Build every component named in *config*, reporting any degradation."""
    return build_components(config, brain=brain)


__all__ = [
    # client classes
    "Memory", "AsyncMemory", "MemoryClient",
    # configuration
    "MemoryConfig", "ComponentConfig", "SUPPORTED_VERSION",
    "DEFAULT_HISTORY_DB_PATH", "DEFAULT_INSTRUCTIONS",
    # option objects
    "AddMemoryOptions", "SearchMemoryOptions", "GetAllMemoryOptions",
    "UpdateMemoryOptions", "DeleteAllOptions", "ListEventsOptions",
    # errors
    "ApiError", "ApiValidationError", "ApiNotFoundError",
    "ApiConflictError", "ApiProviderError",
    # building blocks
    "MemoryItem", "DEFAULT_CATEGORIES", "ExtractionResult", "FactExtractor",
    "EventStatus", "ENTITY_KEYS", "MEMORY_ITEM_FIELDS",
    # filters
    "compile_filter", "normalize_filters", "split_scope",
    # projection
    "derive_namespace", "to_memory_item",
    # providers
    "PROVIDERS", "available_providers", "build", "DEFAULT_BASE_URL",
    "__version__",
]


class MemoryItem(dict):
    """The projected memory shape, as a dict subclass.

    A thin subclass rather than a dataclass, because every consumer of this API
    indexes into the result (``item["memory"]``) and a dataclass would silently
    change that to attribute access — a breaking change disguised as a cleanup.
    Construction via :meth:`from_record` applies the same projection the engine
    uses internally, so a hand-built item and a returned one are identical.
    """

    @classmethod
    def from_record(cls, record: Dict[str, Any], **kwargs: Any) -> "MemoryItem":
        return cls(to_memory_item(record, **kwargs))

    @property
    def id(self) -> str:
        return self.get("id", "")

    @property
    def memory(self) -> str:
        return self.get("memory", "")

    @property
    def metadata(self) -> Dict[str, Any]:
        return self.get("metadata") or {}

    @property
    def score(self) -> float:
        try:
            return float(self.get("score") or 0.0)
        except (TypeError, ValueError):
            return 0.0
