#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — ``MemoryClient``
===============================

The hosted-style client: same API as :class:`~mnemosyne.api.memory.Memory`,
addressed over HTTP instead of in-process.

It has **two transports and one surface**:

``embedded`` (default when no ``base_url`` is given)
    Calls an in-process ``Memory``. Identical semantics, no server to run. This
    is the mode that makes the class usable in a notebook or a CLI on a machine
    with nothing deployed — and it means code can start embedded and move to a
    server by changing one argument, rather than by rewriting call sites.

``http`` (when ``base_url`` is set)
    Speaks the REST surface documented in ``docs/API_REFERENCE.md``. Writes are
    asynchronous by contract, so ``add()`` returns an ``event_id`` you poll — the
    hosted model, and the only one that behaves sanely when the client is a
    short-lived CI job.

Because both transports return the same shapes, a caller cannot accidentally
depend on which one it is using, except through ``transport``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from ..providers.transport import HttpError, redact
from .config import MemoryConfig
from .errors import ApiError, ApiNotFoundError, ApiValidationError
from .memory import Memory
from .options import (AddMemoryOptions, DeleteAllOptions, GetAllMemoryOptions,
                      ListEventsOptions, SearchMemoryOptions, UpdateMemoryOptions)

__all__ = ["MemoryClient", "DEFAULT_BASE_URL"]

#: Conventional local address of a self-hosted Mnemosyne REST server.
DEFAULT_BASE_URL = "http://127.0.0.1:8788"

_TIMEOUT = 60.0


class MemoryClient:
    """A client for a Mnemosyne memory service.

    Parameters
    ----------
    api_key:
        Bearer token. Optional in embedded mode, where no authentication is in
        play — the alternative would be requiring a fake key to use a local
        object, which teaches the wrong habit.
    base_url:
        Server root. Omit for embedded mode.
    config:
        A :class:`MemoryConfig`, or a mapping for one. Only used embedded.
    headers:
        Extra request headers for HTTP mode.
    """

    def __init__(self, api_key: Optional[str] = None,
                 base_url: Optional[str] = None,
                 config: Optional[Any] = None,
                 headers: Optional[Dict[str, str]] = None,
                 timeout: float = _TIMEOUT,
                 **kwargs: Any):
        self.api_key = api_key or os.environ.get("MNEMOSYNE_API_KEY")
        self.base_url = (base_url or kwargs.pop("host", None)
                         or (DEFAULT_BASE_URL if kwargs.pop("http", False) else None))
        self.headers = dict(headers or {})
        self.timeout = float(timeout)
        self._extra = kwargs

        if self.base_url:
            self.transport = "http"
            self.base_url = str(self.base_url).rstrip("/")
            self._memory: Optional[Memory] = None
        else:
            self.transport = "embedded"
            cfg = config
            if isinstance(cfg, MemoryConfig):
                pass
            elif isinstance(cfg, dict):
                cfg = MemoryConfig.from_dict(cfg)
            else:
                cfg = MemoryConfig.from_dict(None)
            self._memory = Memory(cfg)

    # -- transport ----------------------------------------------------------

    @property
    def memory(self) -> Memory:
        """The embedded instance, for the native extensions.

        Raises for an HTTP client: the engine's additions (capsule, expand, ledger
        verification) are local-privileged operations and are deliberately not
        exposed through an API key.
        """
        if self._memory is None:
            raise ApiError(
                "this client uses the HTTP transport; native extensions such as "
                "capsule()/expand() are only available with an embedded client "
                "(construct MemoryClient without base_url)")
        return self._memory

    def _request(self, method: str, path: str,
                 payload: Optional[Dict[str, Any]] = None,
                 params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            clean = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list))
                         else v) for k, v in params.items() if v is not None}
            if clean:
                url = f"{url}?{urllib.parse.urlencode(clean)}"

        headers = {"Accept": "application/json", **self.headers}
        if self.api_key:
            # Two spellings are accepted because the reference platform uses
            # `Token` while most gateways expect `Bearer`; sending the same value
            # in both slots is harmless everywhere either is understood.
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["X-API-Key"] = self.api_key

        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:  # pragma: no cover
                pass
            detail: Any = body
            try:
                detail = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                pass
            if e.code == 404:
                raise ApiNotFoundError(
                    _message_of(detail) or f"not found: {path}", detail={"path": path}) from None
            if e.code == 400 or e.code == 422:
                raise ApiValidationError(_message_of(detail) or "invalid request",
                                          detail={"path": path}) from None
            raise ApiError(f"HTTP {e.code} from {redact(url)}: {redact(str(detail))}",
                            code="HTTP_%d" % e.code, detail={"path": path}) from None
        except (urllib.error.URLError, OSError) as e:
            raise ApiError(
                f"cannot reach memory service at {redact(url)}: {e}",
                code="TRANSPORT_001",
                detail={"base_url": self.base_url, "hint":
                        "Start the server with 'mnemosyne-web' or pass "
                        "'base_url' pointing at a running instance."}) from None

    def _opts(self, options: Any, kwargs: Dict[str, Any], cls: Any) -> Any:
        """Merge loose kwargs with a typed options object.

        The options object wins on conflict, because a caller who built one
        explicitly has stated its intent more recently than a defaulted kwarg.
        """
        if isinstance(options, cls):
            merged = options.to_payload()
            merged.update({k: v for k, v in kwargs.items() if v is not None
                           and k not in merged})
            return merged
        return cls(**{k: v for k, v in kwargs.items() if v is not None}).to_payload()

    # -- health -------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """Verify connectivity and report the live configuration."""
        if self.transport == "embedded":
            return {"transport": "embedded", "status": "ok",
                    "base_url": None, "describe": self.memory.describe()}
        try:
            info = self._request("GET", "/v1/status/")
            return {"transport": "http", "status": "ok",
                    "base_url": self.base_url, "server": info}
        except ApiError as e:
            return {"transport": "http", "status": "error",
                    "base_url": self.base_url, "error": str(e),
                    "hint": e.detail.get("hint")}

    # -- memory operations --------------------------------------------------

    def add(self, messages: Any, options: Any = None,
            **kwargs: Any) -> Dict[str, Any]:
        """Add memories. HTTP mode returns an ``event_id``; embedded returns results."""
        if self.transport == "embedded":
            body = self._opts(options, kwargs, AddMemoryOptions)
            body.pop("async_mode", None)
            for key in ("filters",):
                body.pop(key, None)
            return self.memory.add(messages, **body)

        body = self._opts(options, kwargs, AddMemoryOptions)
        body["messages"] = messages if isinstance(messages, list) else \
            [{"role": "user", "content": messages}]
        return self._request("POST", "/v3/memories/add/", payload=body)

    def search(self, query: str, options: Any = None,
               **kwargs: Any) -> Dict[str, Any]:
        """Semantic search."""
        body = self._opts(options, kwargs, SearchMemoryOptions)
        if self.transport == "embedded":
            return self.memory.search(query, **body)
        body["query"] = query
        return self._request("POST", "/v3/memories/search/", payload=body)

    def get(self, memory_id: str) -> Dict[str, Any]:
        """Fetch one memory."""
        if self.transport == "embedded":
            return self.memory.get(memory_id)
        return self._request("GET", f"/v3/memories/{urllib.parse.quote(str(memory_id))}/")

    def get_all(self, options: Any = None, **kwargs: Any) -> Dict[str, Any]:
        """List memories."""
        body = self._opts(options, kwargs, GetAllMemoryOptions)
        if self.transport == "embedded":
            return self.memory.get_all(**body)
        return self._request("POST", "/v3/memories/get-all/", payload=body)

    def update(self, memory_id: str, options: Any = None,
               **kwargs: Any) -> Dict[str, Any]:
        """Patch one memory."""
        body = self._opts(options, kwargs, UpdateMemoryOptions)
        if self.transport == "embedded":
            return self.memory.update(memory_id, **body)
        return self._request("PUT", f"/v3/memories/{urllib.parse.quote(str(memory_id))}/",
                             payload=body)

    def delete(self, memory_id: str) -> Dict[str, Any]:
        """Delete one memory."""
        if self.transport == "embedded":
            return self.memory.delete(memory_id)
        return self._request("DELETE",
                             f"/v3/memories/{urllib.parse.quote(str(memory_id))}/")

    def delete_all(self, options: Any = None, **kwargs: Any) -> Dict[str, Any]:
        """Delete an entire scope."""
        body = self._opts(options, kwargs, DeleteAllOptions)
        if self.transport == "embedded":
            return self.memory.delete_all(**body)
        return self._request("DELETE", "/v3/memories/", payload=body)

    def history(self, memory_id: str) -> Dict[str, Any]:
        """Change history for one memory."""
        if self.transport == "embedded":
            return self.memory.history(memory_id)
        return self._request(
            "GET", f"/v3/memories/{urllib.parse.quote(str(memory_id))}/history/")

    def reset(self) -> Dict[str, Any]:
        """Delete every memory the client can see."""
        if self.transport == "embedded":
            self.memory.reset()
            return {"message": "All memories reset"}
        return self._request("POST", "/v1/memories/reset/")

    # -- entities & events --------------------------------------------------

    def list_entities(self, options: Any = None, **kwargs: Any) -> Dict[str, Any]:
        body = self._opts(options, kwargs, ListEventsOptions)
        if self.transport == "embedded":
            return self.memory.list_entities(**{k: v for k, v in body.items()
                                                if k not in ("status", "operation")})
        return self._request("GET", "/v2/entities/", params=body)

    def delete_entities(self, options: Any = None, **kwargs: Any) -> Dict[str, Any]:
        body = self._opts(options, kwargs, DeleteAllOptions)
        if self.transport == "embedded":
            return self.memory.delete_entities(**body)
        return self._request("DELETE", "/v2/entities/", payload=body)

    def list_events(self, options: Any = None, **kwargs: Any) -> Dict[str, Any]:
        body = self._opts(options, kwargs, ListEventsOptions)
        if self.transport == "embedded":
            return self.memory.list_events(**body)
        return self._request("GET", "/v1/events/", params=body)

    def get_event_status(self, event_id: str) -> Dict[str, Any]:
        if self.transport == "embedded":
            return self.memory.get_event_status(event_id)
        return self._request("GET", f"/v1/event/{urllib.parse.quote(str(event_id))}/")

    # -- native extensions (embedded only) ----------------------------------

    def capsule(self, memory_id: str, budget_tokens: Optional[int] = None) -> Dict[str, Any]:
        return self.memory.capsule(memory_id, budget_tokens=budget_tokens)

    def expand(self, ref: str, verify: bool = True) -> Dict[str, Any]:
        return self.memory.expand(ref, verify=verify)

    def verify_integrity(self) -> Dict[str, Any]:
        return self.memory.verify_integrity()

    def describe(self) -> Dict[str, Any]:
        return self.memory.describe()

    @classmethod
    def from_config(cls, config_dict: Optional[Dict[str, Any]] = None,
                    **kwargs: Any) -> "MemoryClient":
        """Build from a config mapping; pass ``base_url`` for the HTTP transport."""
        base_url = None
        cfg: Optional[Dict[str, Any]] = None
        if isinstance(config_dict, dict):
            cfg = dict(config_dict)
            base_url = cfg.pop("base_url", None) or cfg.pop("host", None)
            api_key = cfg.pop("api_key", None)
        else:
            api_key = None
        return cls(api_key=api_key, base_url=base_url, config=cfg or None, **kwargs)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        target = self.base_url if self.transport == "http" else "embedded"
        return f"<MemoryClient transport={self.transport} target={target}>"


def _message_of(detail: Any) -> str:
    if isinstance(detail, dict):
        for key in ("error", "message", "detail"):
            if detail.get(key):
                return str(detail[key])
        return json.dumps(detail, ensure_ascii=False)[:400]
    return str(detail or "")[:400]
