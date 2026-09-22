<p align="center">
  <img src="assets/banner.png" alt="mnemosyne OS" width="100%">
</p>

# mnemosyne OS

<p align="center">
  <a href="https://pypi.org/project/mnemosyne-os/">PyPI</a> ·
  <a href="https://github.com/FrankHu-HK/mnemosyne">GitHub</a> ·
  <a href="README_CN.md">中文</a>
</p>

<p align="center">
  <a href="https://pypi.org/project/mnemosyne-os/"><img src="https://img.shields.io/badge/PyPI-mnemosyne--os-blue?style=for-the-badge" alt="PyPI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License: MIT"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.8%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.8+"></a>
  <a href="#-mcp-server"><img src="https://img.shields.io/badge/MCP-31%20Tools-00ADD8?style=for-the-badge" alt="Model Context Protocol"></a>
  <a href="#quick-start"><img src="https://img.shields.io/badge/dependencies-0-brightgreen?style=for-the-badge" alt="Zero dependencies"></a>
  <a href="https://pepy.tech/projects/mnemosyne-os"><img src="https://img.shields.io/pepy/dt/mnemosyne-os?style=for-the-badge" alt="Downloads"></a>
  <a href="https://x.com/mnemosyne_oos"><img src="https://img.shields.io/badge/X-@mnemosyne_oos-black?style=for-the-badge&logo=x&logoColor=white" alt="X"></a>
</p>

<p align="center">
  <a href="README_TW.md"><img src="https://img.shields.io/badge/Lang-繁體中文-red?style=for-the-badge" alt="繁體中文"></a>
  <a href="README.es.md"><img src="https://img.shields.io/badge/Lang-Español-orange?style=for-the-badge" alt="Español"></a>
  <a href="README.ru.md"><img src="https://img.shields.io/badge/Lang-Русский-blue?style=for-the-badge" alt="Русский"></a>
  <a href="README.de.md"><img src="https://img.shields.io/badge/Lang-Deutsch-lightgrey?style=for-the-badge" alt="Deutsch"></a>
  <a href="README.th.md"><img src="https://img.shields.io/badge/Lang-ไทย-blue?style=for-the-badge" alt="ไทย"></a>
  <a href="README.ko.md"><img src="https://img.shields.io/badge/Lang-한국어-green?style=for-the-badge" alt="한국어"></a>
  <a href="README.ja.md"><img src="https://img.shields.io/badge/Lang-日本語-red?style=for-the-badge" alt="日本語"></a>
</p>

**Mnemosyne OS 8.0.0** — a zero-dependency, local-first AI memory system. Graph
memory, multimodal ingestion, reranking, temporal reasoning, a hash-chained audit
ledger, lossless compression, and 31 MCP tools.

> The only AI memory engine whose **core genuinely carries zero third-party
> dependencies** — no vector database, no LLM runtime, no cloud account.
> `install_requires` is an empty list. It runs on a laptop, a server, or
> serverless infrastructure alike.

Use it as a **Python library**, a **CLI**, an **HTTP API**, or an **MCP server**.

---

## 🚀 Quick start

### Install

```bash
pip install mnemosyne-os          # core: zero third-party dependencies
```

### Remember and recall without configuring anything

```python
from mnemosyne import Memory

m = Memory()                       # built-in embedder + rule-based extractor
m.add("I prefer dark mode and use vim keybindings. My name is Alice.",
      user_id="alice")

for hit in m.search("what does alice prefer", filters={"user_id": "alice"})["results"]:
    print(f"{hit['score']:.3f}  {hit['memory']}")
```

Offline, no API key, no model download, no database to install — which is what
makes the next section possible.

### Attach real models only once recall needs to be stronger

```python
from mnemosyne import Memory

m = Memory.from_config({
    "llm":          {"provider": "openai", "config": {"model": "gpt-4o-mini"}},
    "embedder":     {"provider": "openai", "config": {"model": "text-embedding-3-small"}},
    "vector_store": {"provider": "qdrant", "config": {"url": "http://localhost:6333"}},
    "reranker":     {"provider": "cohere", "config": {"api_key": "..."}},
    "graph_store":  {"provider": "builtin"},
})
```

Every component is independently optional. When a provider cannot be built it
falls back to the built-in equivalent and **says so** — nothing degrades silently:

```python
m.describe()["degraded"]
# {'llm': {'requested': 'openai', 'used': 'rules', 'reason': 'no API key configured',
#          'hint': 'Set MNEMOSYNE_LLM_OPENAI_API_KEY ...'}}
```

### Or drive it from the command line

```bash
mnemosyne init
mnemosyne add "I prefer dark mode and vim keybindings" --user-id alice
mnemosyne search "what does alice prefer" --user-id alice
mnemosyne list  --user-id alice
mnemosyne event --limit 10
mnemosyne --agent search "preferences" --user-id alice   # JSON envelope for tool loops
```

### Or expose it over MCP

```json
{
  "mcpServers": {
    "mnemosyne": {
      "command": "python",
      "args": ["-m", "mnemosyne.webui.mcp_server",
               "--brain-dir", "./mem", "--namespace", "default"],
      "env": { "MNEMOSYNE_MCP_TOKEN": "<random 32+ chars>" }
    }
  }
}
```

### Or serve it over HTTP

```bash
mnemosyne-web --port 9090          # console and REST share one port
curl -X POST http://127.0.0.1:8788/v3/memories/add/ \
  -H "Authorization: Bearer $MNEMOSYNE_API_KEY" -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"I moved to Berlin in 2023."}],"user_id":"alice"}'
```

---

## 📊 Benchmarks

Measured with the harness shipped in this repository. Reproduce with
`scripts/verify_recall_quality.py` and `scripts/verify_precision_recall.py`.

| Benchmark | Score | What it measures |
| --- | --- | --- |
| LongMemEval | **96.2** | long-horizon conversational recall |
| LoCoMo | **94.8** | multi-session dialogue memory |
| BEAM (1M) | **68.5** | recall under a 1M-token context budget |
| BEAM (10M) | **53.9** | recall under a 10M-token context budget |

Scores are out of 100.

---

## 🧩 Capabilities

<table>
<tr><td><b>Memory API</b></td><td><code>Memory</code> / <code>AsyncMemory</code> / <code>MemoryClient</code> with a complete method surface: <code>add</code> <code>get</code> <code>get_all</code> <code>search</code> <code>update</code> <code>delete</code> <code>delete_all</code> <code>history</code> <code>reset</code> <code>close</code> <code>from_config</code>.</td></tr>
<tr><td><b>Four-dimensional scoping</b></td><td><code>user_id</code> / <code>agent_id</code> / <code>run_id</code> / <code>app_id</code> — enforced by <b>physical isolation</b>: one SQLite file per scope, rather than shared rows with a filter applied.</td></tr>
<tr><td><b>Filter language</b></td><td><code>eq</code> <code>ne</code> <code>gt</code> <code>gte</code> <code>lt</code> <code>lte</code> <code>in</code> <code>nin</code> <code>contains</code> <code>icontains</code> <code>wildcard</code>, with arbitrarily nested <code>AND</code>/<code>OR</code>/<code>NOT</code>.</td></tr>
<tr><td><b>Single-pass ADD-only extraction</b></td><td>One model call per write; memories accumulate and are never overwritten. Because nothing is rewritten, a bad extraction only introduces noise — it can never destroy a real fact.</td></tr>
<tr><td><b>Graph memory, always on</b></td><td>Entity linking and multi-hop traversal live in the same SQLite file. No external graph database required.</td></tr>
<tr><td><b>Multimodal ingestion</b></td><td>Accepts the OpenAI, Anthropic and Gemini image content shapes (plus audio). With a vision model configured it stores a description; without one it stores the reference — nothing is dropped.</td></tr>
<tr><td><b>Multi-signal retrieval</b></td><td>Semantic + BM25 keyword + entity graph + temporal + tag, fused with calibrated relevance floors and a lexical fallback.</td></tr>
<tr><td><b>Temporal reasoning</b></td><td>Observation dates, relative-time resolution, expiry semantics, and per-entity version chains.</td></tr>
<tr><td><b>Tiered memory</b></td><td>Hot / warm / cold tiers with forgetfulness economics: low-value memories are demoted and compressed, never silently deleted.</td></tr>
<tr><td><b>Lossless compression (AIC)</b></td><td>Compresses a memory into <i>pointer + structured facts + content atoms</i>. Numbers, dates, amounts and model numbers survive at every tier; <code>expand()</code> recovers the original text byte-for-byte and verifies its hash.</td></tr>
<tr><td><b>Hash-chained audit ledger</b></td><td>A SHA-256 chain; <code>verify_integrity()</code> detects tampering and names the exact entry that changed.</td></tr>
<tr><td><b>Async API and events</b></td><td><code>AsyncMemory</code> for high-throughput writes, plus a persisted operation log so an accepted write stays visible across processes.</td></tr>
<tr><td><b>Chinese-optimised</b></td><td>Bigram tokenisation + FTS5 + a built-in synonym dictionary, with full Latin-script support.</td></tr>
<tr><td><b>Safety notary</b></td><td>Detects credentials, invisible Unicode and HTML injection before a write lands, and redacts at field level.</td></tr>
</table>

---

## 🔌 Integrations

Every adapter is optional. **stdlib** adapters need no third-party package at all
— they speak HTTP directly through `urllib`. **sdk** adapters import their SDK
lazily and tell you exactly which package is missing.

### LLM providers (20)

| Transport | Providers |
| --- | --- |
| **stdlib HTTP** | `openai` `openai_structured` `azure_openai` `azure_openai_structured` `ollama` `anthropic` `gemini` `groq` `together` `deepseek` `minimax` `xai` `sarvam` `openrouter` `litellm` `lmstudio` `vllm` |
| **sdk** | `langchain` `aws_bedrock` |
| **built-in** | `rules` — a deterministic offline extractor, which is why `add()` works with no model configured at all |

### Embedders (13)

| Transport | Providers |
| --- | --- |
| **stdlib HTTP** | `openai` `azure_openai` `ollama` `gemini` `vertexai` `together` `lmstudio` `huggingface` |
| **sdk** | `fastembed` `langchain` `aws_bedrock` |
| **built-in** | `builtin` (128-dim, zero-dependency, deterministic) · `hashing` (any dimension, offline) |

### Vector stores (28)

| Transport | Stores |
| --- | --- |
| **embedded** | `builtin` (one SQLite file holds both memories and vectors) · `memory` · `generic` (declarative REST) |
| **stdlib HTTP** | `qdrant` `pinecone` `elasticsearch` `opensearch` `weaviate` `upstash_vector` `turbopuffer` |
| **sdk** | `chroma` `pgvector` `milvus` `mongodb` `redis` `valkey` `azure_ai_search` `azure_mysql` `baidu` `cassandra` `databricks` `faiss` `langchain` `neptune` `oracledb` `s3_vectors` `supabase` `vertex_ai_vector_search` |

### Graph stores (6)

`builtin` (native SQLite triples) · `neo4j` · `memgraph` · `neptune` · `kuzu` · `sparql` (any SPARQL 1.1 endpoint)

### Rerankers (5)

`llm` · `cohere` · `zero_entropy` · `huggingface` · `sentence_transformer`

### Framework adapters

LangChain · LlamaIndex · CrewAI · Dify · n8n · Vercel AI SDK · Ollama · MCP (stdio + Streamable HTTP)

---

## 🛠 MCP server

Runs over stdio JSON-RPC:

```bash
export MNEMOSYNE_MCP_TOKEN="your-secret-token"   # optional, but recommended
python -m mnemosyne.webui.mcp_server --brain-dir ./mem --namespace default
```

**31 tools** — twenty native, plus eleven that reuse the conventional
agent-memory tool names, so an existing MCP client can be pointed at Mnemosyne
without rewriting its tool definitions.

**Native (20):**

| Tool | Purpose |
| --- | --- |
| `retain` | Store one memory |
| `recall` | Retrieve memories |
| `retain_batch` | Bulk write, roughly 15× faster |
| `forget` | Forget a memory — by id, or by locating it with a natural-language query |
| `capsule` | Compress a memory into pointer + facts + atoms |
| `expand` | Recover a capsule's original text byte-for-byte |
| `recall_health` | Read-only recall quality metrics |
| `consolidate` | Merge near-duplicate memories into one representative |
| `reflect` | Statistics, frequent entities, conflict detection, cognitive patterns |
| `dedup` | Detect duplicates and near-duplicates |
| `graph_query` | Knowledge-graph traversal |
| `temporal_query` | Version-chain queries |
| `list_projects` | List isolated projects |
| `doctor` | Health check — integrity, counts, disk, backend state |
| `stats` | Runtime statistics |
| `audit` | Audit-chain queries |
| `confidence_history` | Confidence trajectories |
| `memory/export-v1` | Export via the Memory Exchange Protocol |
| `memory/import-v1` | Import via the Memory Exchange Protocol |
| `memory/claim` | Take over memories from an external export |

**Client-compatible (11):**

| Tool | Purpose |
| --- | --- |
| `add_memory` | Save text or conversation history for a user/agent |
| `search_memories` | Semantic search with filters |
| `get_memories` | Structured filter + paginated listing |
| `get_memory` | Fetch one by id |
| `update_memory` | Overwrite text and/or metadata |
| `delete_memory` | Delete one |
| `delete_all_memories` | Clear a scope |
| `delete_entities` | Delete entities and cascade |
| `list_entities` | List users/agents/apps/runs |
| `list_events` | List memory operations |
| `get_event_status` | Poll an async operation |

---

## 🌐 Self-hosted REST API

One process, one port, accepting `X-API-Key`, `Bearer` and `Token` auth headers.
The console and the API share the same listener.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/v1/status/` | Liveness probe + live configuration report |
| `GET` | `/v1/providers/` | Every provider and its current availability |
| `POST` | `/v3/memories/add/` | Extract and store (async, returns an event id) |
| `POST` | `/v3/memories/search/` | Semantic search |
| `POST` | `/v3/memories/get-all/` | Filtered listing |
| `GET` / `PUT` / `DELETE` | `/v3/memories/{id}/` | Fetch / update / delete one |
| `DELETE` | `/v3/memories/` | Clear a scope |
| `GET` | `/v3/memories/{id}/history/` | Change history |
| `GET` | `/v1/event/{id}/` · `/v1/events/` | Poll / list operations |
| `GET` / `DELETE` | `/v2/entities/` | List / delete scopes |
| `POST` | `/v3/graph/{add,search,get-all,delete-all}/` | Graph memory |
| `POST` | `/v1/capsule/` · `/v1/expand/` | Lossless compression |
| `GET` | `/v1/integrity/` | Ledger verification |
| `GET` / `POST` / `DELETE` | `/v1/keys/` | API key management |

---

## 🧠 Python API

```python
from mnemosyne import Memory, AsyncMemory, MemoryClient

# --- extraction / scope / filters ---------------------------------------------
m = Memory()
m.add([{"role": "user", "content": "I moved to Berlin in 2023."}],
      user_id="alice", metadata={"source": "onboarding"},
      observation_date="2023-06-01")
m.add("The invoice number is INV-2024-001.", user_id="alice", immutable=True)

hits = m.search("where does the user live",
                filters={"user_id": "alice",
                         "AND": [{"source": {"eq": "onboarding"}}]},
                top_k=5, threshold=0.1, rerank=False, explain=True)

# --- multimodal ---------------------------------------------------------------
m.add([{"role": "user", "content": [
    {"type": "text", "text": "My new desk."},
    {"type": "image_url", "image_url": {"url": "https://example.com/desk.jpg"}},
]}], user_id="alice")

# --- graph memory -------------------------------------------------------------
m.graph_add("Jobs founded Apple in Cupertino.", user_id="alice")
m.graph_search("Apple", filters={"user_id": "alice"})

# --- async and events ---------------------------------------------------------
async def ingest():
    am = AsyncMemory()
    await am.add_many([{"messages": t, "options": {"user_id": "alice"}}
                       for t in transcripts])
```

`Memory`, `AsyncMemory` and `MemoryClient` also accept the conventional
agent-memory call shape used by other memory libraries, so code already written
against that shape can switch by changing only the import. See
[docs/COMPATIBILITY.md](docs/COMPATIBILITY.md).

The engine's own capabilities hang off the same object:

```python
from mnemosyne import MemoryBrain

brain = MemoryBrain("./memories", enable_embeddings=True)
brain.ensure_init()
brain.retain("His laptop is an ASUS VivoBook Pro 14", fast=True)

results = brain.recall("what are that machine's specs", k=5)
results, cost = brain.recall("that machine's specs", k=5, budget_tokens=100)

cap = brain.capsule("<memory_id>", budget_tokens=60)   # pointer + facts + atoms
brain.expand(cap["ref"])                                # byte-exact recovery
brain.verify_integrity()                                # SHA-256 ledger check
```

---

## 📂 Layout

```
mnemosyne/
├── api/                 # the memory API: Memory / AsyncMemory / MemoryClient
│   ├── memory.py        #   engine-backed client
│   ├── config.py        #   MemoryConfig + dimension consistency checks
│   ├── filters.py       #   filter language -> predicates
│   ├── extract.py       #   single-pass ADD-only extraction
│   ├── multimodal.py    #   image / audio attachment parsing
│   ├── events.py        #   persisted operation log
│   └── client.py        #   embedded + HTTP transports
├── providers/           # optional component adapters (72 in total)
│   ├── llms.py          #   20 LLM providers
│   ├── embedders.py     #   13 embedder providers
│   ├── vector_stores.py #   28 vector stores
│   ├── graph_stores.py  #   6 graph stores
│   ├── rerankers.py     #   5 rerankers
│   ├── vision.py        #   three image wire formats
│   └── transport.py     #   stdlib HTTP + retries + credential redaction
├── brain.py             # MemoryBrain — the engine facade
├── capsule.py           # AIC lossless compression
├── retrieval.py         # multi-signal fusion and relevance calibration
├── graph.py             # temporal triple store
├── notary.py            # pre-write trust pipeline
├── cli.py               # native CLI
├── api_cli.py           # client-API CLI
└── webui/
    ├── web_server.py    # console + REST host
    ├── api_routes.py    # /v1 /v2 /v3 routes
    ├── mcp_server.py    # 20 native MCP tools
    └── mcp_api.py       # 11 client-compatible MCP tools

storage/                 # SQLite backend, hash-chained ledger, plugin SDK
security/                # contradiction detection, security reporting
scripts/                 # verification scripts
docs/                    # acceptance guide, recall strategy, compatibility
```

---

## ✅ Tests

```bash
python verify.py                              # self-check
python scripts/verify_api.py                  # client API verification, fully offline
python scripts/verify_memory_lifecycle.py --brain-dir ./mem --src-root .
python scripts/verify_precision_recall.py     # offline precision regression
python scripts/verify_recall_quality.py       # end-to-end recall quality
```

---

## 📚 Documentation

- [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) — the conventional agent-memory call shape
- [docs/ACCEPTANCE_GUIDE.md](docs/ACCEPTANCE_GUIDE.md) — acceptance criteria and the script for each
- [docs/RECALL_STRATEGY.md](docs/RECALL_STRATEGY.md) — how retrieval is assembled and budgeted
- [docs/KNOWN_DEFECTS.md](docs/KNOWN_DEFECTS.md) — confirmed defects, with measurements and fixes
- [docs/DEPLOY_DEEPSEEK_HARNESS.md](docs/DEPLOY_DEEPSEEK_HARNESS.md) — MCP deployment walkthrough
- [CHANGELOG.md](CHANGELOG.md) — version history

---

## 📄 License

MIT License — see [LICENSE](LICENSE).

Built by the Mnemosyne OS contributors.
