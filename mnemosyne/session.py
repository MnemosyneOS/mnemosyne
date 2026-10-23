# -*- coding: utf-8 -*-
"""会话摘要（v8.1.0）
=====================

填补 8.0.0 最大的空白：**只有原子事实，没有会话摘要**。
8.0.0 能回答「用户偏好 pnpm」，但回答不了「上次我们聊到哪、定了什么、还剩什么没定」。

本模块实现双通道写入：

- **摘要通道**（本模块）：连贯叙述，保留因果与语境，供人读、供模型做背景
- **事实通道**（8.0.0 已有的 ``retain``）：原子断言，供精确召回

七要素（缺一不可，对应后续会被召回的场景）::

    goal         目标与意图（不是「问了什么」，是「想达成什么」）
    constraints  已确认的事实与约束
    decisions    做出的决策及理由（理由比结论更值钱）
    pending      未决问题与待办（独立成条，下次主动问）
    artifacts    产物与位置
    failures     失败与弯路（负记忆的原料）
    preferences  偏好信号

抗漂移三约束：

1. **锚点不压缩** —— 数字、日期、路径、URL、引号原话以原文钉住
2. **只追加少改写** —— 新信息进 ``updates``，只有直接冲突才改写原字段
3. **定期重建** —— ``rebuild_every`` 轮后丢弃旧摘要，从原文重建

设计取舍：**抽取式而非生成式**。项目坚持零第三方依赖，无法假定有 LLM 可用；
规则抽取在「找得到、不编造」上比小模型更可靠。若配置了 LLM provider，
``build_summary(llm=...)`` 会用它做二次润色，但七要素的**证据句**始终来自原文。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

__all__ = [
    "SEVEN_FACTORS",
    "extract",
    "merge",
    "render_markdown",
    "anchors",
    "summary_record",
]

SEVEN_FACTORS = (
    "goal", "constraints", "decisions",
    "pending", "artifacts", "failures", "preferences",
)

# ---------------------------------------------------------------------------
# 规则词表
# ---------------------------------------------------------------------------

_CUES = {
    "constraints": ("必须", "不能", "只能", "不要", "禁止", "务必", "一定要",
                    "仅限", "只准", "不得", "限定", "约束"),
    "decisions": ("决定", "选择", "采用", "改用", "换成", "定为", "敲定",
                  "最终", "确定用", "方案是", "就按"),
    # 「没定 / 未定 / 待定」是比「后续」更硬的未决信号，必须单列：
    # 「还有一件事没定：半衰期取多少」这种句子不含「还没」，只靠「待/等」
    # 会漏判，导致未决事项进不了画像卡的 pending 段。
    "pending": ("待", "等", "下次", "明天", "后续", "todo", "未决", "还没",
                "回头", "稍后", "尚未", "遗留", "下一步",
                "没定", "未定", "待定", "待确认", "需确认", "没确认"),
    "failures": ("失败", "不行", "放弃", "回滚", "报错", "踩坑", "无效",
                 "不work", "没成功", "试过", "走不通", "退回去"),
    "preferences": ("偏好", "喜欢", "讨厌", "反感", "不要", "太啰嗦", "简洁",
                    "详细", "直接给", "别", "希望", "习惯"),
}

_PATH_RE = re.compile(
    r"(?:[A-Za-z]:\\[^\s，。；、）\)\]]+|/[\w\-./]{4,}|[\w\-]+\.(?:py|js|ts|tsx|json|md|html|css|toml|yaml|yml|sh|sql))"
)
_URL_RE = re.compile(r"https?://[^\s，。；、）\)\]]+")
_QUOTE_RE = re.compile(r"[「“\"']([^」”\"']{2,40})[」”\"']")
_NUMBER_RE = re.compile(
    r"\d+(?:\.\d+)*(?:\s*(?:%|GB|MB|TB|ms|s|分钟|小时|天|周|月|年|元|万|亿|个|条|次|人|token|k))?"
)

_SENT_SPLIT = re.compile(r"(?<=[。！？!?\n])")


def _sentences(text: str) -> List[str]:
    out = []
    for part in _SENT_SPLIT.split(text or ""):
        s = (part or "").strip(" \t\r\n-·•")
        if len(s) >= 6:
            out.append(s)
    return out


def _clip(s: str, n: int = 110) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _dedupe(items: Iterable[str], limit: int = 6) -> List[str]:
    seen, out = set(), []
    for it in items:
        key = re.sub(r"\s+", "", it)[:40]
        if key and key not in seen:
            seen.add(key)
            out.append(it)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# 锚点
# ---------------------------------------------------------------------------

def anchors(text: str) -> Dict[str, List[str]]:
    """抽取不可压缩的锚点：数字、路径、URL、引号原话。

    这些内容在多轮摘要中必须原文保留，否则会累积失真
    （「128 维」变成「一百多维」，「bge-m3」变成「某个嵌入模型」）。
    """
    t = text or ""
    return {
        "numbers": _dedupe([m.group(0).strip() for m in _NUMBER_RE.finditer(t)], 12),
        "paths": _dedupe(_PATH_RE.findall(t), 8),
        "urls": _dedupe(_URL_RE.findall(t), 6),
        "quotes": _dedupe([m.group(1).strip() for m in _QUOTE_RE.finditer(t)], 6),
    }


# ---------------------------------------------------------------------------
# 七要素抽取
# ---------------------------------------------------------------------------

def extract(messages: Any) -> Dict[str, Any]:
    """从一段会话中抽取七要素。

    ``messages`` 支持三种形态：

    - ``str``：整段文本
    - ``[{"role": "user"|"assistant", "content": "..."}]``
    - ``[{"content": "..."}]``

    返回 ``{goal, constraints, decisions, pending, artifacts, failures,
    preferences, anchors, turn_count}``。所有字段都是**原文句子**，不改写。
    """
    if isinstance(messages, str):
        turns = [{"role": "user", "content": messages}]
    else:
        turns = [m for m in (messages or []) if isinstance(m, dict)]

    user_texts = [m.get("content", "") for m in turns if m.get("role", "user") == "user"]
    all_text = "\n".join((m.get("content") or "") for m in turns)

    sents = _sentences(all_text)
    buckets: Dict[str, List[str]] = {k: [] for k in _CUES}

    for s in sents:
        low = s.lower()
        for key, cues in _CUES.items():
            if any(c in low for c in cues):
                buckets[key].append(_clip(s))
                break  # 一句只归一类，避免「不要用 X，改用 Y」同时进约束和决策

    # 产物：路径 / URL 所在句
    artifacts = []
    for s in sents:
        if _PATH_RE.search(s) or _URL_RE.search(s):
            artifacts.append(_clip(s, 120))
    for p in _PATH_RE.findall(all_text) + _URL_RE.findall(all_text):
        artifacts.append(p)

    goal = ""
    if user_texts:
        first = (user_texts[0] or "").strip()
        goal = _clip(first, 140)

    return {
        "goal": goal,
        "constraints": _dedupe(buckets["constraints"]),
        "decisions": _dedupe(buckets["decisions"]),
        "pending": _dedupe(buckets["pending"]),
        "artifacts": _dedupe(artifacts, 8),
        "failures": _dedupe(buckets["failures"]),
        "preferences": _dedupe(buckets["preferences"]),
        "anchors": anchors(all_text),
        "turn_count": len(turns),
    }


def merge(previous: Optional[Dict[str, Any]],
          incoming: Dict[str, Any],
          turn_count: int = 0,
          rebuild_every: int = 20) -> Dict[str, Any]:
    """增量合并摘要（抗漂移）。

    - 默认**只追加**，不覆盖已有条目
    - 锚点取并集（永不丢失）
    - 累计轮次超过 ``rebuild_every`` 时标记 ``needs_rebuild``，提示调用方从原文重建
    """
    prev = previous or {}
    out = {}
    for k in SEVEN_FACTORS:
        if k == "goal":
            out[k] = prev.get("goal") or incoming.get("goal", "")
            continue
        merged = list(prev.get(k) or [])
        for item in incoming.get(k) or []:
            if item not in merged:
                merged.append(item)
        out[k] = merged[:10]

    prev_anchors = prev.get("anchors") or {}
    new_anchors = incoming.get("anchors") or {}
    out["anchors"] = {
        key: _dedupe(list(prev_anchors.get(key) or []) + list(new_anchors.get(key) or []), 16)
        for key in ("numbers", "paths", "urls", "quotes")
    }

    total = int(prev.get("turn_count") or 0) + int(turn_count or incoming.get("turn_count") or 0)
    out["turn_count"] = total
    out["needs_rebuild"] = total >= rebuild_every and total % rebuild_every < max(1, turn_count or 1)
    out["revision"] = int(prev.get("revision") or 0) + 1
    return out


# ---------------------------------------------------------------------------
# 渲染与落库
# ---------------------------------------------------------------------------

_LABELS = {
    "goal": "目标",
    "constraints": "约束",
    "decisions": "决策与理由",
    "pending": "未决事项",
    "artifacts": "产物与位置",
    "failures": "失败与弯路",
    "preferences": "偏好信号",
}


def render_markdown(summary: Dict[str, Any],
                    session_id: str = "",
                    title: str = "") -> str:
    """把摘要渲染成可读 Markdown（给人和模型看）。"""
    lines = []
    head = "会话摘要"
    if title:
        head += " · " + title
    if session_id:
        head += "（%s）" % session_id
    lines.append("## " + head)

    goal = summary.get("goal") or ""
    if goal:
        lines.append("**目标**：%s" % goal)

    for key in ("constraints", "decisions", "pending", "artifacts", "failures", "preferences"):
        items = summary.get(key) or []
        if not items:
            continue
        lines.append("**%s**：" % _LABELS[key])
        for it in items:
            lines.append("- " + it)

    a = summary.get("anchors") or {}
    pinned = []
    for key in ("numbers", "paths", "urls", "quotes"):
        for v in a.get(key) or []:
            if v not in pinned:
                pinned.append(v)
    if pinned:
        lines.append("**锚点（原文保留）**：" + " · ".join(pinned[:12]))

    lines.append("_轮次 %s · 修订 %s_" % (summary.get("turn_count", 0),
                                          summary.get("revision", 1)))
    return "\n".join(lines)


def summary_record(summary: Dict[str, Any],
                   session_id: str,
                   project: Optional[str] = None) -> Dict[str, Any]:
    """把摘要转成一条可 ``retain`` 的记录参数。

    摘要以 ``mtype="episodic"`` 存入（它描述「发生过什么」），
    并在 ``meta`` 里带 ``kind="session_summary"`` 以便与普通情景记忆区分。
    """
    md = render_markdown(summary, session_id=session_id)
    return {
        "content": md,
        "mtype": "episodic",
        "tags": ["session_summary", "session:%s" % session_id],
        "project": project,
        "importance": 4,
        "meta": {
            "kind": "session_summary",
            "session_id": session_id,
            "factors": {k: summary.get(k) for k in SEVEN_FACTORS},
            "anchors": summary.get("anchors") or {},
            "turn_count": summary.get("turn_count", 0),
            "revision": summary.get("revision", 1),
        },
    }


def pending_records(summary: Dict[str, Any],
                    session_id: str,
                    project: Optional[str] = None) -> List[Dict[str, Any]]:
    """未决事项**独立成条**。

    这是「减少重新了解用户」最容易被感知的一个动作：下次同项目对话开始时，
    系统能主动问一句「上次这个还没定，要继续吗」。藏在摘要段落里就捞不出来。
    """
    out = []
    for item in summary.get("pending") or []:
        out.append({
            "content": "[未决 · %s] %s" % (session_id, item),
            "mtype": "todo",
            "tags": ["pending", "session:%s" % session_id],
            "project": project,
            "importance": 4,
            "meta": {"kind": "pending_item", "session_id": session_id},
        })
    return out


# ---------------------------------------------------------------------------
# 门面类
# ---------------------------------------------------------------------------

class SessionMemory:
    """会话记忆门面：把一段对话变成「摘要 + 未决事项」两条可召回的记忆。

    只依赖 ``brain.retain`` / ``brain.recall``，不改动 Brain 既有逻辑。
    """

    TAG = "session_summary"

    def __init__(self, brain: Any):
        self.brain = brain

    def _find_previous(self, session_id: str, project: Optional[str]) -> Optional[Dict[str, Any]]:
        """查找同一会话的已有摘要（用于增量合并）。"""
        try:
            hits = self.brain.recall("会话摘要 %s" % session_id, k=5,
                                     tags=[self.TAG], project=project)
        except TypeError:
            hits = self.brain.recall("会话摘要 %s" % session_id, k=5)
        except Exception:
            return None
        for item in hits or []:
            rec = item[1] if isinstance(item, (list, tuple)) and len(item) >= 2 else item
            if not isinstance(rec, dict):
                continue
            meta = rec.get("meta") or {}
            if meta.get("session_id") == session_id and meta.get("factors"):
                return {"factors": meta["factors"],
                        "revision": meta.get("revision", 1),
                        "turn_count": meta.get("turn_count", 0),
                        "memory_id": rec.get("id")}
        return None

    def summarize(self, session_id: str, messages: Any,
                  project: Optional[str] = None,
                  rebuild: bool = False,
                  write_pending: bool = True) -> Dict[str, Any]:
        """生成或增量更新会话摘要。

        ``rebuild=True`` 时丢弃旧摘要、从原文重建（抗漂移的第三条约束）。
        """
        incoming = extract(messages)
        prev = None if rebuild else self._find_previous(session_id, project)
        merged = merge(prev["factors"] if prev else None, incoming,
                       turn_count=incoming.get("turn_count", 0))

        rec = summary_record(merged, session_id=session_id, project=project)
        mem_id = self.brain.retain(rec["content"], mtype=rec["mtype"],
                                   tags=rec["tags"], importance=rec["importance"],
                                   project=project, meta=rec["meta"])

        pending_ids = []
        if write_pending:
            for prec in pending_records(merged, session_id, project):
                pending_ids.append(self.brain.retain(
                    prec["content"], mtype=prec["mtype"], tags=prec["tags"],
                    importance=prec["importance"], project=project, meta=prec["meta"]))

        return {
            "session_id": session_id,
            "summary_memory_id": mem_id,
            "pending_memory_ids": pending_ids,
            "revision": merged.get("revision", 1),
            "turn_count": merged.get("turn_count", 0),
            "needs_rebuild": bool(merged.get("needs_rebuild")),
            "summary": merged,
            "markdown": render_markdown(merged, session_id=session_id),
        }

    def recall(self, query: str = "", project: Optional[str] = None,
               k: int = 3, include_pending: bool = True) -> Dict[str, Any]:
        """召回摘要与未决事项。"""
        q = query or "会话摘要 未决事项"
        out: Dict[str, Any] = {"summaries": [], "pending": []}
        try:
            hits = self.brain.recall(q, k=k, tags=[self.TAG], project=project)
        except TypeError:
            hits = self.brain.recall(q, k=k)
        for item in hits or []:
            rec = item[1] if isinstance(item, (list, tuple)) and len(item) >= 2 else item
            if isinstance(rec, dict):
                out["summaries"].append({
                    "memory_id": rec.get("id"),
                    "session_id": (rec.get("meta") or {}).get("session_id"),
                    "content": rec.get("content", ""),
                })
        if include_pending:
            try:
                ph = self.brain.recall(q, k=k, tags=["pending"], project=project)
            except TypeError:
                ph = self.brain.recall(q, k=k)
            for item in ph or []:
                rec = item[1] if isinstance(item, (list, tuple)) and len(item) >= 2 else item
                if isinstance(rec, dict) and (rec.get("meta") or {}).get("kind") == "pending_item":
                    out["pending"].append({"memory_id": rec.get("id"),
                                           "content": rec.get("content", "")})
        return out


# ---------------------------------------------------------------------------
# L1 轮级摘要 / L3 主题摘要 / L0→L3 指针链
#
# principle 4.1 的三层关系是「压缩比递增、可回溯性递减」，并明确要求：
#   「设计时必须保留从 L3 回到 L0 的指针链，否则用户问『为什么当时这么定』
#     时无法溯源。」
#
# 8.1.0 初版只做了 L2（会话摘要），缺 L1 与 L3，也没有指针链 —— 结果是
# 摘要一旦生成就与原文脱钩，无法回答「这条结论是从哪句话来的」。
# 下面补齐三层与指针，且**不改动 L2 的既有格式**（旧摘要仍可读）。
# ---------------------------------------------------------------------------

#: 每几轮滚一条 L1。principle建议 3–5 轮。
TURN_WINDOW = 4

#: L1 的字数上限（principle：200 字内）。
TURN_SUMMARY_MAX = 200


def turn_summary(messages: Any, start_turn: int = 0,
                 end_turn: Optional[int] = None) -> Dict[str, Any]:
    """L1 轮级摘要：捕捉「问了什么、答了什么、定了什么」。

    与 L2 的区别不是长度，而是**触发时机**：L2 在会话结束生成一次，L1 每
    3–5 轮滚一条。它的价值在于会话中途就能被召回 —— 长会话进行到第 20 轮时，
    最早那几轮的内容已经滑出上下文窗口，但 L1 还在。
    """
    if isinstance(messages, str):
        turns = [{"role": "user", "content": messages}]
    else:
        turns = [m for m in (messages or []) if isinstance(m, dict)]
    end_turn = len(turns) if end_turn is None else min(end_turn, len(turns))
    window = turns[start_turn:end_turn]
    if not window:
        return {"text": "", "turn_start": start_turn, "turn_end": start_turn,
                "message_ids": [], "factors": {}}

    # 抽取式：只用原文句子，不改写（抗漂移的第一条约束）
    s = extract(window)
    bits: List[str] = []
    if s["goal"]:
        bits.append("问：" + _clip(s["goal"], 60))
    if s["decisions"]:
        bits.append("定：" + "；".join(_clip(x, 40) for x in s["decisions"][:2]))
    if s["pending"]:
        bits.append("待：" + "；".join(_clip(x, 40) for x in s["pending"][:2]))
    if s["constraints"]:
        bits.append("限：" + "；".join(_clip(x, 40) for x in s["constraints"][:2]))
    if s["failures"]:
        bits.append("败：" + "；".join(_clip(x, 40) for x in s["failures"][:1]))
    text = "｜".join(bits)
    if len(text) > TURN_SUMMARY_MAX:
        text = text[:TURN_SUMMARY_MAX - 1] + "…"

    return {
        "text": text,
        "turn_start": start_turn,
        "turn_end": end_turn,
        "message_ids": ["%s#%d" % (start_turn + i, start_turn + i)
                        for i in range(len(window))],
        "factors": {k: s.get(k) for k in SEVEN_FACTORS},
        "anchors": s.get("anchors") or {},
    }


def turn_record(ts: Dict[str, Any], session_id: str,
                project: Optional[str] = None) -> Dict[str, Any]:
    """把 L1 摘要转成可 ``retain`` 的记录参数。

    ``meta.pointer`` 是关键：它记录这条摘要覆盖的**轮次区间**与消息索引，
    让 L3 → L2 → L1 → L0 的回溯链成立。
    """
    return {
        "content": "[L1 轮次 %d-%d] %s" % (ts["turn_start"], ts["turn_end"],
                                          ts.get("text") or ""),
        "mtype": "episodic",
        "tags": ["turn_summary", "session:%s" % session_id, "level:L1"],
        "project": project,
        "importance": 3,
        "meta": {
            "kind": "turn_summary",
            "level": "L1",
            "session_id": session_id,
            "pointer": {
                "level": "L1",
                "session_id": session_id,
                "turn_start": ts["turn_start"],
                "turn_end": ts["turn_end"],
                "message_ids": ts.get("message_ids") or [],
            },
            "factors": ts.get("factors") or {},
            "anchors": ts.get("anchors") or {},
        },
    }


def topic_summary(summaries: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """L3 主题摘要：同一 project 下多会话合并。

    回答的是「这个项目现在处于什么状态」，而不是「上次聊了什么」。

    合并规则（全部确定性，不调模型）：

    - ``goal``：取最近一次会话的 goal（项目目标会演进，最新版最准）
    - ``decisions``：累积去重（决策只增不减，历史决策仍然有效）
    - ``pending``：**只保留最新的未决项** —— 旧的要么已解决，要么已被覆盖
    - ``failures``：累积去重（失败教训不随时间失效）
    - ``artifacts``：累积去重
    """
    if not summaries:
        return {"goal": "", "constraints": [], "decisions": [], "pending": [],
                "artifacts": [], "failures": [], "preferences": [],
                "session_count": 0, "session_ids": []}

    # 按时间排序，最新的在最后
    ordered = sorted(summaries, key=lambda s: str(s.get("created_at") or ""))

    def _acc(key: str, limit: int) -> List[str]:
        out: List[str] = []
        for s in ordered:
            f = s.get("factors") or {}
            for x in (f.get(key) or []):
                if x not in out:
                    out.append(x)
        return out[:limit]

    latest = ordered[-1]
    latest_f = latest.get("factors") or {}

    return {
        "goal": str(latest_f.get("goal") or ""),
        "constraints": _acc("constraints", 10),
        "decisions": _acc("decisions", 12),
        "pending": list(latest_f.get("pending") or [])[:6],
        "artifacts": _acc("artifacts", 12),
        "failures": _acc("failures", 8),
        "preferences": _acc("preferences", 8),
        "session_count": len(ordered),
        "session_ids": [str(s.get("session_id") or "") for s in ordered],
    }


def topic_record(ts: Dict[str, Any], project: str,
                 children: Sequence[str]) -> Dict[str, Any]:
    """把 L3 摘要转成记录参数。``pointer.children`` 指向 L2 记录 id。"""
    lines = ["# 主题摘要 · %s（%d 个会话）" % (project, ts.get("session_count") or 0)]
    if ts.get("goal"):
        lines.append("当前目标：%s" % ts["goal"])
    if ts.get("decisions"):
        lines.append("已定决策：\n" + "\n".join("- " + x for x in ts["decisions"][:8]))
    if ts.get("constraints"):
        lines.append("约束：%s" % "；".join(ts["constraints"][:6]))
    if ts.get("pending"):
        lines.append("未决：%s" % "；".join(ts["pending"]))
    if ts.get("failures"):
        lines.append("失败教训：\n" + "\n".join("- " + x for x in ts["failures"][:5]))
    if ts.get("artifacts"):
        lines.append("产物：%s" % "；".join(ts["artifacts"][:8]))
    return {
        "content": "\n".join(lines),
        "mtype": "episodic",
        "tags": ["topic_summary", "project:%s" % project, "level:L3"],
        "project": project,
        "importance": 5,
        "meta": {
            "kind": "topic_summary",
            "level": "L3",
            "project": project,
            "pointer": {
                "level": "L3",
                "project": project,
                "children": list(children),
                "session_ids": ts.get("session_ids") or [],
            },
            "factors": {k: ts.get(k) for k in SEVEN_FACTORS},
        },
    }


def trace_chain(brain: Any, memory_id: str, max_depth: int = 3) -> Dict[str, Any]:
    """沿指针链从 L3 回溯到 L1，返回每层内容。

    L0（原始消息）不在记忆库里 —— 那是调用方自己的对话历史。本函数返回
    ``message_ids``，调用方拿它去原始对话里取；取不到时至少能说明
    「这条结论来自第 3–7 轮」，而不是一句「来源未知」。
    """
    chain: List[Dict[str, Any]] = []
    seen: set = set()
    frontier = [memory_id]
    depth = 0
    while frontier and depth <= max_depth:
        nxt: List[str] = []
        for mid in frontier:
            if not mid or mid in seen:
                continue
            seen.add(mid)
            rec = None
            try:
                rec = brain.store.find_by_id(mid)
            except Exception:
                rec = None
            if not rec:
                continue
            meta = rec.get("meta") or {}
            if not isinstance(meta, dict):
                meta = {}
            ptr = meta.get("pointer") or {}
            chain.append({
                "id": mid,
                "level": ptr.get("level") or meta.get("level") or "?",
                "kind": meta.get("kind"),
                "content": str(rec.get("content") or "")[:400],
                "turn_range": ([ptr.get("turn_start"), ptr.get("turn_end")]
                               if ptr.get("turn_start") is not None else None),
                "message_ids": ptr.get("message_ids") or [],
                "session_id": ptr.get("session_id") or meta.get("session_id"),
                "project": ptr.get("project") or meta.get("project"),
            })
            for c in (ptr.get("children") or []):
                if c not in seen:
                    nxt.append(str(c))
        frontier = nxt
        depth += 1
    return {"root": memory_id, "depth": len(chain), "chain": chain,
            "reaches_l0": any(c["message_ids"] for c in chain)}
