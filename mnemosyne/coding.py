# -*- coding: utf-8 -*-
"""代码演进记忆（v8.1.0）
=========================

填补 8.0.0 的空白：**完全没有代码演进记忆**。编程 Agent 只能看到仓库当前状态，
看不到「这段代码为什么长这样」「试过什么不行」。

核心抽象不是 ``diff``，而是**变更事件**（``ChangeEvent``）：

    intent              这次改动想解决什么（git 里没有）
    constraints         当时有哪些约束（不能改 schema、线上迁移未就绪……）
    touched             影响的文件与**符号**（符号级锚点比文件级精准）
    rejected_approaches 试过但放弃的方案（git 永远不会有）
    outcome             merged / reverted / superseded
    lesson              提炼出的一句话教训

三种检索（对应principle 8.3）：

===============  ==================================================
代码考古          以文件/符号为锚，回溯它经历过的所有变更
决策回溯          以 issue/ADR 为锚，找回当时的约束与放弃理由
坑位预警          以任务意图为锚，检索同类改动中的失败与回滚
===============  ==================================================
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Dict, List, Optional, Sequence

__all__ = ["ChangeEvent", "CodingMemory"]


def _clip(s: str, n: int = 200) -> str:
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


class ChangeEvent:
    """一次代码变更事件。"""

    __slots__ = ("change_id", "repo", "commit", "parent", "intent",
                 "intent_type", "constraints", "linked", "files", "symbols",
                 "impact", "diff_summary", "rejected_approaches",
                 "review_notes", "outcome", "reverted_by", "regression",
                 "lesson", "at")

    def __init__(self, intent: str, repo: str = "", commit: str = "",
                 parent: Optional[str] = None, intent_type: str = "feature",
                 constraints: Optional[Sequence[str]] = None,
                 linked: Optional[Dict[str, str]] = None,
                 files: Optional[Sequence[str]] = None,
                 symbols: Optional[Sequence[str]] = None,
                 impact: Optional[Sequence[str]] = None,
                 diff_summary: str = "",
                 rejected_approaches: Optional[Sequence[Dict[str, str]]] = None,
                 review_notes: Optional[Sequence[Dict[str, str]]] = None,
                 outcome: str = "merged", lesson: str = "",
                 at: Optional[str] = None,
                 change_id: Optional[str] = None):
        self.intent = _clip(intent, 300)
        self.repo = repo or ""
        self.commit = commit or ""
        self.parent = parent
        self.intent_type = intent_type or "feature"
        self.constraints = _as_list(constraints)
        self.linked = linked or {}
        self.files = _as_list(files)
        self.symbols = _as_list(symbols)
        self.impact = _as_list(impact)
        self.diff_summary = _clip(diff_summary, 300)
        self.rejected_approaches = list(rejected_approaches or [])
        self.review_notes = list(review_notes or [])
        self.outcome = outcome
        self.reverted_by = None
        self.regression = outcome == "reverted"
        self.lesson = _clip(lesson, 300)
        self.at = at or time.strftime("%Y-%m-%dT%H:%M:%S")
        self.change_id = change_id or ("chg_" + hashlib.sha1(
            (self.repo + self.commit + self.intent + self.at).encode("utf-8")).hexdigest()[:12])

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__slots__}

    def render(self) -> str:
        parts = ["【代码变更 · %s · %s】" % (self.change_id, self.at),
                 "意图：%s（%s）" % (self.intent, self.intent_type)]
        if self.repo or self.commit:
            parts.append("仓库：%s @ %s" % (self.repo or "?", self.commit or "?"))
        if self.constraints:
            parts.append("约束：" + "；".join(self.constraints))
        if self.files:
            parts.append("文件：" + "；".join(self.files[:8]))
        if self.symbols:
            parts.append("符号：" + "；".join(self.symbols[:8]))
        if self.impact:
            parts.append("影响面：" + "；".join(self.impact[:8]))
        if self.diff_summary:
            parts.append("改动：" + self.diff_summary)
        if self.rejected_approaches:
            ra = []
            for r in self.rejected_approaches:
                if isinstance(r, dict):
                    ra.append("%s（放弃原因：%s）" % (r.get("what", ""), r.get("why_not", "")))
            if ra:
                parts.append("放弃的方案：" + "；".join(ra))
        if self.review_notes:
            rn = ["%s: %s" % (r.get("by", "?"), r.get("note", ""))
                  for r in self.review_notes if isinstance(r, dict)]
            if rn:
                parts.append("评审意见：" + "；".join(rn))
        parts.append("结果：%s" % self.outcome)
        if self.lesson:
            parts.append("教训：" + self.lesson)
        return "\n".join(parts)


class CodingMemory:
    """代码演进记忆的读写门面。只依赖 ``brain.retain`` / ``brain.recall``。"""

    TAG = "code_change"

    def __init__(self, brain: Any):
        self.brain = brain

    # ---- 写入 ---------------------------------------------------------

    def record_change(self, intent: str, repo: str = "", commit: str = "",
                      parent: Optional[str] = None, intent_type: str = "feature",
                      constraints: Optional[Sequence[str]] = None,
                      linked: Optional[Dict[str, str]] = None,
                      files: Optional[Sequence[str]] = None,
                      symbols: Optional[Sequence[str]] = None,
                      impact: Optional[Sequence[str]] = None,
                      diff_summary: str = "",
                      rejected_approaches: Optional[Sequence[Dict[str, str]]] = None,
                      review_notes: Optional[Sequence[Dict[str, str]]] = None,
                      outcome: str = "merged", lesson: str = "",
                      project: Optional[str] = None) -> Dict[str, Any]:
        """记录一次变更事件。"""
        ev = ChangeEvent(intent=intent, repo=repo, commit=commit, parent=parent,
                         intent_type=intent_type, constraints=constraints,
                         linked=linked, files=files, symbols=symbols, impact=impact,
                         diff_summary=diff_summary,
                         rejected_approaches=rejected_approaches,
                         review_notes=review_notes, outcome=outcome, lesson=lesson)

        tags = [self.TAG, "intent:%s" % ev.intent_type, "outcome:%s" % ev.outcome]
        if ev.repo:
            tags.append("repo:%s" % ev.repo)
        for f in ev.files[:6]:
            tags.append("file:%s" % f)
        for s in ev.symbols[:6]:
            tags.append("symbol:%s" % s)

        mem_id = self.brain.retain(
            ev.render(), mtype="episodic", tags=tags, importance=4,
            project=project,
            meta={"kind": "code_change", "change": ev.to_dict()},
        )
        return {"change_id": ev.change_id, "memory_id": mem_id,
                "change": ev.to_dict()}

    # ---- 检索 ---------------------------------------------------------

    def _records(self, tags: List[str], query: str, k: int,
                 project: Optional[str]) -> List[Dict[str, Any]]:
        try:
            hits = self.brain.recall(query, k=max(k * 3, 9), tags=tags, project=project)
        except TypeError:
            hits = self.brain.recall(query, k=max(k * 3, 9))
        out = []
        for item in hits or []:
            rec = item[1] if isinstance(item, (list, tuple)) and len(item) >= 2 else item
            if isinstance(rec, dict) and (rec.get("meta") or {}).get("change"):
                out.append(rec)
        return out

    def archaeology(self, target: str, repo: str = "", k: int = 8,
                    project: Optional[str] = None) -> Dict[str, Any]:
        """代码考古：以文件或符号为锚，回溯变更链。

        返回按时间倒序的事件列表 + 三行摘要（最近 3 次意图 / 是否被回滚 / 已知约束）。
        """
        target = (target or "").strip()
        is_symbol = not re.search(r"[\\/.]", target)
        tags = [self.TAG]
        if repo:
            tags.append("repo:%s" % repo)
        tags.append(("symbol:%s" if is_symbol else "file:%s") % target)

        recs = self._records(tags, "变更 %s" % target, k, project)
        events = [(r.get("meta") or {}).get("change") or {} for r in recs]
        events.sort(key=lambda e: e.get("at") or "", reverse=True)
        events = events[:k]

        reverted = [e for e in events if e.get("outcome") == "reverted"]
        constraints = []
        for e in events[:5]:
            for c in e.get("constraints") or []:
                if c not in constraints:
                    constraints.append(c)

        brief = {
            "recent_intents": ["%s（%s）" % (e.get("intent", ""), e.get("intent_type", ""))
                               for e in events[:3]],
            "ever_reverted": bool(reverted),
            "known_constraints": constraints[:5],
        }
        return {"target": target, "kind": "symbol" if is_symbol else "file",
                "events": events, "count": len(events), "brief": brief}

    def pitfall_check(self, intent: str, files: Optional[Sequence[str]] = None,
                      k: int = 5, project: Optional[str] = None) -> Dict[str, Any]:
        """坑位预警：这次要做的改动，历史上踩过什么坑。"""
        tags = [self.TAG]
        for f in _as_list(files)[:3]:
            tags.append("file:%s" % f)
        recs = self._records(tags, intent or "改动", k, project)
        pitfalls = []
        for r in recs:
            ev = (r.get("meta") or {}).get("change") or {}
            if ev.get("outcome") in ("reverted", "superseded") or ev.get("rejected_approaches"):
                pitfalls.append({
                    "change_id": ev.get("change_id"),
                    "intent": ev.get("intent"),
                    "outcome": ev.get("outcome"),
                    "rejected": ev.get("rejected_approaches") or [],
                    "lesson": ev.get("lesson"),
                })
        return {"intent": intent, "pitfalls": pitfalls[:k],
                "count": len(pitfalls),
                "advice": "改动前请先读这些失败记录，避免重复踩坑" if pitfalls else "未发现历史坑位"}

    def evolution_chain(self, change_id: str, k: int = 10,
                        project: Optional[str] = None) -> List[Dict[str, Any]]:
        """沿 parent 指针回溯演进链（最多 k 跳）。"""
        chain: List[Dict[str, Any]] = []
        cur = change_id
        seen = set()
        while cur and cur not in seen and len(chain) < k:
            seen.add(cur)
            recs = self._records([self.TAG], "变更 %s" % cur, 3, project)
            found = None
            for r in recs:
                ev = (r.get("meta") or {}).get("change") or {}
                if ev.get("change_id") == cur:
                    found = ev
                    break
            if not found:
                break
            chain.append(found)
            cur = found.get("parent")
        return chain
