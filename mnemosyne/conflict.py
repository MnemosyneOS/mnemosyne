# -*- coding: utf-8 -*-
"""冲突分类与消解（v8.1.0）
============================

修复 8.0.0 的核心缺陷：**冲突只检测、不消解，且检测本身误报严重**。

8.0.0 的判据（``brain.py:_detect_conflicts_at_write``）是「共享实体 + 都是 fact +
前 30 字不同 + 余弦 > 0.3」，``reflect`` 用的是「共享实体 + ≥2 条 fact」。
两者都把「同一实体出现在不同记忆里」当成冲突，实测 14 组告警里绝大多数是假阳性
（例如 ``an agent`` 同时出现在「规则槽是 SOUL.md」和「记忆走云端」两条互不矛盾的
记忆里）。

本模块用**断言对齐**替代实体共现：只有同时满足

1. 共享实体（讨论的是同一个对象）
2. 共享谓词（讨论的是同一方面）
3. 宾语不同或极性相反（说法不一样）

才判为冲突。并进一步细分为六类，每类给出确定的消解动作。

四操作（与主流记忆系统对齐）：

===========  ==========================================
``ADD``      无冲突，正常新增
``NOOP``     重复或细化，只增加印证，不新增条目
``UPDATE``   时间性更新，失效旧条目（不删除）
``DELETE``   新证据明确推翻旧条目且旧条目无历史价值
===========  ==========================================
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "ConflictKind",
    "ACTIONS",
    "assertion",
    "classify_pair",
    "resolve_candidates",
]

# ---------------------------------------------------------------------------
# 冲突类型
# ---------------------------------------------------------------------------

class ConflictKind:
    """六类冲突（与业界通行的记忆系统设计原则5.2 节对齐）。"""

    NONE = "none"                              # 不是冲突
    DUPLICATE = "duplicate"                    # 重复
    REFINEMENT = "refinement"                  # 细化/补全
    SCOPE_DIFF = "scope_diff"                  # 伪冲突：作用域不同
    TEMPORAL_UPDATE = "temporal_update"        # 时间性更新（换公司、搬家）
    PREFERENCE_CHANGE = "preference_change"    # 偏好变更
    PRECISION_DIFF = "precision_diff"          # 精度差异
    LOGICAL_CONTRADICTION = "logical_contradiction"  # 逻辑矛盾
    SOURCE_CONFLICT = "source_conflict"        # 来源冲突


#: 冲突类型 → 动作。保守优先：能并存就并存，能失效就不删除。
ACTIONS = {
    ConflictKind.NONE: "ADD",
    ConflictKind.DUPLICATE: "NOOP",
    ConflictKind.REFINEMENT: "NOOP",
    ConflictKind.SCOPE_DIFF: "ADD",
    ConflictKind.TEMPORAL_UPDATE: "UPDATE",
    ConflictKind.PREFERENCE_CHANGE: "UPDATE",
    ConflictKind.PRECISION_DIFF: "NOOP",
    ConflictKind.LOGICAL_CONTRADICTION: "KEEP_BOTH",   # 不自动选边，降权 + 待确认
    ConflictKind.SOURCE_CONFLICT: "KEEP_BOTH",
}

# ---------------------------------------------------------------------------
# 断言抽取
# ---------------------------------------------------------------------------

#: 谓词词表。命中即认为两条记忆在讨论「同一方面」。
#: 只收关系性动词，不收泛动词（如「做」「有」），否则谓词共享会退化成实体共现。
_PREDICATES = (
    "是", "为", "位于", "属于", "来自", "使用", "采用", "走", "依赖", "需要",
    "支持", "不支持", "喜欢", "偏好", "讨厌", "拒绝", "禁止", "允许",
    "等于", "大于", "小于", "包含", "负责", "拥有", "版本", "地址", "路径",
    "密码", "端口", "模型", "语言", "框架", "数据库", "负责人", "截止",
)

_NEGATIONS = ("不", "没", "无", "非", "未", "别", "勿", "禁止", "取消", "停止", "不再")

_NUMBER_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|GB|MB|TB|ms|s|分钟|小时|天|周|月|年|元|块|万|亿|个|条|次|人)?"
)
_DATE_RE = re.compile(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}|\d{1,2}月\d{1,2}日")


def _polarity(text: str) -> int:
    """否定极性：命中否定词记 -1，否则 +1。

    只取「首个谓词前后 6 字」窗口内的否定词，避免「不支持 A 但支持 B」被整体判负。
    """
    if not text:
        return 1
    for pred in _PREDICATES:
        idx = text.find(pred)
        if idx < 0:
            continue
        window = text[max(0, idx - 6): idx + len(pred) + 2]
        if any(neg in window for neg in _NEGATIONS):
            return -1
    return -1 if any(neg in text[:12] for neg in _NEGATIONS) else 1


def _numbers(text: str) -> List[str]:
    vals = [m.group(0).strip() for m in _NUMBER_RE.finditer(text or "")]
    vals += [m.group(0) for m in _DATE_RE.finditer(text or "")]
    return [v for v in vals if v]


def _attr_value(text: str, entities: Optional[set] = None) -> Tuple[set, set]:
    """从一句话里析出「属性 → 取值」。

    做法：取第一个谓词的位置，**从它前面回溯一小段中文作为属性**，后面的部分作为
    取值。完全不依赖 ``entities`` —— 因为中文实体抽取本身是噪声源
    （`用户在北京的公司是甲公司` 会被抽成 `用户在北京的` + `公司是甲公司`），
    任何「从 head 里减掉实体」的推导都会被这些碎片带偏。回溯法则只看相对位置::

        用户在北京的公司是甲公司   → 谓词前回溯 → 属性 北京的公司 / 取值 甲公司
        an agent 的规则注入槽是 X  → 属性 规则注入槽     / 取值 SOUL.md
        an agent 的记忆通道是 Y    → 属性 记忆通道       / 取值 云端

    于是「换公司」两句属性一致、取值不同 → 时间性更新；而 an agent 那两句**属性
    本就不同**，不会被误判成同一事实的变更。
    """
    text = (text or "").strip()
    idx, pred = -1, ""
    for p in _PREDICATES:
        i = text.find(p)
        if i > 0 and (idx < 0 or i < idx):
            idx, pred = i, p
    if idx <= 0:
        return set(), set()
    head, tail = text[:idx], text[idx + len(pred):]

    # 属性：谓词前紧邻的一段。优先中文（遇到非中文即停，最多 6 字），
    # 否则退回谓词前最后一个 ASCII 词（"Mnemosyne 是 8.1.0" 这种）。
    attr = ""
    m = re.search(r"[\u4e00-\u9fff]{1,6}$", head.rstrip(" \t的地得"))
    if m:
        attr = m.group(0)
    else:
        m2 = re.search(r"[A-Za-z0-9_.\-]{1,24}$", head)
        if m2:
            attr = m2.group(0)

    # 取值：谓词后第一句，去空白与成对符号
    val = re.split(r"[，。、；：？！\n]", tail)[0]
    val = re.sub(r"[\s「」『』（）()\[\]{}\"'`~@#$%^&*_+=|\\/<>]+", "", val).strip()
    return ({attr} if attr else set()), ({val} if val else set())


def assertion(record: Dict[str, Any]) -> Dict[str, Any]:
    """把一条记忆压成可比较的断言结构。

    返回 ``{entities, predicates, polarity, numbers, text, scope}``。

    ``entities`` 存的是**谓词切分后的实体主干**（见 :func:`_ent_core`）而非原始
    实体串。原因：中文抽取兜底正则会把「该方案是不安全的」切出「该方案是不安」和
    「全的」，与「该方案是安全的」抽出的「该方案是安全」字符串不等；若按原始串比，
    这两条本应冲突的记忆会因为「无共享实体」被直接判为 NONE。存主干后三者都归一
    为 `该方案`，后续的谓词/极性/数值比对才排得上用场。
    """
    text = (record.get("content") or "").strip()
    raw = record.get("entities") or []
    entities = {c for c in (_ent_key(e) for e in raw) if c}
    predicates = {p for p in _PREDICATES if p in text}
    attrs, values = _attr_value(text, entities)
    return {
        "entities": entities,
        "predicates": predicates,
        "polarity": _polarity(text),
        "numbers": set(_numbers(text)),
        "attrs": attrs,
        "values": values,
        "text": text,
        "scope": (
            record.get("project") or "",
            record.get("namespace") or "",
            (record.get("meta") or {}).get("project") or "",
        ),
    }


def _similarity(a: str, b: str) -> float:
    """轻量 Jaccard 词集相似度（零依赖，不引入向量计算）。"""
    ta = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", (a or "").lower()))
    tb = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", (b or "").lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / float(len(ta | tb))


def _contains(a: str, b: str) -> bool:
    """短句是否是长句的子串（判断「细化」而非「矛盾」）。"""
    sa, sb = (a or "").strip(), (b or "").strip()
    if not sa or not sb:
        return False
    short, long_ = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    return len(short) >= 8 and short in long_


# ---------------------------------------------------------------------------
# 成对分类
# ---------------------------------------------------------------------------

def classify_pair(new: Dict[str, Any],
                  old: Dict[str, Any]) -> Tuple[str, str, str]:
    """判定新记忆与一条旧记忆的关系。

    返回 ``(kind, action, reason)``。判定顺序即优先级，从「最不可能是冲突」开始。
    """
    if new.get("id") and new.get("id") == old.get("id"):
        return ConflictKind.NONE, "ADD", "同一条记录"

    a, b = assertion(new), assertion(old)

    # 1. 作用域不同 → 伪冲突（最常见的误判来源）
    if a["scope"] != b["scope"] and (a["scope"][0] or b["scope"][0]):
        return (ConflictKind.SCOPE_DIFF, "ADD",
                "作用域不同（project/namespace 不一致），各自独立成立")

    sim = _similarity(a["text"], b["text"])

    # 2. 完全重复。
    #    必须同时要求「实体集合一致」：像「用户在 A 公司」vs「用户在 B 公司」
    #    这种只差一个宾语的句子，词集相似度会高达 0.9+，但它们显然是**更新**
    #    而不是重复。实体不一致时继续往下判定，交给时间性更新分支处理。
    if sim >= 0.9 and a["entities"] == b["entities"]:
        return ConflictKind.DUPLICATE, "NOOP", "内容高度重复（相似度 %.2f）" % sim

    # 3. 无共享实体 → 不相关
    shared_ent = a["entities"] & b["entities"]
    if not shared_ent:
        return ConflictKind.NONE, "ADD", "无共享实体，互不相关"

    # 4. 共享实体但无共享谓词 → 讨论的是同一对象的不同方面，不是冲突
    #    （这是 8.0.0 误报的主因）
    shared_pred = a["predicates"] & b["predicates"]
    if not shared_pred:
        return (ConflictKind.NONE, "ADD",
                "共享实体 %s 但谓词不同，属不同方面，不构成冲突"
                % "、".join(list(shared_ent)[:2]))

    # 5. 细化：一方是另一方的子串
    if _contains(a["text"], b["text"]):
        return ConflictKind.REFINEMENT, "NOOP", "一方为另一方的细化补充，保留信息更全者"

    # 6. 极性相反 + 共享谓词 → 逻辑矛盾（不自动选边）
    if a["polarity"] != b["polarity"]:
        return (ConflictKind.LOGICAL_CONTRADICTION, "KEEP_BOTH",
                "谓词相同但否定极性相反（%s），需人工或对话确认" % "、".join(list(shared_pred)[:2]))

    # 7. 属性相同、取值不同 → 时间性更新
    #    这是principle「六类冲突」里的 TEMPORAL_UPDATE（换公司、搬家、改版本号）。
    #    必须同时满足「属性一致」，否则 `A 的 X 是 1` 与 `A 的 Y 是 2` 会被误判——
    #    那是同一对象的不同侧面，本来就是两条独立事实（见第 4 条同理）。
    same_attr = a["attrs"] & b["attrs"]
    if same_attr and a["values"] and b["values"] and a["values"] != b["values"]:
        return (ConflictKind.TEMPORAL_UPDATE, "UPDATE",
                "同一属性「%s」的取值发生变化（%s → %s）"
                % ("、".join(list(same_attr)[:1]),
                   "、".join(list(a["values"])[:1]),
                   "、".join(list(b["values"])[:1])))

    # 8. 数值不同但谓词相同 → 精度差异或时间性更新
    if a["numbers"] and b["numbers"] and a["numbers"] != b["numbers"]:
        # 有明确事件时间且新旧不同 → 时间性更新；否则视为精度差异
        ta, tb = new.get("event_time"), old.get("event_time")
        if ta and tb and ta != tb:
            return (ConflictKind.TEMPORAL_UPDATE, "UPDATE",
                    "同一事实的数值/日期随事件时间变化（%s → %s）"
                    % (tb, ta))
        return (ConflictKind.PRECISION_DIFF, "NOOP", "数值精度不同，保留精确者")

    # 9. 偏好类且宾语不同 → 偏好变更（需新证据足够强）
    if new.get("type") == "preference" and old.get("type") == "preference":
        if sim < 0.85:
            return (ConflictKind.PREFERENCE_CHANGE, "UPDATE",
                    "偏好对象发生变化，新记忆置信度达标时失效旧条目")

    # 10. 来源冲突：同事实不同来源
    sa, sb = new.get("source_type"), old.get("source_type")
    if sa and sb and sa != sb and sim >= 0.6:
        return (ConflictKind.SOURCE_CONFLICT, "KEEP_BOTH",
                "同一事实来自不同来源（%s vs %s），按可信度排序而非删除" % (sa, sb))

    # 11. 时间性更新的兜底：共享实体+谓词、相似度中等、事件时间不同
    ta, tb = new.get("event_time"), old.get("event_time")
    if ta and tb and ta != tb and sim >= 0.35:
        return (ConflictKind.TEMPORAL_UPDATE, "UPDATE",
                "同一对象同一方面在不同时间有不同说法（%s → %s）" % (tb, ta))

    return ConflictKind.NONE, "ADD", "未发现实质冲突"


# ---------------------------------------------------------------------------
# 批量消解
# ---------------------------------------------------------------------------

def resolve_candidates(new: Dict[str, Any],
                       candidates: Sequence[Dict[str, Any]],
                       dry_run: bool = True) -> Dict[str, Any]:
    """对一批候选旧记忆做冲突分类，给出**计划**（不直接改库）。

    调用方（``Brain.conflict_resolve``）负责按计划落库。``dry_run`` 只影响调用方
    是否执行，本函数始终只返回计划，便于先给用户看再动手。
    """
    verdicts: List[Dict[str, Any]] = []
    for old in candidates or []:
        kind, action, reason = classify_pair(new, old)
        if kind == ConflictKind.NONE:
            continue
        verdicts.append({
            "kind": kind,
            "action": action,
            "reason": reason,
            "old_id": old.get("id"),
            "old_content": (old.get("content") or "")[:120],
            "old_confidence": old.get("confidence"),
            "old_event_time": old.get("event_time"),
        })

    # 计划汇总：以最严重的动作为准
    actions = {v["action"] for v in verdicts}
    if "KEEP_BOTH" in actions:
        plan = "KEEP_BOTH"
    elif "UPDATE" in actions:
        plan = "UPDATE"
    elif actions == {"NOOP"}:
        plan = "NOOP"
    else:
        plan = "ADD"

    return {
        "dry_run": dry_run,
        "plan": plan,
        "verdicts": verdicts,
        "conflict_count": len(verdicts),
        "note": "KEEP_BOTH 表示不自动选边，建议下次对话主动向用户确认",
    }


# ---------------------------------------------------------------------------
# 门面类
# ---------------------------------------------------------------------------

def _ent_core(ent: str) -> str:
    """取实体名中谓词之前的主干。

    为什么需要：`_extract_entity_names` 的中文兜底正则是 `[\\u4e00-\\u9fff]{2,6}`，
    会把 7 字以上的中文串**硬切成 6+2 两段**——「该方案是不安全的」被抽成
    「该方案是不安」和「全的」，而「该方案是安全的」被抽成「该方案是安全」。
    三条碎片字符串两两不等，分桶时永远凑不到一对。

    与其改动影响面很大的抽取器（`entities` 字段同时喂图通道、temporal_query、
    recall 的图信号），这里只在冲突分桶这一层做归一：**把谓词及其后内容切掉**，
    只留主干。`该方案是安全` 与 `该方案是不安` 的主干都是 `该方案`，即可正确配对。
    """
    s = ent or ""
    cut = len(s)
    for p in _PREDICATES:
        i = s.find(p)
        if 0 < i < cut:
            cut = i
    return s[:cut] if 0 < cut < len(s) else s


def _ent_key(ent: str) -> str:
    """分桶用的实体归一化键：去标点 → 切主干 → 小写。"""
    s = re.sub(r"[\s，。、；：？！「」『』（）()\[\]{},.;:!?\"'`~@#$%^&*_+=|\\/<>-]+",
               "", ent or "").lower()
    core = _ent_core(s)
    return core or s


def _ent_bigrams(ent: str) -> set:
    """实体名的二元字组集合，用于容忍中文抽取的断词差异。

    即使切了主干，长定语仍可能让两个主干不完全相等（`用户的显卡` vs
    `用户的显卡型号`）。按二元字组重合度兜底，重合度 ≥ threshold 判为同一实体。
    """
    s = re.sub(r"[\s，。、；：？！「」『』（）()\[\]{},.;:!?\"'`~@#$%^&*_+=|\\/<>-]+", "", ent or "").lower()
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _same_entity(a: str, b: str, threshold: float = 0.6) -> bool:
    """两个实体名是否指向同一个实体（主干优先，二元字组兜底）。"""
    if not a or not b:
        return False
    ka, kb = _ent_key(a), _ent_key(b)
    if ka and ka == kb:
        return True
    ba, bb = _ent_bigrams(ka or a), _ent_bigrams(kb or b)
    if not ba or not bb:
        return False
    return len(ba & bb) / float(len(ba | bb)) >= threshold


class ConflictResolver:
    """冲突消解门面：检测 → 分类 → 按计划落库。

    与 8.0.0 的关键差别：**8.0.0 只把冲突写进 ``meta.write_conflicts`` 就结束**，
    没有任何后续动作；本类会把 ``UPDATE`` 计划真正执行（旧条目置
    ``verification=superseded`` + ``superseded_by``，**不删除**）。
    """

    def __init__(self, brain: Any):
        self.brain = brain

    def _candidates(self, new_record: Dict[str, Any],
                    limit: int = 8) -> List[Dict[str, Any]]:
        """取候选：优先按共享实体，退化为语义召回。"""
        entities = list(new_record.get("entities") or [])[:3]
        query = " ".join(entities) if entities else (new_record.get("content") or "")[:80]
        try:
            hits = self.brain.recall(query, k=limit)
        except Exception:
            return []
        out = []
        for item in hits or []:
            rec = item[1] if isinstance(item, (list, tuple)) and len(item) >= 2 else item
            if isinstance(rec, dict) and rec.get("id") != new_record.get("id"):
                out.append(rec)
        return out

    def resolve(self, content: str, mtype: str = "semantic",
                project: Optional[str] = None, dry_run: bool = True,
                apply: bool = False) -> Dict[str, Any]:
        """对一条「待写入内容」做冲突判定，可选地执行消解。

        ``dry_run=True``（默认）只返回计划，不动数据 —— 与 ``forget`` 的
        ``dry_run`` 语义保持一致，先给人看再动手。
        """
        from .models import _build_record

        probe = _build_record(content, mtype=mtype, skip_detailed=True)
        if project:
            probe["project"] = project
        cands = self._candidates(probe)
        plan = resolve_candidates(probe, cands, dry_run=dry_run)

        applied = []
        if apply and not dry_run and plan["plan"] == "UPDATE":
            store = getattr(self.brain, "store", None)
            for v in plan["verdicts"]:
                if v["action"] != "UPDATE" or not v.get("old_id"):
                    continue
                if store is None:
                    break
                ok = store.update_by_id(v["old_id"], {
                    "verification": "superseded",
                    "superseded_by": "pending_write",
                    "status": "active",
                })
                if ok:
                    applied.append(v["old_id"])

        plan["applied"] = applied
        plan["candidate_count"] = len(cands)
        return plan

    def audit(self, limit: int = 500) -> Dict[str, Any]:
        """全库冲突审计：**只报真冲突**，替代 8.0.0 ``reflect`` 的实体共现误报。

        做法：按实体分桶 → 桶内两两 ``classify_pair`` → 只保留非 NONE 的结果。
        桶内规模超过 12 条时按置信度取前 12，避免 O(n²) 爆炸。
        """
        store = getattr(self.brain, "store", None)
        if store is None:
            return {"error": "no store"}
        records = [r for r in store.all_records()
                   if not r.get("_corrupt") and r.get("status") != "deleted"][:limit]

        buckets: Dict[str, List[Dict[str, Any]]] = {}
        # 实体归一化：把断词产生的多个变体合并到同一个规范名下，再分桶。
        groups: List[Dict[str, Any]] = []   # [{"name": str, "bigrams": set}]
        for r in records:
            for e in (r.get("entities") or [])[:6]:
                g = None
                for cand in groups:
                    if _same_entity(cand["name"], e):
                        g = cand
                        break
                if g is None:
                    g = {"name": e, "bigrams": _ent_bigrams(e)}
                    groups.append(g)
                buckets.setdefault(g["name"], []).append(r)

        findings = []
        for ent, rs in buckets.items():
            if len(rs) < 2:
                continue
            rs = sorted(rs, key=lambda x: -(x.get("confidence") or 0))[:12]
            for i in range(len(rs)):
                for j in range(i + 1, len(rs)):
                    kind, action, reason = classify_pair(rs[i], rs[j])
                    if kind == ConflictKind.NONE:
                        continue
                    findings.append({
                        "entity": ent, "kind": kind, "action": action,
                        "reason": reason,
                        "a_id": rs[i].get("id"), "b_id": rs[j].get("id"),
                        "a": (rs[i].get("content") or "")[:90],
                        "b": (rs[j].get("content") or "")[:90],
                    })
        # 去重（同一对可能在多个实体桶里重复命中）
        seen, uniq = set(), []
        for f in findings:
            key = tuple(sorted([f["a_id"] or "", f["b_id"] or ""])) + (f["kind"],)
            if key not in seen:
                seen.add(key)
                uniq.append(f)
        return {
            "scanned": len(records),
            "findings": uniq[:50],
            "count": len(uniq),
            "note": "仅报告共享实体且共享谓词的实质冲突，实体共现不再计入",
        }
