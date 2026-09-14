# 更新记录（Release Notes）

## 7.0.1（2026-09-14）

针对记忆栈的一轮缺陷修复与可验证性补强。核心引擎的改动集中在**记忆生命周期语义**
与**语义检索索引**两条线上；记忆库格式、MCP 协议版本与 CLI 命令面均保持向后兼容，
已有记忆库可直接沿用。

### 新增
- **MCP `forget` 工具**（工具数 13 → 14）：按 `memory_id` 或自然语言 `query` 遗忘一条记忆。
  默认**软遗忘**——`confidence` 归零 + `status=deleted`，审计链与可信度轨迹保留、可回溯；
  `evict=true` 才做物理删除。支持 `dry_run=true` 先返回候选供确认。
- **记忆更正（`correct` / `supersedes`）**：`retain(supersedes=<旧记忆id>)` 与
  `brain.correct()` 可在写入新说法的同时把旧记忆标为 `verification=superseded`
  （是标记而非删除，历史可完整回溯）。`recall` 结果会回传 `verification` 与
  `superseded_by`，便于区分"哪条说法已被取代"。
- **`MNEMOSYNE_PLUGINS` 环境变量**：`MemoryBrain` 在 `plugins` 为 `None` 时读取该变量
  （逗号分隔）。CLI / MCP Server / WebUI 三个入口原先都不传 `plugins`，现在无需逐个改动
  入口即可启用插件。未设置时行为与上游 7.0.0 完全一致。
- **官方插件 `mnemosyne_plugins/qdrant_backend`**：Qdrant ANN 向量后端（BGE-M3 嵌入，
  OpenAI 兼容 `/v1/embeddings`）。相对 `numpy_vector` 的进程内内存字典，它提供**跨进程
  持久**的向量索引；内置熔断器、代理绕过、`warmup()` 预建集合与 `health()` 诊断。
  见 `docs/plugins/qdrant.md`。
- **文档与脚本**：
  - `docs/KNOWN_DEFECTS.md` —— 11 项已确证缺陷，逐项给出事实、实测证据、影响面与修法。
  - `docs/RECALL_STRATEGY.md` —— 召回通路拓扑、四路打分构成、语义候选召回与
    "每轮召回"的代价评估。
  - `docs/ACCEPTANCE_GUIDE.md` —— 验收principle：握手与工具面、子进程环境逐键对齐、
    冷热延迟预算、命名空间隔离、审计链、生命周期闭环、假失败速查。
  - `scripts/verify_memory_lifecycle.py` —— 参数化的 20 项断言生命周期验收脚本。

### 修复
- **`retain()` 显式 `confidence` 不再被覆盖**：Notary 评估会无条件改写
  `record["confidence"]`，导致调用方显式传入的值（包括 `0`，即撤回语义）被静默丢弃。
  现在显式值优先，Notary 的原始判断留档于 `meta["notary_confidence"]`，信息不丢失。
- **`retain_batch()` 逐项元数据透传**：原实现把每项的第三个元素写死为 `{}`，
  `confidence` / `tags` / `importance` 全部被丢弃，使批量路径上的元数据设定静默失效。
- **`fast` 写入路径补上向量索引**：快速路径此前把 `embedding` 固定置 `None` 且从不调用
  向量后端的 `add()`，导致经该路径写入的记忆**完全无法参与语义召回**（即使被语义候选
  召回，也会因 `n_vec=0` 排在末尾而等于白召回）。现在一次 `encode()` 同时喂给
  SQLite 与向量后端。
- **`retain_batch()` 的 `fast=False` 分支补上 `add()`**：该分支此前只把 `embedding` 写进
  record，从不调用向量后端 `add()`，因此经 `retain_batch` 写入的记忆从未真正进入向量
  索引（MCP 与 Python SDK 两条路径都受影响）。
- **遗忘/撤回的记忆输出前硬过滤**：`retrieval.py` 在打分排序之后剔除 `confidence` 归零的
  输出项。位置刻意选在候选池之外，避免破坏 `records` 与 `_doc_tf_cache` 的下标对齐。
  这条为"调用方显式写入 `confidence=0`"这类撤回语义兜底。
- **遗忘后检索缓存显式失效**：`_invalidate_retrieval_index()` 强制下一次 `recall` 重建
  索引，避免已遗忘内容从旧缓存继续被召回。
- **MCP `namespace` 报告修正**：`retain_batch` / `list_projects` / `audit` /
  `confidence_history` / `export` / `import` / `claim` 此前把命名空间报成字面量
  `"default"`，现改为报告**实际生效**的命名空间。
- **MCP `recall` 回传 `memory_id`**：原实现不回传 id，导致调用方拿到结果也无法对其执行
  遗忘或更正；同时补充回传 `tags`。`retain` 工具面补齐 `tags` / `confidence` /
  `importance` / `supersedes`，`mtype` 枚举由 3 类扩至 8 类。
- **9 种语言 README 与工具面对齐**：工具数由 13 更正为 14，补充 `forget` 说明与
  `qdrant_backend` 插件名。

### 升级须知
- **无破坏性变更**：记忆库格式、MCP 协议版本（`2024-11-05`）与 CLI 命令面均不变。
- `forget` 为纯增量工具，旧客户端忽略未知工具即可。
- `qdrant_backend` 为可选插件，需要本机可用的 Qdrant 与嵌入服务；未启用时行为与
  7.0.0 一致。
- 若此前依赖"批量写入时 `confidence` 被 Notary 覆盖"的行为，请注意该行为已按
  "显式值优先"修正。

## 7.0.0（2026-08-25）

Mnemosyne 7.0.0 正式版。核心为**零依赖、本地优先**的 AI 记忆系统。

### 用户可见的主要能力
- **默认 SQLite + FTS5 存储**：全文检索、WAL 模式，支持 JSONL 兼容与 `migrate` 迁移。
- **哈希链账本**：每条记忆写入链式 SHA-256 校验，`verify_chain()` 可定位篡改。
- **公证可信度**：写入前做来源指纹、交叉印证、注入检测、时间一致性检查。
- **插件体系**：官方插件含 `numpy_vector`（语义检索）、`crypto`（加密）、`reranker`（重排）、`hrr`、`async`、`context-engine`。
- **MCP 服务器**：13 个工具，支持令牌鉴权与多租户命名空间隔离。
- **记忆交换协议**：JSONL + manifest 导出/导入。
- **遗忘经济学**：低价值记忆按价值模型迁移至温/冷层（gzip 归档 + 布隆过滤），而非删除。
- **Token 预算器**：`recall(budget_tokens=...)` 按得分×可信度×边际信息量贪心选择。
- **字段级脱敏**：密码、邮箱、卡号、API key 写入前打码。
- **本地 Web 管理界面**：暗色面板，无外部 CDN（`web_server.py`）。
- **异步 API**：`AsyncMemoryBrain`。
- **会话历史 / 用户画像 / 知识树视图 / 外部源适配器**。

### 升级须知
- Python 要求 ≥ 3.8。
- 核心零第三方依赖；仅示例中的 Ollama / LangChain / numpy / HRR 插件在启用时按需本地依赖。
- 首次运行 Web 管理端会自动创建 `admin/mnemosyne` 默认账号，请登录后修改密码。
- 历史 JSONL 记忆库可用 `migrate` 命令升级到 SQLite 后端。
