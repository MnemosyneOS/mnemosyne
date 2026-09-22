#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — Record projection and dimension scoping
=====================================================

Two translations live here, both of which exist to keep one representation from
leaking into the other:

**Projection.** Mnemosyne stores a rich record — layer, fact type, confidence,
verification state, verification chain, capsule pointer, access counters. The
compatible API exposes a deliberately smaller object (``id``, ``memory``,
``metadata``, ``score``, ``created_at``, ``updated_at``, plus the entity
dimensions).  :func:`to_memory_item` performs that reduction, and
:func:`to_record_updates` performs the inverse for writes.

The reduction is lossy *by design* but not destructive: anything omitted from the
top level is still reachable through ``metadata``, so a caller that needs
confidence or verification can read it without a second call.  A projection that
simply dropped those fields would make the compatible API a downgrade.

**Dimension scoping.** The reference treats ``user_id`` / ``agent_id`` /
``run_id`` / ``app_id`` as filter values inside one shared store.  Mnemosyne
treats them as *physical* isolation: each distinct combination gets its own
directory and its own SQLite file.  That is a stronger guarantee — one tenant
cannot read another's rows even with a malformed filter — and it is why
:func:`derive_namespace` must be deterministic and collision-resistant.

The namespace is a hash of the *sorted* dimension set with each key and value
length-prefixed.  Sorting removes ordering sensitivity; length-prefixing removes
the ``("a", "bc")`` vs ``("ab", "c")`` collision that plain concatenation
introduces, which would otherwise merge two tenants into one store.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

from .errors import ENTITY_KEYS

__all__ = ["to_memory_item", "to_memory_items", "to_record_updates",
           "derive_namespace", "scope_summary", "item_id", "is_immutable",
           "MEMORY_ITEM_FIELDS"]


def is_immutable(record: Dict[str, Any]) -> bool:
    """Whether a record is protected from text edits.

    Checks both the top-level key and ``meta``. Immutability is a
    compatibility-layer concept, so it is written into ``meta`` (which is what
    persists) rather than relying on a top-level column that the store does not
    have — reading only the top level is how the guard silently stopped working
    the moment the record came back from disk.
    """
    if not isinstance(record, dict):
        return False
    if record.get("immutable"):
        return True
    meta = record.get("meta")
    return bool(isinstance(meta, dict) and meta.get("immutable"))

#: The fields the compatible API exposes at the top level of a memory item.
MEMORY_ITEM_FIELDS = ("id", "memory", "hash", "metadata", "score",
                      "created_at", "updated_at")


def item_id(record: Dict[str, Any]) -> Optional[str]:
    """Return a record's identifier under either naming convention."""
    return record.get("id") or record.get("memory_id")


def to_memory_item(record: Dict[str, Any], *,
                   score: Optional[float] = None,
                   include_metadata: bool = True) -> Dict[str, Any]:
    """Reduce an engine record to a compatible memory item.

    ``metadata`` carries three kinds of key, merged in this order so later ones
    win on collision:

    1. the record's own diagnostic fields (``type``, ``layer``, ``confidence``,
       ``verification``, ``importance``, ...) — always present, because they are
       what makes a projected item still useful for debugging;
    2. the user's ``metadata`` mapping, which is the part a caller round-tripped
       through ``add()``;
    3. the entity dimensions, so a caller can see which scope a hit came from
       without having to remember what it asked for.
    """
    if not isinstance(record, dict):
        return {}
    rid = item_id(record)
    content = record.get("content") or record.get("memory") or ""

    meta: Dict[str, Any] = {}
    if include_metadata:
        meta.update({
            "type": record.get("type"),
            "layer": record.get("layer"),
            "fact_type": record.get("fact_type"),
            "source_type": record.get("source_type"),
            "importance": record.get("importance"),
            "confidence": record.get("confidence"),
            "verification": record.get("verification"),
            "tags": list(record.get("tags") or []),
            "entities": list(record.get("entities") or []),
            "access_count": record.get("access_count"),
        })
        if record.get("event_time"):
            meta["event_time"] = record["event_time"]
        if record.get("session_id"):
            meta["session_id"] = record["session_id"]
        if record.get("topic_tag"):
            meta["topic_tag"] = record["topic_tag"]
        if record.get("superseded_by"):
            meta["superseded_by"] = record["superseded_by"]
        if is_immutable(record):
            meta["immutable"] = True
        if record.get("expiration_date") or record.get("expires_at"):
            meta["expiration_date"] = record.get("expiration_date") \
                or record.get("expires_at")

        user_meta = record.get("meta")
        if isinstance(user_meta, dict):
            meta.update(user_meta)

        for key in ENTITY_KEYS:
            val = record.get(key)
            if val is None and isinstance(user_meta, dict):
                val = user_meta.get(key)
            if val is not None:
                meta[key] = val

    item: Dict[str, Any] = {
        "id": rid,
        "memory": content,
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at") or record.get("created_at"),
    }
    if record.get("content_hash"):
        item["hash"] = record["content_hash"]
    if score is not None:
        item["score"] = round(float(score), 6)
    if include_metadata:
        item["metadata"] = {k: v for k, v in meta.items() if v is not None}

    # Categories are promoted to the top level because the reference contract
    # documents them there and callers branch on `item["categories"]`.
    cats = meta.get("categories")
    if cats:
        item["categories"] = list(cats)
    return item


def to_memory_items(records_with_scores: Any, *,
                    include_metadata: bool = True) -> List[Dict[str, Any]]:
    """Project an ``[(score, record, reasons)]`` or ``[(record, score)]`` list.

    The engine's own recall returns a 3-tuple; the compatible layer returns a
    2-tuple.  Accepting both here means the projection is not duplicated per
    caller and cannot drift between them.
    """
    out: List[Dict[str, Any]] = []
    for entry in records_with_scores or []:
        score: Optional[float] = None
        record: Any = None
        if isinstance(entry, dict):
            record = entry
        elif isinstance(entry, (list, tuple)):
            if len(entry) >= 3 and isinstance(entry[0], (int, float)):
                score, record = entry[0], entry[1]
            elif len(entry) == 2:
                a, b = entry
                if isinstance(a, (int, float)):
                    score, record = a, b
                elif isinstance(b, (int, float)):
                    record, score = a, b
                else:
                    record = a
            elif len(entry) == 1:
                record = entry[0]
        if isinstance(record, dict):
            out.append(to_memory_item(record, score=score,
                                      include_metadata=include_metadata))
    return out


def to_record_updates(text: Optional[str] = None,
                      metadata: Optional[Dict[str, Any]] = None,
                      expiration_date: Any = "__unset__",
                      immutable: Optional[bool] = None) -> Dict[str, Any]:
    """Translate ``update()`` arguments into engine record updates.

    Only supplied values appear in the result, so an update never clears a field
    the caller did not mention — the failure mode that makes an "edit one field"
    call silently wipe everything else.

    ``expiration_date`` uses a sentinel rather than ``None`` so "not supplied"
    and "explicitly cleared" remain distinguishable.
    """
    updates: Dict[str, Any] = {}
    if text is not None:
        updates["content"] = str(text)
    if expiration_date != "__unset__":
        updates["expiration_date"] = expiration_date
        updates["expires_at"] = expiration_date
    if immutable is not None:
        updates["immutable"] = bool(immutable)
    if metadata:
        clean = {k: v for k, v in metadata.items() if v is not None}
        if clean:
            # Metadata is merged, not replaced, because the reference treats
            # update(metadata=...) as a patch over the existing mapping.
            updates["meta_patch"] = clean
    return updates


def derive_namespace(scope: Optional[Dict[str, Any]],
                     base: Optional[str] = None) -> str:
    """Derive a filesystem-safe, collision-resistant namespace from a scope.

    An empty scope maps to ``"default"`` so the un-scoped case is readable in a
    directory listing — a hash there would make the common case the opaque one.

    A single dimension keeps its value in the name for the same reason: a
    deployment with only ``user_id`` should see ``user_alice``, not a hex blob.
    Two or more dimensions are hashed, because the readable form does not survive
    arbitrary values (a value may contain the separator, be very long, or be a
    path fragment).
    """
    dims = {k: str(v) for k, v in (scope or {}).items()
            if k in ENTITY_KEYS and v is not None and str(v) != ""}
    if base:
        dims = {"base": str(base), **dims}
    if not dims:
        return "default"

    if len(dims) == 1:
        key, value = next(iter(dims.items()))
        return _sanitize(f"{key}_{value}")

    digest = hashlib.sha256()
    for key in sorted(dims):
        value = dims[key]
        digest.update(f"{len(key)}:{key}={len(value)}:{value}\x1f".encode("utf-8"))
    prefix = "_".join(k[:2] for k in sorted(dims))
    return f"{prefix}_{digest.hexdigest()[:16]}"


def _sanitize(name: str) -> str:
    """Make *name* safe to use as a single path component."""
    import re
    import unicodedata

    s = unicodedata.normalize("NFKC", str(name))
    s = re.sub(r"[^\w\-.]", "_", s, flags=re.UNICODE)
    s = re.sub(r"_{2,}", "_", s).strip("._")
    if not s:
        s = "unnamed"
    return s[:96]


def scope_summary(scope: Optional[Dict[str, Any]]) -> str:
    """Render a scope for logs and audit entries."""
    dims = {k: v for k, v in (scope or {}).items()
            if k in ENTITY_KEYS and v is not None}
    if not dims:
        return "global"
    return " ".join(f"{k}={dims[k]}" for k in ENTITY_KEYS if k in dims)
