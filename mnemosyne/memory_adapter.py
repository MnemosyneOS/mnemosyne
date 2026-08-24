import os
from typing import Any, Dict, List, Optional, Union

from .brain import (MemoryBrain,)
INDEX_NAME = "index.jsonl"
GRAPH_NAME = "graph.jsonl"
META_NAME = "meta.json"
EMBEDDING_DIM = 128
PROJ_BUCKETS = 2048
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".mnemosyne")


class MnemosyneMemory:
    """框架无关的 Agent 记忆适配器——提供 LangChain/CrewAI 兼容的 load/save 接口。

任何 Agent 框架（LangChain/CrewAI/AutoGPT/MetaGPT 等）都能用：
- load_memory_variables(inputs) 召回记忆上下文
- save_context(inputs, outputs) 保存一轮对话

核心保持零依赖：不 import 任何框架，只提供通用接口。"""

    def __init__(self, base_dir: str = DEFAULT_DIR, k: int = 5,
                 ai_retain: str = "summary", ai_max_chars: int = 200,
                 **kwargs: Any) -> None:
        self.brain: MemoryBrain = MemoryBrain(base_dir, **kwargs)
        self.k: int = k
        # AI 回复保留策略：默认 "summary"（仅保留摘要），避免全文写入污染记忆库。
        # 可选 "none"（完全不保留 AI 回复）或 "full"（全文保留，显式开启）。
        self.ai_retain: str = ai_retain
        self.ai_max_chars: int = ai_max_chars
        self.brain.ensure_init()

    def load_memory_variables(self, inputs: Optional[Dict[str, Any]] = None,
                              query: Optional[str] = None) -> Dict[str, str]:
        """召回记忆上下文。返回 {"history": "..."}（LangChain 风格）。"""
        q = query or ""
        if not q and isinstance(inputs, dict):
            q = inputs.get("input") or inputs.get("query") or inputs.get("question") or ""
        if not q:
            return {"history": ""}
        results = self.brain.recall(q, k=self.k)
        lines: List[str] = []
        for r in results:
            rec = r[1] if isinstance(r, (tuple, list)) and len(r) > 1 else r
            content = rec.get("content", "") if isinstance(rec, dict) else str(rec)
            if content:
                lines.append("- " + content[:300])
        return {"history": "\n".join(lines)}

    def save_context(self, inputs: Optional[Dict[str, Any]] = None,
                     outputs: Optional[Dict[str, Any]] = None) -> str:
        """保存一轮对话。自动判断用户输入和 AI 回复。

        AI 回复不默认全文 retain：按 ai_retain 策略处理——
          - "summary"：保留前 ai_max_chars 字符的摘要（默认）
          - "none"：完全不保留 AI 回复
          - "full"：全文保留（需显式开启，谨慎使用）
        """
        user = ""
        ai = ""
        if isinstance(inputs, dict):
            user = inputs.get("input") or inputs.get("query") or inputs.get("question") or ""
        if isinstance(outputs, dict):
            ai = outputs.get("output") or outputs.get("response") or ""
        if user:
            self.brain.retain(user, fast=True)
        if ai:
            policy = (self.ai_retain or "summary").lower()
            if policy == "none":
                pass
            elif policy == "full":
                self.brain.retain(ai, fast=True)
            else:  # summary（默认）
                summary = ai.strip()
                if len(summary) > self.ai_max_chars:
                    summary = summary[: self.ai_max_chars].rstrip() + "…"
                if summary:
                    self.brain.retain(
                        f"[AI 回复摘要] {summary}", fast=True, mtype="conversation")
        return user or ai

    # 便捷委托
    def remember(self, content: str, **kwargs: Any) -> str:
        return self.brain.retain(content, **kwargs)

    def recall(self, query: str, k: Optional[int] = None,
               **kwargs: Any) -> List[Any]:
        return self.brain.recall(query, k=k or self.k, **kwargs)

    def forget(self, memory_id: str) -> bool:
        return self.brain.forget(memory_id)

    def __enter__(self) -> "MnemosyneMemory":
        """支持 with 语句。"""
        self.brain.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        """退出 with 语句时释放资源。"""
        self.brain.__exit__(exc_type, exc_value, traceback)
        return False
