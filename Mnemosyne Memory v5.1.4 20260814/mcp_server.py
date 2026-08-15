#!/usr/bin/env python3
"""
MCP (Model Context Protocol) Server for Mnemosyne Memory
Zero external dependencies — pure stdlib JSON-RPC over stdio.
Usage: python mcp_server.py [--brain-dir ~/.mnemosyne]
Supports: retain, recall, stats, graph_query, retain_batch, doctor, temporal_query, list_projects
"""
import sys, json, os, argparse
from mnemosyne import MemoryBrain

DEFAULT_BRAIN = os.path.expanduser("~/.mnemosyne")
brain = None

def _ensure_brain(brain_dir=None):
    global brain
    if brain_dir is None:
        brain_dir = DEFAULT_BRAIN
    if brain is None:
        brain = MemoryBrain(brain_dir)
        brain.ensure_init()
    return brain

# ---------- 8 tools ----------
TOOLS = [
    {"name":"retain","description":"写入记忆。content: 内容; mtype: semantic/episodic/procedural; project: 可选项目隔离",
     "inputSchema":{"type":"object","properties":{"content":{"type":"string"},"mtype":{"type":"string","enum":["semantic","episodic","procedural"],"default":"semantic"},"project":{"type":"string"}},"required":["content"]}},
    {"name":"recall","description":"检索记忆。query: 查询; k: 返回条数; project: 可选项目隔离",
     "inputSchema":{"type":"object","properties":{"query":{"type":"string"},"k":{"type":"integer","default":5},"project":{"type":"string"}},"required":["query"]}},
    {"name":"stats","description":"运行统计——写入/召回/Token节省等全维度",
     "inputSchema":{"type":"object","properties":{}}},
    {"name":"graph_query","description":"知识图谱查询",
     "inputSchema":{"type":"object","properties":{"entity":{"type":"string"}},"required":["entity"]}},
    {"name":"retain_batch","description":"批量写入（15x加速）",
     "inputSchema":{"type":"object","properties":{"items":{"type":"array","items":{"type":"object","properties":{"content":{"type":"string"},"mtype":{"type":"string","default":"semantic"}}}},"project":{"type":"string"}},"required":["items"]}},
    {"name":"doctor","description":"健康检查——扫描记忆库完整性、记录数、磁盘",
     "inputSchema":{"type":"object","properties":{}}},
    {"name":"temporal_query","description":"时序查询——按时间排序返回版本链",
     "inputSchema":{"type":"object","properties":{"entity":{"type":"string"}},"required":[]}},
    {"name":"list_projects","description":"列出所有项目名（多项目隔离）",
     "inputSchema":{"type":"object","properties":{}}}
]

def handle_tools_list():
    return {"tools": TOOLS}

def handle_tools_call(name, arguments):
    b = _ensure_brain()
    project = arguments.get("project", "")
    
    if name == "retain":
        content, mtype = arguments["content"], arguments.get("mtype", "semantic")
        b.retain(content, mtype=mtype, fast=True, project=project)
        return {"result": "已记住"}

    elif name == "recall":
        query, k = arguments["query"], arguments.get("k", 5)
        results = b.recall(query, k=k, project=project)
        out = []
        for r in results:
            try:
                out.append({"score": round(float(r[0]), 4), "content": (r[1].get("content","") if isinstance(r[1], dict) else str(r[1]))[:300], "type": r[1].get("type","semantic") if isinstance(r[1], dict) else "unknown", "created_at": r[1].get("created_at","") if isinstance(r[1], dict) else "", "version": r[1].get("version", 1) if isinstance(r[1], dict) else 1})
            except (ValueError, TypeError, IndexError):
                pass
        return {"results": out}

    elif name == "stats":
        return b.stats_tracker.summary() if b.stats_tracker else {}

    elif name == "graph_query":
        try: return b.graph_query(arguments["entity"])
        except Exception as e: return {"error": str(e)}

    elif name == "retain_batch":
        items = arguments["items"]
        batch = [(it["content"], it.get("mtype","semantic"), {}) for it in items]
        b.retain_batch(batch, fast=True)
        return {"result": f"批量写入 {len(items)} 条"}

    elif name == "doctor":
        try: return b.doctor()
        except Exception as e: return {"error": str(e)}

    elif name == "temporal_query":
        entity = arguments.get("entity")
        try: return {"results": b.temporal_query(entity=entity)}
        except Exception as e: return {"error": str(e)}

    elif name == "list_projects":
        try:
            projs = b.list_projects()
            return {"projects": projs, "count": len(projs)}
        except Exception as e: return {"error": str(e)}

    return {"error": f"Unknown tool: {name}"}

# ---------- JSON-RPC ----------
def handle_request(req):
    method = req.get("method", ""); rid = req.get("id")
    if method == "initialize":
        return {"jsonrpc":"2.0","id":rid,"result":{"protocolVersion":"2024-11-05","serverInfo":{"name":"mnemosyne-memory","version":"5.1.4"},"capabilities":{"tools":{}}}}
    elif method == "tools/list":
        return {"jsonrpc":"2.0","id":rid,"result":handle_tools_list()}
    elif method == "tools/call":
        params = req.get("params", {})
        result = handle_tools_call(params.get("name",""), params.get("arguments",{}))
        return {"jsonrpc":"2.0","id":rid,"result":{"content":[{"type":"text","text":json.dumps(result,ensure_ascii=False)}]}}
    elif method == "notifications/initialized":
        return None
    return {"jsonrpc":"2.0","id":rid,"error":{"code":-32601,"message":f"Method not found: {method}"}}

def main():
    global DEFAULT_BRAIN, brain
    p = argparse.ArgumentParser(description="Mnemosyne MCP Server")
    p.add_argument("--brain-dir", default=DEFAULT_BRAIN, help="记忆库目录")
    args = p.parse_args()
    DEFAULT_BRAIN = args.brain_dir
    _ensure_brain(args.brain_dir)
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
