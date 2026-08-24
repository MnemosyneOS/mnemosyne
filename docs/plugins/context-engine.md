# 插件：context-engine（上下文压缩引擎）

> 代码位置：`context_engine/`（`core.py`、`__init__.py`、`l1.py`、`l2.py`、`lexical.py`）。

## 简介

上下文压缩引擎分两层设计：

- `core.MnemosyneCompressor`：**引擎无关**核心（任何 Agent 框架可用），OpenAI 消息
  格式进出，两层压缩（L1 词法无损 + L2 LLM 语义摘要）。
- `__init__.py` 的 `MnemosyneContextEngine`：**Hermes 适配层**，把核心包装成
  Hermes 的 `ContextEngine` 接口并注入 `call_llm`。

## 引擎无关核心

```python
from context_engine.core import MnemosyneCompressor
comp = MnemosyneCompressor(llm_call=my_llm_fn)   # my_llm_fn(messages) -> str
new_messages = comp.compress(messages)            # OpenAI 格式进出
```

核心参数：`threshold_percent`（触发阈值）、`protect_first_n`/`protect_last_n`
（头尾保护条数）、`summary_max_tokens`、`context_length`。

压缩流程：

1. L1 词法预压缩（`l1_precompress`，无损裁剪/去重/去噪）。
2. 定位 head（保护）/ middle（待摘要）/ tail（保护）。
3. L2 LLM 语义摘要（middle → 结构化摘要，含 `## Topics` 主题索引）。
4. 组装 head + 摘要 + tail；L2 失败或无 llm_call → 回退纯 L1。

## Hermes 适配层

```yaml
# config.yaml
context:
  engine: mnemosyne
```

`MnemosyneContextEngine` 提供 `compress`、`should_compress`、`update_model`、
`update_from_response`、`prune_tool_results_only`、`get_status` 等方法，与
`register(ctx)` 插件注册钩子。脱离 Hermes（无 `agent.*` 模块）时可正常 import，
L2 摘要降级为纯 L1。

## 摘要质量自检

`_assess_quality()` 计算 `keyword_coverage`（关键词覆盖率）与
`technical_detail_coverage`（技术细节覆盖率，含版本号/路径/文件名/URL/代码块）。

## 主题检索

`query_topic(topic, summary)` 从摘要的「主题索引」按词法相似度匹配，返回最相关
主题的一句话摘要。

## 零依赖

`lexical.py`（token 估算、相似度）与 `l1.py` 纯标准库；L2 摘要的 LLM 由调用方注入。
