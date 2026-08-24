# 插件：reranker（结果重排）

> 代码位置：`mnemosyne_plugins/reranker/plugin.py`（`HeuristicReranker`）。

## 简介

对检索结果重排的启发式插件，透明零依赖公式：

```python
score = confidence * freshness * access_freq
```

- `confidence`：记忆自身可信度（0–1）。
- `freshness`：基于年龄的指数衰减（`0.5 ^ (age_days / 30)`，半衰期 30 天）。
- `access_freq`：`log(access_count+1)/log(11)`（访问频率，封顶归一化）。

最终分数与原始分数混合：`0.7 * new_score + 0.3 * orig_score`。

## 启用

```python
from mnemosyne import MemoryBrain
brain = MemoryBrain("./mem", plugins=["reranker"])
```

## 核心 API

| 方法 | 说明 |
|---|---|
| `rerank(query, results)` | 重排 `(score, record, reasons)` 列表，返回新分数列表 |
| `_freshness(record)` | 新鲜度（指数衰减） |
| `_access_freq(record)` | 访问频率分数 |
| `available` | 恒为 True（纯标准库） |

重排后每条命中追加 `reranked:confidence*freshness*access_freq` 到 reasons。

## 设计说明

该插件在五路融合结果之上工作，无需外部 cross-encoder 模型。`rerank()` 接受
`(score, record, reasons)` 二元或三元组，兼容不同结果形态。

## 测试

`tests/test_plugins.py`。
