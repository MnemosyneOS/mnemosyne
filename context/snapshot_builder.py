"""Memory snapshot builder — generates a frozen context prompt string.

Zero-dependency: uses only the Python standard library.

A snapshot is a point-in-time, immutable string representation of the
brain's relevant memory state, suitable for injection into an LLM
context.  Once generated, the snapshot is frozen — subsequent mutations
to the memory store do not change it.

Usage::

    from context.snapshot_builder import SnapshotBuilder
    sb = SnapshotBuilder(brain)
    prompt = sb.build_context_prompt(query="what is AI?", max_chars=2000)
"""
import copy
import hashlib
import json

__all__ = ["SnapshotBuilder", "MemorySnapshot"]


class MemorySnapshot:
    """An immutable snapshot of memory context at a point in time."""

    def __init__(self, query=None, max_chars=2000, content="", source_ids=None,
                 token_estimate=0, timestamp=None, schema_version="snapshot-v1.0"):
        self._query = query
        self._max_chars = max_chars
        self._content = content
        self._source_ids = tuple(source_ids or [])
        self._token_estimate = token_estimate
        from datetime import datetime, timezone, timedelta
        if timestamp is None:
            tz = timezone(timedelta(hours=8))
            timestamp = datetime.now(tz).isoformat(timespec="seconds")
        self._timestamp = timestamp
        self._schema_version = schema_version
        # Freeze: store a copy so later mutations don't affect this snapshot
        self._frozen = False
        self._hash = hashlib.sha256(
            content.encode("utf-8") if isinstance(content, str) else b""
        ).hexdigest()[:16]

    @property
    def content(self):
        return self._content

    @property
    def token_estimate(self):
        return self._token_estimate

    @property
    def timestamp(self):
        return self._timestamp

    @property
    def source_ids(self):
        return list(self._source_ids)

    @property
    def query(self):
        return self._query

    @property
    def hash(self):
        return self._hash

    def to_string(self):
        """Return the frozen snapshot as a string (immutable)."""
        return self._content

    def __str__(self):
        return self._content

    def __len__(self):
        return len(self._content)

    def to_dict(self):
        return {
            "schema_version": self._schema_version,
            "query": self._query,
            "max_chars": self._max_chars,
            "timestamp": self._timestamp,
            "token_estimate": self._token_estimate,
            "source_ids": list(self._source_ids),
            "hash": self._hash,
            "char_count": len(self._content),
        }


class SnapshotBuilder:
    """Builds frozen context snapshots from a MemoryBrain or compatible object.

    A snapshot is a point-in-time, immutable string representation of the
    brain's relevant memory state, suitable for injection into an LLM
    context.  Once generated, the snapshot is frozen — subsequent mutations
    to the memory store do not change it.
    """

    def __init__(self, brain=None):
        self.brain = brain
        self._snapshots = []  # history of snapshots

    def build_context_prompt(self, query=None, max_chars=2000):
        """Build a frozen context prompt from current memory state.

        Parameters
        ----------
        query : str or None
            Optional query to focus the snapshot on relevant memories.
            If set, the brain's recall() is used to retrieve relevant
            memories; otherwise all active memories are included.
        max_chars : int
            Maximum number of characters in the snapshot content.

        Returns
        -------
        str : The frozen snapshot string (immutable — subsequent store
              mutations do not change it).
        """
        parts = []
        source_ids = []
        token_estimate = 0

        # Header
        header = f"[Mnemosyne Context Snapshot | "
        if query:
            header += f"Query: {query[:100]} | "
        header += f"Max chars: {max_chars}]"
        parts.append(header)

        if self.brain is not None:
            # Retrieve relevant memories
            if query:
                try:
                    results = self.brain.recall(query, k=10)
                    for score, rec, reasons in results:
                        content = rec.get("content", "") if isinstance(rec, dict) else str(rec)
                        if content:
                            parts.append(f"- {content[:300]}")
                            source_ids.append(rec.get("id", ""))
                            token_estimate += len(content) // 4
                except Exception:
                    pass
            else:
                # All active memories
                try:
                    records = self.brain.store.all_records()
                    for r in records:
                        if r.get("status", "active") in ("active", "working"):
                            content = r.get("content", "")
                            if content:
                                parts.append(f"- {content[:300]}")
                                source_ids.append(r.get("id", ""))
                                token_estimate += len(content) // 4
                except Exception:
                    pass

        # Profile context (if available)
        if self.brain is not None and hasattr(self.brain, "profile_manager"):
            try:
                profiles = self.brain.profile_manager.get_all_profiles()
                if profiles:
                    parts.append("\n[User Profile]")
                    for k, v in profiles.items():
                        parts.append(f"- {k}: {str(v)[:200]}")
            except Exception:
                pass

        # Assemble with char limit
        content = "\n".join(parts)
        if len(content) > max_chars:
            content = content[:max_chars] + "\n...[truncated]"

        snapshot = MemorySnapshot(
            query=query,
            max_chars=max_chars,
            content=content,
            source_ids=source_ids,
            token_estimate=token_estimate,
        )
        # Deep-copy to ensure immutability even if references are held
        self._snapshots.append(copy.deepcopy(snapshot.to_dict()))
        return content

    def get_snapshot_history(self):
        """Return the list of snapshot metadata dicts."""
        return list(self._snapshots)
