# -*- coding: utf-8 -*-
"""统一记忆信封（Unified Memory Envelope）。


## 为什么需要这一层

8.0.0 的记忆记录是「按功能长出来的」：mtype 有 12 种（semantic / preference /
todo / lesson / belief / observation / strategy / reflective / web / identity /
episodic / procedural），每种对应一个历史时期的用法。这带来两个后果：

1. **分类无法治理**。the principle requires八类记忆各有独立的写入时机、检索触发与过期策略；
   12 个 mtype 里 `preference` 和 `semantic` 的生命周期完全不同，却走同一套 TTL。
2. **信封字段缺失**。principle的 envelope 有 `evidence`（证据链）、`t_invalid`
   （显式失效时间）、`ttl_days`、`structured`（SPO 三元组）、四维 `scope`。
   8.0.0 只有 `event_time` / `knowledge_time` / `source_type` / `confidence`，
   缺的三项恰好是「能被质疑、能被追溯、能被失效」的前提。

## 设计原则：不迁移数据，只做视图

本模块**不改存储格式**。所有函数都是「记录 → 信封」或「信封 → 记录」的纯转换，
缺失字段按默认值补齐但**不回写原记录**。这样：

- 旧数据零迁移成本，读取时自动获得信封视图；
- 新写入可以选择性带上 `evidence` / `ttl_days`，不带也能正常工作；
- 存储层（JSONL / SQLite）完全不需要知道信封的存在。

## 八类记忆的 TTL 策略

| type | 默认 TTL | 理由 |
|---|---|---|
| semantic | 不过期 | 长期，但需定期复核（偏好会变） |
| episodic | 90 天 | 原始留存 90 天，摘要长期 |
| procedural | 不过期 | 长期，按成功率淘汰 |
| working | 7 天 | 任务结束后降级归档 |
| resource | 不过期 | 随源失效，需校验可达性 |
| anti_pattern | 永久 | 价值最高、最易被忽略 |
| meta | 随宿主 | 与宿主记忆同生命周期 |
| relational | 不过期 | 长期，随图演化 |
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "MEMORY_TYPES",
    "TTL_POLICY",
    "SCOPE_DIMENSIONS",
    "classify_type",
    "default_ttl_days",
    "to_envelope",
    "from_envelope",
    "scope_of",
    "structured_of",
    "evidence_of",
    "is_expired",
    "is_valid_at",
    "envelope_diff",
]


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: principle定义的八类记忆。顺序即优先级（冲突时靠前者胜）。
MEMORY_TYPES: Tuple[str, ...] = (
    "semantic",      # 语义：画像、稳定事实、偏好、约束、实体关系
    "episodic",      # 情景：发生了什么（时间、参与者、结果）
    "procedural",    # 程序性：SOP、workflow、规则、检查清单
    "working",       # 工作：当前目标、进度、待办、临时约束
    "resource",      # 资源：文件/链接/数据集的引用（不是内容本身）
    "anti_pattern",  # 负记忆：失败教训、被回滚方案、明确禁止
    "meta",          # 元记忆：来源、置信度、证据链、派生关系
    "relational",    # 关系：人-人、项目-项目、任务-代码的关联网络
)

#: 每类记忆的默认生命周期（天）。None = 不过期。
TTL_POLICY: Dict[str, Optional[int]] = {
    "semantic": None,
    "episodic": 90,
    "procedural": None,
    "working": 7,
    "resource": None,
    "anti_pattern": None,
    "meta": None,
    "relational": None,
}

#: 四维命名空间。决定「谁能读到这条记忆」。
SCOPE_DIMENSIONS: Tuple[str, ...] = ("user", "project", "agent", "session")

#: 旧 mtype → 八类。映射不了的一律归 semantic（保守：语义类不过期）。
_TYPE_ALIASES: Dict[str, str] = {
    "semantic": "semantic",
    "identity": "semantic",
    "observation": "semantic",
    "web": "semantic",
    "reflective": "semantic",
    "belief": "semantic",
    "preference": "semantic",
    "episodic": "episodic",
    "procedural": "procedural",
    "lesson": "procedural",
    "strategy": "procedural",
    "todo": "working",
    "task": "working",
    "playbook": "procedural",
    "anti_pattern": "anti_pattern",
    "resource": "resource",
    "meta": "meta",
    "relational": "relational",
}

#: 标签里出现这些词时，即使 mtype 是 semantic 也判为负记忆。
#: 负记忆的价值密度最高，不能被「一条普通 fact」淹没。
_ANTI_TAGS = frozenset({"anti_pattern", "negative", "lesson_failed", "rejected",
                        "forbidden", "do_not", "failure"})


# ---------------------------------------------------------------------------
# 时间工具
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> Optional[datetime]:
    """尽最大努力把各种时间表示解析成带时区的 datetime。

    存量数据里时间格式不统一（ISO 带 Z、带偏移、纯日期、Unix 时间戳），
    解析失败一律返回 None 而不是抛异常 —— 信封是只读视图，不能因为一条
    脏数据让整个 recall 崩掉。
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    s = str(value).strip()
    if not s:
        return None
    # 纯日期
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        s = s + "T00:00:00+00:00"
    # 结尾 Z → +00:00（fromisoformat 在 3.10 之前不认 Z）
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # 再退一步：去掉毫秒和时区，只留到秒
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s[:19], fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# 分类
# ---------------------------------------------------------------------------

def classify_type(record: Dict[str, Any]) -> str:
    """把一条记录判成八类之一。

    判定顺序（前面的优先）：
    1. 记录里已显式写了八类之一的 `type` → 直接用（新写入走这条）；
    2. 带负记忆标签 → ``anti_pattern``；
    3. 带 URI 类内容（路径/URL）且 mtype 不是 working → ``resource``；
    4. 按旧 mtype 别名表映射；
    5. 兜底 ``semantic``。

    注意第 2、3 条：负记忆和资源记忆在 8.0.0 里根本没有独立类型，它们混在
    `lesson` 和普通 `fact` 里。如果不在这里显式捞出来，principle最强调的两类
    （「价值密度最高」的负记忆、「随源失效」的资源记忆）永远无法被单独治理。
    """
    # ⚠️ 关键：不能用 `record["type"]` 直接判定八类。
    # 存储层的 `type` 字段历史上存的是 **mtype**（semantic/preference/todo…），
    # 而 mtype 里 semantic 恰好也是八类之一。如果看到 `type="semantic"` 就返回，
    # 后面所有「负记忆 / 资源记忆」的判定永远执行不到 —— 这两类在 8.0.0 里
    # 本来就混在 lesson 和普通 fact 中，正是最需要被捞出来的。
    # 所以顺序是：显式声明 → 标签/内容语义 → mtype 映射。
    meta = record.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}
    explicit = str(meta.get("memory_type") or "").strip().lower()
    if explicit in MEMORY_TYPES:
        return explicit

    raw = str(record.get("type") or record.get("mtype") or "").strip().lower()

    tags = {str(t).lower() for t in (record.get("tags") or [])}
    if tags & _ANTI_TAGS:
        return "anti_pattern"

    # 资源记忆：内容是「引用」而不是「陈述」。
    # 判据三种，满足其一即可：
    #   1. 显式 uri 字段 / 显式 resource 标签；
    #   2. 内容本身就是 URI（http(s):// / file:// / 以 / 开头的绝对路径）；
    #   3. 内容短且以常见文件扩展名结尾（「接口文档在 /docs/api.md」这类）。
    # 收紧到这三种是为了避免把「含路径的普通事实」误判成资源。
    if record.get("uri") or ("resource" in tags):
        return "resource"
    _content = str(record.get("content") or "").strip()
    if _content:
        low = _content.lower()
        if low.startswith(("http://", "https://", "file://", "ftp://", "s3://")):
            return "resource"
        if len(_content) <= 200 and _content.startswith("/") and " " not in _content:
            return "resource"
        _exts = (".md", ".json", ".yaml", ".yml", ".py", ".ts", ".js", ".go",
                 ".pdf", ".csv", ".xlsx", ".docx", ".png", ".jpg", ".zip")
        if len(_content) <= 200 and low.endswith(_exts):
            return "resource"

    if raw in _TYPE_ALIASES:
        return _TYPE_ALIASES[raw]
    return "semantic"


def default_ttl_days(mtype: str) -> Optional[int]:
    """返回某类记忆的默认 TTL（天）。None 表示不过期。"""
    return TTL_POLICY.get(mtype if mtype in TTL_POLICY else "semantic")


# ---------------------------------------------------------------------------
# scope / structured / evidence
# ---------------------------------------------------------------------------

def scope_of(record: Dict[str, Any]) -> Dict[str, str]:
    """四维命名空间。

    8.0.0 只有 namespace（等价 user 级）、project、session_id，**没有 agent 维度**。
    the principle requires四维，因为「同一个用户在同一个项目下，被两个不同的 Agent 写入的记忆
    不应该互相污染」——这是多 Agent 协作场景的基本隔离要求。

    agent 维度从 ``meta.agent`` / ``tool_name`` / ``tags`` 里的 ``agent:<name>`` 取。
    """
    meta = record.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}
    agent = str(meta.get("agent") or "").strip()
    if not agent:
        agent = str(record.get("tool_name") or "").strip()
    if not agent:
        for t in (record.get("tags") or []):
            s = str(t)
            if s.startswith("agent:"):
                agent = s[6:].strip()
                break
    return {
        "user": str(record.get("namespace") or meta.get("user") or "").strip(),
        "project": str(record.get("project") or "").strip(),
        "agent": agent,
        "session": str(record.get("session_id") or "").strip(),
    }


def structured_of(record: Dict[str, Any]) -> Dict[str, str]:
    """SPO 三元组视图。

    8.0.0 只存了扁平的 ``entities`` 列表，没有主谓宾结构。没有 SPO 就无法回答
    「用户的包管理器偏好是什么」这类结构化问题，只能靠语义相似度碰运气。

    这里做**确定性**抽取（不调模型）：取第一个实体做 subject，从谓词表匹配
    predicate，谓词之后的部分做 object。抽不出就返回空 dict —— 宁可没有，
    也不要编一个错的 SPO 污染下游冲突判定。
    """
    explicit = record.get("structured")
    if isinstance(explicit, dict) and explicit:
        return {
            "subject": str(explicit.get("subject") or ""),
            "predicate": str(explicit.get("predicate") or ""),
            "object": str(explicit.get("object") or ""),
        }

    text = str(record.get("content") or "").strip()
    if not text:
        return {}
    ents = record.get("entities") or []

    # 谓词表：中英混排，按长度倒序匹配，避免「是」抢先匹配掉「不是」
    predicates = (
        "不是", "不能", "不会", "不再", "不喜欢", "不需要",
        "偏好", "喜欢", "讨厌", "反感", "拒绝", "禁止", "避免",
        "负责", "使用", "采用", "依赖", "住在", "位于", "属于",
        "是", "有", "在", "用",
        "prefers", "likes", "dislikes", "uses", "is", "has",
    )
    for p in sorted(predicates, key=len, reverse=True):
        idx = text.find(p)
        if idx <= 0:
            continue
        head = text[:idx].strip()
        tail = text[idx + len(p):].strip()
        # object 取到第一个断句符为止，避免把整段都当宾语
        for sep in ("，", "。", "；", "、", "：", "\n", ",", ".", ";", ":"):
            if sep in tail:
                tail = tail.split(sep)[0].strip()
        if not tail:
            continue
        # subject 优先用谓词前的那一段（它是确定的主语位置）；
        # 抽出的实体只在「是 head 的子串且非空」时才采信 —— 否则会把
        # 「用户偏好用」这种把谓词也吞进去的脏碎片当成主语。
        subj = head
        if ents:
            cand = str(ents[0]).strip()
            if cand and cand in head and len(cand) >= 2:
                subj = cand
        if not subj:
            continue
        return {
            "subject": subj,
            "predicate": p,
            "object": tail,
        }
    return {}


def evidence_of(record: Dict[str, Any]) -> List[str]:
    """证据链：这条记忆是从哪几条消息/事件推出来的。

    8.0.0 完全没有这个字段。缺了它，一条错误记忆就无法回答「你凭什么这么认为」，
    也无法在底层证据被删时重算结论。这里同时兼容三种历史写法：
    ``evidence`` / ``evidence_ids`` / ``meta.evidence``。
    """
    for key in ("evidence", "evidence_ids"):
        v = record.get(key)
        if isinstance(v, (list, tuple)):
            return [str(x) for x in v if str(x).strip()]
    meta = record.get("meta") or {}
    if isinstance(meta, dict):
        v = meta.get("evidence") or meta.get("evidence_ids")
        if isinstance(v, (list, tuple)):
            return [str(x) for x in v if str(x).strip()]
    # 会话摘要的未决事项会挂 source_message_ids
    v = record.get("source_message_ids")
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if str(x).strip()]
    return []


# ---------------------------------------------------------------------------
# 信封转换
# ---------------------------------------------------------------------------

def to_envelope(record: Dict[str, Any]) -> Dict[str, Any]:
    """把一条存储记录转成principle定义的统一信封。

    纯函数，不改动入参。缺失字段按默认值补齐。
    """
    mtype = classify_type(record)
    meta = record.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}

    created = _parse_ts(record.get("knowledge_time") or record.get("created_at"))
    t_valid = _parse_ts(record.get("event_time") or record.get("created_at"))
    last_verified = _parse_ts(record.get("last_verified") or record.get("updated_at")
                              or record.get("created_at"))

    # t_invalid：显式失效时间。三种历史表示法都要认。
    t_invalid = _parse_ts(record.get("t_invalid") or record.get("superseded_at")
                          or meta.get("superseded_at"))
    status = str(record.get("status") or "active").lower()
    if t_invalid is None and status in ("superseded", "deleted", "expired"):
        # 状态说失效了但没记时间：退化为 updated_at，至少能让时间线排序成立
        t_invalid = _parse_ts(record.get("updated_at") or record.get("created_at"))

    # ttl_days：显式值优先；否则按类型默认；否则从 expires_at 反推
    ttl_days = record.get("ttl_days")
    if ttl_days is None:
        ttl_days = meta.get("ttl_days")
    if ttl_days is None:
        exp = _parse_ts(record.get("expires_at"))
        if exp is not None and created is not None:
            ttl_days = max(0, int((exp - created).total_seconds() // 86400))
    if ttl_days is None:
        ttl_days = default_ttl_days(mtype)
    try:
        ttl_days = int(ttl_days) if ttl_days is not None else None
    except (TypeError, ValueError):
        ttl_days = default_ttl_days(mtype)

    confidence = record.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else 0.7
    except (TypeError, ValueError):
        confidence = 0.7

    return {
        "id": record.get("id"),
        # `type` 是历史字段名（8.0.0 里它存的是 mtype）。v8.1.0 起它表示
        # **八类记忆**，但保留旧名以免破坏既有消费方；同时新增语义明确的
        # `memory_type`，新代码应优先读它。两个键永远同值。
        "type": mtype,
        "memory_type": mtype,
        "scope": scope_of(record),
        "content": record.get("content") or "",
        "structured": structured_of(record),
        "evidence": evidence_of(record),
        "confidence": round(max(0.0, min(1.0, confidence)), 4),
        "source": record.get("source_type") or record.get("source") or "unknown",
        "sensitivity": record.get("sensitivity") or "normal",
        "t_valid": _iso(t_valid),
        "t_invalid": _iso(t_invalid),
        "t_created": _iso(created),
        "last_verified": _iso(last_verified),
        "ttl_days": ttl_days,
        # 以下三项不属于principle envelope，但对追溯必要，放在扩展区
        "verification": record.get("verification") or "unverified",
        "derived_from": list(record.get("derived_from") or record.get("consolidated_from") or []),
        "status": status,
    }


def from_envelope(env: Dict[str, Any]) -> Dict[str, Any]:
    """把信封转回存储记录格式（供新写入路径使用）。

    只输出存储层认识的字段，不认识的塞进 ``meta``，避免污染主表 schema。
    """
    if not isinstance(env, dict):
        raise TypeError("from_envelope() 需要 dict")
    mtype = str(env.get("type") or "semantic").lower()
    if mtype not in MEMORY_TYPES:
        mtype = "semantic"

    scope = env.get("scope") or {}
    if not isinstance(scope, dict):
        scope = {}
    structured = env.get("structured") or {}
    if not isinstance(structured, dict):
        structured = {}

    rec: Dict[str, Any] = {
        "content": env.get("content") or "",
        "type": mtype,
        "namespace": scope.get("user") or None,
        "project": scope.get("project") or None,
        "session_id": scope.get("session") or None,
        "confidence": env.get("confidence"),
        "source_type": env.get("source"),
        "sensitivity": env.get("sensitivity") or "normal",
        "event_time": env.get("t_valid"),
        "knowledge_time": env.get("t_created"),
        "evidence": list(env.get("evidence") or []),
        "structured": dict(structured),
        "ttl_days": env.get("ttl_days"),
    }
    meta: Dict[str, Any] = {}
    if scope.get("agent"):
        meta["agent"] = scope["agent"]
    if env.get("derived_from"):
        meta["derived_from"] = list(env["derived_from"])
    if meta:
        rec["meta"] = meta
    return {k: v for k, v in rec.items() if v is not None}


# ---------------------------------------------------------------------------
# 有效性判定
# ---------------------------------------------------------------------------

def is_expired(record: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """是否已过 TTL。

    判定顺序：显式 ``expires_at`` → 信封 ``ttl_days`` + ``t_created``。
    两者都没有 → 不过期。
    """
    now = now or _now()
    exp = _parse_ts(record.get("expires_at"))
    if exp is not None:
        return now >= exp
    env = to_envelope(record)
    if env["ttl_days"] is None:
        return False
    created = _parse_ts(env["t_created"])
    if created is None:
        return False
    return now >= created + timedelta(days=env["ttl_days"])


def is_valid_at(record: Dict[str, Any], when: Optional[Any] = None) -> bool:
    """在某个时间点上，这条记忆是否成立。

    这是双时间轴的真正用途：既能答「他现在在哪家公司」，也能答
    「去年他在哪家公司」。实现上是闭区间 ``[t_valid, t_invalid)``。
    """
    at = _parse_ts(when) or _now()
    env = to_envelope(record)
    start = _parse_ts(env["t_valid"])
    end = _parse_ts(env["t_invalid"])
    if start is not None and at < start:
        return False
    if end is not None and at >= end:
        return False
    return True


def envelope_diff(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, List[str]]:
    """两封信封之间「哪些字段变了」。

    给后台管理与审计用：用户问「这条记忆为什么改了」，要能答出改的是置信度
    还是内容本身，而不是只给一个 updated_at。
    """
    a, b = to_envelope(old), to_envelope(new)
    changed: Dict[str, List[str]] = {}
    for k in set(a) | set(b):
        va, vb = a.get(k), b.get(k)
        if va != vb:
            changed[k] = [str(va), str(vb)]
    return changed
