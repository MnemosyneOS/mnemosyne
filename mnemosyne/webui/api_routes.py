#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — client API REST surface
===========================================

The HTTP face of the compatibility layer, mounted alongside the existing
``/api/*`` console routes.  Both live in one process on one port, because a
memory service that needs two servers to be useful is two things to deploy and
two things to get out of sync.

Route layout
------------

======================  ====================================================
``GET  /v1/status/``    Liveness plus live provider/config report
``GET  /v1/providers/`` Every provider, transport, and whether it is usable
``POST /v3/memories/add/``       Extract and store (asynchronous, returns event)
``POST /v3/memories/search/``    Semantic search
``POST /v3/memories/get-all/``   List with filters
``GET  /v3/memories/{id}/``      Fetch one
``PUT  /v3/memories/{id}/``      Patch one
``DELETE /v3/memories/{id}/``    Soft-delete one
``DELETE /v3/memories/``         Delete a scope
``GET  /v3/memories/{id}/history/``  Change history
``POST /v1/memories/reset/``     Delete everything in scope
``GET  /v1/event/{id}/``         Poll an accepted operation
``GET  /v1/events/``             List accepted operations
``GET  /v2/entities/``           Enumerate scopes
``DELETE /v2/entities/``         Delete a scope and cascade
``POST /v3/graph/add/``          Extract relations into the graph
``POST /v3/graph/search/``       Graph-near memories
``POST /v3/graph/get-all/``      Whole relation set
``POST /v1/capsule/``            Lossless compression (native)
``POST /v1/expand/``             Byte-exact recovery (native)
``GET  /v1/integrity/``          Ledger verification (native)
======================  ====================================================

Authentication
--------------

API keys are stored as SHA-256 hashes in ``<brain_dir>/api_keys.json`` — never
in plaintext, so a leaked backup does not leak credentials.  A request may
present its key as ``Authorization: Bearer <k>``, ``Authorization: Token <k>``
or ``X-API-Key: <k>``; the three spellings exist because different clients assume
different ones and rejecting two of them would be a gratuitous incompatibility.

When no key has ever been issued **and** no key is required by configuration, the
API runs open — the local-first default.  The moment a key exists, authentication
becomes mandatory for every ``/v1`` ``/v2`` ``/v3`` route.  That rule means adding
a key is all it takes to lock a deployment down, with no second switch to forget.

Errors
------

Failures return the compatibility layer's ``{"error", "code", "detail"}`` shape
with an HTTP status mapped from the error class, so a client can branch on either
without a translation table.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from ..api.config import MemoryConfig
from ..api.errors import (ApiConflictError, ApiError,
                                  ApiNotFoundError, ApiProviderError,
                                  ApiValidationError)
from ..api.memory import Memory

__all__ = ["MemoryAPI", "get_memory", "reset_memory", "API_PREFIXES"]

#: Path prefixes this module owns.  Anything else falls through to the console.
API_PREFIXES = ("/v1/", "/v2/", "/v3/")

_LOCK = threading.RLock()
_MEMORY: Optional[Memory] = None
_MEMORY_KEY: Optional[tuple] = None


# ---------------------------------------------------------------------------
# Singleton memory + configuration
# ---------------------------------------------------------------------------

def _brain_dir() -> str:
    return os.environ.get("MNEMOSYNE_DIR") or os.path.expanduser("~/.mnemosyne")


def _config_path() -> str:
    return os.environ.get("MNEMOSYNE_API_CONFIG") or os.path.join(
        _brain_dir(), "api.config.json")


def load_server_config() -> Dict[str, Any]:
    """Read the server's component configuration.

    Precedence: the file named by ``MNEMOSYNE_API_CONFIG``, then
    ``<brain_dir>/api.config.json``, then ``MNEMOSYNE_API_CONFIG_JSON`` as an
    inline string, then empty (all built-ins).  A malformed file is reported and
    ignored rather than fatal — a server that refuses to boot because a JSON comma
    is missing is worse than one that boots with defaults and says so.
    """
    cfg: Dict[str, Any] = {}
    path = _config_path()
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cfg = json.load(fh) or {}
        except (OSError, json.JSONDecodeError) as e:
            cfg = {"_config_error": f"{path}: {e}"}
    elif os.environ.get("MNEMOSYNE_API_CONFIG_JSON"):
        try:
            cfg = json.loads(os.environ["MNEMOSYNE_API_CONFIG_JSON"])
        except json.JSONDecodeError as e:
            cfg = {"_config_error": f"MNEMOSYNE_API_CONFIG_JSON: {e}"}

    cfg.setdefault("brain_dir", _brain_dir())
    cfg.setdefault("namespace", os.environ.get("MNEMOSYNE_NAMESPACE") or None)
    return cfg


def get_memory() -> Memory:
    """Return the process-wide ``Memory``, building it on first use.

    Cached keyed on the resolved config so that editing ``api.config.json`` and
    restarting the *request* (not the process) picks up the change — which is what
    makes the console's configuration page useful rather than decorative.
    """
    global _MEMORY, _MEMORY_KEY
    cfg = load_server_config()
    key = (json.dumps(cfg, sort_keys=True), _brain_dir())
    with _LOCK:
        if _MEMORY is None or _MEMORY_KEY != key:
            if _MEMORY is not None:
                try:
                    _MEMORY.close()
                except Exception:  # pragma: no cover
                    pass
            _MEMORY = Memory(MemoryConfig.from_dict(cfg))
            _MEMORY_KEY = key
        return _MEMORY


def reset_memory() -> None:
    """Drop the cached instance so the next request rebuilds it."""
    global _MEMORY, _MEMORY_KEY
    with _LOCK:
        if _MEMORY is not None:
            try:
                _MEMORY.close()
            except Exception:  # pragma: no cover
                pass
        _MEMORY = None
        _MEMORY_KEY = None


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

class KeyStore:
    """Hashed API-key registry backed by a single JSON file.

    Only the SHA-256 of a key is stored, and the plaintext is returned exactly
    once at creation.  Keys are compared with :func:`hmac.compare_digest` so a
    timing side channel cannot be used to recover one character at a time.
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()

    def _load(self) -> List[Dict[str, Any]]:
        if not os.path.isfile(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _save(self, rows: List[Dict[str, Any]]) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:  # pragma: no cover - platform dependent
            pass

    @staticmethod
    def _hash(key: str) -> str:
        return hashlib.sha256(str(key).encode("utf-8")).hexdigest()

    def create(self, label: str = "", scopes: Optional[Dict[str, str]] = None,
               expires_at: Optional[str] = None) -> Dict[str, Any]:
        """Issue a new key. The plaintext appears once, in the return value."""
        raw = "mn-" + secrets.token_urlsafe(32)
        row = {
            "id": "key-" + secrets.token_hex(6),
            "label": label or "default",
            "hash": self._hash(raw),
            "prefix": raw[:10],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "last_used_at": None,
            "uses": 0,
            "revoked": False,
            "expires_at": expires_at,
            "scopes": scopes or {},
        }
        with self._lock:
            rows = self._load()
            rows.append(row)
            self._save(rows)
        return {**{k: v for k, v in row.items() if k != "hash"}, "key": raw}

    def list(self) -> List[Dict[str, Any]]:
        return [{k: v for k, v in r.items() if k != "hash"} for r in self._load()]

    def revoke(self, key_id: str) -> bool:
        with self._lock:
            rows = self._load()
            hit = False
            for r in rows:
                if r.get("id") == key_id:
                    r["revoked"] = True
                    hit = True
            if hit:
                self._save(rows)
            return hit

    def verify(self, presented: Optional[str]) -> Optional[Dict[str, Any]]:
        """Return the matching key row, or ``None``.

        An expired key is rejected by comparison against the same string format
        used for issuing, so clock skew between processes cannot silently extend a
        key's life.
        """
        if not presented:
            return None
        want = self._hash(presented)
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:
            rows = self._load()
            for r in rows:
                if r.get("revoked") or r.get("hash") != want:
                    continue
                exp = r.get("expires_at")
                if exp and str(exp) < now:
                    return None
                return r
        return None

    def has_keys(self) -> bool:
        return bool(self._load())


def _key_store() -> KeyStore:
    return KeyStore(os.path.join(_brain_dir(), "api_keys.json"))


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------

_STATUS_MAP = [
    (ApiNotFoundError, 404),
    (ApiValidationError, 400),
    (ApiConflictError, 409),
    (ApiProviderError, 503),
    (ApiError, 500),
]


class MemoryAPI:
    """Routes the compatible REST surface onto a :class:`Memory`.

    Framework-free on purpose: the console already runs on ``http.server``, and
    adding a web framework to expose five endpoints would put a dependency in the
    core that the zero-dependency story cannot afford.
    """

    def __init__(self, memory: Optional[Memory] = None, key_store: Optional[KeyStore] = None):
        self._memory = memory
        self._keys = key_store

    # -- wiring -------------------------------------------------------------

    @property
    def memory(self) -> Memory:
        return self._memory or get_memory()

    @property
    def keys(self) -> KeyStore:
        return self._keys or _key_store()

    # -- entry point --------------------------------------------------------

    def handle(self, method: str, path: str, *,
               body: Optional[Dict[str, Any]] = None,
               query: Optional[Dict[str, Any]] = None,
               headers: Optional[Dict[str, str]] = None
               ) -> Optional[Tuple[Dict[str, Any], int]]:
        """Dispatch one request.

        Returns ``(payload, status)``, or ``None`` when the path is not part of
        this surface so the caller can fall through to another router.
        """
        if not any(path.startswith(p) for p in API_PREFIXES):
            return None

        body = body if isinstance(body, dict) else {}
        query = query or {}
        headers = headers or {}
        method = method.upper()

        auth = self._authenticate(path, headers)
        if auth is not None:
            return auth

        try:
            return self._dispatch(method, path, body, query)
        except Exception as e:  # noqa: BLE001 - one shape for every failure
            return self._error(e)

    # -- auth ---------------------------------------------------------------

    def _authenticate(self, path: str, headers: Dict[str, str]
                      ) -> Optional[Tuple[Dict[str, Any], int]]:
        """Enforce API-key auth when any key exists; otherwise allow.

        ``/v1/status/`` is always readable so a health check can run before a key
        is provisioned — a liveness probe that needs credentials is a liveness
        probe that reports outages during credential rotation.
        """
        keys = self.keys
        if not keys.has_keys() and not _auth_required():
            return None
        if _norm_path(path) == "/v1/status/":
            return None

        presented = _presented_key(headers)
        if not presented:
            return ({"error": "authentication required", "code": "UNAUTHORIZED",
                     "detail": {"hint": "Send the key as 'Authorization: Bearer <key>' "
                                        "or 'X-API-Key: <key>'."}}, 401)
        row = keys.verify(presented)
        if row is None:
            return ({"error": "invalid or revoked API key", "code": "UNAUTHORIZED",
                     "detail": {"hint": "Issue a new key from the console "
                                        "(Settings -> API keys)."}}, 401)
        return None

    # -- dispatch -----------------------------------------------------------

    def _dispatch(self, method: str, path: str, body: Dict[str, Any],
                  query: Dict[str, Any]) -> Optional[Tuple[Dict[str, Any], int]]:
        p = _norm_path(path)
        mem = self.memory

        # ---- meta ---------------------------------------------------------
        if p == "/v1/status/" and method == "GET":
            return self._status(), 200
        if p == "/v1/providers/" and method == "GET":
            from ..api import available_providers, PROVIDERS

            return {"providers": PROVIDERS, "detail": available_providers()}, 200
        if p == "/v1/integrity/" and method == "GET":
            return {"result": mem.verify_integrity()}, 200

        # ---- API keys -----------------------------------------------------
        if p == "/v1/keys/":
            if method == "GET":
                return {"results": self.keys.list()}, 200
            if method == "POST":
                return {"result": self.keys.create(
                    label=str(body.get("label") or ""),
                    scopes=_mapping(body.get("scopes")),
                    expires_at=body.get("expires_at"))}, 201
        if p.startswith("/v1/keys/") and method == "DELETE":
            key_id = p[len("/v1/keys/"):].strip("/")
            ok = self.keys.revoke(key_id)
            return ({"message": "key revoked"} if ok else
                    {"error": "key not found", "code": "NOT_FOUND_001"}), (200 if ok else 404)

        # ---- memories -----------------------------------------------------
        if p == "/v3/memories/add/" and method == "POST":
            return self._add(body, query), 200
        if p == "/v3/memories/search/" and method == "POST":
            return self._search(body, query), 200
        if p == "/v3/memories/get-all/" and method == "POST":
            return mem.get_all(**_mem_opts(body)), 200
        if p == "/v3/memories/" and method == "DELETE":
            return mem.delete_all(**_entity_opts(body)), 200
        if p == "/v1/memories/reset/" and method == "POST":
            mem.reset()
            return {"message": "All memories reset"}, 200

        m = _match_memory_id(p)
        if m:
            memory_id, action = m
            if action == "" and method == "GET":
                return mem.get(memory_id), 200
            if action == "" and method == "PUT":
                return mem.update(memory_id, **_update_opts(body)), 200
            if action == "" and method == "DELETE":
                return mem.delete(memory_id), 200
            if action == "history" and method == "GET":
                return mem.history(memory_id), 200

        # ---- events -------------------------------------------------------
        if p == "/v1/events/" and method == "GET":
            return mem.list_events(status=query.get("status"),
                                   limit=_int(query.get("limit"), 50),
                                   operation=query.get("operation")), 200
        if p.startswith("/v1/event/") and method == "GET":
            event_id = p[len("/v1/event/"):].strip("/")
            return mem.get_event_status(event_id), 200

        # ---- entities -----------------------------------------------------
        if p == "/v2/entities/" and method == "GET":
            return mem.list_entities(limit=_int(query.get("limit"), 100)), 200
        if p == "/v2/entities/" and method == "DELETE":
            return mem.delete_entities(**_entity_opts(body)), 200

        # ---- graph --------------------------------------------------------
        if p == "/v3/graph/add/" and method == "POST":
            data = body.get("data") or body.get("text") or ""
            if not data:
                raise ApiValidationError("graph add requires 'data'")
            return mem.graph_add(data, user_id=body.get("user_id"),
                                 agent_id=body.get("agent_id"),
                                 run_id=body.get("run_id"),
                                 metadata=_mapping(body.get("metadata"))), 200
        if p == "/v3/graph/search/" and method == "POST":
            q = body.get("query") or query.get("query") or ""
            if not q:
                raise ApiValidationError("graph search requires 'query'")
            return mem.graph_search(q, user_id=body.get("user_id"),
                                    agent_id=body.get("agent_id"),
                                    run_id=body.get("run_id"),
                                    top_k=_int(body.get("top_k"), 20)), 200
        if p == "/v3/graph/get-all/" and method == "POST":
            return mem.graph_get_all(**_entity_opts(body)), 200
        if p == "/v3/graph/delete-all/" and method == "POST":
            return mem.graph_delete_all(**_entity_opts(body)), 200

        # ---- native extensions -------------------------------------------
        if p == "/v1/capsule/" and method == "POST":
            mid = body.get("memory_id")
            if not mid:
                raise ApiValidationError("capsule requires 'memory_id'")
            return mem.capsule(mid, budget_tokens=body.get("budget_tokens")), 200
        if p == "/v1/expand/" and method == "POST":
            ref = body.get("ref") or query.get("ref")
            if not ref:
                raise ApiValidationError("expand requires 'ref'")
            return mem.expand(ref), 200
        if p == "/v1/temporal/" and method == "POST":
            return mem.temporal_query(entity=body.get("entity"),
                                      limit=_int(body.get("limit"), 20)), 200

        return {"error": f"no route for {method} {p}",
                "code": "NOT_FOUND_001",
                "detail": {"path": p, "method": method}}, 404

    # -- operation handlers -------------------------------------------------

    def _add(self, body: Dict[str, Any], query: Dict[str, Any]) -> Dict[str, Any]:
        """Accept a write and answer with an event handle.

        Asynchronous is the default because the endpoint's latency would otherwise
        be the extraction model's latency, and a client with a 10-second timeout
        would fail on exactly the large imports it most wants to succeed.  Pass
        ``sync=true`` (or ``"async_mode": false``) to wait for the result.
        """
        messages = body.get("messages")
        if messages is None:
            raise ApiValidationError("'messages' is required")

        sync = str(query.get("sync", "")).lower() in ("1", "true", "yes") \
            or body.get("async_mode") is False
        kwargs = {
            "user_id": body.get("user_id"),
            "agent_id": body.get("agent_id"),
            "run_id": body.get("run_id"),
            "app_id": body.get("app_id"),
            "metadata": _mapping(body.get("metadata")),
            "infer": bool(body.get("infer", True)),
            "memory_type": body.get("memory_type"),
            "prompt": body.get("prompt") or body.get("custom_instructions"),
            "immutable": bool(body.get("immutable", False)),
            "custom_categories": body.get("custom_categories"),
            "observation_date": body.get("observation_date"),
            "timezone": body.get("timezone"),
            "temporal_reasoning": bool(body.get("temporal_reasoning", False)),
            "includes": body.get("includes"),
            "excludes": body.get("excludes"),
            "async_mode": not sync,
        }
        if body.get("timestamp") is not None:
            kwargs["timestamp"] = body["timestamp"]
        if body.get("expiration_date"):
            kwargs["expiration_date"] = body["expiration_date"]

        return self.memory.add(messages, **kwargs)

    def _search(self, body: Dict[str, Any], query: Dict[str, Any]) -> Dict[str, Any]:
        q = body.get("query") or query.get("query") or ""
        if not str(q).strip():
            raise ApiValidationError("'query' is required")
        return self.memory.search(
            q,
            top_k=_int(body.get("top_k"), 20),
            filters=_mapping(body.get("filters")),
            threshold=body.get("threshold", 0.1),
            rerank=bool(body.get("rerank", False)),
            explain=bool(body.get("explain", False)),
            reference_date=body.get("reference_date"),
            show_expired=bool(body.get("show_expired", False)),
        )

    def _status(self) -> Dict[str, Any]:
        from ..api import available_providers
        from .. import __version__

        mem = self.memory
        describe = mem.describe()
        return {
            "status": "ok",
            "server": "mnemosyne",
            "version": __version__,
            "api_version": "v3",
            "auth_required": self.keys.has_keys() or _auth_required(),
            "config_path": _config_path(),
            "config_error": describe["config"].get("_config_error"),
            "brain_dir": describe["config"]["brain_dir"],
            "degraded": describe["degraded"],
            "contexts": describe["contexts"],
            "providers": {k: v["fallback"] for k, v in
                          available_providers().items()},
            "capabilities": {
                "graph_memory": True,
                "multimodal": True,
                "reranking": True,
                "temporal_reasoning": True,
                "capsule_compression": True,
                "ledger_integrity": True,
                "async_events": True,
                "entity_scoping": ["user_id", "agent_id", "run_id", "app_id"],
            },
        }

    # -- errors -------------------------------------------------------------

    @staticmethod
    def _error(exc: Exception) -> Tuple[Dict[str, Any], int]:
        for cls, status in _STATUS_MAP:
            if isinstance(exc, cls):
                payload = exc.to_dict() if hasattr(exc, "to_dict") else {
                    "error": str(exc)}
                return payload, status
        return ({"error": f"{type(exc).__name__}: {exc}",
                 "code": "INTERNAL_001"}, 500)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm_path(path: str) -> str:
    """Normalise a path: strip the query string, ensure one trailing slash."""
    p = urllib.parse.urlparse(path).path or "/"
    if not p.endswith("/"):
        p += "/"
    return p


def _match_memory_id(p: str) -> Optional[Tuple[str, str]]:
    """Parse ``/v3/memories/{id}/`` and ``/v3/memories/{id}/history/``."""
    prefix = "/v3/memories/"
    if not p.startswith(prefix):
        return None
    rest = p[len(prefix):].strip("/")
    if not rest:
        return None
    parts = rest.split("/")
    if len(parts) == 1:
        return urllib.parse.unquote(parts[0]), ""
    if len(parts) == 2 and parts[1] == "history":
        return urllib.parse.unquote(parts[0]), "history"
    return None


def _presented_key(headers: Dict[str, str]) -> Optional[str]:
    """Extract an API key from any of the three accepted header spellings."""
    lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
    for name in ("x-api-key", "api-key"):
        if lowered.get(name):
            return str(lowered[name]).strip()
    auth = lowered.get("authorization") or ""
    if auth:
        parts = str(auth).split(None, 1)
        if len(parts) == 2 and parts[0].lower() in ("bearer", "token", "apikey"):
            return parts[1].strip()
        if len(parts) == 1:
            return parts[0].strip()
    return None


def _auth_required() -> bool:
    return str(os.environ.get("MNEMOSYNE_REQUIRE_AUTH", "")).lower() in (
        "1", "true", "yes", "on")


def _mapping(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _entity_opts(body: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the four entity dimensions out of a request body.

    Accepts them nested under ``filters`` as well, because a client ported from
    the OSS SDK habitually wraps them — and silently dropping the scope would
    turn a scoped delete into a global one.
    """
    src = dict(body or {})
    nested = _mapping(src.get("filters"))
    out: Dict[str, Any] = {}
    for key in ("user_id", "agent_id", "run_id", "app_id"):
        val = src.get(key, nested.get(key))
        if val is not None:
            out[key] = val
    return out


def _mem_opts(body: Dict[str, Any]) -> Dict[str, Any]:
    opts: Dict[str, Any] = {}
    if body.get("filters") is not None:
        opts["filters"] = _mapping(body["filters"])
    for key in ("user_id", "agent_id", "run_id", "app_id"):
        if body.get(key) is not None:
            opts.setdefault("filters", {})[key] = body[key]
    if body.get("top_k") is not None:
        opts["top_k"] = _int(body.get("top_k"), 20)
    if body.get("show_expired") is not None:
        opts["show_expired"] = bool(body["show_expired"])
    return opts


def _update_opts(body: Dict[str, Any]) -> Dict[str, Any]:
    opts: Dict[str, Any] = {}
    if body.get("text") is not None:
        opts["text"] = body["text"]
    if body.get("data") is not None and "text" not in opts:
        opts["text"] = body["data"]
    if body.get("metadata") is not None:
        opts["metadata"] = _mapping(body["metadata"])
    if body.get("expiration_date") is not None:
        opts["expiration_date"] = body["expiration_date"]
    return opts


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
