import collections
import json
import math
import os
from datetime import datetime

from .models import (_infer_fact_type, _infer_source_type,)
from .utils import (_extract_entity_names, _extract_relationships, _now_iso,)
INDEX_NAME = "index.jsonl"
GRAPH_NAME = "graph.jsonl"
META_NAME = "meta.json"
EMBEDDING_DIM = 128
PROJ_BUCKETS = 2048
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".mnemosyne")


def _upgrade_record(rec):
    """将 v1.x 记录升级到 v2.0 格式。"""
    if rec.get("_corrupt"):
        return rec
    # 确保 v2.0 Field存在
    defaults = {
        "version": rec.get("version", 1),
        "fact_type": rec.get("fact_type") or _infer_fact_type(rec.get("type", ""), rec.get("content", "")),
        "confidence": rec.get("confidence", 0.7),
        "source_type": rec.get("source_type") or _infer_source_type(rec.get("type", ""), rec.get("source")),
        "verification": rec.get("verification", "unverified"),
        "event_time": rec.get("event_time") or rec.get("created_at", ""),
        "knowledge_time": rec.get("knowledge_time") or rec.get("created_at", ""),
        "graph_edges": rec.get("graph_edges", []),
        "parent_id": rec.get("parent_id"),
        "consolidated_from": rec.get("consolidated_from", []),
        "consolidated_at": rec.get("consolidated_at"),
        "entities_detailed": rec.get("entities_detailed", []),
    }
    for k, v in defaults.items():
        if k not in rec:
            rec[k] = v
    return rec


class MemoryGraphStore:
    """记忆知识图谱（实体-关系边存储）。

    与主 JSONL 并存，存储在 graph.jsonl。
    支持实体查询、关系遍历、多跳扩展。
    """

    def __init__(self, base_dir=DEFAULT_DIR):
        self.base_dir = base_dir
        self.graph_path = os.path.join(base_dir, GRAPH_NAME)

    def ensure_init(self):
        if not os.path.exists(self.graph_path):
            open(self.graph_path, "w", encoding="utf-8").close()

    @property
    def exists(self):
        return os.path.exists(self.graph_path)

    def add_edges(self, edges, memory_id=None):
        """添加边：`[(from, to, relation, strength, memory_id), ...]` 或 dict 列表。

        v7.0.2（D9）修复：**dict 型边过去完全忽略 `memory_id` 形参**。
        旧实现对 dict 直接 `e = edge` 后原样落盘，而 `_extract_relationships()`
        产出的正是 dict 且其 `memory_id` 恒为 `None`（抽取器不知道记录 id）——
        于是正常写入路径产生的**每一条边**在 graph.jsonl 里都是
        `"memory_id": null`（实测确认），只有回填路径显式赋值过。
        后果：任何"按记录查它连了哪些节点"的能力都拿不到数据，
        图通道只能退化成比对 `entities` 中文滑窗碎片（见 retrieval 的 D8 说明）。
        修法：dict 分支也补上 `memory_id`（不原地改调用方的 dict）。
        """
        self.ensure_init()
        with open(self.graph_path, "a", encoding="utf-8") as f:
            for edge in edges:
                if isinstance(edge, dict):
                    e = dict(edge)  # 复制，不污染调用方对象
                    if not e.get("memory_id"):
                        e["memory_id"] = memory_id
                else:
                    e = {
                        "from": edge[0], "to": edge[1],
                        "relation": edge[2] if len(edge) > 2 else "related_to",
                        "strength": edge[3] if len(edge) > 3 else 1.0,
                        "memory_id": edge[4] if len(edge) > 4 else memory_id,
                    }
                e["created_at"] = _now_iso()
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def iter_edges(self):
        if not self.exists:
            return
        with open(self.graph_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def purge_edges_without_memory_id(self):
        """删掉 `memory_id` 为空的边，返回删除条数（v7.0.2 D9 修存量）。

        【为什么必须删而不是留着】`edges` 有唯一索引
        `(from_entity, to_entity, relation, qualifier)`，且**不含 memory_id**。
        7.0.1 及以前写入的边 `memory_id` 全是 null；若不先清掉，回填时重新写入
        的"带正确 memory_id 的同一条边"会被 `INSERT OR IGNORE` 直接忽略 ——
        等于脏数据把正确数据挡在门外，且**不报错**。
        边是**派生索引**（权威数据在 memories 里，随时可由正文重算），
        所以删除是安全的：紧接着的回填会把它们按当前抽取规则重建。
        """
        if not self.exists:
            return 0
        kept, dropped = [], 0
        with open(self.graph_path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    e = json.loads(s)
                except json.JSONDecodeError:
                    dropped += 1
                    continue
                if not e.get("memory_id"):
                    dropped += 1
                    continue
                kept.append(s)
        if dropped:
            tmp = self.graph_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for s in kept:
                    f.write(s + "\n")
            os.replace(tmp, self.graph_path)
        return dropped

    def all_edges(self):
        return list(self.iter_edges())

    def get_neighbors(self, entity, max_depth=1):
        """获取指定实体的邻居节点（支持多跳）。"""
        edges = self.all_edges()
        result = {
            "entity": entity,
            "depth_0": [entity],
        }
        current = {entity}
        for depth in range(1, max_depth + 1):
            neighbors = set()
            for e in edges:
                frm, to = e.get("from", ""), e.get("to", "")
                if frm in current and to not in current and to not in neighbors:
                    neighbors.add(to)
                if to in current and frm not in current and frm not in neighbors:
                    neighbors.add(frm)
            result[f"depth_{depth}"] = sorted(neighbors)
            current = current | neighbors
        return result

    def search_path(self, from_entity, to_entity, max_depth=3):
        """BFS 查找两实体间的最短路径。"""
        if from_entity == to_entity:
            return [from_entity]
        edges = self.all_edges()
        # Build邻接Table
        adj = collections.defaultdict(set)
        for e in edges:
            frm, to = e.get("from", ""), e.get("to", "")
            adj[frm].add(to)
            adj[to].add(frm)
        # BFS
        queue = collections.deque([(from_entity, [from_entity])])
        visited = {from_entity}
        while queue:
            node, path = queue.popleft()
            if len(path) > max_depth:
                continue
            for nb in adj.get(node, set()):
                if nb == to_entity:
                    return path + [to_entity]
                if nb not in visited:
                    visited.add(nb)
                    queue.append((nb, path + [nb]))
        return None

    def get_entity_graph(self, entities, depth=2):
        """获取多实体的子图（用于检索增强）。"""
        result = {}
        for ent in entities:
            result[ent] = self.get_neighbors(ent, max_depth=depth)
        return result

    def close(self):
        """清理资源（图存储为纯文件 IO，无需要关闭的连接；保留接口兼容）。"""
        return None


# ============================================================================
# Part 6: Retrieval Layer（5-Way Fusion：BM25 + 向量 + 图 + Time + 可信度）
# ============================================================================

def _idf(doc_tf_list):
    n = len(doc_tf_list)
    df = collections.Counter()
    for tf in doc_tf_list:
        for tok in set(tf):
            df[tok] += 1
    return {tok: math.log((n + 1) / (freq + 0.5) + 1) for tok, freq in df.items()}


def _bm25_score(query_tf, doc_tf, idf, avg_len, k1=1.5, b=0.75):
    doc_len = sum(doc_tf.values())
    score = 0.0
    for tok, qf in query_tf.items():
        if tok not in doc_tf:
            continue
        tf = doc_tf[tok]
        denom = tf + k1 * (1 - b + b * doc_len / max(avg_len, 1))
        score += idf.get(tok, 0) * qf * (tf * (k1 + 1)) / denom
    return score


def _cosine(vec_a, vec_b):
    dot = sum(vec_a.get(t, 0) * vec_b.get(t, 0) for t in set(vec_a) | set(vec_b))
    na = math.sqrt(sum(v * v for v in vec_a.values()))
    nb = math.sqrt(sum(v * v for v in vec_b.values()))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _temporal_score(created_iso, now_ts, half_life_days=90):
    """计算时间衰减分数。"""
    try:
        created_ts = datetime.fromisoformat(created_iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        created_ts = now_ts
    age_days = max(0, (now_ts - created_ts) / 86400.0)
    return math.exp(-age_days * math.log(2) / half_life_days)


def _confidence_weight(record):
    """based on 可信度调整权重。"""
    conf = record.get("confidence", 0.7)
    verify = record.get("verification", "unverified")
    multiplier = 1.0
    if verify == "verified":
        multiplier = 1.2
    elif verify == "contradicted":
        multiplier = 0.5
    elif verify == "outdated":
        multiplier = 0.6
    elif verify == "superseded":
        multiplier = 0.3
    return conf * multiplier


# ============================================================================
# 5-Way Fusion检Index擎
# ============================================================================

