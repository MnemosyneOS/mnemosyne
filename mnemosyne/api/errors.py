#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — client API error types
==========================================

Error *codes* matter more than messages here.  A caller migrating from the
reference SDK typically branches on the code or on the exception type, and a
message that is merely similar will not keep that branch working.  The codes
below are the ones the compatibility layer is contracted to preserve:

===================  ==========================================================
``VALIDATION_003``   ``add()`` was given a ``messages`` value of the wrong type.
``VALIDATION_004``   An entity id was empty or whitespace-only.
``VALIDATION_005``   An entity id contained internal whitespace.
``VALIDATION_006``   ``threshold`` fell outside ``[0, 1]``.
``VALIDATION_007``   A top-level entity kwarg was passed to a method that
                     requires it inside ``filters``.
``VALIDATION_008``   ``filters`` was not a mapping.
``NOT_FOUND_001``    No memory exists for the given ``memory_id``.
``CONFLICT_001``     The operation contradicts the current state (e.g. deleting
                     an immutable memory).
``PROVIDER_001``     A configured component could not be built; the message
                     carries the provider's own hint.
===================  ==========================================================
"""

from __future__ import annotations

from typing import Any, List, Optional

__all__ = [
    "ApiError", "ApiValidationError", "ApiNotFoundError",
    "ApiConflictError", "ApiProviderError",
    "ENTITY_KEYS", "validate_entity_id", "validate_entity_ids",
    "VALIDATION_003", "VALIDATION_004", "VALIDATION_005", "VALIDATION_006",
    "VALIDATION_007", "VALIDATION_008", "NOT_FOUND_001", "CONFLICT_001",
    "PROVIDER_001",
]

VALIDATION_003 = "VALIDATION_003"
VALIDATION_004 = "VALIDATION_004"
VALIDATION_005 = "VALIDATION_005"
VALIDATION_006 = "VALIDATION_006"
VALIDATION_007 = "VALIDATION_007"
VALIDATION_008 = "VALIDATION_008"
NOT_FOUND_001 = "NOT_FOUND_001"
CONFLICT_001 = "CONFLICT_001"
PROVIDER_001 = "PROVIDER_001"

#: The four scoping dimensions, in the order they are reported.
ENTITY_KEYS = ("user_id", "agent_id", "run_id", "app_id")


class ApiError(Exception):
    """Base class for every compatibility-layer error."""

    code: str = "ERROR_000"

    def __init__(self, message: str, *, code: Optional[str] = None,
                 detail: Optional[dict] = None):
        self.message = message
        self.detail = detail or {}
        if code:
            self.code = code
        super().__init__(f"[{self.code}] {message}")

    def to_dict(self) -> dict:
        return {"error": self.message, "code": self.code, "detail": self.detail}


class ApiValidationError(ApiError, ValueError):
    """Input failed validation.  Subclasses :class:`ValueError` so existing
    ``except ValueError`` handlers keep working unchanged."""

    code = "VALIDATION_000"


class ApiNotFoundError(ApiError, KeyError):
    """The referenced memory does not exist.

    Subclasses :class:`KeyError` for the same reason as above — the reference
    implementation raises ``KeyError`` here.
    """

    code = NOT_FOUND_001


class ApiConflictError(ApiError, RuntimeError):
    """The operation is valid but conflicts with the stored state."""

    code = CONFLICT_001


class ApiProviderError(ApiError, RuntimeError):
    """A configured component (LLM / embedder / store) is unusable.

    Kept distinct from validation errors because the fix is configuration, not
    input — and because the engine can often continue with a fallback, so a
    caller may reasonably choose to catch this and carry on.
    """

    code = PROVIDER_001

    def __init__(self, message: str, *, provider: str = "",
                 hint: str = "", detail: Optional[dict] = None):
        self.provider = provider
        self.hint = hint
        d = dict(detail or {})
        if provider:
            d["provider"] = provider
        if hint:
            d["hint"] = hint
        super().__init__(message, code=PROVIDER_001, detail=d)


# ---------------------------------------------------------------------------
# Entity-id validation
# ---------------------------------------------------------------------------

def validate_entity_id(value: Any, name: str) -> Optional[str]:
    """Normalise and validate one entity identifier.

    Rules, matching the reference contract exactly:

    * ``None`` passes through as ``None`` — an unspecified dimension is legal;
    * the value is coerced with ``str()`` and trimmed;
    * an empty or whitespace-only result raises ``VALIDATION_004``;
    * an identifier containing internal whitespace raises ``VALIDATION_005``.

    The internal-whitespace ban is not cosmetic: these ids become part of a
    namespace path and of filter keys, and allowing ``"a b"`` in one dimension
    while another layer trims it silently produces two stores where the caller
    believed there was one.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise ApiValidationError(
            f"entity id '{name}' must be a non-empty, non-whitespace string",
            code=VALIDATION_004, detail={"field": name, "value": value},
        )
    if any(ch.isspace() for ch in text):
        raise ApiValidationError(
            f"entity id '{name}' must not contain whitespace",
            code=VALIDATION_005, detail={"field": name, "value": value},
        )
    return text


def validate_entity_ids(filters_or_kwargs: dict,
                        *, allow_all_empty: bool = True) -> dict:
    """Validate every present entity key in a mapping.

    Returns a new dict containing only the entity keys that were supplied, with
    values normalised.  Raises on the first invalid value so the error names the
    offending dimension.
    """
    out: dict = {}
    for key in ENTITY_KEYS:
        if key not in filters_or_kwargs:
            continue
        norm = validate_entity_id(filters_or_kwargs.get(key), key)
        if norm is not None:
            out[key] = norm
    if not out and not allow_all_empty:
        raise ApiValidationError(
            "at least one of user_id / agent_id / run_id / app_id is required",
            code=VALIDATION_004,
        )
    return out


def reject_top_level_entity_kwargs(kwargs: dict, method: str) -> None:
    """Raise if a caller passed entity ids as keyword arguments.

    ``search()`` and ``get_all()`` require entity ids inside ``filters`` while
    ``add()`` and ``delete_all()`` take them as top-level keywords.  Accepting
    both spellings would make a typo silently return everything, so the mismatch
    is turned into a hard error with a copy-pasteable fix.
    """
    present = [k for k in ENTITY_KEYS if k in kwargs and kwargs[k] is not None]
    if not present:
        return
    shown = ", ".join(f'{k}="{kwargs[k]}"' for k in present)
    raise ApiValidationError(
        f"{method}() no longer accepts {', '.join(present)} as top-level "
        f"keyword arguments; pass them inside filters instead",
        code=VALIDATION_007,
        detail={
            "method": method,
            "received": present,
            "fix": f'{method}(..., filters={{{shown}}})',
        },
    )


def validate_threshold(threshold: Any) -> float:
    """Coerce *threshold* and require it to lie in ``[0, 1]``."""
    try:
        val = float(threshold)
    except (TypeError, ValueError):
        raise ApiValidationError(
            f"threshold must be a number, got {type(threshold).__name__}",
            code=VALIDATION_006, detail={"threshold": threshold},
        ) from None
    if not (0.0 <= val <= 1.0):
        raise ApiValidationError(
            f"threshold must be within [0, 1], got {val}",
            code=VALIDATION_006, detail={"threshold": val},
        )
    return val


def validate_messages(messages: Any) -> List[dict]:
    """Normalise ``add()``'s ``messages`` argument into ``list[dict]``.

    Accepts a single string, a single message dict, or a list of message dicts.
    Anything else raises ``VALIDATION_003``.  A bare string is wrapped as a
    single ``user`` turn because that is the overwhelmingly common shorthand.
    """
    if messages is None:
        raise ApiValidationError(
            "messages is required and must be a str, dict, or list of dicts",
            code=VALIDATION_003, detail={"received": "None"},
        )
    if isinstance(messages, str):
        if not messages.strip():
            raise ApiValidationError(
                "messages string must not be empty",
                code=VALIDATION_003, detail={"received": "empty str"},
            )
        return [{"role": "user", "content": messages}]
    if isinstance(messages, dict):
        if "content" not in messages:
            raise ApiValidationError(
                "message dict must contain a 'content' key",
                code=VALIDATION_003, detail={"received": list(messages)},
            )
        return [{"role": messages.get("role", "user"),
                 "content": _content_text(messages["content"])}]
    if isinstance(messages, (list, tuple)):
        out: List[dict] = []
        for i, item in enumerate(messages):
            if isinstance(item, str):
                out.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                if "content" not in item:
                    raise ApiValidationError(
                        f"messages[{i}] must contain a 'content' key",
                        code=VALIDATION_003, detail={"index": i},
                    )
                out.append({"role": item.get("role", "user"),
                            "content": _content_text(item["content"])})
            else:
                raise ApiValidationError(
                    f"messages[{i}] must be a str or dict, got "
                    f"{type(item).__name__}",
                    code=VALIDATION_003, detail={"index": i},
                )
        if not out:
            raise ApiValidationError("messages must not be empty",
                                      code=VALIDATION_003)
        return out
    raise ApiValidationError(
        "messages must be a str, dict, or list of dicts, got "
        f"{type(messages).__name__}",
        code=VALIDATION_003, detail={"received": type(messages).__name__},
    )


def _content_text(content: Any) -> str:
    """Flatten a message's ``content`` into plain text.

    Multimodal turns arrive as a list of typed parts.  Text parts are
    concatenated and non-text parts (images) are kept as a marked placeholder so
    the extractor can tell that something visual was present rather than
    silently treating the turn as empty.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: List[str] = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                ptype = part.get("type")
                if ptype in (None, "text", "input_text"):
                    chunks.append(str(part.get("text") or part.get("content") or ""))
                elif ptype in ("image_url", "input_image", "image"):
                    url = part.get("image_url") or part.get("url") or part.get("image")
                    if isinstance(url, dict):
                        url = url.get("url")
                    chunks.append(f"[image: {url or 'inline'}]")
                elif ptype in ("audio", "input_audio"):
                    chunks.append("[audio]")
        return " ".join(c for c in chunks if c)
    if content is None:
        return ""
    return str(content)
