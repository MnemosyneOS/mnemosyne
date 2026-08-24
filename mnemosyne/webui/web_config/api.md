# Mnemosyne Web 管理端 — 外部智能体对接 API 文档

> 本文档说明外部智能体（DeepSeek Harness / Hermes Agent / OpenClaw / Codex 等）如何通过 HTTP 与 Web 管理端对接，实现记忆写入与查询，并按智能体隔离。

## 0. 智能体隔离机制

- 每个“智能体”映射到 Mnemosyne 的 `project` 字段（格式 `agent:<slug>`）。
- 写入时传入 `agent`（或 `project`）字段，Web 端会解析为 `project` 传给 `MemoryBrain.retain(content, project=...)`。
- 查询时传入 `agent`（或 `project`）参数，Web 端在 `recall` / 记录列表中按 `project` 过滤。
- 不传 `agent`（或 `agent=all`）时返回“全部智能体”数据。

## 1. 鉴权

- 默认开启登录鉴权（`MNEMOSYNE_WEB_AUTH=1`）。
- 外部智能体需先登录获取会话 Cookie：
  ```
  POST /api/auth/login   {"username":"admin","password":"mnemosyne"}
  ```
  之后在所有请求中携带返回的 `mnemosyne_sid` Cookie。
- 若部署时关闭鉴权（`--no-auth` 或 `MNEMOSYNE_WEB_AUTH=0`），则无需 Cookie。

## 2. 智能体管理

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/agents` | 智能体列表（含每智能体记忆条数 count / recent7 / last_activity） |
| POST | `/api/agents` | 添加智能体 `{"name":"DeepSeek Harness"}`（可选 `project`） |
| PUT | `/api/agents/:id` | 重命名 / 修改 project |
| DELETE | `/api/agents/:id` | 删除智能体配置（记忆数据保留） |

`web_config/agents.json` 格式：
```json
{"agents":[{"id":"<uuid>","name":"DeepSeek Harness","project":"agent:deepseek_harness","created_at":"...","updated_at":"..."}]}
```

## 3. 记忆写入

```
POST /api/memories
Content-Type: application/json

{
  "content": "用户偏好结论先行的回答风格",
  "mtype": "preference",          // 可选
  "agent": "DeepSeek Harness"      // 或 "project": "agent:deepseek_harness"
}
```
响应：`201 {"id":"<memory_id>","message":"Memory added","project":"agent:deepseek_harness"}`

## 4. 记忆查询

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/memories?agent=DeepSeek Harness` | 该智能体记忆列表（`agent=all` 或省略 = 全部） |
| GET | `/api/memories?action=search&q=...&agent=X` | 该智能体内检索 |
| GET | `/api/stats?agent=X` | 该智能体统计 |
| GET | `/api/graph?agent=X` | 该智能体实体图谱 |
| GET | `/api/graph/timeline?agent=X` | 该智能体径向时间图 |
| GET | `/api/tree?agent=X` | 该智能体知识树 |
| GET | `/api/notary?agent=X` | 该智能体公证所 |
| GET | `/api/heatmap?agent=X` | 该智能体热力图数据 |
| GET | `/api/insights?agent=X` | 该智能体长期画像 |
| GET | `/api/audit?agent=X` | 该智能体相关审计（按记忆归属关联） |

## 5. 会话

会话存储底层未按 `project` 隔离（`SessionStore` 无 project 字段），因此 `/api/sessions` 为全局视图，不做智能体过滤（待底层支持）。

## 6. 外部数据源（任务3，可选）

Web 端可配置外部智能体的 HTTP API 或共享目录，主动抓取记忆：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/sources` | 数据源列表 |
| POST | `/api/sources` | 添加数据源 `{"name":"...","type":"api|dir","url":"...","path":"...","agent":"...","enabled":true,"interval":60}` |
| PUT | `/api/sources/:id` | 修改 |
| DELETE | `/api/sources/:id` | 删除 |
| POST | `/api/sources/sync` | 手动同步 `{"id":"<source_id>"}` |

- `api` 类型：GET 请求 `url`，解析返回的 JSON 数组 / JSONL / 纯文本，写入本地。
- `dir` 类型：扫描共享目录下 `.json` / `.jsonl` 文件，写入本地。
- 后台每 60 秒自动同步 `enabled` 的数据源（失败静默）。
- 若外部智能体不暴露 API 或目录不可达，同步会返回错误，界面提示“暂不可用”。

## 7. 端口与启动

```bash
python web_server.py --port 9090 --host 0.0.0.0 --dir <记忆库目录> --namespace default
```
启动时控制台会打印外部对接端点列表。
