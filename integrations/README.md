# Integrations

Adapters for the frameworks people actually build agents with. Each one is a
thin bridge on purpose: Mnemosyne already exposes a Python API, an HTTP API and an
MCP server, so an adapter only has to translate the framework's own history
protocol onto `add()` / `search()`.

Everything here runs against the **embedded** engine, so nothing needs a server,
a key or a network.

| Framework | File | What it gives you |
| --- | --- | --- |
| LangChain | [`langchain.py`](langchain.py) | `MnemosyneMemory` — a `BaseMemory`-compatible history class |
| LlamaIndex | [`llama_index.py`](llama_index.py) | `MnemosyneChatStore` / retriever bridge |
| CrewAI | [`crewai.py`](crewai.py) | Shared long-term memory for a crew |
| Dify | [`dify.md`](dify.md) | HTTP tool definition backed by the REST API |
| n8n | [`n8n.md`](n8n.md) | HTTP Request node recipes |
| Vercel AI SDK | [`vercel-ai-sdk.md`](vercel-ai-sdk.md) | `useChat` with memory via the REST API |
| MCP clients | `../docs/DEPLOY_DEEPSEEK_HARNESS.md` | Claude Code, Cursor, Codex, Hermes, OpenWebUI |

## The shape every adapter follows

Regardless of framework, the integration has two seams and no others:

```
before the model call   →  search(query, filters={scope}, top_k=N)  →  inject
after the turn          →  add(messages, user_id=scope)             →  persist
```

Anything more elaborate than that is the framework's business, not the memory
layer's. A memory layer that also answers the question acquires its own model and
its own opinions about the user's data, and stops being a memory layer — which is
why `Memory.chat()` raises `NotImplementedError` here, exactly as the reference
implementation does.

## Choosing a scope

Reuse an identifier the application already has:

| Application shape | Scope |
| --- | --- |
| Single-user desktop tool | `user_id="local"` |
| Multi-user web app | `user_id=<account id>` |
| Multi-tenant SaaS | `user_id=<account id>` + `app_id=<tenant slug>` |
| One agent per customer | `agent_id=<customer id>` |
| Per-conversation memory | `run_id=<conversation id>` (plus `user_id`) |

Each distinct combination gets its own SQLite file, so a scope key that varies per
request (a fresh UUID per call) silently creates a new, empty store every time.
That is the single most common integration mistake: pick a key that is stable for
as long as the memories should persist.

## Testing an integration

```python
import tempfile
from mnemosyne.api import Memory, MemoryConfig

def make_memory(tmp):
    return Memory(MemoryConfig.from_dict({
        "brain_dir": tmp,
        "llm": {"provider": "rules"},        # deterministic, offline
        "embedder": {"provider": "builtin"},
    }))
```

Always point the test at a temporary directory — a test that writes into
`~/.mnemosyne` both pollutes the developer's memory and stops being repeatable the
second time it runs, because the first run's memories are still there.

And make the test query **share tokens** with the stored text. With the built-in
embedder, `search("what laptop")` legitimately returns nothing for `"I own a
ThinkPad"` — a test asserting otherwise is testing the wrong thing.
