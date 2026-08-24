"""NumPy vector backend plugin for Mnemosyne OS.

Uses ``numpy`` for efficient dense-vector storage and cosine-similarity
retrieval.  This plugin is **optional** — numpy is a third-party
dependency, so the plugin degrades gracefully when numpy is not
installed.

When ``sentence-transformers`` and a local model are available, the
plugin loads a high-quality dense embedding model (BAAI/bge-small-zh-v1.5)
for semantic matching.  Otherwise it falls back to a hash-based
deterministic encoder that provides basic character n-gram overlap
signals.

Model loading is **lazy** — the sentence-transformers model is only
loaded on the first call to ``encode()``, so plugin initialization
has zero latency.

Usage::

    brain = MemoryBrain(dir, plugins=["numpy_vector"])
"""
import os

# Graceful degradation: numpy is optional
try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    np = None
    _HAS_NUMPY = False

from storage.plugin_sdk import VectorBackendPlugin

__all__ = ["NumpyVectorBackend", "register", "get_plugin_class"]


class NumpyVectorBackend(VectorBackendPlugin):
    """Vector storage + cosine similarity retrieval backed by numpy.

    If ``sentence-transformers`` is installed and the
    BAAI/bge-small-zh-v1.5 model can be loaded, the ``encode`` method
    produces high-quality dense embeddings suitable for semantic search.
    Otherwise the plugin falls back to a hash-based deterministic encoder.
    """

    name = "numpy_vector"
    description = "NumPy-accelerated vector storage and cosine similarity retrieval"

    def __init__(self, brain=None, dim=128, model_name=None):
        super().__init__(brain)
        self.dim = dim
        self._vectors: dict[str, list] = {}
        self._available = _HAS_NUMPY
        self._model = None
        self._model_loaded = False
        self._model_name = model_name or os.environ.get(
            "MNEMOSYNE_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"
        )

    @property
    def available(self):
        return self._available

    def _ensure_model(self):
        """Lazily load the sentence-transformers model on first use.

        加载优先级：
          1. ``self._model_name`` 为本地目录路径 → 直接离线加载；
          2. ``MNEMOSYNE_ALLOW_MODEL_DOWNLOAD=1`` → 允许联网下载（端点受
             ``HF_ENDPOINT`` 控制）；
          3. 否则仅检查标准 HF hub 缓存（``$HF_HOME/hub/models--<org>--<name>``），
             命中才加载；未命中回退哈希编码（不发起任何网络请求）。
        """
        if self._model_loaded:
            return
        self._model_loaded = True
        allow_download = os.environ.get("MNEMOSYNE_ALLOW_MODEL_DOWNLOAD", "0") == "1"
        try:
            from sentence_transformers import SentenceTransformer
            name = self._model_name
            # 1) 本地目录路径（工作区/任意路径）直接离线加载
            if os.path.isdir(name):
                self._model = SentenceTransformer(name)
                return
            # 2) 允许联网下载
            if allow_download:
                self._model = SentenceTransformer(name)
                return
            # 3) 仅本地缓存：需实际权重文件存在才加载，且强制离线（绝不下载）。
            #    注意：仅判断缓存目录是否存在不可靠——目录可能只有 .locks/元数据，
            #    会误触发联网下载导致挂起（阶段3 发现并修复）。
            import glob
            hub = os.environ.get("HF_HOME", os.path.expanduser("~/.cache/huggingface"))
            snapshots = os.path.join(hub, "hub", "models--" + name.replace("/", "--"),
                                     "snapshots", "*")
            has_weights = any(
                os.path.isfile(os.path.join(s, w))
                for s in glob.glob(snapshots)
                for w in ("model.safetensors", "pytorch_model.bin")
            )
            if has_weights:
                os.environ["HF_HUB_OFFLINE"] = "1"
                os.environ["TRANSFORMERS_OFFLINE"] = "1"
                try:
                    self._model = SentenceTransformer(name)
                finally:
                    os.environ.pop("HF_HUB_OFFLINE", None)
                    os.environ.pop("TRANSFORMERS_OFFLINE", None)
            else:
                self._model = None  # 未缓存，回退哈希编码
        except Exception:
            self._model = None  # fall back to hash-based encoding


    def encode(self, text):
        """Encode text into a dense vector.

        If a sentence-transformers model is loaded, uses it.  Otherwise
        falls back to a deterministic hash-based encoder using character
        n-grams (bigrams + trigrams).
        """
        if not self._available:
            return None
        text = (text or "").lower()

        # v7.0.2: Lazy model loading
        self._ensure_model()

        # Path 1: high-quality sentence-transformers model (if loaded)
        if self._model is not None:
            try:
                vec = self._model.encode(text, normalize_embeddings=True)
                return vec.tolist()
            except Exception as exc:  # 模型推理失败 → 回退哈希编码
                import logging
                logging.getLogger("mnemosyne_plugins.numpy_vector").debug(
                    "模型编码失败，回退哈希编码：%s", exc)

        # Path 2: hash-based deterministic encoder (fallback)
        import hashlib
        vec = np.zeros(self.dim, dtype=np.float64)
        # Character bigrams
        for i in range(len(text) - 1):
            gram = text[i:i + 2]
            h = int(hashlib.md5(gram.encode("utf-8")).hexdigest(), 16)
            bucket = h % self.dim
            vec[bucket] += 1.0
        # Character trigrams (additional signal)
        for i in range(len(text) - 2):
            gram = text[i:i + 3]
            h = int(hashlib.md5(gram.encode("utf-8")).hexdigest(), 16)
            bucket = h % self.dim
            vec[bucket] += 0.5
        # L2-normalise
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        else:
            vec = np.zeros(self.dim, dtype=np.float64)
            vec[0] = 1.0
        return vec.tolist()

    def similarity(self, vec_a, vec_b):
        """两个向量间的余弦相似度（drop-in 兼容 EmbeddingEngine.similarity）。

        向量可能来自模型（512 维）或哈希编码（128 维），故按维度自适应。
        """
        if not self._available or vec_a is None or vec_b is None:
            return 0.0
        try:
            a = np.asarray(vec_a, dtype=np.float64)
            b = np.asarray(vec_b, dtype=np.float64)
        except Exception:
            return 0.0
        na = np.linalg.norm(a)
        nb = np.linalg.norm(b)
        if na == 0 or nb == 0:
            return 0.0
        return float(max(0.0, np.dot(a, b) / (na * nb)))

    def add(self, memory_id, vector, **kwargs):
        if not self._available or vector is None:
            return
        arr = np.asarray(vector, dtype=np.float64)
        norm = np.linalg.norm(arr)
        if norm > 0:
            arr = arr / norm
        self._vectors[memory_id] = arr.tolist()

    def search(self, query_vector, top_k=5, **kwargs):
        """Return ``[(memory_id, score), ...]`` sorted by descending score."""
        if not self._available or not self._vectors:
            return []
        qv = np.asarray(query_vector, dtype=np.float64)
        qnorm = np.linalg.norm(qv)
        if qnorm > 0:
            qv = qv / qnorm
        results = []
        for mid, vec in self._vectors.items():
            arr = np.asarray(vec, dtype=np.float64)
            dot = float(np.dot(qv, arr))
            results.append((mid, max(0.0, dot)))
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    def save(self, brain):
        """No-op: vectors are recomputed on load."""
        pass

    def load(self, brain):
        """No-op: vectors are populated via :meth:`add` during retain."""
        pass


def register(brain):
    """Plugin entry point — returns a NumpyVectorBackend instance."""
    return NumpyVectorBackend(brain)


def get_plugin_class():
    return NumpyVectorBackend
