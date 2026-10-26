---
name: mnemosyne
description: >
  Use the Mnemosyne OS memory API to give an agent persistent memory. Load this
  when a task involves remembering user preferences or facts across sessions,
  retrieving prior context, scoping memories to a user/agent/run/app, filtering
  with structured predicates, compressing a memory to fit a context budget, or
  running existing agent-memory code on a self-hosted engine.
---

# Mnemosyne memory API

Mnemosyne OS is a local-first memory engine. Its core has **zero third-party
dependencies** — no vector database, no model runtime, no cloud account. Every
provider (LLM, embedder, vector store, graph store, reranker) is optional and
swappable.

The API also accepts the conventional agent-memory call shape — the same class
names, method signatures, defaults, return envelopes and error codes used by
other memory libraries — so code already written against that shape runs on
Mnemosyne unchanged.

## Install

```bash
pip install mnemosyne-os          # core, zero dependencies
```

There is no extra to install for the provider adapters: the ones that need no
third-party package are HTTP clients built on the standard library.

## The 30-second version

```python
from mnemosyne import Memory

m = Memory()                       # works offline, no key, no download
m.add("I prefer dark mode and vim keybindings", user_id="alice")

for hit in m.search("what does alice prefer", filters={"user_id": "alice"})["results"]:
    print(hit["score"], hit["memory"])
```

## Method surface

| Method | Notes |
| --- | --- |
| `add(messages, *, user_id, agent_id, run_id, app_id, metadata, infer=True, memory_type, immutable, expiration_date, observation_date, ...)` | Single-pass ADD-only extraction. Returns `{"results": [{"id", "memory", "event"}]}`. |
| `search(query, *, filters, top_k=20, threshold=0.1, rerank=False, explain=False)` | `threshold` is a relevance floor in `[0,1]`; `explain=True` adds the matching signals. |
| `get_all(*, filters, top_k=20, show_expired=False)` | Newest first. |
| `get(memory_id)` / `update(memory_id, text=, metadata=)` / `delete(memory_id)` | `update` patches only supplied fields. |
| `delete_all(user_id=, agent_id=, run_id=, app_id=)` | Requires at least one scope. |
| `history(memory_id)` | From the hash-chained ledger, so it cannot be rewritten. |
| `reset()` / `close()` | |

**Critical rule.** `add()` and `delete_all()` take entity ids as **top-level
keywords**; `search()` and `get_all()` take them **inside `filters`**. Passing a
top-level id to `search` raises `VALIDATION_007` with a copy-pasteable fix.

## Scoping is physical

Each `(user_id, agent_id, run_id, app_id)` combination gets its own directory and
its own SQLite file — not a filter over shared rows. One tenant cannot read
another's memories even with a malformed filter, and deleting a tenant is a
directory removal.

Namespaces are derived by hashing the **sorted, length-prefixed** components, so
`(user=a, agent=bc)` and `(user=ab, agent=c)` cannot collide and key order does
not change the result.

## Filters

```python
filters={"user_id": "alice"}                       # shorthand equality
filters={"priority": {"gte": 3}}                   # gt gte lt lte
filters={"category": {"in": ["work", "health"]}}   # in / nin
filters={"text": {"icontains": "deadline"}}        # contains / icontains / wildcard
filters={"AND": [{"a": 1}, {"OR": [{"b": 2}, {"c": 3}]}]}
```

A bare `"*"` means "field is present and non-null".

## Providers

Do not hardcode a vendor. Name one in the config and let the engine degrade
visibly if it is unavailable:

```python
m = Memory.from_config({
    "llm":          {"provider": "openai", "config": {"model": "gpt-4o-mini"}},
    "embedder":     {"provider": "openai", "config": {"model": "text-embedding-3-small"}},
    "vector_store": {"provider": "qdrant", "config": {"url": "http://localhost:6333"}},
})
print(m.describe()["degraded"])   # what was substituted, and why
```

Registry: `from mnemosyne import Memory` then
`from mnemosyne.api import PROVIDERS, available_providers`.
20 LLM · 13 embedder · 28 vector store · 6 graph store · 5 reranker — 72 in total.
`available_providers()` reports what can actually run on this machine.

## Things worth knowing before you rely on it

- **Zero-config recall is lexical.** The built-in embedder is a deterministic
  128-dimension projection of TF-IDF. It has no cross-word generalisation, so
  "what laptop" will not match "I own a ThinkPad" by itself. Configure a real
  embedder when that matters; check `describe()` to confirm which one is live.
- **A provider failure degrades, it does not raise.** Set
  `"strict_providers": true` to make it raise instead (right for CI).
- **Metadata survives restarts.** It is a real column; round-trip it freely.
- **Never put base64 in a memory's text.** Pass images as `image_url` content
  parts; the engine stores a reference plus, optionally, a description.

## Extensions beyond the conventional surface

- `capsule(memory_id, budget_tokens=60)` → pointer + facts + content atoms.
  Numbers, dates, amounts and model names survive every compression level.
- `expand(ref)` → the original text, byte-for-byte, with a hash check.
- `verify_integrity()` → SHA-256 hash-chain verification; names the exact altered record.
- `graph_add` / `graph_search` / `graph_get_all` — graph memory, always on.
- `temporal_query(entity)` — version chains.
- `list_events` / `get_event_status` — durable operation log.

## Verify your integration

```bash
python scripts/verify_api.py     # 29 checks, all offline
```
