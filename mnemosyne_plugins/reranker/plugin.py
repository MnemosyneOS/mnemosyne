"""Heuristic reranker plugin for Mnemosyne OS.

Re-ranks retrieval results using a transparent, zero-dependency
formula that combines three factors:

    score = confidence * freshness * access_freq

where:
  - **confidence**: the memory's own confidence value (0–1)
  - **freshness**: exponential decay based on age (newer = higher)
  - **access_freq**: logarithmic normalisation of ``access_count``

The reranker is designed to work on top of the existing 5-Way Fusion
retrieval results without requiring an external cross-encoder model.

Usage::

    brain = MemoryBrain(dir, plugins=["reranker"])
"""
import math
from datetime import datetime, timezone

from storage.plugin_sdk import RerankerPlugin

__all__ = ["HeuristicReranker", "register", "get_plugin_class"]


class HeuristicReranker(RerankerPlugin):
    """Heuristic reranker: ``score = confidence * freshness * access_freq``."""

    name = "reranker"
    description = "Heuristic reranker combining confidence, freshness, and access frequency"

    # Half-life in days for the freshness decay
    FRESHNESS_HALF_LIFE_DAYS = 30.0

    def __init__(self, brain=None):
        super().__init__(brain)

    @property
    def available(self):
        return True

    def _freshness(self, record):
        """Compute freshness as exponential decay from creation time.

        Returns a float in (0, 1].
        """
        created_at = record.get("created_at") or record.get("knowledge_time")
        if not created_at:
            return 1.0
        try:
            created_ts = datetime.fromisoformat(
                created_at.replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            return 1.0
        now_ts = datetime.now(timezone.utc).timestamp()
        age_days = max(0, (now_ts - created_ts) / 86400.0)
        # Exponential decay: 0.5 ^ (age / half_life)
        return 0.5 ** (age_days / self.FRESHNESS_HALF_LIFE_DAYS)

    def _access_freq(self, record):
        """Compute access frequency score in [0, 1].

        Uses log scaling: log(access_count + 1) / log(max_access + 1)
        where max_access is capped at 10 for normalisation.
        """
        access_count = record.get("access_count") or 0
        if access_count <= 0:
            return 0.5  # baseline for never-accessed
        # Capped log normalisation
        max_access = 10.0
        return min(math.log(access_count + 1) / math.log(max_access + 1), 1.0)

    def rerank(self, query, results, **kwargs):
        """Re-rank *results* in place.

        Parameters
        ----------
        query : str
            The original query (unused in this heuristic but available
            for custom implementations).
        results : list of (score, record, reasons)
            The output of :meth:`RetrievalEngine.retrieve`.

        Returns
        -------
        list of (new_score, record, reasons)
            The same tuples with updated scores, sorted by desc score.
        """
        if not results:
            return results

        scored = []
        for entry in results:
            if isinstance(entry, tuple) and len(entry) == 3:
                orig_score, rec, reasons = entry
            elif isinstance(entry, tuple) and len(entry) == 2:
                orig_score, rec = entry
                reasons = []
            else:
                rec = entry
                orig_score = 1.0
                reasons = []

            confidence = float(rec.get("confidence", 0.5))
            freshness = self._freshness(rec)
            access_freq = self._access_freq(rec)

            new_score = confidence * freshness * access_freq
            # Blend with original score (70% reranker, 30% original)
            blended = 0.7 * new_score + 0.3 * float(orig_score)

            if isinstance(reasons, list):
                reasons = list(reasons) + ["reranked:confidence*freshness*access_freq"]
            else:
                reasons = ["reranked:confidence*freshness*access_freq"]

            scored.append((blended, rec, reasons))

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored


def register(brain):
    """Plugin entry point — returns a HeuristicReranker instance."""
    return HeuristicReranker(brain)


def get_plugin_class():
    return HeuristicReranker
