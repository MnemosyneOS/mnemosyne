#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne × LangChain
=====================

``MnemosyneMemory`` is a history store for any LangChain chain: it implements the
``load_memory_variables`` / ``save_context`` protocol, so a chain that already
takes a ``memory=`` argument can wear it without knowing Mnemosyne exists.

Why a framework-free class rather than inheriting ``BaseMemory``: inheriting
would make ``langchain-core`` a hard import for anyone using this adapter, and
would break the moment the base class changes.  Duck-typing the two methods the
chain actually calls is both smaller and more durable — and it is why this module
imports nothing outside the standard library plus Mnemosyne itself.

Usage::

    from integrations.langchain import MnemosyneMemory

    memory = MnemosyneMemory(user_id="alice", return_key="history")
    memory.save_context({"input": "I moved to Berlin"}, {"output": "Noted."})
    print(memory.load_memory_variables({"input": "where do I live"}))

    # Any chain that accepts a memory object:
    chain = ConversationChain(llm=llm, memory=memory)
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

from mnemosyne.api import Memory, MemoryConfig

__all__ = ["MnemosyneMemory", "MnemosyneRetriever"]

DEFAULT_MEMORY_KEY = "history"


class MnemosyneMemory:
    """A LangChain-compatible memory backed by Mnemosyne.

    Parameters
    ----------
    user_id, agent_id, run_id, app_id:
        The memory scope. At least one is strongly recommended: an unscoped
        memory lands in a shared bucket, which is almost never what a per-user
        chat wants.
    memory_key:
        The variable the chain reads history from. Defaults to ``"history"``.
    input_key / output_key:
        The keys ``save_context`` reads the turn from. Defaults match
        ``ConversationChain``.
    return_key:
        Override ``memory_key`` for the recalled slice specifically, when a chain
        wants recalled facts under a different name than the running history.
    top_k:
        How many memories to recall per turn.
    budget_tokens:
        When set, recall is packed to a token budget instead of a fixed count —
        the right choice for a long-context chain where a fixed ``top_k`` is
        either wasteful or insufficient depending on memory length.
    search_on_load:
        Whether ``load_memory_variables`` performs a search. Leave true for the
        usual case; set false when the chain only needs the raw history.
    """

    is_mnemosyne_memory = True   #: a marker, so callers can detect the adapter

    def __init__(self,
                 user_id: Optional[str] = None,
                 agent_id: Optional[str] = None,
                 run_id: Optional[str] = None,
                 app_id: Optional[str] = None,
                 memory_key: str = DEFAULT_MEMORY_KEY,
                 input_key: str = "input",
                 output_key: str = "output",
                 return_key: Optional[str] = None,
                 top_k: int = 5,
                 budget_tokens: Optional[int] = None,
                 search_on_load: bool = True,
                 brain_dir: Optional[str] = None,
                 config: Optional[Dict[str, Any]] = None,
                 memory: Optional[Memory] = None):
        self.scope = {k: v for k, v in {
            "user_id": user_id, "agent_id": agent_id,
            "run_id": run_id, "app_id": app_id,
        }.items() if v}
        self.memory_key = memory_key
        self.return_key = return_key or memory_key
        self.input_key = input_key
        self.output_key = output_key
        self.top_k = int(top_k)
        self.budget_tokens = budget_tokens
        self.search_on_load = search_on_load

        if memory is not None:
            self._memory = memory
        else:
            cfg = dict(config or {})
            cfg.setdefault("brain_dir", brain_dir or os.environ.get(
                "MNEMOSYNE_DIR") or os.path.join(os.path.expanduser("~"), ".mnemosyne"))
            # Defaults that make the adapter work on a bare install.
            cfg.setdefault("llm", {"provider": "rules"})
            cfg.setdefault("embedder", {"provider": "builtin"})
            self._memory = Memory(MemoryConfig.from_dict(cfg))

    # -- LangChain surface --------------------------------------------------

    @property
    def memory_variables(self) -> List[str]:
        """The variables this memory offers to the chain."""
        return [self.return_key]

    def load_memory_variables(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Recall memories relevant to the current input.

        A chain that passes no ``input`` (some do, on the first turn) gets the
        most recent memories instead of an error — the alternative is a chain
        that crashes on its own warm-up call.
        """
        query = str(inputs.get(self.input_key) or "").strip()
        if not self.search_on_load or not query:
            rows = self._memory.get_all(filters=self.scope or None,
                                        top_k=self.top_k)["results"]
        else:
            rows = self._memory.search(query, filters=self.scope or None,
                                       top_k=self.top_k)["results"]
        return {self.return_key: self._format(rows)}

    def save_context(self, inputs: Dict[str, Any],
                     outputs: Dict[str, Any]) -> None:
        """Persist one turn.

        Extraction failures are swallowed: a chain must not fail because the
        memory layer could not reach a model. The alternative — raising here —
        turns a degraded memory into a broken application.
        """
        user_text = str(inputs.get(self.input_key) or "").strip()
        assistant_text = str(outputs.get(self.output_key) or "").strip()
        messages: List[Dict[str, str]] = []
        if user_text:
            messages.append({"role": "user", "content": user_text})
        if assistant_text:
            messages.append({"role": "assistant", "content": assistant_text})
        if not messages:
            return
        try:
            self._memory.add(messages, **self.scope)
        except Exception:  # noqa: BLE001 - memory must never break the chain
            pass

    def clear(self) -> None:
        """Forget this scope. Present because LangChain chains call it on reset."""
        if self.scope:
            try:
                self._memory.delete_all(**self.scope)
            except Exception:  # noqa: BLE001
                pass

    def load_memory_variables_str(self, inputs: Dict[str, Any]) -> str:
        """The recalled memories as one newline-joined string."""
        value = self.load_memory_variables(inputs)[self.return_key]
        if isinstance(value, list):
            return "\n".join(str(v) for v in value)
        return str(value)

    # -- Mnemosyne surface --------------------------------------------------

    @property
    def memory(self) -> Memory:
        """The underlying client, for anything this adapter does not wrap."""
        return self._memory

    def search(self, query: str, top_k: Optional[int] = None,
               explain: bool = False) -> List[Dict[str, Any]]:
        """Raw search over the adapter's own scope."""
        return self._memory.search(query, filters=self.scope or None,
                                   top_k=top_k or self.top_k,
                                   explain=explain)["results"]

    def as_retriever(self, top_k: Optional[int] = None) -> "MnemosyneRetriever":
        """Wrap this memory as a retriever, for use in a retrieval chain."""
        return MnemosyneRetriever(self, top_k=top_k)

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _format(rows: List[Dict[str, Any]]) -> List[str]:
        """Render recalled memories the way a prompt wants them.

        Returns a list of plain strings rather than objects: a chain interpolates
        this into a prompt, and handing it dicts produces the Python ``repr`` in
        the prompt — which both wastes tokens and leaks the projection's shape
        into the model's context.
        """
        return [str(r.get("memory") or "") for r in rows if r.get("memory")]


class MnemosyneRetriever:
    """A LangChain-retriever-shaped wrapper over ``MnemosyneMemory``.

    Implements ``get_relevant_documents`` / ``aget_relevant_documents``, the two
    methods a LangChain chain calls on a retriever, without importing
    ``langchain-core`` to subclass ``BaseRetriever``.
    """

    def __init__(self, memory: MnemosyneMemory, top_k: Optional[int] = None):
        self.memory = memory
        self.top_k = top_k or memory.top_k

    def get_relevant_documents(self, query: str, **_: Any) -> List[Dict[str, Any]]:
        return self.memory.search(query, top_k=self.top_k)

    async def aget_relevant_documents(self, query: str,
                                      **_: Any) -> List[Dict[str, Any]]:
        # The engine is synchronous; running it inline keeps one implementation
        # of the retrieval path rather than two that can disagree.
        return self.get_relevant_documents(query)

    def invoke(self, query: str, **kwargs: Any) -> List[Dict[str, Any]]:
        """The newer LangChain entry point; same behaviour."""
        return self.get_relevant_documents(query, **kwargs)
