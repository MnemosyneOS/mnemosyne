# -*- coding: utf-8 -*-
"""召回五步流水线与预算内注入。


## 五步，每一步都解决一个具体失败模式

| 步骤 | 做什么 | 不做会怎样 |
|---|---|---|
| ① 查询构造 | 拼 goal + 近 3 轮 + 当前实体 | 单句查询是召回不准的第一大原因 |
| ② 多路召回 | 语义 / 词法 / 图 / 时间 四路各取 Top-N | 向量对专名极弱，漏掉「bge」「SOUL.md」这类词 |
| ③ RRF 融合 | 按名次融合，不碰分数尺度 | 各通道分数量纲不同，加权合并必然有一边被压死 |
| ④ 重排裁剪 | confidence × 时间衰减 × 来源权重 | 高置信旧事实被低置信新噪声挤掉 |
| ⑤ 预算内注入 | 先目录后正文 + 四段配额 | token 爆炸，注意力被稀释，效果反而更差 |

## 为什么用 RRF 而不是加权

8.0.0 已经有一套「动态权重」（向量 .55 / BM25 .12 / 图 .10 / 时间 .20 /
置信 .10）。那是**分数级**融合，需要各通道分数可比。但 BM25 分数是无界的，
向量余弦是 [-1,1]，图通道给的是跳数倒数——三者量纲根本不同，任何固定权重
都只是把某一路的偏差放大。

RRF 只用**名次**：``score = Σ 1/(k + rank)``，k=60。不需要调尺度，工程上
最省事，对「某一路特别强/特别弱」也最鲁棒。这里保留 8.0.0 的加权融合作为
``mode="weighted"``，默认走 RRF。

## 注入预算：30 / 35 / 20 / 15

按principle 5.4 的分配（假设总预算 4000 token）：

- **30% 常驻画像**：身份、偏好、硬约束（几乎每次都相关）
- **35% 任务相关**：当前 goal 相关的事实与历史案例
- **20% 经验与负记忆**：候选 playbook + 已知禁忌
- **15% 未决事项**：上次留下的待办与待确认问题

这个比例不是拍脑袋：画像几乎恒定相关所以给固定份额；任务相关波动最大所以
给最多；经验和禁忌是「防止犯错的护栏」，量少但必须留位置；未决事项最少，
因为通常只有 1–3 条。

## 渐进披露：目录层是必需的

全量注入的代价是 O(全部)，目录层把它降到 O(目录 + 少量正文)。这里产出
``catalog``（标题 + 一句话 + token 估算），调用方判断后再用 ``expand``
（或 ``brain.expand``）取正文。
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "InjectionPipeline",
    "BUDGET_SHARES",
    "build_query",
    "rrf_fuse",
    "time_decay",
    "SOURCE_WEIGHTS",
]


#: 注入预算的四段配额（比例之和为 1.0）。
BUDGET_SHARES: Dict[str, float] = {
    "profile": 0.30,      # 常驻画像
    "task_related": 0.35,  # 任务相关
    "experience": 0.20,   # 经验与负记忆
    "pending": 0.15,      # 未决事项
}

#: 来源权重（与 policy.SOURCE_TRUST 一致，这里独立一份避免循环导入）。
SOURCE_WEIGHTS: Dict[str, float] = {
    "user": 0.95, "user_explicit": 0.95, "system": 0.90, "file": 0.85,
    "tool_result": 0.85, "user_implicit": 0.75,
    "agent_generated": 0.55, "inference": 0.50, "external": 0.40,
    "external_web": 0.40, "web_search": 0.40,
}

#: 时间衰减半衰期（天）。旧事可能突然变相关，所以是衰减不是删除。
DECAY_HALF_LIFE_DAYS = 90.0

#: RRF 的 k 常数。60 是原论文（Cormack et al., 2009）的推荐值。
RRF_K = 60


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


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    return int(cjk + (len(text) - cjk) / 4.0)


def time_decay(record: Dict[str, Any], now: Optional[datetime] = None) -> float:
    """时间衰减因子，范围 (0, 1]。

    用指数衰减而不是线性：一年前的记忆不应该直接归零（旧事可能突然变相关），
    但也不该和昨天的记忆同等权重。半衰期 90 天意味着：
    3 个月 → 0.5，6 个月 → 0.25，1 年 → 0.06。
    """
    now = now or _now()
    ts = _parse(record.get("last_accessed_at") or record.get("event_time")
                or record.get("created_at"))
    if ts is None:
        return 0.5  # 无时间戳：给中位权重，不偏袒也不惩罚
    age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
    return math.exp(-age_days * math.log(2) / DECAY_HALF_LIFE_DAYS)


def build_query(query: str, goal: str = "",
                recent_turns: Optional[Sequence[str]] = None,
                entities: Optional[Sequence[str]] = None,
                max_extra: int = 3) -> str:
    """① 查询构造。

    单句查询是召回不准的第一大原因。该设计原则要求拼上三样：

    1. working memory 的 ``goal``（用户这次到底想达成什么）；
    2. 最近 3 轮对话（补足指代与省略）；
    3. 当前实体（文件名 / 项目名 / 人名）。

    去重后拼接，总长度受控 —— 拼太多会稀释查询本身的信号。
    """
    parts: List[str] = []
    base = str(query or "").strip()
    if base:
        parts.append(base)
    g = str(goal or "").strip()
    if g and g != base:
        parts.append(g)
    turns = [str(t).strip() for t in (recent_turns or []) if str(t).strip()]
    for t in turns[-max_extra:]:
        if t and t not in parts:
            parts.append(t)
    ents = [str(e).strip() for e in (entities or []) if str(e).strip()]
    for e in ents[:6]:
        if e and e not in parts:
            parts.append(e)
    # 去重保序
    seen, out = set(), []
    for p in parts:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return " ".join(out)


def rrf_fuse(channels: Dict[str, Sequence[Any]], k: int = RRF_K,
             key: str = "id") -> List[Tuple[float, Dict[str, Any]]]:
    """③ Reciprocal Rank Fusion。

    ``channels`` 形如 ``{"vector": [(score, rec), ...], "bm25": [...]}``。
    返回 ``[(rrf_score, rec), ...]``，按分数降序。

    同一记录出现在多路里会累加 —— 这正是 RRF 的价值：多路都认为相关，
    比单路给个超高分数更可信。
    """
    scores: Dict[str, float] = {}
    store: Dict[str, Dict[str, Any]] = {}
    for _name, items in channels.items():
        for rank, item in enumerate(items or [], 1):
            rec = _record_of(item)
            if not rec:
                continue
            rid = str(rec.get(key) or rec.get("id") or id(rec))
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + rank)
            store.setdefault(rid, rec)
    fused = [(s, store[rid]) for rid, s in scores.items()]
    fused.sort(key=lambda x: x[0], reverse=True)
    return fused


def _record_of(item: Any) -> Optional[Dict[str, Any]]:
    if isinstance(item, dict):
        return item
    if isinstance(item, (list, tuple)) and len(item) >= 2 and isinstance(item[1], dict):
        return item[1]
    return None


class InjectionPipeline:
    """召回五步流水线。

    用法::

        pipe = InjectionPipeline(brain)
        out = pipe.inject("上次那个冲突策略定了吗", budget_tokens=4000)
        print(out["text"])          # 可直接塞进系统提示
        print(out["catalog"])       # 目录层，按需 expand
    """

    def __init__(self, brain: Any):
        self.brain = brain

    # ------------------------------------------------------------------
    # ② 多路召回
    # ------------------------------------------------------------------

    def multi_recall(self, query: str, per_channel: int = 12,
                     project: Optional[str] = None,
                     date_from: Optional[str] = None,
                     date_to: Optional[str] = None) -> Dict[str, List[Any]]:
        """四路各取 Top-N。

        每一路用 ``retrieve()`` 的不同参数组合实现，而不是重写通道：
        ``retrieval.py`` 是 8.0.0 最强也最脆弱的部分，**不动它**，
        只换调用方式。

        - ``vector``：语义通道（向量 + 置信 + 时间的融合）
        - ``lexical``：关掉向量，纯词法（专名、版本号、文件路径靠它）
        - ``graph``：开多跳，实体关联
        - ``temporal``：限定时间窗（近期记忆）
        """
        out: Dict[str, List[Any]] = {}
        base = dict(k=per_channel, project=project, apply_floor=False)

        try:
            out["vector"] = self.brain.retrieval.retrieve(
                self.brain.store, query, use_graph=False, **base) or []
        except TypeError:
            out["vector"] = self.brain.retrieval.retrieve(
                self.brain.store, query, k=per_channel) or []
        except Exception:
            out["vector"] = []

        try:
            out["lexical"] = self.brain.retrieval.retrieve(
                self.brain.store, query, use_vector=False, use_graph=False, **base) or []
        except Exception:
            out["lexical"] = []

        try:
            out["graph"] = self.brain.retrieval.retrieve(
                self.brain.store, query, use_vector=False, use_graph=True,
                multi_hop=True, **base) or []
        except Exception:
            out["graph"] = []

        # 时间路：不给窗就取最近 30 天
        if not date_from:
            from datetime import timedelta
            date_from = (_now() - timedelta(days=30)).strftime("%Y-%m-%d")
        try:
            out["temporal"] = self.brain.retrieval.retrieve(
                self.brain.store, query, date_from=date_from, date_to=date_to,
                **base) or []
        except Exception:
            out["temporal"] = []

        return out

    # ------------------------------------------------------------------
    # ④ 重排
    # ------------------------------------------------------------------

    def rerank(self, fused: List[Tuple[float, Dict[str, Any]]],
               now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """按 ``RRF × 置信 × 时间衰减 × 来源权重`` 重排。

        四个因子相乘而不是相加：任何一个因子接近 0 都应该把条目压下去
        （低置信 + 外部来源 + 很旧 = 不该出现在上下文里）。
        低置信条目不直接丢，标记 ``low_confidence=True`` 让调用方决定。
        """
        now = now or _now()
        out: List[Dict[str, Any]] = []
        for base_score, rec in fused:
            try:
                conf = float(rec.get("confidence") or 0.7)
            except (TypeError, ValueError):
                conf = 0.7
            src = str(rec.get("source_type") or rec.get("source") or "").lower()
            sw = SOURCE_WEIGHTS.get(src, 0.6)
            td = time_decay(rec, now)
            score = base_score * (0.5 + 0.5 * conf) * sw * (0.4 + 0.6 * td)
            out.append({
                "record": rec,
                "score": round(score, 6),
                "rrf": round(base_score, 6),
                "confidence": round(conf, 3),
                "source_weight": sw,
                "time_decay": round(td, 4),
                "low_confidence": conf < 0.5,
            })
        out.sort(key=lambda x: x["score"], reverse=True)
        return out

    # ------------------------------------------------------------------
    # ⑤ 预算内注入
    # ------------------------------------------------------------------

    def inject(self, query: str, budget_tokens: int = 4000,
               goal: str = "", recent_turns: Optional[Sequence[str]] = None,
               entities: Optional[Sequence[str]] = None,
               project: Optional[str] = None,
               mode: str = "rrf",
               include_profile: bool = True,
               per_channel: int = 12) -> Dict[str, Any]:
        """完整五步，产出可直接注入的文本 + 目录层 + 预算账目。"""
        # ① 查询构造
        q = build_query(query, goal=goal, recent_turns=recent_turns,
                        entities=entities)

        # ② 多路召回
        channels = self.multi_recall(q, per_channel=per_channel, project=project)

        # ③ 融合
        if mode == "weighted":
            fused = self._weighted_fuse(channels)
        else:
            fused = rrf_fuse(channels)

        # ④ 重排
        ranked = self.rerank(fused)

        # ⑤ 预算分配
        quotas = {k: int(budget_tokens * v) for k, v in BUDGET_SHARES.items()}
        used = {k: 0 for k in quotas}
        out: Dict[str, Any] = {
            "query": q,
            "channels": {k: len(v) for k, v in channels.items()},
            "candidates": len(ranked),
            "budget_tokens": budget_tokens,
            "quota": quotas,
            "used": used,
            "mode": mode,
        }

        # 30% 常驻画像
        if include_profile:
            prof = self._profile_block(quotas["profile"])
            out["profile"] = prof
            used["profile"] = prof["tokens"]
        else:
            out["profile"] = {"text": "", "tokens": 0}

        # 分类候选：经验/负记忆 vs 任务相关 vs 未决
        # 先剔除画像里已经注入的条目 —— 同一条「用户偏好 pnpm」既在画像里
        # 又在「相关记忆」里出现，是纯粹的 token 浪费，还会让模型以为有两件事。
        _in_profile = set(out["profile"].get("contents") or [])
        exp_items, task_items, pending_items = [], [], []
        for item in ranked:
            rec = item["record"]
            _content = str(rec.get("content") or "").strip()
            if _content in _in_profile:
                continue
            tags = {str(t).lower() for t in (rec.get("tags") or [])}
            mtype = str(rec.get("type") or rec.get("mtype") or "").lower()
            if tags & {"playbook", "anti_pattern", "negative"} or mtype in ("lesson",):
                exp_items.append(item)
            elif tags & {"pending"} or "待确认" in _content:
                pending_items.append(item)
            else:
                task_items.append(item)

        out["task_related"] = self._pack(task_items, quotas["task_related"])
        out["experience"] = self._pack(exp_items, quotas["experience"])
        out["pending"] = self._pack(pending_items, quotas["pending"])
        for k in ("task_related", "experience", "pending"):
            used[k] = out[k]["tokens"]

        # 组装最终文本
        blocks = [out["profile"]["text"]]
        for key, title in (("task_related", "相关记忆"),
                           ("experience", "经验与禁忌"),
                           ("pending", "未决事项")):
            blk = out[key]
            if blk["items"]:
                blocks.append("## " + title + "\n" + "\n".join(
                    "- " + it["excerpt"] for it in blk["items"]))
        out["text"] = "\n\n".join(b for b in blocks if b)
        out["tokens"] = _estimate_tokens(out["text"])
        out["over_budget"] = out["tokens"] > budget_tokens
        return out

    def _profile_block(self, quota: int) -> Dict[str, Any]:
        """画像块：按配额决定注入多少段。

        配额不够时先砍情境与未决（它们可以走召回层），保住身份与偏好 ——
        这两段是「基础人格对齐」，缺了它回答风格就会跑偏。

        **未决事项不在这里渲染**：它有独立的 15% 配额与专门的 pending 块。
        如果两边都渲染，同一句「待确认」会在上下文里出现两次，既浪费 token
        又会让模型误以为是两件事。
        """
        try:
            from .profile import build_profile, render, token_estimate
            records = self.brain.store.all_records()
            prof = build_profile(records)
        except Exception:
            return {"text": "", "tokens": 0, "contents": []}

        contents = [it["content"] for tier in ("identity", "preference", "context")
                    for it in (prof.get(tier) or [])]

        full = render(prof, include_pending=False)
        if token_estimate(full) <= quota:
            return {"text": full, "tokens": token_estimate(full),
                    "contents": contents}
        slim = render(prof, include_context=False, include_pending=False)
        if token_estimate(slim) <= quota:
            return {"text": slim, "tokens": token_estimate(slim),
                    "contents": contents, "trimmed": True}
        ident = render({"identity": prof.get("identity") or []},
                       include_context=False, include_pending=False)
        return {"text": ident, "tokens": token_estimate(ident),
                "contents": [it["content"] for it in (prof.get("identity") or [])],
                "trimmed": True}

    def _pack(self, items: List[Dict[str, Any]], quota: int) -> Dict[str, Any]:
        """按配额装包，同时产出目录层。

        装不下的条目不丢，进 ``catalog`` —— 这正是渐进披露的意义：
        先让模型看到「有什么」，需要时再 expand。
        """
        picked: List[Dict[str, Any]] = []
        catalog: List[Dict[str, Any]] = []
        used = 0
        for it in items:
            rec = it["record"]
            content = str(rec.get("content") or "").strip()
            if not content:
                continue
            cost = _estimate_tokens(content)
            entry = {
                "id": rec.get("id"),
                "excerpt": content[:400],
                "tokens": cost,
                "score": it["score"],
                "low_confidence": it.get("low_confidence", False),
            }
            if used + cost <= quota:
                picked.append(entry)
                used += cost
            catalog.append({
                "id": rec.get("id"),
                "title": content[:40],
                "one_line": content[:100],
                "tokens": cost,
                "score": it["score"],
            })
        return {"items": picked, "catalog": catalog, "tokens": used,
                "dropped": max(0, len(catalog) - len(picked))}

    def _weighted_fuse(self, channels: Dict[str, Sequence[Any]]) -> List[Tuple[float, Dict[str, Any]]]:
        """保留 8.0.0 的加权融合作为可选模式（默认不用）。"""
        weights = {"vector": 0.55, "lexical": 0.12, "graph": 0.10, "temporal": 0.20}
        acc: Dict[str, float] = {}
        store: Dict[str, Dict[str, Any]] = {}
        for name, items in channels.items():
            w = weights.get(name, 0.1)
            if not items:
                continue
            top = max(float(x[0]) if isinstance(x, (list, tuple)) and x else 0.0
                      for x in items) or 1.0
            for item in items:
                rec = _record_of(item)
                if not rec:
                    continue
                raw = float(item[0]) if isinstance(item, (list, tuple)) and item else 0.0
                rid = str(rec.get("id") or id(rec))
                acc[rid] = acc.get(rid, 0.0) + w * (raw / top)
                store.setdefault(rid, rec)
        fused = [(s, store[r]) for r, s in acc.items()]
        fused.sort(key=lambda x: x[0], reverse=True)
        return fused
