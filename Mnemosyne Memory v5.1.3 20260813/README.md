# Mnemosyne Memory v5.1.3 Stable

[English](README.md) | [中文](README_CN.md)

> **The L1 Memory Cache for AI Agents — zero dependencies, 100% local, multilingual, cross-framework. Give any AI a real long-term memory.**

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.21870436.svg)](https://doi.org/10.5281/zenodo.21870436)
[![Code DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.21870790.svg)](https://doi.org/10.5281/zenodo.21870790)
[![Version](https://img.shields.io/badge/version-5.1.3-blue)]()
[![Python](https://img.shields.io/badge/python-3.8%2B-green)]()
[![Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)]()
[![License](https://img.shields.io/badge/license-MIT-orange)]()
[![Hindsight](https://img.shields.io/badge/Hindsight-9.58%2F10-gold)]()

---

## ⚡ Install (10 seconds)

**For AI Agent users** — tell your AI:
> Install @user_663a53c6/mnemosyne-memory from SkillHub

**For developers** — copy one file:
```bash
cp mnemosyne.py your-project/
```

**Verify**:
```bash
python -c "from mnemosyne import MemoryBrain; b=MemoryBrain('test'); b.ensure_init(); b.retain('hello'); print('✓ Mnemosyne OK')"
```

**Query stats** (auto-displayed after each recall):
```python
brain.stats_print()
```

Or directly in terminal:
```bash
python -c "from mnemosyne import MemoryBrain; b=MemoryBrain('your_brain'); b.stats_print()"
# → Current/Today/Total 3-column · 9-dimensional Token monitoring table
```

---

## 🚀 Quick Start

```python
from mnemosyne import MemoryBrain

brain = MemoryBrain("my_brain")
brain.ensure_init()              # first time only
brain.retain("Alice joined Acme Corp in 2024")
results = brain.recall("When did Alice join?", k=3)
# → (0.891, {"content":"Alice joined Acme Corp in 2024",...})
```

---

## In One Sentence

**Before calling any LLM, Mnemosyne pre-filters massive memory stores down to the Top-10 most relevant items — at zero compute cost for retrieval, saving 80%+ tokens with <10ms latency. But it's much more than token savings: it's the world's only single-file, zero-dependency, fully-local AI memory engine — with native support for 7 languages, 8+ Agent frameworks, knowledge-graph reasoning, multi-hop retrieval, automatic reflection, and memory consolidation. Copy one .py file, and your AI gains persistent long-term memory that never forgets.**

---

## Key Highlights

| Feature | Details |
|------|------|
| 🧠 **Real Long-Term Memory** | Cross-session, cross-platform — AI that remembers |
| 📦 **Single-File, Zero Deps** | One .py file. No pip install. No external libraries. |
| 🔒 **100% Local** | Memory stored on your disk. No network. No upload. |
| 🌐 **7 Native Languages** | Chinese · English · 日本語 · 한국어 · Français · Deutsch · Русский |
| 🤖 **8+ Agent Frameworks** | Hermes · OpenClaw · LangChain · AutoGPT · CrewAI · Dify · Coze · OpenAI |
| 🔗 **Knowledge Graph** | Auto-extract entity-relation, multi-hop reasoning |
| ⚡ **Millisecond Retrieval** | Inverted index + 5-way fusion, pure CPU <10ms |
| 💰 **80%+ Token Savings** | L1 pre-retrieval filters to Top-10, drastically cutting LLM cost |
| 🧪 **Verified Benchmarks** | Hindsight 9.58/10 · Session Recall 85.0% · Zenodo-published paper |
| 🕐 **Temporal Query** | `temporal_query()` — query memory by time, version chain & timeline |
| 🏥 **Health Check** | `doctor()` self-check + `memory_repair()` auto-recovery |
| 📂 **Multi-Project Isolation** | `project=` param on retain/recall + `list_projects()` |
| 🔌 **MCP Server** | 8-tool MCP server — standard protocol for any MCP client |

---

## 📊 Benchmark Scores

| Benchmark | Score | Notes |
|------|:--:|------|
| **Hindsight 14-Dim Architecture** | **9.58/10** | Surpasses Hindsight 8.69 baseline; 13/14 dimensions ahead |
| **Session Recall@10** | **85.0%** | Locating correct conversation among 18,000+ records |
| **Token Savings** | **80%+** | L1 coarse-filter → feed only Top-10 to LLM |
| **Retrieval Latency** | **<10ms** | Inverted index + 5-way fusion, pure CPU |
| **Write Speed** | **~12ms/item** | Fast Write mode, millions-scale capable |

---

## 🚀 Advanced Features

```python
# Multi-project isolation — same engine, isolated memory per project
brain.retain("Acme uses Kafka", project="acme")
brain.recall("message queue?", project="acme")   # only sees acme memories
brain.list_projects()                            # -> ["acme", "default", ...]

# Temporal query — reconstruct what happened when
brain.temporal_query(entity="deployment")        # -> version chain sorted by time

# Health check & self-repair
brain.doctor()          # scan integrity, record count, disk usage
brain.memory_repair()   # auto-recover from corruption

# Batch write — 15x faster
brain.retain_batch(["a", "b", "c"])
```

**MCP Server** (8 tools: `retain`, `recall`, `stats`, `graph_query`, `retain_batch`, `doctor`, `temporal_query`, `list_projects`):
```bash
python mcp_server.py   # stdio MCP server, drop into any MCP client
```

---

## 🆚 Competitive Landscape

| Capability | Mnemosyne | Generic Embedding+Vector DB | Cloud Memory API |
|------|:---:|:---:|:---:|
| External Dependencies | **0** | PyTorch + C++ | API Key |
| GPU Required | **No** | Yes | No (but billed) |
| Session Recall | **85.0%** | 80–85% | 80–90% |
| Retrieval Latency | **<10ms** | 100–500ms | 200–1000ms |
| Token Cost | **$0** | Embedding cost | API fees |
| Privacy Compliance | **100% Local** | Local but needs GPU | Data in cloud |
| Deployment | **Copy 1 file** | pip install (GBs) | Sign-up + API key |

---

## 🏗️ Architecture

```
User Query
  ↓
┌─────────────────────────────────┐
│     Mnemosyne L1 Memory Cache    │
│   BM25 + Vector + Graph + Time   │
│   + Confidence → 5-Way Fusion   │
│   0 GPU / 0 Token / <10ms       │
└─────────────────────────────────┘
  ↓ Top-10 (85.0% coverage)
┌─────────────────────────────────┐
│     LLM / Reader (any model)     │
│   Reads only Top-10, saves 80%   │
│   Generates precise answers     │
└─────────────────────────────────┘
  ↓
Precise Answer
```

---

## 🌐 Supported Agent Frameworks

| Framework | Integration | Lines of Code |
|------|:--:|:--:|
| Hermes Agent | Skill plugin | 10 |
| OpenClaw | Tool registration | 8 |
| LangChain | Tool wrapper | 12 |
| AutoGPT | Python hook | 6 |
| CrewAI | Agent subclass | 10 |
| Dify / Coze | REST API | 1 command |
| **Any Python project** | `import mnemosyne` | **1 line** |

---

## 🔐 Privacy

- **100% local runtime** — no internet required
- **Zero API calls** — no API keys needed
- **You own your data** — JSONL format on disk; view / backup / delete anytime
- **Compliance-friendly** — deployable in finance, healthcare, government, military

---

## 📦 Installation

```bash
# Option 1: Direct copy
cp mnemosyne.py your_project/scripts/

# Option 2: Clone repository
git clone https://github.com/FrankHu-HK/mnemosyne.git
```

**No pip install. No Docker. No database.**

---

## 📖 Documentation

- [English README](README.md) · [中文 README](README_CN.md)
- [Competitive Comparison](comparison.md)
- [Paper (Zenodo)](https://doi.org/10.5281/zenodo.21870436)
- [Code (Zenodo)](https://doi.org/10.5281/zenodo.21870790)

---

## 📄 Citation

```bibtex
@article{hu2026mnemosyne,
  title={Mnemosyne: Pushing Lexical Retrieval to the LongMemEval Ceiling
         with a Zero-Dependency Architecture},
  author={Hu, Jingkun},
  year={2026},
  doi={10.5281/zenodo.21870436},
  url={https://doi.org/10.5281/zenodo.21870436}
}
```

---

**Mnemosyne** — 100KB of code to give your AI true long-term memory.
