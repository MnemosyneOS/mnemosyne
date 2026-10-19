# -*- coding: utf-8 -*-
"""工作记忆：任务事实层 + 经验层（v8.1.0）
==========================================

填补 8.0.0 的空白：**只有 ``todo`` 类型，没有「任务」与「经验」的结构**。
结果是每次任务都从零开始，一次投入无法产生复利。

两层物理分离（对应principle第 7 章）：

======================  ==========================  ==========================
层                      回答什么                    写入时机
======================  ==========================  ==========================
事实层 ``TaskFact``     这次做了什么？结果如何？      任务结束立即写（只追加）
经验层 ``Playbook``     这类事该怎么做？何时有效？    同类任务成功 ≥2 次后归纳
======================  ==========================  ==========================

**经验必须带适用条件**。这是本模块与「存一条笔记」的本质区别：
不带 ``preconditions`` 的经验会在条件不匹配时主动把 Agent 带沟里
（Agent Workflow Memory 的实验已观察到这一负迁移）。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

__all__ = ["TaskFact", "Playbook", "WorkingMemory"]


def _clip(s: str, n: int = 160) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _as_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in re.split(r"[;；,，\n]", v) if x.strip()]
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v)]


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

class TaskFact:
    """一次任务的事实记录（不可变，只追加）。"""

    __slots__ = ("task_id", "goal", "goal_type", "context", "steps",
                 "artifacts", "outcome", "duration_min", "rework_count",
                 "user_feedback", "started_at")

    def __init__(self, goal: str, goal_type: str = "general",
                 context: Optional[Dict[str, Any]] = None,
                 steps: Optional[Sequence[Dict[str, Any]]] = None,
                 artifacts: Optional[Sequence[str]] = None,
                 outcome: str = "success", duration_min: float = 0.0,
                 rework_count: int = 0, user_feedback: str = "unknown",
                 started_at: Optional[str] = None,
                 task_id: Optional[str] = None):
        import hashlib
        import time
        self.goal = _clip(goal, 300)
        self.goal_type = goal_type or "general"
        self.context = context or {}
        self.steps = list(steps or [])
        self.artifacts = _as_list(artifacts)
        self.outcome = outcome
        self.duration_min = float(duration_min or 0)
        self.rework_count = int(rework_count or 0)
        self.user_feedback = user_feedback
        self.started_at = started_at or time.strftime("%Y-%m-%dT%H:%M:%S")
        self.task_id = task_id or ("t_" + hashlib.sha1(
            (self.goal + self.started_at).encode("utf-8")).hexdigest()[:12])

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__slots__}

    def render(self) -> str:
        """渲染成一条可读的记忆正文。"""
        parts = ["【任务事实 · %s · %s】" % (self.task_id, self.started_at),
                 "目标：%s（类型 %s）" % (self.goal, self.goal_type)]
        cons = _as_list(self.context.get("constraints"))
        if cons:
            parts.append("约束：" + "；".join(cons))
        pre = _as_list(self.context.get("preconditions"))
        if pre:
            parts.append("前置条件：" + "；".join(pre))
        if self.steps:
            lines = []
            for i, st in enumerate(self.steps, 1):
                if not isinstance(st, dict):
                    lines.append("%d. %s" % (i, st))
                    continue
                seg = "%d. %s → %s" % (i, st.get("action", ""), st.get("result", ""))
                if st.get("reason"):
                    seg += "（%s）" % st["reason"]
                lines.append(seg)
            parts.append("步骤：" + " / ".join(lines))
        if self.artifacts:
            parts.append("产物：" + "；".join(self.artifacts))
        parts.append("结果：%s｜返工 %d 次｜用户反馈 %s"
                     % (self.outcome, self.rework_count, self.user_feedback))
        return "\n".join(parts)


class Playbook:
    """一条可复用的经验。

    ``preconditions`` 是灵魂：``must`` / ``must_not`` / ``typical_env`` 三件套。
    ``failure_modes`` 与 ``anti_patterns`` 决定它会不会被误用。
    """

    __slots__ = ("playbook_id", "goal_type", "pattern", "preconditions",
                 "steps", "stats", "failure_modes", "anti_patterns", "scope",
                 "evidence")

    def __init__(self, pattern: str, goal_type: str = "general",
                 preconditions: Optional[Dict[str, Any]] = None,
                 steps: Optional[Sequence[str]] = None,
                 failure_modes: Optional[Sequence[Dict[str, Any]]] = None,
                 anti_patterns: Optional[Sequence[str]] = None,
                 scope: Optional[Dict[str, Any]] = None,
                 evidence: Optional[Sequence[str]] = None,
                 playbook_id: Optional[str] = None):
        import hashlib
        self.pattern = _clip(pattern, 300)
        self.goal_type = goal_type or "general"
        self.preconditions = preconditions or {"must": [], "must_not": [], "typical_env": []}
        self.steps = _as_list(steps)
        self.stats = {"applied": 0, "succeeded": 0, "failed": 0,
                      "success_rate": 0.0, "last_used": None}
        self.failure_modes = list(failure_modes or [])
        self.anti_patterns = _as_list(anti_patterns)
        self.scope = scope or {}
        self.evidence = _as_list(evidence)
        self.playbook_id = playbook_id or ("pb_" + hashlib.sha1(
            (self.pattern + self.goal_type).encode("utf-8")).hexdigest()[:12])

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__slots__}

    def render(self) -> str:
        parts = ["【经验 · %s · %s】" % (self.playbook_id, self.goal_type),
                 "模式：%s" % self.pattern]
        pre = self.preconditions or {}
        must = _as_list(pre.get("must"))
        must_not = _as_list(pre.get("must_not"))
        env = _as_list(pre.get("typical_env"))
        if must:
            parts.append("适用前提：" + "；".join(must))
        if must_not:
            parts.append("不适用：" + "；".join(must_not))
        if env:
            parts.append("典型环境：" + "；".join(env))
        if self.steps:
            parts.append("步骤：" + " / ".join(self.steps))
        if self.failure_modes:
            fm = ["%s → %s" % (f.get("when", ""), f.get("why", ""))
                  for f in self.failure_modes if isinstance(f, dict)]
            parts.append("已知失败模式：" + "；".join(fm))
        if self.anti_patterns:
            parts.append("反模式：" + "；".join(self.anti_patterns))
        st = self.stats or {}
        parts.append("成功率：%d/%d（%.0f%%）"
                     % (st.get("succeeded", 0), st.get("applied", 0),
                        100 * float(st.get("success_rate") or 0)))
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# 工作记忆
# ---------------------------------------------------------------------------

class WorkingMemory:
    """任务事实层 + 经验层的读写门面。

    只依赖传入的 ``brain``（需提供 ``retain`` / ``recall``），因此可独立测试，
    也不改动 ``Brain`` 的任何既有逻辑。
    """

    TAG_TASK = "task_fact"
    TAG_PLAYBOOK = "playbook"

    def __init__(self, brain: Any):
        self.brain = brain

    # ---- 事实层 -------------------------------------------------------

    def record_task(self, goal: str, goal_type: str = "general",
                    context: Optional[Dict[str, Any]] = None,
                    steps: Optional[Sequence[Dict[str, Any]]] = None,
                    artifacts: Optional[Sequence[str]] = None,
                    outcome: str = "success",
                    duration_min: float = 0.0,
                    rework_count: int = 0,
                    user_feedback: str = "unknown",
                    started_at: Optional[str] = None,
                    project: Optional[str] = None) -> Dict[str, Any]:
        """记录一次任务（只追加，不改写历史）。返回 task 字典。"""
        task = TaskFact(goal=goal, goal_type=goal_type, context=context,
                        steps=steps, artifacts=artifacts, outcome=outcome,
                        duration_min=duration_min, rework_count=rework_count,
                        user_feedback=user_feedback, started_at=started_at)
        mem_id = self.brain.retain(
            task.render(),
            mtype="episodic",
            tags=[self.TAG_TASK, "goal_type:%s" % task.goal_type,
                  "outcome:%s" % task.outcome],
            importance=4,
            project=project,
            meta={"kind": "task_fact", "task": task.to_dict()},
        )
        return {"task_id": task.task_id, "memory_id": mem_id, "task": task.to_dict()}

    def recall_tasks(self, goal_type: str = "", query: str = "",
                     k: int = 5, project: Optional[str] = None) -> List[Dict[str, Any]]:
        """检索同类任务案例（给「上次怎么做的」提供参考）。"""
        q = query or ("任务 %s 结果" % goal_type if goal_type else "任务")
        tags = [self.TAG_TASK]
        if goal_type:
            tags.append("goal_type:%s" % goal_type)
        try:
            hits = self.brain.recall(q, k=k, tags=tags, project=project)
        except TypeError:
            hits = self.brain.recall(q, k=k)
        out = []
        for item in hits or []:
            rec = item[1] if isinstance(item, (list, tuple)) and len(item) >= 2 else item
            if isinstance(rec, dict):
                out.append(rec.get("meta", {}).get("task") or
                           {"content": rec.get("content", "")})
        return out

    # ---- 经验层 -------------------------------------------------------

    def save_playbook(self, pattern: str, goal_type: str = "general",
                      preconditions: Optional[Dict[str, Any]] = None,
                      steps: Optional[Sequence[str]] = None,
                      failure_modes: Optional[Sequence[Dict[str, Any]]] = None,
                      anti_patterns: Optional[Sequence[str]] = None,
                      scope: Optional[Dict[str, Any]] = None,
                      evidence: Optional[Sequence[str]] = None,
                      project: Optional[str] = None) -> Dict[str, Any]:
        """沉淀一条经验。**必须**给出 preconditions，否则经验会变成偏见。"""
        pb = Playbook(pattern=pattern, goal_type=goal_type,
                      preconditions=preconditions, steps=steps,
                      failure_modes=failure_modes, anti_patterns=anti_patterns,
                      scope=scope, evidence=evidence)
        mem_id = self.brain.retain(
            pb.render(),
            mtype="procedural",
            tags=[self.TAG_PLAYBOOK, "goal_type:%s" % pb.goal_type],
            importance=5,
            project=project,
            meta={"kind": "playbook", "playbook": pb.to_dict()},
        )
        return {"playbook_id": pb.playbook_id, "memory_id": mem_id,
                "playbook": pb.to_dict()}

    def recall_playbooks(self, goal_type: str = "", context: Optional[Dict[str, Any]] = None,
                         k: int = 3, project: Optional[str] = None) -> List[Dict[str, Any]]:
        """检索候选经验，并做**前置条件校验**。

        返回项带 ``applicable`` / ``blocked_by``：调用方应只把 ``applicable=True``
        的经验注入上下文，其余作为「不适用参考」丢弃——这是防负迁移的关键一步。
        """
        q = "经验 %s 方法" % (goal_type or "任务")
        tags = [self.TAG_PLAYBOOK]
        if goal_type:
            tags.append("goal_type:%s" % goal_type)
        try:
            hits = self.brain.recall(q, k=max(k * 2, 6), tags=tags, project=project)
        except TypeError:
            hits = self.brain.recall(q, k=max(k * 2, 6))

        ctx_text = " ".join(str(v) for v in (context or {}).values())
        out = []
        for item in hits or []:
            rec = item[1] if isinstance(item, (list, tuple)) and len(item) >= 2 else item
            if not isinstance(rec, dict):
                continue
            pb = (rec.get("meta") or {}).get("playbook")
            if not pb:
                continue
            applicable, blocked = self._check_preconditions(pb, ctx_text)
            entry = dict(pb)
            entry["applicable"] = applicable
            entry["blocked_by"] = blocked
            entry["memory_id"] = rec.get("id")
            out.append(entry)
            if len(out) >= k:
                break
        return out

    @staticmethod
    def _check_preconditions(playbook: Dict[str, Any],
                             ctx_text: str):
        """前置条件校验：must 必须全部出现，must_not 一个都不能出现。"""
        pre = playbook.get("preconditions") or {}
        blocked: List[str] = []
        for m in _as_list(pre.get("must")):
            if m and m not in ctx_text:
                blocked.append("缺少必要条件：%s" % m)
        for m in _as_list(pre.get("must_not")):
            if m and m in ctx_text:
                blocked.append("命中排除条件：%s" % m)
        return (len(blocked) == 0), blocked

    def update_playbook_stats(self, playbook_id: str, success: bool,
                              memory_id: Optional[str] = None) -> Dict[str, Any]:
        """回写一次应用结果。

        成功率只统计最近窗口（这里用累计近似，窗口化由调用方按需裁剪）。
        连续失败 3 次的降级由 ``should_demote`` 判断。
        """
        store = getattr(self.brain, "store", None)
        if store is None or not memory_id:
            return {"updated": False, "reason": "缺少 store 或 memory_id"}
        rec = store.find_by_id(memory_id)
        if not rec:
            return {"updated": False, "reason": "未找到记忆 %s" % memory_id}
        meta = rec.get("meta") or {}
        pb = meta.get("playbook") or {}
        stats = pb.get("stats") or {"applied": 0, "succeeded": 0, "failed": 0}
        stats["applied"] = int(stats.get("applied", 0)) + 1
        if success:
            stats["succeeded"] = int(stats.get("succeeded", 0)) + 1
        else:
            stats["failed"] = int(stats.get("failed", 0)) + 1
        applied = max(1, stats["applied"])
        stats["success_rate"] = round(stats["succeeded"] / float(applied), 3)
        import time
        stats["last_used"] = time.strftime("%Y-%m-%d")
        pb["stats"] = stats
        meta["playbook"] = pb
        store.update_by_id(memory_id, {"meta": meta,
                                       "confidence": max(0.2, min(1.0, stats["success_rate"] + 0.2))})
        return {"updated": True, "stats": stats,
                "should_demote": stats["success_rate"] < 0.4 and applied >= 3}
