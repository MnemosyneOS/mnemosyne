import os
import re
import dataclasses

from .utils import (_extract_entities, _extract_entity_names, _now_iso, _stable_id, _tokenize, _tokenize_preprocess, inject_time_expressions, _unique_salt,)
INDEX_NAME = "index.jsonl"
GRAPH_NAME = "graph.jsonl"
META_NAME = "meta.json"
EMBEDDING_DIM = 128
PROJ_BUCKETS = 2048
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".mnemosyne")

def _build_record(content, mtype="semantic", layer=None, tags=None, source=None,
                  importance=None, expires_at=None, context="", meta=None,
                  # v2.0 新增Field
                  fact_type=None, confidence=None, source_type=None,
                  verification=None, event_time=None, knowledge_time=None,
                  embedding=None, graph_edges=None, parent_id=None,
                  topic_tag=None, version=2,
                  # v2.1 会话归属 + 工具调用归属
                  session_id=None, tool_name=None,
                  # v7.0.0 性能：快速路径跳过 entities_detailed 抽取
                  skip_detailed=False):
    """Build一条完整记忆Record。

    v2.0 新增Field（向后兼容：v1.x reads 时自动填充默认Value）：
      - fact_type: fact/opinion/belief/observation/inference/hypothesis
      - confidence: 0-1 可信度
      - source_type: user/system/inference/web_search/file/agent_generated
      - verification: unverified/verified/contradicted/outdated/superseded
      - event_time: Event实际发生Time（ISO）
      - knowledge_time: Agent获知此信息的Time（ISO）
      - embedding: 预computes 向量（可选，惰性computes ）
      - graph_edges: 实体关系边列Table
      - parent_id: 上级记忆ID（用于consolidation）
    """
    layer = layer or _default_layer(mtype)
    # 自动推断 fact_type
    if fact_type is None:
        fact_type = _infer_fact_type(mtype, content)
    # 自动推断 source_type
    if source_type is None:
        source_type = _infer_source_type(mtype, source)
    # 自动computes 重要性
    if importance is None:
        importance = _auto_importance(content, mtype, tags)

    record = {
        "id": _stable_id(content, _unique_salt()),
        "content": content.strip() if content else "",
        "type": mtype,
        "layer": layer,
        "tags": tags or [],
        "entities": _extract_entity_names(content) if content else [],
        "entities_detailed": ([] if skip_detailed
                              else _extract_entities(content)) if content else [],
        "source": source,
        "source_type": source_type,
        "importance": int(importance),
        "context": context,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "last_accessed_at": None,
        "access_count": 0,
        "expires_at": expires_at,
        "status": "active",
        "meta": meta or {},
        # ---- v2.0 新增 ----
        "version": version,
        "fact_type": fact_type,
        "confidence": confidence if confidence is not None else _default_confidence(source_type),
        "verification": verification or "unverified",
        "event_time": event_time,
        "knowledge_time": knowledge_time or _now_iso(),
        "embedding": embedding,
        "graph_edges": graph_edges or [],
        "parent_id": parent_id,
        "topic_tag": topic_tag,
        "consolidated_from": [],
        "consolidated_at": None,
        # ---- v2.1 会话归属 + 工具调用归属 ----
        "session_id": session_id,
        "tool_name": tool_name,
    }
    return record


def _default_layer(mtype):
    return {
        "semantic": "semantic", "episodic": "episodic",
        "procedural": "procedural", "reflective": "reflective",
        "web": "semantic", "preference": "reflective",
        "todo": "working", "identity": "semantic",
        "belief": "reflective", "observation": "semantic",
        "lesson": "procedural", "strategy": "reflective",
    }.get(mtype, "semantic")


def _infer_fact_type(mtype, content):
    """based on Type和内容推断事实Type。"""
    mapping = {
        "preference": "opinion", "belief": "belief",
        "observation": "observation", "lesson": "inference",
        "strategy": "inference", "reflective": "inference",
    }
    if mtype in mapping:
        return mapping[mtype]
    # 启发式：含主观词 → opinion
    opinion_markers = ["偏好", "喜欢", "讨厌", "认为", "觉得", "建议", "推荐", "应该", "最好"]
    if any(w in (content or "") for w in opinion_markers):
        return "opinion"
    return "fact"


def _infer_source_type(mtype, source):
    if source and isinstance(source, dict):
        kind = source.get("kind", "")
        if kind == "web_search":
            return "web_search"
    if mtype == "web":
        return "web_search"
    if mtype in ("procedural", "lesson"):
        return "inference"
    if mtype == "reflective":
        return "agent_generated"
    return "user"


def _default_confidence(source_type):
    """不同来源的默认可信度。"""
    return {
        "user": 0.95, "file": 0.85, "web_search": 0.65,
        "system": 0.90, "inference": 0.50, "agent_generated": 0.45,
        "external": 0.60,
    }.get(source_type, 0.70)


def _auto_importance(content, mtype, tags):
    """自动重要性评分（规则式）。"""
    score = 3  # 默认
    # Type加权
    type_weights = {
        "identity": 5, "preference": 4, "procedural": 4,
        "lesson": 4, "strategy": 4, "reflective": 3,
        "episodic": 3, "semantic": 2, "web": 2, "todo": 2,
        "belief": 3, "observation": 3,
    }
    score = type_weights.get(mtype, 3)
    # 内容信号
    content = content or ""
    if any(kw in content for kw in ["密码", "密钥", "token", "secret"]):
        score = max(score, 5)
    if any(kw in content for kw in ["关Key", "重要", "必须", "核心", "决策"]):
        score = min(score + 1, 5)
    if tags and any(t in (tags or []) for t in ["关Key", "重要", "core"]):
        score = min(score + 1, 5)
    return score


def _extract_event_time(content, record_created_at):
    """从内容中提取EventTime（启发式）。"""
    # Date模式：YYYY-MM-DD 或 YYYY年MM月DD日
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日]?", content or "")
    if m:
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return f"{y:04d}-{mo:02d}-{d:02d}T00:00:00"
        except ValueError:
            pass
    return record_created_at


# ============================================================================
# 报告数据类（v7.0.0 门面层返回值）
# ============================================================================


@dataclasses.dataclass
class ConsolidationReport:
    """记忆合并报告。

    dry_run=True 时 merges_executed 恒为 0，仅统计 merges_planned。
    """
    dry_run: bool = False
    merges_planned: int = 0
    merges_executed: int = 0
    consolidated: int = 0
    groups: list = dataclasses.field(default_factory=list)

    def to_dict(self):
        return {
            "dry_run": self.dry_run,
            "merges_planned": self.merges_planned,
            "merges_executed": self.merges_executed,
            "consolidated": self.consolidated,
            "groups": self.groups,
        }


@dataclasses.dataclass
class DemoteReport:
    """降级（遗忘经济学）报告。"""
    migrations_count: int = 0
    demoted: list = dataclasses.field(default_factory=list)
    cold_migrated: int = 0
    budget_bytes: int = 0
    current_bytes: int = 0

    def to_dict(self):
        return {
            "migrations_count": self.migrations_count,
            "demoted": self.demoted,
            "cold_migrated": self.cold_migrated,
            "budget_bytes": self.budget_bytes,
            "current_bytes": self.current_bytes,
        }


