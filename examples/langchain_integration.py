#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LangChain 集成示例：用 Mnemosyne 作为 LangChain/CrewAI 兼容的记忆层。

本示例演示 Mnemosyne 的框架无关适配器 `MnemosyneMemory`，它提供
LangChain 记忆接口约定的两个方法：
  - load_memory_variables(inputs, query) -> {"history": ...}
  - save_context(inputs, outputs) -> 保存一轮对话

说明：
  - Mnemosyne 核心零依赖，不 import 任何框架。
  - AI 回复默认按「摘要」策略保留（ai_retain="summary"），避免全文写入污染记忆库。
  - 若本机安装了 langchain，可用 langchain 的 ConversationChain 直接挂载本适配器
    （本示例用 try/except 优雅处理 langchain 未安装的情况）。

运行：python examples/langchain_integration.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mnemosyne import MnemosyneMemory


def demo_native_adapter() -> None:
    """不依赖 langchain，直接演示适配器的 load/save 接口。"""
    tmp = tempfile.mkdtemp(prefix="mnemosyne_langchain_")
    memory = MnemosyneMemory(tmp, k=5, ai_retain="summary", ai_max_chars=200)

    # 保存一轮对话：用户输入 + AI 回复（AI 回复仅存摘要）
    memory.save_context(
        {"input": "苹果公司是哪一年成立的？"},
        {"output": "苹果公司成立于 1976 年，由乔布斯等人创立。"},
    )

    # 加载记忆上下文（LangChain 风格返回 {"history": ...}）
    ctx = memory.load_memory_variables({"input": "苹果公司"})
    print("召回的记忆上下文：")
    print(ctx["history"])

    memory.brain.close()  # MnemosyneMemory 无 close()，关闭其内部 brain
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def demo_with_langchain() -> None:
    """若安装了 langchain，演示挂载到 ConversationChain；否则提示。"""
    try:
        from langchain.memory import BaseMemory  # 仅用于探测 langchain 是否可用
        from langchain.chains import ConversationChain
        from langchain.llms.fake import FakeListLLM
    except ImportError:
        print("（未安装 langchain，跳过 ConversationChain 演示。"
              "可用 `pip install langchain` 后重试。）")
        return

    tmp = tempfile.mkdtemp(prefix="mnemosyne_langchain_")
    memory = MnemosyneMemory(tmp, k=5)

    # 说明：MnemosyneMemory 已提供 load_memory_variables / save_context，
    # 可直接作为 LangChain 记忆对象使用（本示例不展开完整 Chain 挂载细节）。
    memory.save_context({"input": "你好"}, {"output": "你好，有什么可以帮你？"})
    print("已通过 MnemosyneMemory 保存一轮对话，history 如下：")
    print(memory.load_memory_variables({"input": "你好"})["history"])

    memory.brain.close()  # MnemosyneMemory 无 close()，关闭其内部 brain
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    demo_native_adapter()
    print("-" * 60)
    demo_with_langchain()
