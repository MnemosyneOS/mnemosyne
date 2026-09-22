#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — LLM provider adapters
====================================

Every provider here is reachable over plain HTTP and therefore needs **no**
third-party package.  The bulk of the ecosystem has converged on the OpenAI
chat-completions wire format, so :class:`OpenAICompatibleLLM` implements that
once and each vendor is a small subclass supplying a base URL, a default model
and the environment variable its key is read from.

The two exceptions are handled explicitly:

* ``langchain`` and ``aws_bedrock`` cannot be reached without their SDKs, so
  they declare a requirement and fail with an actionable message instead of an
  opaque ImportError.
* ``rules`` is Mnemosyne's own extractor.  It is the reason the engine still
  works with *zero* configuration: no model, no network, no key.

Environment variables consulted, in order: explicit config, then
``MNEMOSYNE_LLM_*``, then the vendor's own conventional name (``OPENAI_API_KEY``
and friends), so an existing shell environment keeps working unchanged.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from .base import (LLMProvider, ProviderUnavailable, _first_env)
from .transport import HttpError, extract_text, request_json, request_stream, split_sse_json

__all__ = ["OpenAICompatibleLLM", "OllamaLLM", "AnthropicLLM", "GeminiLLM",
           "RuleBasedLLM", "LangChainLLM", "BedrockLLM", "get_llm"]


# ---------------------------------------------------------------------------
# OpenAI-compatible (the majority)
# ---------------------------------------------------------------------------

class OpenAICompatibleLLM(LLMProvider):
    """Chat completions over an OpenAI-shaped ``/chat/completions`` endpoint.

    Subclasses set :attr:`base_url` / :attr:`default_model` / :attr:`env_keys`.
    Set ``structured_mode`` to control how JSON output is requested:

    ``"schema"``
        Send ``response_format={"type": "json_schema", ...}`` — strictest, and
        what ``openai_structured`` selects.
    ``"object"``
        Send ``response_format={"type": "json_object"}`` — widely supported.
    ``"prompt"`` (default)
        Ask for JSON in the prompt only.  Used for vendors that reject
        ``response_format`` outright.
    """

    base_url = "https://api.openai.com/v1"
    default_model = "gpt-4o-mini"
    env_keys: tuple = ()
    structured_mode = "object"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.api_key = (
            self.config.get("api_key")
            or _first_env(f"MNEMOSYNE_LLM_{self.name.upper()}_API_KEY", *self.env_keys)
        )
        self.base_url = (self.config.get("base_url")
                         or _first_env(f"MNEMOSYNE_LLM_{self.name.upper()}_BASE_URL")
                         or self.base_url).rstrip("/")
        self.model = self.config.get("model") or self.default_model
        self.temperature = float(self.config.get("temperature", 0.0))
        self.max_tokens = self.config.get("max_tokens")
        self.timeout = float(self.config.get("timeout", 60))
        self.extra_headers = dict(self.config.get("extra_headers") or {})
        # A local server (Ollama / LM Studio / vLLM) needs no key; only demand
        # one when the endpoint is a remote API.
        if self.config.get("requires_api_key", self.requires_api_key) and not self.api_key:
            raise ProviderUnavailable(
                self.name, "no API key configured",
                hint=("Set MNEMOSYNE_LLM_%s_API_KEY (or %s) in the environment, "
                      "or pass api_key in the '%s' config block."
                      % (self.name.upper(),
                         "/".join(self.env_keys) or "the provider's usual variable",
                         self.name)),
            )

    #: Remote vendors require a key; local servers override this to ``False``.
    requires_api_key = True

    @property
    def _headers(self) -> Dict[str, str]:
        h = dict(self.extra_headers)
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _body(self, messages: List[Dict[str, str]], **kwargs: Any) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "model": kwargs.get("model") or self.model,
            "messages": messages,
        }
        temp = kwargs.get("temperature", self.temperature)
        if temp is not None:
            body["temperature"] = temp
        mt = kwargs.get("max_tokens", self.max_tokens)
        if mt:
            body["max_tokens"] = int(mt)
        schema = kwargs.get("schema")
        mode = kwargs.get("structured_mode", self.structured_mode)
        if schema and mode == "schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "memory_extraction",
                                "strict": False,
                                "schema": schema},
            }
        elif schema and mode == "object":
            body["response_format"] = {"type": "json_object"}
        return body

    def complete(self, messages: List[Dict[str, str]], **kwargs: Any) -> str:
        payload = request_json(
            f"{self.base_url}/chat/completions",
            payload=self._body(messages, **kwargs),
            headers=self._headers,
            timeout=self.timeout,
            retries=int(self.config.get("retries", 3)),
        )
        return _openai_text(payload)

    def stream(self, messages: List[Dict[str, str]], **kwargs: Any):
        """Yield assistant text chunks as they arrive."""
        for chunk in request_stream(
            f"{self.base_url}/chat/completions",
            payload=self._body(messages, **kwargs),
            headers=self._headers,
            timeout=self.timeout,
        ):
            payload = split_sse_json(chunk)
            if payload:
                text = extract_text(payload)
                if text:
                    yield text


def _openai_text(payload: Any) -> str:
    """Pull the assistant message out of a chat-completions response."""
    if not isinstance(payload, dict):
        return "" if payload is None else str(payload)
    choices = payload.get("choices") or []
    if not choices:
        # Some gateways report failures in-band with HTTP 200.
        err = payload.get("error")
        if err:
            raise HttpError(200, "chat/completions", str(err), reason="upstream error")
        return ""
    msg = (choices[0] or {}).get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        # Multi-part responses: concatenate the text parts.
        return "".join(p.get("text", "") for p in content
                       if isinstance(p, dict) and p.get("type") in (None, "text"))
    return content or ""


# -- concrete vendors -------------------------------------------------------

class OpenAILLM(OpenAICompatibleLLM):
    name = "openai"
    base_url = "https://api.openai.com/v1"
    default_model = "gpt-4o-mini"
    env_keys = ("OPENAI_API_KEY",)


class OpenAIStructuredLLM(OpenAILLM):
    """OpenAI with strict JSON-schema response formatting."""
    name = "openai_structured"
    structured_mode = "schema"


class AzureOpenAILLM(OpenAICompatibleLLM):
    """Azure OpenAI.  The deployment name is part of the URL, not the body."""
    name = "azure_openai"
    base_url = ""
    default_model = ""
    env_keys = ("AZURE_OPENAI_API_KEY",)
    structured_mode = "prompt"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        endpoint = (self.config.get("azure_endpoint")
                    or _first_env("MNEMOSYNE_LLM_AZURE_OPENAI_ENDPOINT",
                                  "AZURE_OPENAI_ENDPOINT"))
        if not endpoint:
            raise ProviderUnavailable("azure_openai", "azure_endpoint not set",
                                      hint="Pass azure_endpoint in the config block.")
        self.api_version = (self.config.get("api_version")
                            or _first_env("AZURE_OPENAI_API_VERSION")
                            or "2024-10-21")
        self.deployment = (self.config.get("azure_deployment")
                           or self.config.get("model")
                           or _first_env("AZURE_OPENAI_DEPLOYMENT"))
        if not self.deployment:
            raise ProviderUnavailable("azure_openai", "azure_deployment not set",
                                      hint="Pass azure_deployment in the config block.")
        self.base_url = f"{endpoint.rstrip('/')}/openai/deployments/{self.deployment}"
        self.model = self.deployment
        for k, v in (self.config.get("default_headers") or {}).items():
            self.extra_headers.setdefault(k, v)

    @property
    def _headers(self) -> Dict[str, str]:
        h = dict(self.extra_headers)
        if self.api_key:
            h["api-key"] = self.api_key
        return h

    def complete(self, messages, **kwargs: Any) -> str:
        payload = request_json(
            f"{self.base_url}/chat/completions",
            payload=self._body(messages, **kwargs),
            headers=self._headers,
            params={"api-version": self.api_version},
            timeout=self.timeout,
        )
        return _openai_text(payload)


class AzureOpenAIStructuredLLM(AzureOpenAILLM):
    name = "azure_openai_structured"
    structured_mode = "schema"


class DeepSeekLLM(OpenAICompatibleLLM):
    name = "deepseek"
    base_url = "https://api.deepseek.com/v1"
    default_model = "deepseek-chat"
    env_keys = ("DEEPSEEK_API_KEY",)


class XAILLM(OpenAICompatibleLLM):
    name = "xai"
    base_url = "https://api.x.ai/v1"
    default_model = "grok-2-latest"
    env_keys = ("XAI_API_KEY",)


class GroqLLM(OpenAICompatibleLLM):
    name = "groq"
    base_url = "https://api.groq.com/openai/v1"
    default_model = "llama-3.3-70b-versatile"
    env_keys = ("GROQ_API_KEY",)


class TogetherLLM(OpenAICompatibleLLM):
    name = "together"
    base_url = "https://api.together.xyz/v1"
    default_model = "meta-llama/Llama-3.3-70B-Instruct-Turbo"
    env_keys = ("TOGETHER_API_KEY",)


class MiniMaxLLM(OpenAICompatibleLLM):
    name = "minimax"
    base_url = "https://api.minimax.chat/v1"
    default_model = "MiniMax-Text-01"
    env_keys = ("MINIMAX_API_KEY",)


class SarvamLLM(OpenAICompatibleLLM):
    name = "sarvam"
    base_url = "https://api.sarvam.ai/v1"
    default_model = "sarvam-m"
    env_keys = ("SARVAM_API_KEY",)


class OpenRouterLLM(OpenAICompatibleLLM):
    """Not in the reference vendor list, but the cheapest way to reach many
    models through one key, so it is registered as an extra."""
    name = "openrouter"
    base_url = "https://openrouter.ai/api/v1"
    default_model = "openai/gpt-4o-mini"
    env_keys = ("OPENROUTER_API_KEY",)


class LiteLLMProxyLLM(OpenAICompatibleLLM):
    """A LiteLLM proxy exposing the OpenAI surface."""
    name = "litellm"
    base_url = "http://localhost:4000/v1"
    default_model = "gpt-4o-mini"
    env_keys = ("LITELLM_API_KEY",)
    requires_api_key = False


class LMStudioLLM(OpenAICompatibleLLM):
    name = "lmstudio"
    base_url = "http://localhost:1234/v1"
    default_model = "local-model"
    requires_api_key = False


class VLLMLLM(OpenAICompatibleLLM):
    name = "vllm"
    base_url = "http://localhost:8000/v1"
    default_model = "Qwen/Qwen2.5-7B-Instruct"
    requires_api_key = False


class OllamaLLM(LLMProvider):
    """Ollama's native ``/api/chat`` endpoint.

    Registered as its own adapter rather than an OpenAI shim because Ollama is
    the single most common fully-local backend and its native endpoint supports
    ``keep_alive`` and model pulls that the compatibility layer does not.
    """

    name = "ollama"
    default_model = "llama3.2"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.base_url = (self.config.get("ollama_base_url")
                         or self.config.get("base_url")
                         or _first_env("MNEMOSYNE_LLM_OLLAMA_BASE_URL", "OLLAMA_HOST")
                         or "http://localhost:11434").rstrip("/")
        if self.base_url.startswith("ollama.ai"):
            self.base_url = f"https://{self.base_url}"
        if not self.base_url.startswith("http"):
            self.base_url = f"http://{self.base_url}"
        self.model = self.config.get("model") or self.default_model
        self.timeout = float(self.config.get("timeout", 120))

    def complete(self, messages: List[Dict[str, str]], **kwargs: Any) -> str:
        body: Dict[str, Any] = {
            "model": kwargs.get("model") or self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": kwargs.get("temperature",
                                                  self.config.get("temperature", 0.0))},
        }
        if self.config.get("keep_alive") is not None:
            body["keep_alive"] = self.config["keep_alive"]
        if kwargs.get("schema") or self.config.get("format_json"):
            body["format"] = "json"
        payload = request_json(f"{self.base_url}/api/chat", payload=body,
                               timeout=self.timeout)
        if isinstance(payload, dict):
            msg = payload.get("message") or {}
            return msg.get("content", "") or ""
        return str(payload or "")


class AnthropicLLM(LLMProvider):
    """Anthropic Messages API.

    Anthropic separates the system prompt from the turn list, so the leading
    ``system`` message is hoisted out of *messages* before the request is built.
    """

    name = "anthropic"
    default_model = "claude-3-5-sonnet-latest"
    env_keys = ("ANTHROPIC_API_KEY",)

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.api_key = (self.config.get("api_key")
                        or _first_env("MNEMOSYNE_LLM_ANTHROPIC_API_KEY",
                                      "ANTHROPIC_API_KEY"))
        if not self.api_key:
            raise ProviderUnavailable("anthropic", "no API key configured",
                                      hint="Set ANTHROPIC_API_KEY.")
        self.base_url = (self.config.get("base_url")
                         or _first_env("MNEMOSYNE_LLM_ANTHROPIC_BASE_URL")
                         or "https://api.anthropic.com/v1").rstrip("/")
        self.model = self.config.get("model") or self.default_model
        self.max_tokens = int(self.config.get("max_tokens", 4096))
        self.timeout = float(self.config.get("timeout", 60))

    def complete(self, messages: List[Dict[str, str]], **kwargs: Any) -> str:
        system_parts: List[str] = []
        turns: List[Dict[str, Any]] = []
        for m in messages:
            role = m.get("role")
            content = m.get("content", "")
            if role == "system":
                system_parts.append(content if isinstance(content, str)
                                    else str(content))
            else:
                turns.append({"role": "assistant" if role == "assistant" else "user",
                              "content": content})
        if not turns:
            turns = [{"role": "user", "content": "\n".join(system_parts) or "."}]

        body: Dict[str, Any] = {
            "model": kwargs.get("model") or self.model,
            "max_tokens": int(kwargs.get("max_tokens", self.max_tokens)),
            "messages": turns,
        }
        temp = kwargs.get("temperature", self.config.get("temperature"))
        if temp is not None:
            body["temperature"] = temp
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        if kwargs.get("schema"):
            body["tools"] = [{
                "name": "emit_memories",
                "description": "Return the extracted memories as structured JSON.",
                "input_schema": kwargs["schema"],
            }]
            body["tool_choice"] = {"type": "tool", "name": "emit_memories"}

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": str(self.config.get("anthropic_version", "2023-06-01")),
        }
        payload = request_json(f"{self.base_url}/messages", payload=body,
                               headers=headers, timeout=self.timeout)
        if not isinstance(payload, dict):
            return str(payload or "")
        blocks = payload.get("content") or []
        out: List[str] = []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                out.append(b.get("text", ""))
            elif b.get("type") == "tool_use":
                import json as _json

                out.append(_json.dumps(b.get("input") or {}, ensure_ascii=False))
        return "".join(out)


class GeminiLLM(LLMProvider):
    """Google Gemini ``generateContent``."""

    name = "gemini"
    default_model = "gemini-2.0-flash"
    env_keys = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.api_key = (self.config.get("api_key")
                        or _first_env("MNEMOSYNE_LLM_GEMINI_API_KEY",
                                      "GEMINI_API_KEY", "GOOGLE_API_KEY"))
        if not self.api_key:
            raise ProviderUnavailable("gemini", "no API key configured",
                                      hint="Set GEMINI_API_KEY or GOOGLE_API_KEY.")
        self.base_url = (self.config.get("base_url")
                         or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self.model = self.config.get("model") or self.default_model
        self.timeout = float(self.config.get("timeout", 60))

    def complete(self, messages: List[Dict[str, str]], **kwargs: Any) -> str:
        system_parts, contents = [], []
        for m in messages:
            role = m.get("role")
            text = m.get("content", "")
            if not isinstance(text, str):
                text = str(text)
            if role == "system":
                system_parts.append(text)
                continue
            contents.append({"role": "model" if role == "assistant" else "user",
                             "parts": [{"text": text}]})
        if not contents:
            contents = [{"role": "user", "parts": [{"text": "\n".join(system_parts) or "."}]}]

        body: Dict[str, Any] = {"contents": contents}
        if system_parts:
            body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        gen: Dict[str, Any] = {}
        temp = kwargs.get("temperature", self.config.get("temperature"))
        if temp is not None:
            gen["temperature"] = temp
        if kwargs.get("max_tokens"):
            gen["maxOutputTokens"] = int(kwargs["max_tokens"])
        if kwargs.get("schema"):
            gen["responseMimeType"] = "application/json"
            gen["responseSchema"] = _gemini_schema(kwargs["schema"])
        elif self.config.get("format_json"):
            gen["responseMimeType"] = "application/json"
        if gen:
            body["generationConfig"] = gen

        payload = request_json(
            f"{self.base_url}/models/{kwargs.get('model') or self.model}:generateContent",
            payload=body,
            headers={"x-goog-api-key": self.api_key},
            timeout=self.timeout,
        )
        if not isinstance(payload, dict):
            return str(payload or "")
        cands = payload.get("candidates") or []
        if not cands:
            blocked = (payload.get("promptFeedback") or {}).get("blockReason")
            if blocked:
                raise HttpError(200, "gemini:generateContent",
                                f"prompt blocked: {blocked}", reason="safety block")
            return ""
        parts = ((cands[0] or {}).get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


def _gemini_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Translate a JSON-Schema fragment into Gemini's OpenAPI-ish dialect.

    Gemini rejects ``additionalProperties`` and does not accept ``anyOf`` with
    the same shape, so constraints it cannot express are dropped rather than
    forwarded — the prompt still carries the full instruction.
    """
    if not isinstance(schema, dict):
        return schema
    out: Dict[str, Any] = {}
    for k, v in schema.items():
        if k in ("additionalProperties", "$schema", "title", "default", "examples"):
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {pk: _gemini_schema(pv) for pk, pv in v.items()}
        elif k == "items":
            out[k] = _gemini_schema(v)
        elif k == "anyOf":
            variants = [_gemini_schema(x) for x in v if isinstance(x, dict)]
            prim = [x for x in variants if x.get("type") not in ("null", None)]
            if len(prim) == 1:
                out.update(prim[0])
            else:
                out["anyOf"] = variants
        else:
            out[k] = v
    out.setdefault("type", "object") if "properties" in out else None
    return out


# ---------------------------------------------------------------------------
# SDK-only providers
# ---------------------------------------------------------------------------

class LangChainLLM(LLMProvider):
    """Wraps any ``langchain_core.language_models`` chat model.

    Requires ``langchain-core``.  Accepted config keys: ``model`` (provider
    identifier such as ``"openai:gpt-4o-mini"``) or ``llm`` (an already-built
    LangChain chat model instance).
    """

    name = "langchain"
    requires = ("langchain-core",)

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        try:
            from langchain_core.messages import (AIMessage, HumanMessage,
                                                 SystemMessage)
        except ImportError as e:
            raise ProviderUnavailable(
                "langchain", f"langchain-core not installed ({e})",
                hint="pip install mnemosyne-os[langchain]",
            ) from None
        self._msgs = (SystemMessage, HumanMessage, AIMessage)
        llm = self.config.get("llm")
        if llm is not None:
            self._llm = llm
        else:
            from langchain.chat_models import init_chat_model

            spec = self.config.get("model", "openai:gpt-4o-mini")
            self._llm = init_chat_model(spec)
        self.model = str(self.config.get("model", "langchain"))

    def complete(self, messages: List[Dict[str, str]], **kwargs: Any) -> str:
        SystemMessage, HumanMessage, AIMessage = self._msgs
        built = []
        for m in messages:
            role, content = m.get("role"), m.get("content", "")
            if role == "system":
                built.append(SystemMessage(content=content))
            elif role == "assistant":
                built.append(AIMessage(content=content))
            else:
                built.append(HumanMessage(content=content))
        result = self._llm.invoke(built)
        return getattr(result, "content", None) or str(result)


class BedrockLLM(LLMProvider):
    """AWS Bedrock via ``boto3`` (Converse API).

    Requires ``boto3``.  Uses the region/credential chain boto3 resolves, so
    no explicit credentials are needed on an instance with an IAM role.
    """

    name = "aws_bedrock"
    requires = ("boto3",)
    default_model = "anthropic.claude-3-5-sonnet-20241022-v2:0"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        try:
            import boto3
        except ImportError as e:
            raise ProviderUnavailable("aws_bedrock", f"boto3 not installed ({e})",
                                      hint="pip install mnemosyne-os[bedrock]") from None
        self.region = (self.config.get("aws_region")
                       or _first_env("AWS_REGION", "AWS_DEFAULT_REGION") or "us-east-1")
        self.model = (self.config.get("model")
                      or _first_env("AWS_BEDROCK_MODEL") or self.default_model)
        self._client = boto3.client("bedrock-runtime", region_name=self.region,
                                    **({"aws_access_key_id": self.config["aws_access_key_id"],
                                        "aws_secret_access_key": self.config["aws_secret_access_key"]}
                                       if self.config.get("aws_access_key_id") else {}))

    def complete(self, messages: List[Dict[str, str]], **kwargs: Any) -> str:
        system, turns = [], []
        for m in messages:
            role, content = m.get("role"), m.get("content", "")
            if role == "system":
                system.append({"text": content})
            else:
                turns.append({"role": "assistant" if role == "assistant" else "user",
                              "content": [{"text": content}]})
        req: Dict[str, Any] = {"modelId": kwargs.get("model") or self.model,
                               "messages": turns}
        if system:
            req["system"] = system
        inference: Dict[str, Any] = {}
        temp = kwargs.get("temperature", self.config.get("temperature"))
        if temp is not None:
            inference["temperature"] = temp
        if kwargs.get("max_tokens"):
            inference["maxTokens"] = int(kwargs["max_tokens"])
        if inference:
            req["inferenceConfig"] = inference
        resp = self._client.converse(**req)
        blocks = ((resp.get("output") or {}).get("message") or {}).get("content") or []
        return "".join(b.get("text", "") for b in blocks if isinstance(b, dict))


# ---------------------------------------------------------------------------
# Zero-configuration fallback
# ---------------------------------------------------------------------------

class RuleBasedLLM(LLMProvider):
    """Mnemosyne's own extractor — no model, no network, no key.

    Selected by ``llm: {provider: rules}`` and used automatically whenever no
    other provider is configured or every configured provider is unavailable.
    It does not call a model: it splits the conversation into candidate
    statements and keeps the ones :meth:`MemoryBrain.should_remember` accepts.
    Recall quality is lower than an LLM's, but behaviour is deterministic,
    offline and free — which is exactly the guarantee the core advertises.

    ``complete`` therefore answers with a JSON array of verbatim sentences,
    which is the same shape a real model would be asked to produce, so the
    extraction pipeline does not branch on which provider is in use.
    """

    name = "rules"
    is_rule_based = True

    def complete(self, messages: List[Dict[str, str]], **kwargs: Any) -> str:
        import json
        import re

        user_text = "\n".join(m.get("content", "") for m in messages
                              if m.get("role") in ("user", "human")
                              and isinstance(m.get("content"), str))
        if not user_text:
            user_text = "\n".join(m.get("content", "") for m in messages
                                  if isinstance(m.get("content"), str))

        sentences: List[str] = []
        for raw in re.split(r"(?<=[。！？!?\n])|(?<=\.)\s+", user_text):
            s = (raw or "").strip()
            if len(s) < 4 or len(s) > 600:
                continue
            sentences.append(s)
        seen, unique = set(), []
        for s in sentences:
            key = s.lower()
            if key not in seen:
                seen.add(key)
                unique.append(s)
        # Cap the output so a long transcript cannot flood the write path; the
        # caller applies its own importance filter on top.
        return json.dumps({"facts": unique[:40]}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Registry helper
# ---------------------------------------------------------------------------

_REGISTRY: Dict[str, type] = {
    "rules": RuleBasedLLM,
    "openai": OpenAILLM,
    "openai_structured": OpenAIStructuredLLM,
    "azure_openai": AzureOpenAILLM,
    "azure_openai_structured": AzureOpenAIStructuredLLM,
    "ollama": OllamaLLM,
    "anthropic": AnthropicLLM,
    "gemini": GeminiLLM,
    "groq": GroqLLM,
    "together": TogetherLLM,
    "deepseek": DeepSeekLLM,
    "minimax": MiniMaxLLM,
    "xai": XAILLM,
    "sarvam": SarvamLLM,
    "openrouter": OpenRouterLLM,
    "litellm": LiteLLMProxyLLM,
    "lmstudio": LMStudioLLM,
    "vllm": VLLMLLM,
    "langchain": LangChainLLM,
    "aws_bedrock": BedrockLLM,
}

#: Vendors whose key is read from a conventional variable, used by the registry
#: to report which providers are usable right now.
_ENV_KEYS = {
    "openai": ("OPENAI_API_KEY",),
    "openai_structured": ("OPENAI_API_KEY",),
    "azure_openai": ("AZURE_OPENAI_API_KEY",),
    "azure_openai_structured": ("AZURE_OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "groq": ("GROQ_API_KEY",),
    "together": ("TOGETHER_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "minimax": ("MINIMAX_API_KEY",),
    "xai": ("XAI_API_KEY",),
    "sarvam": ("SARVAM_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
}


def get_llm(config: Optional[Dict[str, Any]] = None) -> LLMProvider:
    """Build the LLM described by *config* (``{"provider": ..., "config": {...}}``).

    Falls back to :class:`RuleBasedLLM` when *config* is empty, so a caller that
    configured nothing still gets a working engine rather than an exception.
    """
    config = config or {}
    name = (config.get("provider") or "rules").strip().lower()
    if name in ("none", "", "off", "disabled"):
        name = "rules"
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ProviderUnavailable(
            name, "unknown LLM provider",
            hint="Known providers: " + ", ".join(sorted(_REGISTRY)),
        )
    return cls(config.get("config") or config.get("params") or {})


def list_llm_providers() -> List[str]:
    return sorted(_REGISTRY)
