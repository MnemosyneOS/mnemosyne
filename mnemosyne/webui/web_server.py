# -*- coding: utf-8 -*-
"""Mnemosyne 7.0.0 — Web 管理端（深度优化版）。

零依赖：仅使用 Python 标准库（http.server / json / os / sqlite3 / hashlib 等），
不引入 Flask、不使用任何 CDN，前端所有资源均为本地静态文件。

后端：Python ``http.server`` 提供 REST API + 会话认证 + 静态资源服务。
前端：``static/index.html`` + ``static/css/app.css`` + ``static/js/app.js``。

启动：
    python web_server.py --port 9090 --dir <记忆库目录> [--auth|--no-auth]
    或
    python -c "from web_server import run_server; run_server(port=9090, base_dir='<记忆库目录>')"

认证：
    默认开启（环境变量 MNEMOSYNE_WEB_AUTH 默认 "1"）。
    初始账号 admin / mnemosyne（首次运行时自动创建，密码使用 PBKDF2-HMAC-SHA256 + 盐）。
    旧测试套件 / 无头 API 消费方可用 --no-auth 或 MNEMOSYNE_WEB_AUTH=0 关闭鉴权。
"""

import io
import json
import os
import re
import sys
import time
import uuid
import shutil
import hashlib
import secrets
import shutil
import threading
import zipfile
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from http import cookies as http_cookies
from urllib.parse import urlparse, parse_qs, unquote
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
WEB_CONFIG_DIR = os.path.join(BASE_DIR, "web_config")
USERS_FILE = os.path.join(WEB_CONFIG_DIR, "users.json")
PROFILES_FILE = os.path.join(WEB_CONFIG_DIR, "profiles.json")
AGENT_CONFIG_FILE = os.path.join(WEB_CONFIG_DIR, "agent_config.json")
AGENTS_FILE = os.path.join(WEB_CONFIG_DIR, "agents.json")
SOURCES_FILE = os.path.join(WEB_CONFIG_DIR, "external_sources.json")
NAMESPACE_FILE = os.path.join(WEB_CONFIG_DIR, "namespace.json")

SESSION_COOKIE = "mnemosyne_sid"
SESSION_TTL = 8 * 3600          # 会话有效期 8 小时
MAX_FAILURES = 5                # 连续失败锁定阈值
LOCK_SECONDS = 60               # 锁定秒数
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 上传上限 50MB

_THEME = {
    "bg": "#0A0A0F",
    "neon_blue": "#00D4FF",
    "accent_purple": "#A855F7",
    "green": "#22C55E",
    "yellow": "#F59E0B",
    "red": "#EF4444",
}

# 进程内会话表 + 登录失败表（单进程 http.server 足够；重启即失效，符合安全预期）
_SESSIONS = {}      # token -> {"username", "role", "expires"}
_FAILURES = {}      # username -> {"count", "locked_until"}

_KNOWN_AGENTS = [
    "Openclaw", "Hermes Agent", "DeepSeek Harness", "Codex",
    "AutoGPT", "LangChain", "Claude Code", "Cursor", "Dify",
    "Coze", "n8n", "自定义 / 其他",
]


def _now_iso():
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).isoformat(timespec="seconds")


def _now_ts():
    return int(time.time())


# ---------------------------------------------------------------------------
# 认证与用户凭据（PBKDF2-HMAC-SHA256 + 盐，绝不明文）
# ---------------------------------------------------------------------------

def _hash_password(password, salt_hex=None):
    salt = salt_hex if salt_hex else secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt), 120_000)
    return salt, dk.hex()


def _ensure_users_file():
    """首次运行创建 web_config/users.json 与内置 admin 账号。"""
    if not os.path.exists(USERS_FILE):
        salt, h = _hash_password("mnemosyne")
        data = {"users": [{
            "username": "admin",
            "salt": salt,
            "hash": h,
            "role": "admin",
            "created_at": _now_iso(),
        }]}
        os.makedirs(WEB_CONFIG_DIR, exist_ok=True)
        _write_json_atomic(USERS_FILE, data)
        return data
    return _load_json(USERS_FILE) or {"users": []}


def _load_users():
    return (_ensure_users_file() or {}).get("users", [])


def _save_users(users):
    os.makedirs(WEB_CONFIG_DIR, exist_ok=True)
    _write_json_atomic(USERS_FILE, {"users": users})


def _verify_user(username, password):
    for u in _load_users():
        if u.get("username") == username:
            salt, h = _hash_password(password, u.get("salt"))
            return h == u.get("hash"), u
    # 用户名不存在也做一次哈希，避免时序侧信道
    _hash_password(password)
    return False, None


def _auth_enabled():
    val = os.environ.get("MNEMOSYNE_WEB_AUTH", "1")
    return val not in ("0", "off", "no", "false", "False")


# ---------------------------------------------------------------------------
# 通用 JSON 读写 / 原子写入
# ---------------------------------------------------------------------------

def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Brain 生命周期
# ---------------------------------------------------------------------------

def _get_brain():
    """获取或创建全局 brain 实例。"""
    base_dir = os.environ.get("MNEMOSYNE_DIR", os.path.expanduser("~/.mnemosyne"))
    namespace = os.environ.get("MNEMOSYNE_NAMESPACE", "default")
    if not hasattr(_get_brain, "_brain") or _get_brain._brain is None:
        from mnemosyne import MemoryBrain
        _get_brain._brain = MemoryBrain(
            base_dir=base_dir,
            enable_embeddings=False,
            enable_stats=False,
            namespace=namespace,
        )
        _get_brain._brain.ensure_init()
    return _get_brain._brain


def _set_brain(brain):
    _get_brain._brain = brain


def _reset_brain():
    brain = getattr(_get_brain, "_brain", None)
    if brain is not None:
        try:
            brain.close()
        except Exception:
            pass
    _get_brain._brain = None


# ---------------------------------------------------------------------------
# 响应发送
# ---------------------------------------------------------------------------

def _send_json(handler, data, status=200, cookie=None):
    body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    if cookie:
        handler.send_header("Set-Cookie",
                            f"{SESSION_COOKIE}={cookie}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}")
    handler.end_headers()
    handler.wfile.write(body)


def _send_bytes(handler, data, status=200, content_type="application/octet-stream",
                filename=None, extra_headers=None):
    if isinstance(data, str):
        data = data.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    if filename:
        handler.send_header("Content-Disposition",
                            f'attachment; filename="{filename}"')
    for k, v in (extra_headers or {}).items():
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(data)


def _get_body(handler):
    length = int(handler.headers.get("Content-Length", 0) or 0)
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def _parse_multipart(handler):
    """极简 multipart/form-data 解析（零依赖）。返回 [{name, filename, data(bytes)}]。"""
    ctype = handler.headers.get("Content-Type", "")
    if "multipart/form-data" not in ctype:
        return []
    m = re.search(r'boundary="?([^";\s]+)"?', ctype)
    if not m:
        return []
    boundary = m.group(1).strip()
    length = int(handler.headers.get("Content-Length", 0) or 0)
    raw = handler.rfile.read(length)
    delim = ("--" + boundary).encode("utf-8")
    parts = raw.split(delim)
    out = []
    for chunk in parts:
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        if b"\r\n\r\n" in chunk:
            head, body = chunk.split(b"\r\n\r\n", 1)
        elif b"\n\n" in chunk:
            head, body = chunk.split(b"\n\n", 1)
        else:
            continue
        head_str = head.decode("utf-8", "replace")
        name = filename = None
        nm = re.search(r'name="([^"]*)"', head_str)
        if nm:
            name = nm.group(1)
        fn = re.search(r'filename="([^"]*)"', head_str)
        if fn:
            filename = fn.group(1)
        body = body.rstrip(b"\r\n")
        out.append({"name": name, "filename": filename, "data": body})
    return out


# ---------------------------------------------------------------------------
# API 处理函数（保留原有函数名，兼容既有测试）
# ---------------------------------------------------------------------------

def _format_memory(record):
    ne = record.get("notary_evidence") or {}
    inj = ne.get("injection") or {}
    return {
        "id": record.get("id"),
        "content": record.get("content", ""),
        "mtype": record.get("mtype", record.get("type", "")),
        "confidence": record.get("confidence", 0),
        "importance": record.get("importance", 0),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "status": record.get("status", "active"),
        "tier": record.get("tier", "hot"),
        "entities": record.get("entities", []),
        "tags": record.get("tags", []),
        "merged_ids": record.get("merged_ids", []),
        "notary_evidence": ne,
        "flags": record.get("flags", []),
        "injection_suspicious": bool(inj.get("suspicious")),
        "injection_score": inj.get("score", 0),
        "access_count": record.get("access_count", 0),
        "last_accessed_at": record.get("last_accessed_at"),
        "project": record.get("project"),
    }


def api_stats(brain, query_params):
    """GET /api/stats — 增强版仪表盘统计（支持 ?agent= 智能体过滤）。"""
    project = _resolve_project(_agent_from_query(query_params))
    records = _filter_records_by_project(brain.store.all_records(), project)
    total = len(records)
    active = sum(1 for r in records if r.get("status", "active") in ("active", "working", None))
    deleted = sum(1 for r in records if r.get("status") in ("deleted", "archived"))

    tier_counts = {"hot": 0, "warm": 0, "cold": 0}
    for r in records:
        t = r.get("tier", "hot")
        tier_counts[t] = tier_counts.get(t, 0) + 1

    try:
        du = shutil.disk_usage(brain.base_dir)
        storage = {"total_mb": round(du.total / (1024 * 1024), 1),
                   "used_mb": round(du.used / (1024 * 1024), 1),
                   "free_mb": round(du.free / (1024 * 1024), 1)}
    except Exception:
        storage = {"total_mb": 0, "used_mb": 0, "free_mb": 0}

    cap = brain._status_info() if hasattr(brain, "_status_info") else {}

    ledger_hash, ledger_valid = None, True
    if brain.ledger:
        try:
            proof = brain.ledger.verify_chain()
            ledger_valid = proof.get("valid", True)
            entries = brain.ledger.get_entries(limit=1)
            if entries:
                ledger_hash = entries[0].get("hash")
        except Exception:
            pass

    audit_log = []
    if brain.ledger:
        try:
            entries = brain.ledger.get_entries(limit=10)
            audit_log = [{"seq": e.get("seq"), "action": e.get("action"),
                          "timestamp": e.get("ts") or e.get("timestamp")}
                         for e in entries]
        except Exception:
            pass

    suspicious = 0
    mtype_counts = {}
    for r in records:
        mt = r.get("mtype") or r.get("type") or "semantic"
        mtype_counts[mt] = mtype_counts.get(mt, 0) + 1
        ne = r.get("notary_evidence") or {}
        if (ne.get("injection") or {}).get("suspicious") or "suspicious" in (r.get("flags") or []):
            suspicious += 1

    return {
        "total_memories": total,
        "active_memories": active,
        "deleted_memories": deleted,
        "tier_counts": tier_counts,
        "storage_usage": storage,
        "capacity": cap,
        "ledger": {"latest_hash": ledger_hash, "valid": ledger_valid},
        "recent_audit": audit_log,
        "backend": brain.store_backend,
        "namespace": getattr(brain, "namespace", "default"),
        "suspicious_count": suspicious,
        "mtype_counts": mtype_counts,
        "max_active_memories": getattr(brain, "max_active_memories", None),
        "server_time": _now_iso(),
        "agent_project": project,
    }


def api_memories(brain, query_params, body=None):
    """GET /api/memories — 记忆列表（含检索，支持 ?agent= / ?project= 过滤）。"""
    action = query_params.get("action", ["list"])[0]
    project = _resolve_project(_agent_from_query(query_params))

    if action == "search":
        query = query_params.get("q", [""])[0]
        k = int(query_params.get("k", ["20"])[0])
        results = brain.recall(query, k=k, project=project)
        memories = []
        for score, record, reasons in results:
            memories.append({
                "id": record.get("id"),
                "content": record.get("content", "")[:200],
                "mtype": record.get("mtype", record.get("type", "")),
                "confidence": record.get("confidence", 0),
                "importance": record.get("importance", 0),
                "created_at": record.get("created_at"),
                "score": round(score, 4) if isinstance(score, (int, float)) else score,
                "tier": record.get("tier", "hot"),
                "status": record.get("status", "active"),
                "project": record.get("project"),
                "reasons": reasons if isinstance(reasons, list) else [],
            })
        return {"memories": memories, "count": len(memories), "agent_project": project}

    records = _filter_records_by_project(brain.store.all_records(), project)
    k = int(query_params.get("k", ["50"])[0])
    offset = int(query_params.get("offset", ["0"])[0])
    tier = query_params.get("tier", [None])[0]
    if tier:
        records = [r for r in records if r.get("tier") == tier]
    memories = [_format_memory(r) for r in records[offset:offset + k]]
    return {"memories": memories, "count": len(memories), "total": len(records),
            "agent_project": project}


def api_memories_post(brain, body):
    """POST /api/memories — 新增记忆（请求体可含 agent / project 字段）。"""
    content = (body or {}).get("content", "")
    if not content:
        return {"error": "content is required"}, 400
    # 智能体隔离：优先 project 字段，其次 agent 字段
    project = (body or {}).get("project")
    if not project and (body or {}).get("agent"):
        project = _resolve_project((body or {}).get("agent"))
    mid = brain.retain(
        content,
        mtype=body.get("mtype", "semantic"),
        tags=body.get("tags"),
        importance=body.get("importance"),
        confidence=body.get("confidence"),
        project=project,
    )
    return {"id": mid, "message": "Memory added", "project": project}, 201


def api_memory_by_id(brain, memory_id, method, body=None):
    if method == "GET":
        record = brain.store.find_by_id(memory_id)
        if record is None:
            return {"error": "Memory not found"}, 404
        return {"memory": _format_memory(record)}, 200
    elif method == "PUT":
        updates = {}
        if "content" in body and body["content"]:
            updates["content"] = body["content"]
        for field in ("mtype", "confidence", "importance", "tags", "status", "tier"):
            if field in body:
                updates[field] = body[field]
        success = brain.store.update_by_id(memory_id, updates)
        if not success:
            return {"error": "Memory not found"}, 404
        return {"message": "Memory updated"}, 200
    elif method == "DELETE":
        success = brain.store.update_by_id(memory_id, {"status": "deleted"})
        if not success:
            return {"error": "Memory not found"}, 404
        brain._ledger_append("forget", memory_id, {"source": "web_ui"})
        return {"message": "Memory deleted"}, 200
    return {"error": "Method not allowed"}, 405


def api_consolidate(brain, body):
    report = brain.consolidate(
        min_similarity=(body or {}).get("min_similarity", 0.75),
        max_group=(body or {}).get("max_group", 8),
        dry_run=(body or {}).get("dry_run", False),
        generate_summary=(body or {}).get("generate_summary", True),
    )
    report_dict = report.to_dict() if hasattr(report, "to_dict") else {}
    return {"report": report_dict}, 200


def api_demote(brain, body):
    """POST /api/demote — 单条降级。"""
    memory_id = (body or {}).get("memory_id")
    if not memory_id:
        return {"error": "memory_id is required"}, 400
    record = brain.store.find_by_id(memory_id)
    if record is None:
        return {"error": "Memory not found"}, 404
    current_tier = record.get("tier", "hot")
    tier_order = ["hot", "warm", "cold"]
    new_tier = tier_order[min(tier_order.index(current_tier) + 1, 2)] if current_tier in tier_order else "warm"
    brain.store.update_by_id(memory_id, {"tier": new_tier})
    brain._ledger_append("demote", memory_id, {"old_tier": current_tier, "new_tier": new_tier})
    if hasattr(brain, "_check_capacity"):
        brain._check_capacity()
    return {"message": "Memory demoted", "old_tier": current_tier, "new_tier": new_tier}, 200


def api_demote_cycle(brain, body):
    """POST /api/demote-cycle — 遗忘经济学：批量降级并返回报告。"""
    budget = int((body or {}).get("budget_bytes", 0) or 0)
    report = brain.demote_cycle(budget_bytes=budget)
    d = report.to_dict() if hasattr(report, "to_dict") else {}
    return {"report": d, "message": "降级周期完成"}, 200


def api_budget_recall(brain, body):
    """POST /api/budget-recall — 记忆预算器：预算约束检索 + cost_report。"""
    query = (body or {}).get("query", "")
    budget = int((body or {}).get("budget_tokens", 500) or 500)
    k = int((body or {}).get("k", 10) or 10)
    if not query:
        return {"error": "query is required"}, 400
    results, cost_report = brain.recall(query, k=k, budget_tokens=budget)
    out = []
    for score, record, reasons in results:
        out.append({
            "id": record.get("id"),
            "content": record.get("content", "")[:300],
            "mtype": record.get("mtype", record.get("type", "")),
            "confidence": record.get("confidence", 0),
            "importance": record.get("importance", 0),
            "score": round(score, 4) if isinstance(score, (int, float)) else score,
            "tokens": len(record.get("content", "")) // 4,
        })
    return {"results": out, "cost_report": cost_report}, 200


def api_notary(brain, query_params):
    """GET /api/notary — 记忆公证所：confidence + 注入检测 flags（支持 ?agent=）。"""
    limit = int(query_params.get("limit", ["100"])[0])
    project = _resolve_project(_agent_from_query(query_params))
    records = _filter_records_by_project(brain.store.all_records(), project)
    items = []
    for r in records[:limit]:
        ne = r.get("notary_evidence") or {}
        inj = ne.get("injection") or {}
        suspicious = bool(inj.get("suspicious")) or "suspicious" in (r.get("flags") or [])
        items.append({
            "id": r.get("id"),
            "content": r.get("content", "")[:160],
            "confidence": r.get("confidence", 0),
            "verification": r.get("verification", "unverified"),
            "injection_score": inj.get("score", 0),
            "injection_flags": inj.get("flags", []),
            "flags": r.get("flags", []),
            "suspicious": suspicious,
            "created_at": r.get("created_at"),
            "tier": r.get("tier", "hot"),
        })
    suspicious = [i for i in items if i["suspicious"]]
    return {"items": items, "count": len(items), "suspicious_count": len(suspicious)}, 200


def api_audit(brain, query_params):
    """GET /api/audit — 审计日志时间线（支持 ?agent= 按记忆归属过滤）。"""
    memory_id = query_params.get("memory_id", [None])[0]
    limit = int(query_params.get("limit", ["100"])[0])
    project = _resolve_project(_agent_from_query(query_params))
    if brain.ledger:
        entries = brain.ledger.get_entries(limit=limit)
        if memory_id:
            entries = [e for e in entries
                       if e.get("data_summary", {}).get("memory_id") == memory_id
                       or e.get("data_summary", {}).get("id") == memory_id
                       or e.get("memory_id") == memory_id]
        elif project:
            ids = _agent_memory_ids(brain, project)
            entries = [e for e in entries
                       if (e.get("memory_id") or e.get("data_summary", {}).get("memory_id")
                           or e.get("data_summary", {}).get("id")) in ids]
        return {"entries": entries, "count": len(entries)}
    return {"entries": [], "count": 0}


def api_ledger_entries(brain, query_params):
    """GET /api/ledger/entries — 账本最近条目。"""
    limit = int(query_params.get("limit", ["50"])[0])
    if brain.ledger:
        entries = brain.ledger.get_entries(limit=limit)
        total = brain.ledger.count() if hasattr(brain.ledger, "count") else len(entries)
        return {"entries": entries, "count": len(entries), "total": total}, 200
    return {"entries": [], "count": 0, "total": 0}, 200


def api_ledger_verify(brain, query_params):
    if not brain.ledger:
        return {"valid": True, "message": "No ledger"}, 200
    try:
        proof = brain.ledger.verify_chain()
        entries = brain.ledger.get_entries(limit=1)
        latest_hash = entries[0].get("hash") if entries else None
        return {
            "valid": proof.get("valid", True),
            "latest_hash": latest_hash,
            "proof_depth": proof.get("total", 0),
            "total_entries": proof.get("total", 0),
            "verified": proof.get("valid", True),
            "details": proof.get("details", ""),
        }, 200
    except Exception as e:
        return {"valid": False, "error": str(e)}, 500


def api_sessions(brain, query_params):
    """GET /api/sessions — 会话历史（列表 / 检索 / 单会话轮次）。

    底层 SessionStore 未提供 list_sessions()，因此“会话列表”通过标准
    search_conversations("") 的 LIKE 空串匹配得到最近轮次后聚合而来（仅用公开 API）。
    """
    session_id = query_params.get("session_id", [None])[0]
    query = query_params.get("q", [None])[0]
    k = int(query_params.get("k", ["50"])[0])

    if query:
        results = brain.search_conversations(query, session_id=session_id, k=k)
        return {"results": results, "count": len(results)}
    if session_id:
        try:
            rows = brain.session_store.get_session(session_id)
            return {"turns": rows, "count": len(rows)}
        except Exception as e:
            return {"error": str(e)}, 400
    try:
        # 用公开检索 API 取最近轮次（空串走 LIKE '%%' 全量匹配），再按会话聚合
        turns = brain.search_conversations("", k=500) or []
        agg = {}
        for t in turns:
            sid = t.get("session_id") or "unknown"
            agg[sid] = agg.get(sid, 0) + 1
        sessions = [{"session_id": sid, "turn_count": n} for sid, n in agg.items()]
        sessions.sort(key=lambda s: -s["turn_count"])
        return {"sessions": sessions, "count": len(sessions)}
    except Exception:
        return {"sessions": [], "count": 0}


def api_sessions_turns(brain, query_params):
    """GET /api/sessions/turns — 最近会话轮次（实时刷新）。"""
    limit = int(query_params.get("limit", ["50"])[0])
    try:
        turns = brain.search_conversations("", k=limit) or []
        out = []
        for r in turns:
            if isinstance(r, dict):
                out.append({
                    "session_id": r.get("session_id"),
                    "role": r.get("role"),
                    "content": (r.get("content") or "")[:200],
                    "ts": r.get("ts") or r.get("created_at"),
                })
        return {"turns": out, "count": len(out)}, 200
    except Exception:
        return {"turns": [], "count": 0}, 200


def api_session_turn_post(brain, body):
    """POST /api/session/turn — 追加一条会话轮次。"""
    session_id = (body or {}).get("session_id") or "web-session"
    role = (body or {}).get("role", "user")
    content = (body or {}).get("content", "")
    if not content:
        return {"error": "content is required"}, 400
    ts = brain.add_conversation_turn(session_id, role, content)
    return {"message": "turn added", "ts": ts}, 200


def api_graph(brain, query_params):
    """GET /api/graph — 实体关系图（支持 ?agent= 过滤）。"""
    entity = query_params.get("entity", [None])[0]
    depth = int(query_params.get("depth", ["2"])[0])
    project = _resolve_project(_agent_from_query(query_params))

    if entity:
        try:
            res = brain.graph_query(entity, depth=depth)
            nodes = [{"id": n, "label": n, "type": "entity"} for n in res.get("nodes", [])]
            edges = [{"source": e.get("from"), "target": e.get("to"),
                      "relation": e.get("relation", "related")} for e in res.get("edges", [])]
            return {"nodes": nodes, "edges": edges}, 200
        except Exception:
            return {"nodes": [], "edges": []}, 200

    try:
        records = _filter_records_by_project(brain.store.all_records(), project)
        nodes, edges, node_set = [], [], set()
        for r in records[:100]:
            ents = r.get("entities", [])
            for e in ents[:3]:
                if e and e not in node_set:
                    nodes.append({"id": e, "label": e, "type": "entity", "memory_id": r.get("id")})
                    node_set.add(e)
            for i in range(len(ents[:3])):
                for j in range(i + 1, len(ents[:3])):
                    edges.append({"source": ents[i], "target": ents[j], "memory_id": r.get("id")})
        return {"nodes": nodes, "edges": edges}, 200
    except Exception:
        return {"nodes": [], "edges": []}, 200


def api_graph_timeline(brain, query_params):
    """GET /api/graph/timeline — 径向时间图数据（支持 ?agent=）。

    数据映射层（业务）：对齐 Hermes agent/learning_graph.py 的 build_learning_graph：
      · skill 节点 = 技能类记忆（procedural/lesson/strategy）；memory 节点 = 其余记忆
      · 边：skill↔skill（共享实体，代理 related_skills）+ memory→skill（词法重叠 top4）
      · 时间：返回 ts（epoch 秒）与 created_at，供前端 computeRecency 计算环形半径
    """
    project = _resolve_project(_agent_from_query(query_params))
    try:
        records = _filter_records_by_project(brain.store.all_records(), project)
    except Exception:
        return {"nodes": [], "edges": [], "dates": [], "total": 0}, 200

    def _first_line_label(content, n=34):
        """取内容首行的可读标签（去掉 markdown 标记），供星图节点名使用。"""
        try:
            line = next((ln.strip() for ln in str(content).splitlines() if ln.strip()), "")
        except Exception:
            line = str(content)
        if not line:
            return line
        # 去掉行内 markdown 强调/代码标记，保留可读文本
        for pat in ("**", "`", "##", "###", "####", "- ", "* "):
            line = line.replace(pat, "")
        line = line.strip()
        if len(line) > n:
            line = line[: n - 1].rstrip() + "…"
        return line

    SKILL_TYPES = {"procedural", "lesson", "strategy"}
    nodes, node_ids = [], set()
    for r in records:
        if not r:
            continue
        rid = r.get("id")
        if not rid or rid in node_ids or r.get("status") == "deleted":
            continue
        mtype = r.get("type") or r.get("mtype") or "semantic"
        created = r.get("created_at") or r.get("knowledge_time") or ""
        ts = None
        if created:
            try:
                ts = datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = None
        nodes.append({
            "id": rid,
            # 标签用内容首行缩略（技能如“[工具调用] read_file”，记忆如首行标题），
            # 供悬停星座图的邻接节点名使用（Hermes 用 label 作星图喷字）。
            "label": _first_line_label(r.get("content") or "", 34),
            "content": r.get("content") or "",
            "kind": "skill" if mtype in SKILL_TYPES else "memory",
            "type": "skill" if mtype in SKILL_TYPES else "memory",
            "mtype": mtype,
            "created_at": created,
            "date": created[:10] if created else "",
            "ts": ts,
            "importance": int(r.get("importance") or 0),
            "use_count": int(r.get("access_count") or 0),
            "state": r.get("status", "active") or "active",
            "entities": list((r.get("entities") or [])[:5]),
        })
        node_ids.add(rid)

    from mnemosyne.utils import _tokenize
    _STOP = set("的了着过吗呢啊在是的有和与或就把被让从到对向为因所以这那其之而及很也都就")

    def _bigrams(content):
        out = set()
        try:
            for t in _tokenize(content):
                if len(t) == 2 and t[0] not in _STOP and t[1] not in _STOP and t[0].isalnum() and t[1].isalnum():
                    out.add(t)
        except Exception:
            pass
        return out

    # 边构建（对齐 Hermes learning_graph.py build_edges / _memory_skill_edges）
    skills = [n for n in nodes if n["type"] == "skill"]
    memories = [n for n in nodes if n["type"] == "memory"]
    edges, edge_set = [], set()

    def _add_edge(a, b, rel):
        key = tuple(sorted((a, b)))
        if key in edge_set:
            return
        edge_set.add(key)
        edges.append({"source": a, "target": b, "relation": rel})

    # ① skill↔skill：共享实体（Mnemosyne 代理 Hermes related_skills 声明式关联）。
    # Hermes related_skills 是稀疏的（每个技能显式列出少数相关技能），因此这里
    # 每个 skill 只连共享实体数最多的 top-3，避免共享高频实体时退化成完全图。
    skill_ent = {s["id"]: set(s["entities"]) for s in skills}
    for si in skills:
        scored = []
        for sj in skills:
            if si["id"] == sj["id"]:
                continue
            common = skill_ent[si["id"]] & skill_ent[sj["id"]]
            if common:
                scored.append((len(common), sj["id"], sorted(common)[0]))
        scored.sort(key=lambda x: -x[0])
        for _, sid, rel in scored[:3]:
            _add_edge(si["id"], sid, rel)

    # ② memory→skill：词法重叠打分（共享实体/二元组交集），每 memory 取 top4（对齐 _memory_skill_edges）
    skill_terms = {}
    for s in skills:
        skill_terms[s["id"]] = set(s["entities"]) | _bigrams(s.get("content", ""))
    for m in memories:
        mterms = set(m["entities"]) | _bigrams(m.get("content", ""))
        scored = []
        for sid, sterms in skill_terms.items():
            overlap = len(mterms & sterms)
            if overlap:
                scored.append((overlap, sid))
        scored.sort(key=lambda x: -x[0])
        for _, sid in scored[:4]:
            _add_edge(m["id"], sid, "related")

    dates = sorted({n["date"] for n in nodes if n["date"]})
    return {"nodes": nodes, "edges": edges, "dates": dates, "total": len(nodes)}, 200


def api_tree(brain, query_params):
    """GET /api/tree — 知识树（实体 → 关联记忆，可展开；支持 ?agent=）。"""
    limit = int(query_params.get("limit", ["80"])[0])
    project = _resolve_project(_agent_from_query(query_params))
    records = _filter_records_by_project(brain.store.all_records(), project)
    entity_map = {}
    for r in records[:limit]:
        if r.get("status") == "deleted":
            continue
        for e in (r.get("entities") or []):
            entity_map.setdefault(e, []).append({
                "id": r.get("id"),
                "content": (r.get("content") or "")[:80],
                "mtype": r.get("mtype", r.get("type", "")),
                "confidence": r.get("confidence", 0),
            })
    tree = []
    for entity, mems in sorted(entity_map.items(), key=lambda kv: -len(kv[1])):
        tree.append({
            "id": "entity|" + entity,
            "label": entity,
            "type": "entity",
            "count": len(mems),
            "children": [{"id": "mem|" + m["id"], "label": m["content"], "type": "memory",
                          "mtype": m["mtype"], "confidence": m["confidence"]} for m in mems[:6]],
        })
    return {"tree": tree, "count": len(tree), "total_memories": len(records)}, 200


def api_profile(brain, method, body=None, query_params=None):
    """GET/PUT /api/profile — 底层用户画像（兼容原有接口）。"""
    profile_id = "default" if query_params is None else query_params.get("id", ["default"])[0]
    if method == "GET":
        profile = brain.get_profile(profile_id)
        return {"profile": profile or {}}, 200
    elif method == "PUT":
        if not body:
            return {"error": "Profile data is required"}, 400
        brain.set_profile(profile_id, body)
        return {"message": "Profile updated"}, 200
    return {"error": "Method not allowed"}, 405


# ---------------------------------------------------------------------------
# 用户画像自定义管理（web_config/profiles.json，并同步到 brain 以注入快照）
# ---------------------------------------------------------------------------

def _load_profiles():
    data = _load_json(PROFILES_FILE)
    if isinstance(data, dict) and "profiles" in data:
        return data
    if isinstance(data, list):
        return {"profiles": data}
    return {"profiles": []}


def _save_profiles(data):
    os.makedirs(WEB_CONFIG_DIR, exist_ok=True)
    _write_json_atomic(PROFILES_FILE, data)


def _sync_profiles_to_brain(brain, profiles):
    """将画像同步到 brain.profile_manager，使快照注入（build_context_prompt）能读到。"""
    if brain.profile_manager is None:
        return
    try:
        for p in profiles:
            content = p.get("content", "")
            if not content:
                continue
            brain.set_profile("web_profile_" + p.get("id", str(uuid.uuid4().hex)[:8]), content)
        # 默认画像额外以固定 key 写入，便于快照中识别
        default = next((p for p in profiles if p.get("is_default")), None)
        if default and default.get("content"):
            brain.set_profile("default_profile", default["content"])
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 智能体（agent）记忆隔离 —— 基于 MemoryBrain 的 project 字段
# ---------------------------------------------------------------------------

def _slugify(text):
    """把名称转成稳定的 ASCII 标识（project 值）。"""
    import unicodedata
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(c for c in s if c.isascii() and (c.isalnum() or c in "_-"))
    s = re.sub(r"[^a-zA-Z0-9_-]+", "_", s).strip("_").lower()
    return s or ("agent_" + uuid.uuid4().hex[:8])


def _load_agents():
    data = _load_json(AGENTS_FILE)
    if isinstance(data, dict) and "agents" in data:
        return data
    if isinstance(data, list):
        return {"agents": data}
    return {"agents": []}


def _save_agents(data):
    os.makedirs(WEB_CONFIG_DIR, exist_ok=True)
    _write_json_atomic(AGENTS_FILE, data)


def _load_sources():
    data = _load_json(SOURCES_FILE)
    if isinstance(data, dict) and "sources" in data:
        return data
    if isinstance(data, list):
        return {"sources": data}
    return {"sources": []}


def _save_sources(data):
    os.makedirs(WEB_CONFIG_DIR, exist_ok=True)
    _write_json_atomic(SOURCES_FILE, data)


def _resolve_project(agent_value):
    """把外部传入的 agent 标识解析为底层 project 值。

    - None / "" / "all" / "全部" → None（不过滤，即“全部智能体”）
    - 命中已注册智能体（按 id / name / project）→ 该智能体的 project
    - 未注册 → 视为隐式 project：``agent:<slug>``
    """
    if agent_value is None:
        return None
    v = str(agent_value).strip()
    if v == "" or v.lower() in ("all", "全部", "all_agents"):
        return None
    for a in _load_agents().get("agents", []):
        if v in (a.get("id"), a.get("name"), a.get("project")):
            return a.get("project")
    return "agent:" + _slugify(v)


def _agent_from_query(query_params):
    """从 query 参数中提取智能体标识（agent 或 project）。"""
    if query_params is None:
        return None
    v = query_params.get("agent", [None])[0]
    if v is None:
        v = query_params.get("project", [None])[0]
    return v


def _filter_records_by_project(records, project):
    """按 project 过滤记录（None 表示不过滤）。"""
    if not project:
        return records
    return [r for r in records if (r.get("project") or "") == project]


def _agent_memory_ids(brain, project):
    """返回某 project 下所有记忆 id 集合（用于账本/审计关联过滤）。"""
    if not project:
        return None
    return {r.get("id") for r in _filter_records_by_project(brain.store.all_records(), project)}


def api_profiles(brain, method, body=None, profile_id=None, action=None):
    data = _load_profiles()
    profiles = data.get("profiles", [])

    if method == "GET":
        default = next((p for p in profiles if p.get("is_default")), None)
        return {"profiles": profiles, "count": len(profiles),
                "default_id": default.get("id") if default else None}, 200

    if method == "POST" and action == "default":
        for p in profiles:
            p["is_default"] = (p.get("id") == profile_id)
        _save_profiles({"profiles": profiles})
        _sync_profiles_to_brain(brain, profiles)
        return {"message": "默认画像已更新", "default_id": profile_id}, 200

    if method == "POST":
        content = (body or {}).get("content", "")
        if not content:
            return {"error": "content is required"}, 400
        now = _now_iso()
        is_default = len(profiles) == 0
        item = {"id": uuid.uuid4().hex, "content": content, "created_at": now,
                "updated_at": now, "is_default": is_default}
        profiles.append(item)
        _save_profiles({"profiles": profiles})
        _sync_profiles_to_brain(brain, profiles)
        return {"profile": item, "message": "画像已添加"}, 201

    if method == "PUT":
        content = (body or {}).get("content", "")
        if not content:
            return {"error": "content is required"}, 400
        for p in profiles:
            if p.get("id") == profile_id:
                p["content"] = content
                p["updated_at"] = _now_iso()
        _save_profiles({"profiles": profiles})
        _sync_profiles_to_brain(brain, profiles)
        return {"message": "画像已更新"}, 200

    if method == "DELETE":
        removed_default = any(p.get("id") == profile_id and p.get("is_default") for p in profiles)
        profiles = [p for p in profiles if p.get("id") != profile_id]
        if removed_default and profiles:
            profiles[0]["is_default"] = True
        _save_profiles({"profiles": profiles})
        _sync_profiles_to_brain(brain, profiles)
        return {"message": "画像已删除"}, 200

    return {"error": "Method not allowed"}, 405


# ---------------------------------------------------------------------------
# 多租户
# ---------------------------------------------------------------------------

def api_namespaces(brain, query_params):
    base = brain.base_dir
    ns_root = os.path.join(base, "data", "namespaces")
    names = []
    if os.path.isdir(ns_root):
        try:
            for d in sorted(os.listdir(ns_root)):
                if os.path.isdir(os.path.join(ns_root, d)):
                    names.append(d)
        except Exception:
            pass
    current = getattr(brain, "namespace", "default") or "default"
    if current not in names:
        names.append(current)

    out = []
    for n in sorted(set(names)):
        count = 0
        try:
            from storage import SqliteBackend
            sb = SqliteBackend(base, namespace=n)
            sb.ensure_init()
            count = sb.count() if hasattr(sb, "count") else 0
            try:
                sb.close()
            except Exception:
                pass
        except Exception:
            count = 0
        out.append({"name": n, "count": count, "current": n == current})
    return {"namespaces": out, "current": current}, 200


def _sanitize_namespace(ns):
    """规范化命名空间名（防止路径穿越，仅允许字母/数字/下划线/点/连字符）。"""
    s = (ns or "").strip()
    s = re.sub(r"[^\w\-\.]", "_", s)
    s = s.strip("._")
    return s[:64]


def _load_persisted_namespace():
    """读取持久化的命名空间（web_config/namespace.json），供重启后仍生效。"""
    data = _load_json(NAMESPACE_FILE)
    if isinstance(data, dict) and data.get("namespace"):
        return _sanitize_namespace(data.get("namespace"))
    return None


def _persist_namespace(ns):
    os.makedirs(WEB_CONFIG_DIR, exist_ok=True)
    _write_json_atomic(NAMESPACE_FILE, {"namespace": _sanitize_namespace(ns),
                                        "updated_at": _now_iso()})


def api_namespace_switch(brain, body):
    ns = _sanitize_namespace((body or {}).get("namespace"))
    if not ns:
        return {"error": "namespace is required"}, 400
    os.environ["MNEMOSYNE_NAMESPACE"] = ns
    _persist_namespace(ns)
    _reset_brain()
    newbrain = _get_brain()
    return {"namespace": getattr(newbrain, "namespace", ns), "message": "命名空间已切换"}, 200


def api_namespace_rename(brain, body):
    """POST /api/namespace/rename — 重命名当前命名空间。

    通过公开 API brain.clone_namespace(current → new) 克隆全部记忆到新命名空间后切换，
    原命名空间保留（数据安全，可后续手动清理）。
    将新命名空间持久化到 web_config/namespace.json，重启后仍生效。
    """
    new_name = _sanitize_namespace((body or {}).get("namespace") or (body or {}).get("name"))
    if not new_name:
        return {"error": "请输入有效的新命名空间名称（字母/数字/下划线/点/连字符）"}, 400
    current = getattr(brain, "namespace", "default") or "default"
    if new_name == current:
        return {"message": "命名空间未变化", "namespace": current}, 200
    try:
        res = brain.clone_namespace(current, new_name)
    except Exception as e:
        return {"error": "克隆命名空间失败：" + str(e)}, 500
    os.environ["MNEMOSYNE_NAMESPACE"] = new_name
    _persist_namespace(new_name)
    _reset_brain()
    _get_brain()
    return {"message": "命名空间已重命名", "namespace": new_name,
            "old_namespace": current,
            "cloned_records": res.get("cloned_records", 0)}, 200


# ---------------------------------------------------------------------------
# 智能体（agent）管理 —— 每个智能体对应一个 project，实现记忆完全隔离
# ---------------------------------------------------------------------------

def _agent_counts(brain):
    """按 project 统计各智能体记忆条数 + 最近写入时间 + 最近7天写入数。"""
    records = brain.store.all_records()
    agents = _load_agents().get("agents", [])
    now = _now_ts()
    week_ago = now - 7 * 86400
    counts = {}
    recent7 = {}
    last_ts = {}
    for r in records:
        p = r.get("project")
        if not p:
            continue
        counts[p] = counts.get(p, 0) + 1
        created = r.get("created_at") or ""
        try:
            ts = datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp()
        except Exception:
            ts = 0
        if ts > last_ts.get(p, 0):
            last_ts[p] = ts
        if ts >= week_ago:
            recent7[p] = recent7.get(p, 0) + 1
    out = []
    for a in agents:
        p = a.get("project") or ""
        out.append({
            "id": a.get("id"),
            "name": a.get("name"),
            "project": p,
            "count": counts.get(p, 0),
            "recent7": recent7.get(p, 0),
            "last_activity": last_ts.get(p, 0),
            "created_at": a.get("created_at"),
            "updated_at": a.get("updated_at"),
        })
    return out, sum(counts.values())


def api_agents(brain, method, body=None, agent_id=None, query_params=None):
    """GET/POST/PUT/DELETE /api/agents —— 智能体配置 CRUD（含记忆计数）。"""
    data = _load_agents()
    agents = data.get("agents", [])

    if method == "GET":
        counts, total = _agent_counts(brain)
        return {"agents": counts, "count": len(counts), "total_memories": total}, 200

    if method == "POST":
        name = (body or {}).get("name", "").strip()
        if not name:
            return {"error": "智能体名称不能为空"}, 400
        # 去重
        for a in agents:
            if a.get("name") == name:
                return {"error": "同名智能体已存在"}, 409
        now = _now_iso()
        pid = uuid.uuid4().hex
        project = (body or {}).get("project") or ("agent:" + _slugify(name))
        item = {"id": pid, "name": name, "project": project,
                "created_at": now, "updated_at": now}
        agents.append(item)
        _save_agents({"agents": agents})
        return {"agent": item, "message": "智能体已添加"}, 201

    if method == "PUT":
        name = (body or {}).get("name", "").strip()
        for a in agents:
            if a.get("id") == agent_id:
                if name:
                    a["name"] = name
                    if not (body or {}).get("project"):
                        a["project"] = "agent:" + _slugify(name)
                if (body or {}).get("project"):
                    a["project"] = (body or {}).get("project")
                a["updated_at"] = _now_iso()
                _save_agents({"agents": agents})
                return {"agent": a, "message": "智能体已更新"}, 200
        return {"error": "智能体不存在"}, 404

    if method == "DELETE":
        new_agents = [a for a in agents if a.get("id") != agent_id]
        if len(new_agents) == len(agents):
            return {"error": "智能体不存在"}, 404
        _save_agents({"agents": new_agents})
        return {"message": "智能体已删除（记忆数据保留）"}, 200

    return {"error": "Method not allowed"}, 405


# ---------------------------------------------------------------------------
# 外部数据源（任务3）—— Web 端主动抓取外部智能体记忆
# ---------------------------------------------------------------------------

def _sync_source_fetch(brain, source):
    """从外部 HTTP API 或共享目录抓取记忆并写入本地。"""
    stype = source.get("type", "api")
    agent_proj = _resolve_project(source.get("agent"))
    if stype == "dir":
        path = source.get("path", "")
        if not path or not os.path.isdir(path):
            return {"error": "共享目录不存在或不可访问"}
        written = 0
        for fn in sorted(os.listdir(path)):
            if not (fn.endswith(".json") or fn.endswith(".jsonl")):
                continue
            fp = os.path.join(path, fn)
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    text = f.read()
            except Exception:
                continue
            written += _retain_from_text(brain, text, agent_proj)
        return {"written": written, "source": source.get("name")}
    # api 类型
    url = source.get("url", "")
    if not url:
        return {"error": "数据源 URL 为空"}
    import urllib.request as _ur
    req = _ur.Request(url, headers={"User-Agent": "MnemosyneWeb/7.0.0"})
    with _ur.urlopen(req, timeout=source.get("timeout", 10)) as resp:
        text = resp.read().decode("utf-8", "replace")
    written = _retain_from_text(brain, text, agent_proj)
    return {"written": written, "source": source.get("name")}


def _retain_from_text(brain, text, agent_proj):
    """把抓取到的文本（JSON 列表 / JSONL 行 / 纯文本）写入记忆，返回条数。"""
    written = 0
    text = (text or "").strip()
    if not text:
        return 0
    # 尝试 JSON 数组 / JSONL
    try:
        obj = json.loads(text)
        items = obj if isinstance(obj, list) else [obj]
        for it in items[:200]:
            content = it.get("content") if isinstance(it, dict) else str(it)
            if content and content.strip():
                brain.retain(str(content).strip(), mtype=(it.get("mtype") if isinstance(it, dict) else "note") or "note", project=agent_proj)
                written += 1
        return written
    except json.JSONDecodeError:
        pass
    # 逐行 JSON
    for line in text.splitlines()[:200]:
        line = line.strip()
        if not line:
            continue
        try:
            it = json.loads(line)
            content = it.get("content") if isinstance(it, dict) else str(it)
            if content and content.strip():
                brain.retain(str(content).strip(), mtype=(it.get("mtype") if isinstance(it, dict) else "note") or "note", project=agent_proj)
                written += 1
        except json.JSONDecodeError:
            # 纯文本行
            if line:
                brain.retain(line, mtype="note", project=agent_proj)
                written += 1
    if written == 0:
        # 兜底：整体作为一条
        brain.retain(text[:2000], mtype="note", project=agent_proj)
        written = 1
    return written


def api_sources(brain, method, body=None, source_id=None, action=None):
    """GET/POST/PUT/DELETE /api/sources 与 POST /api/sources/sync —— 外部数据源配置。"""
    data = _load_sources()
    sources = data.get("sources", [])

    if method == "GET":
        return {"sources": sources, "count": len(sources)}, 200

    if method == "POST" and action == "sync":
        sid = (body or {}).get("id")
        src = next((s for s in sources if s.get("id") == sid), None)
        if not src:
            return {"error": "数据源不存在"}, 404
        try:
            res = _sync_source_fetch(brain, src)
        except Exception as e:
            return {"error": "同步失败：" + str(e)}, 500
        src["last_sync"] = _now_iso()
        src["last_result"] = res
        _save_sources({"sources": sources})
        return {"result": res, "message": "同步完成"}, 200

    if method == "POST":
        name = (body or {}).get("name", "").strip()
        stype = (body or {}).get("type", "api")
        if not name:
            return {"error": "数据源名称不能为空"}, 400
        item = {
            "id": uuid.uuid4().hex,
            "name": name,
            "type": stype if stype in ("api", "dir") else "api",
            "url": (body or {}).get("url", ""),
            "path": (body or {}).get("path", ""),
            "agent": (body or {}).get("agent"),
            "enabled": bool((body or {}).get("enabled", True)),
            "interval": int((body or {}).get("interval", 60) or 60),
            "created_at": _now_iso(),
            "last_sync": None,
            "last_result": None,
        }
        sources.append(item)
        _save_sources({"sources": sources})
        return {"source": item, "message": "数据源已添加"}, 201

    if method == "PUT":
        for s in sources:
            if s.get("id") == source_id:
                for k in ("name", "type", "url", "path", "agent", "interval"):
                    if k in (body or {}):
                        s[k] = body[k]
                if "enabled" in (body or {}):
                    s["enabled"] = bool(body["enabled"])
                s["updated_at"] = _now_iso()
                _save_sources({"sources": sources})
                return {"source": s, "message": "数据源已更新"}, 200
        return {"error": "数据源不存在"}, 404

    if method == "DELETE":
        new_sources = [s for s in sources if s.get("id") != source_id]
        if len(new_sources) == len(sources):
            return {"error": "数据源不存在"}, 404
        _save_sources({"sources": new_sources})
        return {"message": "数据源已删除"}, 200

    return {"error": "Method not allowed"}, 405


def _background_sync_loop(brain):
    """后台定时同步外部数据源（默认 60s 间隔，失败静默重试）。"""
    while True:
        time.sleep(60)
        try:
            sources = _load_sources().get("sources", [])
            for s in sources:
                if not s.get("enabled"):
                    continue
                interval = max(10, int(s.get("interval", 60) or 60))
                last = s.get("last_sync") or ""
                # 简单按固定周期触发（不精确计时，足够演示）
                try:
                    res = _sync_source_fetch(brain, s)
                except Exception:
                    res = {"error": "sync failed"}
                s["last_sync"] = _now_iso()
                s["last_result"] = res
            _save_sources({"sources": sources})
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 记忆交换协议：导出 / 导入
# ---------------------------------------------------------------------------

def _make_tmp_dir(parent):
    """在可写目录下创建唯一临时目录（避免 tempfile.mkdtemp 在部分环境下的写入限制）。"""
    os.makedirs(parent, exist_ok=True)
    d = os.path.join(parent, ".web_tmp_" + uuid.uuid4().hex[:12])
    os.makedirs(d, exist_ok=True)
    return d


def api_export(brain, query_params):
    """GET /api/export?namespace=X — 导出 ZIP（memories.jsonl + manifest.json）。"""
    ns = query_params.get("namespace", [getattr(brain, "namespace", "default")])[0]
    tmp = _make_tmp_dir(brain.base_dir)
    try:
        res = brain.export_memories(tmp, ns)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname in ("memories.jsonl", "manifest.json"):
                fp = os.path.join(tmp, fname)
                if os.path.exists(fp):
                    zf.write(fp, fname)
        return buf.getvalue(), res.get("record_count", 0), ns
    except Exception as e:
        return {"error": str(e)}, 500
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def api_import(brain, body_bytes, filename):
    """POST /api/import — 导入 JSONL 或 ZIP（含 manifest）。"""
    tmp = _make_tmp_dir(brain.base_dir)
    try:
        name = (filename or "").lower()
        if name.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(body_bytes)) as zf:
                zf.extractall(tmp)
        else:
            with open(os.path.join(tmp, "memories.jsonl"), "wb") as f:
                f.write(body_bytes)
        res = brain.import_memories(tmp, "default")
        return res, 200
    except FileNotFoundError as e:
        return {"error": str(e), "imported": 0}, 400
    except Exception as e:
        return {"error": str(e), "imported": 0}, 500
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 知识库上传（.txt / .md 等文本文件 → brain.retain）
# ---------------------------------------------------------------------------

def _chunk_text(text, size=800):
    text = text.strip()
    if len(text) <= size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            window = text[start:end]
            for sep in ("\n\n", "\n", "。", "！", "？", "；", ". "):
                idx = window.rfind(sep)
                if idx > size // 2:
                    end = start + idx + len(sep)
                    break
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end
    return chunks


def api_knowledge_upload(brain, part):
    """POST /api/knowledge/upload — 解析文本并按块写入记忆。"""
    filename = (part.get("filename") or "").lower()
    ext = os.path.splitext(filename)[1]
    supported = (".txt", ".md", ".markdown", ".csv", ".json")
    if ext not in supported:
        return {"error": f"暂不支持 {ext or '未知'} 格式，请上传 .txt / .md / .csv / .json"}, 400
    try:
        text = part.get("data", b"").decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = part.get("data", b"").decode("gbk", "replace")
        except Exception:
            return {"error": "文件编码无法识别，请使用 UTF-8 编码"}, 400

    if not text.strip():
        return {"error": "文件内容为空"}, 400

    if ext == ".json":
        # JSON 文件：若为记忆列表则逐条写入，否则整体作为一条
        try:
            obj = json.loads(text)
        except Exception:
            obj = None
        if isinstance(obj, list):
            written = 0
            for item in obj[:500]:
                content = item.get("content") if isinstance(item, dict) else str(item)
                if content and content.strip():
                    brain.retain(str(content).strip(), mtype=item.get("mtype", "note") if isinstance(item, dict) else "note")
                    written += 1
            return {"written": written, "chunks": written, "filename": filename}, 200

    mtype = "note"
    if ext == ".md":
        mtype = "semantic"
    chunks = _chunk_text(text)
    written = 0
    for c in chunks[:200]:
        try:
            brain.retain(c, mtype=mtype, tags=["knowledge_base", filename[:20]])
            written += 1
        except Exception:
            continue
    return {"written": written, "chunks": len(chunks), "filename": filename}, 200


# ---------------------------------------------------------------------------
# Agent 对接偏好 / 配置导入导出
# ---------------------------------------------------------------------------

def api_agent_config(brain, method, body=None):
    if method == "GET":
        data = _load_json(AGENT_CONFIG_FILE) or {"agent": None, "updated_at": None}
        return {"agent_config": data, "agents": _KNOWN_AGENTS}, 200
    if method == "POST":
        agent = (body or {}).get("agent")
        cfg = {"agent": agent, "updated_at": _now_iso()}
        os.makedirs(WEB_CONFIG_DIR, exist_ok=True)
        _write_json_atomic(AGENT_CONFIG_FILE, cfg)
        return {"agent_config": cfg, "message": "Agent 偏好已保存（自动配置将在后续版本提供）"}, 200
    return {"error": "Method not allowed"}, 405


def api_config_export(brain, query_params):
    """GET /api/config/export — 导出 Web 端配置（画像 + Agent 偏好 + 模式）。"""
    profiles = _load_profiles()
    agent = _load_json(AGENT_CONFIG_FILE) or {}
    payload = {
        "kind": "mnemosyne-web-config",
        "exported_at": _now_iso(),
        "profiles": profiles.get("profiles", []),
        "agent_config": agent,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2), "web_config.json"


def api_config_import(brain, body):
    """POST /api/config/import — 导入 Web 端配置（JSON）。"""
    if isinstance(body, dict) and "profiles" in body:
        profiles = body.get("profiles", [])
        _save_profiles({"profiles": profiles})
        _sync_profiles_to_brain(brain, profiles)
        agent = body.get("agent_config")
        if agent:
            os.makedirs(WEB_CONFIG_DIR, exist_ok=True)
            _write_json_atomic(AGENT_CONFIG_FILE, agent)
        return {"message": "配置已导入", "profiles": len(profiles)}, 200
    return {"error": "无效的配置文件"}, 400


# ---------------------------------------------------------------------------
# 热力图 / 长期画像
# ---------------------------------------------------------------------------

def _date_only(iso):
    return (iso or "")[:10]


def api_heatmap(brain, query_params):
    """GET /api/heatmap — 日历热力图（每日新增量 / 调用量；支持 ?agent=）。"""
    project = _resolve_project(_agent_from_query(query_params))
    records = _filter_records_by_project(brain.store.all_records(), project)
    added, accessed = {}, {}
    for r in records:
        d = _date_only(r.get("created_at"))
        if d:
            added[d] = added.get(d, 0) + 1
        la = _date_only(r.get("last_accessed_at"))
        if la:
            accessed[la] = accessed.get(la, 0) + 1

    today = datetime.now(timezone(timedelta(hours=8)))
    days = []
    distinct = set()
    for i in range(364, -1, -1):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        a = added.get(d, 0)
        ac = accessed.get(d, 0)
        if a or ac:
            distinct.add(d)
        days.append({"date": d, "added": a, "accessed": ac})

    dates = sorted({_date_only(r.get("created_at")) for r in records if r.get("created_at")})
    span_days = 0
    if dates:
        try:
            first = datetime.strptime(dates[0], "%Y-%m-%d")
            span_days = max(0, (today - first).days)
        except Exception:
            span_days = 0

    return {
        "days": days,
        "span_days": span_days,
        "active_days": len(distinct),
        # 实时生成：只要有任意一天存在数据即视为可展示（不再等待 5 天）
        "enough_data": True,
        "min_days": 0,
        "total_added": sum(added.values()),
        "total_accessed": sum(accessed.values()),
    }, 200


def api_insights(brain, query_params):
    """GET /api/insights — 长期画像文字描述（基于现有记忆数据；支持 ?agent=）。"""
    project = _resolve_project(_agent_from_query(query_params))
    records = _filter_records_by_project(brain.store.all_records(), project)
    entity_freq, mtype_freq, hour_freq, dow_freq = {}, {}, {}, {}
    for r in records:
        for e in (r.get("entities") or [])[:5]:
            entity_freq[e] = entity_freq.get(e, 0) + 1
        mt = r.get("mtype", r.get("type", "semantic"))
        mtype_freq[mt] = mtype_freq.get(mt, 0) + 1
        ts = r.get("created_at") or r.get("knowledge_time")
        if ts:
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                hour_freq[dt.hour] = hour_freq.get(dt.hour, 0) + 1
                dow_freq[dt.weekday()] = dow_freq.get(dt.weekday(), 0) + 1
            except Exception:
                pass

    top_entities = sorted(entity_freq.items(), key=lambda kv: -kv[1])[:5]
    top_mtypes = sorted(mtype_freq.items(), key=lambda kv: -kv[1])[:5]
    top_hour = max(hour_freq.items(), key=lambda kv: kv[1]) if hour_freq else None
    top_dow = max(dow_freq.items(), key=lambda kv: kv[1]) if dow_freq else None
    dow_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

    parts = []
    if top_entities:
        parts.append("您最常关注的主题是「" + "、".join(e for e, _ in top_entities[:3]) + "」。")
    if top_mtypes:
        parts.append("记忆类型以「" + top_mtypes[0][0] + "」为主。")
    if top_hour:
        parts.append(f"最活跃时间段约为 {top_hour[0]}:00 - {top_hour[0] + 2}:00。")
    if top_dow is not None:
        parts.append("最活跃的星期是" + dow_names[top_dow[0]] + "。")

    return {
        "description": "；".join(parts) if parts else "数据积累中，暂无足够信息生成画像。",
        "top_entities": [{"name": e, "count": c} for e, c in top_entities],
        "top_mtypes": [{"type": t, "count": c} for t, c in top_mtypes],
        "top_hour": top_hour[0] if top_hour else None,
        "top_dow": dow_names[top_dow[0]] if top_dow is not None else None,
        "total_memories": len(records),
    }, 200


def api_snapshot(brain, query_params):
    """GET /api/snapshot — 快照注入预览（验证默认画像是否进入上下文）。"""
    max_chars = int(query_params.get("max_chars", ["2000"])[0])
    try:
        content = brain.build_context_prompt(max_chars=max_chars)
    except Exception as e:
        content = "[快照构建失败] " + str(e)
    return {"snapshot": content or "[快照为空]"}, 200


# ---------------------------------------------------------------------------
# 前端 HTML（供测试直接调用；实际由静态文件服务）
# ---------------------------------------------------------------------------

_index_cache = {"mtime": 0, "content": ""}


def get_frontend_html():
    """返回前端 HTML（读取 static/index.html，带 mtime 缓存）。"""
    path = os.path.join(STATIC_DIR, "index.html")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return "<!DOCTYPE html><html><body>index.html missing</body></html>"
    if _index_cache["mtime"] != mtime or not _index_cache["content"]:
        with open(path, "r", encoding="utf-8") as f:
            _index_cache["content"] = f.read()
        _index_cache["mtime"] = mtime
    return _index_cache["content"]


# ---------------------------------------------------------------------------
# HTTP 请求处理器
# ---------------------------------------------------------------------------

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
}


class MnemosyneWebHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器：静态资源 + 会话认证 + REST API。"""

    server_version = "MnemosyneWeb/7.0.0"

    def log_message(self, format, *args):
        pass

    # ---- 认证 ----

    def _get_session(self):
        raw = self.headers.get("Cookie", "")
        if not raw:
            return None
        try:
            c = http_cookies.SimpleCookie(raw)
            token = c.get(SESSION_COOKIE)
            if not token:
                return None
            sess = _SESSIONS.get(token.value)
            if not sess:
                return None
            if sess.get("expires", 0) < _now_ts():
                _SESSIONS.pop(token.value, None)
                return None
            return sess
        except Exception:
            return None

    def _set_session_cookie(self, token):
        self.send_header("Set-Cookie",
                         f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}")

    def _clear_session_cookie(self):
        self.send_header("Set-Cookie",
                         f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")

    def _auth_status(self):
        sess = self._get_session()
        if sess:
            return {"authenticated": True, "username": sess.get("username"),
                    "role": sess.get("role")}
        return {"authenticated": False, "username": None, "role": None}

    def _is_auth_exempt(self, method, path):
        if not _auth_enabled():
            return True
        if method == "GET" and path in ("/api/auth/status", "/api/health"):
            return True
        if method == "POST" and path == "/api/auth/login":
            return True
        return False

    def _require_auth(self, method, path):
        if self._is_auth_exempt(method, path):
            return True
        if not _auth_enabled():
            return True
        sess = self._get_session()
        if sess:
            return True
        _send_json(self, {"error": "未登录或会话已过期", "code": "unauthorized"}, 401)
        return False

    # ---- 登录 / 登出 / 改密 ----

    def _handle_login(self, body):
        username = (body or {}).get("username", "")
        password = (body or {}).get("password", "")
        if not username or not password:
            return _send_json(self, {"error": "请输入用户名和密码"}, 400)

        # 简单防爆破：连续失败锁定
        fail = _FAILURES.get(username)
        if fail and fail.get("locked_until", 0) > _now_ts():
            remain = fail["locked_until"] - _now_ts()
            return _send_json(self, {"error": f"尝试次数过多，请 {remain} 秒后再试"}, 429)

        ok, user = _verify_user(username, password)
        if not ok:
            fail = _FAILURES.get(username, {"count": 0, "locked_until": 0})
            fail["count"] = fail.get("count", 0) + 1
            if fail["count"] >= MAX_FAILURES:
                fail["locked_until"] = _now_ts() + LOCK_SECONDS
                fail["count"] = 0
            _FAILURES[username] = fail
            return _send_json(self, {"error": "用户名或密码错误"}, 401)

        _FAILURES.pop(username, None)
        token = secrets.token_urlsafe(32)
        _SESSIONS[token] = {"username": user.get("username"), "role": user.get("role", "admin"),
                            "expires": _now_ts() + SESSION_TTL}
        _send_json(self, {"authenticated": True, "username": user.get("username"),
                          "role": user.get("role", "admin")}, 200, cookie=token)

    def _handle_logout(self):
        sess = self._get_session()
        raw = self.headers.get("Cookie", "")
        if raw:
            try:
                c = http_cookies.SimpleCookie(raw)
                token = c.get(SESSION_COOKIE)
                if token:
                    _SESSIONS.pop(token.value, None)
            except Exception:
                pass
        body = json.dumps({"authenticated": False}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._clear_session_cookie()
        self.end_headers()
        self.wfile.write(body)

    def _handle_change_password(self, body):
        sess = self._get_session()
        if not sess:
            return _send_json(self, {"error": "未登录"}, 401)
        old_pw = (body or {}).get("old_password", "")
        new_pw = (body or {}).get("new_password", "")
        if not old_pw or not new_pw:
            return _send_json(self, {"error": "请填写旧密码和新密码"}, 400)
        if len(new_pw) < 6:
            return _send_json(self, {"error": "新密码至少 6 位"}, 400)
        users = _load_users()
        for u in users:
            if u.get("username") == sess.get("username"):
                salt, h = _hash_password(old_pw, u.get("salt"))
                if h != u.get("hash"):
                    return _send_json(self, {"error": "旧密码错误"}, 401)
                salt, h = _hash_password(new_pw)
                u["salt"] = salt
                u["hash"] = h
                _save_users(users)
                return _send_json(self, {"message": "密码修改成功"}, 200)
        return _send_json(self, {"error": "用户不存在"}, 404)

    def _handle_change_username(self, body):
        sess = self._get_session()
        if not sess:
            return _send_json(self, {"error": "未登录"}, 401)
        new_name = (body or {}).get("username", "").strip()
        if not new_name:
            return _send_json(self, {"error": "请输入新用户名"}, 400)
        if not re.match(r"^[\w\-\.@]{1,32}$", new_name):
            return _send_json(self, {"error": "用户名仅支持字母/数字/下划线/点/@，长度 1-32"}, 400)
        old_name = sess.get("username")
        users = _load_users()
        for u in users:
            if u.get("username") == new_name and new_name != old_name:
                return _send_json(self, {"error": "用户名已存在"}, 409)
        for u in users:
            if u.get("username") == old_name:
                u["username"] = new_name
                break
        _save_users(users)
        # 更新当前会话中的用户名（sess 为 _SESSIONS 中同一 dict 的引用）
        sess["username"] = new_name
        return _send_json(self, {"message": "用户名已修改", "username": new_name}, 200)

    # ---- 静态资源 ----

    def _serve_index(self):
        html = get_frontend_html()
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path):
        rel = path
        if rel.startswith("/"):
            rel = rel[1:]
        # 兼容 /css/xxx、/js/xxx 直接映射到 static 下
        if rel.startswith("static/"):
            rel = rel[len("static/"):]
        if rel in ("", "favicon.ico"):
            rel = "favicon.ico" if rel == "favicon.ico" else "index.html"
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(os.path.normpath(STATIC_DIR)):
            return _send_json(self, {"error": "Forbidden"}, 403)
        if not os.path.isfile(full):
            return _send_json(self, {"error": "Not found", "path": path}, 404)
        ext = os.path.splitext(full)[1].lower()
        ctype = _CONTENT_TYPES.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            data = f.read()
        _send_bytes(self, data, content_type=ctype,
                    extra_headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

    # ---- 路由 ----

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/", "/login", "/index.html"):
            return self._serve_index()
        if path.startswith("/static/") or path.startswith("/css/") or path.startswith("/js/") \
                or path == "/favicon.ico" \
                or os.path.splitext(path)[1].lower() in (".jpg", ".jpeg", ".png", ".gif",
                                                         ".webp", ".svg", ".ico", ".woff",
                                                         ".woff2", ".ttf"):
            return self._serve_static(path)

        if path.startswith("/api/"):
            if not self._require_auth("GET", path):
                return
            return self._api_get(path, query)

        # SPA 深链回退
        return self._serve_index()

    def _api_get(self, path, query):
        brain = _get_brain()
        try:
            if path == "/api/auth/status":
                return _send_json(self, self._auth_status())
            if path == "/api/health":
                return _send_json(self, {"status": "ok", "time": _now_iso()})
            if path == "/api/stats":
                return _send_json(self, api_stats(brain, query))
            if path == "/api/memories":
                return _send_tuple(self, api_memories(brain, query))
            if path == "/api/notary":
                return _send_tuple(self, api_notary(brain, query))
            if path == "/api/audit":
                return _send_tuple(self, api_audit(brain, query))
            if path == "/api/ledger/entries":
                return _send_tuple(self, api_ledger_entries(brain, query))
            if path == "/api/ledger/verify":
                return _send_tuple(self, api_ledger_verify(brain, query))
            if path == "/api/sessions":
                return _send_tuple(self, api_sessions(brain, query))
            if path == "/api/sessions/turns":
                return _send_tuple(self, api_sessions_turns(brain, query))
            if path == "/api/graph":
                return _send_tuple(self, api_graph(brain, query))
            if path == "/api/graph/timeline":
                return _send_tuple(self, api_graph_timeline(brain, query))
            if path == "/api/tree":
                return _send_tuple(self, api_tree(brain, query))
            if path == "/api/profiles":
                return _send_tuple(self, api_profiles(brain, "GET"))
            if path == "/api/profile":
                return _send_tuple(self, api_profile(brain, "GET", None, query))
            if path == "/api/namespaces":
                return _send_tuple(self, api_namespaces(brain, query))
            if path == "/api/agent-config":
                return _send_tuple(self, api_agent_config(brain, "GET"))
            if path == "/api/agents":
                return _send_tuple(self, api_agents(brain, "GET"))
            if path == "/api/sources":
                return _send_tuple(self, api_sources(brain, "GET"))
            if path == "/api/heatmap":
                return _send_tuple(self, api_heatmap(brain, query))
            if path == "/api/insights":
                return _send_tuple(self, api_insights(brain, query))
            if path == "/api/snapshot":
                return _send_tuple(self, api_snapshot(brain, query))

            if path == "/api/export":
                data = api_export(brain, query)
                if isinstance(data, tuple) and len(data) == 3:
                    buf, count, ns = data
                    return _send_bytes(self, buf, content_type="application/zip",
                                       filename=f"mnemosyne-memories-{ns}.zip")
                return _send_tuple(self, data)

            if path == "/api/config/export":
                content, fname = api_config_export(brain, query)
                return _send_bytes(self, content, content_type="application/json; charset=utf-8",
                                   filename=fname)

            if path.startswith("/api/memories/") and len(path.split("/")) >= 4:
                memory_id = "/".join(path.split("/")[3:])
                return _send_tuple(self, api_memory_by_id(brain, memory_id, "GET"))

            return _send_json(self, {"error": "Not found", "path": path}, 404)
        except Exception as e:
            return _send_json(self, {"error": str(e)}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/auth/login":
            body = _get_body(self)
            return self._handle_login(body)

        if not self._require_auth("POST", path):
            return

        # multipart 上传
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" in ctype:
            parts = _parse_multipart(self)
            if path == "/api/knowledge/upload":
                brain = _get_brain()
                part = parts[0] if parts else {}
                return _send_tuple(self, api_knowledge_upload(brain, part))
            if path == "/api/import":
                brain = _get_brain()
                part = parts[0] if parts else {}
                return _send_tuple(self, api_import(brain, part.get("data", b""), part.get("filename")))
            return _send_json(self, {"error": "Not found", "path": path}, 404)

        body = _get_body(self)
        brain = _get_brain()
        try:
            if path == "/api/auth/logout":
                return self._handle_logout()
            if path == "/api/auth/change-password":
                return self._handle_change_password(body)
            if path == "/api/auth/change-username":
                return self._handle_change_username(body)
            if path == "/api/memories":
                return _send_tuple(self, api_memories_post(brain, body))
            if path == "/api/consolidate":
                return _send_tuple(self, api_consolidate(brain, body))
            if path == "/api/demote":
                return _send_tuple(self, api_demote(brain, body))
            if path == "/api/demote-cycle":
                return _send_tuple(self, api_demote_cycle(brain, body))
            if path == "/api/budget-recall":
                return _send_tuple(self, api_budget_recall(brain, body))
            if path == "/api/session/turn":
                return _send_tuple(self, api_session_turn_post(brain, body))
            if path == "/api/namespace/switch":
                return _send_tuple(self, api_namespace_switch(brain, body))
            if path == "/api/namespace/rename":
                return _send_tuple(self, api_namespace_rename(brain, body))
            if path == "/api/agent-config":
                return _send_tuple(self, api_agent_config(brain, "POST", body))
            if path == "/api/agents":
                return _send_tuple(self, api_agents(brain, "POST", body))
            if path == "/api/sources":
                return _send_tuple(self, api_sources(brain, "POST", body))
            if path == "/api/sources/sync":
                return _send_tuple(self, api_sources(brain, "POST", body, None, "sync"))
            if path == "/api/config/import":
                return _send_tuple(self, api_config_import(brain, body))
            if path == "/api/profiles":
                return _send_tuple(self, api_profiles(brain, "POST", body))
            if path.startswith("/api/profiles/") and path.endswith("/default"):
                pid = "/".join(path.split("/")[3:-1])
                return _send_tuple(self, api_profiles(brain, "POST", body, pid, "default"))
            if path.startswith("/api/profiles/"):
                pid = "/".join(path.split("/")[3:])
                return _send_tuple(self, api_profiles(brain, "PUT", body, pid))
            return _send_json(self, {"error": "Not found", "path": path}, 404)
        except Exception as e:
            return _send_json(self, {"error": str(e)}, 500)

    def do_PUT(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._require_auth("PUT", path):
            return
        body = _get_body(self)
        brain = _get_brain()
        try:
            if path.startswith("/api/memories/"):
                memory_id = "/".join(path.split("/")[3:])
                return _send_tuple(self, api_memory_by_id(brain, memory_id, "PUT", body))
            if path == "/api/profile":
                return _send_tuple(self, api_profile(brain, "PUT", body, None))
            if path.startswith("/api/profiles/"):
                pid = "/".join(path.split("/")[3:])
                return _send_tuple(self, api_profiles(brain, "PUT", body, pid))
            if path.startswith("/api/agents/"):
                aid = "/".join(path.split("/")[3:])
                return _send_tuple(self, api_agents(brain, "PUT", body, aid))
            if path.startswith("/api/sources/"):
                sid = "/".join(path.split("/")[3:])
                return _send_tuple(self, api_sources(brain, "PUT", body, sid))
            return _send_json(self, {"error": "Not found", "path": path}, 404)
        except Exception as e:
            return _send_json(self, {"error": str(e)}, 500)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._require_auth("DELETE", path):
            return
        brain = _get_brain()
        try:
            if path.startswith("/api/memories/"):
                memory_id = "/".join(path.split("/")[3:])
                return _send_tuple(self, api_memory_by_id(brain, memory_id, "DELETE"))
            if path.startswith("/api/profiles/"):
                pid = "/".join(path.split("/")[3:])
                return _send_tuple(self, api_profiles(brain, "DELETE", None, pid))
            if path.startswith("/api/agents/"):
                aid = "/".join(path.split("/")[3:])
                return _send_tuple(self, api_agents(brain, "DELETE", None, aid))
            if path.startswith("/api/sources/"):
                sid = "/".join(path.split("/")[3:])
                return _send_tuple(self, api_sources(brain, "DELETE", None, sid))
            return _send_json(self, {"error": "Not found", "path": path}, 404)
        except Exception as e:
            return _send_json(self, {"error": str(e)}, 500)


def _send_tuple(handler, data):
    if isinstance(data, tuple) and len(data) == 2:
        _send_json(handler, data[0], data[1])
    else:
        _send_json(handler, data)


# ---------------------------------------------------------------------------
# 服务器启动
# ---------------------------------------------------------------------------

def run_server(port=9090, host="0.0.0.0", base_dir=None, namespace="default", auth=None):
    """启动 Web 管理端。

    auth: True 强制开启鉴权 / False 强制关闭 / None 依据环境变量 MNEMOSYNE_WEB_AUTH。
    """
    if auth is True:
        os.environ["MNEMOSYNE_WEB_AUTH"] = "1"
    elif auth is False:
        os.environ["MNEMOSYNE_WEB_AUTH"] = "0"

    _reset_brain()
    if base_dir:
        os.environ["MNEMOSYNE_DIR"] = base_dir
    if namespace:
        os.environ["MNEMOSYNE_NAMESPACE"] = namespace
    # 重启后沿用持久化的命名空间（优先于默认值）
    _persisted_ns = _load_persisted_namespace()
    if _persisted_ns:
        os.environ["MNEMOSYNE_NAMESPACE"] = _persisted_ns

    _ensure_users_file()
    brain = _get_brain()

    print("Mnemosyne Web 管理端 v7.0.0")
    print(f"  Base dir:   {brain.base_dir}")
    print(f"  Namespace:  {getattr(brain, 'namespace', 'default')}")
    print(f"  Backend:    {brain.store_backend}")
    print(f"  认证:       {'开启' if _auth_enabled() else '关闭'}")
    print(f"  Listening:  http://{host}:{port}")
    print(f"  登录页:     http://{host}:{port}/login")
    print()
    print("  外部智能体对接端点（HTTP JSON）：")
    print(f"    POST http://{host}:{port}/api/memories         写入记忆（body 可含 agent / project / content / mtype）")
    print(f"    GET  http://{host}:{port}/api/memories?agent=X 查询某智能体记忆（agent=all 查全部）")
    print(f"    GET  http://{host}:{port}/api/memories?action=search&q=...&agent=X  检索")
    print(f"    GET  http://{host}:{port}/api/agents           智能体列表（含记忆计数）")
    print(f"    POST http://{host}:{port}/api/agents           添加智能体")
    print(f"    GET  http://{host}:{port}/api/stats?agent=X    某智能体统计")
    print(f"    GET  http://{host}:{port}/api/graph?agent=X    某智能体图谱")
    print()
    print(f"  详见 web_config/api.md")
    print()

    # 后台自动同步外部数据源（任务3，可选；daemon 线程，失败静默）
    threading.Thread(target=_background_sync_loop, args=(brain,), daemon=True).start()

    server = HTTPServer((host, port), MnemosyneWebHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Mnemosyne Web Management Interface")
    parser.add_argument("--port", type=int, default=9090, help="Port to listen on")
    parser.add_argument("--host", default="0.0.0.0", help="Host address")
    parser.add_argument("--dir", default=None, help="Base directory")
    parser.add_argument("--namespace", default="default", help="Namespace")
    parser.add_argument("--auth", dest="auth", action="store_true", default=None, help="Force enable auth")
    parser.add_argument("--no-auth", dest="no_auth", action="store_true", default=False, help="Disable auth")
    args = parser.parse_args()
    auth_arg = None
    if args.no_auth:
        auth_arg = False
    elif args.auth:
        auth_arg = True
    run_server(port=args.port, host=args.host, base_dir=args.dir,
               namespace=args.namespace, auth=auth_arg)
