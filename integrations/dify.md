# Dify integration

Two ways to give a Dify app memory. Pick based on whether the app runs Dify's own
memory or Mnemosyne's.

## Option A — as a custom tool (recommended)

Dify tools are HTTP calls with a JSON-Schema declaration. Four tools cover the
whole surface, and they run against a self-hosted Mnemosyne REST server.

### 1. Run the server

```bash
pip install mnemosyne-os
mnemosyne-web --port 8788          # console + REST on one port
```

Issue an API key once (from the console's *Settings → API keys*, or directly):

```bash
curl -X POST http://127.0.0.1:8788/v1/keys/ -H "Content-Type: application/json" \
  -d '{"label": "dify"}'
# {"result": {"id": "key-...", "key": "mn-...", ...}}   <- the key shows once
```

Set `MNEMOSYNE_REQUIRE_AUTH=1` on the server if you want authentication enforced
even before the first key exists. Once any key exists it is enforced anyway.

### 2. Declare the tools in Dify

**memory_add**

```yaml
Authentication: Bearer
Method: POST
URL: http://<your-host>:8788/v3/memories/add/
Headers:
  Content-Type: application/json
Body:
  {
    "messages": [{"role": "user", "content": "{{text}}"}],
    "user_id": "{{user_id}}",
    "app_id": "{{app_id}}",
    "metadata": {"source": "dify"},
    "async_mode": false
  }
```

`async_mode: false` makes the call return the created memories instead of an
event id. Dify's tool node cannot poll, so waiting is the correct choice here —
it is the opposite of the default, which exists for clients that *can* poll.

**memory_search**

```yaml
Authentication: Bearer
Method: POST
URL: http://<your-host>:8788/v3/memories/search/
Body:
  {
    "query": "{{query}}",
    "filters": {"user_id": "{{user_id}}"},
    "top_k": 5,
    "threshold": 0.1,
    "explain": true
  }
```

**memory_list** — `POST /v3/memories/get-all/` with `{"filters": {"user_id": "{{user_id}}"}}`

**memory_delete** — `DELETE /v3/memories/{{memory_id}}/`

### 3. Wire it into the workflow

```
[Start] → [memory_search] → [LLM prompt]
                              ├─ system: "What you know about this user:\n{{memory_search.text}}"
                              └─ user:   {{query}}
           → [Answer] → [memory_add]  (text = user turn + answer)
```

Put `memory_add` **after** the answer node so the user does not wait on extraction
latency to see a reply.

Use a stable `user_id`. In Dify that is usually `{{sys.user_id}}` or a
conversation-variable you set once at the start. A value that changes per request
(a timestamp, a fresh UUID) silently creates a new, empty store each call — the
most common Dify integration mistake.

## Option B — as a Dify memory provider

Dify's built-in memory reads a variable it calls `memory`. To use Mnemosyne's
recall *as that variable*, add a Code node after Start:

```python
import json
import urllib.request

def main(query: str, user_id: str) -> dict:
    body = json.dumps({
        "query": query,
        "filters": {"user_id": user_id},
        "top_k": 5,
    }).encode()
    req = urllib.request.Request(
        "http://<your-host>:8788/v3/memories/search/",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer mn-...",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        rows = json.loads(resp.read())["results"]
    return {"memory": "\n".join(r["memory"] for r in rows)}
```

Then reference `{{memory}}` in the prompt. Dify's own memory is left off; you are
replacing it, not nesting inside it.

## Why bother with the REST route at all

Dify apps often run somewhere you do not control the filesystem of. The REST
surface means the memory lives on your machine, Dify holds only a URL and a key,
and moving the memory store is a config change rather than a migration.

If your Dify deployment can run Python code with `mnemosyne-os` installed, you can
skip the server entirely and use the embedded client in a Code node:

```python
from mnemosyne.api import Memory

_memory = None

def main(query: str, user_id: str) -> dict:
    global _memory
    if _memory is None:
        _memory = Memory.from_config({"brain_dir": "/data/mnemosyne",
                                      "llm": {"provider": "rules"}})
    _memory.add(query, user_id=user_id, metadata={"source": "dify"})
    rows = _memory.search(query, filters={"user_id": user_id}, top_k=5)["results"]
    return {"memory": "\n".join(r["memory"] for r in rows)}
```

`llm: rules` keeps this deterministic and offline. Swap in a real provider when
you want model-quality extraction — and check `_memory.describe()["degraded"]`
afterwards to confirm it actually took effect.
