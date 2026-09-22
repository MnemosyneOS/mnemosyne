#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — Operation event log
==================================

Large writes and bulk deletes can be answered before they finish, which means the
caller needs a handle to poll.  That is what ``event_id`` is, and this module is
what makes it more than a promissory note: every accepted operation gets a durable
record with a status that is *observable from outside the process*, so a client
that loses the connection can still find out whether its write landed.

Two properties are load-bearing:

**Durability before acknowledgement.** The event is appended and flushed *before*
``add()`` returns, so an ``event_id`` a client has seen always corresponds to a
row on disk.  Acking first and writing later loses exactly the operations the
caller most wants tracked — the ones that were interrupted.

**Bounded growth.** The log is append-only but self-pruning: past
:data:`MAX_EVENTS_PER_SCOPE` entries the file is rewritten with the newest kept.
An unbounded operation log on a long-lived agent is a slow disk leak, and the old
entries have no reader after the client has seen its status.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

__all__ = ["EventStatus", "EventLog", "new_event_id"]

PENDING = "PENDING"
RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

TERMINAL = frozenset({SUCCEEDED, FAILED, CANCELLED})

MAX_EVENTS_PER_SCOPE = 500


class EventStatus:
    """The status vocabulary, exposed as constants."""

    PENDING = PENDING
    RUNNING = RUNNING
    SUCCEEDED = SUCCEEDED
    FAILED = FAILED
    CANCELLED = CANCELLED
    TERMINAL = TERMINAL


def new_event_id() -> str:
    """A time-sortable, collision-free event identifier."""
    return f"evt-{int(time.time() * 1000):013d}-{uuid.uuid4().hex[:10]}"


class EventLog:
    """Append-only, self-pruning per-scope operation log.

    Thread-safe: the compatibility layer may accept an asynchronous ``add()``
    from one thread and answer ``get_event_status`` from another, so a lock
    guards both the in-memory index and the file rewrite.
    """

    def __init__(self, base_dir: str, scope_key: str = "default"):
        self.base_dir = os.path.join(base_dir, "data", "events")
        self.scope_key = scope_key or "default"
        self.path = os.path.join(self.base_dir, f"{_safe(self.scope_key)}.jsonl")
        self._lock = threading.RLock()
        self._cache: Optional[Dict[str, Dict[str, Any]]] = None

    # -- writes -------------------------------------------------------------

    def record(self, operation: str, *, scope: Optional[Dict[str, Any]] = None,
               status: str = PENDING, detail: Optional[Dict[str, Any]] = None,
               event_id: Optional[str] = None) -> Dict[str, Any]:
        """Append an event and return its full record."""
        evt = {
            "event_id": event_id or new_event_id(),
            "operation": operation,
            "status": status,
            "scope": {k: v for k, v in (scope or {}).items() if v is not None},
            "created_at": _now(),
            "updated_at": _now(),
            "detail": detail or {},
        }
        with self._lock:
            os.makedirs(self.base_dir, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(evt, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            if self._cache is not None:
                self._cache[evt["event_id"]] = evt
                self._prune_if_needed()
        return evt

    def update(self, event_id: str, *, status: Optional[str] = None,
               detail: Optional[Dict[str, Any]] = None,
               error: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Record a status transition.

        Rewrites the single line in place rather than appending a new record, so
        the log stays one-line-per-operation and a reader never has to fold a
        sequence to learn the current state.  Concurrent writers are serialised
        by the lock, and the rewrite is atomic via a temporary file + replace.
        """
        with self._lock:
            entries = self._load(force=True)
            evt = entries.get(event_id)
            if evt is None:
                return None
            if status:
                evt["status"] = status
            if detail:
                evt.setdefault("detail", {}).update(detail)
            if error:
                evt["error"] = error
                evt["status"] = FAILED
            evt["updated_at"] = _now()
            self._rewrite(entries)
            self._cache = entries
            return dict(evt)

    # -- reads --------------------------------------------------------------

    def get(self, event_id: str) -> Optional[Dict[str, Any]]:
        """Fetch one event, or ``None`` when unknown."""
        return self._load().get(event_id)

    def list(self, *, status: Optional[str] = None, limit: int = 50,
             operation: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return events newest-first, optionally filtered."""
        rows = list(self._load().values())
        if status:
            wanted = str(status).upper()
            rows = [r for r in rows if str(r.get("status", "")).upper() == wanted]
        if operation:
            rows = [r for r in rows if r.get("operation") == operation]
        rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        return [dict(r) for r in rows[:limit]]

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for r in self._load().values():
            key = str(r.get("status") or "UNKNOWN")
            out[key] = out.get(key, 0) + 1
        return out

    def clear(self) -> None:
        with self._lock:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass
            self._cache = {}

    # -- internals ----------------------------------------------------------

    def _load(self, force: bool = False) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            if self._cache is not None and not force:
                return self._cache
            events: Dict[str, Dict[str, Any]] = {}
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            # A torn final line from a crash mid-write is
                            # expected; skip it rather than discarding the log.
                            continue
                        eid = row.get("event_id")
                        if eid:
                            events[eid] = row
            except FileNotFoundError:
                pass
            self._cache = events
            return events

    def _rewrite(self, events: Dict[str, Dict[str, Any]]) -> None:
        os.makedirs(self.base_dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for row in events.values():
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def _prune_if_needed(self) -> None:
        if self._cache is None or len(self._cache) <= MAX_EVENTS_PER_SCOPE:
            return
        ordered = sorted(self._cache.values(),
                         key=lambda r: str(r.get("created_at") or ""))
        keep = ordered[-MAX_EVENTS_PER_SCOPE:]
        self._cache = {r["event_id"]: r for r in keep}
        self._rewrite(self._cache)


def _safe(name: str) -> str:
    import re

    s = re.sub(r"[^\w\-.]", "_", str(name or "default"))
    return (s or "default")[:96]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
