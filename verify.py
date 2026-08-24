#!/usr/bin/env python3
"""Mnemosyne 自检脚本 —— 一行命令验证核心功能 + 性能指标。
用法：python verify.py
输出：核心功能检查 + 写入/检索延迟 + Token 计数口径
"""
import time, tempfile, shutil, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def main():
    from mnemosyne import MemoryBrain, VERSION
    print(f"=== Mnemosyne 自检 v{VERSION} ===\n")

    tmp = tempfile.mkdtemp()
    brain = MemoryBrain(tmp, enable_stats=False)
    brain.ensure_init()

    # 1. 核心功能：写入 → 检索
    brain.retain("用户喜欢技术深度分析", fast=True)
    brain.retain("用户偏好简洁的沟通风格", fast=True)
    hits = brain.recall("用户喜欢什么", k=3)
    assert hits, "检索返回空"
    print(f"[1] 写入/检索正常：召回 {len(hits)} 条记忆")

    # 2. 写入性能
    t0 = time.time()
    for i in range(200):
        brain.retain(f"测试记忆条目 {i}", fast=True)
    wt = (time.time() - t0) / 200 * 1000
    print(f"[2] 写入性能：{wt:.2f} ms/条（fast 模式）")

    # 3. 检索性能
    t0 = time.time()
    for _ in range(50):
        brain.recall("测试记忆", k=5)
    rt = (time.time() - t0) / 50 * 1000
    print(f"[3] 检索性能：{rt:.2f} ms/次")

    # 4. Token 计数口径（v7.0.0：默认纯标准库启发式，tiktoken/transformers 为可选）
    from mnemosyne.utils import StatsTracker
    tok = StatsTracker._load_tokenizer(backend="simple", model_id=None)
    tok_desc = ("默认 simple（4 字符≈1 Token，纯标准库零依赖，无网络调用）；"
                "可显式启用 tiktoken / transformers" if tok is None
                else "tiktoken cl100k_base（显式启用）")
    print(f"[4] Token 计数：{tok_desc}")

    # 5. 高级功能存在性
    for name in ("graph_query", "reflect", "consolidate", "temporal_query", "doctor", "memory_repair"):
        assert hasattr(brain, name), f"缺少 {name}"
    print("[5] 高级功能齐全：graph_query / reflect / consolidate / temporal_query / doctor / memory_repair")

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n=== 自检通过，Mnemosyne v{VERSION} 运行正常 ===")

if __name__ == "__main__":
    main()
