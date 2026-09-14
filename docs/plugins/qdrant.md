# 插件：qdrant_backend（Qdrant 向量后端）

> 代码位置：`mnemosyne_plugins/qdrant_backend/plugin.py`（`QdrantVectorBackend`）。

## 简介

把向量索引放到**外部 Qdrant 服务**上的向量后端插件，是内置随机投影 `EmbeddingEngine`
与 `numpy_vector` 插件的替代品。

- 向量由一个**外部嵌入服务**产生（示例：BGE-M3，OpenAI 兼容的 `/v1/embeddings`）；
- 向量存入**本地 Qdrant 集合**，获得跨进程共享的持久化 ANN 索引 ——
  而不是 `numpy_vector` 那种「每进程一份内存字典」。

它存在的理由很具体：**突破 FTS5 关键词候选池**。FTS 关键词命中不了的、
语义等价的记忆，由本插件的 `search()` 捞出来（Path2a 语义候选召回）。

## 设计契约（对接 `brain.py` / `retrieval.py` 的调用点）

| 成员 | 语义 |
|---|---|
| `available` | **能力检查**（环境是否可用），**不是**实时探活。探测失败会静默关掉整个进程的嵌入，所以这里刻意不做网络探测；可达性在每次调用时惰性重查 |
| `encode(text)` | 返回 `list[float]`，失败返回 `None`（所有调用点都容忍 `None`） |
| `add(memory_id, vector)` | `retain()` 期间写入 |
| `search(query_vector, top_k)` | 语义候选召回，返回 `[(memory_id, score), ...]`，按分数降序 |
| `similarity(vec_a, vec_b)` | **必须纯本地 Python** —— 它按候选逐条调用，若发 HTTP 会把一次 `recall` 变成成百上千次往返 |

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MNEMOSYNE_QDRANT_URL` | `http://127.0.0.1:6333` | Qdrant 服务地址 |
| `MNEMOSYNE_QDRANT_COLLECTION` | `mnemosyne_{namespace\|dir}` | **建议不要设置**，见下 |
| `MNEMOSYNE_EMBED_URL` | `http://127.0.0.1:5080/v1/embeddings` | 嵌入服务地址 |
| `MNEMOSYNE_EMBED_MODEL` | `bge-m3` | 模型名 |
| `MNEMOSYNE_EMBED_DIM` | `1024` | BGE-M3 dense 维度 |
| `MNEMOSYNE_EMBED_MAX_CHARS` | `8000` | 单次请求截断长度 |
| `MNEMOSYNE_EMBED_TIMEOUT` | `10` | 嵌入调用超时（秒） |
| `MNEMOSYNE_QDRANT_TIMEOUT` | `5` | 常规 REST 调用超时（秒） |
| `MNEMOSYNE_QDRANT_CREATE_TIMEOUT` | `90` | 建集合超时（秒） |
| `MNEMOSYNE_QDRANT_BREAKER` | `30` | 服务不可达时跳过该服务的时长（秒） |

> ⚠ **`MNEMOSYNE_QDRANT_COLLECTION` 环境变量优先于按命名空间派生集合**。
> 一旦设置，所有命名空间会共用同一个集合，`_derive_collection()` 永不执行，
> 结果是 Path2a 的候选槽被其他命名空间的 id 稀释。
> 它**不是**内容泄漏（`retrieval.py` 会丢弃不属于本命名空间的 id），但会削弱语义召回增益。
> 详见 [docs/KNOWN_DEFECTS.md](../KNOWN_DEFECTS.md) 的 F2。

## 启用

```python
from mnemosyne import MemoryBrain
brain = MemoryBrain("./mem", plugins=["qdrant_backend"])
```

或用环境变量（逗号分隔多个插件）：

```bash
export MNEMOSYNE_PLUGINS=qdrant_backend
```

加载器解析的是 `mnemosyne_plugins.<name>.plugin`。

> **注意**：插件必须被显式启用。没有加载时向量一路全 0，
> 所有条目的最终分会塌到 `0.4052` 这个常数底 ——
> 看起来像"召回完全没有区分度"，其实是配置问题。
> 若验收时看到所有分数相同或只差 0.00x，先查环境。

## 集合的创建成本（**必读**）

在 Windows 上创建一个集合约需 **6 秒**（Qdrant 1.19.1，冷存储）。
它**每个集合只发生一次**，惰性发生在首次写入时 —— 这也是
`MNEMOSYNE_QDRANT_CREATE_TIMEOUT` 单独设成 90 秒的原因。

**部署时应预建集合**，避免运行期第一次写入付这笔成本：

```python
plugin = brain.get_plugin("qdrant_backend")
plugin.warmup()          # 返回 {'collection_ready':…, 'collection_ms':…, 'embed_ok':…, 'embed_dim':…}
```

`collection_ms` 会直接告诉你建集合花了多久。

## 补齐缺失向量

向量**不是**只存在 Qdrant 里 —— Mnemosyne 同时也把 embedding 存在自己的 SQLite
（`memories.embedding`）中，且打分时 `vec_scores` **只在 SQLite 那份非空时**才计算。

所以：

- 从 SQLite 向 Qdrant 补齐向量是**安全且总是可行**的（只增不删，建议先 dry-run）；
- **反过来不行**：只把向量写进 Qdrant、不回填 SQLite，这些记录在排序上仍拿不到向量分
  （恒为垫底常数）。补齐脚本应提供写回 SQLite 的选项。

详见 [docs/KNOWN_DEFECTS.md](../KNOWN_DEFECTS.md) 的 F1 与 F5。

## 核心 API

| 方法 | 说明 |
|---|---|
| `available` | 能力检查（不探活） |
| `encode(text)` | 取向量；失败返回 `None` |
| `add(memory_id, vector)` | 写入/更新一个点 |
| `search(query_vector, top_k)` | 语义候选召回 |
| `similarity(vec_a, vec_b)` | 本地余弦（不发 HTTP） |
| `warmup(probe_text="warmup")` | 预建集合 + 自检 |
| `health()` | 健康检查 |
| `save(brain)` / `load(brain)` | 持久化钩子 |
| `register(brain)` / `get_plugin_class()` | 插件注册入口 |
