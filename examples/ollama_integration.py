#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ollama 集成示例：Mnemosyne + 本地 Ollama 大模型。

本示例演示：用 Mnemosyne 保存用户记忆 → 召回相关记忆 → 构建上下文快照 →
调用本地 Ollama 生成回复 → 将回复摘要保存回记忆库。

依赖说明：
  - Mnemosyne 核心零依赖（仅标准库）。
  - 本示例用标准库 urllib 调用 Ollama 的本地 HTTP 接口，不引入第三方包。
  - 需本地已运行 Ollama（默认 http://localhost:11434），未运行时脚本会提示并退出。

运行：python examples/ollama_integration.py
"""
import json
import os
import sys
import tempfile
import urllib.request

# 确保能 import 到仓库根目录的 mnemosyne 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mnemosyne import MemoryBrain

# Windows 控制台 GBK 兼容：无法编码的字符（如 ⚠）替换为 ? 而非崩溃
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

# Ollama 本地接口地址与模型名（模型名可按本机已拉取的模型修改）
OLLAMA_BASE = os.environ.get("OLLAMA_BASE", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1")


def ollama_available() -> bool:
    """探测本地 Ollama 是否可用（GET /api/tags）。"""
    try:
        with urllib.request.urlopen(f"{OLLAMA_BASE}/api/tags", timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def ollama_generate(prompt: str) -> str:
    """调用 Ollama /api/generate 生成回复，返回文本。"""
    payload = json.dumps({"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_BASE}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("response", "").strip()


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="mnemosyne_ollama_")
    brain = MemoryBrain(tmp, enable_embeddings=False, enable_stats=False)
    brain.ensure_init()

    # 1) 预置几条记忆（模拟历史对话中沉淀的事实/偏好）
    brain.retain("用户偏好：回答要结论先行、简洁，不要啰嗦。", mtype="preference")
    brain.retain("用户正在做的项目是「零依赖 AI 记忆系统」，负责人是张三。", mtype="semantic")

    # 2) 用户提问
    question = "我应该怎么向团队介绍记忆系统的优势？"

    # 3) 召回与问题相关的记忆
    hits = brain.recall(question, k=3)
    print("召回的记忆：")
    for score, rec, reasons in hits:
        print(f"  [{score:.3f}] {rec['content']}  ({'+'.join(reasons)})")

    # 4) 构建上下文快照（冻结的、可注入 LLM 的记忆上下文）
    context = brain.build_context_prompt(query=question, max_chars=1500)

    # 5) 组装提示词：上下文 + 问题
    prompt = f"{context}\n\n请根据上述记忆上下文回答用户问题：{question}"

    # 6) 调用 Ollama（若本地未运行则只打印提示词，优雅退出）
    if not ollama_available():
        print("\n⚠ 未检测到本地 Ollama，请先运行 `ollama serve` 后重试。")
        print("将打印本应发送给 Ollama 的提示词：\n")
        print(prompt)
    else:
        answer = ollama_generate(prompt)
        print("\nOllama 回复：")
        print(answer)
        # 7) 把 AI 回复摘要保存回记忆库（默认摘要策略，避免全文污染）
        brain.retain(f"[AI 回复摘要] {answer[:200]}", mtype="conversation", fast=True)

    brain.close()
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
