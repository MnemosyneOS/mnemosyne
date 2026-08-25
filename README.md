<p align="center">
  <img src="assets/banner.png" alt="Mnemosyne OS" width="100%">
</p>

# Mnemosyne OS ☤

<p align="center">
  <a href="https://pypi.org/project/mnemosyne-os/">Mnemosyne OS</a> | <a href="https://github.com/FrankHu-HK/mnemosyne">GitHub</a> | <a href="README_CN.md">中文文档</a>
</p>

<p align="center">
  <a href="https://pypi.org/project/mnemosyne-os/"><img src="https://img.shields.io/badge/PyPI-mnemosyne--os-blue?style=for-the-badge" alt="PyPI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License: MIT"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.8%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.8+"></a>
  <a href="https://modelcontextprotocol.io/"><img src="https://img.shields.io/badge/MCP-13%20Tools-00ADD8?style=for-the-badge" alt="Model Context Protocol"></a>
 <a href="https://pepy.tech/projects/mnemosyne-os"><img src="https://img.shields.io/pepy/dt/mnemosyne-os?style=for-the-badge" alt="Downloads"></a>
 <a href="https://x.com/mnemosyne_oos"><img src="https://img.shields.io/badge/X-@mnemosyne_oos-black?style=for-the-badge&logo=x&logoColor=white" alt="X"></a>
  <a href="https://github.com/FrankHu-HK/mnemosyne/blob/main/README_CN.md"><img src="https://img.shields.io/badge/Lang-中文-red?style=for-the-badge" alt="中文"></a>
  <a href="https://github.com/FrankHu-HK/mnemosyne/blob/main/README_TW.md"><img src="https://img.shields.io/badge/Lang-繁體中文-red?style=for-the-badge" alt="繁體中文"></a>
  <a href="https://github.com/FrankHu-HK/mnemosyne/blob/main/README.es.md"><img src="https://img.shields.io/badge/Lang-Español-orange?style=for-the-badge" alt="Español"></a>
  <a href="https://github.com/FrankHu-HK/mnemosyne/blob/main/README.ru.md"><img src="https://img.shields.io/badge/Lang-Русский-blue?style=for-the-badge" alt="Русский"></a>
  <a href="https://github.com/FrankHu-HK/mnemosyne/blob/main/README.de.md"><img src="https://img.shields.io/badge/Lang-Deutsch-lightgrey?style=for-the-badge" alt="Deutsch"></a>
  <a href="https://github.com/FrankHu-HK/mnemosyne/blob/main/README.th.md"><img src="https://img.shields.io/badge/Lang-ไทย-blue?style=for-the-badge" alt="ไทย"></a>
  <a href="https://github.com/FrankHu-HK/mnemosyne/blob/main/README.ko.md"><img src="https://img.shields.io/badge/Lang-한국어-green?style=for-the-badge" alt="한국어"></a>
  <a href="https://github.com/FrankHu-HK/mnemosyne/blob/main/README.ja.md"><img src="https://img.shields.io/badge/Lang-日本語-red?style=for-the-badge" alt="日本語"></a>
</p>

**Mnemosyne OS 7.0.0** — a zero-dependency , local-first  AI memory system  with multi-tier forgetting , a hash-chain ledger , a plugin SDK , a local web dashboard , and MCP  support.

> The only AI memory engine whose **core requires zero third-party dependencies**  — no vector database , no LLM  runtime, no cloud lock-in. Runs on a laptop, a server, or serverless infra .

Use it as a **Python  library**, a **CLI **, an **HTTP API **, an **MCP server **, or embed it via the **MCP ** stdio transport.

<table>
<tr><td><b>Zero-dependency core </b></td><td>Runs on the Python standard library alone. No numpy, no torch, no vector DB, no LLM required to store and recall memories.</td></tr>
<tr><td><b>Multi-tier memory </b></td><td>Hot / warm / cold tiers with economic forgetting  — migrate low-value memories, never silently delete them.</td></tr>
<tr><td><b>Hash-chain ledger </b></td><td>SHA-256 chained ledger — <code>verify_chain()</code> detects tampering and locates the exact corrupted record.</td></tr>
<tr><td><b>Plugin SDK </b></td><td><code>VectorBackendPlugin</code> / <code>CryptoPlugin</code> / <code>RerankerPlugin</code> + official plugins (<code>numpy_vector</code>, <code>crypto</code>, <code>reranker</code>, <code>hrr</code>, <code>async</code>, <code>context-engine</code>).</td></tr>
<tr><td><b>MCP server </b></td><td>13 tools over stdio JSON-RPC, with token auth  and multi-tenant namespaces .</td></tr>
<tr><td><b>Web dashboard </b></td><td>Tech-aesthetic local dark dashboard , no external CDN — served from <code>web_server.py</code>.</td></tr>
<tr><td><b>Async API </b></td><td><code>AsyncMemoryBrain</code> asyncio wrapper for high-throughput ingestion.</td></tr>
<tr><td><b>Chinese-optimized </b></td><td>Bigram tokenization  + FTS5 + built-in synonym dictionary .</td></tr>
<tr><td><b>Security notary </b></td><td>Detects credentials, invisible Unicode, and HTML injection; field-level redaction  before write.</td></tr>
</table>

---

## Quick Install 

### From PyPI 

```bash
pip install mnemosyne-os
```

### Zero-dependency core 

```bash
# Core runs on the Python standard library alone
python -c "from mnemosyne import MemoryBrain; print('Ready!')"
```

### Development install 

```bash
git clone https://github.com/FrankHu-HK/mnemosyne.git
cd mnemosyne
pip install -e .
```

---

## Getting Started 

### CLI 

```bash
# Initialize the memory database
python mnemosyne.py --dir ./mem init

# Store a memory
python mnemosyne.py --dir ./mem retain --content "Apple Inc. was founded in 1976"

# Search memories
python mnemosyne.py --dir ./mem recall "Apple" --k 5

# Consolidate similar memories (pre-check)
python mnemosyne.py --dir ./mem consolidate --dry-run

# View status / health check
python mnemosyne.py --dir ./mem status --json
python mnemosyne.py --dir ./mem doctor --json

# Knowledge graph query
python mnemosyne.py --dir ./mem graph-query "Steve Jobs" --depth 2 --json

# Ledger integrity / audit
python mnemosyne.py --dir ./mem verify-integrity --json
python mnemosyne.py --dir ./mem ledger-audit <memory_id>

# Export / import
python mnemosyne.py --dir ./mem export --format json --out ./memories.json
python mnemosyne.py --dir ./mem import ./memories.json

# Migrate JSONL -> SQLite
python mnemosyne.py --dir ./mem migrate --jsonl ./mem/index.jsonl

# Start the web dashboard
python -m mnemosyne.webui.web_server --port 9090
```

### Python API 

```python
from mnemosyne import MemoryBrain

brain = MemoryBrain("./my_memories", enable_embeddings=False)
brain.ensure_init()

# Store
brain.retain("Apple Inc. was founded in 1976", fast=True)

# Recall
results = brain.recall("Apple", k=5)
for score, record, reasons in results:
    print(f"Score: {score:.4f} | {record['content']}")

# Token-budgeted recall
results, cost_report = brain.recall("Apple", k=5, budget_tokens=100)

# Conversation history
brain.add_conversation_turn("session-1", "user", "Tell me about Apple")
hits = brain.search_conversations("Apple", session_id="session-1")

# Context snapshot
snapshot = brain.build_context_prompt(query="Apple", max_chars=2000)
```

### Async API 

```python
import asyncio
from plugins.async_wrapper import AsyncMemoryBrain

async def main():
    brain = AsyncMemoryBrain("./memories", enable_embeddings=False)
    await brain.async_retain("Hello World", fast=True)
    results = await brain.async_recall("Hello", k=5)
    print(results)
    brain.close()

asyncio.run(main())
```

### MCP Server 

Run the MCP server over stdio JSON-RPC :

```bash
export MNEMOSYNE_MCP_TOKEN="your-secret-token"   # optional token auth
python -m mnemosyne.webui.mcp_server --brain-dir ./mem --namespace default
```

The MCP server exposes **13 tools **:

| Tool | Description |
| --- | --- |
| `retain` | Write a memory  |
| `recall` | Retrieve memories  |
| `retain_batch` | Batch write, ~15× speedup  |
| `stats` | Runtime statistics — writes / recalls / token savings  |
| `graph_query` | Knowledge graph query  |
| `temporal_query` | Temporal version-chain query  |
| `list_projects` | List isolated projects  |
| `doctor` | Health check — integrity, record count, disk  |
| `audit` | Audit-trail query  |
| `confidence_history` | Confidence trajectory query  |
| `memory/export-v1` | Export via Memory Exchange Protocol  |
| `memory/import-v1` | Import via Memory Exchange Protocol  |
| `memory/claim` | Claim memories from an external export  |

Connect any MCP host (Claude Desktop, Hermes Agent, etc.) by pointing it at the stdio command above.

### HTTP API 

```bash
python -m mnemosyne.webui.web_server --port 9090
```

Then open `http://127.0.0.1:9090` — a local dark dashboard  with memory browsing, graph view, stats, and a REST  endpoint. The default account `admin / mnemosyne` is created on first run; change the password after login.

---

## Plugins 

```python
# Crypto plugin (requires cryptography; degrades gracefully otherwise)
brain = MemoryBrain("./memories", plugins=["crypto"])

# Numpy vector backend (requires numpy; optional sentence-transformers model)
brain = MemoryBrain("./memories", plugins=["numpy_vector"])

# Reranker plugin
brain = MemoryBrain("./memories", plugins=["reranker"])
```

---

## Project Structure 

```
Mnemosyne7.0.0/
├── mnemosyne.py              # Thin facade re-exporting the mnemosyne package
├── mnemosyne/                # Core engine package (brain / storage / retrieval / cognitive / notary)
├── storage/                  # Storage backends (sqlite_backend / ledger / session_store / plugin_sdk)
├── context/                  # Context snapshots (snapshot_builder)
├── context_engine/           # Context compression engine (engine-agnostic core + Hermes adapter)
├── lexical/                  # Built-in synonym dictionary
├── profiles/                 # User profile management
├── providers/                # External provider adapter + multi-source router
├── security/                 # Contradiction detection + security report
├── session/                  # Conversation importer
├── visualization/            # Knowledge tree generator
├── plugins/                  # Extra plugins (HRR / Async)
├── mnemosyne_plugins/        # Official plugins (numpy_vector / crypto / reranker)
├── examples/                 # Runnable examples (Ollama / LangChain / MCP / CLI / embedded)
└── docs/                     # Documentation (architecture, modules, plugins, API, deployment)
```

## Testing 

```bash
python -m unittest discover -s tests -v
python -m unittest tests.test_plugins -v
```

## Documentation 

- `README_CN.md` — 中文说明 (Chinese README)
- `docs/` — Full docs: architecture, data model, module docs, plugin docs, API / CLI / MCP references, deployment, integration
- `COMPLIANCE.md` — HIPAA / 等保 / GDPR / PIPL compliance mapping
- `comparison.md` — Feature comparison with alternatives
- `CHANGELOG.md` — Version history
- Reports: `quality_report.md` (retrieval quality), `benchmark_report.md` (performance), `security_report.md` (security)

## License 

MIT License — see [LICENSE](LICENSE).

Built by 胡景产品负责人 (Jingkun Hu).
