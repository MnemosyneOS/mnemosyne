"""Qdrant vector backend plugin for Mnemosyne OS (BGE-M3 embeddings).

Drop-in replacement for the built-in random-projection ``EmbeddingEngine``
and for the ``numpy_vector`` plugin.  Vectors are produced by an external
BGE-M3 embedding service (CrispEmbed, OpenAI-compatible ``/v1/embeddings``)
and stored in a local Qdrant collection, which provides a persistent ANN
index shared across processes instead of the per-process in-memory dict
used by ``numpy_vector``.

Design contract (verified against Mnemosyne 7.0.1 call sites)
-------------------------------------------------------------
``available``
    Read at ``mnemosyne/brain.py:184`` to decide whether to splice this
    instance into the retrieval vector path.  It is a **capability check**
    (is the environment usable?), never a live probe: a failed probe here
    would silently disable embeddings for the whole process.  Reachability
    is re-checked lazily on each call instead.

``encode(text)``
    Called at ``brain.py:750/775/936/1328/1425``, ``retrieval.py:477`` and
    ``cognitive.py:383/396``.  Returns a list[float] or ``None`` on
    failure (``None`` is tolerated by every call site).

``add(memory_id, vector)``
    Called at ``brain.py:756/780/942`` on the ``retain()`` /
    ``retain_batch()`` write paths.

``search(query_vector, top_k)``
    Called at ``retrieval.py:485`` for Path2a semantic candidate recall —
    the actual reason this plugin exists: it surfaces semantically
    equivalent memories that fall outside the FTS5 keyword candidate pool
    (``candidate_n=500``).  Returns ``[(memory_id, score), ...]`` sorted by
    descending score.

``similarity(vec_a, vec_b)``
    Called *per candidate* at ``retrieval.py:495``, ``cognitive.py:399``,
    ``utils.py:786`` and ``brain.py:2021``.  It MUST be pure local Python —
    issuing an HTTP request here would turn one ``recall()`` into hundreds
    of round-trips.

Configuration (environment variables)
-------------------------------------
``MNEMOSYNE_QDRANT_URL``            default ``http://127.0.0.1:6333``
``MNEMOSYNE_QDRANT_COLLECTION``     default ``mnemosyne_{namespace|dir}``
``MNEMOSYNE_EMBED_URL``             default ``http://127.0.0.1:5080/v1/embeddings``
``MNEMOSYNE_EMBED_MODEL``           default ``bge-m3``
``MNEMOSYNE_EMBED_DIM``             default ``1024`` (BGE-M3 dense dimension)
``MNEMOSYNE_EMBED_MAX_CHARS``       default ``8000`` (per-request truncation)
``MNEMOSYNE_EMBED_TIMEOUT``         default ``10`` (seconds, embedding call)
``MNEMOSYNE_QDRANT_TIMEOUT``        default ``5`` (seconds, routine REST call)
``MNEMOSYNE_QDRANT_CREATE_TIMEOUT`` default ``90`` (seconds, collection creation)
``MNEMOSYNE_QDRANT_BREAKER``        default ``30`` (seconds to skip an unreachable service)

Note on collection creation cost
--------------------------------
Creating a Qdrant collection on Windows takes ~6 seconds (observed
1.19.1, cold storage).  It happens **once per collection**, lazily on the
first write — hence the dedicated, longer ``CREATE_TIMEOUT``.  Deployments
should pre-create collections via :meth:`warmup` so no runtime write pays
this cost.

Usage::

    # via environment (picked up by MemoryBrain when ``plugins`` is None)
    set MNEMOSYNE_PLUGINS=qdrant_backend

    # or explicitly
    brain = MemoryBrain(dir, plugins=["qdrant_backend"])
"""
import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request

from storage.plugin_sdk import VectorBackendPlugin

__all__ = ["QdrantVectorBackend", "register", "get_plugin_class"]

logger = logging.getLogger("mnemosyne_plugins.qdrant_backend")


class QdrantVectorBackend(VectorBackendPlugin):
    """BGE-M3 embeddings + Qdrant ANN index as Mnemosyne's vector backend."""

    name = "qdrant_backend"
    description = "Qdrant ANN vector index with BGE-M3 embeddings (HTTP)"

    def __init__(self, brain=None, dim=None, model_name=None):
        super().__init__(brain)
        self.dim = int(dim or os.environ.get("MNEMOSYNE_EMBED_DIM", "1024"))
        self.qdrant_url = os.environ.get(
            "MNEMOSYNE_QDRANT_URL", "http://127.0.0.1:6333").rstrip("/")
        self.embed_url = os.environ.get(
            "MNEMOSYNE_EMBED_URL", "http://127.0.0.1:5080/v1/embeddings")
        self.model = model_name or os.environ.get("MNEMOSYNE_EMBED_MODEL", "bge-m3")
        self.timeout = float(os.environ.get("MNEMOSYNE_QDRANT_TIMEOUT", "5"))
        self.create_timeout = float(
            os.environ.get("MNEMOSYNE_QDRANT_CREATE_TIMEOUT", "90"))
        self.embed_timeout = float(os.environ.get("MNEMOSYNE_EMBED_TIMEOUT", "10"))
        self.breaker_seconds = float(os.environ.get("MNEMOSYNE_QDRANT_BREAKER", "30"))
        self.max_chars = int(os.environ.get("MNEMOSYNE_EMBED_MAX_CHARS", "8000"))
        self.collection = os.environ.get("MNEMOSYNE_QDRANT_COLLECTION") or \
            self._derive_collection(brain)

        # This backend only ever talks to localhost services.  Routing those
        # through an ambient HTTP_PROXY is both pointless and harmful: a proxy
        # that cannot reach the port answers 502 (after its own timeout), which
        # is indistinguishable from a service error and used to defeat the
        # circuit breaker.  Bypass proxies unconditionally.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        # Capability flag — must be truthy at load time (brain.py:174).
        self._available = True
        # Runtime state (never gates ``available``).
        self._collection_ready = False
        self._collection_retry_after = 0.0
        self._qdrant_down_until = 0.0
        self._embed_down_until = 0.0
        # memory_id -> Qdrant point id (informational / debugging aid).
        self._point_ids = {}
        self._stats = {"encoded": 0, "upserted": 0, "searched": 0,
                       "encode_errors": 0, "qdrant_errors": 0,
                       "collection_created": 0}

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _derive_collection(brain):
        """Collection name mirrors Mnemosyne's own namespace isolation."""
        ns = getattr(brain, "namespace", None)
        if not ns and brain is not None:
            ns = getattr(getattr(brain, "store", None), "namespace", None)
        if not ns and brain is not None:
            base = getattr(brain, "base_dir", None)
            if base:
                ns = os.path.basename(os.path.normpath(base))
        safe = "".join(
            c if (c.isalnum() or c in "-_") else "_" for c in str(ns or "default"))
        return f"mnemosyne_{safe}"

    @staticmethod
    def _point_id(memory_id):
        """Map a Mnemosyne id to a Qdrant-acceptable uint64 point id.

        Mnemosyne ids are ``sha256(...)[:16]`` hex strings (``utils.py:88``),
        which fit uint64 exactly.  Anything else falls back to a blake2b
        digest; the authoritative id always travels in the payload, and
        ``search()`` always reads it back from there.
        """
        try:
            value = int(str(memory_id), 16)
            if 0 <= value < (1 << 64):
                return value
        except (TypeError, ValueError):
            pass
        return int.from_bytes(
            hashlib.blake2b(str(memory_id).encode("utf-8"), digest_size=8).digest(),
            "big")

    def _http_json(self, url, payload=None, method="GET", timeout=None):
        """Single HTTP JSON call.  Raises on transport errors.

        Uses the proxy-free opener so ambient ``HTTP_PROXY`` settings can
        never intercept these localhost calls.
        """
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json",
                     "User-Agent": "mnemosyne-qdrant-backend/1.0"})
        with self._opener.open(req, timeout=timeout or self.timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
        return json.loads(body) if body.strip() else {}

    # -- circuit breakers: a broken dependency must never stall recall() --
    def _breaker_open(self, which):
        until = self._qdrant_down_until if which == "qdrant" else self._embed_down_until
        return time.time() < until

    def _trip(self, which):
        if which == "qdrant":
            self._qdrant_down_until = time.time() + self.breaker_seconds
        else:
            self._embed_down_until = time.time() + self.breaker_seconds
        self._stats["qdrant_errors" if which == "qdrant" else "encode_errors"] += 1

    def _ensure_collection(self):
        """Make sure the collection exists.  Returns True when usable.

        Uses a cheap short-timeout probe first so that an unreachable
        Qdrant fails fast instead of blocking recall for the (much longer)
        creation timeout.
        """
        if self._collection_ready:
            return True
        now = time.time()
        if now < self._collection_retry_after or self._breaker_open("qdrant"):
            return False
        try:
            self._http_json(f"{self.qdrant_url}/collections/{self.collection}")
            self._collection_ready = True
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return self._create_collection()
            if exc.code == 500:
                # Transient only: Qdrant answers 500 while another process is
                # still creating this collection ("0 of 0 read operations failed").
                self._collection_retry_after = now + 2.0
                self._stats["qdrant_errors"] += 1
                return False
            # Any other HTTP status (502 from a proxy, 401/403, …) means the
            # endpoint is unhealthy → trip the breaker.  Using a short retry
            # window here would re-pay the connection timeout on every call.
            logger.debug("Qdrant 探测失败 HTTP %s，语义召回暂时降级：%s", exc.code, exc)
            self._trip("qdrant")
            return False
        except Exception as exc:
            logger.debug("Qdrant 不可达，语义召回降级为纯 FTS5：%s", exc)
            self._trip("qdrant")
            return False

    def _create_collection(self):
        try:
            self._http_json(
                f"{self.qdrant_url}/collections/{self.collection}",
                {"vectors": {"size": self.dim, "distance": "Cosine"}},
                method="PUT", timeout=self.create_timeout)
            self._collection_ready = True
            self._stats["collection_created"] += 1
        except urllib.error.HTTPError as exc:
            if exc.code == 409:            # already exists — fine
                self._collection_ready = True
            else:
                logger.debug("创建 Qdrant 集合失败 HTTP %s：%s", exc.code, exc)
                self._trip("qdrant")
        except Exception as exc:
            logger.debug("创建 Qdrant 集合失败：%s", exc)
            self._trip("qdrant")
        return self._collection_ready

    # ------------------------------------------------------------------
    # VectorBackendPlugin interface
    # ------------------------------------------------------------------
    @property
    def available(self):
        return self._available

    def encode(self, text):
        """Return a BGE-M3 dense vector for *text*, or ``None`` on failure."""
        if not self._available:
            return None
        text = (text or "").strip()
        if not text:
            return None
        if self._breaker_open("embed"):
            return None
        if len(text) > self.max_chars:
            text = text[:self.max_chars]
        try:
            resp = self._http_json(
                self.embed_url, {"input": [text], "model": self.model},
                method="POST", timeout=self.embed_timeout)
            vec = resp["data"][0]["embedding"]
            if not isinstance(vec, list) or not vec:
                self._trip("embed")
                return None
            self._stats["encoded"] += 1
            return [float(x) for x in vec]
        except Exception as exc:
            logger.debug("BGE-M3 嵌入失败，降级为关键词检索：%s", exc)
            self._trip("embed")
            return None

    def similarity(self, vec_a, vec_b):
        """Local pure-Python cosine similarity (never hits the network).

        Mnemosyne calls this once per candidate during ranking; a network
        round-trip here would be catastrophic for recall latency.
        Dimension mismatch (e.g. vectors written by the pre-plugin random
        projection engine) yields 0.0 rather than an exception.
        """
        if not vec_a or not vec_b:
            return 0.0
        try:
            if len(vec_a) != len(vec_b):
                return 0.0
            dot = 0.0
            na = 0.0
            nb = 0.0
            for a, b in zip(vec_a, vec_b):
                fa = float(a)
                fb = float(b)
                dot += fa * fb
                na += fa * fa
                nb += fb * fb
            if na <= 0.0 or nb <= 0.0:
                return 0.0
            return float(max(0.0, dot / ((na ** 0.5) * (nb ** 0.5))))
        except Exception:
            return 0.0

    def add(self, memory_id, vector, **kwargs):
        """Upsert one memory vector into Qdrant (no-op on failure)."""
        if not self._available or not vector:
            return
        if self._breaker_open("qdrant") or not self._ensure_collection():
            return
        try:
            pid = self._point_id(memory_id)
            payload = {"memory_id": str(memory_id), "collection": self.collection}
            for key in ("namespace", "layer", "content"):
                if kwargs.get(key) is not None:
                    payload[key] = kwargs[key]
            if self.brain is not None and getattr(self.brain, "namespace", None):
                payload.setdefault("namespace", self.brain.namespace)
            self._http_json(
                f"{self.qdrant_url}/collections/{self.collection}/points?wait=true",
                {"points": [{"id": pid, "vector": [float(x) for x in vector],
                             "payload": payload}]},
                method="PUT")
            self._point_ids[str(memory_id)] = pid
            self._stats["upserted"] += 1
        except urllib.error.HTTPError as exc:
            if exc.code == 404:            # collection vanished → recreate lazily
                self._collection_ready = False
                self._collection_retry_after = 0.0
                return
            logger.debug("Qdrant 写入失败 HTTP %s（记忆本身已安全落库）：%s", exc.code, exc)
            self._trip("qdrant")
        except Exception as exc:
            logger.debug("Qdrant 写入失败（记忆本身已安全落库）：%s", exc)
            self._trip("qdrant")

    def search(self, query_vector, top_k=5, **kwargs):
        """Semantic ANN search → ``[(memory_id, score), ...]`` desc."""
        if not self._available or not query_vector:
            return []
        if self._breaker_open("qdrant") or not self._ensure_collection():
            return []
        try:
            limit = max(1, min(int(top_k or 5), 2048))
            resp = self._http_json(
                f"{self.qdrant_url}/collections/{self.collection}/points/query",
                {"query": [float(x) for x in query_vector], "limit": limit,
                 "with_payload": True, "with_vector": False},
                method="POST")
            result = resp.get("result") or {}
            points = result.get("points") if isinstance(result, dict) else result
            out = []
            for p in (points or []):
                mid = (p.get("payload") or {}).get("memory_id")
                if mid is None:
                    continue
                out.append((str(mid), float(p.get("score") or 0.0)))
            out.sort(key=lambda x: x[1], reverse=True)
            self._stats["searched"] += 1
            return out
        except urllib.error.HTTPError as exc:
            if exc.code == 404:            # collection vanished → recreate lazily
                self._collection_ready = False
                self._collection_retry_after = 0.0
                return []
            logger.debug("Qdrant 语义检索失败 HTTP %s，回退纯 FTS5：%s", exc.code, exc)
            self._trip("qdrant")
            return []
        except Exception as exc:
            logger.debug("Qdrant 语义检索失败，回退纯 FTS5：%s", exc)
            self._trip("qdrant")
            return []

    # Qdrant persists its own state; nothing to serialise into the brain.
    def save(self, brain=None):
        pass

    def load(self, brain=None):
        pass

    # ------------------------------------------------------------------
    # diagnostics / deployment helpers
    # ------------------------------------------------------------------
    def warmup(self, probe_text="warmup"):
        """Pre-create the collection and prime the embedding model.

        Deployment should call this once per namespace so that no runtime
        write pays the ~6 s collection-creation cost.
        """
        t0 = time.time()
        created = self._ensure_collection()
        create_ms = (time.time() - t0) * 1000
        t1 = time.time()
        vec = self.encode(probe_text)
        embed_ms = (time.time() - t1) * 1000
        return {"collection": self.collection, "collection_ready": created,
                "collection_ms": round(create_ms, 1), "embed_ok": vec is not None,
                "embed_dim": len(vec) if vec else None,
                "embed_ms": round(embed_ms, 1)}

    def health(self):
        """Return a small dict describing live dependency status."""
        out = {"plugin": self.name, "collection": self.collection,
               "dim": self.dim, "qdrant_url": self.qdrant_url,
               "embed_url": self.embed_url, "stats": dict(self._stats)}
        try:
            r = self._http_json(f"{self.qdrant_url}/collections/{self.collection}")
            res = r.get("result") or {}
            out["qdrant"] = "up"
            out["points"] = res.get("points_count")
            params = (res.get("config", {}).get("params", {}).get("vectors") or {})
            out["vector_size"] = params.get("size")
            out["distance"] = params.get("distance")
        except Exception as exc:
            out["qdrant"] = f"down: {exc}"
        try:
            t0 = time.time()
            vec = self.encode("health probe")
            out["embed"] = "up" if vec else "down"
            out["embed_dim"] = len(vec) if vec else None
            out["embed_ms"] = round((time.time() - t0) * 1000, 1)
        except Exception as exc:
            out["embed"] = f"down: {exc}"
        return out


def register(brain):
    """Plugin entry point — returns a :class:`QdrantVectorBackend`."""
    return QdrantVectorBackend(brain)


def get_plugin_class():
    return QdrantVectorBackend
