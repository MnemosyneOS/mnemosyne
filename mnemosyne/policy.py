# -*- coding: utf-8 -*-
"""记忆策略层（v8.1.0）
========================

解决 8.0.0 的结构性缺口：**没有敏感度分级，也没有来源可信度闸门**。
8.0.0 只做了字段级脱敏（``_redact_sensitive_fields``）与可选 crypto 插件，
但"这条记忆该不该入库、该不该被召回、能不能写成用户偏好"三件事没有统一策略，
导致外部网页内容与用户亲口陈述以同等地位进入长期记忆。

本模块提供三件事：

1. ``classify_sensitivity`` —— 三级敏感度判定（normal / private / secret）
2. ``SOURCE_TRUST`` —— 来源可信度分级（防记忆投毒的第一道闸门）
3. ``admit`` —— 写入准入判定，返回 (是否入库, 等级, 原因列表)

设计原则：**默认宽松，显式收紧**。没有命中任何规则时一律 ``normal`` 且允许入库，
因此升级到 8.1.0 不会改变既有写入行为（向后兼容）。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "SENSITIVITY_LEVELS",
    "SOURCE_TRUST",
    "classify_sensitivity",
    "source_trust",
    "can_write_longterm",
    "admit",
    "recall_allowed",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 敏感度三级。语义：
#: - normal  ：可正常入库、正常召回、可被摘要引用
#: - private ：可入库，但默认不注入外部上下文、不进摘要正文（只留指针）
#: - secret  ：**不入库**（返回拒绝），已存在的存量数据召回时一律过滤
SENSITIVITY_LEVELS = ("normal", "private", "secret")

#: 来源可信度。数值同时用于写入准入与召回排序降权。
#: 顺序即优先级：user_explicit > tool_result > user_implicit > external
SOURCE_TRUST = {
    "user": 0.95,           # 用户亲口陈述
    "system": 0.90,         # 系统/工具返回的结构化结果
    "file": 0.85,           # 本地文件
    "agent_generated": 0.55,  # Agent 自己归纳的（可能是推断）
    "inference": 0.50,      # 模型推断
    "external": 0.40,       # 外部网页/邮件/第三方内容 —— 不可信输入
}

#: 只有这些来源允许写入「用户偏好 / 身份」类长期记忆。
#: 依据：记忆投毒（OWASP ASI06）的典型路径就是把外部内容伪装成用户偏好。
_LONGTERM_TRUSTED_SOURCES = frozenset({"user", "system", "file"})

# ---------------------------------------------------------------------------
# 敏感度规则
# ---------------------------------------------------------------------------

#: (正则, 等级, 说明)。命中 secret 直接拒写；命中 private 允许入库但限制外泄。
_SECRET_PATTERNS = [
    (re.compile(r"\b\d{17}[\dXx]\b"), "secret", "身份证号"),
    (re.compile(r"\b(?:4\d{12}(?:\d{3})?|5[1-5]\d{14}|62\d{14,17})\b"), "secret", "银行卡号"),
    (re.compile(r"\b(?:sk|pk|ghp|gho|glpat|xox[baprs])[-_][A-Za-z0-9_\-]{16,}\b"), "secret", "API 密钥"),
    (re.compile(r"(?i)\b(?:password|passwd|pwd)\s*[:=]\s*\S+"), "secret", "明文密码"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-_\.]{20,}"), "secret", "Bearer Token"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), "secret", "私钥"),
]

_PRIVATE_PATTERNS = [
    (re.compile(r"(?i)\b(?:我的|我家|我老婆|我先生|我孩子|家人)\b"), "private", "家庭信息"),
    (re.compile(r"(?i)(?:住址|家庭住址|门牌|小区|楼栋)"), "private", "住址"),
    (re.compile(r"(?i)(?:手机号|电话|微信|身份证|护照号)"), "private", "联系方式/证件"),
    (re.compile(r"(?i)(?:病历|诊断|用药|抑郁症|焦虑症|体检报告)"), "private", "健康信息"),
    (re.compile(r"(?i)(?:薪资|工资|年薪|月薪|收入|存款|贷款|负债)"), "private", "财务信息"),
    (re.compile(r"(?i)(?:私密|只告诉你|别告诉|不要外传|保密)"), "private", "用户显式保密标记"),
]


def classify_sensitivity(content: str,
                         source_type: Optional[str] = None,
                         mtype: Optional[str] = None) -> Tuple[str, List[str]]:
    """判定一条内容的敏感度等级。

    返回 ``(level, reasons)``。``level`` 取 ``normal`` / ``private`` / ``secret``；
    多个规则命中时取最高等级（secret > private > normal）。
    """
    text = content or ""
    if not text:
        return "normal", []

    for pattern, level, reason in _SECRET_PATTERNS:
        if pattern.search(text):
            return level, [reason]

    reasons: List[str] = []
    for pattern, level, reason in _PRIVATE_PATTERNS:
        if pattern.search(text):
            reasons.append(reason)
    if reasons:
        return "private", reasons

    return "normal", []


# ---------------------------------------------------------------------------
# 来源与准入
# ---------------------------------------------------------------------------

def source_trust(source_type: Optional[str]) -> float:
    """来源可信度（0-1）。未知来源按 external 处理，宁可低估。"""
    return SOURCE_TRUST.get((source_type or "").strip().lower(), SOURCE_TRUST["external"])


def can_write_longterm(source_type: Optional[str], mtype: Optional[str]) -> bool:
    """该来源能否写入长期偏好/身份类记忆。

    这是防投毒的核心闸门：外部网页里的一句话不能被沉淀成「用户偏好 X」。
    非偏好类（episodic 等）不受此限制，因为它们有时间戳且不作为行为依据。
    """
    if mtype not in ("preference", "identity"):
        return True
    return (source_type or "").strip().lower() in _LONGTERM_TRUSTED_SOURCES


def admit(content: str,
          source_type: Optional[str] = None,
          mtype: Optional[str] = None,
          allow_secret: bool = False) -> Dict[str, Any]:
    """写入准入判定。

    返回::

        {
          "admit": bool,          # 是否允许入库
          "sensitivity": str,     # normal/private/secret
          "trust": float,         # 来源可信度
          "reasons": [str],       # 拒绝或降级原因
          "downgrade": bool,      # 是否应降低初始置信度
        }

    规则：
    - ``secret`` 默认拒写（``allow_secret=True`` 可强制，仅供用户显式要求时使用）
    - 外部来源写偏好/身份 → 拒绝，并建议改存为 episodic（带时间戳，不作行为依据）
    - 低可信来源 → 允许写入但要求降权（``downgrade=True``）
    """
    level, reasons = classify_sensitivity(content, source_type, mtype)
    trust = source_trust(source_type)
    result = {
        "admit": True,
        "sensitivity": level,
        "trust": trust,
        "reasons": list(reasons),
        "downgrade": False,
    }

    if level == "secret" and not allow_secret:
        result["admit"] = False
        result["reasons"].append("命中密钥/证件类模式，默认拒绝入库（可显式 allow_secret 覆盖）")
        return result

    if not can_write_longterm(source_type, mtype):
        result["admit"] = False
        result["reasons"].append(
            "来源 %s 不可信，禁止写入 %s 类长期记忆；建议改存 episodic" % (source_type, mtype)
        )
        return result

    if trust < 0.6:
        result["downgrade"] = True
        result["reasons"].append("来源可信度 %.2f 偏低，写入时应降低初始置信度" % trust)

    return result


def recall_allowed(record: Dict[str, Any],
                   context_level: str = "normal") -> bool:
    """召回过滤：``secret`` 级记忆永不进入上下文；``private`` 仅在私密上下文可召回。"""
    level = (record.get("sensitivity") or "normal").lower()
    if level == "secret":
        return False
    if level == "private" and context_level == "public":
        return False
    return True


# ---------------------------------------------------------------------------
# 门面类
# ---------------------------------------------------------------------------

class MemoryPolicy:
    """记忆策略门面：写入准入、召回过滤、存量审计。"""

    LEVELS = SENSITIVITY_LEVELS
    TRUST = SOURCE_TRUST

    @staticmethod
    def classify(content: str, source_type: Optional[str] = None,
                 mtype: Optional[str] = None) -> Dict[str, Any]:
        level, reasons = classify_sensitivity(content, source_type, mtype)
        return {"sensitivity": level, "reasons": reasons,
                "trust": source_trust(source_type),
                "longterm_allowed": can_write_longterm(source_type, mtype)}

    @staticmethod
    def check(content: str, source_type: Optional[str] = None,
              mtype: Optional[str] = None,
              allow_secret: bool = False) -> Dict[str, Any]:
        return admit(content, source_type, mtype, allow_secret)

    @staticmethod
    def filter_recall(records: List[Dict[str, Any]],
                      context_level: str = "normal") -> List[Dict[str, Any]]:
        return [r for r in records if recall_allowed(r, context_level)]

    @staticmethod
    def audit(brain: Any, limit: int = 2000) -> Dict[str, Any]:
        """存量审计：统计敏感度分布，并标出**以 secret 级内容入库**的条目。

        8.1.0 之前写入的数据没有 ``sensitivity`` 字段，这里按内容重新判定，
        让用户能看到「库里有多少条其实不该明文躺着」。
        """
        store = getattr(brain, "store", None)
        if store is None:
            return {"error": "no store"}
        records = [r for r in store.all_records()
                   if not r.get("_corrupt") and r.get("status") != "deleted"][:limit]
        dist = {"normal": 0, "private": 0, "secret": 0}
        flagged = []
        for r in records:
            lvl = (r.get("sensitivity")
                   or classify_sensitivity(r.get("content") or "",
                                           r.get("source_type"), r.get("type"))[0])
            dist[lvl] = dist.get(lvl, 0) + 1
            if lvl == "secret":
                flagged.append({"memory_id": r.get("id"),
                                "type": r.get("type"),
                                "preview": (r.get("content") or "")[:60]})
        return {
            "scanned": len(records),
            "distribution": dist,
            "secret_entries": flagged[:20],
            "secret_count": len(flagged),
            "recommendation": ("存在 %d 条疑似密钥/证件类记忆，建议 forget(evict=true) 清理"
                               % len(flagged)) if flagged else "未发现高危明文记忆",
        }
