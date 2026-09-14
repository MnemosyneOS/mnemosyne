#!/usr/bin/env python3
"""
MCP (Model Context Protocol) Server for Mnemosyne OS
Zero external dependencies — pure stdlib JSON-RPC over stdio.

Multi-tenant support (Module 5.4 item 9 / Module 7.3):
  The client may pass a namespace via:
    - The ``initialize`` request's ``clientInfo.name`` field (e.g. "tenant:acme")
    - OR a per-request ``namespace`` argument on any tool call
  When set, the brain routes all I/O through an isolated sqlite file at
  ``<brain-dir>/data/namespaces/<namespace>/memory.db``.  Tenants are
  physically isolated — two namespaces see completely separate data.

Authentication (v7.0.0 阶段4):
  When ``MNEMOSYNE_MCP_TOKEN`` is set (or ``--token`` is passed), every
  request must carry the matching token, otherwise it is rejected with a
  JSON-RPC error (code -32001). The token may be provided via:
    - ``_meta.authToken`` on the JSON-RPC request (MCP standard)
    - ``params._meta.authToken``
    - a top-level ``authToken`` / ``token`` field
  When no token is configured, authentication is disabled (open mode),
  preserving backward compatibility.
"""
import sys, json, os, argparse, hashlib, hmac
from mnemosyne import MemoryBrain

DEFAULT_BRAIN = os.path.expanduser("~/.mnemosyne")
brain = None
_brain_dir = None
_default_namespace = "default"
_auth_token = None


def _get_expected_token():
    """Return the configured MCP token, or None if auth is disabled."""
    return os.environ.get("MNEMOSYNE_MCP_TOKEN") or _auth_token


def _request_token(req):
    """Extract the client-supplied auth token from a JSON-RPC request."""
    if not isinstance(req, dict):
        return None
    # 1) MCP 标准：请求级 _meta.authToken
    meta = req.get("_meta")
    if isinstance(meta, dict) and meta.get("authToken"):
        return meta["authToken"]
    # 2) params._meta.authToken
    params = req.get("params")
    if isinstance(params, dict):
        pm = params.get("_meta")
        if isinstance(pm, dict) and pm.get("authToken"):
            return pm["authToken"]
    # 3) 顶层 authToken / token 字段
    for key in ("authToken", "token"):
        val = req.get(key)
        if val:
            return val
    return None


def _authorized(req):
    """Return True if the request is authorized (or auth is disabled)."""
    expected = _get_expected_token()
    if not expected:
        return True
    supplied = _request_token(req)
    return bool(supplied) and hmac.compare_digest(str(supplied), str(expected))

def _ensure_brain(brain_dir=None, namespace=None, actor="mcp"):
    """Ensure a brain is initialised for *namespace*.

    Different namespaces get different brain instances (and different Db files).
    """
    global brain
    if brain_dir is None:
        brain_dir = _brain_dir or DEFAULT_BRAIN
    ns = namespace or _default_namespace
    key = (brain_dir, ns)
    cache_key = getattr(_ensure_brain, "_cache_key", None)
    # Use namespace-scoped brain cache
    if not hasattr(_ensure_brain, "_brains"):
        _ensure_brain._brains = {}
    if key not in _ensure_brain._brains:
        b = MemoryBrain(brain_dir, namespace=ns, actor=actor)
        b.ensure_init()
        _ensure_brain._brains[key] = b
    brain = _ensure_brain._brains[key]
    return brain

def _get_ns(arguments):
    """Extract namespace from per-request arguments."""
    return arguments.get("namespace") if isinstance(arguments, dict) else None

# ---------- 14 tools (includes audit + forget + namespace) ----------
TOOLS = [
    {"name":"retain","description":"写入记忆。content: 内容; mtype: 类型; tags: 标签数组（如 [\"偏好\",\"项目\"]）; confidence: 可信度0-1; importance: 重要性1-5; supersedes: 本记忆所更正的旧记忆id（更正场景，写入后旧记忆被标记 superseded）; namespace: 多租户隔离; project: 可选项目隔离",
     "inputSchema":{"type":"object","properties":{"content":{"type":"string"},"mtype":{"type":"string","enum":["semantic","episodic","procedural","preference","identity","lesson","strategy","reflective"],"default":"semantic"},"tags":{"type":"array","items":{"type":"string"}},"confidence":{"type":"number"},"importance":{"type":"integer"},"supersedes":{"type":"string"},"project":{"type":"string"},"namespace":{"type":"string"}},"required":["content"]}},
    {"name":"recall","description":"检索记忆。query: 查询; k: 返回条数; namespace: 多租户隔离; project: 可选项目隔离。返回项含 memory_id（可直接用于 forget / retain(supersedes=)）与 verification——verification=superseded/outdated 表示该条已被更新的说法取代，不得当作当前事实引用",
     "inputSchema":{"type":"object","properties":{"query":{"type":"string"},"k":{"type":"integer","default":5},"namespace":{"type":"string"},"project":{"type":"string"}},"required":["query"]}},
    {"name":"forget","description":"遗忘/删除记忆：把目标记忆的 confidence 降为 0 并标记 status=deleted（默认软遗忘，审计链与可信度轨迹保留、可回溯）。memory_id 与 query 二选一：给 query 时先按相关性解析目标。建议先用 dry_run=true 把候选报给用户确认，再执行。evict=true 为物理删除。",
     "inputSchema":{"type":"object","properties":{"memory_id":{"type":"string"},"query":{"type":"string"},"k":{"type":"integer","default":3},"dry_run":{"type":"boolean","default":False},"evict":{"type":"boolean","default":False},"reason":{"type":"string"},"namespace":{"type":"string"}},"required":[]}},
    {"name":"stats","description":"运行统计——写入/召回/Token节省等全维度；namespace: 可选",
     "inputSchema":{"type":"object","properties":{"namespace":{"type":"string"}}}},
    {"name":"graph_query","description":"知识图谱查询；namespace: 可选",
     "inputSchema":{"type":"object","properties":{"entity":{"type":"string"},"namespace":{"type":"string"}},"required":["entity"]}},
    {"name":"retain_batch","description":"批量写入（15x加速）；items 每项可为 {content, mtype, tags, confidence, importance}；namespace: 可选",
     "inputSchema":{"type":"object","properties":{"items":{"type":"array","items":{"type":"object","properties":{"content":{"type":"string"},"mtype":{"type":"string","default":"semantic"},"tags":{"type":"array","items":{"type":"string"}},"confidence":{"type":"number"},"importance":{"type":"integer"}},"required":["content"]}},"namespace":{"type":"string"},"project":{"type":"string"}},"required":["items"]}},
    {"name":"doctor","description":"健康检查——扫描记忆库完整性、记录数、磁盘；namespace: 可选",
     "inputSchema":{"type":"object","properties":{"namespace":{"type":"string"}}}},
    {"name":"temporal_query","description":"时序查询——按时间排序返回版本链；namespace: 可选",
     "inputSchema":{"type":"object","properties":{"entity":{"type":"string"},"namespace":{"type":"string"}},"required":[]}},
    {"name":"list_projects","description":"列出所有项目名（多项目隔离）；namespace: 可选",
     "inputSchema":{"type":"object","properties":{"namespace":{"type":"string"}}}},
    {"name":"audit","description":"Audit trail 查询；memory_id: 目标id; namespace: 可选",
     "inputSchema":{"type":"object","properties":{"memory_id":{"type":"string"},"namespace":{"type":"string"}},"required":["memory_id"]}},
    {"name":"confidence_history","description":"Confidence trajectory 查询；memory_id: 目标id; namespace: 可选",
     "inputSchema":{"type":"object","properties":{"memory_id":{"type":"string"},"namespace":{"type":"string"}},"required":["memory_id"]}},
    {"name":"memory/export-v1","description":"Export all active memories to JSONL + manifest.json (Memory Exchange Protocol)。namespace: 可选; filepath: 导出目录路径",
     "inputSchema":{"type":"object","properties":{"namespace":{"type":"string"},"filepath":{"type":"string"}},"required":["filepath"]}},
    {"name":"memory/import-v1","description":"Import memories from JSONL + manifest.json (Memory Exchange Protocol)。namespace: 可选; filepath: 导入目录路径",
     "inputSchema":{"type":"object","properties":{"namespace":{"type":"string"},"filepath":{"type":"string"}},"required":["filepath"]}},
    {"name":"memory/claim","description":"Claim memories from an external export (import-and-merge logic)。namespace: 可选; filepath: 导入目录路径",
     "inputSchema":{"type":"object","properties":{"namespace":{"type":"string"},"filepath":{"type":"string"}},"required":["filepath"]}},
]

def handle_tools_list():
    return {"tools": TOOLS}

def handle_tools_call(name, arguments):
    ns = _get_ns(arguments)
    b = _ensure_brain(namespace=ns)
    project = arguments.get("project", "")
    actor = arguments.get("actor", "mcp")

    if name == "retain":
        content = arguments["content"]
        mtype = arguments.get("mtype", "semantic")
        kwargs = {}
        for kw in ("confidence", "importance", "source", "context", "fact_type",
                    "source_type", "expires_at", "tags", "supersedes"):
            if kw in arguments and arguments[kw] is not None:
                kwargs[kw] = arguments[kw]
        mid = b.retain(content, mtype=mtype, fast=True, project=project, **kwargs)
        rec = b.store.find_by_id(mid)
        result = {"memory_id": mid, "result": "已记住"}
        if rec:
            result["confidence"] = rec.get("confidence")
            result["flags"] = rec.get("flags", [])
        return result

    elif name == "recall":
        query, k = arguments["query"], arguments.get("k", 5)
        results = b.recall(query, k=k, project=project)
        out = []
        for r in results:
            try:
                # memory_id 必须回传：forget / retain(supersedes=) 都要用它定位目标。
                # 原实现不回传 id，导致 Agent 拿到结果也无法执行遗忘或更正。
                out.append({"memory_id": r[1].get("id") if isinstance(r[1], dict) else None,
                            "score": round(float(r[0]), 4),
                            "content": (r[1].get("content","") if isinstance(r[1], dict) else str(r[1]))[:300],
                            "type": r[1].get("type","semantic") if isinstance(r[1], dict) else "unknown",
                            "tags": r[1].get("tags", []) if isinstance(r[1], dict) else [],
                            "created_at": r[1].get("created_at","") if isinstance(r[1], dict) else "",
                            "version": r[1].get("version", 1) if isinstance(r[1], dict) else 1,
                            "confidence": r[1].get("confidence") if isinstance(r[1], dict) else None,
                            # verification 必须回传：更正后的旧记忆在库里是
                            # verification=superseded（检索层降权但不删除），
                            # 不回传的话 Agent 无从分辨"哪条说法已被取代"。
                            "verification": r[1].get("verification", "unverified") if isinstance(r[1], dict) else None,
                            "superseded_by": r[1].get("superseded_by") if isinstance(r[1], dict) else None,
                            "flags": r[1].get("flags", []) if isinstance(r[1], dict) else []})
            except (ValueError, TypeError, IndexError):
                pass
        return {"results": out}

    elif name == "stats":
        return b.stats_tracker.summary() if b.stats_tracker else {}

    elif name == "graph_query":
        try:
            entity = arguments.get("entity")
            result = b.graph_query(entity)
            return {"results": result} if not isinstance(result, dict) else result
        except Exception as e:
            return {"error": str(e)}

    elif name == "retain_batch":
        items = arguments["items"]
        # v7.0.0-MCP 修复：原实现把第三个元素写死成 {}，逐项的 confidence / tags /
        # importance 全部被丢弃 —— 于是"批量写入时设定 confidence 和 tags"这条规则
        # 在批量路径上静默失效。这里逐项透传给 _build_record。
        batch = []
        for it in items:
            kw = {}
            for k in ("confidence", "importance", "tags"):
                if it.get(k) is not None:
                    kw[k] = it[k]
            batch.append((it["content"], it.get("mtype", "semantic"), kw))
        b.retain_batch(batch, fast=True)
        return {"result": f"批量写入 {len(items)} 条",
                "with_tags": sum(1 for it in items if it.get("tags")),
                "with_confidence": sum(1 for it in items if it.get("confidence") is not None),
                "namespace": ns or _default_namespace}

    elif name == "forget":
        memory_id = arguments.get("memory_id")
        query = arguments.get("query")
        if not memory_id and not query:
            return {"error": "memory_id 与 query 至少提供一个"}
        try:
            report = b.forget_targets(
                memory_id=memory_id,
                query=query,
                k=arguments.get("k", 3),
                dry_run=bool(arguments.get("dry_run", False)),
                evict=bool(arguments.get("evict", False)),
                reason=arguments.get("reason") or "user_forget",
            )
            report["namespace"] = ns or _default_namespace
            return report
        except Exception as e:
            return {"error": str(e)}

    elif name == "doctor":
        try:
            return b.doctor()
        except Exception as e:
            return {"error": str(e)}

    elif name == "temporal_query":
        entity = arguments.get("entity")
        try:
            return {"results": b.temporal_query(entity=entity)}
        except Exception as e:
            return {"error": str(e)}

    elif name == "list_projects":
        try:
            projs = b.list_projects()
            return {"projects": projs, "count": len(projs), "namespace": ns or _default_namespace}
        except Exception as e:
            return {"error": str(e)}

    elif name == "audit":
        memory_id = arguments.get("memory_id")
        try:
            trail = b.audit(memory_id)
            return {"memory_id": memory_id, "namespace": ns or _default_namespace, "entries": trail, "count": len(trail)}
        except Exception as e:
            return {"error": str(e)}

    elif name == "confidence_history":
        memory_id = arguments.get("memory_id")
        try:
            history = b.store.get_confidence_history(memory_id) if hasattr(b.store, "get_confidence_history") else []
            return {"memory_id": memory_id, "namespace": ns or _default_namespace, "history": history, "count": len(history)}
        except Exception as e:
            return {"error": str(e)}

    elif name == "memory/export-v1":
        filepath = arguments.get("filepath")
        if not filepath:
            return {"error": "filepath parameter required"}
        try:
            result = b.export_memories(filepath, namespace=ns or _default_namespace)
            return {"result": "success", **result}
        except Exception as e:
            return {"error": str(e)}

    elif name == "memory/import-v1":
        filepath = arguments.get("filepath")
        if not filepath:
            return {"error": "filepath parameter required"}
        try:
            result = b.import_memories(filepath, namespace=ns or _default_namespace)
            return {"result": "success", **result}
        except Exception as e:
            return {"error": str(e)}

    elif name == "memory/claim":
        filepath = arguments.get("filepath")
        if not filepath:
            return {"error": "filepath parameter required"}
        try:
            result = b.claim(filepath, namespace=ns or _default_namespace)
            return {"result": "success", **result}
        except Exception as e:
            return {"error": str(e)}

    return {"error": f"Unknown tool: {name}"}

# ---------- JSON-RPC ----------
def handle_request(req):
    method = req.get("method", ""); rid = req.get("id")
    # v7.0.0 阶段4：MCP 鉴权——配置 token 时，未授权请求直接拒绝。
    if not _authorized(req):
        if rid is None:
            return None  # 无 id 的通知类请求：静默丢弃
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32001,
                          "message": "Unauthorized: invalid or missing MCP token"}}
    if method == "initialize":
        params = req.get("params", {})
        # The client may declare a default namespace in clientInfo.name
        client_info = params.get("clientInfo", {})
        ns_hint = None
        if isinstance(client_info, dict):
            cn = client_info.get("name", "")
            if cn and cn.startswith("tenant:"):
                ns_hint = cn.split(":", 1)[1]
        # Eagerly initialise the brain for the requested namespace
        _ensure_brain(namespace=ns_hint)
        result = {"protocolVersion":"2024-11-05",
                  "serverInfo":{"name":"mnemosyne-memory","version":"7.0.1"},
                  "capabilities":{"tools":{}}}
        if ns_hint:
            result["namespace"] = ns_hint
        return {"jsonrpc":"2.0","id":rid,"result":result}
    elif method == "tools/list":
        return {"jsonrpc":"2.0","id":rid,"result":handle_tools_list()}
    elif method == "tools/call":
        params = req.get("params", {})
        name = params.get("name","")
        arguments = params.get("arguments", {})
        result = handle_tools_call(name, arguments)
        return {"jsonrpc":"2.0","id":rid,"result":{"content":[{"type":"text","text":json.dumps(result,ensure_ascii=False)}]}}
    elif method == "notifications/initialized":
        return None
    return {"jsonrpc":"2.0","id":rid,"error":{"code":-32601,"message":f"Method not found: {method}"}}

def main():
    global DEFAULT_BRAIN, brain, _brain_dir, _default_namespace, _auth_token
    p = argparse.ArgumentParser(description="Mnemosyne MCP Server")
    p.add_argument("--brain-dir", default=DEFAULT_BRAIN, help="记忆库目录")
    p.add_argument("--namespace", default=None,
                   help="Default tenant namespace (default: 'default')")
    p.add_argument("--token", default=None,
                   help="MCP 鉴权令牌（等价于环境变量 MNEMOSYNE_MCP_TOKEN）")
    args = p.parse_args()
    _brain_dir = args.brain_dir
    _default_namespace = args.namespace or "default"
    _auth_token = args.token or os.environ.get("MNEMOSYNE_MCP_TOKEN")
    _ensure_brain(namespace=_default_namespace)
    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        try:
            resp = handle_request(json.loads(line))
            if resp:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except json.JSONDecodeError:
            pass

if __name__ == "__main__":
    main()
