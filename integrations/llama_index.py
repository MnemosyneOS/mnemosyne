#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne × LlamaIndex
=======================

Two bridges, because LlamaIndex uses memory in two different places:

``MnemosyneChatStore``
    Durable message storage for a ``ChatMemoryBuffer``.  Use it when you want
    LlamaIndex's own memory semantics (a rolling buffer, a token limit) but the
    messages themselves to survive a restart.

``MnemosyneRetriever``
    A retriever over Mnemosyne's own recall, for a retrieval-augmented query
    engine.  Use it when you want Mnemosyne's multi-signal fusion and relevance
    calibration rather than a plain vector index.

Both are duck-typed rather than subclassing LlamaIndex base classes, so this
module imports nothing outside Mnemosyne and the standard library — which keeps a
``llama-index`` version bump from breaking the memory layer.

Usage::

    from integrations.llama_index import MnemosyneChatStore, MnemosyneRetriever

    store = MnemosyneChatStore(user_id="alice")
    store.add_message({"role": "user", "content": "I moved to Berlin"})

    retriever = MnemosyneRetriever(user_id="alice")
    nodes = retriever.retrieve("where does the user live")
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from mnemosyne.api import Memory, MemoryConfig

__all__ = ["MnemosyneChatStore", "MnemosyneRetriever", "MnemosyneNodeWithScore"]


def _make_memory(user_id: Optional[str], agent_id: Optional[str],
                 brain_dir: Optional[str],
                 config: Optional[Dict[str, Any]],
                 memory: Optional[Memory]) -> Memory:
    if memory is not None:
        return memory
    cfg = dict(config or {})
    cfg.setdefault("brain_dir", brain_dir or os.environ.get("MNEMOSYNE_DIR")
                   or os.path.join(os.path.expanduser("~"), ".mnemosyne"))
    cfg.setdefault("llm", {"provider": "rules"})
    cfg.setdefault("embedder", {"provider": "builtin"})
    return Memory(MemoryConfig.from_dict(cfg))


class MnemosyneNodeWithScore(dict):
    """A duck-typed ``NodeWithScore``: ``.text`` and ``.score``.

    A dict subclass with properties rather than a dataclass because LlamaIndex
    reads both attributes and, in some code paths, indexes the object.  Supporting
    both costs three lines and avoids a class of "works in my chain, not yours"
    reports.
    """

    def __init__(self, text: str, score: float, metadata: Optional[Dict[str, Any]] = None):
        super().__init__(text=text, score=score, metadata=metadata or {})

    @property
    def text(self) -> str:
        return self.get("text", "")

    @property
    def score(self) -> float:
        return float(self.get("score") or 0.0)

    @property
    def metadata(self) -> Dict[str, Any]:
        return self.get("metadata") or {}

    def get_text(self) -> str:
        return self.text


class MnemosyneChatStore:
    """Durable message storage for LlamaIndex chat memory.

    Implements the ``add_message`` / ``get_messages`` / ``set_messages`` /
    ``delete_messages`` quartet.  Messages are stored verbatim
    (``infer=False``): LlamaIndex's buffer already owns the "what to keep"
    decision, and running extraction underneath it would silently replace
    messages with derived facts that the buffer's own token accounting does not
    know about.
    """

    def __init__(self, user_id: Optional[str] = None,
                 agent_id: Optional[str] = None,
                 run_id: Optional[str] = None,
                 app_id: Optional[str] = None,
                 session_key: str = "llamaindex",
                 brain_dir: Optional[str] = None,
                 config: Optional[Dict[str, Any]] = None,
                 memory: Optional[Memory] = None):
        self.scope = {k: v for k, v in {
            "user_id": user_id, "agent_id": agent_id,
            "run_id": run_id, "app_id": app_id}.items() if v}
        self.session_key = session_key
        self._memory = _make_memory(user_id, agent_id, brain_dir, config, memory)
        self._session_id: Optional[str] = None

    @property
    def memory(self) -> Memory:
        return self._memory

    def set_session_id(self, session_id: str) -> None:
        """Bind this store to one conversation.

        The session id becomes the memory's ``session_id`` field *and* the
        ``run_id`` scope, so two concurrent conversations in one process cannot
        read each other's messages.
        """
        self._session_id = str(session_id)

    def add_message(self, message: Dict[str, Any]) -> None:
        text = str(message.get("content") or "").strip()
        if not text:
            return
        role = str(message.get("role") or "user")
        scope = dict(self.scope)
        if self._session_id:
            scope.setdefault("run_id", self._session_id)
        try:
            self._memory.add([{"role": role, "content": text}],
                             infer=False, metadata={"session_id": self._session_id},
                             **scope)
        except Exception:  # noqa: BLE001 - a chat store must not break the chat
            pass

    def add_messages(self, messages: List[Dict[str, Any]]) -> None:
        for message in messages:
            self.add_message(message)

    def get_messages(self, **_: Any) -> List[Dict[str, Any]]:
        """Return the session's messages, oldest first."""
        filters: Dict[str, Any] = dict(self.scope)
        if self._session_id:
            filters["run_id"] = self._session_id
        rows = self._memory.get_all(filters=filters or None, top_k=1000)["results"]
        rows.reverse()   # get_all is newest-first; LlamaIndex wants chronological
        out: List[Dict[str, Any]] = []
        for row in rows:
            meta = row.get("metadata") or {}
            text = row.get("memory") or ""
            # Verbatim rows carry the speaker in metadata when the extractor saw
            # an assistant turn; otherwise a stored message is a user statement.
            role = "assistant" if meta.get("speaker") == "agent" else "user"
            out.append({"role": role, "content": text, "id": row.get("id")})
        return out

    def set_messages(self, messages: List[Dict[str, Any]]) -> None:
        """Replace the session's messages."""
        self.delete_messages()
        self.add_messages(messages)

    def delete_messages(self, **_: Any) -> None:
        filters: Dict[str, Any] = dict(self.scope)
        if self._session_id:
            filters["run_id"] = self._session_id
        if not filters:
            return
        try:
            self._memory.delete_all(**filters)
        except Exception:  # noqa: BLE001
            pass

    def reset(self) -> None:
        self.delete_messages()


class MnemosyneRetriever:
    """A LlamaIndex-shaped retriever over Mnemosyne recall.

    ``retrieve`` is synchronous and ``aretrieve`` delegates to it: the engine is
    synchronous, and having one implementation means the async and sync paths
    cannot return different results for the same query.
    """

    def __init__(self, user_id: Optional[str] = None,
                 agent_id: Optional[str] = None,
                 run_id: Optional[str] = None,
                 app_id: Optional[str] = None,
                 top_k: int = 5,
                 threshold: float = 0.1,
                 rerank: bool = False,
                 brain_dir: Optional[str] = None,
                 config: Optional[Dict[str, Any]] = None,
                 memory: Optional[Memory] = None):
        self.scope = {k: v for k, v in {
            "user_id": user_id, "agent_id": agent_id,
            "run_id": run_id, "app_id": app_id}.items() if v}
        self.top_k = int(top_k)
        self.threshold = float(threshold)
        self.rerank = bool(rerank)
        self._memory = _make_memory(user_id, agent_id, brain_dir, config, memory)

    @property
    def memory(self) -> Memory:
        return self._memory

    def retrieve(self, query: str, **_: Any) -> List[MnemosyneNodeWithScore]:
        result = self._memory.search(query, filters=self.scope or None,
                                     top_k=self.top_k, threshold=self.threshold,
                                     rerank=self.rerank)["results"]
        return [MnemosyneNodeWithScore(
            text=str(r.get("memory") or ""),
            score=float(r.get("score") or 0.0),
            metadata=dict(r.get("metadata") or {}, id=r.get("id")),
        ) for r in result]

    async def aretrieve(self, query: str, **kwargs: Any) -> List[MnemosyneNodeWithScore]:
        return self.retrieve(query, **kwargs)

    def _retrieve(self, query: str, **kwargs: Any) -> List[MnemosyneNodeWithScore]:
        """The private entry point older LlamaIndex versions call."""
        return self.retrieve(query, **kwargs)
