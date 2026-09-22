#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — Filter language
==============================

One filter grammar, evaluated in two places: in Python for the built-in store and
in the database for SQL-backed scans.  Both paths are generated from the same
parsed tree, so a filter cannot mean one thing in a fast path and another in a
slow one — the class of bug that makes a memory system look haunted.

Grammar
-------

.. code-block:: python

    {"user_id": "alice"}                          # shorthand equality
    {"user_id": {"eq": "alice"}}                  # explicit comparison
    {"priority": {"gte": 3}}                      # gt gte lt lte
    {"category": {"in": ["work", "health"]}}      # in / nin
    {"text": {"contains": "deadline"}}            # substring, case-sensitive
    {"name": {"icontains": "ADA"}}                # substring, case-insensitive
    {"path": {"wildcard": "/proj/*/src"}}         # glob with * and ?
    {"AND": [ {...}, {...} ]}                     # all must hold
    {"OR":  [ {...}, {...} ]}                     # at least one must hold
    {"NOT": [ {...} ]}                            # negation

``AND`` / ``OR`` / ``NOT`` may be spelled lower-case.  Nested logical operators
are supported to arbitrary depth.  A bare ``"*"`` in a comparison means "field is
present and non-null" — that is the wildcard convention the reference
implementation uses, and it is honoured rather than treated as a literal string.

Filter keys resolve against four sources in order: the entity scope
(``user_id`` / ``agent_id`` / ``run_id`` / ``app_id``), the memory record's own
columns, user metadata, and — for text predicates only — the memory's content.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from .errors import (ENTITY_KEYS, ApiValidationError, VALIDATION_008,
                     validate_entity_ids)

__all__ = [
    "COMPARISON_OPS", "LOGICAL_OPS", "normalize_filters", "compile_filter",
    "split_scope", "scope_to_where", "combine",
]

COMPARISON_OPS = frozenset({
    "eq", "ne", "gt", "gte", "lt", "lte", "in", "nin",
    "contains", "icontains", "wildcard",
})
LOGICAL_OPS = frozenset({"and", "or", "not"})

#: Keys that are part of the entity scope rather than user metadata.
SCOPE_KEYS = frozenset(ENTITY_KEYS)

#: Record columns that a filter key may address directly.  Anything not in this
#: set is treated as user metadata and looked up under ``meta``.
RECORD_KEYS = frozenset({
    "id", "content", "memory", "type", "layer", "tags", "entities",
    "importance", "confidence", "verification", "fact_type", "source_type",
    "created_at", "updated_at", "event_time", "knowledge_time",
    "access_count", "status", "session_id", "tool_name", "topic_tag",
    "expiration_date", "immutable",
})

_WILDCARD_STAR = "*"


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def _canon_op(op: str) -> str:
    o = str(op).lstrip("$").strip().lower()
    if o in LOGICAL_OPS:
        return o
    if o not in COMPARISON_OPS:
        raise ApiValidationError(
            f"unsupported filter operator: {op!r}",
            code=VALIDATION_008,
            detail={"operator": op, "supported": sorted(COMPARISON_OPS)},
        )
    return o


def normalize_filters(filters: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Validate and canonicalise a filter mapping.

    Returns a new dict whose logical operators are lower-case and whose
    comparison operators are bare (``{"user_id": {"eq": "alice"}}``).  Raises
    ``VALIDATION_008`` for a non-mapping input or an unknown operator.
    """
    if filters is None:
        return {}
    if not isinstance(filters, dict):
        raise ApiValidationError(
            f"filters must be a mapping, got {type(filters).__name__}",
            code=VALIDATION_008, detail={"received": type(filters).__name__},
        )
    out: Dict[str, Any] = {}
    for raw_key, value in filters.items():
        # A leading ``$`` on a field key is stripped for the same reason it is on
        # an operator: clients ported from document-store conventions (MongoDB and
        # friends) write ``$user_id``, and treating that as a literal field name
        # would make the filter match nothing — a silent wrong answer rather than
        # an error.
        key = str(raw_key).lstrip("$")
        lowered = key.lower()
        if lowered in LOGICAL_OPS:
            if not isinstance(value, (list, tuple, dict)):
                raise ApiValidationError(
                    f"{key} expects a list of sub-filters or a single filter dict",
                    code=VALIDATION_008, detail={"key": key},
                )
            subs = value if isinstance(value, (list, tuple)) else [value]
            out[lowered] = [normalize_filters(s) for s in subs]
            continue
        if isinstance(value, dict):
            ops: Dict[str, Any] = {}
            for op, operand in value.items():
                canon = _canon_op(op)
                if canon in LOGICAL_OPS:
                    raise ApiValidationError(
                        f"logical operator {op!r} cannot nest inside a field comparison",
                        code=VALIDATION_008, detail={"field": key},
                    )
                ops[canon] = operand
            out[key] = ops or {"eq": None}
        else:
            out[key] = {"eq": value}
    return out


def split_scope(filters: Optional[Dict[str, Any]]) -> Tuple[Dict[str, str],
                                                            Dict[str, Any]]:
    """Separate entity-scope keys from everything else.

    Only top-level equality on an entity key counts as scope: ``{"user_id":
    {"in": [...]}}`` is a *metadata* predicate, because a memory belongs to
    exactly one user and a set membership test there means the caller is asking
    about many users at once, which is a different question.
    """
    norm = normalize_filters(filters)
    scope: Dict[str, str] = {}
    rest: Dict[str, Any] = {}
    for key, ops in norm.items():
        if key in SCOPE_KEYS and set(ops) == {"eq"}:
            val = ops["eq"]
            if val is not None:
                scope[key] = str(val)
            continue
        rest[key] = ops
    validate_entity_ids(scope)
    return scope, rest


def scope_to_where(scope: Dict[str, str]) -> Dict[str, Any]:
    """Render a scope as a flat ``{meta_key: value}`` mapping for the store.

    Entity ids live in the record's ``meta`` mapping, so the store sees them as
    ordinary metadata keys.  Keeping that translation in one function means the
    SQL path and the Python path cannot disagree about where a scope lives.
    """
    return {k: v for k, v in (scope or {}).items() if v is not None}


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def _field_value(record: Dict[str, Any], key: str) -> Any:
    """Resolve a filter key against a record, checking the four sources."""
    if key in ("memory", "text") and "content" not in record:
        return record.get("memory")
    if key in RECORD_KEYS and key in record:
        return record.get(key)
    meta = record.get("meta")
    if isinstance(meta, dict):
        if key in meta:
            return meta[key]
    # Fall back to a top-level key so a caller can filter on anything the store
    # happens to expose without the engine having to enumerate it.
    return record.get(key)


def _matches(value: Any, op: str, operand: Any) -> bool:
    if op == "eq":
        if operand == _WILDCARD_STAR:
            return value is not None
        if isinstance(operand, (list, tuple, set)):
            return value in operand
        return _loose_eq(value, operand)

    if op == "ne":
        if operand == _WILDCARD_STAR:
            return value is None
        return not _loose_eq(value, operand)

    if op in ("gt", "gte", "lt", "lte"):
        if value is None:
            return False
        a, b = _coerce_pair(value, operand)
        if a is None:
            return False
        try:
            return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
        except TypeError:
            return False

    if op == "in":
        if value is None:
            return False
        options = operand if isinstance(operand, (list, tuple, set)) else [operand]
        if _WILDCARD_STAR in options:
            return True
        return any(_loose_eq(value, o) for o in options)

    if op == "nin":
        options = operand if isinstance(operand, (list, tuple, set)) else [operand]
        return not any(_loose_eq(value, o) for o in options)

    if op == "contains":
        return operand is not None and str(operand) in _flatten(value)

    if op == "icontains":
        return operand is not None and str(operand).lower() in _flatten(value).lower()

    if op == "wildcard":
        pattern = str(operand or "")
        return fnmatch.fnmatchcase(_flatten(value), pattern)

    return False


def _loose_eq(value: Any, operand: Any) -> bool:
    """Equality that tolerates the string/number boundary.

    A filter arriving from JSON or from an HTTP query string is often the string
    ``"3"`` where the stored value is the integer ``3``.  Rejecting that would
    make the same filter work in Python and fail over HTTP, so a numeric-string
    comparison is allowed — but only when one side really is numeric, so
    ``"3"`` never matches ``"03x"``.
    """
    if value == operand:
        return True
    if isinstance(value, bool) or isinstance(operand, bool):
        return False
    if isinstance(value, (int, float)) and isinstance(operand, str):
        try:
            return float(value) == float(operand)
        except (TypeError, ValueError):
            return False
    if isinstance(operand, (int, float)) and isinstance(value, str):
        try:
            return float(value) == float(operand)
        except (TypeError, ValueError):
            return False
    return False


def _coerce_pair(a: Any, b: Any) -> Tuple[Any, Any]:
    if isinstance(a, (int, float)) and isinstance(b, str):
        try:
            return a, float(b)
        except (TypeError, ValueError):
            return None, None
    if isinstance(a, str) and isinstance(b, (int, float)):
        try:
            return float(a), b
        except (TypeError, ValueError):
            return None, None
    return a, b


def _flatten(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return " ".join(str(v) for v in value)
    return str(value)


def compile_filter(filters: Optional[Dict[str, Any]]) -> Callable[[Dict[str, Any]], bool]:
    """Compile *filters* into a predicate over a record dict.

    Returns a function that always answers, never raises: a comparison between
    incomparable types evaluates to ``False`` rather than blowing up a search,
    because one malformed metadata value must not take down a whole query.
    """
    norm = normalize_filters(filters)

    def predicate(record: Dict[str, Any]) -> bool:
        if not isinstance(record, dict):
            return False
        return _eval(norm, record)

    return predicate


def _eval(node: Dict[str, Any], record: Dict[str, Any]) -> bool:
    for key, value in node.items():
        if key == "and":
            if not all(_eval(sub, record) for sub in value):
                return False
        elif key == "or":
            if not any(_eval(sub, record) for sub in value):
                return False
        elif key == "not":
            if any(_eval(sub, record) for sub in value):
                return False
        else:
            current = _field_value(record, key)
            for op, operand in value.items():
                if not _matches(current, op, operand):
                    return False
    return True


def combine(*filters: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """AND several filter mappings together, dropping empty ones.

    Used when the engine layers its own constraints (not-expired, active only)
    on top of a caller's filter without either side having to know about the
    other's shape.
    """
    parts = [normalize_filters(f) for f in filters if f]
    if not parts:
        return {}
    if len(parts) == 1:
        return parts[0]
    return {"and": parts}


def describe_filter(filters: Optional[Dict[str, Any]]) -> str:
    """Render a filter as a readable one-liner — used in audit entries.

    Kept deliberately lossy: an audit log needs to say *which* memories were
    selected without reproducing a filter that might itself contain user data.
    """
    norm = normalize_filters(filters)
    if not norm:
        return "(no filter)"
    bits: List[str] = []
    for key, ops in norm.items():
        if isinstance(ops, list):
            bits.append(f"{key.upper()}({len(ops)})")
            continue
        for op, operand in ops.items():
            shown = operand
            if isinstance(operand, str) and len(operand) > 40:
                shown = operand[:37] + "..."
            bits.append(f"{key} {op} {shown!r}")
    return " AND ".join(bits)
