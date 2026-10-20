# -*- coding: utf-8 -*-
"""三层记忆的度量指标。


## 一条原则：数据不足就说不足

这些指标都依赖「真实使用数据」，冷启动时必然算不出来。**不造假数是底线**：
每个指标返回 ``{"value": None, "status": "insufficient_data", "need": "..."}``，
而不是用 0 或估算值填满仪表盘。0 会被误读成「很差」，估出来的数会被当成
结论用 —— 这比没有指标更糟。

## Chat Memory（6.5）

| 指标 | 定义 | 健康值 | 数据来源 |
|---|---|---|---|
| 首轮有效信息密度 | 对话第一轮中模型已正确使用的已知信息条数 | ≥3 | 需上游标注，本模块给代理指标 |
| 重复提问率 | 模型询问用户已告知信息的次数 / 总提问数 | <5% | 需上游标注 |
| 用户重复陈述率 | 用户主动重复说明同一背景的次数 / 会话数 | 趋近 0 | 可由会话摘要的 constraints 重复度代理 |
| 错误记忆存活时长 | 错误记忆从写入到被纠正/失效的时间 | <7 天 | superseded_at − created_at |
| 记忆命中率 | 召回结果中被实际引用的比例 | >40% | 需引用回传；用 access_count 代理 |

## Working Memory（7.6）

| 指标 | 定义 | 不达标说明 |
|---|---|---|
| 同类任务耗时下降率 | 第 5 次 vs 第 1 次同类任务耗时 | 没下降 = 经验没被复用 |
| 经验采纳率 | 注入的 playbook 被实际采用的比例 | <20% = 检索不准 |
| 负迁移率 | 采用经验后反而失败的比例 | >15% = 前置条件太松 |
| 返工次数 | 同一任务的重复修改轮数 | 不降 = 经验质量低 |

## Coding Memory（8.6）

| 指标 | 定义 |
|---|---|
| 首次改对率 | 一次提交即通过 review 的比例（用 rework_count=0 代理） |
| 回归率 | 改动导致既有测试失败的比例（用 regression 标记） |
| 重复犯错率 | 犯过的同类错误再次出现的次数（负记忆 recurrence） |
| 考古命中率 | 考古检索结果被实际引用的比例 |
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "chat_metrics",
    "working_metrics",
    "coding_metrics",
    "all_metrics",
    "HEALTHY",
]


#: 健康值区间，来自principle表格。用于前端标红/标绿。
HEALTHY: Dict[str, Dict[str, Any]] = {
    "first_turn_density": {"min": 3, "unit": "条"},
    "repeat_question_rate": {"max": 0.05, "unit": "ratio"},
    "user_restate_rate": {"max": 0.05, "unit": "ratio"},
    "wrong_memory_lifetime_days": {"max": 7, "unit": "days"},
    "hit_rate": {"min": 0.40, "unit": "ratio"},
    "task_duration_drop": {"min": 0.40, "unit": "ratio"},
    "playbook_adoption_rate": {"min": 0.20, "unit": "ratio"},
    "negative_transfer_rate": {"max": 0.15, "unit": "ratio"},
    "rework_count_avg": {"max": 1.0, "unit": "次"},
    "first_pass_rate": {"min": 0.70, "unit": "ratio"},
    "regression_rate": {"max": 0.10, "unit": "ratio"},
    "repeat_mistake_rate": {"max": 0.05, "unit": "ratio"},
    "archaeology_hit_rate": {"min": 0.40, "unit": "ratio"},
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _metric(value: Optional[float], healthy_key: str,
            detail: Optional[Dict[str, Any]] = None,
            need: str = "") -> Dict[str, Any]:
    """统一的指标返回结构。

    ``value=None`` 时 status 为 ``insufficient_data``，并说明缺什么数据。
    """
    rule = HEALTHY.get(healthy_key, {})
    if value is None:
        return {"value": None, "status": "insufficient_data",
                "need": need or "更多使用数据", "healthy": rule,
                "ok": None, "detail": detail or {}}
    ok = None
    if "min" in rule:
        ok = value >= rule["min"]
    elif "max" in rule:
        ok = value <= rule["max"]
    return {"value": round(value, 4), "status": "ok", "healthy": rule,
            "ok": ok, "detail": detail or {}}


def _active(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [r for r in records
            if isinstance(r, dict)
            and str(r.get("status") or "active") in ("active", "superseded", "deleted")
            and not r.get("_corrupt")]


# ---------------------------------------------------------------------------
# Chat Memory
# ---------------------------------------------------------------------------

def chat_metrics(records: Sequence[Dict[str, Any]],
                 stats: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Chat Memory 五项指标。

    其中「首轮信息密度」「重复提问率」需要上游标注（模型是否用了已知信息、
    是否问了已告知的事），本模块只能给代理指标并如实标注 ``proxy``。
    不把代理值当真实值报出去 —— 那会让人误以为已经达标。
    """
    recs = _active(records)
    now = _now()

    # ---- 错误记忆存活时长：superseded_at − created_at ----
    # 顶层字段优先，meta 兜底 —— SQLite 后端的列白名单不含 superseded_at，
    # update_by_id 会把它并入 meta（见 storage/sqlite_backend.py 的说明）。
    lifetimes: List[float] = []
    for r in recs:
        if str(r.get("status") or "") != "superseded":
            continue
        meta = r.get("meta") if isinstance(r.get("meta"), dict) else {}
        end = _parse(r.get("superseded_at") or r.get("t_invalid")
                     or meta.get("superseded_at"))
        start = _parse(r.get("created_at") or r.get("knowledge_time"))
        if end and start and end >= start:
            lifetimes.append((end - start).total_seconds() / 86400.0)

    # ---- 记忆命中率：access_count > 0 的比例 ----
    touched = 0
    total = 0
    for r in recs:
        if str(r.get("status") or "active") != "active":
            continue
        total += 1
        try:
            if int(r.get("access_count") or 0) > 0:
                touched += 1
        except (TypeError, ValueError):
            pass

    # ---- 用户重复陈述率（代理）：constraints 在多个会话摘要里重复出现 ----
    cons_counter: Dict[str, int] = {}
    summaries = 0
    for r in recs:
        meta = r.get("meta") or {}
        if not isinstance(meta, dict):
            continue
        if meta.get("kind") != "session_summary":
            continue
        summaries += 1
        for c in (meta.get("constraints") or []):
            key = str(c).strip()
            if key:
                cons_counter[key] = cons_counter.get(key, 0) + 1
    restate = None
    if summaries >= 3 and cons_counter:
        repeated = sum(1 for v in cons_counter.values() if v >= 2)
        restate = repeated / float(len(cons_counter))

    out: Dict[str, Any] = {}
    out["wrong_memory_lifetime_days"] = _metric(
        statistics.mean(lifetimes) if lifetimes else None,
        "wrong_memory_lifetime_days",
        detail={"samples": len(lifetimes),
                "max_days": round(max(lifetimes), 2) if lifetimes else None},
        need="至少一条被 superseded 的记忆")

    out["hit_rate"] = _metric(
        (touched / float(total)) if total else None, "hit_rate",
        detail={"touched": touched, "active": total,
                "note": "以 access_count>0 为代理；真实命中率需上游回传「被引用」信号"},
        need="至少一条 active 记忆")

    out["user_restate_rate"] = _metric(
        restate, "user_restate_rate",
        detail={"summaries": summaries,
                "note": "以会话摘要 constraints 的跨会话重复度为代理"},
        need="至少 3 份会话摘要")

    # 这两项没有上游标注就算不出来，明确报 insufficient_data
    out["first_turn_density"] = _metric(
        None, "first_turn_density",
        need="需上游标注「首轮回答中正确使用的已知信息条数」")
    out["repeat_question_rate"] = _metric(
        None, "repeat_question_rate",
        need="需上游标注「询问用户已告知信息的次数 / 总提问数」")

    # 附上引擎自带的真实计数（如果有），避免全部不可用
    if stats:
        out["engine_stats"] = {
            "total_retain": stats.get("total_retain"),
            "total_recall": stats.get("total_recall"),
            "saved_tokens": stats.get("saved_tokens"),
            "hit_rate_raw": stats.get("hit_rate"),
        }
    return out


# ---------------------------------------------------------------------------
# Working Memory
# ---------------------------------------------------------------------------

def working_metrics(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Working Memory 四项指标。全部从 TaskFact 真实字段计算。"""
    recs = _active(records)
    tasks: List[Dict[str, Any]] = []
    playbooks: List[Dict[str, Any]] = []
    for r in recs:
        meta = r.get("meta") or {}
        if not isinstance(meta, dict):
            continue
        if meta.get("kind") == "task_fact":
            t = meta.get("task") or {}
            if isinstance(t, dict) and t:
                tasks.append(t)
        elif meta.get("kind") == "playbook":
            pb = meta.get("playbook") or {}
            if isinstance(pb, dict) and pb:
                playbooks.append(pb)

    # ---- 同类任务耗时下降率：按 goal_type 分组，首末对比 ----
    by_gt: Dict[str, List[Tuple[str, float]]] = {}
    for t in tasks:
        gt = str(t.get("goal_type") or "general")
        dur = float(t.get("duration_min") or 0)
        started = str(t.get("started_at") or "")
        if dur > 0:
            by_gt.setdefault(gt, []).append((started, dur))
    drops: List[float] = []
    detail_drop: Dict[str, Any] = {}
    for gt, pairs in by_gt.items():
        if len(pairs) < 2:
            continue
        pairs.sort(key=lambda x: x[0])
        first = pairs[0][1]
        last = pairs[-1][1]
        if first > 0:
            drops.append((first - last) / first)
            detail_drop[gt] = {"first_min": first, "last_min": last,
                               "runs": len(pairs)}

    # ---- 返工次数 ----
    reworks = [float(t.get("rework_count") or 0) for t in tasks]

    # ---- 经验采纳率 / 负迁移率 ----
    # 采纳：playbook.stats.applied > 0 的比例；负迁移：failed/applied
    adopted = 0
    applied_total = 0
    failed_total = 0
    for pb in playbooks:
        st = pb.get("stats") or {}
        if not isinstance(st, dict):
            continue
        a = int(st.get("applied") or 0)
        f = int(st.get("failed") or 0)
        applied_total += a
        failed_total += f
        if a > 0:
            adopted += 1

    out: Dict[str, Any] = {}
    out["task_duration_drop"] = _metric(
        statistics.mean(drops) if drops else None, "task_duration_drop",
        detail={"goal_types": detail_drop},
        need="同一 goal_type 至少 2 次带耗时的任务")

    out["rework_count_avg"] = _metric(
        statistics.mean(reworks) if reworks else None, "rework_count_avg",
        detail={"tasks": len(reworks),
                "zero_rework": sum(1 for x in reworks if x == 0)},
        need="至少一条任务事实")

    out["playbook_adoption_rate"] = _metric(
        (adopted / float(len(playbooks))) if playbooks else None,
        "playbook_adoption_rate",
        detail={"playbooks": len(playbooks), "adopted": adopted,
                "note": "以 stats.applied>0 为已采纳"},
        need="至少一条经验（playbook）")

    out["negative_transfer_rate"] = _metric(
        (failed_total / float(applied_total)) if applied_total else None,
        "negative_transfer_rate",
        detail={"applied": applied_total, "failed": failed_total},
        need="至少一次经验应用记录")

    out["counts"] = {"tasks": len(tasks), "playbooks": len(playbooks)}
    return out


# ---------------------------------------------------------------------------
# Coding Memory
# ---------------------------------------------------------------------------

def coding_metrics(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Coding Memory 四项指标。全部从 ChangeEvent 真实字段计算。"""
    recs = _active(records)
    changes: List[Dict[str, Any]] = []
    anti: List[Dict[str, Any]] = []
    for r in recs:
        meta = r.get("meta") or {}
        if not isinstance(meta, dict):
            continue
        if meta.get("kind") == "code_change":
            c = meta.get("change") or {}
            if isinstance(c, dict) and c:
                changes.append(c)
        elif meta.get("kind") == "anti_pattern":
            anti.append(meta)

    merged = [c for c in changes if str(c.get("outcome") or "") == "merged"]
    reverted = [c for c in changes if str(c.get("outcome") or "") == "reverted"]

    # 首次改对率：merged 且 review_notes 为空 且未标记 regression
    first_pass = [c for c in merged
                  if not (c.get("review_notes") or [])
                  and not c.get("regression")]
    regression = [c for c in changes if c.get("regression")]

    # 重复犯错率：负记忆里 recurrence>=2 的比例
    recur = [a for a in anti if int(a.get("recurrence") or 1) >= 2]

    out: Dict[str, Any] = {}
    out["first_pass_rate"] = _metric(
        (len(first_pass) / float(len(merged))) if merged else None,
        "first_pass_rate",
        detail={"merged": len(merged), "first_pass": len(first_pass)},
        need="至少一次 merged 变更")

    out["regression_rate"] = _metric(
        (len(regression) / float(len(changes))) if changes else None,
        "regression_rate",
        detail={"changes": len(changes), "regression": len(regression)},
        need="至少一次变更事件")

    out["repeat_mistake_rate"] = _metric(
        (len(recur) / float(len(anti))) if anti else None,
        "repeat_mistake_rate",
        detail={"anti_patterns": len(anti), "recurring": len(recur)},
        need="至少一条负记忆")

    out["archaeology_hit_rate"] = _metric(
        None, "archaeology_hit_rate",
        need="需上游回传「考古结果被引用」信号")

    out["counts"] = {"changes": len(changes), "merged": len(merged),
                     "reverted": len(reverted), "anti_patterns": len(anti)}
    return out


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def all_metrics(records: Sequence[Dict[str, Any]],
                stats: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """三层指标汇总，附带一份「哪些指标可用」的清单。

    前端用 ``availability`` 决定显示哪些卡片 —— 冷启动时显示「数据不足」
    比显示一堆 0 更诚实。
    """
    chat = chat_metrics(records, stats=stats)
    work = working_metrics(records)
    code = coding_metrics(records)

    availability: Dict[str, int] = {"ok": 0, "insufficient_data": 0}
    for group in (chat, work, code):
        for k, v in group.items():
            if isinstance(v, dict) and "status" in v:
                availability[v["status"]] = availability.get(v["status"], 0) + 1

    return {
        "chat": chat,
        "working": work,
        "coding": code,
        "availability": availability,
        "total_records": len(_active(records)),
        "at": _now().strftime("%Y-%m-%dT%H:%M:%S"),
    }
