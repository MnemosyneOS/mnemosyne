#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — ``AsyncMemory``
==============================

An ``await``-friendly face over :class:`~mnemosyne.api.memory.Memory`.

The wrapping strategy is deliberately the simplest one that is actually correct:
each call is dispatched to a worker thread and awaited.  The alternative — an
``async`` driver for every provider — would mean two implementations of the
extraction, filtering and scoring paths, and two implementations of a retrieval
algorithm is how the same query starts returning different answers depending on
which API the caller happened to use.

Thread dispatch is safe here because the engine is synchronous *and* thread-safe:
the store serialises writes, and each scope's context is guarded by its own lock.
That is also why two concurrent ``await memory.add(...)`` calls cannot interleave
and corrupt a record.
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any, Dict, List, Optional

from .config import MemoryConfig
from .memory import Memory

__all__ = ["AsyncMemory"]


class AsyncMemory:
    """Async wrapper around :class:`Memory` — identical surface, ``await``-able.

    Also usable as an async context manager::

        async with AsyncMemory.from_config(cfg) as memory:
            await memory.add("I prefer dark mode", user_id="alice")
    """

    def __init__(self, config: Optional[MemoryConfig] = None):
        self._sync = config if isinstance(config, Memory) else Memory(config)
        self._executor = None

    @classmethod
    def from_config(cls, config_dict: Optional[Dict[str, Any]] = None) -> "AsyncMemory":
        return cls(MemoryConfig.from_dict(config_dict))

    # -- plumbing -----------------------------------------------------------

    def _executor_or_default(self):
        if self._executor is None:
            # A bounded pool, because the work is SD-card and network bound, not
            # CPU bound. An unbounded default would let a burst of add() calls
            # open as many SQLite writers as there are coroutines and start
            # failing on lock contention under exactly the load it was meant to
            # absorb.
            self._executor = None  # asyncio's default pool is already bounded
        return self._executor

    async def _run(self, fn_name: str, *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        fn = functools.partial(getattr(self._sync, fn_name), *args, **kwargs)
        return await loop.run_in_executor(self._executor, fn)

    # -- compatible surface -------------------------------------------------

    async def add(self, messages: Any, **kwargs: Any) -> Dict[str, Any]:
        """Async :meth:`Memory.add`."""
        if kwargs.pop("async_mode", False):
            # Already non-blocking by construction: the sync layer hands the work
            # to its own worker and returns an event id immediately, so awaiting
            # here would be pure overhead.
            kwargs["async_mode"] = True
        return await self._run("add", messages, **kwargs)

    async def get(self, memory_id: str) -> Dict[str, Any]:
        return await self._run("get", memory_id)

    async def get_all(self, **kwargs: Any) -> Dict[str, Any]:
        return await self._run("get_all", **kwargs)

    async def search(self, query: str, **kwargs: Any) -> Dict[str, Any]:
        return await self._run("search", query, **kwargs)

    async def update(self, memory_id: str, **kwargs: Any) -> Dict[str, Any]:
        return await self._run("update", memory_id, **kwargs)

    async def delete(self, memory_id: str) -> Dict[str, Any]:
        return await self._run("delete", memory_id)

    async def delete_all(self, **kwargs: Any) -> Dict[str, Any]:
        return await self._run("delete_all", **kwargs)

    async def history(self, memory_id: str) -> Dict[str, Any]:
        return await self._run("history", memory_id)

    async def reset(self) -> None:
        return await self._run("reset")

    async def close(self) -> None:
        return await self._run("close")

    # -- graph / entities / events -----------------------------------------

    async def graph_add(self, data: str, **kwargs: Any) -> Dict[str, Any]:
        return await self._run("graph_add", data, **kwargs)

    async def graph_search(self, query: str, **kwargs: Any) -> Dict[str, Any]:
        return await self._run("graph_search", query, **kwargs)

    async def graph_get_all(self, **kwargs: Any) -> Dict[str, Any]:
        return await self._run("graph_get_all", **kwargs)

    async def graph_delete_all(self, **kwargs: Any) -> Dict[str, Any]:
        return await self._run("graph_delete_all", **kwargs)

    async def list_entities(self, **kwargs: Any) -> Dict[str, Any]:
        return await self._run("list_entities", **kwargs)

    async def delete_entities(self, **kwargs: Any) -> Dict[str, Any]:
        return await self._run("delete_entities", **kwargs)

    async def list_events(self, **kwargs: Any) -> Dict[str, Any]:
        return await self._run("list_events", **kwargs)

    async def get_event_status(self, event_id: str) -> Dict[str, Any]:
        return await self._run("get_event_status", event_id)

    # -- native extensions --------------------------------------------------

    async def capsule(self, memory_id: str, **kwargs: Any) -> Dict[str, Any]:
        return await self._run("capsule", memory_id, **kwargs)

    async def expand(self, ref: str, **kwargs: Any) -> Dict[str, Any]:
        return await self._run("expand", ref, **kwargs)

    async def temporal_query(self, entity: Optional[str] = None,
                             **kwargs: Any) -> Dict[str, Any]:
        return await self._run("temporal_query", entity, **kwargs)

    async def verify_integrity(self) -> Dict[str, Any]:
        return await self._run("verify_integrity")

    async def describe(self) -> Dict[str, Any]:
        return await self._run("describe")

    async def add_many(self, items: List[Dict[str, Any]],
                       **common: Any) -> List[Dict[str, Any]]:
        """``add()`` many conversations concurrently.

        Provided because the intended high-throughput path is many *small* writes
        from many conversations, and the natural way to write that is a loop of
        awaits — which would serialise.  This gathers them instead, while
        ``return_exceptions`` keeps one malformed item from discarding the rest.
        """
        tasks = [self.add(item.get("messages"), **{**common,
                                                   **(item.get("options") or {})})
                 for item in items]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [r if not isinstance(r, BaseException) else {"error": str(r)}
                for r in results]

    # -- escape hatches -----------------------------------------------------

    @property
    def sync(self) -> Memory:
        """The underlying synchronous instance, for non-async callers."""
        return self._sync

    async def __aenter__(self) -> "AsyncMemory":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()
