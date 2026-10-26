#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — Vector store adapters
====================================

The engine's *own* storage is the built-in one: vectors live in the same SQLite
file as the memories, so there is no second system to run, no second backup to
take, and no consistency problem between "the memory" and "its vector".  That is
what :class:`BuiltinVectorStore` exposes, and it is why ``vector_store: builtin``
(the default) needs no configuration at all.

External stores are supported for deployments that already run one, and are
grouped by how they are reached:

``http``
    A plain REST API reaching the store over HTTP — implemented here with the
    standard library only.  Qdrant, Pinecone, Elasticsearch/OpenSearch, Weaviate,
    Upstash and Turbopuffer all fall in this bucket.

``sdk``
    The store's Python SDK is mandatory (it speaks a binary protocol or a
    driver-specific wire format).  These adapters load the SDK lazily and are
    registered as *unavailable* rather than absent, so ``list_providers()``
    tells the truth and an error message names the exact extra to install.

``generic``
    :class:`GenericRESTVectorStore` — a declarative adapter so a store with no
    dedicated class can still be used by describing its three endpoints in
    config, instead of writing code.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from typing import Any, Dict, Iterable, List, Optional

from .base import (ProviderUnavailable, VectorStoreProvider, _first_env)
from .transport import HttpError, request_json

__all__ = ["BuiltinVectorStore", "InMemoryVectorStore", "QdrantVectorStore",
           "PineconeVectorStore", "ElasticsearchVectorStore", "WeaviateVectorStore",
           "UpstashVectorStore", "TurbopufferVectorStore", "SDKVectorStore",
           "GenericRESTVectorStore", "get_vector_store", "list_vector_store_providers"]


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / math.sqrt(na * nb)


# ---------------------------------------------------------------------------
# Built-in
# ---------------------------------------------------------------------------

class BuiltinVectorStore(VectorStoreProvider):
    """Mnemosyne's own index — memories and vectors in one SQLite file.

    Deliberately thin. The heavy lifting (FTS5 keyword search, the BGE/MIB
    vector tier, tier demotion) already lives in ``storage/sqlite_backend.py``
    and ``mnemosyne/retrieval.py``; re-implementing any of it here would create
    two ranking paths that could disagree. This adapter is an *adapter*: it maps
    the shared vector-store contract onto the existing backend, so a caller can
    treat the built-in store and Qdrant identically without the engine growing a
    second source of truth.
    """

    name = "builtin"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self._brain = self.config.get("brain")

    def _store(self):
        if self._brain is None:
            raise ProviderUnavailable(
                "builtin", "no MemoryBrain attached",
                hint="The built-in vector store requires a live brain instance.",
            )
        return self._brain.store

    def upsert(self, vectors, payloads, ids) -> None:
        store = self._store()
        for vid, vec, payload in zip(ids, vectors, payloads):
            rec = dict(payload or {})
            rec["id"] = vid
            rec["embedding"] = list(vec)
            if store.find_by_id(vid):
                store.update_by_id(vid, {"embedding": list(vec)})
            else:
                store.append(rec)

    def search(self, query_vector, top_k: int = 20,
               filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        store = self._store()
        scored: List[Dict[str, Any]] = []
        for rec in store.iter_records():
            if not _passes(rec, filters):
                continue
            emb = rec.get("embedding")
            if not emb:
                continue
            scored.append({"id": rec.get("id"),
                           "score": _cosine(query_vector, list(emb)),
                           "payload": rec})
        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored[:top_k]

    def delete(self, ids: List[str]) -> None:
        store = self._store()
        for vid in ids:
            try:
                store.delete(vid)
            except Exception:  # pragma: no cover - already gone
                pass

    def get(self, vector_id: str) -> Optional[Dict[str, Any]]:
        rec = self._store().find_by_id(vector_id)
        if not rec:
            return None
        return {"id": vector_id, "payload": rec}

    def list_all(self, filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        out = []
        for rec in self._store().iter_records():
            if _passes(rec, filters):
                out.append({"id": rec.get("id"), "payload": rec})
        return out

    def reset(self) -> None:
        store = self._store()
        for rec in list(store.iter_records()):
            try:
                store.delete(rec.get("id"))
            except Exception:  # pragma: no cover
                pass


def _passes(record: Dict[str, Any], filters: Optional[Dict[str, Any]]) -> bool:
    """Minimal filter check used by adapters that post-filter in Python."""
    if not filters:
        return True
    for key, want in filters.items():
        if key in ("$and", "and"):
            if not all(_passes(record, f) for f in (want or [])):
                return False
            continue
        if key in ("$or", "or"):
            if not any(_passes(record, f) for f in (want or [])):
                return False
            continue
        got = record.get(key)
        if isinstance(want, dict):
            for op, val in want.items():
                if op in ("$eq", "eq") and got != val:
                    return False
                if op in ("$ne", "ne") and got == val:
                    return False
                if op in ("$in", "in") and got not in (val or []):
                    return False
                if op in ("$nin", "nin") and got in (val or []):
                    return False
                if op in ("$gt", "gt") and not _cmp(got, val, ">"):
                    return False
                if op in ("$gte", "gte") and not _cmp(got, val, ">="):
                    return False
                if op in ("$lt", "lt") and not _cmp(got, val, "<"):
                    return False
                if op in ("$lte", "lte") and not _cmp(got, val, "<="):
                    return False
                if op in ("$contains", "contains") and str(val).lower() not in str(got).lower():
                    return False
        elif got != want:
            return False
    return True


def _cmp(a: Any, b: Any, op: str) -> bool:
    try:
        if op == ">":
            return a > b
        if op == ">=":
            return a >= b
        if op == "<":
            return a < b
        return a <= b
    except TypeError:
        return False


class InMemoryVectorStore(VectorStoreProvider):
    """Brute-force cosine over a dict — no persistence, zero dependencies.

    Used for tests, one-shot scripts and the compatibility layer's dry runs.
    Correct for any corpus that fits in memory, which for a *memory* system is
    usually true up to the point where the built-in store is the better answer
    anyway.
    """

    name = "memory"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self._points: Dict[str, Dict[str, Any]] = {}

    def upsert(self, vectors, payloads, ids) -> None:
        for vid, vec, payload in zip(ids, vectors, payloads):
            self._points[vid] = {"vector": list(vec), "payload": dict(payload or {})}

    def search(self, query_vector, top_k: int = 20,
               filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        rows = [{"id": i, "score": _cosine(query_vector, p["vector"]), "payload": p["payload"]}
                for i, p in self._points.items() if _passes(p["payload"], filters)]
        rows.sort(key=lambda r: r["score"], reverse=True)
        return rows[:top_k]

    def delete(self, ids: List[str]) -> None:
        for vid in ids:
            self._points.pop(vid, None)

    def get(self, vector_id: str) -> Optional[Dict[str, Any]]:
        p = self._points.get(vector_id)
        return {"id": vector_id, "payload": p["payload"]} if p else None

    def list_all(self, filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        return [{"id": i, "payload": p["payload"]} for i, p in self._points.items()
                if _passes(p["payload"], filters)]

    def reset(self) -> None:
        self._points.clear()


# ---------------------------------------------------------------------------
# HTTP stores
# ---------------------------------------------------------------------------

class QdrantVectorStore(VectorStoreProvider):
    """Qdrant REST API.

    This is the store the reference stack defaults to, and it is reachable with
    nothing but ``urllib`` — so a Qdrant user gets a fully featured vector tier
    without installing the official client.  Point ids are derived from the
    memory id by UUIDv5 so an upsert is genuinely idempotent.
    """

    name = "qdrant"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.url = (self.config.get("url") or self.config.get("host")
                    or _first_env("MNEMOSYNE_VECTOR_STORE_QDRANT_URL", "QDRANT_URL")
                    or "http://localhost:6333").rstrip("/")
        if not self.url.startswith("http"):
            self.url = f"http://{self.url}"
        self.api_key = (self.config.get("api_key")
                        or self.config.get("apiKey")
                        or _first_env("QDRANT_API_KEY"))
        self.collection = self.config.get("collection_name") or self.config.get("collection") or "mnemosyne"
        self.dims = int(self.config.get("embedding_model_dims")
                        or self.config.get("dimensions") or 1536)
        self.timeout = float(self.config.get("timeout", 60))
        # Collection creation is deferred to first use. Doing I/O in __init__
        # would turn a transient upstream outage into a construction failure,
        # leaving the object permanently unusable even after the service recovers
        # — and it would make merely *describing* the configuration require the
        # network to be up.
        self._ready = False

    def _ready_guard(self) -> None:
        """Ensure the collection exists, creating it on first use."""
        if self._ready:
            return
        self._ensure_collection()
        self._ready = True

    @property
    def _headers(self) -> Dict[str, str]:
        return {"api-key": self.api_key} if self.api_key else {}

    def _ensure_collection(self) -> None:
        try:
            request_json(f"{self.url}/collections/{self.collection}", method="GET",
                         headers=self._headers, timeout=self.timeout, retries=1)
            return
        except HttpError as e:
            if e.status not in (404, 0):
                raise
        request_json(
            f"{self.url}/collections/{self.collection}",
            method="PUT",
            payload={"vectors": {"size": self.dims, "distance": "Cosine"}},
            headers=self._headers, timeout=self.timeout, retries=1,
        )

    @staticmethod
    def _point_id(memory_id: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"mnemosyne:{memory_id}"))

    def upsert(self, vectors, payloads, ids) -> None:
        points = [{"id": self._point_id(vid), "vector": list(vec),
                   "payload": dict(payload or {}, _mnemosyne_id=vid)}
                  for vid, vec, payload in zip(ids, vectors, payloads)]
        request_json(f"{self.url}/collections/{self.collection}/points",
                     method="PUT", payload={"points": points},
                     headers=self._headers, timeout=self.timeout)

    def search(self, query_vector, top_k: int = 20,
               filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        body: Dict[str, Any] = {"vector": list(query_vector), "limit": top_k,
                                "with_payload": True}
        qfilter = _qdrant_filter(filters)
        if qfilter:
            body["filter"] = qfilter
        payload = request_json(f"{self.url}/collections/{self.collection}/points/search",
                               payload=body, headers=self._headers, timeout=self.timeout)
        out = []
        for r in ((payload or {}).get("result") or []):
            pl = dict(r.get("payload") or {})
            out.append({"id": pl.pop("_mnemosyne_id", str(r.get("id"))),
                        "score": float(r.get("score") or 0.0),
                        "payload": pl})
        return out

    def delete(self, ids: List[str]) -> None:
        if not ids:
            return
        request_json(f"{self.url}/collections/{self.collection}/points/delete",
                     payload={"points": [self._point_id(i) for i in ids]},
                     headers=self._headers, timeout=self.timeout)

    def list_all(self, filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        body: Dict[str, Any] = {"limit": int(self.config.get("scroll_limit", 1000)),
                                "with_payload": True}
        qfilter = _qdrant_filter(filters)
        if qfilter:
            body["filter"] = qfilter
        payload = request_json(f"{self.url}/collections/{self.collection}/points/scroll",
                               payload=body, headers=self._headers, timeout=self.timeout)
        out = []
        for r in (((payload or {}).get("result") or {}).get("points") or []):
            pl = dict(r.get("payload") or {})
            out.append({"id": pl.pop("_mnemosyne_id", str(r.get("id"))), "payload": pl})
        return out

    def reset(self) -> None:
        request_json(f"{self.url}/collections/{self.collection}", method="DELETE",
                     headers=self._headers, timeout=self.timeout, retries=1)
        self._ensure_collection()


def _qdrant_filter(filters: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Translate the engine's filter dict into Qdrant's filter DSL."""
    if not filters:
        return None
    must: List[Dict[str, Any]] = []
    should: List[Dict[str, Any]] = []
    for key, want in filters.items():
        if key in ("$and", "and"):
            for sub in (want or []):
                inner = _qdrant_filter(sub)
                if inner:
                    must.extend(inner.get("must") or [])
                    should.extend(inner.get("should") or [])
            continue
        if key in ("$or", "or"):
            for sub in (want or []):
                inner = _qdrant_filter(sub)
                if inner:
                    should.append({"must": inner.get("must") or []} if len(inner) > 1 else
                                  (inner.get("must") or [{}])[0])
            continue
        if isinstance(want, dict):
            for op, val in want.items():
                mapping = {"$eq": "match", "eq": "match", "$ne": "match",
                           "ne": "match", "$in": "match", "in": "match",
                           "$gt": "range", "gt": "range", "$gte": "range",
                           "gte": "range", "$lt": "range", "lt": "range",
                           "$lte": "range", "lte": "range"}
                kind = mapping.get(op)
                if kind == "match":
                    must.append({"key": key, "match": {"value": val}})
                elif kind == "range":
                    rng = {}
                    if op in ("$gte", "gte"):
                        rng["gte"] = val
                    elif op in ("$gt", "gt"):
                        rng["gt"] = val
                    elif op in ("$lte", "lte"):
                        rng["lte"] = val
                    else:
                        rng["lt"] = val
                    must.append({"key": key, "range": rng})
        else:
            must.append({"key": key, "match": {"value": want}})
    out: Dict[str, Any] = {}
    if must:
        out["must"] = must
    if should:
        out["should"] = should
    return out or None


class PineconeVectorStore(VectorStoreProvider):
    """Pinecone data-plane REST API."""

    name = "pinecone"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.api_key = (self.config.get("api_key")
                        or _first_env("MNEMOSYNE_VECTOR_STORE_PINECONE_API_KEY",
                                      "PINECONE_API_KEY"))
        if not self.api_key:
            raise ProviderUnavailable("pinecone", "no API key configured",
                                      hint="Set PINECONE_API_KEY.")
        host = self.config.get("index_host") or self.config.get("host")
        if not host:
            index = self.config.get("index_name") or self.config.get("index")
            if not index:
                raise ProviderUnavailable("pinecone", "index_host or index_name required")
            desc = request_json(
                f"https://api.pinecone.io/indexes/{index}", method="GET",
                headers={"Api-Key": self.api_key}, timeout=30,
            )
            host = (desc or {}).get("host")
        self.url = host if str(host).startswith("http") else f"https://{host}"
        self.namespace = self.config.get("namespace", "")
        self.timeout = float(self.config.get("timeout", 60))

    @property
    def _headers(self) -> Dict[str, str]:
        return {"Api-Key": self.api_key, "Content-Type": "application/json"}

    def upsert(self, vectors, payloads, ids) -> None:
        payload = {"vectors": [{"id": str(vid), "values": list(vec),
                                "metadata": dict(pl or {})}
                               for vid, vec, pl in zip(ids, vectors, payloads)]}
        if self.namespace:
            payload["namespace"] = self.namespace
        request_json(f"{self.url}/vectors/upsert", payload=payload,
                     headers=self._headers, timeout=self.timeout)

    def search(self, query_vector, top_k: int = 20,
               filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {"vector": list(query_vector), "topK": top_k,
                                   "includeMetadata": True}
        if self.namespace:
            payload["namespace"] = self.namespace
        if filters:
            payload["filter"] = _pinecone_filter(filters)
        resp = request_json(f"{self.url}/query", payload=payload,
                            headers=self._headers, timeout=self.timeout)
        return [{"id": m.get("id"), "score": float(m.get("score") or 0.0),
                 "payload": m.get("metadata") or {}}
                for m in ((resp or {}).get("matches") or [])]

    def delete(self, ids: List[str]) -> None:
        if not ids:
            return
        payload: Dict[str, Any] = {"ids": [str(i) for i in ids]}
        if self.namespace:
            payload["namespace"] = self.namespace
        request_json(f"{self.url}/vectors/delete", payload=payload,
                     headers=self._headers, timeout=self.timeout)


def _pinecone_filter(filters: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, want in filters.items():
        if key in ("$and", "and"):
            out["$and"] = [_pinecone_filter(f) for f in (want or [])]
        elif key in ("$or", "or"):
            out["$or"] = [_pinecone_filter(f) for f in (want or [])]
        elif isinstance(want, dict):
            out[key] = {op.lstrip("$"): val for op, val in want.items()}
        else:
            out[key] = {"$eq": want}
    return out


class ElasticsearchVectorStore(VectorStoreProvider):
    """Elasticsearch / OpenSearch ``_knn_search`` (or ``_search`` fallback)."""

    name = "elasticsearch"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.url = (self.config.get("url") or self.config.get("host")
                    or _first_env("MNEMOSYNE_VECTOR_STORE_ELASTICSEARCH_URL",
                                  "ELASTICSEARCH_URL")
                    or "http://localhost:9200").rstrip("/")
        self.index = self.config.get("index_name") or self.config.get("index") or "mnemosyne"
        self.username = self.config.get("user") or _first_env("ELASTICSEARCH_USERNAME")
        self.password = self.config.get("password") or _first_env("ELASTICSEARCH_PASSWORD")
        self.api_key = self.config.get("api_key") or _first_env("ELASTICSEARCH_API_KEY")
        self.dims = int(self.config.get("embedding_model_dims") or self.config.get("dimensions") or 1536)
        self.timeout = float(self.config.get("timeout", 60))
        self._ensure_index()

    @property
    def _headers(self) -> Dict[str, str]:
        if self.api_key:
            return {"Authorization": f"ApiKey {self.api_key}"}
        return {}

    def _auth(self):
        if self.username and self.password:
            return (self.username, self.password)
        return None

    def _ensure_index(self) -> None:
        try:
            request_json(f"{self.url}/{self.index}", method="GET",
                         headers=self._headers, auth=self._auth(),
                         timeout=self.timeout, retries=1)
            return
        except HttpError as e:
            if e.status not in (404, 0):
                raise
        request_json(
            f"{self.url}/{self.index}", method="PUT",
            payload={"mappings": {"properties": {
                "vector": {"type": "dense_vector", "dims": self.dims, "index": True,
                           "similarity": "cosine"},
                "memory_id": {"type": "keyword"},
                "payload": {"type": "object", "enabled": True},
            }}},
            headers=self._headers, auth=self._auth(), timeout=self.timeout, retries=1,
        )

    def upsert(self, vectors, payloads, ids) -> None:
        lines: List[str] = []
        for vid, vec, pl in zip(ids, vectors, payloads):
            lines.append('{"index": {"_index": "%s", "_id": "%s"}}'
                         % (self.index, str(vid).replace('"', "")))
            lines.append(_es_doc(vec, pl, vid))
        body = ("\n".join(lines) + "\n").encode("utf-8")
        import urllib.error
        import urllib.request

        req = urllib.request.Request(f"{self.url}/_bulk", data=body, method="POST",
                                     headers={**self._headers,
                                              "Content-Type": "application/x-ndjson"})
        try:
            urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            raise HttpError(e.code, f"{self.url}/_bulk",
                            e.read().decode("utf-8", errors="replace")) from None

    def search(self, query_vector, top_k: int = 20,
               filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {
            "knn": {"field": "vector", "query_vector": list(query_vector), "k": top_k,
                    "num_candidates": max(top_k * 10, 100)},
            "size": top_k, "_source": True,
        }
        if filters:
            payload["knn"]["filter"] = {"bool": {"must": _es_clauses(filters)}}
        try:
            resp = request_json(f"{self.url}/{self.index}/_search", payload=payload,
                                headers=self._headers, auth=self._auth(),
                                timeout=self.timeout)
        except HttpError as e:
            # Older OpenSearch builds lack _knn_search; fall back to script_score.
            if e.status != 400:
                raise
            payload = {"size": top_k,
                       "query": {"script_score": {
                           "query": {"match_all": {}},
                           "script": {"source": "cosineSimilarity(params.q, 'vector') + 1.0",
                                      "params": {"q": list(query_vector)}}}}}
            resp = request_json(f"{self.url}/{self.index}/_search", payload=payload,
                                headers=self._headers, auth=self._auth(),
                                timeout=self.timeout)
        hits = ((resp or {}).get("hits") or {}).get("hits") or []
        return [{"id": (h.get("_source") or {}).get("memory_id", h.get("_id")),
                 "score": float(h.get("_score") or 0.0),
                 "payload": (h.get("_source") or {}).get("payload") or {}}
                for h in hits]

    def delete(self, ids: List[str]) -> None:
        if not ids:
            return
        for vid in ids:
            try:
                request_json(f"{self.url}/{self.index}/_doc/{vid}", method="DELETE",
                             headers=self._headers, auth=self._auth(),
                             timeout=self.timeout, retries=1)
            except HttpError:
                pass

    def list_all(self, filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {"size": int(self.config.get("scroll_limit", 1000)),
                                  "query": {"match_all": {}}}
        if filters:
            payload["query"] = {"bool": {"must": _es_clauses(filters)}}
        resp = request_json(f"{self.url}/{self.index}/_search", payload=payload,
                            headers=self._headers, auth=self._auth(),
                            timeout=self.timeout)
        hits = ((resp or {}).get("hits") or {}).get("hits") or []
        return [{"id": (h.get("_source") or {}).get("memory_id", h.get("_id")),
                 "payload": (h.get("_source") or {}).get("payload") or {}} for h in hits]


def _es_doc(vec: List[float], payload: Optional[Dict[str, Any]], vid: str) -> str:
    import json

    return json.dumps({"vector": list(vec), "memory_id": vid,
                       "payload": _jsonable(payload or {})}, ensure_ascii=False)


def _jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def _es_clauses(filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for key, want in filters.items():
        field = f"payload.{key}"
        if key in ("$and", "and"):
            out.extend({"bool": {"must": _es_clauses(f)}} for f in (want or []))
            continue
        if key in ("$or", "or"):
            out.append({"bool": {"should": _es_clauses(f) for f in (want or [])}})
            continue
        if isinstance(want, dict):
            for op, val in want.items():
                op = op.lstrip("$")
                if op == "eq":
                    out.append({"term": {field: val}})
                elif op == "ne":
                    out.append({"bool": {"must_not": [{"term": {field: val}}]}})
                elif op == "in":
                    out.append({"terms": {field: list(val or [])}})
                elif op in ("gt", "gte", "lt", "lte"):
                    out.append({"range": {field: {op: val}}})
                elif op == "contains":
                    out.append({"match": {field: val}})
        else:
            out.append({"term": {field: want}})
    return out


class WeaviateVectorStore(VectorStoreProvider):
    """Weaviate v1 REST (GraphQL for search, objects API for writes)."""

    name = "weaviate"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.url = (self.config.get("url") or self.config.get("cluster_url")
                    or _first_env("WEAVIATE_URL") or "http://localhost:8080").rstrip("/")
        self.api_key = self.config.get("api_key") or _first_env("WEAVIATE_API_KEY")
        self.class_name = (self.config.get("collection_name")
                           or self.config.get("class_name") or "MnemosyneMemory")
        self.class_name = self.class_name[0].upper() + self.class_name[1:]
        self.timeout = float(self.config.get("timeout", 60))

    @property
    def _headers(self) -> Dict[str, str]:
        h = {}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def upsert(self, vectors, payloads, ids) -> None:
        for vid, vec, pl in zip(ids, vectors, payloads):
            body = {"class": self.class_name,
                    "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"mnemosyne:{vid}")),
                    "properties": {"memoryId": vid, "payload": _json_dumps(pl or {})},
                    "vector": list(vec)}
            try:
                request_json(f"{self.url}/v1/objects", payload=body,
                             headers=self._headers, timeout=self.timeout, retries=1)
            except HttpError as e:
                if e.status != 422:  # already exists -> update instead
                    raise
                oid = body["id"]
                request_json(f"{self.url}/v1/objects/{self.class_name}/{oid}",
                             method="PUT", payload=body, headers=self._headers,
                             timeout=self.timeout, retries=1)

    def search(self, query_vector, top_k: int = 20,
               filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        where = _weaviate_where(filters)
        gql = ("{ Get { %s(limit: %d%s nearVector: {vector: %s}) "
               "{ memoryId payload _additional { distance } } } }") % (
            self.class_name, top_k, where, _json_dumps(list(query_vector)))
        resp = request_json(f"{self.url}/v1/graphql", payload={"query": gql},
                            headers=self._headers, timeout=self.timeout)
        data = (((resp or {}).get("data") or {}).get("Get") or {}).get(self.class_name) or []
        out = []
        for row in data:
            dist = ((row.get("_additional") or {}).get("distance")) or 0.0
            out.append({"id": row.get("memoryId"),
                        "score": 1.0 - float(dist),
                        "payload": _json_loads(row.get("payload") or "{}")})
        out.sort(key=lambda r: r["score"], reverse=True)
        return out

    def delete(self, ids: List[str]) -> None:
        for vid in ids:
            oid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mnemosyne:{vid}"))
            try:
                request_json(f"{self.url}/v1/objects/{self.class_name}/{oid}",
                             method="DELETE", headers=self._headers,
                             timeout=self.timeout, retries=1)
            except HttpError:
                pass

    def list_all(self, filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        where = _weaviate_where(filters)
        gql = "{ Get { %s(limit: %d%s) { memoryId payload } } }" % (
            self.class_name, int(self.config.get("scroll_limit", 1000)), where)
        resp = request_json(f"{self.url}/v1/graphql", payload={"query": gql},
                            headers=self._headers, timeout=self.timeout)
        rows = (((resp or {}).get("data") or {}).get("Get") or {}).get(self.class_name) or []
        return [{"id": r.get("memoryId"),
                 "payload": _json_loads(r.get("payload") or "{}")} for r in rows]


def _weaviate_where(filters: Optional[Dict[str, Any]]) -> str:
    if not filters:
        return ""
    clauses = []
    for key, want in filters.items():
        if isinstance(want, dict) and "eq" in want:
            want = want["eq"]
        elif isinstance(want, dict):
            want = next(iter(want.values()))
        clauses.append('{path: ["payload"], operator: Equal, valueString: %s}'
                       % _json_dumps(str(want)))
    if not clauses:
        return ""
    if len(clauses) == 1:
        return f", where: {clauses[0]}"
    return ', where: {operator: And, operands: [%s]}' % ", ".join(clauses)


class UpstashVectorStore(VectorStoreProvider):
    """Upstash Vector REST API."""

    name = "upstash_vector"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.url = (self.config.get("url") or _first_env("UPSTASH_VECTOR_REST_URL") or "").rstrip("/")
        self.token = self.config.get("token") or _first_env("UPSTASH_VECTOR_REST_TOKEN")
        if not self.url or not self.token:
            raise ProviderUnavailable("upstash_vector",
                                      "url and token are both required",
                                      hint="Set UPSTASH_VECTOR_REST_URL and UPSTASH_VECTOR_REST_TOKEN.")
        self.namespace = self.config.get("namespace", "")
        self.timeout = float(self.config.get("timeout", 60))

    @property
    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def upsert(self, vectors, payloads, ids) -> None:
        request_json(f"{self.url}/upsert",
                     payload=[{"id": str(vid), "vector": list(vec),
                               "metadata": _jsonable(pl or {})}
                              for vid, vec, pl in zip(ids, vectors, payloads)],
                     headers=self._headers,
                     params={"namespace": self.namespace} if self.namespace else None,
                     timeout=self.timeout)

    def search(self, query_vector, top_k: int = 20,
               filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {"vector": list(query_vector),
                                   "topK": top_k, "includeMetadata": True}
        if filters:
            payload["filter"] = _json_dumps(filters)
        resp = request_json(f"{self.url}/query", payload=payload,
                            headers=self._headers,
                            params={"namespace": self.namespace} if self.namespace else None,
                            timeout=self.timeout)
        return [{"id": r.get("id"), "score": float(r.get("score") or 0.0),
                 "payload": r.get("metadata") or {}}
                for r in ((resp or {}).get("result") or [])]

    def delete(self, ids: List[str]) -> None:
        if not ids:
            return
        request_json(f"{self.url}/delete", payload={"ids": [str(i) for i in ids]},
                     headers=self._headers,
                     params={"namespace": self.namespace} if self.namespace else None,
                     timeout=self.timeout)


class TurbopufferVectorStore(VectorStoreProvider):
    """Turbopuffer REST API."""

    name = "turbopuffer"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.api_key = self.config.get("api_key") or _first_env("TURBOPUFFER_API_KEY")
        if not self.api_key:
            raise ProviderUnavailable("turbopuffer", "no API key configured",
                                      hint="Set TURBOPUFFER_API_KEY.")
        self.base_url = (self.config.get("base_url") or "https://api.turbopuffer.com/v2").rstrip("/")
        self.namespace = self.config.get("collection_name") or self.config.get("namespace") or "mnemosyne"
        self.dims = int(self.config.get("embedding_model_dims") or self.config.get("dimensions") or 1536)
        self.timeout = float(self.config.get("timeout", 60))

    @property
    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def upsert(self, vectors, payloads, ids) -> None:
        rows = []
        for vid, vec, pl in zip(ids, vectors, payloads):
            row = {"id": str(vid), "vector": list(vec)}
            row.update(_jsonable(pl or {}))
            rows.append(row)
        request_json(f"{self.base_url}/vectors/{self.namespace}",
                     method="PUT", payload={"upsert_rows": rows},
                     headers=self._headers, timeout=self.timeout)

    def search(self, query_vector, top_k: int = 20,
               filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        body: Dict[str, Any] = {"rank_by": ["vector", "ANN", list(query_vector)],
                                "limit": top_k, "include_attributes": True}
        if filters:
            body["filters"] = _json_dumps(filters)
        resp = request_json(f"{self.base_url}/vectors/{self.namespace}/query",
                            payload=body, headers=self._headers, timeout=self.timeout)
        return [{"id": r.get("id"), "score": float(r.get("$dist") or 0.0),
                 "payload": {k: v for k, v in r.items() if k not in ("id", "$dist")}}
                for r in (resp or [])]

    def delete(self, ids: List[str]) -> None:
        if not ids:
            return
        request_json(f"{self.base_url}/vectors/{self.namespace}/query",
                     payload={"deletes": [str(i) for i in ids]},
                     headers=self._headers, timeout=self.timeout)


class SDKVectorStore(VectorStoreProvider):
    """Adapter for stores whose SDK is mandatory.

    Rather than one hand-written class per store, each SDK-only store is
    described by a small spec — module, class, and how a collection object is
    obtained — and this adapter drives the LangChain-compatible interface the
    majority of those SDKs already expose (``add_texts`` / ``similarity_search``
    with scores).  When the SDK is absent the error names the exact extra.
    """

    name = "sdk"

    SPECS: Dict[str, Dict[str, Any]] = {
        "chroma": {"module": "langchain_chroma", "class": "Chroma",
                   "requires": ("langchain-chroma",), "extra": "chroma"},
        "pgvector": {"module": "langchain_postgres", "class": "PGVector",
                     "requires": ("langchain-postgres",), "extra": "pgvector"},
        "milvus": {"module": "langchain_milvus", "class": "Milvus",
                   "requires": ("langchain-milvus",), "extra": "milvus"},
        "mongodb": {"module": "langchain_mongodb", "class": "MongoDBAtlasVectorSearch",
                    "requires": ("langchain-mongodb",), "extra": "mongodb"},
        "redis": {"module": "langchain_redis", "class": "RedisVectorStore",
                  "requires": ("langchain-redis",), "extra": "redis"},
        "valkey": {"module": "langchain_redis", "class": "RedisVectorStore",
                   "requires": ("langchain-redis",), "extra": "redis"},
        "azure_ai_search": {"module": "langchain_community.vectorstores",
                            "class": "AzureSearch",
                            "requires": ("langchain-community",), "extra": "azure-search"},
        "azure_mysql": {"module": "langchain_community.vectorstores",
                        "class": "AzureMySQL",
                        "requires": ("langchain-community",), "extra": "azure-mysql"},
        "baidu": {"module": "langchain_community.vectorstores", "class": "BaiduVectorDB",
                  "requires": ("langchain-community",), "extra": "baidu"},
        "cassandra": {"module": "langchain_community.vectorstores",
                      "class": "Cassandra",
                      "requires": ("langchain-community",), "extra": "cassandra"},
        "databricks": {"module": "langchain_databricks", "class": "DatabricksVectorSearch",
                       "requires": ("langchain-databricks",), "extra": "databricks"},
        "faiss": {"module": "langchain_community.vectorstores", "class": "FAISS",
                  "requires": ("langchain-community", "faiss-cpu"), "extra": "faiss"},
        "langchain": {"module": "langchain_core.vectorstores", "class": "VectorStore",
                      "requires": ("langchain-core",), "extra": "langchain"},
        "neptune": {"module": "langchain_aws", "class": "NeptuneAnalyticsVector",
                    "requires": ("langchain-aws",), "extra": "neptune"},
        "opensearch": {"module": "langchain_community.vectorstores",
                       "class": "OpenSearchVectorSearch",
                       "requires": ("langchain-community", "opensearch-py"),
                       "extra": "opensearch"},
        "oracledb": {"module": "langchain_oracledb", "class": "OracleVS",
                     "requires": ("langchain-oracledb",), "extra": "oracle"},
        "s3_vectors": {"module": "langchain_aws", "class": "S3Vectors",
                       "requires": ("langchain-aws",), "extra": "s3-vectors"},
        "supabase": {"module": "langchain_community.vectorstores",
                     "class": "SupabaseVectorStore",
                     "requires": ("langchain-community", "supabase"), "extra": "supabase"},
        "vertex_ai_vector_search": {"module": "langchain_google_vertexai",
                                    "class": "VectorSearchVectorStore",
                                    "requires": ("langchain-google-vertexai",),
                                    "extra": "vertexai"},
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        provider = (self.config.get("_sdk_provider")
                    or self.config.get("provider") or "").strip().lower()
        spec = self.SPECS.get(provider)
        if spec is None:
            raise ProviderUnavailable(provider or "sdk", "no SDK spec registered")
        self.name = provider
        try:
            module = __import__(spec["module"], fromlist=[spec["class"].split(".")[-1]])
        except ImportError as e:
            raise ProviderUnavailable(
                provider, f"{spec['module']} not installed ({e})",
                hint=(f"pip install mnemosyne-os[{spec['extra']}] "
                      f"(requires {' + '.join(spec['requires'])})"),
            ) from None
        obj = module
        for part in spec["class"].split("."):
            obj = getattr(obj, part, None)
            if obj is None:
                raise ProviderUnavailable(provider,
                                          f"{spec['module']}.{spec['class']} not found")
        self._cls = obj
        self._store = self.config.get("store") or self.config.get("client")
        if self._store is None:
            raise ProviderUnavailable(
                provider, "an initialised client/store instance is required",
                hint=("Pass the already-constructed vector store as "
                      f"'store' in the {provider} config block; Mnemosyne does not "
                      "guess connection parameters for SDK-backed stores."),
            )

    def upsert(self, vectors, payloads, ids) -> None:
        texts = [str((p or {}).get("memory") or (p or {}).get("content") or "") for p in payloads]
        metas = [dict(p or {}, memory_id=vid) for vid, p in zip(ids, payloads)]
        emb = list(vectors)
        sv = self._store
        if hasattr(sv, "add_embeddings"):
            sv.add_embeddings(text_embeddings=list(zip(texts, emb)),
                              metadatas=metas, ids=list(ids))
        elif hasattr(sv, "add_texts"):
            sv.add_texts(texts=texts, metadatas=metas, ids=list(ids),
                         embeddings=emb)

    def search(self, query_vector, top_k: int = 20,
               filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        sv = self._store
        if hasattr(sv, "similarity_search_with_score_by_vector"):
            rows = sv.similarity_search_with_score_by_vector(query_vector, k=top_k)
        elif hasattr(sv, "similarity_search_by_vector_with_relevance_scores"):
            rows = sv.similarity_search_by_vector_with_relevance_scores(query_vector, k=top_k)
        else:
            return []
        out = []
        for doc, score in rows:
            meta = dict(getattr(doc, "metadata", None) or {})
            out.append({"id": meta.pop("memory_id", None),
                        "score": float(score),
                        "payload": {**meta, "memory": getattr(doc, "page_content", "")}})
        return out

    def delete(self, ids: List[str]) -> None:
        if hasattr(self._store, "delete"):
            for vid in ids:
                try:
                    self._store.delete([vid])
                except Exception:  # pragma: no cover - SDK-specific
                    pass


class GenericRESTVectorStore(VectorStoreProvider):
    """Declarative adapter for a REST store with no dedicated class.

    Config::

        vector_store:
          provider: generic
          config:
            base_url: https://vectors.example.com
            headers: {Authorization: "Bearer ..."}
            upsert_path: /v1/points         # POST {"vectors": [...]}
            search_path: /v1/points/search  # POST {"vector": [...], "top_k": n}
            delete_path: /v1/points/delete  # POST {"ids": [...]}

    Exists so that supporting a niche store is a config change rather than a code
    change — the practical difference between "we can use it today" and "we will
    support it next release".
    """

    name = "generic"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.base_url = str(self.config.get("base_url") or "").rstrip("/")
        if not self.base_url:
            raise ProviderUnavailable("generic", "base_url is required")
        self.headers = dict(self.config.get("headers") or {})
        self.upsert_path = self.config.get("upsert_path", "/upsert")
        self.search_path = self.config.get("search_path", "/search")
        self.delete_path = self.config.get("delete_path", "/delete")
        self.timeout = float(self.config.get("timeout", 60))
        self._id_field = self.config.get("id_field", "id")
        self._vector_field = self.config.get("vector_field", "vector")
        self._score_field = self.config.get("score_field", "score")
        self._payload_field = self.config.get("payload_field", "payload")
        self._results_path = self.config.get("results_path", "results")

    def _unwrap(self, payload: Any) -> List[Dict[str, Any]]:
        cur = payload
        for part in str(self._results_path).split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
        return cur if isinstance(cur, list) else []

    def upsert(self, vectors, payloads, ids) -> None:
        request_json(f"{self.base_url}{self.upsert_path}",
                     payload={"vectors": [
                         {self._id_field: str(vid), self._vector_field: list(vec),
                          self._payload_field: dict(pl or {})}
                         for vid, vec, pl in zip(ids, vectors, payloads)]},
                     headers=self.headers, timeout=self.timeout)

    def search(self, query_vector, top_k: int = 20,
               filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        body: Dict[str, Any] = {self._vector_field: list(query_vector), "top_k": top_k}
        if filters:
            body["filters"] = _jsonable(filters)
        resp = request_json(f"{self.base_url}{self.search_path}", payload=body,
                            headers=self.headers, timeout=self.timeout)
        return [{"id": r.get(self._id_field), "score": float(r.get(self._score_field) or 0.0),
                 "payload": r.get(self._payload_field) or {}}
                for r in self._unwrap(resp)]

    def delete(self, ids: List[str]) -> None:
        if not ids:
            return
        id_key = self.config.get("delete_id_field", "ids")
        request_json(f"{self.base_url}{self.delete_path}",
                     payload={id_key: [str(i) for i in ids]},
                     headers=self.headers, timeout=self.timeout)


def _json_dumps(v: Any) -> str:
    import json

    return json.dumps(_jsonable(v), ensure_ascii=False)


def _json_loads(s: str) -> Any:
    import json

    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return {}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_HTTP_STORES: Dict[str, type] = {
    "qdrant": QdrantVectorStore,
    "pinecone": PineconeVectorStore,
    "elasticsearch": ElasticsearchVectorStore,
    "opensearch": ElasticsearchVectorStore,  # wire-compatible
    "weaviate": WeaviateVectorStore,
    "upstash_vector": UpstashVectorStore,
    "turbopuffer": TurbopufferVectorStore,
}

_BUILTIN_STORES: Dict[str, type] = {
    "builtin": BuiltinVectorStore,
    "memory": InMemoryVectorStore,
    "generic": GenericRESTVectorStore,
}


def _sdk_factory(provider: str):
    def build(config: Optional[Dict[str, Any]] = None):
        cfg = dict(config or {})
        cfg["_sdk_provider"] = provider
        return SDKVectorStore(cfg)

    return build


def _all_names() -> List[str]:
    return sorted(set(_BUILTIN_STORES) | set(_HTTP_STORES) | set(SDKVectorStore.SPECS))


def get_vector_store(config: Optional[Dict[str, Any]] = None,
                     brain: Any = None) -> VectorStoreProvider:
    """Build the vector store described by *config*.

    Default is ``builtin`` — the engine's own SQLite-backed index — so an
    unconfigured install has a working vector tier with no external service.
    """
    config = config or {}
    name = (config.get("provider") or "builtin").strip().lower()
    if name in ("none", "", "off", "disabled"):
        name = "builtin"
    params = config.get("config") or config.get("params") or {}

    if name in _BUILTIN_STORES:
        cls = _BUILTIN_STORES[name]
        if cls is BuiltinVectorStore:
            return cls({**params, "brain": brain})
        return cls(params)
    if name in _HTTP_STORES:
        return _HTTP_STORES[name](params)
    if name in SDKVectorStore.SPECS:
        return _sdk_factory(name)(params)
    raise ProviderUnavailable(
        name, "unknown vector store provider",
        hint="Known providers: " + ", ".join(_all_names()),
    )


def list_vector_store_providers() -> List[str]:
    return _all_names()


def describe_vector_stores() -> List[Dict[str, Any]]:
    """Report every store, how it is reached, and whether it is usable now.

    SDK-backed stores are probed for their ``langchain-*`` bridge so the answer
    reflects this machine rather than a static table.
    """
    import importlib.util

    out: List[Dict[str, Any]] = []
    for name, cls in sorted(_BUILTIN_STORES.items()):
        out.append({"name": name, "transport": "embedded", "available": True,
                    "requires": [], "notes": (cls.__doc__ or "").strip().split("\n")[0]})
    for name, cls in sorted(_HTTP_STORES.items()):
        out.append({"name": name, "transport": "http", "available": True,
                    "requires": [], "notes": (cls.__doc__ or "").strip().split("\n")[0]})
    for name, spec in sorted(SDKVectorStore.SPECS.items()):
        ok = all(_mod_installed(m) for m in spec["requires"])
        out.append({"name": name, "transport": "sdk", "available": ok,
                    "requires": list(spec["requires"]),
                    "extra": spec.get("extra", ""),
                    "notes": "requires the %s SDK bridge" % spec["module"]})
    return out


def _mod_installed(dist: str) -> bool:
    import importlib.util

    mod = dist.replace("-", "_")
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False
