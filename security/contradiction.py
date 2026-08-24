"""Standalone contradiction detection tool (Module 7.3 Innovation 3).

Detects contradictions in stored memories — records that share entities
but express conflicting facts.

Zero-dependency: uses only the Python standard library.

Usage (standalone)::

    python -m security.contradiction /path/to/brain "entity_name"

API::

    from security.contradiction import find_contradictions
    contradictions = find_contradictions(brain, entity="Apple", min_similarity=0.3)
"""
import sys
import os
import json
from datetime import datetime

__all__ = ["find_contradictions", "ContradictionDetector"]


# Keywords that signal opposing sentiment / factual direction
CONTRADICTION_KEYWORDS = {
    "成立": ["倒闭", "关闭", "解散", "不存在", "从未", "没有"],
    "倒闭": ["成立", "重开", "恢复", "复苏"],
    "喜欢": ["讨厌", "憎恶", "厌恶", "不喜欢"],
    "讨厌": ["喜欢", "爱", "喜好"],
    "成功": ["失败", "崩溃", "倒下"],
    "失败": ["成功", "胜利", "获胜"],
    "上涨": ["下跌", "暴跌", "下降"],
    "下跌": ["上涨", "飙升", "增长"],
    "对": ["错", "错误", "不对", "错误的"],
    "错": ["对", "正确", "准确"],
    "存在": ["不存在", "否定", "取消"],
    "美国加利福尼亚": ["中国", "北京"],
    "苹果": ["倒闭", "关闭", "失败"],
}


class ContradictionDetector:
    """Detect contradictions in memory records using semantic heuristics."""

    def __init__(self, brain):
        self.brain = brain
        self.records = []
        if brain is not None and hasattr(brain, "store"):
            self.records = [r for r in brain.store.all_records()
                            if not r.get("_corrupt")
                            and r.get("status", "active") in ("active", "working")]

    def _tokenize(self, text):
        """Simple tokenization using bigrams for CJK and whitespace for Latin."""
        if not text:
            return []
        # Use the brain's tokenizer if available, else fallback
        if self.brain is not None:
            try:
                from mnemosyne import _tokenize
                return set(_tokenize(text))
            except Exception:
                pass
        # Fallback: simple splitting
        tokens = set()
        # CJK bigrams
        cleaned = ''.join(c for c in text if c.isalnum() or '\u4e00' <= c <= '\u9fff')
        for i in range(len(cleaned) - 1):
            tokens.add(cleaned[i:i+2])
        # Also add words split by non-alnum for Latin
        for word in cleaned.split():
            tokens.add(word)
        return tokens

    def _cosine(self, vec_a, vec_b):
        """Cosine similarity between two token sets (Jaccard fallback)."""
        if not vec_a or not vec_b:
            return 0.0
        intersection = len(vec_a & vec_b)
        union = len(vec_a | vec_b)
        if union == 0:
            return 0.0
        return intersection / union

    def _extract_entities(self, record):
        """Extract entities from a record (from meta or content)."""
        entities = set(record.get("entities") or [])
        meta = record.get("meta", {})
        if isinstance(meta, dict):
            entities.update(meta.get("entities", []))
        return entities

    def _find_keyword_contradiction(self, content_a, content_b):
        """Check for explicit contradictory keyword pairs."""
        for kw, opposites in CONTRADICTION_KEYWORDS.items():
            if kw in content_a or kw in content_b:
                combined = content_a + " " + content_b
                for opp in opposites:
                    if opp in content_a and kw in content_b:
                        return True
                    if opp in content_b and kw in content_a:
                        return True
        return False

    def detect(self, entity=None, min_similarity=0.3):
        """Find contradictions in stored memories.

        Parameters
        ----------
        entity : str or None
            If set, restrict to records mentioning this entity.
        min_similarity : float
            Minimum token similarity for two records to be considered
            potentially related (and thus checked for contradiction).

        Returns
        -------
        list[dict] : Each dict: {record_a_id, record_b_id, entity,
        content_a, content_b, reason}
        """
        contradictions = []
        records = self.records

        # Filter by entity if specified
        if entity:
            entity_lower = entity.lower()
            filtered = []
            for r in records:
                content = r.get("content", "").lower()
                entities = self._extract_entities(r)
                ent_lower = {e.lower() for e in entities}
                if entity_lower in content or entity_lower in ent_lower:
                    filtered.append(r)
            records = filtered

        # Check all pairs for contradictions
        for i, rec_a in enumerate(records):
            tokens_a = self._tokenize(rec_a.get("content", ""))
            ents_a = self._extract_entities(rec_a)
            for j in range(i + 1, len(records)):
                rec_b = records[j]
                content_a = rec_a.get("content", "")
                content_b = rec_b.get("content", "")

                # Check entity overlap
                ents_b = self._extract_entities(rec_b)
                common_ents = ents_a & ents_b

                # If filtering by entity, both must match
                if entity and not common_ents:
                    # Still check if the entity appears in both contents
                    if entity.lower() not in content_a.lower() or entity.lower() not in content_b.lower():
                        continue

                tokens_b = self._tokenize(content_b)
                sim = self._cosine(tokens_a, tokens_b)

                if sim < min_similarity:
                    continue

                # Check for keyword contradiction
                is_contradiction = self._find_keyword_contradiction(content_a, content_b)

                # Also check: high similarity but contradictory keywords
                if not is_contradiction and sim >= 0.5 and common_ents:
                    # Check for contradictory sentiment
                    combined = content_a + " " + content_b
                    for kw, opposites in CONTRADICTION_KEYWORDS.items():
                        if kw in content_a:
                            for opp in opposites:
                                if opp in content_b:
                                    is_contradiction = True
                                    break
                        if kw in content_b:
                            for opp in opposites:
                                if opp in content_a:
                                    is_contradiction = True
                                    break
                        if is_contradiction:
                            break

                if is_contradiction:
                    contradictions.append({
                        "record_a_id": rec_a.get("id"),
                        "record_b_id": rec_b.get("id"),
                        "entity": entity or (list(common_ents)[0] if common_ents else "unknown"),
                        "content_a": content_a[:200],
                        "content_b": content_b[:200],
                        "similarity": round(sim, 4),
                        "reason": "contradictory_keywords",
                    })

        return contradictions


def find_contradictions(brain, entity=None, min_similarity=0.3):
    """Find contradictions in the brain's memory store.

    Parameters
    ----------
    brain : MemoryBrain or compatible
        The brain instance whose records to check.
    entity : str or None
        If set, restrict to records mentioning this entity.
    min_similarity : float
        Minimum similarity threshold.

    Returns
    -------
    list[dict] : Contradiction records (see ContradictionDetector.detect).
    """
    detector = ContradictionDetector(brain)
    return detector.detect(entity=entity, min_similarity=min_similarity)


def _cli():
    """CLI entry point: python -m security.contradiction <brain_dir> [entity]"""
    if len(sys.argv) < 2:
        print("Usage: python -m security.contradiction <brain_dir> [entity] [--min-similarity 0.5]")
        sys.exit(1)
    if sys.argv[1].startswith("-"):
        # 防御：目录参数不允许是选项（如 --help），避免把选项名当目录写入磁盘
        print("Usage: python -m security.contradiction <brain_dir> [entity] [--min-similarity 0.5]")
        sys.exit(0 if sys.argv[1] in ("-h", "--help") else 2)
    brain_dir = sys.argv[1]
    entity = sys.argv[2] if len(sys.argv) > 2 else None
    min_sim = 0.3
    if "--min-similarity" in sys.argv:
        idx = sys.argv.index("--min-similarity")
        if idx + 1 < len(sys.argv):
            min_sim = float(sys.argv[idx + 1])

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from mnemosyne import MemoryBrain
    brain = MemoryBrain(brain_dir, enable_embeddings=False, enable_stats=False)
    brain.ensure_init()

    results = find_contradictions(brain, entity=entity, min_similarity=min_sim)
    if results:
        print(f"🔍 Found {len(results)} contradictions:")
        for c in results:
            print(f"  - [{c['entity']}] {c['content_a'][:80]}")
            print(f"    vs. {c['content_b'][:80]}")
            print(f"    Similarity: {c['similarity']}, Reason: {c['reason']}")
            print()
    else:
        print("✅ No contradictions found.")
    brain.close()


if __name__ == "__main__":
    _cli()
