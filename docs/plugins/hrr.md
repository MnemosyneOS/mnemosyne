# 插件：hrr（HRR 代数查询）

> 代码位置：`plugins/hrr_plugin.py`（`SimpleHRR`、`HRRPlugin`）。

## 简介

全息缩减表示（Holographic Reduced Representation，HRR）的简化实现，用于向量符号
架构操作：编码实体、与随机键绑定、向量叠加（bundle）、探测（probe）与推理（reason）。
核心零依赖（numpy 可选加速，`try/except` 导入）。

## 快速使用

```python
from plugins.hrr_plugin import SimpleHRR
hrr = SimpleHRR(vector_dim=1024)
hrr.store("苹果", "公司成立于1976年")
hrr.store("谷歌", "公司成立于1998年")
print(hrr.probe(hrr.encode("苹果"), k=5))   # [(label, similarity)]
print(hrr.reason("苹果", k=5))
```

## 核心操作

| 操作 | 说明 |
|---|---|
| `encode(label, content=None)` | 确定性向量编码（label 与 content 绑定） |
| `store(label, content=None)` | 编码并存入内存，更新联想记忆 |
| `probe(query_vector, k=5)` | 余弦相似度 top-k 匹配 |
| `reason(query_label, k=5)` | 用联想记忆绑定推理 |
| `clear()` | 清空记忆 |

`_stable_hash(seed_text, length)` 用 hashlib 生成确定性随机向量（有 numpy 用
RandomState，无 numpy 用纯 Python 回退）。

## 插件包装

`HRRPlugin` 提供 `encode`/`store`/`probe`/`reason` 与 `available()`（恒 True，
有/无 numpy 均可工作）。

## 测试

`tests/test_hrr_plugin.py`。
