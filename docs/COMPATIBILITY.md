# Compatibility

Mnemosyne's memory API accepts the **conventional agent-memory call shape** used
by other memory libraries: the same class names, the same method names, the same
parameter names and defaults, the same keyword-only markers, and the same error
codes.

That property exists so that code already written against that shape can move to
Mnemosyne — and, more importantly, move *back* — without an adapter layer or a
rewrite. It is a property of the API, not its identity: the API is Mnemosyne's
own, and the engine underneath it is what makes it worth using.

```python
from mnemosyne import Memory

m = Memory()
m.add("I prefer dark mode and vim keybindings", user_id="alice")
hits = m.search("what does alice prefer?", filters={"user_id": "alice"})
```

## What is preserved

| Contract | Detail |
| --- | --- |
| Client classes | `Memory`, `AsyncMemory`, `MemoryClient` |
| Methods | `add` `get` `get_all` `search` `update` `delete` `delete_all` `history` `reset` `close` `from_config` `chat` |
| Configuration | `Memory.from_config({...})` with `llm` / `embedder` / `vector_store` / `graph_store` / `reranker` keys, plus `history_db_path`, `version`, `custom_instructions`, `custom_categories` |
| Scope | `user_id` / `agent_id` / `run_id` / `app_id`, validated identically |
| Filters | `eq` `ne` `gt` `gte` `lt` `lte` `in` `nin` `contains` `icontains` `startswith` `endswith` `wildcard`, nested with `AND` / `OR` / `NOT` |
| Result envelope | `{"results": [...]}` for `add` / `search` / `get_all`, with `id`, `memory`, `metadata`, `score`, `created_at`, `updated_at` per item |
| Error codes | `VALIDATION_003` … `VALIDATION_008`, `NOT_FOUND_001`, `CONFLICT_001`, `PROVIDER_001` |
| Option objects | `AddMemoryOptions`, `SearchMemoryOptions`, `GetAllMemoryOptions`, `UpdateMemoryOptions`, `DeleteAllOptions`, `ListEventsOptions` — including camelCase aliases |

`m.add()` accepts a bare string, a single message dict, or a list of message
dicts. Multimodal turns (image and audio parts in the OpenAI, Anthropic or
Gemini shapes) are accepted as-is.

## Where Mnemosyne deliberately goes further

These are additions, not differences — nothing above changes because of them.

| Addition | Why |
| --- | --- |
| `describe()` | Reports the live configuration **including what was degraded and why**. When recall is worse than expected, "which component actually ran" is the first question, and a static feature list cannot answer it. |
| `capsule()` / `expand()` | Lossless hierarchical compression with byte-exact recovery and hash verification. |
| `verify_integrity()` | Verifies the hash-chained audit ledger and names the entry that changed. |
| `temporal_query()` | Version chains for a single entity. |
| `graph_add()` / `graph_search()` | Native graph memory with no external graph database. |

## One behavioural difference worth knowing

When a configured provider cannot be constructed — a missing API key, an
unreachable endpoint, an SDK that is not installed — Mnemosyne **degrades to the
built-in equivalent and records that it did**, rather than raising during
construction:

```python
m.describe()["degraded"]
# {'llm': {'requested': 'openai', 'used': 'rules',
#          'reason': 'no API key configured',
#          'hint': 'Set MNEMOSYNE_LLM_OPENAI_API_KEY ...'}}
```

Set `strict_providers: true` in the config to get a hard failure instead:

```python
m = Memory.from_config({"strict_providers": True, "llm": {"provider": "openai"}})
```

## Verifying

```bash
python scripts/verify_api.py     # fully offline, no network access
```
