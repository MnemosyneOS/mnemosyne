#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — Embedding provider adapters
==========================================

Two families live here:

**Built-in (zero-dependency).** :class:`BuiltinEmbedder` wraps Mnemosyne's own
:class:`~mnemosyne.utils.EmbeddingEngine` — a fixed-seed Johnson–Lindenstrauss
projection of TF-IDF into 128 dimensions.  It needs no download, no network and
no third-party package, and it is what runs when nobody configures anything.
Its weakness is real: it has no cross-lingual or paraphrase understanding.

**Remote.** The rest call a hosted embedding API over plain HTTP. This matters
more for embeddings than for chat, because the reference ecosystem's default is
a 1536-dimensional hosted model while the built-in one is 128-dimensional —
mixing the two in one vector index silently destroys recall.  The dimension
mismatch is therefore detected and reported rather than tolerated; see
``mnemosyne.api.config`` for how the guard is applied.

Provider keys mirror the component taxonomy exactly so a config block written
against the reference layout keeps working.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Dict, List, Optional

from .base import EmbedderProvider, ProviderUnavailable, _first_env
from .transport import HttpError, request_json

__all__ = ["BuiltinEmbedder", "OpenAIEmbedder", "OllamaEmbedder",
           "HuggingFaceEmbedder", "GeminiEmbedder", "VertexAIEmbedder",
           "TogetherEmbedder", "FastEmbedEmbedder", "LangChainEmbedder",
           "BedrockEmbedder", "get_embedder", "list_embedder_providers"]


# ---------------------------------------------------------------------------
# Built-in
# ---------------------------------------------------------------------------

class BuiltinEmbedder(EmbedderProvider):
    """Mnemosyne's dependency-free embedder — always available.

    Deterministic, offline and instant.  Intended as the default and as the
    automatic fallback when a configured remote embedder is unreachable, so a
    network outage degrades recall quality instead of breaking writes.
    """

    name = "builtin"
    dims = 128

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        from ..utils import EMBEDDING_DIM, EmbeddingEngine

        self.dims = int(self.config.get("dimensions") or self.config.get("dims")
                        or EMBEDDING_DIM)
        self._engine = EmbeddingEngine(dim=self.dims,
                                       seed=int(self.config.get("seed", 42)))

    def embed(self, texts: List[str]) -> List[List[float]]:
        return [list(self._engine.encode(t or "")) for t in texts]

    def similarity(self, a: List[float], b: List[float]) -> float:
        return self._engine.similarity(a, b)


# ---------------------------------------------------------------------------
# OpenAI-compatible
# ---------------------------------------------------------------------------

class OpenAICompatibleEmbedder(EmbedderProvider):
    """``POST {base_url}/embeddings`` with ``{"input": [...]}``.

    Handles both ``data[*].embedding`` (OpenAI, Azure, Together, Ollama's
    compatibility endpoint) and ``embeddings`` (Gemini's OpenAI shim), and
    sorts by ``index`` because some gateways do not preserve request order.
    """

    base_url = "https://api.openai.com/v1"
    default_model = "text-embedding-3-small"
    default_dims = 1536
    env_keys: tuple = ()
    requires_api_key = True
    key_header = "Authorization"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.api_key = (self.config.get("api_key")
                        or _first_env(f"MNEMOSYNE_EMBEDDER_{self.name.upper()}_API_KEY",
                                      *self.env_keys))
        self.base_url = (self.config.get("base_url")
                         or _first_env(f"MNEMOSYNE_EMBEDDER_{self.name.upper()}_BASE_URL")
                         or self.base_url).rstrip("/")
        self.model = self.config.get("model") or self.default_model
        self.dims = int(self.config.get("dimensions") or self.config.get("embedding_dims")
                        or self.default_dims)
        self.batch_size = int(self.config.get("batch_size", 100))
        self.timeout = float(self.config.get("timeout", 60))
        if self.requires_api_key and not self.api_key:
            raise ProviderUnavailable(
                self.name, "no API key configured",
                hint=("Set MNEMOSYNE_EMBEDDER_%s_API_KEY or %s in the environment."
                      % (self.name.upper(), "/".join(self.env_keys) or "the provider's variable")),
            )

    def _headers(self) -> Dict[str, str]:
        if not self.api_key:
            return {}
        if self.key_header == "api-key":
            return {"api-key": self.api_key}
        return {"Authorization": f"Bearer {self.api_key}"}

    def _extra_body(self) -> Dict[str, Any]:
        return {}

    def embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        out: List[List[float]] = []
        for start in range(0, len(texts), self.batch_size):
            chunk = [(t if t else " ") for t in texts[start:start + self.batch_size]]
            body: Dict[str, Any] = {"model": self.model, "input": chunk}
            # OpenAI's v3 models accept a truncation hint; harmless elsewhere.
            if self.config.get("dimensions"):
                body["dimensions"] = self.dims
            body.update(self._extra_body())
            payload = request_json(f"{self.base_url}/embeddings", payload=body,
                                   headers=self._headers(), timeout=self.timeout,
                                   retries=int(self.config.get("retries", 3)))
            out.extend(self._parse(payload))
        return out

    def _parse(self, payload: Any) -> List[List[float]]:
        if not isinstance(payload, dict):
            raise HttpError(200, f"{self.base_url}/embeddings",
                            f"unexpected payload: {str(payload)[:200]}")
        rows = payload.get("data")
        if rows is None:
            rows = payload.get("embeddings")
        if isinstance(rows, list) and rows and isinstance(rows[0], (list, tuple)):
            return [list(map(float, r)) for r in rows]
        if not isinstance(rows, list):
            err = payload.get("error")
            if err:
                raise HttpError(200, f"{self.base_url}/embeddings", str(err),
                                reason="upstream error")
            raise HttpError(200, f"{self.base_url}/embeddings", "no vectors in response")
        ordered = sorted(rows, key=lambda r: (r or {}).get("index", 0))
        vecs = [list(map(float, (r or {}).get("embedding") or [])) for r in ordered]
        if vecs and vecs[0]:
            self.dims = len(vecs[0])
        return vecs


class OpenAIEmbedder(OpenAICompatibleEmbedder):
    name = "openai"
    env_keys = ("OPENAI_API_KEY",)


class AzureOpenAIEmbedder(OpenAICompatibleEmbedder):
    name = "azure_openai"
    base_url = ""
    default_model = ""
    default_dims = 1536
    env_keys = ("AZURE_OPENAI_API_KEY",)
    key_header = "api-key"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        endpoint = (self.config.get("azure_endpoint")
                    or _first_env("AZURE_OPENAI_ENDPOINT"))
        if not endpoint:
            raise ProviderUnavailable("azure_openai", "azure_endpoint not set")
        self.api_version = (self.config.get("api_version")
                            or _first_env("AZURE_OPENAI_API_VERSION") or "2024-10-21")
        self.deployment = (self.config.get("azure_deployment")
                           or self.config.get("model")
                           or _first_env("AZURE_OPENAI_EMBEDDING_DEPLOYMENT"))
        if not self.deployment:
            raise ProviderUnavailable("azure_openai", "azure_deployment not set")
        self.base_url = f"{endpoint.rstrip('/')}/openai/deployments/{self.deployment}"
        self.model = self.deployment

    def embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        out: List[List[float]] = []
        for start in range(0, len(texts), self.batch_size):
            chunk = [(t or " ") for t in texts[start:start + self.batch_size]]
            payload = request_json(
                f"{self.base_url}/embeddings",
                payload={"model": self.model, "input": chunk},
                headers=self._headers(),
                params={"api-version": self.api_version},
                timeout=self.timeout,
            )
            out.extend(self._parse(payload))
        return out


class TogetherEmbedder(OpenAICompatibleEmbedder):
    name = "together"
    base_url = "https://api.together.xyz/v1"
    default_model = "BAAI/bge-large-en-v1.5"
    default_dims = 1024
    env_keys = ("TOGETHER_API_KEY",)


class LMStudioEmbedder(OpenAICompatibleEmbedder):
    name = "lmstudio"
    base_url = "http://localhost:1234/v1"
    default_model = "text-embedding-nomic-embed-text-v1.5"
    default_dims = 768
    requires_api_key = False


class OllamaEmbedder(EmbedderProvider):
    """Ollama's native ``/api/embed``.

    Preferred over the OpenAI-compatibility path because it also reports the
    model's true dimension in the response, which lets the engine verify that a
    configured ``embedding_dims`` is honest instead of trusting it.
    """

    name = "ollama"
    default_model = "nomic-embed-text"
    default_dims = 768

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.base_url = (self.config.get("ollama_base_url")
                         or self.config.get("base_url")
                         or _first_env("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
        if not self.base_url.startswith("http"):
            self.base_url = f"http://{self.base_url}"
        self.model = self.config.get("model") or self.default_model
        self.dims = int(self.config.get("dimensions") or self.config.get("embedding_dims")
                        or self.default_dims)
        self.timeout = float(self.config.get("timeout", 120))

    def embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        payload = request_json(
            f"{self.base_url}/api/embed",
            payload={"model": self.model, "input": [t or " " for t in texts]},
            timeout=self.timeout,
        )
        vecs = (payload or {}).get("embeddings")
        if not isinstance(vecs, list):
            # Older Ollama builds only expose /api/embeddings, one text at a time.
            vecs = []
            for t in texts:
                one = request_json(f"{self.base_url}/api/embeddings",
                                   payload={"model": self.model, "prompt": t or " "},
                                   timeout=self.timeout)
                vecs.append((one or {}).get("embedding") or [])
        vecs = [list(map(float, v or [])) for v in vecs]
        if vecs and vecs[0]:
            self.dims = len(vecs[0])
        return vecs


class HuggingFaceEmbedder(EmbedderProvider):
    """Hugging Face Inference API (``feature-extraction``).

    Returns a per-token tensor for many models, so the mean over tokens is
    taken and L2-normalised — the standard sentence-embedding reduction.
    """

    name = "huggingface"
    default_model = "sentence-transformers/all-MiniLM-L6-v2"
    default_dims = 384
    env_keys = ("HUGGINGFACE_API_KEY", "HF_TOKEN")

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.api_key = (self.config.get("api_key")
                        or _first_env("MNEMOSYNE_EMBEDDER_HUGGINGFACE_API_KEY",
                                      "HUGGINGFACE_API_KEY", "HF_TOKEN"))
        if not self.api_key:
            raise ProviderUnavailable("huggingface", "no API token configured",
                                      hint="Set HF_TOKEN or HUGGINGFACE_API_KEY.")
        self.model = self.config.get("model") or self.default_model
        self.base_url = (self.config.get("base_url")
                         or "https://api-inference.huggingface.co/pipeline/feature-extraction").rstrip("/")
        self.dims = int(self.config.get("embedding_dims") or self.default_dims)
        self.timeout = float(self.config.get("timeout", 60))

    def embed(self, texts: List[str]) -> List[List[float]]:
        out: List[List[float]] = []
        headers = {"Authorization": f"Bearer {self.api_key}"}
        for t in texts:
            payload = request_json(f"{self.base_url}/{self.model}",
                                   payload={"inputs": t or " ", "options": {"wait_for_model": True}},
                                   headers=headers, timeout=self.timeout)
            out.append(_reduce_tensor(payload))
        if out and out[0]:
            self.dims = len(out[0])
        return out


def _reduce_tensor(payload: Any) -> List[float]:
    """Mean-pool a nested tensor into one L2-normalised vector."""
    if not isinstance(payload, list) or not payload:
        return []
    if all(isinstance(x, (int, float)) for x in payload):
        vec = [float(x) for x in payload]
    else:
        rows = [r for r in payload if isinstance(r, list)]
        if rows and all(isinstance(r[0], (int, float)) for r in rows if r):
            # Token-level tensor: average across tokens.
            n = len([r for r in rows if r])
            vec = [sum(float(r[i]) for r in rows if r) / max(1, n)
                   for i in range(len(rows[0]))]
        else:
            flat = _flatten(payload)
            if not flat:
                return []
            dim = len(flat[0])
            vec = [sum(float(r[i]) for r in flat) / len(flat) for i in range(dim)]
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


def _flatten(nested: Any) -> List[List[float]]:
    if isinstance(nested, list) and nested and isinstance(nested[0], list):
        return [r for r in nested if isinstance(r, list) and
                all(isinstance(x, (int, float)) for x in r)]
    return []


class GeminiEmbedder(EmbedderProvider):
    """Google ``:embedContent`` / ``:batchEmbedContents``."""

    name = "gemini"
    default_model = "text-embedding-004"
    default_dims = 768
    env_keys = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.api_key = (self.config.get("api_key")
                        or _first_env("MNEMOSYNE_EMBEDDER_GEMINI_API_KEY",
                                      "GEMINI_API_KEY", "GOOGLE_API_KEY"))
        if not self.api_key:
            raise ProviderUnavailable("gemini", "no API key configured",
                                      hint="Set GEMINI_API_KEY.")
        self.base_url = (self.config.get("base_url")
                         or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self.model = self.config.get("model") or self.default_model
        self.dims = int(self.config.get("output_dimensionality")
                        or self.config.get("embedding_dims") or self.default_dims)
        self.timeout = float(self.config.get("timeout", 60))

    def embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        reqs = []
        for t in texts:
            req: Dict[str, Any] = {"model": f"models/{self.model}",
                                   "content": {"parts": [{"text": t or " "}]}}
            if self.config.get("output_dimensionality"):
                req["outputDimensionality"] = self.dims
            reqs.append(req)
        payload = request_json(
            f"{self.base_url}/models/{self.model}:batchEmbedContents",
            payload={"requests": reqs},
            headers={"x-goog-api-key": self.api_key},
            timeout=self.timeout,
        )
        vecs = [list(map(float, (e or {}).get("values") or []))
                for e in ((payload or {}).get("embeddings") or [])]
        if vecs and vecs[0]:
            self.dims = len(vecs[0])
        return vecs


class VertexAIEmbedder(EmbedderProvider):
    """Vertex AI ``:predict`` using an OAuth bearer token.

    Accepts a pre-minted ``access_token``; otherwise it shells out to
    ``gcloud auth print-access-token`` when that binary is present, so a machine
    already authenticated for gcloud needs no extra configuration.
    """

    name = "vertexai"
    default_model = "text-embedding-004"
    default_dims = 768
    env_keys = ("VERTEX_ACCESS_TOKEN",)

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.project = (self.config.get("project_id")
                        or _first_env("GOOGLE_CLOUD_PROJECT", "VERTEX_PROJECT_ID"))
        self.location = (self.config.get("location")
                         or _first_env("VERTEX_LOCATION") or "us-central1")
        self.model = self.config.get("model") or self.default_model
        self.dims = int(self.config.get("embedding_dims") or self.default_dims)
        self.timeout = float(self.config.get("timeout", 60))
        if not self.project:
            raise ProviderUnavailable("vertexai", "project_id not set",
                                      hint="Pass project_id, or set GOOGLE_CLOUD_PROJECT.")

    def _token(self) -> str:
        tok = self.config.get("access_token") or _first_env("VERTEX_ACCESS_TOKEN")
        if tok:
            return tok
        import subprocess

        try:
            out = subprocess.run(["gcloud", "auth", "print-access-token"],
                                 capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as e:
            raise ProviderUnavailable("vertexai", f"cannot obtain access token ({e})",
                                      hint="Pass access_token explicitly.") from None
        if out.returncode != 0 or not out.stdout.strip():
            raise ProviderUnavailable("vertexai", "gcloud auth failed",
                                      hint="Run 'gcloud auth login' or pass access_token.")
        return out.stdout.strip()

    def embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        url = (f"https://{self.location}-aiplatform.googleapis.com/v1/projects/"
               f"{self.project}/locations/{self.location}/publishers/google/"
               f"models/{self.model}:predict")
        payload = request_json(
            url,
            payload={"instances": [{"content": t or " "} for t in texts]},
            headers={"Authorization": f"Bearer {self._token()}"},
            timeout=self.timeout,
        )
        vecs = [list(map(float, ((p or {}).get("embeddings") or {}).get("values") or []))
                for p in ((payload or {}).get("predictions") or [])]
        if vecs and vecs[0]:
            self.dims = len(vecs[0])
        return vecs


# ---------------------------------------------------------------------------
# SDK-only providers
# ---------------------------------------------------------------------------

class FastEmbedEmbedder(EmbedderProvider):
    """``fastembed`` (ONNX, downloads the model once, then fully offline)."""

    name = "fastembed"
    requires = ("fastembed",)
    default_model = "BAAI/bge-small-en-v1.5"
    default_dims = 384

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        try:
            from fastembed import TextEmbedding
        except ImportError as e:
            raise ProviderUnavailable("fastembed", f"fastembed not installed ({e})",
                                      hint="pip install mnemosyne-os[fastembed]") from None
        self.model = self.config.get("model") or self.default_model
        self._model = TextEmbedding(model_name=self.model)
        self.dims = int(self.config.get("embedding_dims") or self.default_dims)

    def embed(self, texts: List[str]) -> List[List[float]]:
        vecs = [list(map(float, v)) for v in self._model.embed([t or " " for t in texts])]
        if vecs and vecs[0]:
            self.dims = len(vecs[0])
        return vecs


class LangChainEmbedder(EmbedderProvider):
    """Wraps any ``langchain_core.embeddings.Embeddings`` implementation."""

    name = "langchain"
    requires = ("langchain-core",)

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        emb = self.config.get("embeddings")
        if emb is None:
            try:
                from langchain_core.embeddings import FakeEmbeddings
            except ImportError as e:
                raise ProviderUnavailable("langchain", f"langchain-core missing ({e})") from None
            emb = FakeEmbeddings(size=int(self.config.get("embedding_dims", 384)))
        self._emb = emb
        self.dims = int(self.config.get("embedding_dims") or 0) or None
        self.model = str(self.config.get("model", "langchain"))

    def embed(self, texts: List[str]) -> List[List[float]]:
        vecs = [list(map(float, v)) for v in self._emb.embed_documents(
            [t or " " for t in texts])]
        if vecs and vecs[0]:
            self.dims = len(vecs[0])
        return vecs


class BedrockEmbedder(EmbedderProvider):
    """AWS Bedrock embeddings via ``boto3``'s ``invoke_model``."""

    name = "aws_bedrock"
    requires = ("boto3",)
    default_model = "amazon.titan-embed-text-v2:0"
    default_dims = 1024

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        try:
            import boto3
        except ImportError as e:
            raise ProviderUnavailable("aws_bedrock", f"boto3 not installed ({e})",
                                      hint="pip install mnemosyne-os[bedrock]") from None
        self.region = (self.config.get("aws_region")
                       or _first_env("AWS_REGION", "AWS_DEFAULT_REGION") or "us-east-1")
        self.model = self.config.get("model") or self.default_model
        self.dims = int(self.config.get("embedding_dims") or self.default_dims)
        self._client = boto3.client("bedrock-runtime", region_name=self.region)

    def embed(self, texts: List[str]) -> List[List[float]]:
        import json as _json

        out: List[List[float]] = []
        for t in texts:
            body: Dict[str, Any] = {"inputText": t or " "}
            if self.config.get("normalize") is not None:
                body["normalize"] = bool(self.config["normalize"])
            resp = self._client.invoke_model(modelId=self.model,
                                             body=_json.dumps(body))
            raw = resp["body"].read()
            parsed = _json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
            out.append(list(map(float, parsed.get("embedding") or [])))
        if out and out[0]:
            self.dims = len(out[0])
        return out


class HashingEmbedder(EmbedderProvider):
    """A dependency-free fallback for a *pinned* dimension.

    Exists so that a config demanding, say, 1536 dimensions still works with no
    network: it hashes tokens into the requested number of buckets.  Recall
    quality equals the built-in embedder's, so it is never chosen automatically —
    only when a dimension must be honoured offline.
    """

    name = "hashing"
    dims = 1536

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.dims = int(self.config.get("dimensions") or self.config.get("embedding_dims")
                        or self.config.get("dims") or 1536)

    def embed(self, texts: List[str]) -> List[List[float]]:
        from ..utils import _tokenize

        out: List[List[float]] = []
        for text in texts:
            vec = [0.0] * self.dims
            toks = _tokenize(text or "")
            if not toks:
                out.append(vec)
                continue
            for tok in toks:
                h = hashlib.md5(tok.encode("utf-8")).digest()
                idx = int.from_bytes(h[:4], "little") % self.dims
                sign = 1.0 if h[4] & 1 else -1.0
                vec[idx] += sign
            norm = math.sqrt(sum(v * v for v in vec))
            out.append([v / norm for v in vec] if norm else vec)
        return out


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: Dict[str, type] = {
    "builtin": BuiltinEmbedder,
    "hashing": HashingEmbedder,
    "openai": OpenAIEmbedder,
    "azure_openai": AzureOpenAIEmbedder,
    "ollama": OllamaEmbedder,
    "huggingface": HuggingFaceEmbedder,
    "gemini": GeminiEmbedder,
    "vertexai": VertexAIEmbedder,
    "together": TogetherEmbedder,
    "lmstudio": LMStudioEmbedder,
    "fastembed": FastEmbedEmbedder,
    "langchain": LangChainEmbedder,
    "aws_bedrock": BedrockEmbedder,
}

_DEFAULT_DIMS = {
    "builtin": 128,
    "hashing": 1536,
    "openai": 1536,
    "azure_openai": 1536,
    "ollama": 768,
    "huggingface": 384,
    "gemini": 768,
    "vertexai": 768,
    "together": 1024,
    "lmstudio": 768,
    "fastembed": 384,
    "aws_bedrock": 1024,
}


def get_embedder(config: Optional[Dict[str, Any]] = None) -> EmbedderProvider:
    """Build the embedder described by *config*.

    An empty config yields the built-in zero-dependency embedder.  Note there is
    deliberately **no** silent substitution for a *named* provider: if the user
    asked for ``openai`` and no key is present, that is an error worth surfacing,
    because the alternative is writing vectors that nothing can query.
    """
    config = config or {}
    name = (config.get("provider") or "builtin").strip().lower()
    if name in ("none", "", "off", "disabled"):
        name = "builtin"
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ProviderUnavailable(
            name, "unknown embedding provider",
            hint="Known providers: " + ", ".join(sorted(_REGISTRY)),
        )
    return cls(config.get("config") or config.get("params") or {})


def default_dims(provider: str) -> int:
    """Report the documented dimension for *provider* (0 if unknown)."""
    return _DEFAULT_DIMS.get((provider or "").strip().lower(), 0)


def list_embedder_providers() -> List[str]:
    return sorted(_REGISTRY)
