---
name: mnemosyne-integrate
description: >
  Wire Mnemosyne memory into an existing repository, test-first. Load this when a
  task is "add memory to this project", "make the assistant remember across
  sessions", "run our memory layer on something self-hosted", or "persist context
  between runs". Produces a working integration plus a regression test, not just
  a code snippet.
---

# Integrate Mnemosyne into an existing repository

A pipeline skill: run it end to end, in order. Do not skip step 2 — the test is
what makes the integration verifiable rather than merely plausible.

## 1. Survey the repository before writing anything

Answer these four questions, with file evidence, before choosing an approach:

1. **Where does conversation context already live?** Look for the message
   assembly site — the place that builds the prompt sent to the model. That is
   where recalled memories belong.
2. **Is there an existing identity?** A logged-in user id, a session id, a tenant
   id. Mnemosyne scopes on `user_id` / `agent_id` / `run_id` / `app_id`; reusing an
   existing identifier is always better than inventing a parallel one.
3. **What is already installed?** `pip freeze` / `package.json`. If the project
   already has an LLM client or an embedding provider, reuse its credentials
   instead of asking for new ones.
4. **Is there a write hook?** A place after each turn where the transcript is
   available. That is where `add()` goes.

Report the four findings before proceeding. If any is missing, say so and propose
the smallest change that supplies it.

## 2. Write the failing test first

The integration is not done until this passes. Adapt the transport:

```python
import os, tempfile
from mnemosyne.api import Memory, MemoryConfig

def test_memory_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        m = Memory(MemoryConfig.from_dict({
            "brain_dir": tmp,
            "llm": {"provider": "rules"},        # deterministic, offline
            "embedder": {"provider": "builtin"},
        }))
        m.add("The user prefers dark mode and vim keybindings.", user_id="test")
        hits = m.search("dark mode vim keybindings",
                        filters={"user_id": "test"}, explain=True)
        assert hits["results"], "recall must return the memory that was just written"
        assert hits["results"][0]["memory"]
```

Two properties this test has and a naive one does not:

- the query **shares tokens** with the stored text (see step 5);
- the store is a **temporary directory**, so the test is repeatable and does not
  touch the developer's real memory.

Run it. It should pass on a clean install. If it fails, fix the integration
before continuing — do not proceed to the real wiring with a broken baseline.

## 3. Choose the approach

| Situation | Approach |
| --- | --- |
| Single-process app, one machine | Embedded `Memory()` — no server, no network |
| Several services or processes | `mnemosyne-web` + `MemoryClient(base_url=...)` |
| An MCP-capable assistant (Claude Code, Cursor, Codex, Hermes) | Register the MCP server — see below |
| A LangChain / LlamaIndex / CrewAI app | `integrations/` in this repository |

For an MCP-capable assistant, the smallest correct configuration:

```json
{
  "mcpServers": {
    "mnemosyne": {
      "command": "python",
      "args": ["-m", "mnemosyne.webui.mcp_server",
               "--brain-dir", "~/.mnemosyne", "--namespace", "default"],
      "env": { "MNEMOSYNE_MCP_TOKEN": "<random 32+ chars>" }
    }
  }
}
```

Set the token. An unauthenticated local MCP server is one shell escape away from
being an unauthenticated network one.

## 4. Wire it in at the two real seams

**Before the model call** — inject what is relevant:

```python
hits = memory.search(user_message, filters={"user_id": user_id}, top_k=5)
if hits["results"]:
    system += "\n\nWhat you already know about this user:\n" + "\n".join(
        f"- {h['memory']}" for h in hits["results"])
```

Budget the injected text. If the context window is tight, use the token-budgeted
form instead of truncating the list:

```python
from mnemosyne import MemoryBrain
results, cost = brain.recall(user_message, k=5, budget_tokens=400)
```

**After the turn** — persist it:

```python
memory.add([{"role": "user", "content": user_message},
            {"role": "assistant", "content": reply}], user_id=user_id)
```

Do the write **after** sending the reply where possible. `add()` may call a model
for extraction, and a user should not wait on memory bookkeeping to see a
response.

## 5. Set expectations correctly

Three behaviours surprise people, and each has a fix:

- **Zero-config recall is lexical.** With the built-in embedder, a query sharing
  no token with a memory returns that memory only via the lexical fallback — and
  a completely unrelated query correctly returns nothing. Configure an embedding
  provider when paraphrase recall matters, and assert on it in a test.
- **Scope must be passed consistently.** `user_id` at the top level for `add()`
  and `delete_all()`; inside `filters` for `search()` and `get_all()`. Mixing them
  raises `VALIDATION_007` rather than silently returning everything.
- **A provider outage degrades instead of raising.** Assert
  `memory.describe()["degraded"] == {}` in your own test if silent degradation
  would be unacceptable in your deployment — or set `"strict_providers": true`.

## 6. Leave a health check behind

```bash
python scripts/verify_api.py     # the upstream suite, 29 checks
curl -s http://127.0.0.1:8788/v1/status/ | python -m json.tool
```

The status route reports the live configuration and every degradation. It is the
first thing to read when recall is worse in production than in the test — and it
stays readable even while API keys are being rotated.

## 7. Report

State: which files changed, the test that now covers the integration, the scope
keys in use, which providers are configured, and anything from step 5 that the
deployment will need to know. Name anything you could not do.
