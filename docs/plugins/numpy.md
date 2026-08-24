# 插件：numpy（NumPy 向量后端）

> 代码位置：`mnemosyne_plugins/numpy_vector/plugin.py`（`NumpyVectorBackend`）。

## 简介

基于 numpy 的稠密向量存储与余弦相似度检索插件。numpy 为**可选**第三方依赖，
缺省优雅降级（`_available=False`）。安装了 `sentence-transformers` 且本地有
`BAAI/bge-small-zh-v1.5` 模型时，`encode()` 产出高质量中文语义向量；否则回退
哈希确定性编码（字符 bigram + trigram）。

## 启用

```python
from mnemosyne import MemoryBrain
brain = MemoryBrain("./mem", plugins=["numpy_vector"])
# 插件接线后，retain()/recall() 实际使用插件编码与语义候选召回
```

## 模型加载策略（`_ensure_model`，惰性）

1. `model_name` 为本地目录 → 直接离线加载。
2. `MNEMOSYNE_ALLOW_MODEL_DOWNLOAD=1` → 允许联网下载（受 `HF_ENDPOINT` 控制）。
3. 否则仅检查 HF hub 缓存（须存在权重文件 `model.safetensors`/`pytorch_model.bin`），
   命中才离线加载；未命中回退哈希编码（不发任何网络请求）。

模型名可由构造参数 `model_name` 或环境变量 `MNEMOSYNE_EMBEDDING_MODEL` 指定。

## 核心 API

| 方法 | 说明 |
|---|---|
| `encode(text)` | 模型编码（512 维）或哈希编码（128 维） |
| `similarity(vec_a, vec_b)` | 余弦相似度（按维度自适应） |
| `add(memory_id, vector)` | 写入向量索引（归一化） |
| `search(query_vector, top_k)` | 返回 `[(memory_id, score)]` |

## 依赖

- `numpy`（可选，缺失降级）。
- `sentence-transformers`（可选，缺失回退哈希编码）。

## 测试

`tests/test_plugins.py`、`tests/test_semantic_retrieval.py`。
