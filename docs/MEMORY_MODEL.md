# 记忆模型（Memory Model）

> 版本：Mnemosyne OS 8.1.0
> 本文说明「一条记忆从头到尾是什么形态」，是理解 `docs/CONFLICT_POLICY.md`
> 与 `docs/RECALL_STRATEGY.md` 的前置。

---

## 1. 一条记忆的解剖

每条记忆是一个 JSON 记录，字段分四组。

### 1.1 身份组

| 字段 | 含义 |
|---|---|
| `id` | 内容 + 命名空间 + 盐的稳定哈希，同内容同库必然同 id（这就是去重的依据） |
| `namespace` | 多租户隔离边界，**物理隔离**：不同 namespace 落在不同目录 |
| `project` | 项目边界，作用域软隔离，冲突判定与召回过滤都看它 |

### 1.2 内容组

| 字段 | 含义 |
|---|---|
| `content` | 正文，永不改写（只追加新版本） |
| `type` | 记忆类型：`semantic` / `episodic` / `procedural` / `working` / `resource` / `lesson` / `todo` / `identity` / `preference` / `fact` |
| `layer` | L0 原始 / L1 摘要 / L2 索引 / L3 归档 / L4 冷存 |
| `fact_type` | `fact`（客观事实）/ `opinion`（观点）/ `hypothesis`（假设）/ `intent`（意图） |
| `tags` | 自由标签，召回按 tag 过滤（AND/OR 见 `recall()` 文档字符串） |

### 1.3 信任组

| 字段 | 含义 |
|---|---|
| `source_type` | `user` / `system` / `file` / `agent_generated` / `inference` / `external` |
| `confidence` | 0–1。**外部来源一律低起**，因为不可验证 |
| `verification` | `verified` / `unverified` / `superseded` / `failed` |
| `sensitivity` | `normal` / `private` / `secret`（8.1.0 新增，见第 4 节） |
| `evidence_ids` | 支撑本条记忆的其他记忆 id，用于溯源与连带降权 |

### 1.4 时间组

| 字段 | 含义 |
|---|---|
| `created_at` | 写入本库的时间（摄取时间） |
| `event_time` | 事实发生的时间（事件时间）——**双时间轴** |
| `knowledge_time` | 系统得知此事的时间 |
| `expires_at` | TTL，到期走遗忘流程 |
| `supersedes` / `superseded_by` | 版本链指针，指向被本条取代/取代本条的记忆 |

> **双时间轴是记忆系统的地基**。只记「什么时候存进去」的系统无法回答
> 「用户 2024 年在哪工作」这种问题——库里只剩最后一次写入的值。

---

## 2. 三种记忆形态（8.1.0 新增的形成层）

8.1.0 之前只有 `type=semantic` 一类的原子事实，回答不了「上次我们聊到哪了」
「这个任务以前怎么做的」「这段代码为什么这么写」。为此新增三种结构化记忆，
**它们仍然落在同一个 store 里**，靠 `meta.kind` 区分，不需要第二套存储。

### 2.1 会话摘要（`meta.kind = "session_summary"`）

由 `session_summarize` 工具生成，覆盖七要素：

| 要素 | 为什么必须有 |
|---|---|
| `goal` | 这轮想达成什么 |
| `constraints` | 有哪些不能碰的红线（最容易被漏、也最容易在下次被违反） |
| `decisions` | 定了什么，**附理由** |
| `pending` | 未决事项——**独立成条**存为 `mtype=todo`，不埋在摘要里 |
| `artifacts` | 产出了哪些文件/配置 |
| `failures` | 试过什么不行（负记忆） |
| `preferences` | 用户表达的新偏好 |

**未决事项独立成条**是关键设计：藏在摘要段落里的待办，下次没人会去翻摘要；
单独成条才能被主动召回、才能在下次对话时问一句「上次这个还没定」。

### 2.2 任务事实与经验（`meta.kind = "task"` / `"playbook"`）

- **事实层**（`task_record`）：只追加，记录「做了什么、结果如何」，不做抽象。
- **经验层**（`playbook_save`）：提炼「什么方法有效、适用于什么条件」，
  **强制 `preconditions` 三件套**——`must` / `must_not` / `typical_env`。

`playbook_recall` 返回的每条都带 `applicable` 与 `blocked_by`，**只注入
`applicable=True` 的条目**。没有前置条件的经验会主动把 Agent 带错方向。

### 2.3 代码变更事件（`meta.kind = "change"`）

核心抽象不是 diff，而是**变更事件**。字段：

| 字段 | 说明 |
|---|---|
| `intent` | 这次想解决什么（git 里没有） |
| `intent_type` | `feature` / `fix` / `refactor` / `revert` |
| `constraints` | 当时的约束（不能改 schema、迁移未就绪……） |
| `rejected_approaches` | 试过但放弃的方案——**git 永远不会有，价值最高** |
| `outcome` | `merged` / `reverted` / `abandoned` |
| `lesson` | 一句话教训 |
| `parent` | 父变更 id，构成演进链 |

配套检索：`code_archaeology`（按文件/符号回溯 + 坑位预警）、
`evolution_chain`（沿 `parent` 展开演进链）、`pitfall_check`（动手前查坑）。

---

## 3. 写入路径

```
retain(content, mtype, ...)
  ├─ _build_record          抽取实体、算稳定 id、补默认信封字段
  ├─ _extract_event_time    从文本反推事件时间（"下周三" → ISO 日期）
  ├─ _detect_conflicts_at_write
  │     8.0.0：共享实体 + 都是 fact + 前 30 字不同 + 余弦 > 0.3  →  大量误报
  │     8.1.0：委托 mnemosyne/conflict.py 的 classify_pair()
  │           只有「共享实体 + 共享谓词 + 值/极性不同」才算冲突
  │           结果写入 meta.write_conflicts，供人复核，**不自动改写**
  ├─ policy.admit           敏感度分级 + 来源闸门，secret 直接拒收
  ├─ 双通道落库：SQLite（结构化）+ FTS5（BM25）
  └─ 向量后端（可选插件，siliconflow bge-m3 / qdrant……）
```

**写入默认不消解**。所有「UPDATE / DELETE」类动作都需要显式调用
`conflict_resolve`，且**先 dry_run 给人看**。理由见
`docs/CONFLICT_POLICY.md` 第 2 节。

---

## 4. 敏感度分级（8.1.0）

`mnemosyne/policy.py` 定义三级，与来源可信度共同决定「能不能进、能不能出」。

| 级别 | 判定示例 | 写入 | 召回 |
|---|---|---|---|
| `normal` | 「用户偏好 pnpm」 | 允许 | 允许 |
| `private` | 「我家在某某小区 3 栋」 | 允许 | 允许 |
| `secret` | 「API key 是 sk-…」 | **默认拒收**（`allow_secret=True` 才进，且只进加密库） | **永不召回** |

来源可信度表：`user` 0.95 / `system` 0.90 / `file` 0.85 /
`agent_generated` 0.55 / `inference` 0.50 / `external` 0.40。

进入长期层的白名单只剩 `user` / `system` / `file`。
**`external` 来源永远拿不到高可信度**——这是防注入的硬边界。

---

## 5. 遗忘

| 机制 | 触发 | 效果 |
|---|---|---|
| TTL | `expires_at` 到期 | 物理删除 |
| 时间衰减 | 距 `event_time` 越远 | 召回排序降权（不删） |
| 软遗忘 | `forget()` | `confidence → 0` + `status = deleted` |
| 物理清除 | `forget(evict=True)` | 真删 |
| 冷归档 | L3/L4 | 降层，不占热路径 |
| 级联失效 | `superseded_by` 被删时 | 派生记忆一并降权 |

**默认软删不硬删**：记忆的审计价值高于它占的空间。
