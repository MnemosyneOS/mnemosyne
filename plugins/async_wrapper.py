"""Asynchronous API wrapper for MemoryBrain (missing-feature list).

Wraps synchronous MemoryBrain methods with async/await using
``asyncio.to_thread`` for non-blocking operation.

Zero-dependency: uses only the Python standard library (asyncio).

Usage::

    from plugins.async_wrapper import AsyncMemoryBrain
    brain = AsyncMemoryBrain(base_dir="/path/to/memories")
    await brain.async_retain("Hello, world!")
    results = await brain.async_recall("Hello")
"""
import asyncio
import functools

__all__ = ["AsyncMemoryBrain"]


class AsyncMemoryBrain:
    """Async wrapper around MemoryBrain.

    All operations run in a thread executor to avoid blocking the
    asyncio event loop.
    """

    def __init__(self, base_dir=None, enable_embeddings=True,
                 enable_graph=True, **kwargs):
        from mnemosyne import MemoryBrain
        self._brain = MemoryBrain(
            base_dir=base_dir,
            enable_embeddings=enable_embeddings,
            enable_graph=enable_graph,
            **kwargs,
        )

    def _run(self, func, *args, **kwargs):
        """Run a synchronous function in a thread and return an awaitable."""
        return asyncio.to_thread(func, *args, **kwargs)

    async def async_retain(self, content, mtype="semantic", fast=False,
                           project=None, **kwargs):
        """Async version of retain."""
        return await self._run(
            self._brain.retain, content, mtype=mtype,
            fast=fast, project=project, **kwargs,
        )

    async def async_retain_detailed(self, content, mtype="semantic",
                                    fast=False, project=None, **kwargs):
        """Async version of retain_detailed."""
        return await self._run(
            self._brain.retain_detailed, content, mtype=mtype,
            fast=fast, project=project, **kwargs,
        )

    async def async_recall(self, query, k=5, project=None, **kwargs):
        """Async version of recall."""
        return await self._run(
            self._brain.recall, query, k=k, project=project, **kwargs,
        )

    async def async_consolidate(self, min_similarity=0.75, max_group=8,
                                dry_run=False, **kwargs):
        """Async version of consolidate."""
        return await self._run(
            self._brain.consolidate, min_similarity=min_similarity,
            max_group=max_group, dry_run=dry_run, **kwargs,
        )

    async def async_forget(self, memory_id):
        """Async version of forget."""
        return await self._run(self._brain.forget, memory_id)

    async def async_forget_by_filter(self, mtype=None, tag=None,
                                     project=None, older_than=None,
                                     limit=None):
        """Async version of forget_by_filter."""
        return await self._run(
            self._brain.forget_by_filter, mtype=mtype, tag=tag,
            project=project, older_than=older_than, limit=limit,
        )

    async def async_status(self, **kwargs):
        """Async version of status."""
        return await self._run(self._brain.status, **kwargs)

    async def async_add_conversation_turn(self, session_id, role, content,
                                          metadata=None):
        """Async version of add_conversation_turn."""
        return await self._run(
            self._brain.add_conversation_turn, session_id, role,
            content, metadata=metadata,
        )

    async def async_search_conversations(self, query, session_id=None, k=10):
        """Async version of search_conversations."""
        return await self._run(
            self._brain.search_conversations, query,
            session_id=session_id, k=k,
        )

    async def async_set_profile(self, profile_id, data):
        """Async version of set_profile."""
        return await self._run(
            self._brain.set_profile, profile_id, data,
        )

    async def async_get_profile(self, profile_id):
        """Async version of get_profile."""
        return await self._run(self._brain.get_profile, profile_id)

    async def async_delete_profile(self, profile_id):
        """Async version of delete_profile."""
        return await self._run(self._brain.delete_profile, profile_id)

    async def async_build_context_prompt(self, query=None, max_chars=2000):
        """Async version of build_context_prompt."""
        return await self._run(
            self._brain.build_context_prompt, query=query,
            max_chars=max_chars,
        )

    async def async_export_memories(self, filepath, namespace="default"):
        """Async version of export_memories."""
        return await self._run(
            self._brain.export_memories, filepath, namespace=namespace,
        )

    async def async_import_memories(self, filepath, namespace="default"):
        """Async version of import_memories."""
        return await self._run(
            self._brain.import_memories, filepath, namespace=namespace,
        )

    @property
    def brain(self):
        """Access the underlying synchronous brain."""
        return self._brain

    def close(self):
        """Close the underlying brain."""
        if hasattr(self._brain, "close"):
            self._brain.close()


# ---- Convenience: async context manager ----
class AsyncMemoryBrainContext:
    """Async context manager for AsyncMemoryBrain."""

    def __init__(self, base_dir=None, **kwargs):
        self._brain = None
        self._base_dir = base_dir
        self._kwargs = kwargs

    async def __aenter__(self):
        self._brain = AsyncMemoryBrain(self._base_dir, **self._kwargs)
        return self._brain

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._brain:
            self._brain.close()
        return False
