#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""嵌入式脚本示例：在单个 Python 脚本中直接使用 Mnemosyne。

本示例演示「嵌入式（库）」用法——无需 pip install，把仓库根目录加入 sys.path
即可 `from mnemosyne import MemoryBrain`。适合把记忆能力嵌入到独立脚本/工具中。

覆盖：初始化、写入、检索、会话历史、上下文快照、账本校验、with 语句。

运行：python examples/embedded_script.py
"""
import os
import sys
import tempfile

# 把仓库根目录加入 sys.path，从而能 import 到 mnemosyne 包（嵌入式场景常见做法）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mnemosyne import MemoryBrain


def main() -> None:
    # 使用临时目录作为记忆库
    tmp = tempfile.mkdtemp(prefix="mnemosyne_embedded_")

    # with 语句：进入时 ensure_init，退出时 close 释放资源
    with MemoryBrain(tmp, enable_embeddings=False, enable_stats=False) as brain:
        # 1) 写入记忆
        brain.retain("用户偏好：回答要简洁、结论先行。", mtype="preference")
        brain.retain("项目「零依赖 AI 记忆系统」的负责人是张三。", mtype="semantic")

        # 2) 检索
        print("检索「偏好」：")
        for score, rec, reasons in brain.recall("偏好", k=3):
            print(f"  [{score:.3f}] {rec['content']}")

        # 3) 会话历史
        brain.add_conversation_turn("s1", "user", "介绍一下项目负责人")
        print("\n会话检索「负责人」：")
        for turn in brain.search_conversations("负责人", session_id="s1"):
            print(f"  [{turn['role']}] {turn['content']}")

        # 4) 上下文快照
        snapshot = brain.build_context_prompt(query="项目", max_chars=800)
        print("\n上下文快照：")
        print(snapshot)

        # 5) 账本完整性校验
        print("\n账本校验：", brain.verify_integrity())

    # with 退出后资源已释放，清理临时目录
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print("完成。")


if __name__ == "__main__":
    main()
