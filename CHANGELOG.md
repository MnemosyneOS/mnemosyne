# 更新记录（Release Notes）

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
