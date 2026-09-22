#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne × CrewAI
==================

A crew is several agents with different roles working one task.  The memory problem
that creates is specific and worth naming: each agent needs the *shared* facts
(context nobody should have to be told twice) without inheriting the other agents'
role-flavoured reasoning.  A single undifferentiated memory blob gives them both.

``MnemosyneCrewMemory`` therefore partitions by ``agent_id`` by default: the crew's
common facts live in the crew scope, and each agent's own observations live in its
own scope.  Reads span both, writes are attributed.

Usage::

    from integrations.crewai import MnemosyneCrewMemory

    crew_memory = MnemosyneCrewMemory(crew_id="research-crew")

    # An agent records what it learned, attributed to itself.
    crew_memory.remember("Acme's Q3 revenue was $4.2M.", agent="analyst")

    # Any agent recalls from the crew scope plus its own.
    for row in crew_memory.recall("Acme revenue", agent="writer"):
        print(row["memory"])

    # Inject into a task description.
    context = crew_memory.context_for("Acme revenue", agent="writer")
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from mnemosyne.api import Memory, MemoryConfig

__all__ = ["MnemosyneCrewMemory", "MnemosyneAgentMemory"]


class MnemosyneCrewMemory:
    """Shared long-term memory for a crew, partitioned per agent.

    Parameters
    ----------
    crew_id:
        Scope for facts the whole crew shares.  Required: an unscoped crew memory
        would land in the default bucket alongside every other unscoped caller.
    user_id:
        The human the crew works for, when there is one.  Keeping it in the scope
        means one deployment can serve many end users without their memories
        touching.
    agent_ids:
        The crew's roster.  Supplied so :meth:`recall` can search every member's
        scope without being told which agents exist at call time — and so a typo
        in an agent name is reported rather than silently producing an empty
        scope.
    include_agent_scopes:
        Whether recall spans member scopes as well as the crew scope.  Leave true
        for the default "shared facts + what the crew already knows" behaviour;
        set false when a task must be answered only from crew-approved facts.
    """

    def __init__(self, crew_id: str,
                 user_id: Optional[str] = None,
                 agent_ids: Optional[List[str]] = None,
                 include_agent_scopes: bool = True,
                 top_k: int = 8,
                 brain_dir: Optional[str] = None,
                 config: Optional[Dict[str, Any]] = None,
                 memory: Optional[Memory] = None):
        if not str(crew_id or "").strip():
            raise ValueError("crew_id is required — an unscoped crew memory shares "
                             "one bucket with every other unscoped caller")
        self.crew_id = str(crew_id).strip()
        self.user_id = user_id
        self.agent_ids = [str(a) for a in (agent_ids or [])]
        self.include_agent_scopes = include_agent_scopes
        self.top_k = int(top_k)

        if memory is not None:
            self._memory = memory
        else:
            cfg = dict(config or {})
            cfg.setdefault("brain_dir", brain_dir or os.environ.get("MNEMOSYNE_DIR")
                           or os.path.join(os.path.expanduser("~"), ".mnemosyne"))
            cfg.setdefault("llm", {"provider": "rules"})
            cfg.setdefault("embedder", {"provider": "builtin"})
            self._memory = Memory(MemoryConfig.from_dict(cfg))

    # -- scope helpers ------------------------------------------------------

    @property
    def memory(self) -> Memory:
        return self._memory

    def _crew_scope(self) -> Dict[str, str]:
        """The shared scope.  ``app_id`` carries the crew so it partitions cleanly."""
        scope = {"app_id": self.crew_id}
        if self.user_id:
            scope["user_id"] = str(self.user_id)
        return scope

    def _agent_scope(self, agent: Optional[str]) -> Dict[str, str]:
        scope = self._crew_scope()
        if agent:
            name = str(agent).strip()
            if self.agent_ids and name not in self.agent_ids:
                # Report rather than write into a scope nobody will ever read.
                raise ValueError(
                    f"unknown agent {name!r}; the roster is {self.agent_ids}. "
                    f"Add it to agent_ids if it is a real member.")
            scope["agent_id"] = name
        return scope

    def _scopes_for_recall(self, agent: Optional[str]) -> List[Dict[str, str]]:
        scopes = [self._crew_scope()]
        if self.include_agent_scopes:
            for member in self.agent_ids:
                scopes.append(self._agent_scope(member))
            if agent and agent not in self.agent_ids:
                scopes.append(self._agent_scope(agent))
        elif agent:
            scopes.append(self._agent_scope(agent))
        return scopes

    # -- write --------------------------------------------------------------

    def remember(self, text: str, agent: Optional[str] = None,
                 metadata: Optional[Dict[str, Any]] = None,
                 share_with_crew: bool = False) -> Dict[str, Any]:
        """Record a fact.

        ``share_with_crew=False`` (the default) attributes the fact to the agent
        that produced it; ``True`` writes it into the crew scope so every member
        recalls it.  The default is the conservative one: an agent's intermediate
        reasoning should not become the crew's shared truth just because it was
        said out loud.
        """
        scope = self._crew_scope() if share_with_crew else self._agent_scope(agent)
        meta = dict(metadata or {})
        if agent:
            meta.setdefault("agent", agent)
        meta.setdefault("shared", bool(share_with_crew))
        return self._memory.add(text, metadata=meta, **scope)

    def remember_task(self, description: str, outcome: str,
                      agent: Optional[str] = None,
                      share_with_crew: bool = False) -> Dict[str, Any]:
        """Record a task and its outcome as one memory."""
        text = f"Task: {description.strip()}\nOutcome: {outcome.strip()}"
        return self.remember(text, agent=agent, share_with_crew=share_with_crew,
                             metadata={"kind": "task"})

    # -- read ---------------------------------------------------------------

    def recall(self, query: str, agent: Optional[str] = None,
               top_k: Optional[int] = None,
               explain: bool = False) -> List[Dict[str, Any]]:
        """Recall across the crew scope and (by default) every member scope.

        Scopes are searched separately and merged on score, because scopes are
        physically separate stores — a single query cannot span them, and
        pretending otherwise would silently search only the crew bucket.
        """
        limit = int(top_k or self.top_k)
        merged: Dict[str, Dict[str, Any]] = {}
        for scope in self._scopes_for_recall(agent):
            try:
                rows = self._memory.search(query, filters=scope, top_k=limit,
                                           explain=explain)["results"]
            except Exception:  # noqa: BLE001 - one empty scope is not a failure
                continue
            for row in rows:
                rid = row.get("id")
                if rid and (rid not in merged or
                            float(row.get("score") or 0) >
                            float(merged[rid].get("score") or 0)):
                    merged[rid] = row
        out = sorted(merged.values(), key=lambda r: float(r.get("score") or 0),
                     reverse=True)
        return out[:limit]

    def context_for(self, query: str, agent: Optional[str] = None,
                    top_k: Optional[int] = None,
                    max_chars: int = 2000) -> str:
        """Ready-to-inject text, capped so it cannot crowd out the task itself.

        The cap is a character budget rather than a memory count because memories
        vary in length by an order of magnitude; a fixed count can inject ten
        times more text than intended.
        """
        lines: List[str] = []
        used = 0
        for row in self.recall(query, agent=agent, top_k=top_k):
            text = str(row.get("memory") or "").strip()
            if not text:
                continue
            if used + len(text) + 2 > max_chars:
                break
            lines.append(f"- {text}")
            used += len(text) + 2
        if not lines:
            return ""
        return "Known context (recalled from crew memory):\n" + "\n".join(lines)

    # -- maintenance --------------------------------------------------------

    def forget_agent(self, agent: str) -> Dict[str, Any]:
        """Drop one member's private memories, keeping the crew's shared facts."""
        return self._memory.delete_all(**self._agent_scope(agent))

    def forget_crew(self) -> Dict[str, Any]:
        """Drop the shared scope only, keeping members' private memories."""
        return self._memory.delete_all(**self._crew_scope())

    def roster(self) -> List[str]:
        return list(self.agent_ids)


class MnemosyneAgentMemory:
    """Convenience wrapper for a single agent — a crew of one.

    Exists because most integrations start with one agent and grow into a crew;
    starting with ``MnemosyneCrewMemory(crew_id=..., agent_ids=[...])`` would make
    the common case carry the uncommon case's ceremony.
    """

    def __init__(self, agent_id: str, user_id: Optional[str] = None,
                 top_k: int = 8, brain_dir: Optional[str] = None,
                 config: Optional[Dict[str, Any]] = None,
                 memory: Optional[Memory] = None):
        self._crew = MnemosyneCrewMemory(
            crew_id=str(agent_id), user_id=user_id, agent_ids=[str(agent_id)],
            top_k=top_k, brain_dir=brain_dir, config=config, memory=memory)

    @property
    def crew(self) -> MnemosyneCrewMemory:
        return self._crew

    @property
    def memory(self) -> Memory:
        return self._crew.memory

    def remember(self, text: str, **kwargs: Any) -> Dict[str, Any]:
        return self._crew.remember(text, **kwargs)

    def recall(self, query: str, **kwargs: Any) -> List[Dict[str, Any]]:
        return self._crew.recall(query, **kwargs)

    def context_for(self, query: str, **kwargs: Any) -> str:
        return self._crew.context_for(query, **kwargs)
