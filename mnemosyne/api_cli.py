#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — client API CLI
==================================

Registers the reference command set on top of the existing ``mnemosyne`` parser,
so one binary serves both vocabularies::

    mnemosyne add "I prefer dark mode" --user-id alice
    mnemosyne search "what does alice prefer" --user-id alice
    mnemosyne recall "..."            # native, unchanged

Commands added: ``init``, ``add``, ``search``, ``list``, ``get``, ``update``,
``delete``, ``import``, ``config``, ``entity``, ``event``.
(``status`` already exists in the native set with equivalent meaning, so it is
extended rather than duplicated.)

Agent mode
----------

``--agent`` (alias ``--json``) switches every command to a machine envelope::

    {"status": "success", "command": "search", "duration_ms": 134,
     "scope": {"user_id": "alice"}, "count": 2, "data": [...]}

Three properties matter for a tool loop, and all three are deliberate:

* **sanitised data** — only the fields an agent acts on (id, memory, score,
  metadata); no internal counters or storage paths;
* **no human output** — no colours, spinners or banners to parse around;
* **errors as JSON on stdout** with a non-zero exit — so a caller never has to
  choose between "did it fail" and "can I parse the reason".

Output modes: ``text`` (default), ``json`` (raw), ``table`` (default for
``list``), ``quiet`` (ids only), ``agent``.

Environment variables (all optional): ``MNEMOSYNE_API_KEY``, ``MNEMOSYNE_BASE_URL``,
``MNEMOSYNE_USER_ID``, ``MNEMOSYNE_AGENT_ID``, ``MNEMOSYNE_APP_ID``, ``MNEMOSYNE_RUN_ID``,
``MNEMOSYNE_ENABLE_GRAPH``, plus the ``MNEMOSYNE_`` equivalents and
``MNEMOSYNE_DIR`` for the store location.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

__all__ = ["add_subparsers", "dispatch", "API_COMMANDS", "build_client", "emit"]

#: Commands routed to the compatibility layer by ``main()``.  ``init`` and
#: ``import`` are deliberately absent: the native CLI already owns those names and
#: performs the equivalent operation (store init + CLI config; bulk JSON import),
#: and shadowing a native command with a differently-behaving one is worse than
#: reusing it.  Both handlers remain in this module, so ``dispatch(["init", ...])``
#: still works for a caller that wants the wizard explicitly.
API_COMMANDS = frozenset({
    "add", "search", "list", "get", "update", "delete",
    "config", "entity", "event",
})

#: Compatibility command -> native command that covers it.
NATIVE_EQUIVALENTS = {"init": "init", "import": "import"}

_SCOPE_ENV = {
    "user_id": ("MNEMOSYNE_USER_ID", "MNEMOSYNE_USER_ID"),
    "agent_id": ("MNEMOSYNE_AGENT_ID", "MNEMOSYNE_AGENT_ID"),
    "app_id": ("MNEMOSYNE_APP_ID", "MNEMOSYNE_APP_ID"),
    "run_id": ("MNEMOSYNE_RUN_ID", "MNEMOSYNE_RUN_ID"),
}


# ---------------------------------------------------------------------------
# Parser registration
# ---------------------------------------------------------------------------

def _agent_parent() -> argparse.ArgumentParser:
    """Shared flags every compatible command accepts.

    A parent parser rather than top-level flags, because the native commands
    already define their own ``--json`` and a duplicate at the top level would be
    parsed twice with the second silently winning.
    """
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--agent", action="store_true",
                   help="Agent mode: machine-readable JSON envelope")
    p.add_argument("--json", action="store_true", dest="agent_json",
                   help="Alias for --agent")
    p.add_argument("--output", default=None,
                   choices=["text", "json", "table", "quiet", "agent"],
                   help="Output format (default: text, or table for list)")
    p.add_argument("--api-key", default=None, help="API key for a remote service")
    p.add_argument("--base-url", default=None,
                   help="Remote service root; omit to run fully embedded")
    p.add_argument("--user-id", default=None)
    p.add_argument("--agent-id", default=None)
    p.add_argument("--run-id", default=None)
    p.add_argument("--app-id", default=None)
    p.add_argument("--dir", default=None, dest="api_dir",
                   help="Memory store directory for embedded mode")
    return p


def add_subparsers(sub, existing=None) -> None:
    """Register every compatible subcommand on *sub*.

    ``existing`` is the set of subcommand names the host parser already owns.
    argparse raises on a duplicate name, and silently replacing a native command
    with a differently-behaving one would be worse than leaving it alone, so a
    name that is already taken is skipped. In practice that is ``init`` and
    ``import``, both of which the native CLI already implements with equivalent
    behaviour —
    """
    parent = _agent_parent()
    taken = set(existing or ())

    if "init" not in taken:
        i = sub.add_parser("init", parents=[parent],
                           help="初始化配置（wizard / API key）")
        i.add_argument("--email", default=None)
        i.add_argument("--agent-caller", default=None,
                       help="Name of the calling agent (mirrors 'init --agent')")
        i.add_argument("--force", action="store_true")

    a = sub.add_parser("add", parents=[parent],
                       help="写入记忆（单遍 ADD-only 抽取）")
    a.add_argument("messages", nargs="?", default=None,
                   help="文本；省略时从 --file 或 stdin 读取")
    a.add_argument("--file", default=None, help="从文件读取消息（JSON 或纯文本）")
    a.add_argument("--metadata", default=None, help="JSON 对象")
    a.add_argument("--type", dest="memory_type", default=None)
    a.add_argument("--infer", dest="infer", action="store_true", default=None)
    a.add_argument("--no-infer", dest="infer", action="store_false")
    a.add_argument("--immutable", action="store_true")
    a.add_argument("--expires", default=None, help="YYYY-MM-DD")
    a.add_argument("--includes", default=None)
    a.add_argument("--excludes", default=None)

    s = sub.add_parser("search", parents=[parent], help="语义检索")
    s.add_argument("query")
    s.add_argument("--top-k", type=int, default=20)
    s.add_argument("--threshold", type=float, default=0.1)
    s.add_argument("--rerank", action="store_true")
    s.add_argument("--explain", action="store_true")
    s.add_argument("--filters", default=None, help="JSON 对象")
    s.add_argument("--reference-date", default=None)

    l = sub.add_parser("list", parents=[parent], help="列出记忆")
    l.add_argument("--top-k", type=int, default=20)
    l.add_argument("--filters", default=None, help="JSON 对象")
    l.add_argument("--show-expired", action="store_true")

    g = sub.add_parser("get", parents=[parent], help="按 id 取一条记忆")
    g.add_argument("memory_id")

    u = sub.add_parser("update", parents=[parent], help="更新一条记忆")
    u.add_argument("memory_id")
    u.add_argument("text", nargs="?", default=None)
    u.add_argument("--metadata", default=None, help="JSON 对象")

    d = sub.add_parser("delete", parents=[parent],
                       help="删除记忆 / 清空范围 / 删除实体")
    d.add_argument("memory_id", nargs="?", default=None)
    d.add_argument("--all", action="store_true", dest="delete_all")
    d.add_argument("--entity", action="store_true")
    d.add_argument("--yes", action="store_true")

    if "import" not in taken:
        im = sub.add_parser("import", parents=[parent], help="从 JSON 文件批量导入")
        im.add_argument("path")

    c = sub.add_parser("config", parents=[parent], help="查看/修改 CLI 配置")
    c.add_argument("--show", action="store_true")
    c.add_argument("--get", default=None)
    c.add_argument("--set", nargs=2, action="append", default=None,
                   metavar=("KEY", "VALUE"))
    c.add_argument("--path", action="store_true")

    e = sub.add_parser("entity", parents=[parent], help="列出/删除实体范围")
    e.add_argument("--list", action="store_true", dest="entity_list")
    e.add_argument("--delete", action="store_true", dest="entity_delete")
    e.add_argument("--yes", action="store_true")
    e.add_argument("--limit", type=int, default=100)

    ev = sub.add_parser("event", parents=[parent], help="查看异步操作事件")
    ev.add_argument("--status", default=None)
    ev.add_argument("--operation", default=None)
    ev.add_argument("--limit", type=int, default=50)
    ev.add_argument("--id", dest="event_id", default=None)


def write_default_config(brain_dir: Optional[str] = None) -> str:
    """Create or refresh the CLI config for *brain_dir*.

    Called by the native ``init`` so that one command both initialises the store
    and records where the CLI will look for it — which is what the reference's
    setup wizard accomplishes in two steps. Existing keys are preserved: init must
    not discard an API key a user already configured.
    """
    cfg = load_cli_config()
    cfg.setdefault("brain_dir", os.path.expanduser(
        brain_dir or os.environ.get("MNEMOSYNE_DIR")
        or os.path.join(os.path.expanduser("~"), ".mnemosyne")))
    return save_cli_config(cfg)


# ---------------------------------------------------------------------------
# Configuration + client construction
# ---------------------------------------------------------------------------

def _config_path() -> str:
    base = os.environ.get("MNEMOSYNE_DIR") or os.path.expanduser("~/.mnemosyne")
    return os.path.join(base, "cli.config.json")


def load_cli_config() -> Dict[str, Any]:
    path = _config_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_cli_config(cfg: Dict[str, Any]) -> str:
    path = _config_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - platform dependent
        pass
    return path


def _env(*names: str) -> Optional[str]:
    for n in names:
        val = os.environ.get(n)
        if val:
            return val
    return None


def build_client(args) -> Any:
    """Construct a client from flags, then env, then the saved config.

    Precedence is flags > environment > config file, which is the order that lets a
    CI job override a developer's saved defaults without editing anything on disk.
    """
    from .api import MemoryClient

    cfg = load_cli_config()
    base_url = (getattr(args, "base_url", None) or _env("MNEMOSYNE_BASE_URL",
                                                        "MNEMOSYNE_BASE_URL")
                or cfg.get("base_url"))
    api_key = (getattr(args, "api_key", None) or _env("MNEMOSYNE_API_KEY",
                                                      "MNEMOSYNE_API_KEY")
               or cfg.get("api_key"))
    if base_url:
        return MemoryClient(api_key=api_key, base_url=base_url)

    # `--dir` exists at both levels: the native top-level flag and the compatible
    # per-command one. Both are honoured so `mnemosyne --dir X add ...` and
    # `mnemosyne add --dir X ...` agree — the alternative is a silent split where
    # one spelling writes to ~/.mnemosyne and the other does not.
    brain_dir = (getattr(args, "api_dir", None) or getattr(args, "dir", None)
                 or _env("MNEMOSYNE_DIR")
                 or cfg.get("brain_dir") or os.path.expanduser("~/.mnemosyne"))
    mem_cfg: Dict[str, Any] = {"brain_dir": brain_dir}
    for key in ("llm", "embedder", "vector_store", "graph_store", "reranker"):
        if isinstance(cfg.get(key), dict):
            mem_cfg[key] = cfg[key]
    return MemoryClient(api_key=api_key, config=mem_cfg)


def _scope_from(args) -> Dict[str, Any]:
    """Resolve entity scope: explicit flag, else env, else config default."""
    cfg = load_cli_config()
    out: Dict[str, Any] = {}
    for key, names in _SCOPE_ENV.items():
        val = getattr(args, key, None) or _env(*names) or cfg.get(key)
        if val:
            out[key] = val
    return out


def _json_arg(value: Optional[str], what: str) -> Optional[Dict[str, Any]]:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as e:
        raise SystemExit(f"--{what} must be valid JSON: {e}") from None
    if not isinstance(parsed, dict):
        raise SystemExit(f"--{what} must be a JSON object")
    return parsed


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def emit(args, command: str, *, data: Any, scope: Optional[Dict[str, Any]] = None,
         started: float, extra: Optional[Dict[str, Any]] = None) -> int:
    """Print a result in the requested format and return an exit code."""
    agent = bool(getattr(args, "agent", False) or getattr(args, "agent_json", False))
    fmt = "agent" if agent else (getattr(args, "output", None)
                                 or ("table" if command == "list" else "text"))

    rows = data if isinstance(data, list) else ([data] if data is not None else [])
    envelope = {
        "status": "success",
        "command": command,
        "duration_ms": int((time.time() - started) * 1000),
        "scope": scope or {},
        "count": len(rows),
        "data": _sanitize(rows),
    }
    if extra:
        envelope.update(extra)

    if fmt in ("agent", "json"):
        # `agent` and `json` differ for the *native* commands; here both must be
        # parseable by a tool loop, so the same envelope is emitted and the only
        # difference is that `json` keeps the raw data unsanitised.
        payload = envelope if fmt == "agent" else {
            **envelope, "data": rows}
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0
    if fmt == "quiet":
        for row in rows:
            if isinstance(row, dict) and row.get("id"):
                print(row["id"])
            elif isinstance(row, str):
                print(row)
        return 0
    if fmt == "table":
        print(_table(rows))
        return 0

    if not rows:
        print("(no results)")
        return 0
    for row in rows:
        if isinstance(row, dict):
            mid = row.get("id") or ""
            score = row.get("score")
            text = row.get("memory") or row.get("content") or ""
            head = f"[{score:.3f}] " if isinstance(score, (int, float)) else ""
            print(f"{head}{text[:160]}")
            if mid:
                print(f"       id: {mid}")
            if row.get("reason"):
                print(f"       match: {row['reason']}")
        else:
            print(str(row))
    return 0


def fatal(args, command: str, exc: Exception) -> int:
    """Report a failure in the same envelope shape, and return a non-zero code."""
    agent = bool(getattr(args, "agent", False) or getattr(args, "agent_json", False))
    payload = {
        "status": "error",
        "command": command,
        "error": str(exc),
        "code": getattr(exc, "code", type(exc).__name__),
    }
    detail = getattr(exc, "detail", None)
    if detail:
        payload["detail"] = detail
    if agent:
        print(json.dumps(payload, ensure_ascii=False, default=str))
    else:
        print(f"error: {payload['error']}", file=sys.stderr)
        if detail:
            print(f"detail: {json.dumps(detail, ensure_ascii=False)}", file=sys.stderr)
    return 1


def _sanitize(rows: List[Any]) -> List[Any]:
    """Keep only the fields an agent acts on.

    Drops storage-level internals (embeddings, templates, counters) that would
    bloat a tool result and tempt a caller into depending on them.  Everything the
    projection already put under ``metadata`` is preserved, since that is the part
    the caller supplied and may legitimately need back.
    """
    keep = {"id", "memory", "score", "created_at", "updated_at", "metadata",
            "categories", "reason", "verified", "text", "content", "message",
            "event_id", "status", "deleted", "count", "results", "namespace",
            "detail", "entities", "source", "relationship", "target", "memory_id",
            "old_memory", "new_memory", "event", "is_deleted"}
    out: List[Any] = []
    for row in rows:
        if not isinstance(row, dict):
            out.append(row)
            continue
        out.append({k: v for k, v in row.items() if k in keep})
    return out


def _table(rows: List[Any]) -> str:
    if not rows:
        return "(no results)"
    headers = ["id", "score", "memory"]
    body: List[List[str]] = []
    for row in rows:
        if not isinstance(row, dict):
            body.append([str(row), "", ""])
            continue
        score = row.get("score")
        body.append([
            str(row.get("id") or "")[:20],
            f"{score:.4f}" if isinstance(score, (int, float)) else "",
            (row.get("memory") or row.get("content") or "").replace("\n", " ")[:70],
        ])
    widths = [max(len(headers[i]), *(len(r[i]) for r in body)) for i in range(3)]
    sep = "-+-".join("-" * w for w in widths)
    lines = [" | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)), sep]
    lines += [" | ".join(r[i].ljust(widths[i]) for i in range(3)) for r in body]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def dispatch(args) -> int:
    """Run one compatible command. Returns a process exit code."""
    command = args.command
    started = time.time()
    try:
        if command == "init":
            return _cmd_init(args, started)
        if command == "config":
            return _cmd_config(args, started)
        client = build_client(args)
        fn = {
            "add": _cmd_add, "search": _cmd_search, "list": _cmd_list,
            "get": _cmd_get, "update": _cmd_update, "delete": _cmd_delete,
            "import": _cmd_import, "entity": _cmd_entity, "event": _cmd_event,
        }[command]
        return fn(args, client, started)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - one envelope for every failure
        return fatal(args, command, e)


def _cmd_init(args, started: float) -> int:
    """Write a usable CLI configuration.

    ``--agent-caller`` mirrors the reference's "sign up as an agent" flow: it
    records which agent asked for the key so a later human claim can tell them
    apart.  Nothing is minted remotely here — the API key is yours to issue — so
    the wizard's job is to make the local half correct and explicit.
    """
    cfg = load_cli_config()
    if cfg and not args.force:
        print(f"config already exists: {_config_path()} (use --force to overwrite)")

    if getattr(args, "email", None):
        cfg["claim_email"] = args.email
    if getattr(args, "agent_caller", None):
        cfg["agent_caller"] = args.agent_caller
    if getattr(args, "api_key", None):
        cfg["api_key"] = args.api_key
    if getattr(args, "base_url", None):
        cfg["base_url"] = args.base_url
    if getattr(args, "api_dir", None):
        cfg["brain_dir"] = args.api_dir
    for key in ("user_id", "agent_id", "app_id", "run_id"):
        val = getattr(args, key, None)
        if val:
            cfg[key] = val

    path = save_cli_config(cfg)
    client = build_client(args)
    info = client.status()
    payload = {"config_path": path, "transport": client.transport,
               "status": info.get("status"), "defaults": {
                   k: cfg.get(k) for k in ("user_id", "agent_id", "app_id",
                                           "run_id", "base_url") if cfg.get(k)}}
    return emit(args, "init", data=[payload], started=started)


def _cmd_config(args, started: float) -> int:
    if args.path:
        print(_config_path())
        return 0
    cfg = load_cli_config()
    if args.set:
        for key, value in args.set:
            try:
                cfg[key] = json.loads(value)
            except json.JSONDecodeError:
                cfg[key] = value
        path = save_cli_config(cfg)
        print(f"saved: {path}")
        return 0
    if args.get:
        print(json.dumps(cfg.get(args.get), ensure_ascii=False, default=str))
        return 0
    payload = dict(cfg)
    # Report the *effective* configuration rather than only the file contents. A
    # fresh install has no config file at all, so answering `config --show` with
    # an empty object hides the values the CLI will actually use — which is
    # precisely the question the command exists to answer.
    payload.setdefault("brain_dir", os.path.expanduser(
        os.environ.get("MNEMOSYNE_DIR")
        or os.path.join(os.path.expanduser("~"), ".mnemosyne")))
    payload.setdefault(
        "transport",
        "http" if (cfg.get("base_url") or _env("MNEMOSYNE_BASE_URL", "MNEMOSYNE_BASE_URL"))
        else "embedded")
    for key in ("user_id", "agent_id", "app_id", "run_id"):
        if not payload.get(key):
            env_val = _env(*_SCOPE_ENV[key])
            if env_val:
                payload[key] = env_val
    payload["config_path"] = _config_path()
    payload["config_exists"] = os.path.isfile(_config_path())
    payload = {k: ("***" if "key" in str(k).lower() and v else v)
               for k, v in payload.items()}
    if getattr(args, "agent", False) or getattr(args, "agent_json", False):
        return emit(args, "config", data=[payload], started=started)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def _cmd_add(args, client, started: float) -> int:
    messages = _read_messages(args)
    scope = _scope_from(args)
    kwargs: Dict[str, Any] = dict(scope)
    if getattr(args, "metadata", None):
        kwargs["metadata"] = _json_arg(args.metadata, "metadata")
    if getattr(args, "memory_type", None):
        kwargs["memory_type"] = args.memory_type
    if getattr(args, "infer", None) is not None:
        kwargs["infer"] = args.infer
    if getattr(args, "immutable", False):
        kwargs["immutable"] = True
    if getattr(args, "expires", None):
        kwargs["expiration_date"] = args.expires
    if getattr(args, "includes", None):
        kwargs["includes"] = args.includes
    if getattr(args, "excludes", None):
        kwargs["excludes"] = args.excludes
    # The CLI is a foreground process: waiting for the result is what a user
    # expects, unlike the REST/MCP surfaces where returning an event is correct.
    kwargs["async_mode"] = False

    result = client.add(messages, **kwargs)
    rows = result.get("results") if isinstance(result, dict) else None
    # `scope` is reported without the write options (async_mode, infer, ...) so the
    # envelope answers "which memories were touched", not "what did I pass".
    return emit(args, "add", data=rows or [], scope=scope,
                started=started, extra={"results": rows or []})


def _read_messages(args) -> Any:
    """Load messages from a positional string, ``--file`` or stdin.

    A file whose contents parse as JSON is taken as message objects; anything else
    is treated as one text blob.  Guessing wrong here is recoverable (the text is
    still remembered verbatim), whereas forcing the caller to declare the shape
    would make the common case verbose.
    """
    if getattr(args, "file", None):
        with open(args.file, "r", encoding="utf-8") as fh:
            raw = fh.read()
    elif getattr(args, "messages", None) is not None:
        raw = args.messages
    else:
        if sys.stdin.isatty():
            raise SystemExit("nothing to add: pass text, --file, or pipe stdin")
        raw = sys.stdin.read()
    if not raw or not raw.strip():
        raise SystemExit("message text is empty")
    stripped = raw.strip()
    if stripped[:1] in ("[", "{"):
        try:
            parsed = json.loads(stripped)
            return parsed
        except json.JSONDecodeError:
            pass
    return raw


def _cmd_search(args, client, started: float) -> int:
    kwargs: Dict[str, Any] = {
        "filters": _merge_filters(args),
        "top_k": args.top_k,
        "threshold": args.threshold,
        "rerank": bool(args.rerank),
        "explain": bool(args.explain),
    }
    if getattr(args, "reference_date", None):
        kwargs["reference_date"] = args.reference_date
    result = client.search(args.query, **kwargs)
    return emit(args, "search", data=result.get("results") or [],
                scope=kwargs["filters"], started=started)


def _merge_filters(args) -> Dict[str, Any]:
    filters = _json_arg(getattr(args, "filters", None), "filters") or {}
    for key, val in _scope_from(args).items():
        filters.setdefault(key, val)
    return filters


def _cmd_list(args, client, started: float) -> int:
    kwargs: Dict[str, Any] = {"top_k": args.top_k,
                              "filters": _merge_filters(args)}
    if getattr(args, "show_expired", False):
        kwargs["show_expired"] = True
    result = client.get_all(**kwargs)
    return emit(args, "list", data=result.get("results") or [],
                scope=kwargs["filters"], started=started)


def _cmd_get(args, client, started: float) -> int:
    item = client.get(args.memory_id)
    return emit(args, "get", data=[item], started=started)


def _cmd_update(args, client, started: float) -> int:
    kwargs: Dict[str, Any] = {}
    if args.text is not None:
        kwargs["text"] = args.text
    if getattr(args, "metadata", None):
        kwargs["metadata"] = _json_arg(args.metadata, "metadata")
    if not kwargs:
        raise SystemExit("nothing to update: pass text or --metadata")
    result = client.update(args.memory_id, **kwargs)
    return emit(args, "update", data=[result], started=started)


def _cmd_delete(args, client, started: float) -> int:
    if args.delete_all:
        scope = _scope_from(args)
        if not scope:
            raise SystemExit(
                "--all requires a scope (--user-id / --agent-id / --run-id / "
                "--app-id); refusing to empty the shared default scope")
        if not _confirm(args, f"delete ALL memories in {scope}?"):
            return emit(args, "delete", data=[{"message": "aborted"}], started=started)
        return emit(args, "delete", data=[client.delete_all(**scope)],
                    scope=scope, started=started)
    if args.entity:
        scope = _scope_from(args)
        if not scope:
            raise SystemExit("--entity requires a scope")
        if not _confirm(args, f"delete entity {scope} and cascade?"):
            return emit(args, "delete", data=[{"message": "aborted"}], started=started)
        return emit(args, "delete", data=[client.delete_entities(**scope)],
                    scope=scope, started=started)
    if not args.memory_id:
        raise SystemExit("pass a memory_id, or --all / --entity with a scope")
    if not _confirm(args, f"delete memory {args.memory_id}?"):
        return emit(args, "delete", data=[{"message": "aborted"}], started=started)
    return emit(args, "delete", data=[client.delete(args.memory_id)],
                started=started)


def _confirm(args, prompt: str) -> bool:
    """Require ``--yes`` unless stdin is not a TTY.

    In agent mode ``--yes`` is assumed, because a tool loop has no way to answer a
    prompt — and a command that blocks forever waiting for input is the worst
    possible behaviour there.
    """
    if getattr(args, "yes", False):
        return True
    if getattr(args, "agent", False) or getattr(args, "agent_json", False):
        return True
    if not sys.stdin.isatty():
        return True
    try:
        reply = input(f"{prompt} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return reply in ("y", "yes")


def _cmd_import(args, client, started: float) -> int:
    with open(args.path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    items = payload if isinstance(payload, list) else payload.get("results") or []
    if not isinstance(items, list):
        raise SystemExit("import file must be a JSON array or {\"results\": [...]}")
    scope = _scope_from(args)
    added, duplicates, failed = 0, 0, 0
    for item in items:
        try:
            text = item if isinstance(item, str) else (
                item.get("memory") or item.get("content") or "")
            if not text:
                failed += 1
                continue
            meta = dict(item.get("metadata") or {}) if isinstance(item, dict) else {}
            out = client.add(text, metadata=meta, infer=False,
                             async_mode=False, **scope)
            for r in (out.get("results") or []):
                if r.get("event") == "ADD":
                    added += 1
                elif r.get("event") == "DUPLICATE":
                    duplicates += 1
                else:
                    failed += 1
        except Exception:  # noqa: BLE001 - keep going, report at the end
            failed += 1
    return emit(args, "import",
                data=[{"added": added, "duplicates": duplicates,
                       "failed": failed, "total": len(items)}],
                scope=scope, started=started)


def _cmd_entity(args, client, started: float) -> int:
    if args.entity_delete:
        scope = _scope_from(args)
        if not scope:
            raise SystemExit("--delete requires a scope")
        if not _confirm(args, f"delete entity {scope}?"):
            return emit(args, "entity", data=[{"message": "aborted"}], started=started)
        return emit(args, "entity", data=[client.delete_entities(**scope)],
                    scope=scope, started=started)
    result = client.list_entities(limit=args.limit)
    return emit(args, "entity", data=result.get("results") or [], started=started)


def _cmd_event(args, client, started: float) -> int:
    if args.event_id:
        return emit(args, "event", data=[client.get_event_status(args.event_id)],
                    started=started)
    result = client.list_events(status=args.status, operation=args.operation,
                                limit=args.limit)
    return emit(args, "event", data=result.get("results") or [], started=started)
