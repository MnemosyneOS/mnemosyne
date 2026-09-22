#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — Typed option objects for the hosted-style client
==============================================================

The reference client accepts either loose keyword arguments or a typed options
object, and the typed form is the documented one for newer code.  These classes
reproduce that: each is a plain dataclass with an ``to_payload()`` that produces
exactly the JSON body the endpoint expects.

Validation happens in ``to_payload()`` rather than in ``__init__``, so an object
can be constructed, inspected and partially filled before it is sent — which is
what makes them usable for building a request in stages.

``camelCase`` aliases are accepted on construction, because the TypeScript SDK
spells these fields ``topK`` / ``userId`` and code is frequently ported between
the two.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Optional

from .errors import ApiValidationError, validate_threshold

__all__ = ["AddMemoryOptions", "SearchMemoryOptions", "GetAllMemoryOptions",
           "UpdateMemoryOptions", "DeleteAllOptions", "ListEventsOptions"]


_CAMEL_ALIASES = {
    "topK": "top_k",
    "userId": "user_id",
    "agentId": "agent_id",
    "runId": "run_id",
    "appId": "app_id",
    "expirationDate": "expiration_date",
    "customCategories": "custom_categories",
    "customInstructions": "custom_instructions",
    "memoryType": "memory_type",
    "showExpired": "show_expired",
    "referenceDate": "reference_date",
    "observationDate": "observation_date",
    "asyncMode": "async_mode",
    "temporalReasoning": "temporal_reasoning",
}


class _Base:
    """Shared behaviour: camelCase aliasing, payload building, round-tripping."""

    def __init__(self, **kwargs: Any):
        allow = {f.name for f in dataclasses.fields(self)}  # type: ignore[arg-type]
        unknown: List[str] = []
        for key, value in kwargs.items():
            name = _CAMEL_ALIASES.get(key, key)
            if name not in allow:
                unknown.append(key)
                continue
            setattr(self, name, value)
        if unknown:
            raise ApiValidationError(
                f"unknown option(s) for {type(self).__name__}: {', '.join(unknown)}",
                detail={"unknown": unknown, "supported": sorted(allow)},
            )

    def to_payload(self) -> Dict[str, Any]:
        """Drop ``None`` fields and return the request body."""
        return {k: v for k, v in dataclasses.asdict(self).items()  # type: ignore[call-overload]
                if v is not None}

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)  # type: ignore[call-overload]

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        parts = ", ".join(f"{k}={v!r}" for k, v in self.to_dict().items()
                          if v is not None)
        return f"{type(self).__name__}({parts})"


@dataclasses.dataclass(init=False)
class AddMemoryOptions(_Base):
    """Options for ``client.add()``.

    ``filters`` is accepted as a convenience alias for the four entity fields:
    the hosted API takes them at the top level of the body, but callers porting
    from the OSS SDK habitually reach for ``filters``, and silently ignoring it
    would drop the scope — the worst possible outcome, since the write would
    succeed into the wrong tenant.
    """

    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    run_id: Optional[str] = None
    app_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    infer: Optional[bool] = None
    memory_type: Optional[str] = None
    prompt: Optional[str] = None
    immutable: Optional[bool] = None
    expiration_date: Optional[str] = None
    custom_categories: Optional[List[Any]] = None
    custom_instructions: Optional[str] = None
    includes: Optional[str] = None
    excludes: Optional[str] = None
    observation_date: Optional[str] = None
    timezone: Optional[str] = None
    temporal_reasoning: Optional[bool] = None
    async_mode: Optional[bool] = None
    filters: Optional[Dict[str, Any]] = None

    def to_payload(self) -> Dict[str, Any]:
        body = super().to_payload()
        scope = body.pop("filters", None)
        if isinstance(scope, dict):
            for key, value in scope.items():
                if key in ("user_id", "agent_id", "run_id", "app_id") and value is not None:
                    body.setdefault(key, value)
        return body


@dataclasses.dataclass(init=False)
class SearchMemoryOptions(_Base):
    """Options for ``client.search()``."""

    filters: Optional[Dict[str, Any]] = None
    top_k: Optional[int] = None
    threshold: Optional[float] = None
    rerank: Optional[bool] = None
    explain: Optional[bool] = None
    reference_date: Optional[str] = None
    show_expired: Optional[bool] = None
    keyword_search: Optional[bool] = None
    version: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        body = super().to_payload()
        if "threshold" in body:
            body["threshold"] = validate_threshold(body["threshold"])
        return body


@dataclasses.dataclass(init=False)
class GetAllMemoryOptions(_Base):
    """Options for ``client.get_all()``."""

    filters: Optional[Dict[str, Any]] = None
    top_k: Optional[int] = None
    show_expired: Optional[bool] = None
    page: Optional[int] = None
    page_size: Optional[int] = None


@dataclasses.dataclass(init=False)
class UpdateMemoryOptions(_Base):
    """Options for ``client.update()``."""

    text: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    expiration_date: Optional[str] = None


@dataclasses.dataclass(init=False)
class DeleteAllOptions(_Base):
    """Options for ``client.delete_all()``."""

    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    run_id: Optional[str] = None
    app_id: Optional[str] = None


@dataclasses.dataclass(init=False)
class ListEventsOptions(_Base):
    """Options for ``client.list_events()``."""

    status: Optional[str] = None
    operation: Optional[str] = None
    limit: Optional[int] = None
    filters: Optional[Dict[str, Any]] = None
