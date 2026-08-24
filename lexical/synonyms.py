"""Built-in synonym dictionary for zero-dependency query expansion.

Maps canonical terms to their synonyms, enabling the core retrieval
pipeline to recognize paraphrases (e.g. "成立" ↔ "创立") without any
third-party libraries.

Usage::

    from lexical.synonyms import expand_query

    expanded_terms = expand_query("成立")
    # -> ['成立', '创建', '建立', '创立']

NOTE: canonical form is the FIRST key in each group. Variants are the
remaining words. Every term appears as a key only ONCE (no bidirectional
entries), so the canonical mapping is deterministic and safe.
"""
import os

__all__ = ["SYNONYMS", "expand_query", "expand_token"]

# — Synonym groups (canonical form first, single-direction) —
SYNONYMS: dict[str, list[str]] = {
    # Establishment / founding
    "成立": ["创建", "建立", "创立", "开创", "发起"],
    "倒闭": ["破产", "关闭", "解散", "停止", "歇业"],
    # Weather
    "天气": ["气候", "气温", "温度"],
    # Product / company
    "产品": ["商品", "货物", "产物"],
    "公司": ["企业", "厂商", "机构", "组织"],
    # Sentiment
    "很好": ["不错", "非常好", "棒", "优秀", "出色"],
    "喜欢": ["爱", "喜好", "青睐", "中意", "喜爱"],
    "讨厌": ["憎恶", "厌恶", "不喜欢", "反感"],
    # Common verbs
    "查找": ["搜索", "寻找", "检索", "找回"],
    "介绍": ["讲述", "概述", "说明"],
    "描述": ["描绘", "形容"],
    "研究": ["探索", "调查"],
    "发布": ["推出", "上市", "推广"],
    "有": ["拥有", "具有", "持有"],
    "发现": ["查出", "找到"],
    # Common nouns
    "信息": ["消息", "讯息", "资讯"],
    "新闻": ["消息", "资讯", "报道"],
    "股价": ["股票价格", "价格"],
    "行星": ["星球", "天体"],
    "八大": ["八颗", "八个"],
    "来源": ["来自", "起源"],
    "迁移": ["移动", "迁徙"],
    "感染": ["传染", "传播"],
    # Common
    "什么": ["啥", "何", "哪些"],
    "怎么": ["如何", "怎样"],
    # Structural
    "首都": ["京城", "首府"],
    "的": [""],  # removing "的"
}


def expand_query(query: str, max_depth: int = 2) -> list[str]:
    """Expand a query into a list of synonymous variants.

    Each token in the query is replaced with its synonyms, generating
    alternative query strings.  The original query is always first.
    """
    # Simple tokenization: split by space and CJK bigrams
    expanded = [query]
    for canonical, syns in SYNONYMS.items():
        if canonical in query:
            for syn in syns:
                if syn not in expanded and syn != canonical:
                    if syn:  # skip empty synonyms (like "" for "的")
                        expanded.append(query.replace(canonical, syn))
    return expanded


def expand_token(token: str) -> set[str]:
    """Expand a single token into a set including its synonyms."""
    if not token:
        return {token}
    result: set[str] = {token}
    if token in SYNONYMS:
        result.update(SYNONYMS[token])
    return result
