# n8n integration

No custom node required. Everything below is a stock **HTTP Request** node, which
means it survives n8n upgrades without maintenance.

## 1. Run the memory server

```bash
pip install mnemosyne-os
mnemosyne-web --port 8788
```

Issue a key once:

```bash
curl -X POST http://127.0.0.1:8788/v1/keys/ \
  -H "Content-Type: application/json" -d '{"label": "n8n"}'
```

In n8n, create a **Header Auth** credential:

| Field | Value |
| --- | --- |
| Name | `Authorization` |
| Value | `Bearer mn-...` |

Reuse that credential on every memory node. One credential means one place to
rotate.

## 2. The four nodes

### Add memory

| Setting | Value |
| --- | --- |
| Method | `POST` |
| URL | `http://memory:8788/v3/memories/add/` |
| Body Content Type | JSON |
| JSON | see below |

```json
{
  "messages": [
    { "role": "user", "content": "{{ $json.userMessage }}" },
    { "role": "assistant", "content": "{{ $json.answer }}" }
  ],
  "user_id": "{{ $json.userId }}",
  "metadata": { "workflow": "{{ $workflow.name }}", "source": "n8n" },
  "async_mode": false
}
```

`async_mode: false` returns the created memories inline. n8n nodes do not poll, so
this is the correct setting here — the opposite of the API default, which exists
for clients that can poll.

### Search memory

```json
{
  "query": "{{ $json.userMessage }}",
  "filters": { "user_id": "{{ $json.userId }}" },
  "top_k": 5,
  "threshold": 0.1,
  "explain": true
}
```

Then inject it into your LLM node's system prompt. n8n expressions index into the
response array, so:

```
What you already know about this user:
{{ $json.results.map(r => "- " + r.memory).join("\n") }}
```

### List memories

`POST /v3/memories/get-all/` with body `{"filters": {"user_id": "{{ $json.userId }}"}, "top_k": 100}`.

### Delete a memory

`DELETE http://memory:8788/v3/memories/{{ $json.memoryId }}/`

## 3. Recommended workflow shape

```
Webhook / Chat Trigger
  → Set userId            (from the trigger payload or a fixed value)
  → HTTP: memory_search
  → AI Agent / LLM        (system prompt includes the recalled text)
  → Respond to Webhook    (answer the user first)
  → HTTP: memory_add      (persist afterwards)
```

Putting `memory_add` after the response matters: extraction may call a model, and
a user should not wait on bookkeeping.

## 4. Use a stable `userId`

This is the one setting that silently ruins an n8n integration. The `userId` must
be the **same value every time** for as long as the memories should persist.

| Source | Verdict |
| --- | --- |
| `{{ $json.body.from }}` (a chat handle) | ✅ |
| An n8n credential or workflow variable | ✅ |
| A fixed string, for a single-user bot | ✅ |
| `{{ $now }}`, `{{ $execution.id }}`, a fresh UUID | ❌ creates a new empty store per run |

If memory "works but never remembers anything", this is why. Confirm with a
`GET /v2/entities/` node: it lists every scope that exists, with counts.

## 5. Health check node

Add a scheduled workflow with one HTTP node:

| Setting | Value |
| --- | --- |
| Method | `GET` |
| URL | `http://memory:8788/v1/status/` |

It reports the live configuration and every degraded provider — the first thing to
read when recall quality drops.

## 6. Docker Compose

If n8n runs in a container, put the memory server on the same network so the URL
is a service name:

```yaml
services:
  n8n:
    image: n8nio/n8n
    ports: ["5678:5678"]
    depends_on: [memory]

  memory:
    image: python:3.12-slim
    command: >
      sh -c "pip install --no-cache-dir mnemosyne-os &&
             mnemosyne-web --port 8788 --host 0.0.0.0"
    environment:
      MNEMOSYNE_DIR: /data
      MNEMOSYNE_REQUIRE_AUTH: "1"
    volumes:
      - mnemosyne-data:/data

volumes:
  mnemosyne-data:
```

Two things about this compose file are deliberate. The memory volume is a named
volume, not a bind mount to the host — it is the system of record, and it should
not be casually edited. And `--host 0.0.0.0` is only safe because
`MNEMOSYNE_REQUIRE_AUTH=1` is set alongside it; publishing the API on all
interfaces without authentication turns a local memory store into a public one.
