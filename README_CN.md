<p align="center">
  <img src="assets/banner.png" alt="Mnemosyne OS" width="100%">
</p>

# Mnemosyne OS ☤

<p align="center">
  <a href="https://pypi.org/project/mnemosyne-os/">Mnemosyne OS</a> | <a href="https://github.com/FrankHu-HK/mnemosyne">GitHub</a> | <a href="README.md">English</a>
</p>

<p align="center">
  <a href="https://pypi.org/project/mnemosyne-os/"><img src="https://img.shields.io/badge/PyPI-mnemosyne--os-blue?style=for-the-badge" alt="PyPI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="许可证: MIT"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.8%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.8+"></a>
  <a href="https://modelcontextprotocol.io/"><img src="https://img.shields.io/badge/MCP-20%20Tools-00ADD8?style=for-the-badge" alt="模型上下文协议"></a>
 <a href="https://pepy.tech/projects/mnemosyne-os"><img src="https://img.shields.io/pepy/dt/mnemosyne-os?style=for-the-badge" alt="Downloads"></a>
 <a href="https://x.com/mnemosyne_oos"><img src="https://img.shields.io/badge/X-@mnemosyne_oos-black?style=for-the-badge&logo=x&logoColor=white" alt="X"></a>
  <a href="https://github.com/FrankHu-HK/mnemosyne/blob/main/README.md"><img src="https://img.shields.io/badge/Lang-English-blue?style=for-the-badge" alt="English"></a>
  <a href="https://github.com/FrankHu-HK/mnemosyne/blob/main/README_TW.md"><img src="https://img.shields.io/badge/Lang-繁體中文-red?style=for-the-badge" alt="繁體中文"></a>
  <a href="https://github.com/FrankHu-HK/mnemosyne/blob/main/README.es.md"><img src="https://img.shields.io/badge/Lang-Español-orange?style=for-the-badge" alt="Español"></a>
  <a href="https://github.com/FrankHu-HK/mnemosyne/blob/main/README.ru.md"><img src="https://img.shields.io/badge/Lang-Русский-blue?style=for-the-badge" alt="Русский"></a>
  <a href="https://github.com/FrankHu-HK/mnemosyne/blob/main/README.de.md"><img src="https://img.shields.io/badge/Lang-Deutsch-lightgrey?style=for-the-badge" alt="Deutsch"></a>
  <a href="https://github.com/FrankHu-HK/mnemosyne/blob/main/README.th.md"><img src="https://img.shields.io/badge/Lang-ไทย-blue?style=for-the-badge" alt="ไทย"></a>
  <a href="https://github.com/FrankHu-HK/mnemosyne/blob/main/README.ko.md"><img src="https://img.shields.io/badge/Lang-한국어-green?style=for-the-badge" alt="한국어"></a>
  <a href="https://github.com/FrankHu-HK/mnemosyne/blob/main/README.ja.md"><img src="https://img.shields.io/badge/Lang-日本語-red?style=for-the-badge" alt="日本語"></a>
</p>

**Mnemosyne OS 7.0.2** — 零依赖 (zero-dependency)、本地优先 (local-first) 的 AI 记忆系统 (AI memory system)，支持多层次遗忘 (multi-tier forgetting)、哈希链账本 (hash-chain ledger)、插件 SDK (plugin SDK)、本地 Web 管理界面 (local web dashboard) 与 MCP (Model Context Protocol / 模型上下文协议) 协议。

> 唯一核心**零第三方依赖** (仅依赖 Python 标准库 3.8+) 的 AI 记忆引擎 —— 无需向量库 (vector database)、无需大语言模型 (LLM) 运行时、无云端锁定 (no cloud lock-in)。可在笔记本、服务器或无服务器架构 (serverless) 上运行。

可作为 **Python 库 (Python library)**、**命令行 (CLI)**、**HTTP API (API 接口)**、**MCP 服务器 (MCP server)** 使用，或通过 **MCP (模型上下文协议)** stdio 传输嵌入。

<table>
<tr><td><b>零依赖核心 (Zero-dependency core)</b></td><td>仅需 Python 标准库即可运行，存储与检索记忆无需 numpy、torch、向量库或大语言模型。</td></tr>
<tr><td><b>多层次记忆 (Multi-tier memory)</b></td><td>热/温/冷三层存储与遗忘经济学 (economic forgetting) —— 低价值记忆迁移，而非静默删除。</td></tr>
<tr><td><b>哈希链账本 (Hash-chain ledger)</b></td><td>SHA-256 链式账本，<code>verify_chain()</code> 检测篡改并定位被篡改记录。</td></tr>
<tr><td><b>插件 SDK (Plugin SDK)</b></td><td><code>VectorBackendPlugin</code> / <code>CryptoPlugin</code> / <code>RerankerPlugin</code> + 官方插件 (<code>numpy_vector</code> / <code>crypto</code> / <code>reranker</code> / <code>hrr</code> / <code>async</code> / <code>context-engine</code>)。</td></tr>
<tr><td><b>MCP 服务器 (MCP server)</b></td><td>20 个工具 (stdio JSON-RPC)，支持令牌鉴权 (token auth) 与多租户命名空间隔离 (multi-tenant namespaces)。</td></tr>
<tr><td><b>Web 管理界面 (Web dashboard)</b></td><td>本地科技感暗色面板 (dark dashboard)，无外部 CDN —— 由 <code>web_server.py</code> 提供。</td></tr>
<tr><td><b>异步 API (Async API)</b></td><td><code>AsyncMemoryBrain</code> 异步封装，支持高吞吐写入。</td></tr>
<tr><td><b>中文优化 (Chinese-optimized)</b></td><td>二分词 (bigram tokenization) + FTS5 + 内置同义词词典 (synonym dictionary)。</td></tr>
<tr><td><b>安全检查 (Security notary)</b></td><td>检测凭据、不可见 Unicode 与 HTML 注入；写入前字段级脱敏 (field-level redaction)。</td></tr>
</table>

---

## 快速安装 (Quick Install)

### 通过 PyPI 安装

```bash
pip install mnemosyne-os
```

### 零依赖核心（无需 pip install）

```bash
# 核心仅依赖 Python 标准库即可运行
python -c "from mnemosyne import MemoryBrain; print('Ready!')"
```

### 开发模式安装

```bash
git clone https://github.com/FrankHu-HK/mnemosyne.git
cd mnemosyne
pip install -e .
```

---

## 快速开始 (Getting Started)

### 命令行 (CLI)

```bash
# 初始化记忆数据库
python mnemosyne.py --dir ./mem init

# 存储一条记忆
python mnemosyne.py --dir ./mem retain --content "苹果公司成立于1976年"

# 检索记忆
python mnemosyne.py --dir ./mem recall "苹果" --k 5

# 合并相似记忆（预检）
python mnemosyne.py --dir ./mem consolidate --dry-run

# 查看状态 / 健康检查
python mnemosyne.py --dir ./mem status --json
python mnemosyne.py --dir ./mem doctor --json

# 知识图谱查询
python mnemosyne.py --dir ./mem graph-query "张三" --depth 2 --json

# 账本完整性校验 / 审计
python mnemosyne.py --dir ./mem verify-integrity --json
python mnemosyne.py --dir ./mem ledger-audit <memory_id>

# 导出 / 导入
python mnemosyne.py --dir ./mem export --format json --out ./memories.json
python mnemosyne.py --dir ./mem import ./memories.json

# 迁移 JSONL -> SQLite
python mnemosyne.py --dir ./mem migrate --jsonl ./mem/index.jsonl

# 启动 Web 管理界面
python -m mnemosyne.webui.web_server --port 9090
```

### Python 接口 (Python API)

```python
from mnemosyne import MemoryBrain

brain = MemoryBrain("./my_memories", enable_embeddings=False)
brain.ensure_init()

# 存储
brain.retain("苹果公司成立于1976年", fast=True)

# 检索
results = brain.recall("苹果", k=5)
for score, record, reasons in results:
    print(f"Score: {score:.4f} | {record['content']}")

# 按 Token 预算检索
results, cost_report = brain.recall("苹果", k=5, budget_tokens=100)

# 会话历史
brain.add_conversation_turn("session-1", "user", "Tell me about Apple")
hits = brain.search_conversations("Apple", session_id="session-1")

# 上下文快照
snapshot = brain.build_context_prompt(query="Apple", max_chars=2000)
```

### 异步接口 (Async API)

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

### MCP 服务器 (MCP Server)

通过 stdio JSON-RPC（标准 JSON-RPC 传输）运行 MCP 服务器：

```bash
export MNEMOSYNE_MCP_TOKEN="your-secret-token"   # 可选令牌鉴权
python -m mnemosyne.webui.mcp_server --brain-dir ./mem --namespace default
```

MCP 服务器暴露 **20 个工具 (20 tools)**：

| 工具 (Tool) | 说明 (Description) |
| --- | --- |
| `retain` | 写入记忆 |
| `recall` | 检索记忆 |
| `retain_batch` | 批量写入，约 15 倍加速 |
| `stats` | 运行统计 —— 写入/召回/Token 节省 |
| `graph_query` | 知识图谱查询 |
| `temporal_query` | 时序版本链查询 |
| `list_projects` | 列出隔离项目 |
| `doctor` | 健康检查 —— 完整性/记录数/磁盘 |
| `audit` | 审计追踪查询 |
| `confidence_history` | 置信度轨迹查询 |
| `memory/export-v1` | 记忆交换协议导出 |
| `memory/import-v1` | 记忆交换协议导入 |
| `memory/claim` | 认领外部导出记忆 |
| `forget` | 遗忘一条记忆 —— 可信度归零并软删除（支持 `memory_id`，或自然语言 `query`） |
| `consolidate` | 压缩引擎·记忆合并 —— 把高度相似/同义的记忆聚合并合并为一条代表性记忆，原记忆标记 `consolidated`（`min_similarity` / `max_group` / `generate_summary` / `dry_run`） |
| `reflect` | 压缩引擎·增强反思 —— 统计记忆总量、类型 / 层 / 事实类型 / 可信度分布，抽取高频实体，检测事实冲突与时序密度；`deep=true` 追加认知模式发现 |
| `dedup` | 压缩引擎·去重 —— 基于内容指纹 + 向量 / 词频相似度检测重复与近似记忆；`dry_run=true` 仅报告 |

将任意 MCP 宿主（Claude Desktop、Hermes Agent 等）指向上述 stdio 命令即可接入。

### HTTP API / Web 管理界面

```bash
python -m mnemosyne.webui.web_server --port 9090
```

打开 `http://127.0.0.1:9090` —— 本地暗色面板 (dark dashboard)，支持记忆浏览、图谱视图、统计与 REST (表述性状态传递) 接口。首次运行创建默认账号 `admin / mnemosyne`，登录后请修改密码。

---

## 插件 (Plugins)

```python
# Crypto 插件（需 cryptography；缺失时优雅降级）
brain = MemoryBrain("./memories", plugins=["crypto"])

# Numpy 向量后端（需 numpy；可选 sentence-transformers 模型）
brain = MemoryBrain("./memories", plugins=["numpy_vector"])

# Reranker 插件
brain = MemoryBrain("./memories", plugins=["reranker"])
```

---

## 项目结构 (Project Structure)

```
Mnemosyne7.0.2/
├── mnemosyne.py              # 薄门面，重新导出 mnemosyne 包
├── mnemosyne/                # 核心引擎包（brain / storage / retrieval / cognitive / notary）
├── storage/                  # 存储后端（sqlite_backend / ledger / session_store / plugin_sdk）
├── context/                  # 上下文快照（snapshot_builder）
├── context_engine/           # 上下文压缩引擎（引擎无关核心 + Hermes 适配器）
├── lexical/                  # 内置同义词词典
├── profiles/                 # 用户画像管理
├── providers/                # 外部 Provider 适配器 + 多源路由
├── security/                 # 矛盾检测 + 安全报告
├── session/                  # 会话导入器
├── visualization/            # 知识树生成器
├── plugins/                  # 额外插件（HRR / Async）
├── mnemosyne_plugins/        # 官方插件（numpy_vector / crypto / reranker / qdrant_backend）
├── examples/                 # 可运行示例（Ollama / LangChain / MCP / CLI / embedded）
└── docs/                     # 文档（架构、模块、插件、API、部署）
```

## 测试 (Testing)

```bash
python -m unittest discover -s tests -v
python -m unittest tests.test_plugins -v
```

## 文档 (Documentation)

- `README.md` — 英文说明 (English README)
- `docs/DEPLOY_DEEPSEEK_HARNESS.md` — 部署principle：接入 DeepSeek Harness（通过 MCP）
- `docs/KNOWN_DEFECTS.md` — 7.0.2 记忆栈已确证缺陷：事实、实测、影响与修法
- `docs/RECALL_STRATEGY.md` — 召回机制与每轮注入策略评估
- `docs/ACCEPTANCE_GUIDE.md` — 验收principle（附 `scripts/verify_memory_lifecycle.py`）
- `docs/` — 完整文档：架构、数据模型、模块文档、插件文档、API / CLI / MCP 参考、部署、集成
- `COMPLIANCE.md` — HIPAA / 等保 / GDPR / PIPL 合规映射
- `comparison.md` — 与同类框架的功能对比
- `CHANGELOG.md` — 版本变更记录
- 报告：`quality_report.md`（检索质量）、`benchmark_report.md`（性能基准）、`security_report.md`（安全测试）

## 许可证 (License)

MIT 许可证 —— 见 [LICENSE](LICENSE)。

开发者：胡景产品负责人 (Jingkun Hu)。
