#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — Vision request shapes
====================================

Image input is the one place where the ecosystem has *not* converged, so this
module holds the three wire formats in one place and dispatches by provider name.

Keeping it separate from :mod:`mnemosyne.providers.llms` has a second benefit: the
capability flag and the request builder live together, so adding a vision-capable
vendor is one entry in :data:`VISION_PROVIDERS` plus, at most, one new builder —
rather than an edit scattered across the adapter class.

The three families:

``openai``
    ``content`` becomes a list of typed parts, with the image as
    ``{"type": "image_url", "image_url": {"url": ...}}``.  Shared by every
    OpenAI-compatible vendor.
``anthropic``
    The image is a top-level block with an explicit ``source``: a URL for remote
    images, base64 for inline ones.
``gemini``
    ``parts`` contains ``inlineData`` for bytes or ``fileData`` for a URI.

Every builder accepts both an ``http(s)`` URL and a ``data:`` URI, because the
multimodal parser produces both and a vendor that only handled one would fail on
exactly the payload type the caller was least likely to test.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .base import ProviderUnavailable
from .transport import HttpError, request_json

__all__ = ["VISION_PROVIDERS", "supports_vision", "describe", "split_data_uri"]

#: Providers whose upstream API accepts image input.
VISION_PROVIDERS = frozenset({
    # OpenAI-compatible
    "openai", "openai_structured", "azure_openai", "azure_openai_structured",
    "groq", "together", "deepseek", "xai", "minimax", "sarvam", "openrouter",
    "litellm", "lmstudio", "vllm",
    # Native shapes
    "anthropic", "gemini", "vertexai_llm", "ollama",
})

_DATA_URI = re.compile(r"^data:(?P<mime>[\w./+-]+);base64,(?P<data>.*)$", re.DOTALL)

#: Providers whose image part is Anthropic-shaped.
_ANTHROPIC_STYLE = frozenset({"anthropic"})
#: Providers whose image part is Gemini-shaped.
_GEMINI_STYLE = frozenset({"gemini", "vertexai_llm"})
#: Providers whose image part is Ollama-shaped.
_OLLAMA_STYLE = frozenset({"ollama"})


def supports_vision(provider_name: str) -> bool:
    """Whether *provider_name* can accept image input."""
    return str(provider_name or "").lower() in VISION_PROVIDERS


def split_data_uri(value: str) -> Optional[Tuple[str, str]]:
    """Split a ``data:`` URI into ``(mime, base64_payload)``, else ``None``."""
    if not isinstance(value, str):
        return None
    match = _DATA_URI.match(value)
    return (match.group("mime"), match.group("data")) if match else None


def describe(llm: Any, prompt: str, image_url: str,
             max_tokens: int = 400) -> str:
    """Send one image + prompt to *llm* and return the description.

    Raises :class:`ProviderUnavailable` for a provider with no vision support, so
    the caller degrades to storing the reference alone.  Anything the upstream
    rejects surfaces as an :class:`~mnemosyne.providers.transport.HttpError`, which
    the multimodal layer also treats as "no description available" — a vendor
    outage must not fail a memory write.
    """
    name = str(getattr(llm, "name", "") or "").lower()
    if not supports_vision(name):
        raise ProviderUnavailable(
            name or "unknown", "no vision support",
            hint="Configure a vision-capable LLM provider to describe images.",
        )
    if not image_url:
        raise ProviderUnavailable(name, "empty image reference")

    if name in _ANTHROPIC_STYLE:
        return _describe_anthropic(llm, prompt, image_url, max_tokens)
    if name in _GEMINI_STYLE:
        return _describe_gemini(llm, prompt, image_url, max_tokens)
    if name in _OLLAMA_STYLE:
        return _describe_ollama(llm, prompt, image_url, max_tokens)
    return _describe_openai(llm, prompt, image_url, max_tokens)


def _describe_openai(llm: Any, prompt: str, image_url: str, max_tokens: int) -> str:
    from .llms import _openai_text

    body: Dict[str, Any] = {
        "model": getattr(llm, "model", "") or "",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        }],
        "max_tokens": max_tokens,
    }
    payload = request_json(
        f"{llm.base_url}/chat/completions", payload=body,
        headers=llm._headers,                      # noqa: SLF001 - adapter-internal
        params=({"api-version": llm.api_version} if hasattr(llm, "api_version") else None),
        timeout=getattr(llm, "timeout", 60),
    )
    return _openai_text(payload)


def _describe_anthropic(llm: Any, prompt: str, image_url: str, max_tokens: int) -> str:
    inline = split_data_uri(image_url)
    if inline:
        block: Dict[str, Any] = {"type": "image", "source": {
            "type": "base64", "media_type": inline[0], "data": inline[1]}}
    else:
        block = {"type": "image", "source": {"type": "url", "url": image_url}}

    body = {
        "model": getattr(llm, "model", ""),
        "max_tokens": max_tokens,
        "messages": [{"role": "user",
                      "content": [block, {"type": "text", "text": prompt}]}],
    }
    headers = {
        "x-api-key": getattr(llm, "api_key", ""),
        "anthropic-version": str(getattr(llm, "anthropic_version", None)
                                 or "2023-06-01"),
    }
    payload = request_json(f"{llm.base_url}/messages", payload=body, headers=headers,
                           timeout=getattr(llm, "timeout", 60))
    if not isinstance(payload, dict):
        return ""
    return "".join(b.get("text", "") for b in (payload.get("content") or [])
                   if isinstance(b, dict) and b.get("type") == "text")


def _describe_gemini(llm: Any, prompt: str, image_url: str, max_tokens: int) -> str:
    inline = split_data_uri(image_url)
    if inline:
        part: Dict[str, Any] = {"inlineData": {"mimeType": inline[0], "data": inline[1]}}
    else:
        part = {"fileData": {"fileUri": image_url}}

    body = {"contents": [{"role": "user", "parts": [part, {"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens}}
    payload = request_json(
        f"{llm.base_url}/models/{getattr(llm, 'model', '')}:generateContent",
        payload=body, headers={"x-goog-api-key": getattr(llm, "api_key", "")},
        timeout=getattr(llm, "timeout", 60),
    )
    if not isinstance(payload, dict):
        return ""
    candidates = payload.get("candidates") or []
    if not candidates:
        return ""
    parts = ((candidates[0] or {}).get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


def _describe_ollama(llm: Any, prompt: str, image_url: str, max_tokens: int) -> str:
    """Ollama takes raw base64 in an ``images`` list, and only for local files."""
    inline = split_data_uri(image_url)
    if not inline:
        raise ProviderUnavailable(
            "ollama", "Ollama's vision API takes inline base64, not remote URLs",
            hint="Pass the image as a data: URI, or use a hosted vision provider.",
        )
    body = {
        "model": getattr(llm, "model", ""),
        "messages": [{"role": "user", "content": prompt, "images": [inline[1]]}],
        "stream": False,
        "options": {"num_predict": max_tokens},
    }
    payload = request_json(f"{llm.base_url}/api/chat", payload=body,
                           timeout=getattr(llm, "timeout", 120))
    if isinstance(payload, dict):
        return ((payload.get("message") or {}).get("content") or "")
    return ""
