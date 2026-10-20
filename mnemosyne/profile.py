# -*- coding: utf-8 -*-
"""用户画像卡（Profile Card）。


## 要解决的问题

「记住用户」失败的最常见原因，是把三种**变化速度完全不同**的信息混成一个池子，
然后用同一套过期策略管理：

| 层级 | 内容 | 变化速度 | 置信度起点 | 过期策略 |
|---|---|---|---|---|
| 稳定身份 | 职业、语言、时区、专业领域、长期技术栈 | 年级 | 0.9（一次明确陈述即可） | 不过期，每年复核 |
| 偏好与约束 | 回答风格、输出格式、禁忌、工作习惯 | 月级 | 0.6 起，多次印证后上升 | 6 个月未印证则降权 |
| 情境状态 | 当前项目、近期目标、临时限制 | 天/周级 | 0.5 | TTL 7–30 天，到期失效 |

混在一起的后果是双向的：情境被当成偏好（用户说「这次用英文回我」，系统记成
「偏好英文」，之后所有对话都变英文），或者偏好被当成情境（用户的核心禁忌
7 天后失效，下次就踩雷）。

## 最容易犯的错，用代码拦住

principle 6.1 给的判据：这句话有没有「这次」「今天」「先」这类**限定词**？有没有
**跨会话复现**？两个都没有，就只能进情境层，带 TTL。

本模块把这个判据硬编码成 ``_looks_situational()``：只要命中限定词，无论 mtype
写的是 preference 还是 identity，一律降级为情境层。宁可少记一条长期偏好，
也不要让一条临时the design requirement永久污染画像。

## 输出形态

``render()`` 产出的就是principle 6.4 的画像卡格式，可直接作为系统提示的一部分注入：

    # 用户画像（自动维护，请勿手工编辑）
    ## 稳定身份
    - 后端工程师，主要 Go / TypeScript [c=0.95, ev=3, since=2025-03]
    ...
    ## 偏好与约束（含反面）
    - 反感空话套话 [c=0.9, ev=5]
    ...
    ## 当前情境（带 TTL）
    - 正在做 Mnemosyne 记忆系统 [ttl=30d, c=0.8]
    ...
    ## 未决事项
    - 冲突消解策略：默认保守 vs 时间优先，等确认 [since=2026-10-19]
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "PROFILE_TIERS",
    "TIER_RULES",
    "classify_tier",
    "build_profile",
    "render",
    "token_estimate",
    "decay_candidates",
    "review_candidates",
]


#: 三段 + 未决。顺序即注入顺序（稳定的在前，临时的在后）。
PROFILE_TIERS: Tuple[str, ...] = ("identity", "preference", "context", "pending")

#: 每段的 TTL 与复核策略。
#: - ``ttl_days``：超过这个天数直接不再注入（情境层）
#: - ``decay_days``：超过这个天数未印证则降权（偏好层，降权不失效）
#: - ``review_days``：超过这个天数标记「待复核」（身份层）
TIER_RULES: Dict[str, Dict[str, Optional[int]]] = {
    "identity":   {"ttl_days": None, "decay_days": None, "review_days": 365},
    "preference": {"ttl_days": None, "decay_days": 180, "review_days": None},
    "context":    {"ttl_days": 30,   "decay_days": None, "review_days": None},
    "pending":    {"ttl_days": None, "decay_days": None, "review_days": None},
}

#: 情境限定词。命中即降级到 context 层 —— 这是principle 6.1 那条判据的代码化。
#: 注意「先」不单独列入：单字会误伤「先生」「优先」「首先」，只收「先+动词」组合。
_SITUATIONAL_MARKERS = (
    "这次", "此次", "本次", "今天", "今晚", "明天", "本周", "这周", "下周",
    "这个月", "本月", "暂时", "临时", "先不", "先别", "先用", "先按", "先看",
    "目前", "现在", "最近", "正在", "在做", "近期",
    "这阵子", "这段时间", "等会", "稍后", "一会儿",
    "this time", "today", "tonight", "tomorrow", "this week", "next week",
    "for now", "temporarily", "currently", "right now",
)

#: 明确的反面偏好信号（「不要 X」「别 X」「禁止 X」）。这类断言价值高，
#: 但必须进「偏好与约束」而不是普通偏好 —— 禁忌被违反的代价最大。
_NEGATIVE_MARKERS = (
    "不要", "别", "禁止", "避免", "拒绝", "反感", "讨厌", "不能",
    "do not", "don't", "never", "avoid", "hate",
)

#: 未决事项标记。
_PENDING_MARKERS = (
    "待确认", "等确认", "待定", "未定", "等用户", "待办", "未决",
    "pending", "todo", "tbd", "to be confirmed",
)

_IDENTITY_MTYPES = frozenset({"identity"})
_PREFERENCE_MTYPES = frozenset({"preference", "belief"})
_CONTEXT_MTYPES = frozenset({"todo"})

#: 身份事实的**内容**判据（v8.1.0 补齐）。
#:
#: 【为什么必须靠内容而不是 mtype】身份事实（"我是大三学生"、"我在腾讯工作"、
#: "我住在杭州"）在真实写入里几乎都以 ``mtype="semantic"`` 进来 —— 调用方
#: 不会为了"这是身份"专门改 mtype。只按 mtype 判，画像卡的「稳定身份」段
#: 永远是空的，而「偏好」段却塞满了本该属于身份的内容。
#:
#: 约束：主语必须是**用户本人**（用户/我/本人/我的）。否则"项目代号是 Mnemosyne"
#: 会被当成身份写进画像 —— 那是项目事实，不是人的属性。
_IDENTITY_PATTERNS = (
    re.compile(r"^(?:用户|我|本人|我的)[^。；，]{0,6}(?:是|为|在|于|住在|来自|毕业)"
               r"[^。；]{1,30}(?:学生|大学|学院|公司|工作|工程师|经理|总监|老师|医生|"
               r"律师|设计师|产品|运营|销售|会计|研究生|博士|本科|硕士|大一|大二|大三|大四|"
               r"岁|人|北京|上海|广州|深圳|杭州|成都|武汉|南京|西安)"),
    re.compile(r"^(?:用户|我|本人)(?:的)?(?:姓名|名字|年龄|生日|职业|学历|专业|籍贯|"
               r"常住地|所在城市|公司|岗位|职位)"),
    re.compile(r"^(?:用户|我|本人)[^。；]{0,4}(?:今年|明年)?(?:大一|大二|大三|大四|"
               r"研一|研二|研三|博一|博二|博三)"),
)


def _looks_like_identity(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return any(p.search(t) for p in _IDENTITY_PATTERNS)


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


def _has_any(text: str, markers: Iterable[str]) -> bool:
    low = text.lower()
    return any(m.lower() in low for m in markers)


def classify_tier(record: Dict[str, Any]) -> Optional[str]:
    """判定一条记忆属于画像的哪一段；不属于画像则返回 ``None``。

    判定顺序（前面的优先，且**情境限定词可以否决一切**）：

    1. 未决标记 → ``pending``；
    2. 命中情境限定词 → ``context``（即使 mtype 写着 preference）；
    3. ``mtype=todo`` → ``context``；
    4. ``mtype=identity`` 或 identity 标签 → ``identity``；
    5. ``mtype=preference/belief`` 或含反面标记 → ``preference``；
    6. 其余（semantic / episodic / procedural / resource / anti_pattern …）
       → ``None``，**不进画像**。

    第 2 条是这套逻辑的关键：它把「这次用英文回我」挡在长期偏好之外。
    第 6 条同样关键：画像卡只放「人」的信息，普通事实（接口文档在哪、
    某次排障怎么修的）应该走召回层，塞进画像只会挤占常驻预算。
    """
    text = str(record.get("content") or "")
    mtype = str(record.get("type") or record.get("mtype") or "").strip().lower()
    tags = {str(t).lower() for t in (record.get("tags") or [])}

    if "pending" in tags or "pending_decision" in tags or _has_any(text, _PENDING_MARKERS):
        return "pending"

    if _has_any(text, _SITUATIONAL_MARKERS):
        return "context"

    if mtype in _CONTEXT_MTYPES:
        return "context"
    if mtype in _IDENTITY_MTYPES or "identity" in tags:
        return "identity"
    # 内容判据必须排在 preference 之前：身份事实常以 semantic 进来，
    # 但也会被写成 "用户是……" 这种带断言语气的句子，容易被误判成偏好。
    if _looks_like_identity(text):
        return "identity"
    if mtype in _PREFERENCE_MTYPES:
        return "preference"
    if _has_any(text, _NEGATIVE_MARKERS):
        return "preference"
    return None


def _evidence_count(record: Dict[str, Any]) -> int:
    """印证次数：证据条数，至少 1（本身算一次）。"""
    ev = record.get("evidence") or record.get("evidence_ids") or []
    if isinstance(ev, (list, tuple)) and ev:
        return len(ev)
    try:
        return max(1, int(record.get("evidence_count") or 1))
    except (TypeError, ValueError):
        return 1


def _confidence(record: Dict[str, Any]) -> float:
    try:
        return max(0.0, min(1.0, float(record.get("confidence", 0.6))))
    except (TypeError, ValueError):
        return 0.6


def _is_active(record: Dict[str, Any]) -> bool:
    status = str(record.get("status") or "active").lower()
    return status == "active"


def _tier_ttl_days(record: Dict[str, Any], tier: str) -> Optional[int]:
    """该条在该段的有效期（天）。记录自带 ttl_days 优先。"""
    raw = record.get("ttl_days")
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    return TIER_RULES.get(tier, {}).get("ttl_days")


def _since(record: Dict[str, Any]) -> str:
    """``since=2025-03`` 形式的起始标记。"""
    dt = _parse(record.get("event_time") or record.get("created_at"))
    return dt.strftime("%Y-%m") if dt else ""


def _age_days(record: Dict[str, Any], now: datetime) -> Optional[int]:
    dt = _parse(record.get("last_verified") or record.get("updated_at")
                or record.get("created_at"))
    if dt is None:
        return None
    return max(0, int((now - dt).total_seconds() // 86400))


def build_profile(records: Iterable[Dict[str, Any]],
                  now: Optional[datetime] = None,
                  max_per_tier: int = 20) -> Dict[str, List[Dict[str, Any]]]:
    """从记忆记录构建画像。

    返回 ``{tier: [item, ...]}``，每项::

        {"content", "tier", "confidence", "evidence_count", "ttl_days",
         "since", "age_days", "id", "negative", "expired", "needs_review"}

    排序规则：同段内按 ``confidence × log(1+evidence)`` 降序 —— 印证次数多的
    优先，但不让单次高置信的条目完全压住多次印证的低置信条目。

    过滤规则：
    - 非 active 状态不进画像（失效的不该继续影响行为）；
    - 情境层超过 TTL 的不进（这是「这周在出差」不会永久驻留的原因）；
    - 每段最多 ``max_per_tier`` 条，保证画像卡不膨胀。
    """
    now = now or _now()
    buckets: Dict[str, List[Dict[str, Any]]] = {t: [] for t in PROFILE_TIERS}

    for r in records:
        if not isinstance(r, dict) or not _is_active(r):
            continue
        content = str(r.get("content") or "").strip()
        if not content:
            continue
        tier = classify_tier(r)
        if tier is None:
            continue  # 不属于画像的事实（文档位置、排障记录）走召回层
        conf = _confidence(r)
        ev = _evidence_count(r)
        ttl = _tier_ttl_days(r, tier)
        age = _age_days(r, now)

        expired = False
        if ttl is not None and age is not None and age > ttl:
            expired = True
        if tier == "context" and expired:
            continue  # 情境过期直接不注入

        review_days = TIER_RULES.get(tier, {}).get("review_days")
        needs_review = bool(review_days and age is not None and age > review_days)

        buckets[tier].append({
            "content": content,
            "tier": tier,
            "confidence": round(conf, 3),
            "evidence_count": ev,
            "ttl_days": ttl,
            "since": _since(r),
            "age_days": age,
            "id": r.get("id"),
            "negative": _has_any(content, _NEGATIVE_MARKERS),
            "expired": expired,
            "needs_review": needs_review,
        })

    import math
    for tier, items in buckets.items():
        items.sort(key=lambda x: (x["confidence"] * math.log(1 + x["evidence_count"]),
                                  x["confidence"]), reverse=True)
        buckets[tier] = items[:max_per_tier]
    return buckets


def render(profile: Dict[str, List[Dict[str, Any]]],
           tier_titles: Optional[Dict[str, str]] = None,
           include_context: bool = True,
           include_pending: bool = True) -> str:
    """把画像渲染成可直接注入的 Markdown 文本。

    ``include_context=False`` 时只输出「稳定身份 + 偏好」两段（约 200 token），
    把情境与未决项留给召回层按需加载 —— 对应principle 6.4 的最后一段建议。
    """
    titles = tier_titles or {
        "identity": "稳定身份",
        "preference": "偏好与约束（含反面）",
        "context": "当前情境（带 TTL）",
        "pending": "未决事项",
    }
    order = ["identity", "preference"]
    if include_context:
        order.append("context")
    if include_pending:
        order.append("pending")

    lines: List[str] = ["# 用户画像（自动维护，请勿手工编辑）"]
    for tier in order:
        items = profile.get(tier) or []
        if not items:
            continue
        lines.append("## " + titles.get(tier, tier))
        for it in items:
            marks = ["c=%s" % it["confidence"], "ev=%s" % it["evidence_count"]]
            if it.get("ttl_days") is not None:
                marks.append("ttl=%dd" % it["ttl_days"])
            if it.get("since"):
                marks.append("since=%s" % it["since"])
            if it.get("needs_review"):
                marks.append("待复核")
            lines.append("- %s [%s]" % (it["content"], ", ".join(marks)))
    if len(lines) == 1:
        lines.append("（暂无画像数据）")
    return "\n".join(lines)


def token_estimate(text: str) -> int:
    """粗略 token 估算：中文按字、英文按 4 字符。

    不引第三方分词器（项目零依赖约束）。画像卡的预算校验不需要精确值，
    只要量级对得上即可 —— 500 token 的硬约束用这个估法误差在 ±15% 以内。
    """
    if not text:
        return 0
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = len(text) - cjk
    return int(cjk * 1.0 + other / 4.0)


def decay_candidates(records: Iterable[Dict[str, Any]],
                     now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """偏好层「6 个月未印证」的降权候选。

    只降权不失效：偏好会变，但旧偏好仍有语境价值（「他以前喜欢详细解释，
    后来嫌啰嗦」这个变化本身就是重要信息）。
    """
    now = now or _now()
    out: List[Dict[str, Any]] = []
    for r in records:
        if not isinstance(r, dict) or not _is_active(r):
            continue
        if classify_tier(r) != "preference":
            continue
        age = _age_days(r, now)
        limit = TIER_RULES["preference"]["decay_days"]
        if age is not None and limit is not None and age > limit:
            out.append({
                "id": r.get("id"),
                "content": str(r.get("content") or "")[:120],
                "age_days": age,
                "limit_days": limit,
                "confidence": _confidence(r),
                "suggested_confidence": round(max(0.3, _confidence(r) * 0.8), 3),
            })
    return out


def review_candidates(records: Iterable[Dict[str, Any]],
                      now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """身份层「一年未复核」的候选。

    身份信息变化最慢，但不是不变（换职业、换时区、换母语）。the principle requires
    「不过期，但每年复核一次」—— 这里只产出待复核清单，是否修改由人或
    上游决定，不自动改内容。
    """
    now = now or _now()
    out: List[Dict[str, Any]] = []
    for r in records:
        if not isinstance(r, dict) or not _is_active(r):
            continue
        if classify_tier(r) != "identity":
            continue
        age = _age_days(r, now)
        limit = TIER_RULES["identity"]["review_days"]
        if age is not None and limit is not None and age > limit:
            out.append({
                "id": r.get("id"),
                "content": str(r.get("content") or "")[:120],
                "age_days": age,
                "limit_days": limit,
            })
    return out
