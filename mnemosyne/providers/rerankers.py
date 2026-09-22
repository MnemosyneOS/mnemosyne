#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — Reranker provider adapters
=========================================

Reranking is the one retrieval component that is almost never worth running on
every query: it costs a round trip per candidate set and only pays off where the
first-pass ranking is systematically weak (paraphrase-heavy queries, long
memories, cross-language recall).  The engine therefore treats these adapters as
strictly opt-in, driven by ``search(rerank=True)`` or a configured
``memory.reranker`` block.

Five adapters mirror the component taxonomy: ``llm``, ``cohere``,
``zero_entropy``, ``huggingface`` and ``sentence_transformer``.  Four of them are
pure-stdlib HTTP; only the local sentence-transformer needs a package.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from .base import ProviderUnavailable, RerankerProvider, _first_env
from .json_utils import loads_lenient
from .transport import request_json

__all__ = ["LLMReranker", "CohereReranker", "ZeroEntropyReranker",
           "HuggingFaceReranker", "SentenceTransformerReranker",
           "get_reranker", "list_reranker_providers"]


def _normalise(results: List[Dict[str, Any]],
               key: str = "relevance_score") -> List[Dict[str, Any]]:
    """Sort descending by *key* and clamp scores into ``[0, 1]``.

    Different vendors report relevance on different scales (Cohere: 0–1,
    cross-encoders: unbounded logits, LLMs: 1–10).  The engine only ever
    compares scores *within one result set*, so a min–max rescale is enough to
    make the values comparable and keeps a fused-score step from being dominated
    by whichever reranker happens to produce the largest numbers.
    """
    rows = [r for r in results if isinstance(r, dict)]
    if not rows:
        return []
    vals = [float(r.get(key) or 0.0) for r in rows]
    lo, hi = min(vals), max(vals)
    span = hi - lo
    for r, v in zip(rows, vals):
        r["raw_score"] = v
        r["score"] = 1.0 if span == 0 else (v - lo) / span
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


class LLMReranker(RerankerProvider):
    """Rescore with a chat model, one batched call for the whole candidate set.

    Chosen when a deployment already has an LLM configured and wants reranking
    without adding another vendor.  The model is asked to return a JSON array of
    ``{"index", "score"}``; scores are requested on a fixed 0–10 scale so the
    normalisation step is stable.
    """

    name = "llm"

    PROMPT = (
        "You are a relevance judge for a memory-retrieval system.\n"
        "Given a QUERY and a numbered list of CANDIDATE memories, score how "
        "useful each candidate is for answering the query.\n"
        "Use 0 for irrelevant and 10 for directly answering it. Judge only "
        "semantic usefulness — ignore length and wording.\n"
        "Reply with JSON only: {\"scores\": [{\"index\": <int>, \"score\": <number>}, ...]}\n"
        "Include every candidate index exactly once."
    )

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        llm = self.config.get("llm")
        if llm is None:
            from .llms import get_llm

            llm = get_llm(self.config.get("llm_config") or {})
        self._llm = llm
        self.top_n = self.config.get("top_n")

    def rerank(self, query: str, documents: List[str],
               top_n: Optional[int] = None) -> List[Dict[str, Any]]:
        if not documents:
            return []
        listing = "\n".join(f"[{i}] {d[:1200]}" for i, d in enumerate(documents))
        user = f"QUERY: {query}\n\nCANDIDATES:\n{listing}"
        raw = self._llm.complete(
            [{"role": "system", "content": self.PROMPT},
             {"role": "user", "content": user}],
            temperature=0.0,
        )
        try:
            parsed = loads_lenient(raw)
        except ValueError:
            # A malformed judge response must not fail the whole search: fall
            # back to the incoming order with neutral scores.
            return [{"index": i, "score": 0.5} for i in range(len(documents))]
        rows = parsed.get("scores") if isinstance(parsed, dict) else parsed
        if not isinstance(rows, list):
            return [{"index": i, "score": 0.5} for i in range(len(documents))]
        out: List[Dict[str, Any]] = []
        seen = set()
        for r in rows:
            if not isinstance(r, dict):
                continue
            try:
                idx = int(r.get("index"))
            except (TypeError, ValueError):
                continue
            if idx < 0 or idx >= len(documents) or idx in seen:
                continue
            seen.add(idx)
            out.append({"index": idx, "relevance_score": float(r.get("score") or 0.0)})
        # Any candidate the judge skipped keeps a neutral score rather than
        # vanishing, so a rerank can only reorder results, never drop them.
        for i in range(len(documents)):
            if i not in seen:
                out.append({"index": i, "relevance_score": 5.0})
        ranked = _normalise(out)
        limit = top_n or self.top_n
        return ranked[:limit] if limit else ranked


class CohereReranker(RerankerProvider):
    """Cohere ``/v2/rerank``."""

    name = "cohere"
    default_model = "rerank-v3.5"
    env_keys = ("COHERE_API_KEY",)

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.api_key = (self.config.get("api_key")
                        or _first_env("MNEMOSYNE_RERANKER_COHERE_API_KEY",
                                      "COHERE_API_KEY"))
        if not self.api_key:
            raise ProviderUnavailable("cohere", "no API key configured",
                                      hint="Set COHERE_API_KEY.")
        self.base_url = (self.config.get("base_url")
                         or "https://api.cohere.com/v2").rstrip("/")
        self.model = self.config.get("model") or self.default_model
        self.timeout = float(self.config.get("timeout", 60))

    def rerank(self, query: str, documents: List[str],
               top_n: Optional[int] = None) -> List[Dict[str, Any]]:
        if not documents:
            return []
        payload = request_json(
            f"{self.base_url}/rerank",
            payload={"model": self.model, "query": query,
                     "documents": [d[:4000] for d in documents],
                     "top_n": top_n or self.config.get("top_n") or len(documents)},
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
        )
        rows = [{"index": r.get("index"), "relevance_score": r.get("relevance_score")}
                for r in ((payload or {}).get("results") or []) if isinstance(r, dict)]
        return _normalise(rows)


class ZeroEntropyReranker(RerankerProvider):
    """ZeroEntropy ``/v1/models/rerank``."""

    name = "zero_entropy"
    default_model = "zerank-1"
    env_keys = ("ZEROENTROPY_API_KEY", "ZERO_ENTROPY_API_KEY")

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.api_key = (self.config.get("api_key")
                        or _first_env("MNEMOSYNE_RERANKER_ZERO_ENTROPY_API_KEY",
                                      "ZEROENTROPY_API_KEY", "ZERO_ENTROPY_API_KEY"))
        if not self.api_key:
            raise ProviderUnavailable("zero_entropy", "no API key configured",
                                      hint="Set ZEROENTROPY_API_KEY.")
        self.base_url = (self.config.get("base_url")
                         or "https://api.zeroentropy.dev/v1").rstrip("/")
        self.model = self.config.get("model") or self.default_model
        self.timeout = float(self.config.get("timeout", 60))

    def rerank(self, query: str, documents: List[str],
               top_n: Optional[int] = None) -> List[Dict[str, Any]]:
        if not documents:
            return []
        payload = request_json(
            f"{self.base_url}/models/rerank",
            payload={"model": self.model, "query": query,
                     "documents": [d[:4000] for d in documents],
                     "top_n": top_n or len(documents)},
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
        )
        rows = [{"index": r.get("index"), "relevance_score": r.get("relevance_score")}
                for r in ((payload or {}).get("results") or []) if isinstance(r, dict)]
        return _normalise(rows)


class HuggingFaceReranker(RerankerProvider):
    """Hugging Face text-classification models used as cross-encoders.

    A cross-encoder receives ``query + document`` as a single input and returns
    a relevance logit, so candidates are scored one at a time.  That is slower
    than Cohere's batched API but works on the free inference tier.
    """

    name = "huggingface"
    default_model = "BAAI/bge-reranker-base"
    env_keys = ("HUGGINGFACE_API_KEY", "HF_TOKEN")

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.api_key = (self.config.get("api_key")
                        or _first_env("HUGGINGFACE_API_KEY", "HF_TOKEN"))
        if not self.api_key:
            raise ProviderUnavailable("huggingface", "no API token configured",
                                      hint="Set HF_TOKEN.")
        self.model = self.config.get("model") or self.default_model
        self.base_url = (self.config.get("base_url")
                         or "https://api-inference.huggingface.co/pipeline/text-classification").rstrip("/")
        self.timeout = float(self.config.get("timeout", 60))

    def rerank(self, query: str, documents: List[str],
               top_n: Optional[int] = None) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        headers = {"Authorization": f"Bearer {self.api_key}"}
        for i, doc in enumerate(documents):
            payload = request_json(
                f"{self.base_url}/{self.model}",
                payload={"inputs": {"text": query, "text_pair": doc[:4000]},
                         "options": {"wait_for_model": True}},
                headers=headers, timeout=self.timeout,
            )
            score = 0.0
            for item in (payload if isinstance(payload, list) else []):
                if isinstance(item, dict) and str(item.get("label", "")).lower() in (
                        "label_1", "1", "relevant", "positive"):
                    score = float(item.get("score") or 0.0)
                    break
            rows.append({"index": i, "relevance_score": score})
        ranked = _normalise(rows)
        limit = top_n or self.config.get("top_n")
        return ranked[:limit] if limit else ranked


class SentenceTransformerReranker(RerankerProvider):
    """Local ``sentence_transformers.CrossEncoder``.

    The only adapter that needs a package, and the only one that keeps every
    query and memory on the machine.  Model loading is deferred to the first
    :meth:`rerank` call so constructing the object stays cheap.
    """

    name = "sentence_transformer"
    requires = ("sentence-transformers",)
    default_model = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        try:
            import sentence_transformers  # noqa: F401
        except ImportError as e:
            raise ProviderUnavailable(
                "sentence_transformer", f"sentence-transformers not installed ({e})",
                hint="pip install mnemosyne-os[local-reranker]",
            ) from None
        self.model = self.config.get("model") or self.default_model
        self._model = None

    def _ensure(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(self.model)
        return self._model

    def rerank(self, query: str, documents: List[str],
               top_n: Optional[int] = None) -> List[Dict[str, Any]]:
        if not documents:
            return []
        model = self._ensure()
        raw = model.predict([(query, d[:4000]) for d in documents])
        rows = [{"index": i, "relevance_score": float(v)} for i, v in enumerate(raw)]
        ranked = _normalise(rows)
        limit = top_n or self.config.get("top_n")
        return ranked[:limit] if limit else ranked


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: Dict[str, type] = {
    "llm": LLMReranker,
    "cohere": CohereReranker,
    "zero_entropy": ZeroEntropyReranker,
    "huggingface": HuggingFaceReranker,
    "sentence_transformer": SentenceTransformerReranker,
}


def get_reranker(config: Optional[Dict[str, Any]] = None) -> Optional[RerankerProvider]:
    """Build the reranker described by *config*, or ``None`` when disabled.

    ``None`` is a valid and common answer: with no reranker block configured the
    engine's own multi-signal fusion is used, which costs nothing extra.
    """
    if not config:
        return None
    name = str(config.get("provider") or "").strip().lower()
    if name in ("", "none", "off", "disabled"):
        return None
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ProviderUnavailable(
            name, "unknown reranker provider",
            hint="Known providers: " + ", ".join(sorted(_REGISTRY)),
        )
    return cls(config.get("config") or config.get("params") or {})


def list_reranker_providers() -> List[str]:
    return sorted(_REGISTRY)
