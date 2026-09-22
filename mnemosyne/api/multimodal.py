#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — Multimodal ingestion
===================================

Images are handled with a rule that is stated once and applied everywhere:

**An image never becomes a memory on its own; it becomes an attachment of a
memory.** A bare picture with no caption is not something an agent can recall in
words, so storing it as a memory row produces a store full of unrecallable rows.
What *is* recallable is the description or the caption, so the pipeline attaches
the image reference to whichever text memory it accompanies and — when a
vision-capable model is configured — also stores a description so the image's
content is searchable by language rather than only by URL.

Two consequences worth being explicit about:

* With no vision model, an image-only turn is still **recorded**, as a memory
  whose text is the image reference and whose metadata marks
  ``modality: "image"``. Nothing is silently dropped; the caller can see exactly
  what was and was not understood.
* Nothing is downloaded. The reference is stored as given (an ``http(s)`` URL, a
  ``data:`` URI, or a local path kept verbatim). Fetching a remote asset during a
  memory write would turn a text operation into a network operation with an
  unbounded failure mode.

The parser accepts the shapes the ecosystem uses in practice: the OpenAI
multi-part content list (``{"type": "image_url", "image_url": {"url": ...}}``),
Anthropic's (``{"type": "image", "source": {...}}``), Gemini's inline parts
(``{"type": "image", "data": ...}``) and the shorthand
``{"type": "input_image", "image_url": "..."}``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["ImageRef", "MultimodalBatch", "parse_messages", "image_placeholder",
           "describe_image", "looks_like_image_ref"]

_IMAGE_TYPES = frozenset({"image", "image_url", "input_image", "input_image_url",
                          "image_base64", "audio", "input_audio"})

_DATA_URI = re.compile(r"^data:(?P<mime>[\w./+-]+);base64,(?P<data>.*)$", re.DOTALL)
_URLISH = re.compile(r"^(?:https?://|file://|data:)", re.I)
_IMAGE_EXT = re.compile(r"\.(?:png|jpe?g|gif|webp|bmp|tiff?|svg|heic|avif)(?:$|\?)", re.I)

#: Longest reference copied verbatim into a memory's text. Beyond this the value
#: is almost certainly a payload rather than a locator.
_MAX_TEXT_URL = 300


class ImageRef(dict):
    """One non-text attachment.

    A ``dict`` subclass rather than a dataclass because it is serialised straight
    into memory metadata, where every consumer already expects plain JSON.
    """

    @property
    def url(self) -> str:
        return str(self.get("url") or "")

    @property
    def kind(self) -> str:
        """``"image"`` or ``"audio"`` — what the attachment actually is."""
        return self.get("kind") or "image"

    @property
    def mime(self) -> Optional[str]:
        return self.get("mime")

    @property
    def inline(self) -> bool:
        """True when the payload is carried inline (a ``data:`` URI or base64)."""
        return bool(self.get("inline"))

    def to_metadata(self) -> Dict[str, Any]:
        """The form stored in a memory's ``attachments`` list.

        Inline payloads are described by length and MIME only. Copying a
        multi-megabyte base64 blob into every memory's metadata would inflate the
        store and, worse, put image bytes into the full-text index.
        """
        out: Dict[str, Any] = {"kind": self.kind}
        if self.url:
            out["url"] = (f"inline:{self.mime or 'binary'};{len(self.url)}ch"
                          if self.inline else self.url)
        if self.mime:
            out["mime"] = self.mime
        if self.get("detail"):
            out["detail"] = self["detail"]
        return out


class MultimodalBatch:
    """The result of parsing a message list that may contain attachments."""

    def __init__(self) -> None:
        self.text_messages: List[Dict[str, str]] = []
        self.images: List[ImageRef] = []
        self.audio: List[ImageRef] = []

    @property
    def has_attachments(self) -> bool:
        return bool(self.images or self.audio)

    @property
    def text_only(self) -> List[Dict[str, str]]:
        """The text turns, safe to hand to a text-only extractor."""
        return list(self.text_messages)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_messages(messages: List[Any]) -> MultimodalBatch:
    """Split a message list into text turns and attachments.

    Modal parts that carry no text are removed from the text stream and recorded
    as attachments; the text stream keeps a lightweight placeholder
    (``[image: …]``) so the extractor still sees that something visual was
    present in that turn, and so a sentence referring to "the screenshot above"
    is not read as a dangling reference.
    """
    batch = MultimodalBatch()
    for message in messages or []:
        if not isinstance(message, dict):
            if isinstance(message, str) and message.strip():
                batch.text_messages.append({"role": "user", "content": message})
            continue

        role = str(message.get("role") or "user")
        content = message.get("content")

        if isinstance(content, str):
            if content.strip():
                batch.text_messages.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            continue

        texts: List[str] = []
        placeholders: List[str] = []
        for part in content:
            if isinstance(part, str):
                if part.strip():
                    texts.append(part)
                continue
            if not isinstance(part, dict):
                continue

            ptype = str(part.get("type") or "").lower()
            if ptype in ("text", "input_text", "") and part.get("text"):
                texts.append(str(part["text"]))
                continue

            ref = _image_from_part(part)
            if ref is None:
                continue
            if ref.kind == "audio":
                batch.audio.append(ref)
            else:
                batch.images.append(ref)
            placeholders.append(image_placeholder(ref))

        merged = " ".join(texts + placeholders).strip()
        if merged:
            batch.text_messages.append({"role": role, "content": merged})
    return batch


def _image_from_part(part: Dict[str, Any]) -> Optional[ImageRef]:
    """Extract an :class:`ImageRef` from one content part, or ``None``."""
    ptype = str(part.get("type") or "").lower()
    if ptype not in _IMAGE_TYPES:
        # A bare {"url": "..."} or {"data": "..."} is accepted too: that is what a
        # hand-written payload usually looks like before someone reads the spec.
        if not any(k in part for k in ("image_url", "url", "data", "base64")):
            return None

    ref = ImageRef(kind="audio" if "audio" in ptype else "image")
    ref["type"] = ptype or "image"

    candidate = part.get("image_url")
    if isinstance(candidate, dict):
        ref["url"] = str(candidate.get("url") or "")
        if candidate.get("detail"):
            ref["detail"] = str(candidate["detail"])
    elif isinstance(candidate, str):
        ref["url"] = candidate

    if not ref.get("url"):
        source = part.get("source")
        if isinstance(source, dict):
            # Anthropic: {"source": {"type": "base64", "media_type": ..., "data": ...}}
            if source.get("data"):
                mime = source.get("media_type") or "image/png"
                ref["url"] = f"data:{mime};base64,{source['data']}"
                ref["mime"] = mime
            elif source.get("url"):
                ref["url"] = str(source["url"])
        elif isinstance(source, str) and source:
            ref["url"] = source

    # Gemini spells an inline image as {"inlineData": {"mimeType":…, "data":…}}
    # and a remote one as {"fileData": {"fileUri":…}}; handle those shapes before
    # the flat-key fallback so the payload is recognised as inline.
    inline_data = part.get("inlineData") or part.get("inline_data")
    if isinstance(inline_data, dict) and not ref.get("url"):
        mime = inline_data.get("mimeType") or inline_data.get("mime_type") or "image/png"
        payload = inline_data.get("data")
        if isinstance(payload, str) and payload:
            ref["url"] = f"data:{mime};base64,{payload}"
            ref["inline"] = True
            ref["mime"] = mime
    file_data = part.get("fileData") or part.get("file_data")
    if isinstance(file_data, dict) and not ref.get("url"):
        uri = file_data.get("fileUri") or file_data.get("file_uri")
        if isinstance(uri, str) and uri:
            ref["url"] = uri

    for key in ("url", "data", "base64", "b64", "file_uri", "gcs_uri"):
        if ref.get("url"):
            break
        value = part.get(key)
        if not isinstance(value, str) or not value:
            continue
        if key in ("data", "base64", "b64"):
            # A bare base64 string under `data`/`base64` is a payload, not a
            # reference. Wrapping it in a data: URI is what makes the inline branch
            # recognise it — otherwise the raw bytes were treated as a URL and
            # copied verbatim into the memory's text and index.
            mime = (part.get("mime_type") or part.get("media_type")
                    or ("image/png" if ref.kind == "image" else "audio/wav"))
            ref["url"] = f"data:{mime};base64,{value}"
            ref["inline"] = True
            ref["mime"] = mime
        else:
            ref["url"] = value

    if not ref.get("url"):
        return None

    match = _DATA_URI.match(ref.url)
    if match:
        ref["inline"] = True
        ref["mime"] = ref.get("mime") or match.group("mime")
    if part.get("mime_type"):
        ref["mime"] = str(part["mime_type"])
    return ref


def image_placeholder(ref: ImageRef) -> str:
    """A short, indexable stand-in for an attachment in the text stream.

    Deliberately short and URL-free for inline payloads: putting a base64 blob
    into the text would blow up token counts, the full-text index and every
    downstream prompt.
    """
    if ref.inline:
        return f"[{ref.kind}: inline {ref.mime or 'binary'}]"
    return f"[{ref.kind}: {ref.url[:200]}]"


def looks_like_image_ref(value: str) -> bool:
    """Whether *value* is plausibly an image reference rather than prose."""
    if not isinstance(value, str) or not value:
        return False
    if _DATA_URI.match(value):
        return True
    if _URLISH.match(value):
        return bool(_IMAGE_EXT.search(value))
    return bool(_IMAGE_EXT.search(value))


# ---------------------------------------------------------------------------
# Description
# ---------------------------------------------------------------------------

DEFAULT_VISION_PROMPT = (
    "Describe this image as a memory would: one or two sentences, concrete and "
    "self-contained. Include any text that appears in it verbatim, and any "
    "identifiable people, places, products or numbers. Do not speculate about "
    "anything not visible. Do not mention that you are describing an image."
)


def describe_image(llm: Any, ref: ImageRef,
                   prompt: Optional[str] = None) -> Optional[str]:
    """Return a textual description of *ref*, or ``None`` if unavailable.

    Returns ``None`` rather than raising for every failure mode — no vision
    capability, an unreachable URL, a model refusal. A caller that cannot get a
    description still stores the reference, so the memory is degraded rather than
    lost, which is the same contract the rest of the engine follows.
    """
    if llm is None or not getattr(llm, "supports_vision", False):
        return None
    if ref.inline:
        # Inline payloads are not uploaded anywhere; describing them would require
        # forwarding megabytes to a vendor. Left to an explicit opt-in instead.
        return None
    try:
        text = llm.describe_image(prompt or DEFAULT_VISION_PROMPT, ref.url)
    except Exception:  # noqa: BLE001 - description is best-effort by contract
        return None
    text = (text or "").strip()
    return text or None


def attach_to_metadata(metadata: Dict[str, Any],
                       refs: List[ImageRef]) -> Dict[str, Any]:
    """Record attachments and the modality flag on a memory's metadata."""
    if not refs:
        return metadata
    existing = metadata.get("attachments")
    merged: List[Dict[str, Any]] = list(existing) if isinstance(existing, list) else []
    merged.extend(r.to_metadata() for r in refs)
    metadata["attachments"] = merged
    kinds = sorted({r.kind for r in refs})
    if len(kinds) == 1:
        metadata["modality"] = kinds[0]
    else:
        metadata["modality"] = "multimodal"
    return metadata


def image_fact_text(ref: ImageRef, description: Optional[str]) -> str:
    """The text a stored image memory carries.

    With a description, the memory reads as the description and the reference
    lives in metadata — so the memory is searchable by meaning. Without one, the
    text is the reference itself, which keeps the row identifiable and lets a
    later pass with a vision model enrich it.
    """
    if description:
        return description
    if ref.inline:
        return f"[{ref.kind} attachment: inline {ref.mime or 'binary'}]"
    # Defensive cap. A reference long enough to be an unrecognised payload (an
    # exotic base64 shape, a signed URL with a huge token) must not be copied into
    # the memory text: it would dominate the token count and the full-text index,
    # and it would not be recallable anyway.
    url = ref.url
    if len(url) > _MAX_TEXT_URL:
        return f"[{ref.kind} attachment: {ref.mime or 'binary'} payload, {len(url)} chars]"
    return f"[{ref.kind} attachment: {url}]"
