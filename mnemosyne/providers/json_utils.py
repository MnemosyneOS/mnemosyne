#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — Lenient JSON extraction
======================================

Models are asked for JSON and frequently deliver it wrapped in a markdown code
fence, prefixed by a sentence of preamble, or with a trailing comment.  Rejecting
those responses would make every extraction failure a hard failure, so the
engine parses defensively instead.

``loads_lenient`` is the single entry point: it tries strict parsing first and
only then walks progressively more aggressive repairs.  It never *invents*
structure — if nothing parses, it raises, and the caller decides whether to
retry, fall back to the rule-based extractor, or surface the error.
"""

from __future__ import annotations

import json
import re
from typing import Any, List, Optional

__all__ = ["loads_lenient", "extract_json_blob", "ensure_list", "coerce_str_list"]

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_LINE_COMMENT_RE = re.compile(r"(?<![:\"\\])//[^\n\"]*$", re.MULTILINE)


class LenientJSONError(ValueError):
    """No JSON value could be recovered from the text."""


def loads_lenient(text: Any) -> Any:
    """Parse *text* as JSON, repairing common model-output defects.

    Handles, in order:

    1. a value that is already not a string (passed through unchanged);
    2. strict ``json.loads``;
    3. a fenced ```` ```json ... ``` ```` block;
    4. the first balanced ``{...}`` or ``[...]`` span in the text;
    5. trailing commas before a closing brace/bracket;
    6. ``//`` line comments outside of strings;
    7. Python-style ``True`` / ``False`` / ``None`` literals.
    """
    if not isinstance(text, (str, bytes, bytearray)):
        return text
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8", errors="replace")

    candidates: List[str] = [text.strip()]

    fence = _FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1).strip())

    blob = extract_json_blob(text)
    if blob:
        candidates.append(blob)

    for cand in list(candidates):
        if not cand:
            continue
        repaired = _TRAILING_COMMA_RE.sub(r"\1", _LINE_COMMENT_RE.sub("", cand))
        if repaired != cand:
            candidates.append(repaired)
        if re.search(r"\b(True|False|None)\b", cand):
            candidates.append(
                cand.replace("True", "true")
                    .replace("False", "false")
                    .replace("None", "null")
            )

    for cand in candidates:
        if not cand:
            continue
        try:
            return json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue

    raise LenientJSONError(
        "could not recover JSON from model output: " + text[:300].replace("\n", " ")
    )


def extract_json_blob(text: str) -> Optional[str]:
    """Return the first balanced ``{...}`` or ``[...]`` span in *text*.

    String literals are respected, so a brace inside a quoted value does not
    end the span early.
    """
    start = None
    opener = closer = ""
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            opener = ch
            closer = "}" if ch == "{" else "]"
            break
    if start is None:
        return None

    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def ensure_list(value: Any) -> List[Any]:
    """Normalise a possibly-scalar value into a list.

    ``None`` becomes ``[]``; a list is returned as-is; a dict becomes a
    single-element list (models sometimes return one object where a list was
    requested); anything else is wrapped.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def coerce_str_list(value: Any) -> List[str]:
    """Best-effort conversion of a value into a list of non-empty strings.

    Accepts a bare string (split on commas — models often answer ``"a, b"``
    where a list was requested), a list of strings, or a list of dicts with a
    ``name``/``value``/``text`` key.
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
        return [p for p in parts if p]
    out: List[str] = []
    for item in ensure_list(value):
        if isinstance(item, str):
            if item.strip():
                out.append(item.strip())
        elif isinstance(item, dict):
            for key in ("name", "value", "text", "label", "memory"):
                v = item.get(key)
                if isinstance(v, str) and v.strip():
                    out.append(v.strip())
                    break
        elif item is not None:
            out.append(str(item))
    return out
