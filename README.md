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
  <a href="https://github.com/FrankHu-HK/mnemosyne/blob/main/README_CN.md"><img src="https://img.shields.io/badge/Lang-中文-red?style=for-the-badge" alt="中文"></a>
  <a href="https://github.com/FrankHu-HK/mnemosyne/blob/main/README_TW.md"><img src="https://img.shields.io/badge/Lang-繁體中文-red?style=for-the-badge" alt="繁體中文"></a>
  <a href="https://github.com/FrankHu-HK/mnemosyne/blob/main/README.es.md"><img src="https://img.shields.io/badge/Lang-Español-orange?style=for-the-badge" alt="Español"></a>
  <a href="https://github.com/FrankHu-HK/mnemosyne/blob/main/README.ru.md"><img src="https://img.shields.io/badge/Lang-Русский-blue?style=for-the-badge" alt="Русский"></a>
  <a href="https://github.com/FrankHu-HK/mnemosyne/blob/main/README.de.md"><img src="https://img.shields.io/badge/Lang-Deutsch-lightgrey?style=for-the-badge" alt="Deutsch"></a>
  <a href="https://github.com/FrankHu-HK/mnemosyne/blob/main/README.th.md"><img src="https://img.shields.io/badge/Lang-ไทย-blue?style=for-the-badge" alt="ไทย"></a>
  <a href="https://github.com/FrankHu-HK/mnemosyne/blob/main/README.ko.md"><img src="https://img.shields.io/badge/Lang-한국어-green?style=for-the-badge" alt="한국어"></a>
  <a href="https://github.com/FrankHu-HK/mnemosyne/blob/main/README.ja.md"><img src="https://img.shields.io/badge/Lang-日本語-red?style=for-the-badge" alt="日本語"></a>
</p>

**Mnemosyne OS 7.0.0** — a zero-dependency (零依赖), local-first (本地优先) AI memory system (AI 记忆系统) with multi-tier forgetting (多层次遗忘), a hash-chain ledger (哈希链账本), a plugin SDK (插件 SDK), a local web dashboard (本地 Web 管理界面), and MCP (Model Context Protocol / 模型上下文协议) support.

> The only AI memory engine whose **core requires zero third-party dependencies** (仅依赖 Python 标准库 3.8+) — no vector database (向量库), no LLM (大语言模型) runtime, no cloud lock-in. Runs on a laptop, a server, or serverless infra (无服务器架构).

Use it as a **Python (Python 库) library**, a **CLI (命令行)**, an **HTTP API (API 接口)**, an **MCP server (MCP 服务器)**, or embed it via the **MCP (模型上下文协议)** stdio transport.

<table>
<tr><td><b>Zero-dependency core (零依赖核心)</b></td><td>Runs on the Python standard library alone. No numpy, no torch, no vector DB, no LLM required to store and recall memories.</td></tr>
<tr><td><b>Multi-tier memory (多层次记忆)</b></td><td>Hot / warm / cold tiers with economic forgetting (遗忘经济学) — migrate low-value memories, never silently delete them.</td></tr>
<tr><td><b>Hash-chain ledger (哈希链账本)</b></td><td>SHA-256 chained ledger — <code>verify_chain()</code> detects tampering and locates the exact corrupted record.</td></tr>
<tr><td><b>Plugin SDK (插件 SDK)</b></td><td><code>VectorBackendPlugin</code> / <code>CryptoPlugin</code> / <code>RerankerPlugin</code> + official plugins (<code>numpy_vector</code>, <code>crypto</code>, <code>reranker</code>, <code>hrr</code>, <code>async</code>, <code>context-engine</code>).</td></tr>
<tr><td><b>MCP server (MCP 服务器)</b></td><td>13 tools over stdio JSON-RPC, with token auth (令牌鉴权) and multi-tenant namespaces (多租户命名空间隔离).</td></tr>
<tr><td><b>Web dashboard (Web 管理界面)</b></td><td>Tech-aesthetic local dark dashboard (本地科技感暗色面板), no external CDN — served from <code>web_server.py</code>.</td></tr>
<tr><td><b>Async API (异步 API)</b></td><td><code>AsyncMemoryBrain</code> asyncio wrapper for high-throughput ingestion.</td></tr>
<tr><td><b>Chinese-optimized (中文优化)</b></td><td>Bigram tokenization (二分词) + FTS5 + built-in synonym dictionary (内置同义词词典).</td></tr>
<tr><td><b>Security notary (安全检查)</b></td><td>Detects credentials, invisible Unicode, and HTML injection; field-level redaction (字段级脱敏) before write.</td></tr>
</table>

---

## Quick Install (快速安装)

### From PyPI (PyPI 安装)

```bash
pip install mnemosyne-os
```

### Zero-dependency core (零依赖核心 — no pip install required)

```bash
# Core runs on the Python standard library alone
python -c "from mnemosyne import MemoryBrain; print('Ready!')"
```

### Development install (开发模式安装)

```bash
git clone https://github.com/FrankHu-HK/mnemosyne.git
cd mnemosyne
pip install -e .
```

---

## Getting Started (快速开始)

### CLI (命令行)

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

### Python API (Python 接口)

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

### Async API (异步接口)

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

### MCP Server (MCP 服务器)

Run the MCP server over stdio JSON-RPC (标准 JSON-RPC 传输):

```bash
export MNEMOSYNE_MCP_TOKEN="your-secret-token"   # optional token auth
python -m mnemosyne.webui.mcp_server --brain-dir ./mem --namespace default
```

The MCP server exposes **13 tools (13 个工具)**:

| Tool | Description |
| --- | --- |
| `retain` | Write a memory (写入记忆) |
| `recall` | Retrieve memories (检索记忆) |
| `retain_batch` | Batch write, ~15× speedup (批量写入) |
| `stats` | Runtime statistics — writes / recalls / token savings (运行统计) |
| `graph_query` | Knowledge graph query (知识图谱查询) |
| `temporal_query` | Temporal version-chain query (时序查询) |
| `list_projects` | List isolated projects (列出项目) |
| `doctor` | Health check — integrity, record count, disk (健康检查) |
| `audit` | Audit-trail query (审计追踪) |
| `confidence_history` | Confidence trajectory query (置信度历史) |
| `memory/export-v1` | Export via Memory Exchange Protocol (记忆交换协议导出) |
| `memory/import-v1` | Import via Memory Exchange Protocol (记忆交换协议导入) |
| `memory/claim` | Claim memories from an external export (认领外部记忆) |

Connect any MCP host (Claude Desktop, Hermes Agent, etc.) by pointing it at the stdio command above.

### HTTP API (API 接口 / Web 管理界面)

```bash
python -m mnemosyne.webui.web_server --port 9090
```

Then open `http://127.0.0.1:9090` — a local dark dashboard (本地暗色面板) with memory browsing, graph view, stats, and a REST (表述性状态传递) endpoint. The default account `admin / mnemosyne` is created on first run; change the password after login.

---

## Plugins (插件)

```python
# Crypto plugin (requires cryptography; degrades gracefully otherwise)
brain = MemoryBrain("./memories", plugins=["crypto"])

# Numpy vector backend (requires numpy; optional sentence-transformers model)
brain = MemoryBrain("./memories", plugins=["numpy_vector"])

# Reranker plugin
brain = MemoryBrain("./memories", plugins=["reranker"])
```

---

## Project Structure (项目结构)

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

## Testing (测试)

```bash
python -m unittest discover -s tests -v
python -m unittest tests.test_plugins -v
```

## Documentation (文档)

- `README_CN.md` — 中文说明 (Chinese README)
- `docs/` — Full docs: architecture, data model, module docs, plugin docs, API / CLI / MCP references, deployment, integration
- `COMPLIANCE.md` — HIPAA / 等保 / GDPR / PIPL compliance mapping
- `comparison.md` — Feature comparison with alternatives
- `CHANGELOG.md` — Version history
- Reports: `quality_report.md` (retrieval quality), `benchmark_report.md` (performance), `security_report.md` (security)

## License (许可证)

MIT License — see [LICENSE](LICENSE).

Built by 胡景产品负责人 (Jingkun Hu).
