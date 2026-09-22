#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — Standard-library HTTP transport
==============================================

A single, tiny HTTP client used by every provider adapter that speaks to a
remote model or database API.  Using :mod:`urllib.request` instead of a vendor
SDK is a deliberate architectural choice:

* it keeps the dependency-free install true — an OpenAI or Ollama user needs no
  ``pip install`` beyond Mnemosyne itself;
* the *wire format* is the durable interface, so an adapter never rots when a
  vendor ships a breaking SDK release;
* one transport means one retry policy, one timeout policy and one place to
  redact credentials from error messages.

Only JSON in / JSON out and server-sent-event (SSE) streaming are supported;
that covers every provider currently registered.
"""

from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, List, Optional

__all__ = ["HttpError", "request_json", "request_stream", "redact"]

_DEFAULT_TIMEOUT = 60.0
_DEFAULT_RETRIES = 3
_RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class HttpError(RuntimeError):
    """A non-2xx response, or a transport-level failure.

    The message always has credentials stripped by :func:`redact` before it is
    raised, so an exception can be logged or shown to a user without leaking a
    key.
    """

    def __init__(self, status: int, url: str, body: str = "", reason: str = ""):
        self.status = status
        self.url = redact(url)
        self.body = redact(body)[:2000]
        self.reason = reason or self.body
        detail = f"HTTP {status} from {self.url}"
        if self.body:
            detail += f": {self.body}"
        super().__init__(detail)

    @property
    def retryable(self) -> bool:
        return self.status in _RETRY_STATUS or self.status >= 500


_SECRET_HEADERS = ("authorization", "x-api-key", "api-key", "x-goog-api-key",
                   "cookie", "proxy-authorization")


def redact(text: str) -> str:
    """Strip anything that looks like a credential out of *text*.

    Applied to every URL and response body that leaves this module, so a key
    echoed back by an upstream service cannot end up in a log line.
    """
    if not text:
        return text
    import re

    out = str(text)
    # scheme://user:pass@host  ->  scheme://***@host
    out = re.sub(r"(//)[^/@\s:]+:[^/@\s]+@", r"\1***@", out)
    # bearer tokens / long opaque keys in prose or JSON
    out = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}", r"\1***", out)
    out = re.sub(r"(?i)((?:api[_-]?key|token|secret|password)\"?\s*[:=]\s*\"?)"
                 r"[A-Za-z0-9._\-]{12,}", r"\1***", out)
    out = re.sub(r"\b(sk|pk|ghp|gho|ghs|xox[baprs])-[A-Za-z0-9]{12,}", r"\1-***", out)
    out = re.sub(r"\bm0-[A-Za-z0-9]{12,}", "m0-***", out)
    return out


def _ssl_context(insecure: bool = False) -> ssl.SSLContext:
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context()


def request_json(
    url: str,
    *,
    method: str = "POST",
    payload: Optional[Any] = None,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = _DEFAULT_TIMEOUT,
    retries: int = _DEFAULT_RETRIES,
    insecure: bool = False,
    auth: Optional[tuple] = None,
) -> Any:
    """Perform one JSON request and return the decoded response body.

    Retries idempotent failures (connection resets, 429 and 5xx) with
    exponential backoff plus jitter.  ``Retry-After`` is honoured when the
    upstream supplies it.

    Returns ``None`` for an empty body (e.g. HTTP 204).
    """
    if params:
        qs = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        )
        url = f"{url}{'&' if '?' in url else '?'}{qs}"

    hdrs: Dict[str, str] = {"Accept": "application/json"}
    hdrs.update(headers or {})

    data: Optional[bytes] = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")

    if auth:
        import base64

        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        hdrs.setdefault("Authorization", f"Basic {token}")

    ctx = _ssl_context(insecure)
    last_exc: Optional[Exception] = None

    for attempt in range(max(1, retries)):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                body = resp.read()
                if not body:
                    return None
                try:
                    return json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return body.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raw = ""
            try:
                raw = e.read().decode("utf-8", errors="replace")
            except Exception:  # pragma: no cover - body already consumed
                pass
            err = HttpError(e.code, url, raw, reason=getattr(e, "reason", ""))
            if not err.retryable or attempt == retries - 1:
                raise err from None
            last_exc = err
            delay = _retry_delay(attempt, e.headers.get("Retry-After"))
            time.sleep(delay)
        except (urllib.error.URLError, socket.timeout, ConnectionResetError,
                ssl.SSLError, TimeoutError) as e:
            last_exc = e
            if attempt == retries - 1:
                raise HttpError(0, url, str(e), reason="transport failure") from None
            time.sleep(_retry_delay(attempt, None))

    raise HttpError(0, url, str(last_exc), reason="retries exhausted")


def _retry_delay(attempt: int, retry_after: Optional[str]) -> float:
    """Backoff in seconds: honour Retry-After, else 0.5 * 2^n with jitter."""
    import random

    if retry_after:
        try:
            return min(30.0, max(0.0, float(retry_after)))
        except (TypeError, ValueError):
            pass
    return min(20.0, 0.5 * (2 ** attempt)) * (0.7 + 0.6 * random.random())


def request_stream(
    url: str,
    *,
    payload: Optional[Any] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = _DEFAULT_TIMEOUT,
    insecure: bool = False,
) -> Iterator[str]:
    """Yield ``data:`` payloads from a server-sent-event response.

    Every provider that supports token streaming does so with SSE, so this one
    parser serves all of them.  Yields the raw string after ``data:`` with the
    terminating ``[DONE]`` sentinel filtered out.
    """
    hdrs: Dict[str, str] = {"Accept": "text/event-stream"}
    hdrs.update(headers or {})
    data: Optional[bytes] = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout,
                                context=_ssl_context(insecure)) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk in ("[DONE]", ""):
                continue
            yield chunk


def split_sse_json(chunk: str) -> Optional[Dict[str, Any]]:
    """Decode one SSE payload, returning ``None`` when it is not JSON."""
    try:
        val = json.loads(chunk)
    except json.JSONDecodeError:
        return None
    return val if isinstance(val, dict) else None


def extract_text(payload: Dict[str, Any]) -> str:
    """Pull assistant text out of a streaming delta from any provider.

    Handles the OpenAI/Anthropic/Gemini delta shapes, which differ only in where
    the token lives.
    """
    if not isinstance(payload, dict):
        return ""
    # OpenAI-compatible: choices[0].delta.content
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        c0 = choices[0] or {}
        delta = c0.get("delta") or {}
        if isinstance(delta, dict):
            content = delta.get("content")
            if isinstance(content, str):
                return content
        msg = c0.get("message") or {}
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            return msg["content"]
    # Anthropic: delta.text
    delta = payload.get("delta")
    if isinstance(delta, dict) and isinstance(delta.get("text"), str):
        return delta["text"]
    # Gemini: candidates[0].content.parts[*].text
    candidates = payload.get("candidates")
    if isinstance(candidates, list) and candidates:
        parts = ((candidates[0] or {}).get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    return ""
