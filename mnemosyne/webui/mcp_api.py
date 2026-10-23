#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — client API MCP tools
========================================

The eleven tool names a client written for the reference MCP server will call,
implemented against the compatibility layer.  They are added *alongside* the
twenty native tools rather than replacing them, so an existing Mnemosyne client
keeps working and a migrating client finds every name it expects.

======================  =========================================================
``add_memory``          Save text or a conversation for a user/agent
``search_memories``     Semantic search with filters
``get_memories``        List with structured filters and pagination
``get_memory``          Fetch one by ``memory_id``
``update_memory``       Overwrite text and/or metadata for a known id
``delete_memory``       Delete one by ``memory_id``
``delete_all_memories`` Bulk delete everything in a scope
``delete_entities``     Delete an entity and cascade
``list_entities``       Enumerate users/agents/apps/runs
``list_events``         List memory operations
``get_event_status``    Poll an async operation
======================  =========================================================

Why these are safe to add: their names are namespaced by convention
(``*_memories`` / ``*_memory`` / ``*_entities`` / ``*_events``) and none collides
with the native set (``retain`` / ``recall`` / ``forget`` / ``capsule`` / ...).
The two families can therefore coexist in one ``tools/list`` response without a
client having to choose.

Difference from the reference worth knowing: these tools write to Mnemosyne's own
store, so graph memory, capsule compression and ledger verification are available
through them even though the reference server offers no such tools.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# 订阅门控（v8.1.1 SaaS 层）。防御性导入：门控缺失绝不能把 11 个兼容工具
# 一起带下线 —— 自部署用户根本不该感知到它的存在。
try:
    from . import tiers as _tiers
except Exception:                                    # pragma: no cover
    try:
        import tiers as _tiers
    except Exception:
        _tiers = None

__all__ = ["TOOLS", "TOOL_NAMES", "handle"]

#: Entity dimensions accepted by every tool that scopes memories.
_SCOPE_PROPS = {
    "user_id": {"type": "string", "description": "Associate with this user."},
    "agent_id": {"type": "string", "description": "Associate with this agent."},
    "run_id": {"type": "string", "description": "Associate with this session/run."},
    "app_id": {"type": "string", "description": "Associate with this app."},
}

_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "add_memory",
        "description": (
            "Save text or conversation history for a user/agent. Single-pass "
            "ADD-only extraction: memories accumulate, nothing is overwritten. "
            "Accepts a plain string, a message dict, or a list of message dicts. "
            "Set infer=false to store verbatim without extraction."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "messages": {"description": "Text or messages to remember.",
                             "oneOf": [{"type": "string"},
                                       {"type": "object"},
                                       {"type": "array"}]},
                **_SCOPE_PROPS,
                "metadata": {"type": "object",
                             "description": "Extra key/value metadata attached to each memory."},
                "infer": {"type": "boolean", "default": True},
                "memory_type": {"type": "string",
                                "description": "semantic / episodic / procedural / preference / identity / strategy"},
                "expiration_date": {"type": "string",
                                    "description": "YYYY-MM-DD; hidden from search after this date."},
                "immutable": {"type": "boolean", "default": False},
                "includes": {"type": "string", "description": "Free-text hint of what to include."},
                "excludes": {"type": "string", "description": "Free-text hint of what to exclude."},
                "observation_date": {"type": "string"},
                "temporal_reasoning": {"type": "boolean", "default": False},
                "async_mode": {"type": "boolean", "default": False,
                               "description": "Return an event_id instead of waiting."},
            },
            "required": ["messages"],
        },
    },
    {
        "name": "search_memories",
        "description": ("Semantic search across existing memories with filters. "
                        "Entity ids go inside `filters` (not top-level). "
                        "threshold is a relevance floor in [0,1]; rerank=true adds "
                        "a second-stage reranker; explain=true adds the matching signals."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "filters": {"type": "object",
                            "description": "e.g. {\"user_id\": \"alice\", \"topic\": {\"eq\": \"work\"}}. "
                                           "Operators: eq/ne/gt/gte/lt/lte/in/nin/contains/icontains/wildcard, "
                                           "plus AND/OR/NOT."},
                "top_k": {"type": "integer", "default": 20},
                "threshold": {"type": "number", "default": 0.1},
                "rerank": {"type": "boolean", "default": False},
                "explain": {"type": "boolean", "default": False},
                "reference_date": {"type": "string",
                                   "description": "Resolve relative time expressions against this date."},
                "show_expired": {"type": "boolean", "default": False},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_memories",
        "description": "List memories with structured filters and pagination.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filters": {"type": "object"},
                "top_k": {"type": "integer", "default": 20},
                "show_expired": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "get_memory",
        "description": "Retrieve one memory by its memory_id.",
        "inputSchema": {"type": "object",
                        "properties": {"memory_id": {"type": "string"}},
                        "required": ["memory_id"]},
    },
    {
        "name": "update_memory",
        "description": ("Overwrite a memory's text and/or metadata after confirming "
                        "the ID. Only supplied fields change. An immutable memory's "
                        "text cannot be changed, but its metadata can."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
                "text": {"type": "string"},
                "metadata": {"type": "object"},
                "expiration_date": {"type": "string"},
            },
            "required": ["memory_id"],
        },
    },
    {
        "name": "delete_memory",
        "description": ("Delete a single memory by memory_id. Soft delete by default: "
                        "the audit ledger entry and confidence trajectory are retained "
                        "so the retraction itself stays answerable."),
        "inputSchema": {"type": "object",
                        "properties": {"memory_id": {"type": "string"}},
                        "required": ["memory_id"]},
    },
    {
        "name": "delete_all_memories",
        "description": ("Bulk delete all memories in a scope. At least one entity id "
                        "is required — this tool will not empty the shared default "
                        "scope by accident."),
        "inputSchema": {
            "type": "object",
            "properties": {"filters": {"type": "object"}, **_SCOPE_PROPS},
        },
    },
    {
        "name": "delete_entities",
        "description": "Delete a user/agent/app/run entity and cascade to its memories.",
        "inputSchema": {
            "type": "object",
            "properties": {"filters": {"type": "object"}, **_SCOPE_PROPS},
        },
    },
    {
        "name": "list_entities",
        "description": "Enumerate the users/agents/apps/runs stored in Mnemosyne, with memory counts.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 100}},
        },
    },
    {
        "name": "list_events",
        "description": "List memory operation events with filters and pagination.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string",
                           "enum": ["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]},
                "operation": {"type": "string", "enum": ["add", "delete"]},
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    {
        "name": "get_event_status",
        "description": "Check the status of an async memory operation by event_id.",
        "inputSchema": {"type": "object",
                        "properties": {"event_id": {"type": "string"}},
                        "required": ["event_id"]},
    },
]

#: The tool definitions, exported under the same name the native module uses.
TOOLS = _TOOLS

#: Fast membership test for the dispatcher.
TOOL_NAMES = frozenset(t["name"] for t in _TOOLS)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _memory():
    """The shared compatibility memory, configured like the REST surface."""
    from .api_routes import get_memory

    return get_memory()


def _scope(args: Dict[str, Any]) -> Dict[str, Any]:
    """Collect entity ids from either the top level or a nested ``filters``."""
    nested = args.get("filters") if isinstance(args.get("filters"), dict) else {}
    out: Dict[str, Any] = {}
    for key in ("user_id", "agent_id", "run_id", "app_id"):
        val = args.get(key, nested.get(key))
        if val is not None:
            out[key] = val
    return out


def handle(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Execute one compatible MCP tool call.

    Returns a plain dict; the caller serialises it into a tool result.  Failures
    come back as ``{"error": ..., "code": ...}`` rather than raising, because an
    MCP tool error should reach the model as content it can reason about, not as a
    transport-level failure that hides the reason.
    """
    args = arguments if isinstance(arguments, dict) else {}

    # 订阅门控（与 mcp_server.py 共用同一份规则 —— 否则「换个工具名就绕过」
    # 是最容易被发现、也最难解释的漏洞）。三段式：准入 → 执行 → 出口过滤。
    if _tiers is not None:
        try:
            allowed, reason = _tiers.check_tier_gate(name, args, _tier_ctx(args))
            if not allowed:
                return {"error": reason or "tier_gate_denied",
                        "code": "TIER_GATE_001", "tool": name}
        except Exception:
            pass
    result = _handle_raw(name, args)
    try:
        return _apply_gate(name, args, result)
    except Exception:
        return result


def _tier_ctx(args: Dict[str, Any]) -> Dict[str, Any]:
    ctx: Dict[str, Any] = {}
    for key in ("_tier", "_namespace", "_allowed_namespaces"):
        if key in args:
            ctx[key[1:]] = args[key]
    return ctx


def _apply_gate(name: str, args: Dict[str, Any], result):
    """出口过滤：召回类按冷层窗口收窄，并附 upgrade_hint 数据信号。"""
    if _tiers is None or not _tiers.gate_enabled():
        return result
    if not isinstance(result, dict) or result.get("error"):
        return result
    if name not in _tiers.RECALL_TOOLS:
        return result
    tier = _tiers.resolve_tier(_tier_ctx(args))
    for key in ("results", "items", "memories"):
        if isinstance(result.get(key), list):
            # 投影不回传 tier → 按 memory_id 回查存储（见 tiers.py 文档）
            kept, dropped = _tiers.filter_recall_items(
                result[key], tier, record_lookup=_make_lookup())
            if dropped:
                result[key] = kept
                hint = _tiers.upgrade_hint(tier, dropped, "cold")
                if hint:
                    result["upgrade_hint"] = hint
            break
    return result


def _make_lookup():
    """兼容层按 id 回查真实记录的闭包。

    注意：兼容层（`api.memory.Memory`）的存储不是 `brain.store`，
    而是它自己的 `_brain`。拿不到就返回 None —— 门控会退化为
    「不回查、按返回值判断」，此时不会误删数据，只是少拦几条。
    """
    try:
        mem = _memory()
        brain = getattr(mem, "_brain", None) or getattr(mem, "brain", None)
        store = getattr(brain, "store", None)
        if store is None or not hasattr(store, "find_by_id"):
            return None
        return lambda mid: store.find_by_id(mid)
    except Exception:
        return None


def _handle_raw(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    mem = _memory()

    if name == "add_memory":
        kwargs = _scope(args)
        if args.get("metadata") is not None:
            kwargs["metadata"] = args["metadata"]
        for key in ("infer", "memory_type", "immutable", "includes", "excludes",
                    "observation_date", "temporal_reasoning"):
            if args.get(key) is not None:
                kwargs[key] = args[key]
        if args.get("expiration_date"):
            kwargs["expiration_date"] = args["expiration_date"]
        # Default to asynchronous so a tool loop is not blocked by extraction
        # latency; callers that need the ids immediately pass async_mode=false.
        kwargs["async_mode"] = bool(args.get("async_mode", True))
        result = mem.add(args.get("messages"), **kwargs)
        if "event_id" in result:
            result["note"] = ("Accepted; poll get_event_status(event_id) for the "
                              "result. Pass async_mode=false to wait instead.")
        return result

    if name == "search_memories":
        kwargs: Dict[str, Any] = {"filters": _scope(args)}
        if args.get("filters") is not None:
            merged = dict(args["filters"]) if isinstance(args["filters"], dict) else {}
            kwargs["filters"] = merged
        for key in ("top_k", "threshold", "rerank", "explain", "reference_date",
                    "show_expired"):
            if args.get(key) is not None:
                kwargs[key] = args[key]
        return mem.search(args.get("query", ""), **kwargs)

    if name == "get_memories":
        filters = dict(args.get("filters") or {})
        filters.update(_scope(args))
        kwargs = {"filters": filters}
        if args.get("top_k") is not None:
            kwargs["top_k"] = args["top_k"]
        if args.get("show_expired") is not None:
            kwargs["show_expired"] = args["show_expired"]
        return mem.get_all(**kwargs)

    if name == "get_memory":
        return mem.get(args.get("memory_id"))

    if name == "update_memory":
        kwargs = {}
        if args.get("text") is not None:
            kwargs["text"] = args["text"]
        if args.get("metadata") is not None:
            kwargs["metadata"] = args["metadata"]
        if args.get("expiration_date") is not None:
            kwargs["expiration_date"] = args["expiration_date"]
        return mem.update(args.get("memory_id"), **kwargs)

    if name == "delete_memory":
        return mem.delete(args.get("memory_id"))

    if name == "delete_all_memories":
        return mem.delete_all(**_scope(args))

    if name == "delete_entities":
        return mem.delete_entities(**_scope(args))

    if name == "list_entities":
        return mem.list_entities(limit=int(args.get("limit") or 100))

    if name == "list_events":
        return mem.list_events(status=args.get("status"),
                               operation=args.get("operation"),
                               limit=int(args.get("limit") or 50))

    if name == "get_event_status":
        return mem.get_event_status(args.get("event_id"))

    return {"error": f"unknown tool: {name}", "code": "NOT_FOUND_001"}
