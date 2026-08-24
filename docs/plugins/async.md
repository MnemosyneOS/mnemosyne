# 插件：async（异步 API 封装）

> 代码位置：`plugins/async_wrapper.py`（`AsyncMemoryBrain`、`AsyncMemoryBrainContext`）。

## 简介

用 `asyncio.to_thread` 把同步 `MemoryBrain` 方法封装为 async/await，非阻塞操作。
零依赖（仅标准库 asyncio）。

## 快速使用

```python
import asyncio
from plugins.async_wrapper import AsyncMemoryBrain

async def main():
    brain = AsyncMemoryBrain("./mem")
    await brain.async_retain("你好，世界")
    results = await brain.async_recall("你好", k=5)
    brain.close()

asyncio.run(main())
```

## 异步方法

| 方法 | 对应同步方法 |
|---|---|
| `async_retain(content, ...)` | `retain` |
| `async_retain_detailed(...)` | `retain_detailed` |
| `async_recall(query, ...)` | `recall` |
| `async_consolidate(...)` | `consolidate` |
| `async_forget(memory_id)` | `forget` |
| `async_forget_by_filter(...)` | `forget_by_filter` |
| `async_status(...)` | `status` |
| `async_add_conversation_turn(...)` | `add_conversation_turn` |
| `async_search_conversations(...)` | `search_conversations` |
| `async_set_profile / get_profile / delete_profile` | 档案操作 |
| `async_build_context_prompt(...)` | `build_context_prompt` |
| `async_export_memories / import_memories` | 交换协议 |

## 异步上下文管理器

```python
from plugins.async_wrapper import AsyncMemoryBrainContext
async with AsyncMemoryBrainContext("./mem") as brain:
    await brain.async_retain("内容")
```

## 测试

`tests/test_async_api.py`。
