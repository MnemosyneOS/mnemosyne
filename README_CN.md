<p align="center">
  <img src="assets/banner.png" alt="mnemosyne OS" width="100%">
</p>

# mnemosyne OS

<p align="center">
  <a href="https://pypi.org/project/mnemosyne-os/">PyPI</a> ·
  <a href="https://github.com/FrankHu-HK/mnemosyne">GitHub</a> ·
  <a href="README.md">English</a>
</p>

<p align="center">
  <a href="https://pypi.org/project/mnemosyne-os/"><img src="https://img.shields.io/badge/PyPI-mnemosyne--os-blue?style=for-the-badge" alt="PyPI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="许可证: MIT"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.8%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.8+"></a>
  <a href="#-mcp-服务器"><img src="https://img.shields.io/badge/MCP-31%20Tools-00ADD8?style=for-the-badge" alt="模型上下文协议"></a>
  <a href="#-快速开始"><img src="https://img.shields.io/badge/dependencies-0-brightgreen?style=for-the-badge" alt="零依赖"></a>
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

**Mnemosyne OS 8.0.0** —— 零依赖、本地优先的 AI 记忆系统。自带图记忆、多模态摄入、
重排、时序推理、哈希链审计账本、无损压缩，以及 31 个 MCP 工具。

> 唯一一个**核心真的零三方依赖**的 AI 记忆引擎 —— 不需要向量数据库、不需要 LLM 运行时、
> 不需要云账号。`install_requires` 是空列表。笔记本、服务器、无服务器环境都能跑。

可以当 **Python 库**、**CLI**、**HTTP API**、**MCP 服务器**用。

---

## 🚀 快速开始

### 安装

```bash
pip install mnemosyne-os          # 核心：零三方依赖
```

### 零配置就能记住和想起

```python
from mnemosyne import Memory

m = Memory()                       # 内置嵌入器 + 规则式抽取器
m.add("I prefer dark mode and use vim keybindings. My name is Alice.",
      user_id="alice")

for hit in m.search("what does alice prefer", filters={"user_id": "alice"})["results"]:
    print(f"{hit['score']:.3f}  {hit['memory']}")
```

离线、无 API Key、不下载模型、不装数据库就能跑。也正因为如此，才有下一节。

### 需要更强召回时，再接真实模型

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

每个组件都独立可选。某个供应商建不起来时，会退到内置等价实现，并且**如实上报**，
绝不静默降级：

```python
m.describe()["degraded"]
# {'llm': {'requested': 'openai', 'used': 'rules', 'reason': 'no API key configured',
#          'hint': 'Set MNEMOSYNE_LLM_OPENAI_API_KEY ...'}}
```

### 或者用 CLI

```bash
mnemosyne init
mnemosyne add "I prefer dark mode and vim keybindings" --user-id alice
mnemosyne search "what does alice prefer" --user-id alice
mnemosyne list  --user-id alice
mnemosyne event --limit 10
mnemosyne --agent search "preferences" --user-id alice   # 供工具循环消费的 JSON 信封
```

### 或者用 MCP 服务器

```json
{
  "mcpServers": {
    "mnemosyne": {
      "command": "python",
      "args": ["-m", "mnemosyne.webui.mcp_server",
               "--brain-dir", "./mem", "--namespace", "default"],
      "env": { "MNEMOSYNE_MCP_TOKEN": "<随机 32+ 位>" }
    }
  }
}
```

### 或者用 HTTP API

```bash
mnemosyne-web --port 9090          # 控制台 + REST 同一个端口
curl -X POST http://127.0.0.1:8788/v3/memories/add/ \
  -H "Authorization: Bearer $MNEMOSYNE_API_KEY" -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"我 2023 年搬到了柏林。"}],"user_id":"alice"}'
```

---

## 📊 基准测试

用仓库内随包交付的复现脚本实测得出。复现脚本：
`scripts/verify_recall_quality.py`、`scripts/verify_precision_recall.py`。

| 基准 | 分数 | 衡量什么 |
| --- | --- | --- |
| LongMemEval | **96.2** | 长跨度对话召回 |
| LoCoMo | **94.8** | 多会话对话记忆 |
| BEAM (1M) | **68.5** | 1M token 上下文预算下的召回 |
| BEAM (10M) | **53.9** | 10M token 上下文预算下的召回 |

满分 100。

---

## 🧩 功能一览

<table>
<tr><td><b>记忆 API</b></td><td><code>Memory</code> / <code>AsyncMemory</code> / <code>MemoryClient</code>，方法面齐全：<code>add</code> <code>get</code> <code>get_all</code> <code>search</code> <code>update</code> <code>delete</code> <code>delete_all</code> <code>history</code> <code>reset</code> <code>close</code> <code>from_config</code>。</td></tr>
<tr><td><b>四维作用域</b></td><td><code>user_id</code> / <code>agent_id</code> / <code>run_id</code> / <code>app_id</code> —— 做的是<b>物理隔离</b>：每个作用域一个 SQLite 文件，而不是共享行上加过滤条件。</td></tr>
<tr><td><b>过滤语言</b></td><td><code>eq</code> <code>ne</code> <code>gt</code> <code>gte</code> <code>lt</code> <code>lte</code> <code>in</code> <code>nin</code> <code>contains</code> <code>icontains</code> <code>wildcard</code>，支持 <code>AND</code>/<code>OR</code>/<code>NOT</code> 任意嵌套。</td></tr>
<tr><td><b>单遍 ADD-only 抽取</b></td><td>每次写入一次模型调用，记忆只累加、不覆盖。因为「不改写」，坏抽取只会引入噪声，永远不会毁掉真事实。</td></tr>
<tr><td><b>图记忆常开</b></td><td>实体链接与多跳遍历由同一个 SQLite 文件承载，不需要任何外部图数据库。</td></tr>
<tr><td><b>多模态摄入</b></td><td>支持 OpenAI / Anthropic / Gemini 三种图片内容格式（含音频）。配了视觉模型就生成描述，没配就存引用——不丢东西。</td></tr>
<tr><td><b>多信号检索</b></td><td>语义 + BM25 关键词 + 实体图 + 时序 + 标签五路融合，带标定过的相关性下限与词面兜底。</td></tr>
<tr><td><b>时序推理</b></td><td>观测日期、相对时间解析、过期语义，以及按实体的版本链。</td></tr>
<tr><td><b>分层记忆</b></td><td>热 / 温 / 冷三层 + 遗忘经济学：低价值记忆降级压缩，绝不静默删除。</td></tr>
<tr><td><b>无损压缩（AIC）</b></td><td>把一条记忆压成「指针 + 结构化事实 + 内容原子」。数字、日期、金额、型号在任何层级都完整保留；<code>expand()</code> 逐字取回原文并校验哈希。</td></tr>
<tr><td><b>哈希链审计账本</b></td><td>SHA-256 链式账本；<code>verify_integrity()</code> 能发现篡改并指出具体是哪一条被改。</td></tr>
<tr><td><b>异步 API 与事件</b></td><td><code>AsyncMemory</code> 支撑高吞吐写入，加上持久化操作日志，让「已受理的写入」跨进程可查。</td></tr>
<tr><td><b>中文优化</b></td><td>Bigram 分词 + FTS5 + 内置同义词词典，同时完整支持拉丁文字。</td></tr>
<tr><td><b>安全公证器</b></td><td>写入前检测凭据、不可见 Unicode 与 HTML 注入，并做字段级脱敏。</td></tr>
</table>

---

## 🔌 支持的集成

所有适配器都是可选的。标 **stdlib** 的不需要任何三方包——直接用 `urllib` 走 HTTP；
标 **sdk** 的惰性加载 SDK，缺包时会明确告诉你装哪个。

### LLM 供应商（20）

| 传输方式 | 供应商 |
| --- | --- |
| **stdlib HTTP** | `openai` `openai_structured` `azure_openai` `azure_openai_structured` `ollama` `anthropic` `gemini` `groq` `together` `deepseek` `minimax` `xai` `sarvam` `openrouter` `litellm` `lmstudio` `vllm` |
| **sdk** | `langchain` `aws_bedrock` |
| **内置** | `rules` —— 确定性离线抽取器，所以完全不配模型也能 `add()` |

### 嵌入模型（13）

| 传输方式 | 供应商 |
| --- | --- |
| **stdlib HTTP** | `openai` `azure_openai` `ollama` `gemini` `vertexai` `together` `lmstudio` `huggingface` |
| **sdk** | `fastembed` `langchain` `aws_bedrock` |
| **内置** | `builtin`（128 维、零依赖、确定性）· `hashing`（任意维度、离线） |

### 向量库（28）

| 传输方式 | 向量库 |
| --- | --- |
| **内嵌** | `builtin`（一个 SQLite 文件同时存记忆和向量）· `memory` · `generic`（声明式 REST） |
| **stdlib HTTP** | `qdrant` `pinecone` `elasticsearch` `opensearch` `weaviate` `upstash_vector` `turbopuffer` |
| **sdk** | `chroma` `pgvector` `milvus` `mongodb` `redis` `valkey` `azure_ai_search` `azure_mysql` `baidu` `cassandra` `databricks` `faiss` `langchain` `neptune` `oracledb` `s3_vectors` `supabase` `vertex_ai_vector_search` |

### 图数据库（6）

`builtin`（原生 SQLite 三元组）· `neo4j` · `memgraph` · `neptune` · `kuzu` · `sparql`（任意 SPARQL 1.1 端点）

### 重排器（5）

`llm` · `cohere` · `zero_entropy` · `huggingface` · `sentence_transformer`

### 框架适配

LangChain · LlamaIndex · CrewAI · Dify · n8n · Vercel AI SDK · Ollama · MCP（stdio + Streamable HTTP）

---

## 🛠 MCP 服务器

以 stdio JSON-RPC 运行：

```bash
export MNEMOSYNE_MCP_TOKEN="your-secret-token"   # 可选，但建议设置
python -m mnemosyne.webui.mcp_server --brain-dir ./mem --namespace default
```

**31 个工具** —— 20 个原生工具，外加 11 个沿用通行 Agent 记忆工具命名的工具，
已有的 MCP 客户端不必改写工具定义就能直接指向 Mnemosyne。

**原生（20）：**

| 工具 | 说明 |
| --- | --- |
| `retain` | 写入一条记忆 |
| `recall` | 检索记忆 |
| `retain_batch` | 批量写入，约快 15× |
| `forget` | 遗忘一条记忆 —— 可用 id，也可用自然语言查询定位 |
| `capsule` | 把一条记忆压成 指针 + 事实 + 原子 |
| `expand` | 逐字取回胶囊原文 |
| `recall_health` | 只读的召回质量指标 |
| `consolidate` | 把近似重复的记忆合并成一条代表 |
| `reflect` | 统计、高频实体、冲突检测、认知模式 |
| `dedup` | 检测重复与近似重复 |
| `graph_query` | 知识图谱遍历 |
| `temporal_query` | 版本链查询 |
| `list_projects` | 列出隔离的项目 |
| `doctor` | 健康检查 —— 完整性、条数、磁盘、后端状态 |
| `stats` | 运行统计 |
| `audit` | 审计链查询 |
| `confidence_history` | 可信度轨迹 |
| `memory/export-v1` | 按 Memory Exchange Protocol 导出 |
| `memory/import-v1` | 按 Memory Exchange Protocol 导入 |
| `memory/claim` | 从外部导出接管记忆 |

**客户端兼容（11）：**

| 工具 | 说明 |
| --- | --- |
| `add_memory` | 为用户/智能体保存文本或对话历史 |
| `search_memories` | 带过滤条件的语义检索 |
| `get_memories` | 结构化过滤 + 分页列表 |
| `get_memory` | 按 id 取一条 |
| `update_memory` | 覆盖文本和/或元数据 |
| `delete_memory` | 删除一条 |
| `delete_all_memories` | 清空某个作用域 |
| `delete_entities` | 删除实体并级联 |
| `list_entities` | 列出 users/agents/apps/runs |
| `list_events` | 列出记忆操作 |
| `get_event_status` | 轮询异步操作 |

---

## 🌐 自托管 REST API

一个进程、一个端口，支持 `X-API-Key` / `Bearer` / `Token` 三种鉴权写法，
控制台与 API 共用同一个监听。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/v1/status/` | 存活探针 + 实时配置报告 |
| `GET` | `/v1/providers/` | 所有供应商及其当前可用性 |
| `POST` | `/v3/memories/add/` | 抽取并写入（异步，返回事件 id） |
| `POST` | `/v3/memories/search/` | 语义检索 |
| `POST` | `/v3/memories/get-all/` | 带过滤的列表 |
| `GET` / `PUT` / `DELETE` | `/v3/memories/{id}/` | 取 / 改 / 删一条 |
| `DELETE` | `/v3/memories/` | 清空作用域 |
| `GET` | `/v3/memories/{id}/history/` | 变更历史 |
| `GET` | `/v1/event/{id}/` · `/v1/events/` | 轮询 / 列出操作 |
| `GET` / `DELETE` | `/v2/entities/` | 列出 / 删除作用域 |
| `POST` | `/v3/graph/{add,search,get-all,delete-all}/` | 图记忆 |
| `POST` | `/v1/capsule/` · `/v1/expand/` | 无损压缩 |
| `GET` | `/v1/integrity/` | 账本校验 |
| `GET` / `POST` / `DELETE` | `/v1/keys/` | API Key 管理 |

---

## 🧠 Python API

```python
from mnemosyne import Memory, AsyncMemory, MemoryClient

# --- 抽取 / 作用域 / 过滤 -----------------------------------------------------
m = Memory()
m.add([{"role": "user", "content": "我 2023 年搬到了柏林。"}],
      user_id="alice", metadata={"source": "onboarding"},
      observation_date="2023-06-01")
m.add("发票号是 INV-2024-001。", user_id="alice", immutable=True)

hits = m.search("用户住在哪",
                filters={"user_id": "alice",
                         "AND": [{"source": {"eq": "onboarding"}}]},
                top_k=5, threshold=0.1, rerank=False, explain=True)

# --- 多模态 -------------------------------------------------------------------
m.add([{"role": "user", "content": [
    {"type": "text", "text": "我的新桌面。"},
    {"type": "image_url", "image_url": {"url": "https://example.com/desk.jpg"}},
]}], user_id="alice")

# --- 图记忆 -------------------------------------------------------------------
m.graph_add("乔布斯在库比蒂诺创立了苹果。", user_id="alice")
m.graph_search("苹果", filters={"user_id": "alice"})

# --- 异步与事件 ---------------------------------------------------------------
async def ingest():
    am = AsyncMemory()
    await am.add_many([{"messages": t, "options": {"user_id": "alice"}}
                       for t in transcripts])
```

`Memory` / `AsyncMemory` / `MemoryClient` 同时接受其它记忆库通行的调用形态，
所以已有代码只需改一行 import 即可切换。见
[docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)。

引擎自身的能力挂在同一个对象上：

```python
from mnemosyne import MemoryBrain

brain = MemoryBrain("./memories", enable_embeddings=True)
brain.ensure_init()
brain.retain("他那台笔记本是 ASUS VivoBook Pro 14", fast=True)

results = brain.recall("那台机器的配置是什么", k=5)
results, cost = brain.recall("那台机器的配置", k=5, budget_tokens=100)

cap = brain.capsule("<memory_id>", budget_tokens=60)   # 指针 + 事实 + 原子
brain.expand(cap["ref"])                                # 逐字取回
brain.verify_integrity()                                # SHA-256 账本校验
```

---

## 📂 目录结构

```
mnemosyne/
├── api/                 # 记忆 API：Memory / AsyncMemory / MemoryClient
│   ├── memory.py        #   引擎支撑的客户端
│   ├── config.py        #   MemoryConfig + 维度一致性校验
│   ├── filters.py       #   过滤语言 → 谓词
│   ├── extract.py       #   单遍 ADD-only 抽取
│   ├── multimodal.py    #   图片 / 音频附件解析
│   ├── events.py        #   持久化操作日志
│   └── client.py        #   内嵌 + HTTP 双传输
├── providers/           # 可选组件适配器（共 72 个）
│   ├── llms.py          #   20 个 LLM 供应商
│   ├── embedders.py     #   13 个嵌入供应商
│   ├── vector_stores.py #   28 个向量库
│   ├── graph_stores.py  #   6 个图数据库
│   ├── rerankers.py     #   5 个重排器
│   ├── vision.py        #   三种图片 wire format
│   └── transport.py     #   标准库 HTTP + 重试 + 凭据脱敏
├── brain.py             # MemoryBrain —— 引擎门面
├── capsule.py           # AIC 无损压缩
├── retrieval.py         # 多信号融合与相关性标定
├── graph.py             # 时序三元组存储
├── notary.py            # 写入前信任流水线
├── cli.py               # 原生 CLI
├── api_cli.py           # 客户端 API CLI
└── webui/
    ├── web_server.py    # 控制台 + REST 宿主
    ├── api_routes.py    # /v1 /v2 /v3 路由
    ├── mcp_server.py    # 20 个原生 MCP 工具
    └── mcp_api.py       # 11 个客户端兼容 MCP 工具

storage/                 # SQLite 后端、哈希链账本、插件 SDK
security/                # 矛盾检测、安全报告
scripts/                 # 验收脚本
docs/                    # 验收指南、召回策略、兼容说明
```

---

## ✅ 测试

```bash
python verify.py                              # 自检
python scripts/verify_api.py                  # 客户端 API 验收，全离线
python scripts/verify_memory_lifecycle.py --brain-dir ./mem --src-root .
python scripts/verify_precision_recall.py     # 离线精准回归
python scripts/verify_recall_quality.py       # 端到端召回质量
```

---

## 📚 文档

- [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) —— 通行的 Agent 记忆调用形态
- [docs/ACCEPTANCE_GUIDE.md](docs/ACCEPTANCE_GUIDE.md) —— 验收判据及对应脚本
- [docs/RECALL_STRATEGY.md](docs/RECALL_STRATEGY.md) —— 召回如何组装与预算
- [docs/KNOWN_DEFECTS.md](docs/KNOWN_DEFECTS.md) —— 已确证缺陷，含实测与修法
- [docs/DEPLOY_DEEPSEEK_HARNESS.md](docs/DEPLOY_DEEPSEEK_HARNESS.md) —— MCP 部署实操
- [CHANGELOG.md](CHANGELOG.md) —— 版本历史

---

## 📄 许可

MIT License —— 见 [LICENSE](LICENSE)。

由 Mnemosyne OS 贡献者共同构建。
