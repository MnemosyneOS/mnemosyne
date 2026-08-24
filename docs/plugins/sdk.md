# 插件：sdk（插件 SDK）

> 代码位置：`storage/plugin_sdk.py`。

## 简介

插件 SDK 定义抽象基类与加载器。每个插件是普通 Python 文件，暴露 `register(brain)`
（返回插件实例）或 `get_plugin_class()`（返回类，以 brain 实例化）。插件从
`mnemosyne_plugins/` 目录加载，也可指定任意路径。零依赖（仅标准库 importlib）。

## 插件契约

一个插件模块必须暴露以下之一：

1. `register(brain)` → 插件实例；或
2. `get_plugin_class()` → 类（随后以 `cls(brain)` 实例化）。

## 抽象基类

| 基类 | 需实现的方法 | 用途 |
|---|---|---|
| `VectorBackendPlugin` | `add` / `search` / `encode` | 替代向量存储与检索 |
| `CryptoPlugin` | `encrypt` / `decrypt` / `get_key` | 字段级加密/解密 |
| `RerankerPlugin` | `rerank` | 结果重排 |

所有接口可选——大脑只调用存在的方法。插件失败不影响核心（加载器捕获异常，
`PluginInfo.error` 记录错误）。

## 快速使用（自定义插件）

```python
from storage.plugin_sdk import RerankerPlugin

class MyReranker(RerankerPlugin):
    def rerank(self, query, results, **kwargs):
        return sorted(results, key=lambda x: x[0], reverse=True)

def register(brain):
    return MyReranker(brain)
```

## 加载器

| 函数 | 说明 |
|---|---|
| `load_plugin(file_path, brain)` | 加载单个插件，返回 `(PluginInfo, instance)` |
| `load_plugins(plugins_dir, brain, pattern)` | 自动发现并加载目录下插件 |

`PluginInfo.to_dict()` 返回 `{name, path, class, enabled, error}`。

## 大脑集成

`MemoryBrain(plugins=["numpy_vector", "crypto", "reranker"])` 按名加载
`mnemosyne_plugins.<name>.plugin` 并绑定快捷属性（`vector_backend_plugin`/
`crypto_plugin`/`reranker_plugin`）。

## 测试

`tests/test_plugins.py`。
