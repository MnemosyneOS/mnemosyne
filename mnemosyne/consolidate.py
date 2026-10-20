# -*- coding: utf-8 -*-
"""巩固：从情景到语义到程序性的抽象升级。


## 为什么巩固必须「可量化触发」

巩固不是「整理」，它是记忆价值密度提升的唯一途径。但它是**最容易做错**的一环：
靠模型「感觉可以总结了」去抽象，会得到一堆过早抽象、过度泛化的错误经验——
这是 Working Memory 最典型的失败模式（the core conclusion of that principle）。

所以三条路径的触发条件全部硬编码成计数器，不接受模型自由裁量：

| 路径 | 触发条件 | 产出 |
|---|---|---|
| A 情景 → 语义 | 同一事实在 **≥3 个不同会话**中被提及 | 一条 semantic，置信度上调 |
| B 情景 → 程序性 | 同类任务（同 goal_type）**成功 ≥2 次** | 一条 playbook，带前置条件与成功率 |
| C 失败 → 负记忆 | 出现 failed / rolled_back / reverted | 一条 anti_pattern，永久保留 |

## 三条路径的共同约束

1. **只追加，不改写源记录**。源记录打上 ``meta.consolidated_into`` 指向新条目，
   但仍然 active —— 因为「这个结论是从哪几条原始记忆推出来的」必须可回溯，
   否则底层证据被删时无法重算（principle 5.5 用户删除级联的要求）。
2. **幂等**。同一批源记录重复跑巩固不会产生重复条目：用
   ``derived_from`` 的集合指纹做去重。
3. **可 dry_run**。先看会产出什么，再决定是否落库。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

__all__ = [
    "ConsolidationEngine",
    "TRIGGERS",
    "similarity",
    "fingerprint",
]


#: 三条路径的量化触发条件。改这里就是改策略，不需要动逻辑。
TRIGGERS: Dict[str, Dict[str, Any]] = {
    "semantic": {
        "min_sessions": 3,        # 至少出现在 3 个不同会话
        "min_similarity": 0.72,   # 簇内相似度下限
        "confidence_boost": 0.10,  # 每多一次印证上调多少
        "confidence_cap": 0.95,
    },
    "procedural": {
        "min_success": 2,         # 同类任务成功至少 2 次
        "min_step_overlap": 0.5,  # 步骤重合度下限（低于此说明不是同一类做法）
    },
    "anti_pattern": {
        "min_severity": "failed",  # failed 起；reverted 更高
    },
}

#: 巩固产物的标签，用于识别与去重。
TAG_CONSOLIDATED = "consolidated"
TAG_ANTI = "anti_pattern"

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]")


def _tokens(text: str) -> Set[str]:
    """粗粒度词元集合。

    中文按单字、英文/数字按整词。对「同一事实不同措辞」的聚类够用，
    且零依赖（不引分词器）。比 char-bigram 更稳：bigram 对语序敏感，
    而巩固要的是「说的是同一件事」而不是「句式相同」。
    """
    return {t.lower() for t in _TOKEN_RE.findall(text or "")}


def similarity(a: str, b: str) -> float:
    """相似度 = ``max(Jaccard, Dice)``，中文改写友好。

    【为什么不能只用 Jaccard】中文的"同一件事换个说法"会**增删字**：
    「项目采用 sqlite-vec 作为向量后端」vs「项目最终采用 sqlite-vec 做向量后端」
    实测 Jaccard 只有 0.667 —— 低于 0.72 的门限，于是跨 3 个会话复述的同一个
    事实永远聚不成一簇，**情景→语义这条路径对中文几乎失效**。

    Dice（``2|A∩B| / (|A|+|B|)``）对"长度略有差异的改写"更宽容：同例得 0.783。
    两者取大，既保留 Jaccard 对"话题不同"的惩罚，又吸收 Dice 对改写的容忍。

    风险与对冲：Dice 在"包含关系"上会给出高分（「喜欢咖啡」vs「喜欢咖啡和茶」
    得 0.857），可能把子集并进超集。这里靠两点兜住：
      ① 代表句取**最长**那条，被并掉的短句信息是超集的子集，不丢事实；
      ② ``_cluster`` 里加了原子守恒守卫：两边的数字/日期/型号等原子
         **都非空且不同**时，无论相似度多高都不合并（防「5 天」被并进「15 天」）。
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    jac = inter / float(len(ta | tb))
    dice = 2.0 * inter / float(len(ta) + len(tb))
    return max(jac, dice)


def _atoms_of(text: str) -> Set[str]:
    """内容原子集合（数字/日期/金额/型号…）。取不到时返回空集。"""
    try:
        from .utils import _content_atoms
        return set(_content_atoms(text).keys())
    except Exception:
        return set()


def _atoms_conflict(a: str, b: str) -> bool:
    """两段文本的原子是否**互斥**（都非空且不同 → 是两个不同的事实）。"""
    aa, ab = _atoms_of(a), _atoms_of(b)
    if not aa or not ab:
        return False
    return aa != ab


def fingerprint(items: Iterable[str]) -> str:
    """源记录集合的指纹，用于幂等去重。"""
    ids = sorted({str(x) for x in items if x})
    return hashlib.sha1("|".join(ids).encode("utf-8")).hexdigest()[:16]


def _now_iso() -> str:
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def session_of(record: Dict[str, Any]) -> str:
    """解析一条记忆属于哪个会话（三种历史写法都要认）。

    【为什么必须有这个函数】``semantic()`` 的触发条件是「同一事实出现在
    ≥3 个**不同会话**」。而"会话"在库里存在三种落法：

    1. 顶层 ``session_id``（``retain(session_id=...)`` 直传）；
    2. ``meta.session_id``（``session.summary_record`` 的写法）；
    3. 标签 ``session:<id>``（``summary_record`` / ``turn_record`` 都打）。

    原实现只读第 1 种，于是第 2、3 种写进来的记忆 ``session_id`` 全是空串，
    被 ``sessions.discard("")`` 丢掉 → 跨会话计数恒为 0 → **情景→语义这条
    路径永远不触发**。而第 2、3 种恰恰是会话摘要层的主要产出形态，
    等于这条最重要的巩固路径对真实数据完全失效。
    """
    sid = record.get("session_id")
    if sid:
        return str(sid).strip()
    meta = record.get("meta") or {}
    if isinstance(meta, dict) and meta.get("session_id"):
        return str(meta["session_id"]).strip()
    for t in (record.get("tags") or []):
        s = str(t)
        if s.lower().startswith("session:"):
            v = s.split(":", 1)[1].strip()
            if v:
                return v
    return ""


class ConsolidationEngine:
    """巩固引擎。所有方法都不修改源记录，只新增派生条目。"""

    def __init__(self, brain: Any):
        self.brain = brain

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _records(self) -> List[Dict[str, Any]]:
        try:
            return [r for r in self.brain.store.all_records()
                    if isinstance(r, dict)
                    and str(r.get("status") or "active") == "active"
                    and not r.get("_corrupt")]
        except Exception:
            return []

    @staticmethod
    def _already(records: Sequence[Dict[str, Any]], fp: str) -> bool:
        """是否已有覆盖同一批源的派生条目（幂等检查）。"""
        for r in records:
            meta = r.get("meta") or {}
            if not isinstance(meta, dict):
                continue
            if meta.get("consolidation_fp") == fp:
                return True
        return False

    def _write(self, content: str, mtype: str, tags: List[str],
               meta: Dict[str, Any], importance: int = 4,
               project: Optional[str] = None,
               dry_run: bool = False) -> Optional[str]:
        if dry_run:
            return None
        return self.brain.retain(content, mtype=mtype, tags=tags,
                                 importance=importance, project=project,
                                 meta=meta)

    # ------------------------------------------------------------------
    # 路径 A：情景 → 语义
    # ------------------------------------------------------------------

    def semantic(self, min_sessions: Optional[int] = None,
                 min_similarity: Optional[float] = None,
                 dry_run: bool = False) -> Dict[str, Any]:
        """同一事实在 ≥N 个不同会话中被提及 → 升为稳定事实。

        为什么以「不同会话数」而不是「出现次数」为触发条件：同一次对话里
        用户连说三遍同一件事，是强调；跨三次会话都说，才是稳定事实。
        """
        cfg = TRIGGERS["semantic"]
        need_sessions = int(min_sessions if min_sessions is not None
                            else cfg["min_sessions"])
        threshold = float(min_similarity if min_similarity is not None
                          else cfg["min_similarity"])

        records = self._records()
        # 候选：还没被升级过的非语义类记忆
        pool = [r for r in records
                if str(r.get("type") or "") not in ("semantic", "identity")
                and str(r.get("mtype") or "") not in ("semantic", "identity")
                and not (r.get("meta") or {}).get("consolidated_into")]

        clusters = self._cluster(pool, threshold)
        promoted: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []

        for members in clusters:
            sessions = {session_of(m) for m in members}
            sessions.discard("")
            if len(sessions) < need_sessions:
                continue
            ids = [m.get("id") for m in members if m.get("id")]
            fp = fingerprint(ids)
            if self._already(records, fp):
                skipped.append({"reason": "already_consolidated", "ids": ids})
                continue

            # 代表句：取最长的那条（信息量通常最大），而不是随便取第一条
            rep = max(members, key=lambda m: len(str(m.get("content") or "")))
            base_conf = sum(float(m.get("confidence") or 0.7) for m in members) / len(members)
            boost = cfg["confidence_boost"] * (len(sessions) - need_sessions + 1)
            conf = min(cfg["confidence_cap"], base_conf + boost)

            content = str(rep.get("content") or "").strip()
            meta = {
                "kind": "consolidated_semantic",
                "consolidation_fp": fp,
                "derived_from": ids,
                "session_count": len(sessions),
                "source_count": len(members),
                "original_confidence": round(base_conf, 3),
                "consolidated_at": _now_iso(),
            }
            mid = self._write(content, "semantic",
                              tags=[TAG_CONSOLIDATED, "semantic_promoted"],
                              meta=meta, importance=5,
                              project=rep.get("project"), dry_run=dry_run)
            # 源记录打标（不删除、不失效）
            if not dry_run:
                for m in members:
                    try:
                        mm = m.setdefault("meta", {})
                        if isinstance(mm, dict):
                            mm["consolidated_into"] = mid or ""
                    except Exception:
                        pass
                # 置信度同步上调，让后续冲突判定看到「多次印证」这个信号
                for m in members:
                    try:
                        if float(m.get("confidence") or 0) < conf:
                            m["confidence"] = round(conf, 3)
                    except (TypeError, ValueError):
                        pass
            promoted.append({"memory_id": mid, "content": content[:120],
                             "sessions": len(sessions), "sources": len(members),
                             "confidence": round(conf, 3), "derived_from": ids})

        return {"path": "semantic", "trigger": {"min_sessions": need_sessions,
                                                "min_similarity": threshold},
                "candidates": len(pool), "clusters": len(clusters),
                "promoted": promoted, "skipped": skipped,
                "dry_run": dry_run}

    def _cluster(self, pool: Sequence[Dict[str, Any]],
                 threshold: float) -> List[List[Dict[str, Any]]]:
        """单遍贪心聚类。

        复杂度 O(n²)，但 pool 只含「未被升级的非语义记忆」，通常几十到几百条。
        用并查集会更快，但贪心对巩固场景足够，且顺序稳定、结果可复现
        （测试里需要确定性）。
        """
        clusters: List[List[Dict[str, Any]]] = []
        reps: List[str] = []
        for r in pool:
            text = str(r.get("content") or "")
            if not text:
                continue
            placed = False
            for i, rep in enumerate(reps):
                if similarity(text, rep) >= threshold:
                    # 原子守恒：两边原子都非空且不同 → 是两个事实，不并簇
                    if _atoms_conflict(text, rep):
                        continue
                    clusters[i].append(r)
                    placed = True
                    break
            if not placed:
                clusters.append([r])
                reps.append(text)
        return clusters

    # ------------------------------------------------------------------
    # 路径 B：情景 → 程序性
    # ------------------------------------------------------------------

    def procedural(self, min_success: Optional[int] = None,
                   dry_run: bool = False) -> Dict[str, Any]:
        """同类任务成功 ≥N 次 → 归纳 workflow。

        归纳出的 playbook **必须带前置条件**：没有适用条件的经验会在条件
        不匹配时主动把 Agent 带沟里（Agent Workflow Memory 已观察到负迁移）。
        前置条件从各次任务 ``context.preconditions`` 取**交集**——只有每次
        都成立的条件才配叫「必须」，出现一次就失效的只能进 ``typical_env``。
        """
        cfg = TRIGGERS["procedural"]
        need = int(min_success if min_success is not None else cfg["min_success"])
        min_overlap = float(cfg["min_step_overlap"])

        records = self._records()
        tasks: Dict[str, List[Dict[str, Any]]] = {}
        for r in records:
            meta = r.get("meta") or {}
            if not isinstance(meta, dict):
                continue
            if meta.get("kind") != "task_fact":
                continue
            task = meta.get("task") or {}
            if not isinstance(task, dict):
                continue
            if str(task.get("outcome") or "") != "success":
                continue
            gt = str(task.get("goal_type") or "general")
            tasks.setdefault(gt, []).append(r)

        promoted: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []

        # 已有 playbook 的 goal_type，避免重复归纳
        existing_gt = set()
        for r in records:
            meta = r.get("meta") or {}
            if isinstance(meta, dict) and meta.get("kind") == "playbook":
                pb = meta.get("playbook") or {}
                if isinstance(pb, dict):
                    existing_gt.add(str(pb.get("goal_type") or ""))

        for gt, rs in tasks.items():
            if len(rs) < need:
                continue
            if gt in existing_gt:
                skipped.append({"goal_type": gt, "reason": "playbook_exists",
                                "tasks": len(rs)})
                continue

            task_dicts = [((r.get("meta") or {}).get("task") or {}) for r in rs]
            step_sets = [self._step_actions(t) for t in task_dicts]
            common = self._common_steps(step_sets)
            if step_sets and len(common) / float(max(1, max(len(s) for s in step_sets))) < min_overlap:
                skipped.append({"goal_type": gt, "reason": "steps_too_different",
                                "tasks": len(rs)})
                continue

            must, typical = self._preconditions(task_dicts)
            ids = [r.get("id") for r in rs if r.get("id")]
            fp = fingerprint(ids)
            if self._already(records, fp):
                skipped.append({"goal_type": gt, "reason": "already_consolidated"})
                continue

            pattern = "处理「%s」类任务时，按已验证的 %d 步流程执行" % (gt, len(common))
            if common:
                pattern += "：" + " → ".join(common[:6])

            content = "\n".join([
                "【经验 · %s】" % gt,
                "模式：%s" % pattern,
                "适用条件（必须）：%s" % ("；".join(must) if must else "无硬性前置"),
                "典型环境：%s" % ("；".join(typical) if typical else "通用"),
                "成功率：%d/%d" % (len(rs), len(rs)),
                "证据任务：%s" % "、".join(str(t.get("task_id") or "") for t in task_dicts[:5]),
            ])
            meta = {
                "kind": "playbook",
                "consolidation_fp": fp,
                "derived_from": ids,
                "playbook": {
                    "goal_type": gt,
                    "pattern": pattern,
                    "preconditions": {"must": must, "must_not": [],
                                      "typical_env": typical},
                    "steps": common,
                    "stats": {"applied": len(rs), "succeeded": len(rs), "failed": 0,
                              "success_rate": 1.0,
                              "last_used": _now_iso(), "confidence": 0.7},
                    "failure_modes": [],
                    "scope": {"project": "*", "domain": gt},
                },
                "consolidated_at": _now_iso(),
            }
            mid = self._write(content, "procedural",
                              tags=[TAG_CONSOLIDATED, "playbook",
                                    "goal_type:%s" % gt],
                              meta=meta, importance=5, dry_run=dry_run)
            promoted.append({"memory_id": mid, "goal_type": gt,
                             "steps": len(common), "tasks": len(rs),
                             "must": must, "typical_env": typical})

        return {"path": "procedural", "trigger": {"min_success": need},
                "goal_types": len(tasks), "promoted": promoted,
                "skipped": skipped, "dry_run": dry_run}

    @staticmethod
    def _step_actions(task: Dict[str, Any]) -> List[str]:
        out: List[str] = []
        for st in (task.get("steps") or []):
            if isinstance(st, dict):
                a = str(st.get("action") or "").strip()
            else:
                a = str(st).strip()
            if a:
                out.append(a)
        return out

    @staticmethod
    def _common_steps(step_sets: Sequence[List[str]]) -> List[str]:
        """共同步骤：按首次出现顺序取并集，但只保留出现在 ≥半数任务里的。

        不做严格的最长公共子序列 —— 真实任务的步骤顺序会有合理差异，
        严格 LCS 会把有价值的步骤全丢掉。
        """
        if not step_sets:
            return []
        n = len(step_sets)
        order: List[str] = []
        counts: Dict[str, int] = {}
        for steps in step_sets:
            seen = set()
            for s in steps:
                if s in seen:
                    continue
                seen.add(s)
                if s not in counts:
                    order.append(s)
                counts[s] = counts.get(s, 0) + 1
        need = max(1, int(math.ceil(n / 2.0)))
        return [s for s in order if counts.get(s, 0) >= need]

    @staticmethod
    def _preconditions(tasks: Sequence[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
        """从多次任务的 context 推导前置条件。

        - ``must``：每次都出现的前置条件（交集）
        - ``typical_env``：出现过但非每次都有的约束（并集去重）
        """
        pre_sets: List[Set[str]] = []
        cons_all: List[str] = []
        for t in tasks:
            ctx = t.get("context") or {}
            if not isinstance(ctx, dict):
                ctx = {}
            pre = ctx.get("preconditions") or []
            if isinstance(pre, str):
                pre = [pre]
            pre_sets.append({str(x).strip() for x in pre if str(x).strip()})
            cons = ctx.get("constraints") or []
            if isinstance(cons, str):
                cons = [cons]
            cons_all.extend(str(x).strip() for x in cons if str(x).strip())
        must = set.intersection(*pre_sets) if pre_sets else set()
        typical: List[str] = []
        for c in cons_all:
            if c not in typical:
                typical.append(c)
        return sorted(must), typical

    # ------------------------------------------------------------------
    # 路径 C：失败 → 负记忆
    # ------------------------------------------------------------------

    def anti_pattern(self, dry_run: bool = False) -> Dict[str, Any]:
        """失败 / 回滚 / 被纠正 → 提炼禁忌条目，永久保留。

        这是principle里「价值密度最高、最易被忽略」的一类。三条来源：

        1. 任务事实 ``outcome in (failed, rolled_back)``；
        2. 代码变更 ``outcome=reverted``；
        3. 任务步骤里 ``result=failed`` 且有 ``reason`` 的（局部失败但整体成功
           的任务里，这些步骤恰恰是最具体的「不要再这样」）。

        严重度：reverted > rolled_back > failed。
        """
        records = self._records()
        produced: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []

        existing = set()
        for r in records:
            meta = r.get("meta") or {}
            if isinstance(meta, dict) and meta.get("kind") == "anti_pattern":
                existing.add(str(meta.get("what_failed") or "").strip())

        severity_rank = {"failed": 1, "rolled_back": 2, "reverted": 3}

        for r in records:
            meta = r.get("meta") or {}
            if not isinstance(meta, dict):
                continue
            kind = meta.get("kind")

            what = ""
            why = ""
            severity = ""
            context: List[str] = []

            if kind == "task_fact":
                task = meta.get("task") or {}
                outcome = str(task.get("outcome") or "")
                if outcome not in ("failed", "rolled_back"):
                    # 整体成功但中间有失败步骤 → 仍然值得记（局部坑）
                    fails = [s for s in (task.get("steps") or [])
                             if isinstance(s, dict)
                             and str(s.get("result") or "") == "failed"]
                    if not fails:
                        continue
                    what = "%s：%s" % (task.get("goal_type") or "任务",
                                       "；".join(str(s.get("action") or "") for s in fails)[:160])
                    why = "；".join(str(s.get("reason") or "") for s in fails
                                   if s.get("reason"))[:300]
                    severity = "failed"
                else:
                    what = str(task.get("goal") or "")[:160]
                    why = "结果 %s" % outcome
                    severity = outcome
                ctx = task.get("context") or {}
                if isinstance(ctx, dict):
                    cons = ctx.get("constraints") or []
                    if isinstance(cons, str):
                        cons = [cons]
                    context = [str(c) for c in cons if str(c).strip()]

            elif kind == "code_change":
                chg = meta.get("change") or {}
                outcome = str(chg.get("outcome") or "")
                if outcome != "reverted":
                    continue
                what = "%s：%s" % (chg.get("repo") or "代码",
                                   str(chg.get("intent") or "")[:140])
                why = str(chg.get("lesson") or "") or "该变更被回滚"
                severity = "reverted"
                context = [str(c) for c in (chg.get("constraints") or [])]

            else:
                continue

            what = what.strip()
            if not what:
                continue
            if what in existing:
                skipped.append({"what_failed": what, "reason": "exists"})
                continue

            content = "\n".join([
                "【负记忆 · 不要重复】%s" % what,
                "为什么不行：%s" % (why or "未记录"),
                "当时的约束：%s" % ("；".join(context) if context else "无"),
                "严重度：%s" % severity,
                "来源：%s" % str(r.get("id") or ""),
            ])
            fp = fingerprint([str(r.get("id") or "")])
            mid = self._write(
                content, "lesson",
                tags=[TAG_ANTI, "negative", "severity:%s" % severity],
                meta={
                    "kind": "anti_pattern",
                    "consolidation_fp": fp,
                    "what_failed": what,
                    "why": why,
                    "severity": severity,
                    "severity_rank": severity_rank.get(severity, 1),
                    "context": context,
                    "recurrence": 1,
                    "derived_from": [r.get("id")],
                    "consolidated_at": _now_iso(),
                },
                importance=5, project=r.get("project"), dry_run=dry_run)
            existing.add(what)
            produced.append({"memory_id": mid, "what_failed": what,
                             "severity": severity})

        return {"path": "anti_pattern", "produced": produced,
                "skipped": skipped, "dry_run": dry_run}

    # ------------------------------------------------------------------
    # 统一入口
    # ------------------------------------------------------------------

    def run_all(self, dry_run: bool = False) -> Dict[str, Any]:
        """跑全部三条路径，返回汇总报告。"""
        return {
            "semantic": self.semantic(dry_run=dry_run),
            "procedural": self.procedural(dry_run=dry_run),
            "anti_pattern": self.anti_pattern(dry_run=dry_run),
            "dry_run": dry_run,
            "at": _now_iso(),
        }
