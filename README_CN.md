<p align="center">
  <img src="assets/banner.png" alt="mnemosyne OS" width="100%">
</p>

# mnemosyne OS

<p align="center">
  <a href="https://pypi.org/project/mnemosyne-os/">PyPI</a> ·
  <a href="https://github.com/MnemosyneOS/mnemosyne">GitHub</a> ·
  <a href="README.md">English</a>
</p>

<p align="center">
  <a href="https://pypi.org/project/mnemosyne-os/"><img src="https://img.shields.io/badge/PyPI-ai--memo--os-blue?style=for-the-badge" alt="PyPI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="许可证: MIT"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.8%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.8+"></a>
  <a href="#-mcp-服务器"><img src="https://img.shields.io/badge/MCP-52%20Tools-00ADD8?style=for-the-badge" alt="模型上下文协议"></a>
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

**Mnemosyne OS 8.1.0** —— 零依赖、本地优先的 AI 记忆系统。自带图记忆、多模态摄入、
重排、时序推理、哈希链审计账本、无损压缩，以及 52 个 MCP 工具（41 原生 + 11 客户端兼容）。

8.1.0 补上了两层。**记忆形成层**：会话摘要、冲突消解、任务与经验、代码演进、敏感度治理——
让系统不只「找得出记忆」，还能「把经历变成记忆」。**记忆运行层**：画像卡、上下文注入、
渐进披露、巩固、度量、掩码、级联删除——为模型组装合适的上下文，并管理记忆的生命周期。

> 唯一一个**核心真的零三方依赖**的 AI 记忆引擎 —— 不需要向量数据库、不需要 LLM 运行时、
> 不需要云账号。`install_requires` 是空列表。笔记本、服务器、无服务器环境都能跑。

可以当 **Python 库**、**CLI**、**HTTP API**、**MCP 服务器**用。

---

## ☁️ Mnemosyne OS SaaS

同一个引擎，托管版，带 Web 面板：**[ai-memory.net](https://ai-memory.net)**。
托管面板的产品名就叫 **Mnemosyne OS SaaS** —— 注册、建密钥、浏览记忆、管理订阅都在那里。

| 档位 | 价格（USD） | 价格（CNY） | 内容 |
|---|---|---|---|
| **Free** | $0 | ¥0 | 52 个 MCP 工具全开 · 冷层记忆 30 天召回窗口 · 1 个命名空间 · 审计窗口 7 天 |
| **Pro** | $6.9/月 · $69/年 | ¥9.9/月 · ¥99/年 | 永久冷层记忆 · 跨命名空间召回 · 审计历史无限制 · 全量导出 |
| **Team** | $19.9/席位/月 · $199/席位/年 | ¥29.9/席位/月 · ¥299/席位/年 | Pro 全部能力 · 多用户命名空间 · 团队记忆策略 · 审计报告导出 |
| **合规证明包** | $9.9/月 | ¥19.9/月 | **只有月付。** Pro / Team 之上的可选加购（非独立层级）：verify_integrity 定期报告、删除证明导出、数据驻留配置 |

年付为「**付 10 个月用 12 个月**」（约 16.7% off，折合 ¥8.25/月）。

每个新账号先送 **30 天 Team 权益，无需绑卡** —— 到期后可以永久留在 Free，
或升级保住全部记忆。美元走 PayPal 订阅自动续费（随时可在 PayPal 内取消）；
人民币走微信收款码 + 管理员人工确认。

自部署永远是 MIT、不设限 —— SaaS 线只是给「不想自己跑服务器」的人准备的。
引擎完全一致，**开源版没有任何功能被锁**。

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

**52 个工具** —— 41 个原生工具（其中 21 个是 8.1.0 新增：9 个记忆形成层 + 12 个记忆运行层），
外加 11 个沿用通行 Agent 记忆工具命名的工具，
已有的 MCP 客户端不必改写工具定义就能直接指向 Mnemosyne。

**原生 · 检索与治理（23）：**

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
| `memory_export-v1` | `memory/export-v1` 的别名（供不接受 `/` 的客户端使用） |
| `memory_import-v1` | `memory/import-v1` 的别名（供不接受 `/` 的客户端使用） |
| `memory_claim` | `memory/claim` 的别名（供不接受 `/` 的客户端使用） |

**原生 · 记忆形成层（9，v8.1.0 新增）：**

| 工具 | 说明 |
| --- | --- |
| `session_summarize` | 会话摘要：七要素 + 锚点原文保留，未决事项独立成条 |
| `session_recall` | 召回摘要与未决事项，供下次对话主动追问 |
| `conflict_resolve` | 六类冲突判定 + 四操作；`scope=audit` 做全库审计（只报真冲突） |
| `task_record` | 记录任务事实（只追加），按 `goal_type` 支撑同类任务检索 |
| `playbook_save` | 沉淀经验，**强制** `preconditions` 三件套 |
| `playbook_recall` | 检索候选经验并做前置条件校验，返回 `applicable` / `blocked_by` |
| `code_change_record` | 记录变更事件：意图、约束、影响面、放弃的方案 |
| `code_archaeology` | 代码考古 / 坑位预警（传 `intent` 即转为坑位检查） |
| `memory_policy` | 敏感度判定、写入准入、存量高危记忆审计 |

**原生 · 记忆运行层（12，v8.1.0 新增）：**

| 工具 | 说明 |
| --- | --- |
| `profile_card` | 用户画像卡：三段分级（稳定身份 / 偏好与约束 / 当前情境）+ 未决事项，可直接拼进 system prompt。`max_tokens` 是硬上限——超预算按「情境→未决→偏好」逐段裁，绝不截断单条。 |
| `memory_inject` | 上下文注入流水线：查询构造 → 四路召回 → RRF 融合 → 重排 → 预算内注入，返回可直接注入的 text 加四段配额账目。给模型准备上下文时比 `recall` 更合适。 |
| `recall_catalog` | 渐进披露·目录层：只返回标题 + 一句话 + 展开成本，不返回正文。先看目录判断哪几条相关，再对 1~2 条调 `expand_memory`——注入成本从 O(全部) 降到 O(目录 + 少量)。 |
| `expand_memory` | 渐进披露·展开：取一条记忆的正文 + 统一信封 + 图邻居 + 证据链。与 `recall_catalog` 配对；邻居来自知识图谱，所以「当时这个决定影响了哪些模块」是可回答的。 |
| `memory_envelope` | 统一记忆信封：八类分类 + 四维 scope + 证据链 + 双时间轴 + TTL + SPO 结构。省略 `memory_id` 时返回全库八类分布统计。 |
| `memory_consolidate` | 巩固三路径：情景→语义、情景→程序性、失败→负记忆。幂等——同一批源不会重复生成。 |
| `memory_metrics` | 度量指标：Chat 5 项 + Working 4 项 + Coding 4 项。诚实原则——数据不足返回 `{value:null, status:"insufficient_data"}` 而不是填 0，0 和「没数据」是两件事。 |
| `memory_cascade_delete` | 级联删除：软删原始 → 失效派生结论 → 清检索缓存 → 依赖摘要标 stale。`hard=true` 才物理删除；先跑 `dry_run=true` 看 dependents。 |
| `memory_mask` | 掩码：把正文替换为占位符但保留记录骨架，保持上下文长度与前缀不变（不破坏 KV-Cache 复用）。用户说「别记这个」时优先用掩码。`action=unmask` 可撤销。 |
| `topic_summarize` | L3 主题摘要：把同一 project 下的多条 L2 会话摘要合并成「这个项目现在处于什么状态」。合并规则确定性（goal 取最新、decisions 累积、pending 只留最新）。`persist=true` 落库并记录 `pointer.children`。 |
| `memory_trace` | 指针链回溯：沿 L3→L2→L1 回溯，返回每层内容、覆盖的轮次区间与 `message_ids`。回答「这条结论是从哪句话来的」。 |
| `memory_hot_path` | 热路径缓冲：`action=enable` 开启异步写入（热路径只入队）；`action=status` 查看队列与统计；`action=flush` 等队列落盘——写完立刻读之前必须 flush 的读一致性屏障。 |

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
    ├── mcp_server.py    # 44 个原生 MCP 工具（含 21 个新增于 8.1.0）
    └── mcp_api.py       # 11 个客户端兼容 MCP 工具

storage/                 # SQLite 后端、哈希链账本、插件 SDK
security/                # 矛盾检测、安全报告
scripts/                 # 验收脚本
docs/                    # 验收principle、召回策略、兼容说明
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
- [docs/DEPLOY_DEEPSEEK_HARNESS.md](docs/DEPLOY_DEEPSEEK_HARNESS.md) —— MCP 部署实操
- [CHANGELOG.md](CHANGELOG.md) —— 版本历史

---

## 📄 许可

MIT License —— 见 [LICENSE](LICENSE)。

由 Mnemosyne OS 贡献者共同构建。
