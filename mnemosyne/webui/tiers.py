#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mnemosyne OS —— 订阅层级与工具门控（入口层，纯 stdlib）

为什么单独一个模块
──────────────────
1. **引擎不可改**：`brain.py` / `retrieval.py` / `capsule.py` / `graph.py` /
   `notary.py` 是记忆引擎核心，门控一律在**出口**做（结果投影层过滤），
   不改检索参数、不改 SQL、不改存储。这样即使门控逻辑写错，最坏情况是
   "少返回几条"，而不是"记忆被删 / 索引被污染"。
2. **两个入口共用一份规则**：`mcp_server.py`（41 个原生工具）与
   `mcp_api.py`（11 个客户端兼容工具）必须给出**完全一致**的判定，
   否则用户会看到"同一个概念，换个工具名就绕过限制"。
3. **自部署默认关闭**：`MNEMOSYNE_TIER_GATE=1` 才生效。PyPI / 本地用户
   没有 SaaS 账号，默认行为必须与 8.1.0 完全一致 —— 这是「本地优先、
   zero-dependency」承诺的一部分。云端 Fly 部署在 fly.toml 显式开启。

定价条款对照
──────────────────────────────────
    Free  : $0        冷层 30 天窗口 / 审计 7 天 / 单 namespace
    Pro   : $6.9/月   $69/年   永久冷层 / 跨 ns / 无限审计 / 全量导出
    Team  : $19.9/月/席位      同 Pro + 多席位 / 团队策略 / 审计报告导出

「冷层窗口」的精确语义（**不是**按 created_at 一刀切）
──────────────────────────────────────────────────────
Design note:「冷层记忆仅返回 created_at > now() - 30d 的条目；不要过滤热层和温层，
只对冷层施加时间窗口。」

所以判定是**两个条件的合取**：
    tier == "cold"  AND  created_at < cutoff   →  排除
热层（hot）/ 温层（warm）无论多老都保留 —— 它们代表"用户还在用"的记忆，
砍掉它们等于惩罚活跃用户，与trial period"先让用户感受到价值"的策略直接冲突。

时间格式陷阱（实测 2026-10-23）
──────────────────────────────
引擎写入的 `created_at` 是**北京时间 ISO 字符串**，形如
`2026-10-23T03:07:31+08:00`（见 `utils._now_iso()`），**不是** epoch 数字。
因此截止时间必须用**同一时区、同一格式**生成，字符串比较才成立。
若误用 UTC 或 `time.time()`，会出现 8 小时偏移 —— 表现为"窗口边界附近的
记忆随机进出"，是最难排查的一类 bug。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

__all__ = [
    "TIERS", "TIER_ORDER", "GATED_TOOLS",
    "gate_enabled", "resolve_tier", "tier_config",
    "cold_cutoff_iso", "is_excluded_by_window",
    "check_tier_gate", "filter_recall_items", "filter_audit_entries",
    "filter_export_records", "namespace_allowed", "upgrade_hint",
]

# 北京时间（UTC+8）—— 与引擎 utils._now_iso() 保持同一时区，
# 否则字符串比较会因 8 小时偏移而在窗口边界出错。
BEIJING_TZ = timezone(timedelta(hours=8))

# ---------------------------------------------------------------------------
# 1. 层级定义
# ---------------------------------------------------------------------------

# 层级能力表。`None` 一律表示「不限」，不用 -1 / 0 这类哨兵值 ——
# 哨兵值会在比较时被误当成"极小"从而把"无限"判成"全禁"。
TIERS = {
    "free": {
        "label": "Free",
        "cold_window_days": 30,      # FREE_TIER: cold tier window = 30d
        "audit_window_days": 7,      # FREE_TIER: audit window = 7d
        "export_window_days": 30,    # FREE_TIER: export = hot + 30d cold only
        "cross_namespace": False,    # FREE_TIER: single namespace only
        "max_namespaces": 1,
    },
    "pro": {
        "label": "Pro",
        "cold_window_days": None,    # PRO_TIER: permanent cold memory
        "audit_window_days": None,   # PRO_TIER: unlimited audit history
        "export_window_days": None,  # PRO_TIER: full export
        "cross_namespace": True,     # PRO_TIER: cross-namespace recall
        "max_namespaces": None,
    },
    "team": {
        "label": "Team",
        "cold_window_days": None,    # TEAM_TIER: everything in Pro
        "audit_window_days": None,
        "export_window_days": None,
        "cross_namespace": True,
        "max_namespaces": None,
        "seats": True,               # TEAM_TIER: multi-user namespace management
    },
}

# 升级比较用：数字越大能力越强。用于「降级不得提升权限」的断言。
TIER_ORDER = {"free": 0, "pro": 1, "team": 2}

# ---------------------------------------------------------------------------
# 2. 工具分类（按类别门控，不逐个工具判断）
# ---------------------------------------------------------------------------
# The design requires「按工具类别执行（不是逐个工具判断）」。这里给出三个受门控的类别；
# 未列出的工具（写入类 / 形成与巩固类）一律无限制放行 —— 这是**刻意**的：
# trial period要让人写得进去，才可能被窗口摩擦到。

# 召回类：受冷层时间窗口 + 跨 namespace 限制
RECALL_TOOLS = frozenset({
    "recall", "search_memories", "get_memories", "get_memory", "get_all",
    "graph_query", "temporal_query", "recall_catalog", "memory_inject",
    "session_recall", "playbook_recall", "expand", "expand_memory", "capsule",
    "list_projects", "list_entities", "list_events", "recall_health",
})

# 审计与导出类：受时间窗口限制
AUDIT_TOOLS = frozenset({
    "audit", "confidence_history", "memory_trace", "memory_metrics",
    "verify_integrity",
})

EXPORT_TOOLS = frozenset({
    "memory/export-v1", "memory_export-v1",
    "memory/import-v1", "memory_import-v1",
    "memory/claim",
})

# 明确**不受限**的类别（写在这里是为了让"为什么不拦它"成为可读的决策，
# 而不是靠"没出现在集合里"这种隐式约定）
UNLIMITED_TOOLS = frozenset({
    # 写入类
    "retain", "retain_batch", "add_memory", "update_memory",
    # 形成与巩固类
    "consolidate", "reflect", "dedup", "session_summarize",
    "memory_consolidate", "conflict_resolve", "topic_summarize",
    "task_record", "playbook_save", "code_change_record",
    "code_archaeology", "memory_policy", "memory_envelope",
    "memory_mask", "memory_cascade_delete", "memory_hot_path",
    "delete_memory", "delete_all_memories", "delete_entities",
    "forget", "stats", "doctor",
})

GATED_TOOLS = RECALL_TOOLS | AUDIT_TOOLS | EXPORT_TOOLS


# ---------------------------------------------------------------------------
# 3. 开关与层级解析
# ---------------------------------------------------------------------------

def gate_enabled() -> bool:
    """门控总开关。默认 **False** —— 自部署 / PyPI 用户行为不变。

    PROMO_STRATEGY: trial period靠"用得爽"acquisition，绝不因为门控误判把本地用户拦死。
    云端在 fly.toml 里显式设 MNEMOSYNE_TIER_GATE=1。
    """
    return str(os.environ.get("MNEMOSYNE_TIER_GATE") or "").strip().lower() in (
        "1", "true", "yes", "on")


def resolve_tier(user_context=None) -> str:
    """确定本次调用的层级。

    优先级（**每次请求实时解析，不缓存** —— the design requirement 明确要求）：
      1. user_context['tier']（由 8789 控制台经内部头下发，最可信）
      2. 环境变量 MNEMOSYNE_DEFAULT_TIER（网关兜底）
      3. "free"（fail-closed：拿不到身份时按最低权限）

    ⚠️ 绝不读 token 内嵌 tier：token 是共享的，且客户端可伪造请求体。
    """
    if isinstance(user_context, dict):
        raw = str(user_context.get("tier") or "").strip().lower()
        if raw in TIERS:
            return raw
    env = str(os.environ.get("MNEMOSYNE_DEFAULT_TIER") or "").strip().lower()
    if env in TIERS:
        return env
    # FREE_TIER: unknown identity falls back to the most restrictive tier
    return "free"


def tier_config(tier: str) -> dict:
    return TIERS.get(str(tier or "").strip().lower()) or TIERS["free"]


# ---------------------------------------------------------------------------
# 4. 时间窗口
# ---------------------------------------------------------------------------

def cold_cutoff_iso(days) -> str | None:
    """返回「冷层窗口下界」的北京时间 ISO 字符串；None 表示不限。

    必须与引擎 `created_at` 同格式（`+08:00` 带偏移、秒级），
    才能直接用字符串比较。
    """
    if days is None:
        return None
    try:
        n = int(days)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    cutoff = datetime.now(BEIJING_TZ) - timedelta(days=n)
    return cutoff.isoformat(timespec="seconds")


def _parse_created(value) -> datetime | None:
    """把 created_at 解析成 aware datetime。

    容错处理三种历史形态（现网库里都真实存在）：
      · `2026-10-23T03:07:31+08:00`  引擎标准输出
      · `2026-10-23T03:07:31`        无偏移（旧数据）→ 按北京时间解释
      · `2026-10-23 03:07:31`        空格分隔（更旧）
    解析失败返回 None —— 调用方必须把「解析不了」当成「不排除」，
    宁可多返回一条，也不要因为一条脏数据把整页结果吞掉。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), BEIJING_TZ)
        except (ValueError, OSError, OverflowError):
            return None
    s = str(value).strip()
    if not s:
        return None
    s = s.replace(" ", "T", 1) if (" " in s and "T" not in s) else s
    # Python 3.11+ 的 fromisoformat 已支持 'Z'，但 3.8/3.9 不支持，手动换掉
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # 截到秒再试一次（有些记录带微秒或多余小数位）
        try:
            dt = datetime.fromisoformat(s[:19])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BEIJING_TZ)
    return dt


def is_excluded_by_window(record, tier: str, window_days) -> bool:
    """该条记录是否应被**时间窗口**排除。

    判定 = 冷层 AND 超窗。热层/温层永不被时间窗口排除。

    FREE_TIER: only cold-tier memories older than the window are dropped.
    """
    if window_days is None:
        return False
    if not isinstance(record, dict):
        return False
    # 只有冷层受窗口约束（the design requirement 明文要求，不可扩大）
    if str(record.get("tier") or "hot").strip().lower() != "cold":
        return False
    created = _parse_created(record.get("created_at"))
    if created is None:
        return False          # 脏数据 → 不排除（宁可多给）
    cutoff = _parse_created(cold_cutoff_iso(window_days))
    if cutoff is None:
        return False
    return created < cutoff


# ---------------------------------------------------------------------------
# 5. 统一门控入口
# ---------------------------------------------------------------------------

def check_tier_gate(tool_name: str, params=None, user_context=None):
    """工具调用前的准入判定。返回 `(allowed: bool, reason: str | None)`。

    ⚠️ 注意：本函数**只做「能不能调」**的判断，不做结果过滤。
    召回/审计/导出的窗口收窄是**出口过滤**（见下面三个 filter_*），
    因为它们必须是"少返回几条"，而不是"整个工具报错"——
    报错会让 Agent 以为记忆系统坏了，从而放弃使用；少返回几条则表现为
    "记忆变淡了"，这正是trial period想要的windowing effect。

    目前只有一种硬拒绝：Free 层跨 namespace 访问。
    """
    tier = resolve_tier(user_context)
    cfg = tier_config(tier)
    name = str(tool_name or "")

    if not gate_enabled():
        return True, None

    # FREE_TIER: cross_namespace = False → 请求其它 namespace 直接拒绝
    if not cfg.get("cross_namespace"):
        want = None
        if isinstance(params, dict):
            want = params.get("namespace")
        bound = None
        if isinstance(user_context, dict):
            bound = user_context.get("namespace")
        if want and bound and str(want) != str(bound):
            return False, (
                "cross_namespace_not_allowed: the Free plan is bound to a single "
                "namespace. Upgrade to Pro to recall across namespaces.")

    # 其余工具类别不设硬拒绝（写入类/巩固类无限制）
    _ = name  # 保留参数以便将来按工具细分；当前按类别统一放行
    return True, None


def filter_recall_items(items, tier: str, record_getter=None, record_lookup=None):
    """召回结果出口过滤。返回 `(kept, dropped_count)`。

    ⚠️ **这是本轮最容易做错的一处，根因写在下面。**

    引擎的召回投影（`mcp_server` 的 recall 分支 / `mcp_api` 的
    `to_memory_item`）**不回传 `tier` 字段** —— 投影只保留
    memory_id / content / created_at / confidence 等展示字段。
    于是「只看返回值判断冷层」必然失效：所有条目的 `tier` 都缺失，
    按默认值 `hot` 处理 → **一条都不会被排除**（2026-10-23 实测复现）。

    两种修法：
      (a) 改投影，让每个工具都多回传一个 `tier` 字段；
      (b) 门控层自己按 memory_id 回查存储拿 `tier`。

    选 (b)：投影是**对外数据契约**，加字段会改变所有客户端的返回形状
    （且 55 个工具的投影分散在十几处，漏一处就出现「某些工具能绕过」）。
    门控层回查只碰自己的判断逻辑，对外零影响；代价是每条多一次
    O(1) 主键查询（`find_by_id`），而不是全表扫描。

    参数：
      · `record_getter(item)` —— 从一项里取出 record dict（可选）
      · `record_lookup(memory_id)` —— 按 id 回查真实记录（可选，**推荐**）
    """
    cfg = tier_config(tier)
    window = cfg.get("cold_window_days")
    if window is None or not gate_enabled():
        return list(items or []), 0

    kept, dropped = [], 0
    for item in (items or []):
        rec = None
        if record_getter is not None:
            try:
                rec = record_getter(item)
            except Exception:
                rec = None
        elif isinstance(item, dict):
            rec = item
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            rec = item[1] if isinstance(item[1], dict) else None

        # 投影里没有 tier → 按 memory_id 回查。回查失败时不排除（宁可多给）。
        if record_lookup is not None:
            mid = None
            if isinstance(rec, dict):
                mid = rec.get("id") or rec.get("memory_id")
            if mid:
                try:
                    real = record_lookup(mid)
                except Exception:
                    real = None
                if isinstance(real, dict):
                    rec = real

        if is_excluded_by_window(rec, tier, window):
            dropped += 1
            continue
        kept.append(item)
    return kept, dropped


def filter_audit_entries(entries, tier: str):
    """审计条目出口过滤。返回 `(kept, dropped_count)`。

    审计条目没有 `tier` 字段（它们不是记忆），所以这里**按时间**过滤，
    不做冷层判定 —— 审计窗口的语义是"能看多久的历史"，与记忆热度无关。

    FREE_TIER: audit window = 7d
    """
    cfg = tier_config(tier)
    window = cfg.get("audit_window_days")
    if window is None or not gate_enabled():
        return list(entries or []), 0

    cutoff = _parse_created(cold_cutoff_iso(window))
    if cutoff is None:
        return list(entries or []), 0

    kept, dropped = [], 0
    for row in (entries or []):
        ts = None
        if isinstance(row, dict):
            for key in ("at", "timestamp", "created_at", "ts", "time"):
                if row.get(key) is not None:
                    ts = row.get(key)
                    break
        dt = _parse_created(ts)
        if dt is not None and dt < cutoff:
            dropped += 1
            continue
        kept.append(row)
    return kept, dropped


def filter_export_records(records, tier: str):
    """导出记录过滤。返回 `(kept, dropped_count)`。

    与召回不同：导出的语义是"带走我的数据"，所以 Free 层按
    **热层 + 窗口内冷层**放行，超出窗口的冷层不导出（但**不删除**）。

    FREE_TIER: export = hot/warm + cold within 30d; nothing is ever deleted.
    """
    cfg = tier_config(tier)
    window = cfg.get("export_window_days")
    if window is None or not gate_enabled():
        return list(records or []), 0
    kept, dropped = [], 0
    for rec in (records or []):
        if is_excluded_by_window(rec, tier, window):
            dropped += 1
            continue
        kept.append(rec)
    return kept, dropped


def namespace_allowed(tier: str, want: str, bound: str) -> bool:
    """Free 层只允许 token 绑定的那一个 namespace。"""
    if not gate_enabled():
        return True
    cfg = tier_config(tier)
    if cfg.get("cross_namespace"):
        return True
    if not want or not bound:
        return True
    return str(want) == str(bound)


def upgrade_hint(tier: str, dropped: int, kind: str = "cold") -> dict | None:
    """生成 `upgrade_hint` 字段。

    Design note:「不是广告，是数据信号」。所以这里只陈述**事实**：
    有多少条因为窗口被排除、升级后能拿回什么。不写营销话术。
    """
    if not dropped or not gate_enabled():
        return None
    cfg = tier_config(tier)
    if cfg.get("cold_window_days") is None:
        return None
    if kind == "audit":
        return {
            "excluded_count": dropped,
            "reason": "audit_window",
            "window_days": cfg.get("audit_window_days"),
            "restored_by": "pro",
            "note": ("%d audit entries are older than the %s-day Free audit window."
                     % (dropped, cfg.get("audit_window_days"))),
        }
    if kind == "export":
        return {
            "excluded_count": dropped,
            "reason": "export_window",
            "window_days": cfg.get("export_window_days"),
            "restored_by": "pro",
            "note": ("%d cold-tier memories fall outside the %s-day Free export window. "
                     "They are still stored and were not deleted."
                     % (dropped, cfg.get("export_window_days"))),
        }
    return {
        "excluded_count": dropped,
        "reason": "cold_window",
        "window_days": cfg.get("cold_window_days"),
        "restored_by": "pro",
        "note": ("%d cold-tier memories are older than the %s-day Free window and were "
                 "not recalled. They are still stored and were not deleted."
                 % (dropped, cfg.get("cold_window_days"))),
    }
