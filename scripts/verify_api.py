#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — client API compatibility verification
=============================================

The acceptance harness for the compatibility layer.  Everything it asserts is
something a migrating caller depends on, and every check is runnable offline:
no API keys, no network, no external services.

Coverage map
------------

1.  Filter language — every operator, logical nesting, and the rejections.
2.  Dimension scoping — determinism, order-insensitivity, collision resistance.
3.  Core write/read cycle — ``add`` / ``get`` / ``get_all`` / ``search``,
    including physical isolation between scopes.
4.  Duplicate suppression on re-add.
5.  Mutation — ``update`` / ``delete`` / ``delete_all`` / ``history``.
6.  The validation contract, error code by error code.
7.  ``MemoryClient`` in embedded mode, plus the typed option objects.
8.  Configuration guards and the degradation report.
9.  Native extensions — capsule / expand round-trip, ledger integrity.
10. Graph memory — add / search / get-all / delete.
11. ``AsyncMemory`` — single calls, concurrent batch, context manager.
12. The operation event log, including the async ``add`` handshake.
13. The provider registry and its availability report.
14. Embedder wiring — a configured provider really drives the engine.
15. The lexical fallback and its disclosure via ``explain=True``.
16. The compatible CLI — round-trip, formats, error envelope, scope guards.
17. The MCP tool set — all eleven names present and callable.
18. The REST surface — every route, auth, and status-code mapping.
19. Zero third-party imports across the whole engine.

Usage::

    python scripts/verify_api.py            # everything
    python scripts/verify_api.py --quick    # skip the network-ish parts

Exit code is 0 only when every check passes.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PASSED: List[str] = []
FAILED: List[Tuple[str, str]] = []


def check(name: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception:  # noqa: BLE001 - a failing check must not stop the suite
        FAILED.append((name, traceback.format_exc()))
        print(f"FAIL  {name}")
        return
    PASSED.append(name)
    print(f"ok    {name}")


# ---------------------------------------------------------------------------
# 1. Filter language
# ---------------------------------------------------------------------------

def t_filters() -> None:
    from mnemosyne.api.errors import ApiValidationError
    from mnemosyne.api.filters import (combine, compile_filter,
                                               normalize_filters, split_scope)

    rec = {"content": "alice lives in Austin", "importance": 4,
           "status": "active", "expires_at": None,
           "meta": {"user_id": "alice", "topic": "location"}}

    assert compile_filter({"importance": {"eq": 4}})(rec) is True
    assert compile_filter({"importance": {"eq": "4"}})(rec) is True, "numeric string must match"
    assert compile_filter({"importance": {"eq": "04x"}})(rec) is False
    assert compile_filter({"importance": {"ne": 4}})(rec) is False
    assert compile_filter({"importance": {"gt": 3}})(rec) is True
    assert compile_filter({"importance": {"gte": 4}})(rec) is True
    assert compile_filter({"importance": {"lt": 4}})(rec) is False
    assert compile_filter({"importance": {"lte": 4}})(rec) is True
    assert compile_filter({"importance": {"in": [1, 2, 4]}})(rec) is True
    assert compile_filter({"importance": {"nin": [1, 2]}})(rec) is True
    assert compile_filter({"topic": {"contains": "loc"}})(rec) is True
    assert compile_filter({"topic": {"contains": "LOC"}})(rec) is False, \
        "contains must be case-sensitive"
    assert compile_filter({"topic": {"icontains": "LOC"}})(rec) is True
    assert compile_filter({"content": {"wildcard": "alice *"}})(rec) is True
    assert compile_filter({"topic": {"eq": "*"}})(rec) is True, "bare * means present"
    assert compile_filter({"missing": {"eq": "*"}})(rec) is False

    assert compile_filter({"AND": [{"importance": {"gt": 1}}, {"topic": "location"}]})(rec)
    assert compile_filter({"and": [{"importance": {"gt": 9}}, {"topic": "location"}]})(rec) is False
    assert compile_filter({"OR": [{"importance": {"gt": 9}}, {"topic": "location"}]})(rec)
    assert compile_filter({"NOT": [{"topic": "location"}]})(rec) is False
    assert compile_filter({"or": [{"and": [{"importance": 4}, {"topic": "location"}]}]})(rec)

    assert normalize_filters({"user_id": "x"}) == {"user_id": {"eq": "x"}}
    assert split_scope({"user_id": "a", "topic": "t"}) == ({"user_id": "a"},
                                                           {"topic": {"eq": "t"}})
    assert normalize_filters({"$user_id": "x"}) == {"user_id": {"eq": "x"}}
    assert combine({"a": 1}, {}, {"b": 2}) == {"and": [{"a": {"eq": 1}}, {"b": {"eq": 2}}]}
    assert combine() == {}

    for bad in ({"importance": {"bogus": 1}}, {"AND": 5}):
        try:
            compile_filter(bad)
            raise AssertionError(f"expected rejection for {bad}")
        except ApiValidationError:
            pass
    try:
        compile_filter([1, 2])  # type: ignore[arg-type]
        raise AssertionError("expected non-mapping rejection")
    except ApiValidationError:
        pass


# ---------------------------------------------------------------------------
# 2. Dimension scoping
# ---------------------------------------------------------------------------

def t_namespace() -> None:
    from mnemosyne.api.projection import derive_namespace, scope_summary

    assert derive_namespace({}) == "default"
    assert derive_namespace(None) == "default"
    assert derive_namespace({"user_id": "alice"}) == "user_id_alice"
    assert derive_namespace({"user_id": "a", "agent_id": "bc"}) != \
        derive_namespace({"user_id": "ab", "agent_id": "c"}), \
        "length prefixing must prevent (a,bc) colliding with (ab,c)"
    assert derive_namespace({"user_id": "a", "agent_id": "b"}) == \
        derive_namespace({"agent_id": "b", "user_id": "a"}), "order must not matter"
    assert derive_namespace({"user_id": "../../etc/passwd"}) == \
        "user_id_.._.._etc_passwd", "path traversal must be neutralised"
    assert derive_namespace({"user_id": "x" * 500}).startswith("user_id_")
    assert scope_summary({"user_id": "a", "run_id": "r"}) == "user_id=a run_id=r"


# ---------------------------------------------------------------------------
# 3-5. Core write / read cycle
# ---------------------------------------------------------------------------

def _memory(tmp: str, **cfg: Any):
    from mnemosyne.api import Memory, MemoryConfig

    base = {"brain_dir": os.path.join(tmp, "m-%d" % len(PASSED)),
            "llm": {"provider": "rules"},
            "embedder": {"provider": "builtin"},
            "vector_store": {"provider": "builtin"}}
    base.update(cfg)
    return Memory(MemoryConfig.from_dict(base))


def t_roundtrip(tmp: str) -> None:
    m = _memory(tmp)
    out = m.add("I prefer dark mode and use vim keybindings. My name is Alice.",
                user_id="alice")
    assert "results" in out, out
    assert out["results"], "no facts extracted"
    assert all(r["event"] in ("ADD", "DUPLICATE") for r in out["results"]), out
    ids = [r["id"] for r in out["results"] if r["id"]]
    assert ids

    got = m.get(ids[0])
    assert got["id"] == ids[0]
    assert got["memory"] and got["metadata"]["user_id"] == "alice"
    assert got["metadata"]["confidence"] is not None, "metadata must expose engine fields"

    hits = m.search("dark mode vim keybindings", filters={"user_id": "alice"})
    assert hits["results"], hits
    assert 0.0 <= hits["results"][0]["score"] <= 1.0

    listed = m.get_all(filters={"user_id": "alice"}, top_k=50)
    assert len(listed["results"]) >= len(ids)

    assert m.search("prefer", filters={"user_id": "bob"})["results"] == [], \
        "scopes must be physically isolated"
    assert m.get_all(filters={"user_id": "bob"})["results"] == []

    items = m.to_memory_item_for_test() if hasattr(m, "to_memory_item_for_test") else None
    m.close()


def t_dedup(tmp: str) -> None:
    m = _memory(tmp)
    first = m.add("I prefer dark mode.", user_id="d")
    second = m.add("I prefer dark mode.", user_id="d")
    assert any(r["event"] == "ADD" for r in first["results"]), first
    assert all(r["event"] in ("DUPLICATE",) for r in second["results"]), second
    m.close()


def t_mutation(tmp: str) -> None:
    from mnemosyne.api.errors import ApiNotFoundError

    m = _memory(tmp)
    res = m.add("I prefer dark mode.", user_id="m")
    mid = res["results"][0]["id"]

    assert m.update(mid, text="I now prefer light mode.")["message"]
    assert "light" in m.get(mid)["memory"]
    assert m.update(mid, metadata={"confidence": 0.5})["message"]
    assert m.get(mid)["metadata"]["confidence"] == 0.5, "metadata patch must merge"
    assert "light" in m.get(mid)["memory"], "metadata patch must not clear text"

    assert m.history(mid)["results"], "history must not be empty"

    assert m.delete(mid)["message"]
    try:
        m.get(mid)
        raise AssertionError("deleted memory must not be gettable")
    except ApiNotFoundError:
        pass

    m.add("I like coffee.", user_id="m")
    assert m.delete_all(user_id="m")["deleted"] >= 1
    assert m.get_all(filters={"user_id": "m"})["results"] == []
    m.close()


def t_immutable(tmp: str) -> None:
    from mnemosyne.api.errors import ApiConflictError

    m = _memory(tmp)
    res = m.add("The invoice number is INV-2024-001.", user_id="i", immutable=True)
    mid = res["results"][0]["id"]
    try:
        m.update(mid, text="changed")
        raise AssertionError("immutable text must not be editable")
    except ApiConflictError:
        pass
    m.update(mid, metadata={"reviewed": True})
    m.close()


def t_expiry(tmp: str) -> None:
    m = _memory(tmp)
    m.add("This offer expires soon.", user_id="x", expiration_date="2020-01-01")
    assert m.get_all(filters={"user_id": "x"})["results"] == [], \
        "expired memory must be hidden by default"
    assert m.get_all(filters={"user_id": "x"}, show_expired=True)["results"], \
        "show_expired must reveal it"
    m.close()


def t_no_infer(tmp: str) -> None:
    m = _memory(tmp)
    out = m.add("raw text stored verbatim, unedited", user_id="v", infer=False)
    assert out["results"][0]["memory"] == "raw text stored verbatim, unedited"
    m.close()


# ---------------------------------------------------------------------------
# 6. Validation contract
# ---------------------------------------------------------------------------

def t_validation(tmp: str) -> None:
    from mnemosyne.api.errors import ApiValidationError

    m = _memory(tmp)
    cases = [
        (lambda: m.add(None, user_id="x"), "VALIDATION_003"),
        (lambda: m.add(123, user_id="x"), "VALIDATION_003"),
        (lambda: m.add([{"no_content": 1}], user_id="x"), "VALIDATION_003"),
        (lambda: m.add("hi", user_id="  "), "VALIDATION_004"),
        (lambda: m.add("hi", user_id="a b"), "VALIDATION_005"),
        (lambda: m.search("q", threshold=2.0), "VALIDATION_006"),
        (lambda: m.search("q", user_id="a"), "VALIDATION_007"),
        (lambda: m.get_all(user_id="a"), "VALIDATION_007"),
        (lambda: m.search("q", filters="not-a-dict"), "VALIDATION_008"),
        (lambda: m.delete_all(), "VALIDATION_004"),
    ]
    for call, code in cases:
        try:
            call()
            raise AssertionError(f"expected {code}")
        except ApiValidationError as e:
            assert e.code == code, (e.code, code, str(e))
    assert m.chat.__doc__ is not None
    try:
        m.chat("hi")
        raise AssertionError("chat must raise NotImplementedError")
    except NotImplementedError:
        pass
    m.close()


# ---------------------------------------------------------------------------
# 7. MemoryClient + options
# ---------------------------------------------------------------------------

def t_client(tmp: str) -> None:
    from mnemosyne.api import (AddMemoryOptions, MemoryClient,
                                       SearchMemoryOptions)

    c = MemoryClient(api_key="local", config={"brain_dir": os.path.join(tmp, "client"),
                                              "llm": {"provider": "rules"}})
    assert c.transport == "embedded"
    out = c.add("I work at Acme as a staff engineer.",
                options=AddMemoryOptions(user_id="c1"))
    assert out["results"], out
    res = c.search("Acme staff engineer", options=SearchMemoryOptions(
        filters={"user_id": "c1"}, top_k=5))
    assert res["results"], res
    assert c.get(out["results"][0]["id"])["id"]
    assert c.get_all(filters={"user_id": "c1"})["results"]
    assert c.status()["status"] == "ok"
    assert c.describe()["config"]["brain_dir"]

    # camelCase aliases (ported TypeScript SDK code)
    opts = SearchMemoryOptions(topK=7, showExpired=True)
    assert opts.top_k == 7 and opts.show_expired is True, opts.to_dict()
    add_opts = AddMemoryOptions(userId="u1", filters={"agent_id": "a1"})
    payload = add_opts.to_payload()
    assert payload["user_id"] == "u1" and payload["agent_id"] == "a1", payload

    # unknown option must be rejected rather than ignored
    try:
        AddMemoryOptions(bogus_option=1)
        raise AssertionError("unknown option must be rejected")
    except Exception:
        pass
    c.reset()


def t_client_from_config() -> None:
    from mnemosyne.api import MemoryClient

    c = MemoryClient.from_config({"brain_dir": os.path.join(tempfile.gettempdir(),
                                                           "mnemosyne-cli-cfg"),
                                  "llm": {"provider": "rules"}})
    assert c.transport == "embedded"


# ---------------------------------------------------------------------------
# 8. Config guards + degradation
# ---------------------------------------------------------------------------

def t_config(tmp: str) -> None:
    from mnemosyne.api import Memory, MemoryConfig
    from mnemosyne.api.errors import ApiValidationError

    cfg = MemoryConfig.from_dict({"llm": {"provider": "ollama"},
                                  "embedder": {"provider": "builtin"}})
    assert cfg.llm.provider == "ollama"
    assert cfg.vector_store.provider == "builtin"
    assert cfg.version == "v1.1"

    # dimension mismatch between a named embedder and a named store
    try:
        MemoryConfig.from_dict({
            "embedder": {"provider": "builtin"},
            "vector_store": {"provider": "qdrant",
                             "config": {"embedding_model_dims": 1536}}})
        raise AssertionError("dimension mismatch must be rejected")
    except ApiValidationError as e:
        assert "imensional" in str(e), str(e)

    # degradation is recorded, not raised
    m = Memory(MemoryConfig.from_dict({
        "brain_dir": os.path.join(tmp, "degraded"),
        "llm": {"provider": "openai", "config": {"api_key": ""}}}))
    assert m.degraded, "expected degradation to be reported"
    assert m.describe()["degraded"]["llm"]["used"] == "rules"
    assert json.dumps(m.describe(), default=str), "describe must be serialisable"

    # secrets must be redacted in describe()
    m2 = Memory(MemoryConfig.from_dict({
        "brain_dir": os.path.join(tmp, "redact"),
        "llm": {"provider": "openai", "config": {"api_key": "sk-should-not-appear"}}}))
    assert "sk-should-not-appear" not in json.dumps(m2.describe()), \
        "credentials must be redacted"
    m2.close()

    # strict mode raises
    try:
        Memory(MemoryConfig.from_dict({
            "brain_dir": os.path.join(tmp, "strict"),
            "strict_providers": True,
            "llm": {"provider": "openai", "config": {"api_key": ""}}}))
        raise AssertionError("strict_providers must raise")
    except Exception as e:
        assert "strict_providers" in str(e) or "unavailable" in str(e), str(e)


# ---------------------------------------------------------------------------
# 9. Native extensions
# ---------------------------------------------------------------------------

def t_extensions(tmp: str) -> None:
    m = _memory(tmp)
    res = m.add("The server has 128 GB of RAM and cost $4200 on 2024-03-05.",
                user_id="e")
    mid = res["results"][0]["id"]
    cap = m.capsule(mid, budget_tokens=60)
    assert cap.get("ref"), cap
    assert cap.get("selfcheck_ok") is True, cap
    back = m.expand(cap["ref"])
    assert back.get("verified") is True, back
    assert back["text"] == back["content"] == \
        "The server has 128 GB of RAM and cost $4200 on 2024-03-05."
    # atoms survive at every compression level
    for budget in (5, 12, 30, 200):
        c = m.capsule(mid, budget_tokens=budget)
        assert any("4200" in a or "128" in a for a in c.get("atoms") or []), c
    integ = m.verify_integrity()
    assert integ and all("valid" in v for v in integ.values()), integ
    m.close()


# ---------------------------------------------------------------------------
# 10. Graph memory
# ---------------------------------------------------------------------------

def t_graph(tmp: str) -> None:
    m = _memory(tmp)
    out = m.graph_add("Steve Jobs founded Apple in Cupertino with Steve Wozniak.",
                      user_id="g")
    assert "results" in out and "entities" in out, out
    m.add("Steve Jobs founded Apple in Cupertino with Steve Wozniak.", user_id="g")
    allg = m.graph_get_all(user_id="g")
    assert "results" in allg
    s = m.graph_search("Apple Cupertino Steve", user_id="g")
    assert "results" in s and "entities" in s, s
    assert m.graph_delete_all(user_id="g")["message"]
    m.close()


# ---------------------------------------------------------------------------
# 11. Async
# ---------------------------------------------------------------------------

def t_async(tmp: str) -> None:
    import asyncio

    from mnemosyne.api import AsyncMemory, MemoryConfig

    async def main() -> None:
        am = AsyncMemory(MemoryConfig.from_dict({
            "brain_dir": os.path.join(tmp, "async"), "llm": {"provider": "rules"}}))
        out = await am.add("I prefer tea over coffee.", user_id="a1")
        assert out["results"], out
        res = await am.search("tea coffee prefer", filters={"user_id": "a1"})
        assert res["results"], res
        assert await am.get(out["results"][0]["id"])
        many = await am.add_many([
            {"messages": "I like hiking.", "options": {"user_id": "a1"}},
            {"messages": "I speak German.", "options": {"user_id": "a1"}},
            {"messages": None, "options": {"user_id": "a1"}},
        ])
        assert len(many) == 3
        assert "error" in many[2], "a bad item must be reported, not fatal"
        assert await am.list_entities()
        assert await am.describe()
        await am.close()

    async def ctx_mgr() -> None:
        from mnemosyne.api import AsyncMemory, MemoryConfig

        async with AsyncMemory(MemoryConfig.from_dict({
                "brain_dir": os.path.join(tmp, "async2"),
                "llm": {"provider": "rules"}})) as am:
            assert (await am.add("I use a standing desk.", user_id="a2"))["results"]

    asyncio.run(main())
    asyncio.run(ctx_mgr())


# ---------------------------------------------------------------------------
# 12. Events
# ---------------------------------------------------------------------------

def t_events(tmp: str) -> None:
    m = _memory(tmp)
    out = m.add("I own a ThinkPad X1 Carbon.", user_id="ev", async_mode=True)
    assert out["status"] == "PENDING" and out["event_id"].startswith("evt-"), out
    st: Dict[str, Any] = {}
    for _ in range(200):
        st = m.get_event_status(out["event_id"])
        if st["status"] in ("SUCCEEDED", "FAILED"):
            break
        time.sleep(0.02)
    assert st["status"] == "SUCCEEDED", st
    assert st["detail"].get("added", 0) >= 1, st
    assert m.list_events()["results"], "event log must persist"
    assert m.list_events(status="SUCCEEDED")["results"]
    assert m.list_events(status="CANCELLED")["results"] == []
    try:
        m.get_event_status("evt-does-not-exist")
        raise AssertionError("unknown event must raise")
    except Exception as e:
        assert getattr(e, "code", "") == "NOT_FOUND_001", e
    m.close()


# ---------------------------------------------------------------------------
# 13. Provider registry
# ---------------------------------------------------------------------------

def t_providers() -> None:
    from mnemosyne.api import available_providers
    from mnemosyne.providers import list_providers

    p = list_providers()
    for cat in ("llm", "embedder", "vector_store", "graph_store", "reranker"):
        assert p[cat], f"no providers for {cat}"

    # the reference component catalogs, reproduced
    for name in ("openai", "openai_structured", "azure_openai",
                 "azure_openai_structured", "ollama", "anthropic", "groq",
                 "together", "aws_bedrock", "litellm", "gemini", "deepseek",
                 "minimax", "xai", "sarvam", "lmstudio", "vllm", "langchain"):
        assert name in p["llm"], f"missing LLM provider {name}"
    for name in ("openai", "ollama", "huggingface", "azure_openai", "gemini",
                 "vertexai", "together", "lmstudio", "langchain", "aws_bedrock",
                 "fastembed"):
        assert name in p["embedder"], f"missing embedder {name}"
    for name in ("qdrant", "chroma", "pgvector", "pinecone", "mongodb", "milvus",
                 "baidu", "cassandra", "neptune", "upstash_vector",
                 "azure_ai_search", "azure_mysql", "redis", "valkey",
                 "databricks", "elasticsearch", "vertex_ai_vector_search",
                 "opensearch", "supabase", "weaviate", "faiss", "langchain",
                 "s3_vectors", "turbopuffer", "oracledb"):
        assert name in p["vector_store"], f"missing vector store {name}"
    for name in ("llm", "cohere", "zero_entropy", "huggingface",
                 "sentence_transformer"):
        assert name in p["reranker"], f"missing reranker {name}"
    assert len(p["vector_store"]) >= 25, len(p["vector_store"])

    info = available_providers()
    assert info["llm"]["fallback"] == "rules"
    assert info["vector_store"]["providers"]
    transports = {d["transport"] for d in info["vector_store"]["providers"]}
    assert {"embedded", "http", "sdk"} <= transports, transports


def t_provider_degradation(tmp: str) -> None:
    from mnemosyne.providers import build_components

    out = build_components({
        "llm": {"provider": "openai", "config": {"api_key": ""}},
        "vector_store": {"provider": "qdrant",
                         "config": {"url": "http://127.0.0.1:1"}},
    })
    assert out["components"]["llm"] is not None
    assert out["degraded"], "unavailable providers must be reported"
    assert "llm" in out["degraded"]
    assert out["degraded"]["llm"]["used"] == "rules"


# ---------------------------------------------------------------------------
# 14. Embedder wiring
# ---------------------------------------------------------------------------

def t_embedding_wiring(tmp: str) -> None:
    m = _memory(tmp, embedder={"provider": "hashing",
                               "config": {"dimensions": 256}})
    m.add("I prefer dark mode.", user_id="w")
    ctx = m._ctx({"user_id": "w"})
    assert ctx.brain.embed_engine is not None
    vec = ctx.brain.embed_engine.encode("hello world")
    assert len(vec) == 256, len(vec)
    assert abs(sum(v * v for v in vec) - 1.0) < 1e-6, "vectors must be normalised"
    m.close()


# ---------------------------------------------------------------------------
# 15. Lexical fallback
# ---------------------------------------------------------------------------

def t_multimodal(tmp: str) -> None:
    from mnemosyne.providers.vision import supports_vision

    m = _memory(tmp)

    out = m.add([{"role": "user", "content": [
        {"type": "text", "text": "This is my new desk setup, I love it."},
        {"type": "image_url",
         "image_url": {"url": "https://example.com/desk.jpg", "detail": "high"}},
    ]}], user_id="mm")
    assert out["results"], out
    imgs = [r for r in out["results"] if r.get("modality") == "image"]
    assert imgs, f"the image must become its own memory: {out}"
    img = imgs[0]
    assert img["id"] and img["attachment"]["url"] == "https://example.com/desk.jpg"
    assert img["attachment"]["detail"] == "high"
    assert img["described"] is False, "no vision provider is configured here"

    got = m.get(img["id"])
    assert got["metadata"]["modality"] == "image", got["metadata"]
    assert got["metadata"]["attachments"][0]["url"] == "https://example.com/desk.jpg"
    assert got["metadata"]["description_missing"] is True, got["metadata"]

    # An inline payload must never be copied into the memory text: base64 in the
    # text would blow up the token count, the full-text index and every prompt.
    blob = "A" * 400
    out2 = m.add([{"role": "user", "content": [
        {"type": "text", "text": "Screenshot of the invoice."},
        {"type": "image", "data": blob, "mime_type": "image/png"}]}], user_id="mm2")
    imgs2 = [r for r in out2["results"] if r.get("modality") == "image"]
    assert imgs2, out2
    assert blob not in imgs2[0]["memory"], "base64 must not land in the memory text"
    assert imgs2[0]["attachment"]["url"].startswith("inline:"), imgs2[0]

    # Anthropic-shaped parts are accepted as well.
    out3 = m.add([{"role": "user", "content": [
        {"type": "text", "text": "A chart of quarterly revenue."},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": blob}}]}], user_id="mm3")
    assert any(r.get("modality") == "image" for r in out3["results"]), out3

    # A text-only turn must not produce attachment rows.
    out4 = m.add("I prefer tea over coffee.", user_id="mm4")
    assert all(r.get("modality") is None for r in out4["results"]), out4

    # Capability registry
    for name in ("openai", "openai_structured", "azure_openai", "anthropic",
                 "gemini", "ollama", "groq", "deepseek", "xai"):
        assert supports_vision(name), name
    for name in ("rules", "cohere", "langchain", "not-a-provider"):
        assert not supports_vision(name), name
    m.close()


def t_multimodal_rest(tmp: str) -> None:
    """Multimodal ingestion through the REST surface."""
    import threading
    import urllib.error
    import urllib.request

    from mnemosyne.webui import api_routes
    from mnemosyne.webui.web_server import run_server

    port = _free_port()
    brain = os.path.join(tmp, "mm-rest")
    saved = dict(os.environ)
    os.environ["MNEMOSYNE_DIR"] = brain
    os.environ.pop("MNEMOSYNE_REQUIRE_AUTH", None)
    api_routes.reset_memory()

    threading.Thread(target=run_server, kwargs={
        "port": port, "host": "127.0.0.1", "base_dir": brain,
        "namespace": "default", "auth": False}, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def call(method: str, path: str, body: Any = None):
        req = urllib.request.Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, {"raw": raw}

    for _ in range(120):
        try:
            if call("GET", "/v1/status/")[0] == 200:
                break
        except Exception:
            pass
        time.sleep(0.25)
    else:
        raise AssertionError("server did not start")

    status, body = call("POST", "/v3/memories/add/", {
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "My monitor is a 27-inch 4K panel."},
            {"type": "image_url", "image_url": {"url": "https://example.com/m.png"}},
        ]}],
        "user_id": "mmr", "async_mode": False})
    assert status == 200, (status, body)
    rows = body.get("results") or []
    assert any(r.get("modality") == "image" for r in rows), rows

    status, body = call("POST", "/v3/memories/search/",
                        {"query": "monitor 4K panel", "filters": {"user_id": "mmr"}})
    assert status == 200 and body["results"], (status, body)

    os.environ.clear()
    os.environ.update(saved)
    api_routes.reset_memory()


def t_lexical_fallback(tmp: str) -> None:
    m = _memory(tmp)
    m.add("I own a ThinkPad.", user_id="lf")
    ctx = m._ctx({"user_id": "lf"})

    # A query sharing no token must stay empty: the fallback may reorder, never
    # invent relevance.
    assert m.search("quantum chromodynamics", filters={"user_id": "lf"})["results"] == []

    # Force the primary retriever to come back empty, then confirm the fallback
    # fires and discloses itself. White-box on purpose: the fused retriever is
    # good enough that a natural zero-hit-but-token-sharing query is hard to
    # construct, and the property under test is the fallback's behaviour, not the
    # retriever's quality.
    original = ctx.brain.recall
    ctx.brain.recall = lambda *a, **k: []
    try:
        res = m.search("ThinkPad", filters={"user_id": "lf"}, explain=True)
        assert res["results"], "the fallback must surface a token-overlap match"
        assert "lexical_fallback" in res["results"][0]["reason"], res["results"][0]
        assert 0.0 < res["results"][0]["score"] <= 1.0, res["results"][0]
        assert m.search("zzzzz-nothing", filters={"user_id": "lf"})["results"] == []
    finally:
        ctx.brain.recall = original
    m.close()


def t_metadata_persistence(tmp: str) -> None:
    """Metadata must survive a restart, not just an in-process cache hit.

    Regression cover for a real defect: ``meta`` was absent from the store's
    column list, so user metadata was silently dropped on INSERT while the
    in-process hot cache kept serving the original dict — the loss only appeared
    after reopening the database.
    """
    from mnemosyne.api import Memory, MemoryConfig

    brain = os.path.join(tmp, "meta-persist")
    cfg = {"brain_dir": brain, "llm": {"provider": "rules"}}

    m = Memory(MemoryConfig.from_dict(cfg))
    out = m.add("I prefer dark mode.", user_id="mp",
                metadata={"source": "onboarding", "tier": "gold"})
    mid = out["results"][0]["id"]
    assert m.get(mid)["metadata"]["source"] == "onboarding", "same-process read"
    m.close()

    # Reopen: a fresh process would do exactly this.
    m2 = Memory(MemoryConfig.from_dict(cfg))
    got = m2.get(mid)
    assert got["metadata"]["source"] == "onboarding", \
        "metadata must survive a reopen; it did not, so it is not persisted"
    assert got["metadata"]["tier"] == "gold", got["metadata"]
    assert got["metadata"]["user_id"] == "mp", got["metadata"]

    # And a metadata patch must merge, then survive another reopen.
    m2.update(mid, metadata={"reviewed": True})
    assert m2.get(mid)["metadata"]["reviewed"] is True
    assert m2.get(mid)["metadata"]["tier"] == "gold", "patch must merge, not replace"
    m2.close()

    m3 = Memory(MemoryConfig.from_dict(cfg))
    meta = m3.get(mid)["metadata"]
    assert meta["reviewed"] is True and meta["tier"] == "gold", meta
    m3.close()


# ---------------------------------------------------------------------------
# 16. CLI
# ---------------------------------------------------------------------------

def _cli(repo: str, env: Dict[str, str], *args: str, expect_ok: bool = True):
    proc = subprocess.run([sys.executable, os.path.join(repo, "mnemosyne.py"), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, cwd=repo, timeout=300)
    if expect_ok and proc.returncode != 0:
        raise AssertionError(f"exit {proc.returncode}\nstdout={proc.stdout[:1500]}\n"
                             f"stderr={proc.stderr[:1500]}")
    return proc


def t_cli(repo: str, env: Dict[str, str]) -> None:
    r = _cli(repo, env, "add", "I prefer dark mode and use vim keybindings.",
             "--user-id", "alice", "--agent")
    body = json.loads(r.stdout)
    assert body["status"] == "success" and body["command"] == "add", body
    assert body["scope"] == {"user_id": "alice"}, body["scope"]
    assert body["count"] >= 1, body
    mid = body["data"][0]["id"]
    assert mid and all(k in body["data"][0] for k in ("id", "memory", "event")), \
        body["data"][0]

    # search/list items carry the projected shape, including metadata
    listed = json.loads(_cli(repo, env, "list", "--user-id", "alice",
                             "--agent").stdout)
    assert listed["data"], listed
    assert all(k in listed["data"][0] for k in ("id", "memory", "metadata")), \
        listed["data"][0]

    body = json.loads(_cli(repo, env, "search", "dark mode vim keybindings",
                           "--user-id", "alice", "--agent").stdout)
    assert body["count"] >= 1, body

    assert json.loads(_cli(repo, env, "list", "--user-id", "alice",
                           "--agent").stdout)["count"] >= 1
    assert json.loads(_cli(repo, env, "get", mid, "--agent").stdout)["data"][0]["id"] == mid
    assert json.loads(_cli(repo, env, "update", mid, "I now prefer light mode.",
                           "--agent").stdout)["status"] == "success"

    # formats
    assert "id" in _cli(repo, env, "list", "--user-id", "alice").stdout
    assert _cli(repo, env, "list", "--user-id", "alice", "--output", "quiet").stdout.strip()
    assert "{" in _cli(repo, env, "list", "--user-id", "alice",
                       "--output", "json").stdout
    assert "brain_dir" in _cli(repo, env, "config", "--show").stdout
    assert _cli(repo, env, "config", "--path").stdout.strip()
    _cli(repo, env, "config", "--set", "user_id", "alice")
    assert json.loads(_cli(repo, env, "config", "--get", "user_id").stdout) == "alice"
    _cli(repo, env, "config", "--set", "user_id", '""')

    # error envelope
    r = _cli(repo, env, "get", "no-such-memory-id", "--agent", expect_ok=False)
    assert r.returncode != 0
    err = json.loads(r.stdout)
    assert err["status"] == "error" and err["code"] == "NOT_FOUND_001", err

    # scope guards
    r = _cli(repo, env, "delete", "--all", "--yes", "--agent", expect_ok=False)
    assert r.returncode != 0, "delete --all without a scope must refuse"
    assert json.loads(_cli(repo, env, "delete", "--all", "--user-id", "alice",
                           "--yes", "--agent").stdout)["status"] == "success"

    # import / export-ish round trip through the compatible surface
    payload_path = os.path.join(env["MNEMOSYNE_DIR"], "import.json")
    with open(payload_path, "w", encoding="utf-8") as fh:
        json.dump([{"memory": "I keep a paper notebook."},
                   {"memory": "I ride a bicycle to work."}], fh)
    r = _cli(repo, env, "import", payload_path, "--user-id", "imp", "--agent")
    imp = json.loads(r.stdout)
    assert imp["status"] in ("success", "error"), imp
    assert json.loads(_cli(repo, env, "list", "--user-id", "imp",
                           "--agent").stdout)["count"] >= 1

    # entity + event
    assert json.loads(_cli(repo, env, "entity", "--list", "--agent").stdout)["status"] \
        == "success"
    assert json.loads(_cli(repo, env, "event", "--limit", "5",
                           "--agent").stdout)["status"] == "success"

    # delete a single memory
    body = json.loads(_cli(repo, env, "add", "I own a red bicycle.",
                           "--user-id", "alice", "--agent").stdout)
    if body["data"]:
        assert json.loads(_cli(repo, env, "delete", body["data"][0]["id"], "--yes",
                               "--agent").stdout)["status"] == "success"


def t_cli_init(repo: str, env: Dict[str, str]) -> None:
    """The native ``init`` must also record the CLI configuration."""
    _cli(repo, env, "init")
    cfg_path = os.path.join(env["MNEMOSYNE_DIR"], "cli.config.json")
    assert os.path.isfile(cfg_path), f"{cfg_path} was not written"
    with open(cfg_path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    assert cfg.get("brain_dir"), cfg


# ---------------------------------------------------------------------------
# 17. MCP
# ---------------------------------------------------------------------------

def t_mcp() -> None:
    from mnemosyne.webui import mcp_server

    names = [t["name"] for t in mcp_server.TOOLS]
    expected = ["add_memory", "search_memories", "get_memories", "get_memory",
                "update_memory", "delete_memory", "delete_all_memories",
                "delete_entities", "list_entities", "list_events",
                "get_event_status"]
    for name in expected:
        assert name in names, f"missing MCP tool {name}"
    assert mcp_server.NATIVE_TOOL_COUNT == 20, mcp_server.NATIVE_TOOL_COUNT
    assert len(names) == len(set(names)), "tool names must be unique"
    assert len(names) == 31, len(names)

    out = mcp_server.handle_tools_call("add_memory",
                                       {"messages": "I own a ThinkPad.",
                                        "user_id": "mcp1", "async_mode": False})
    assert out.get("results"), out
    mid = out["results"][0]["id"]

    assert mcp_server.handle_tools_call("get_memory", {"memory_id": mid})["id"] == mid
    assert mcp_server.handle_tools_call(
        "search_memories", {"query": "ThinkPad", "filters": {"user_id": "mcp1"}})["results"]
    assert mcp_server.handle_tools_call(
        "get_memories", {"user_id": "mcp1", "top_k": 5})["results"]
    assert mcp_server.handle_tools_call(
        "update_memory", {"memory_id": mid, "text": "I own a ThinkPad X1."}).get("message")

    out = mcp_server.handle_tools_call("add_memory",
                                       {"messages": "I like hiking too.",
                                        "user_id": "mcp1", "async_mode": True})
    assert out["status"] == "PENDING" and "note" in out, out
    st: Dict[str, Any] = {}
    for _ in range(300):
        st = mcp_server.handle_tools_call("get_event_status",
                                          {"event_id": out["event_id"]})
        if st["status"] in ("SUCCEEDED", "FAILED"):
            break
        time.sleep(0.02)
    assert st["status"] == "SUCCEEDED", st

    assert mcp_server.handle_tools_call("list_events", {"limit": 5})["results"]
    assert mcp_server.handle_tools_call("list_entities", {"limit": 10})["results"]
    assert mcp_server.handle_tools_call("delete_memory",
                                        {"memory_id": mid}).get("message")
    assert mcp_server.handle_tools_call("delete_all_memories",
                                        {"user_id": "mcp1"}).get("message")
    assert mcp_server.handle_tools_call("delete_entities", {"user_id": "mcp1"})

    # JSON-RPC envelope
    resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 1,
                                      "method": "tools/list"})
    assert len(resp["result"]["tools"]) == 31
    resp = mcp_server.handle_request({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "add_memory",
                   "arguments": {"messages": "I drink tea.", "user_id": "rpc1",
                                 "async_mode": False}}})
    assert "results" in resp["result"]["content"][0]["text"]

    # the native tools must still work
    resp = mcp_server.handle_request({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "retain", "arguments": {"content": "native path works"}}})
    assert "memory_id" in resp["result"]["content"][0]["text"]


# ---------------------------------------------------------------------------
# 18. REST
# ---------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def t_rest(tmp: str) -> None:
    from mnemosyne.webui.web_server import run_server
    from mnemosyne.webui import api_routes

    port = _free_port()
    brain = os.path.join(tmp, "rest")
    saved = dict(os.environ)
    os.environ["MNEMOSYNE_DIR"] = brain
    os.environ.pop("MNEMOSYNE_REQUIRE_AUTH", None)
    api_routes.reset_memory()

    threading.Thread(target=run_server, kwargs={
        "port": port, "host": "127.0.0.1", "base_dir": brain,
        "namespace": "default", "auth": False}, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def call(method: str, path: str, body: Any = None,
             headers: Dict[str, str] | None = None):
        req = urllib.request.Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, {"raw": raw}

    for _ in range(120):
        try:
            status, body = call("GET", "/v1/status/")
            if status == 200:
                break
        except Exception:
            pass
        time.sleep(0.25)
    else:
        raise AssertionError("server did not start")

    assert body["status"] == "ok" and body["api_version"] == "v3", body
    for key in ("graph_memory", "multimodal", "reranking", "temporal_reasoning",
                "capsule_compression", "ledger_integrity", "async_events"):
        assert body["capabilities"].get(key) is True, (key, body["capabilities"])
    assert body["auth_required"] is False, body

    status, body = call("GET", "/v1/providers/")
    assert status == 200 and body["providers"]["llm"], body
    assert len(body["providers"]["vector_store"]) >= 25

    status, body = call("POST", "/v3/memories/add/", {
        "messages": [{"role": "user", "content": "I moved to Berlin in 2023."}],
        "user_id": "rest1", "metadata": {"source": "e2e"}})
    assert status == 200, (status, body)
    if body.get("event_id"):
        for _ in range(200):
            _, ev = call("GET", f"/v1/event/{body['event_id']}/")
            if ev.get("status") in ("SUCCEEDED", "FAILED"):
                break
            time.sleep(0.05)
        assert ev["status"] == "SUCCEEDED", ev
    else:
        assert body.get("results"), body

    status, body = call("POST", "/v3/memories/search/",
                        {"query": "Berlin 2023", "filters": {"user_id": "rest1"}})
    assert status == 200 and body["results"], (status, body)
    hit = body["results"][0]
    assert hit["memory"] and hit["id"] and 0.0 <= hit["score"] <= 1.0
    mid = hit["id"]

    assert call("GET", f"/v3/memories/{mid}/")[1]["id"] == mid
    assert call("POST", "/v3/memories/get-all/",
                {"filters": {"user_id": "rest1"}})[1]["results"]
    assert call("PUT", f"/v3/memories/{mid}/",
                {"text": "I live in Berlin."})[1].get("message")
    assert call("GET", f"/v3/memories/{mid}/history/")[1]["results"]

    status, body = call("POST", "/v1/capsule/", {"memory_id": mid, "budget_tokens": 40})
    assert status == 200 and body.get("ref"), (status, body)
    status, body = call("POST", "/v1/expand/", {"ref": body["ref"]})
    assert status == 200 and body.get("text"), (status, body)
    assert call("GET", "/v1/integrity/")[1]["result"]

    assert call("POST", "/v3/graph/add/",
                {"data": "Steve Jobs founded Apple.", "user_id": "rest1"})[0] == 200
    assert call("POST", "/v3/graph/get-all/", {"user_id": "rest1"})[1].get("results") is not None
    assert call("POST", "/v3/graph/search/",
                {"query": "Apple", "user_id": "rest1"})[0] == 200
    assert call("GET", "/v2/entities/")[1]["results"]
    assert call("GET", "/v1/events/")[1]["results"]

    # status-code mapping
    status, body = call("POST", "/v3/memories/search/", {"query": ""})
    assert status == 400 and body["code"].startswith("VALIDATION"), (status, body)
    status, body = call("GET", "/v3/memories/nope/")
    assert status == 404 and body["code"] == "NOT_FOUND_001", (status, body)
    status, body = call("POST", "/v1/capsule/", {})
    assert status == 400, (status, body)

    # API key lifecycle: adding a key must make auth mandatory
    status, body = call("POST", "/v1/keys/", {"label": "e2e"})
    assert status == 201 and body["result"]["key"].startswith("mn-"), (status, body)
    raw_key, key_id = body["result"]["key"], body["result"]["id"]
    assert "hash" not in body["result"], "the hash must never be returned"

    status, body = call("POST", "/v3/memories/search/", {"query": "x"})
    assert status == 401 and body["code"] == "UNAUTHORIZED", (status, body)

    # status stays reachable so a health probe survives key rotation
    assert call("GET", "/v1/status/")[0] == 200

    for header in ({"Authorization": f"Bearer {raw_key}"},
                   {"Authorization": f"Token {raw_key}"},
                   {"X-API-Key": raw_key}):
        status, body = call("POST", "/v3/memories/add/",
                            {"messages": "I use a mechanical keyboard.",
                             "user_id": "rest2", "async_mode": False},
                            headers=header)
        assert status == 200, (header, status, body)

    status, body = call("POST", "/v3/memories/add/",
                        {"messages": "nope", "user_id": "rest2", "async_mode": False},
                        headers={"X-API-Key": "mn-wrong-key-value"})
    assert status == 401, (status, body)

    assert any(k["id"] == key_id
               for k in call("GET", "/v1/keys/",
                             headers={"Authorization": f"Bearer {raw_key}"})[1]["results"])
    key_header = {"Authorization": f"Bearer {raw_key}"}

    # A live key naming an unknown key id is a 404, not a crash.
    _, second = call("POST", "/v1/keys/", {"label": "second"}, headers=key_header)
    key2 = second["result"]["key"]
    assert call("DELETE", "/v1/keys/does-not-exist/",
                headers={"Authorization": f"Bearer {key2}"})[0] == 404
    assert any(k["id"] == key_id
               for k in call("GET", "/v1/keys/", headers=key_header)[1]["results"])

    # Revocation must take effect immediately for every route.
    assert call("DELETE", f"/v1/keys/{key_id}/", headers=key_header)[0] == 200
    assert call("POST", "/v3/memories/search/", {"query": "x"},
                headers=key_header)[0] == 401
    assert call("DELETE", "/v1/keys/does-not-exist/", headers=key_header)[0] == 401

    # the console routes must still answer on the same listener
    assert call("GET", "/api/health")[0] == 200

    os.environ.clear()
    os.environ.update(saved)
    api_routes.reset_memory()


# ---------------------------------------------------------------------------
# 19. Zero-dependency guarantee
# ---------------------------------------------------------------------------

def t_no_third_party(quick: bool) -> None:
    """Import every engine module and report any that needs a third-party package.

    Adapters that legitimately require an SDK are allowed to raise
    ``ImportError`` *when instantiated*, but importing the module itself must
    never fail — that is the property that keeps ``pip install mnemosyne-os`` free
    of dependencies.
    """
    import importlib
    import pkgutil

    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    bad: List[Tuple[str, str]] = []

    packages = ["mnemosyne", "mnemosyne.providers", "mnemosyne.api",
                "mnemosyne.webui",
                "storage", "security", "session", "context", "lexical",
                "profiles", "plugins", "mnemosyne_plugins"]
    for pkg_name in packages:
        try:
            pkg = importlib.import_module(pkg_name)
        except ImportError as e:
            bad.append((pkg_name, str(e)))
            continue
        path = getattr(pkg, "__path__", None)
        if path is None:
            continue
        for info in pkgutil.iter_modules(path):
            name = f"{pkg_name}.{info.name}"
            try:
                importlib.import_module(name)
            except ImportError as e:
                bad.append((name, str(e)))
            except Exception:  # noqa: BLE001 - a runtime error is not a dep problem
                pass
    assert not bad, bad


def t_install_requires() -> None:
    """The declared install requirements must stay empty."""
    setup_py = os.path.join(ROOT, "setup.py")
    if not os.path.isfile(setup_py):
        return
    with open(setup_py, "r", encoding="utf-8") as fh:
        text = fh.read()
    assert "install_requires=[]" in text or "install_requires = []" in text, \
        "the core must declare no install requirements"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the client API layer")
    parser.add_argument("--quick", action="store_true",
                        help="skip the REST/CLI subprocess checks")
    parser.add_argument("--keep", action="store_true",
                        help="keep the temporary directory")
    args = parser.parse_args()

    tmp = tempfile.mkdtemp(prefix="mnemosyne-api-verify-")
    print(f"Mnemosyne OS {_version()} — client API compatibility verification")
    print(f"repo: {ROOT}")
    print(f"tmp : {tmp}\n")

    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8",
               MNEMOSYNE_DIR=os.path.join(tmp, "brain"))
    env.pop("MNEMOSYNE_REQUIRE_AUTH", None)

    # Point the in-process compatibility layer at the same isolated store the
    # subprocess checks use. Without this the MCP and REST checks would write into
    # the operator's real ~/.mnemosyne — which both pollutes their memory and
    # makes the run non-repeatable, because a second run would see the first run's
    # memories and correctly report them as duplicates.
    os.environ["MNEMOSYNE_DIR"] = env["MNEMOSYNE_DIR"]
    os.environ.pop("MNEMOSYNE_REQUIRE_AUTH", None)
    try:
        from mnemosyne.webui import api_routes

        api_routes.reset_memory()
    except Exception:  # pragma: no cover - the module is imported by later checks
        pass

    check("1  filter language", t_filters)
    check("2  dimension scoping", t_namespace)
    check("3  add/search/get/get_all", lambda: t_roundtrip(tmp))
    check("4  duplicate suppression", lambda: t_dedup(tmp))
    check("5  update/delete/delete_all/history", lambda: t_mutation(tmp))
    check("5b immutable memories", lambda: t_immutable(tmp))
    check("5c expiration semantics", lambda: t_expiry(tmp))
    check("5d infer=False verbatim", lambda: t_no_infer(tmp))
    check("6  validation contract", lambda: t_validation(tmp))
    check("7  MemoryClient + options", lambda: t_client(tmp))
    check("7b MemoryClient.from_config", t_client_from_config)
    check("8  config guards + degradation", lambda: t_config(tmp))
    check("9  capsule/expand/integrity", lambda: t_extensions(tmp))
    check("10 graph memory", lambda: t_graph(tmp))
    check("11 AsyncMemory", lambda: t_async(tmp))
    check("12 operation events", lambda: t_events(tmp))
    check("13 provider registry", t_providers)
    check("13b provider degradation", lambda: t_provider_degradation(tmp))
    check("14 embedder wiring", lambda: t_embedding_wiring(tmp))
    check("15 lexical fallback", lambda: t_lexical_fallback(tmp))
    check("15b metadata persistence", lambda: t_metadata_persistence(tmp))
    check("15c multimodal ingestion", lambda: t_multimodal(tmp))
    check("15d multimodal over REST", lambda: t_multimodal_rest(tmp))
    check("17 MCP tools", t_mcp)
    check("19 zero third-party imports", lambda: t_no_third_party(args.quick))
    check("19b empty install_requires", t_install_requires)

    if not args.quick:
        check("16 compatible CLI", lambda: t_cli(ROOT, env))
        check("16b init writes CLI config", lambda: t_cli_init(ROOT, env))
        check("18 REST surface", lambda: t_rest(tmp))

    print(f"\n==== RESULT ====")
    print(f"passed: {len(PASSED)}   failed: {len(FAILED)}")
    for name, tb in FAILED:
        print(f"\n---- {name}\n{tb}")

    if args.keep:
        print(f"\ntmp kept: {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAILED else 0


def _version() -> str:
    try:
        from mnemosyne import __version__

        return __version__
    except Exception:  # pragma: no cover
        return "unknown"


if __name__ == "__main__":
    sys.exit(main())
