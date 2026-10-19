# 冲突策略（Conflict Policy）

> 版本：Mnemosyne OS 8.1.0
> 实现：`mnemosyne/conflict.py`、`mnemosyne/brain.py` 的两处挂接点
> 工具：`conflict_resolve`（MCP）、`ConflictResolver.audit()`（库内审计）

---

## 1. 8.0.0 的问题：把「实体共现」当成「事实冲突」

### 1.1 旧判据

写入侧（`brain.py:_detect_conflicts_at_write`）：

```
共享实体 + 双方 fact_type=fact + 内容前 30 字不同 + 余弦相似度 > 0.3
```

反思侧（`brain.py:reflect`）：

```
同一实体出现在 ≥2 条 fact_type=fact 的记忆里 + 前 60 字不同
```

**两者都只在比「是否提到同一个东西」，从没比「是否对同一个东西说了相反的话」。**

### 1.2 实测后果

一个 66 条活跃记忆的库，`reflect` 报出 **14 组冲突**。逐条核对后几乎全是假阳性：

| 记忆 | 内容 |
|---|---|
| A | an agent 的规则注入槽是 SOUL.md |
| B | an agent 的记忆通道是云端 |
| C | an agent 从 old version 升级到 new version |

三条互不矛盾，描述同一个对象的不同侧面，却被判成一组冲突。

**后果不是「多几条告警」而是「告警失效」**：用户发现 14 组里有 13 组是噪声后，
会学会忽略整个面板——真正需要处理的那 1 组也被一起忽略了。

---

## 2. 8.1.0 的判据：断言对齐（assertion alignment）

不再看实体共现，而是把每条记忆压成一个**可比较的断言**，只比较同一属性。

```python
assertion(record) = {
    "entities":   {...},   # 实体（8.1.0 存谓词切分后的主干）
    "predicates": {...},   # 谈论的是哪个属性
    "polarity":   ±1,      # 肯定还是否定
    "numbers":    {...},   # 数值与日期
    "attrs":      {...},   # 从文本位置回溯出的属性名
    "values":     {...},   # 属性取值
    "scope":      (...),   # project / namespace
}
```

### 2.1 判定链（顺序即优先级，从最不可能是冲突开始）

| # | 条件 | 结论 |
|---|---|---|
| 1 | 同一条记录 | NONE / ADD |
| 2 | 作用域不同（project 或 namespace 不同） | `scope_diff` / ADD —— **伪冲突** |
| 3 | 词集相似度 ≥ 0.9 且实体集合一致 | DUPLICATE / NOOP |
| 4 | **无共享实体** | NONE / ADD —— 互不相关 |
| 5 | **共享实体但无共享谓词** | NONE / ADD —— 同一对象的不同侧面 |
| 6 | 一方是另一方子串 | REFINEMENT / NOOP |
| 7 | **属性相同、取值不同** | `temporal_update` / UPDATE |
| 8 | 极性相反 + 共享谓词 | `logical_contradiction` / KEEP_BOTH |
| 9 | 数值不同（且事件时间不同） | `temporal_update` 或 `precision_diff` |
| 10 | 偏好类且宾语不同 | `preference_change` / UPDATE |
| 11 | 同事实不同来源 | `source_conflict` / KEEP_BOTH |
| 12 | 其余 | NONE / ADD |

第 4、5、7 条是消除误报与漏报的关键：

- **第 5 条**直接消掉了 8.0.0 那 14 组噪声。
- **第 7 条**补上了 8.0.0 漏掉的真冲突——「换公司」「换负责人」这类
  共享实体、共享谓词、值不同的情况，旧判据反而因为「前 30 字不同 + 余弦高」
  走进了重复检测分支。

### 2.2 属性与取值怎么来

`_attr_value()` 取第一个谓词的位置，**从它前面回溯一小段中文作为属性**，
后面作为取值。完全不依赖 `entities` 字段——因为中文实体抽取本身是噪声源：

```
"用户在北京的公司是甲公司"  →  属性「北京的公司」  取值「甲公司」
"用户在北京的公司是乙公司"  →  属性「北京的公司」  取值「乙公司」   ⇒ 属性同、值不同 ⇒ UPDATE
"an agent 的规则注入槽是 SOUL.md" → 属性「规则注入槽」
"an agent 的记忆通道是云端"       → 属性「记忆通道」               ⇒ 属性不同 ⇒ 不冲突
```

### 2.3 实体归一化

中文抽取兜底正则是 `[\u4e00-\u9fff]{2,6}`，会把 7 字以上的串硬切成 6+2：

```
"该方案是安全的"   → ["该方案是安全"]
"该方案是不安全的" → ["该方案是不安", "全的"]
```

三条碎片字符串两两不等，精确匹配时分不到同一个桶。因此 `assertion()` 存的是
**谓词切分后的实体主干**（`_ent_key`），三条都归一为 `该方案`。
`audit()` 分桶时再用二元字组重合度（≥0.6）兜底。

---

## 3. 消解动作

| 动作 | 语义 | 落到哪里 |
|---|---|---|
| `ADD` | 新记忆，无冲突 | 正常写入 |
| `NOOP` | 重复或细化，保留信息更全者 | 不写 |
| `UPDATE` | 旧记忆被新记忆取代 | 旧条 `verification=superseded` + `superseded_by=新id`，**不删除** |
| `KEEP_BOTH` | 无法自动选边 | 两条都留，新条降权，标 `needs_confirmation` |

**为什么 UPDATE 不删旧条**：双时间轴要求能看到「用户 2024 年在 A 公司、
2026 年在 B 公司」。删除旧条等于抹掉事件时间轴，记忆就退化成一个只有当前值的
KV 存储。这也是 Zep 的做法（边失效而非删边）。

**为什么 KEEP_BOTH 不自动选边**：「支持 Redis」vs「不支持 Redis」可能是用户
改主意了，也可能是其中一条记忆本身记错了。没有额外证据时自动站边，
一旦选错就是把错误信息固化成事实。

---

## 4. `conflict_resolve` 工具用法

```python
# 第一步永远是 dry_run
plan = conflict_resolve(content="用户在北京的公司是丙公司",
                        dry_run=True)
# → {"plan": "UPDATE", "verdicts": [...], "dry_run": true}

# 看完计划再执行
conflict_resolve(content="...", dry_run=False)
```

`dry_run=False` 时按计划落库：只执行 `UPDATE`（置 superseded + 写 superseded_by），
`NOOP` 不写，`KEEP_BOTH` 两条都留并标记待确认。

---

## 5. 全库审计

`ConflictResolver.audit()` 替代了 8.0.0 `reflect` 里的实体共现检测。

- 按实体分桶 → 桶内两两 `classify_pair` → 只保留非 NONE。
- 桶内超过 12 条时按置信度取前 12，避免大库上 O(n²) 失控。
- 返回 `scanned` / `findings` / `count` / `note`。

`brain.reflect()` 现在把审计结果透传出来，并额外给出 `conflicts_scanned`、
`conflicts_total`、`conflicts_note` 三个字段，让「扫了多少条、报了多少条、
按什么口径报」一目了然——扫描范围本身就是判读结果所需的信息。

---

## 6. 回归要求

`scripts/verify_v810.py` 第 2 组锁定以下行为，**改动判据前先确认它们仍全绿**：

| 用例 | 期望 |
|---|---|
| an agent 同实体不同方面 | 0 条 |
| 换公司（同属性不同值） | 1 条 `temporal_update`，动作 UPDATE |
| 「安全」vs「不安全」 | 1 条 `logical_contradiction`，动作 KEEP_BOTH |
| 不同 project | `scope_diff` |
| 项目A 负责人 vs 项目B 负责人 | 0 条（不同主体） |

当前基线：**29 项全通过**（8.1.0 起扩充为 36 项）。
