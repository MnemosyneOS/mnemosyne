"""Holographic Reduced Representation (HRR) plugin.

A simplified HRR implementation using numpy for vector symbolic
architecture operations: encode entities, bind with random keys,
bundle multiple vectors, and probe.

Zero-dependency core: gracefully degrades if numpy is not installed.

Usage::

    from plugins.hrr_plugin import SimpleHRR
    hrr = SimpleHRR(vector_dim=1024)
    hrr.encode("Apple", "company founded 1976")
    result = hrr.probe(hrr.encode("Apple", None))
    related = hrr.reason("company")
"""
import hashlib
import random

try:
    import numpy as np
    _HAS_NUMPY = True
    _np = np
except ImportError:
    _HAS_NUMPY = False
    _np = None

__all__ = ["SimpleHRR", "HRRPlugin"]


def _stable_hash(seed_text, length):
    """Generate a deterministic random vector from a text seed.

    Uses hashlib for reproducibility without numpy's RNG.
    """
    if _HAS_NUMPY:
        h = hashlib.sha256(seed_text.encode("utf-8")).digest()
        seed = int.from_bytes(h[:8], "big") % (2 ** 32 - 1)
        rng = _np.random.RandomState(seed)
        vec = rng.randn(length).astype(_np.float64)
        # Normalize
        norm = _np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec
    else:
        h = hashlib.sha256(seed_text.encode("utf-8")).digest()
        values = []
        for i in range(length):
            idx = (h[i % len(h)] + i * 31) % 256
            val = (idx / 128.0) - 1.0  # range [-1, 1)
            values.append(val)
        norm = sum(v * v for v in values) ** 0.5
        if norm > 0:
            values = [v / norm for v in values]
        return values


class SimpleHRR:
    """Simplified HRR with encode, bind, bundle, and probe operations.

    Zero-dependency (optional numpy acceleration).
    """

    def __init__(self, vector_dim=1024):
        self.vector_dim = vector_dim
        self.memory = {}  # label -> superposed vector
        self._am = None  # associative memory (bundle of all encodings)

    def _encode_vector(self, text):
        """Encode text into a random vector (deterministic)."""
        return _stable_hash(text, self.vector_dim)

    def _random_key(self):
        """Generate a random key for binding."""
        return _stable_hash(str(random.random()), self.vector_dim)

    def _bind(self, a, b):
        """Bind two vectors (circular convolution in HRR ≈ element-wise multiply for simplification)."""
        if _HAS_NUMPY:
            return _np.multiply(a, b)
        return [a[i] * b[i] for i in range(len(a))]

    def _bundle(self, vectors):
        """Bundle a list of vectors (sum and normalize)."""
        if not vectors:
            return self._encode_vector("")
        if _HAS_NUMPY:
            result = _np.zeros(self.vector_dim, dtype=_np.float64)
            for v in vectors:
                result = _np.add(result, v)
            norm = _np.linalg.norm(result)
            if norm > 0:
                result = result / norm
            return result
        result = [0.0] * self.vector_dim
        for v in vectors:
            for i in range(len(v)):
                result[i] += v[i]
        norm = sum(v * v for v in result) ** 0.5
        if norm > 0:
            result = [v / norm for v in result]
        return result

    def _cosine_sim(self, a, b):
        """Compute cosine similarity between two vectors."""
        if _HAS_NUMPY:
            dot = _np.dot(a, b)
            norm_a = _np.linalg.norm(a)
            norm_b = _np.linalg.norm(b)
            if norm_a == 0 or norm_b == 0:
                return 0.0
            return float(dot / (norm_a * norm_b))
        dot = sum(a[i] * b[i] for i in range(len(a)))
        norm_a = sum(v * v for v in a) ** 0.5
        norm_b = sum(v * v for v in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def encode(self, label, content=None):
        """Encode a label (and optionally content) into a vector.

        Returns the encoded vector (not stored in memory).
        """
        vec = self._encode_vector(label)
        if content:
            content_vec = self._encode_vector(content)
            vec = self._bind(vec, content_vec)
        return vec

    def store(self, label, content=None):
        """Encode and store a label+content pair in memory."""
        vec = self.encode(label, content)
        self.memory[label] = vec
        # Update associative memory (bundle of all stored vectors)
        if self._am is None:
            self._am = vec
        else:
            if _HAS_NUMPY:
                self._am = _np.add(self._am, vec)
                norm = _np.linalg.norm(self._am)
                if norm > 0:
                    self._am = self._am / norm
            else:
                self._am = [self._am[i] + vec[i] for i in range(len(vec))]
                norm = sum(v * v for v in self._am) ** 0.5
                if norm > 0:
                    self._am = [v / norm for v in self._am]
        return vec

    def probe(self, query_vector, k=5):
        """Probe the memory with a query vector, returning top-k matches.

        Returns list of (label, similarity_score) sorted by score.
        """
        results = []
        for label, mem_vec in self.memory.items():
            sim = self._cosine_sim(query_vector, mem_vec)
            results.append((label, sim))
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:k]

    def reason(self, query_label, k=5):
        """Reason about a query label: encode it, then probe memory.

        This simulates reasoning by binding the query with the
        associative memory to find related items.
        """
        if query_label not in self.memory:
            # Encode a new query
            q_vec = self.encode(query_label)
        else:
            q_vec = self.memory[query_label]

        # Bind with associative memory for reasoning
        if self._am is not None:
            if _HAS_NUMPY:
                reasoned = _np.multiply(q_vec, self._am)
            else:
                reasoned = [q_vec[i] * self._am[i] for i in range(len(q_vec))]
        else:
            reasoned = q_vec

        return self.probe(reasoned, k=k)

    def clear(self):
        """Clear all stored memories."""
        self.memory = {}
        self._am = None


class HRRPlugin:
    """HRR plugin wrapper for MemoryBrain integration.

    Provides HRR encode/probe/reason as a memory plugin.
    """

    def __init__(self, vector_dim=1024):
        self.hrr = SimpleHRR(vector_dim)

    def encode(self, label, content=None):
        return self.hrr.encode(label, content)

    def store(self, label, content=None):
        return self.hrr.store(label, content)

    def probe(self, query_vector, k=5):
        return self.hrr.probe(query_vector, k)

    def reason(self, query_label, k=5):
        return self.hrr.reason(query_label, k)

    def available(self):
        return True  # Works with or without numpy
