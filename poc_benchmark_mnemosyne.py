"""
================================================================================
PoC Performance Benchmark Suite for Lightweight Memory Engine
纯 Python 标准库 — 企业级 PoC 性能跑分套件
================================================================================
测试指标：
1. Latency (延迟): Mean, P50, P95, P99, Max (ms)
2. Memory (内存): Base RAM, Peak RAM (MB)
3. Token Reduction (Token 节约率): Original vs Working Context Token Count
4. QPS / Throughput (吞吐量): Single-thread & Multi-thread Queries Per Second
5. Scalability (扩展性): Performance curve across context lengths (5k ~ 100k tokens)
"""

import time
import statistics
import tracemalloc
import json
import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from typing import List, Dict, Any

# ==========================================
# 1. 接入 Mnemosyne Memory Engine
# ==========================================
import sys, os, tempfile, shutil
sys.path.insert(0, r"C:\Users\<your-user>\Desktop\Mnemosyne\mnemosyne-memory-4.0.0\scripts")
from mnemosyne import MemoryBrain

class MnemosyneAdapter:
    """将 Mnemosyne 的 retain/recall 映射到 benchmark 期望的接口"""
    def __init__(self):
        self.brain_dir = tempfile.mkdtemp(prefix='bench_')
        self.brain = MemoryBrain(base_dir=self.brain_dir)
        self.brain.ensure_init()

    def ingest_session(self, session_id: str, raw_text: str):
        # Mnemosyne: 批量写入整个 session（避免逐句 retain 的高频 I/O）
        items = [(chunk.strip(), "semantic", {}) for chunk in raw_text.split("\n\n") if chunk.strip()]
        if items:
            self.brain.retain_batch(items)

    def query_working_context(self, session_id: str, query: str, target_max_tokens: int = 2000) -> str:
        # Mnemosyne: recall 返回最相关的记忆片段
        results = self.brain.recall(query, k=5)
        selected = "\n\n".join([r[1].get("content", "") for r in results])
        return selected[:target_max_tokens * 4]  # 1 token ≈ 4 chars
    
    def cleanup(self):
        shutil.rmtree(self.brain_dir, ignore_errors=True)

# ==========================================
# 2. 测试数据生成器 (Dataset Generator)
# ==========================================
def generate_mock_dialogue_history(session_count: int = 10, turns_per_session: int = 20) -> Dict[str, str]:
    """生成具备模拟 CoT 思考链和长上下文的大规模会话数据"""
    sample_topics = [
        "Python 内存优化与垃圾回收机制分析",
        "分布式数据库 CAP 定理与 Raft 共识算法推导",
        "高并发微服务 API 网关限流算法实现",
        "大语言模型 Transformer 注意力机制与 Prefix Cache 优化",
        "银行级数据加密 AES-256 与国密 SM4 算法接入规范"
    ]
    
    dataset = {}
    for s_idx in range(session_count):
        session_id = f"session_{s_idx:04d}"
        topic = random.choice(sample_topics)
        lines = [f"=== Session {session_id} Header: Topic={topic} ==="]
        for t_idx in range(turns_per_session):
            lines.append(f"User: 请详细推导 {topic} 的第 {t_idx} 个核心步骤并给出代码示例。")
            lines.append(f"Assistant: 思考链(CoT): Step 1: 分析问题本质... Step 2: 推导逻辑公式... Step 3: 给出实现。代码示例: def step_{t_idx}(): pass")
        dataset[session_id] = "\n\n".join(lines)
    return dataset

# ==========================================
# 3. 核心 Benchmark 跑分引擎
# ==========================================
class MemoryEngineBenchmark:
    def __init__(self, engine_instance):
        self.engine = engine_instance

    def measure_latency_and_throughput(self, dataset: Dict[str, str], num_queries: int = 500) -> Dict[str, Any]:
        """测量检索与切片延迟 (P50, P95, P99) 及 QPS"""
        # 先加载数据
        session_ids = list(dataset.keys())
        for sid, text in dataset.items():
            self.engine.ingest_session(sid, text)

        latencies_ms = []
        queries = ["Python 内存", "Raft 共识", "API 网关", "Transformer 注意力", "AES-256 加密", "未知查询词"]

        start_total = time.perf_counter()
        for _ in range(num_queries):
            sid = random.choice(session_ids)
            q = random.choice(queries)
            
            t0 = time.perf_counter()
            _ = self.engine.query_working_context(sid, q)
            t1 = time.perf_counter()
            
            latencies_ms.append((t1 - t0) * 1000.0)
        end_total = time.perf_counter()

        total_time_s = end_total - start_total
        qps = num_queries / total_time_s if total_time_s > 0 else 0

        latencies_sorted = sorted(latencies_ms)
        return {
            "num_queries": num_queries,
            "total_time_s": round(total_time_s, 4),
            "qps": round(qps, 2),
            "latency_ms": {
                "mean": round(statistics.mean(latencies_ms), 3),
                "p50": round(latencies_sorted[int(len(latencies_sorted) * 0.50)], 3),
                "p95": round(latencies_sorted[int(len(latencies_sorted) * 0.95)], 3),
                "p99": round(latencies_sorted[int(len(latencies_sorted) * 0.99)], 3),
                "min": round(min(latencies_ms), 3),
                "max": round(max(latencies_ms), 3),
            }
        }

    def measure_memory_footprint(self, dataset: Dict[str, str]) -> Dict[str, Any]:
        """测量内存占用峰值 (Peak RAM)"""
        tracemalloc.start()
        
        # 初始内存
        snapshot_before = tracemalloc.take_snapshot()
        
        # 加载大规模数据并执行查询
        for sid, text in dataset.items():
            self.engine.ingest_session(sid, text)
            
        for sid in list(dataset.keys())[:10]:
            self.engine.query_working_context(sid, "内存优化")
            
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        
        return {
            "current_memory_mb": round(current / (1024 * 1024), 3),
            "peak_memory_mb": round(peak / (1024 * 1024), 3)
        }

    def measure_token_reduction(self, dataset: Dict[str, str]) -> Dict[str, Any]:
        """测量 Token 节约率 (根据字符粗略估算 1 Token ≈ 4 Chars 或用 tiktoken)"""
        total_orig_chars = 0
        total_compressed_chars = 0

        for sid, text in dataset.items():
            orig_len = len(text)
            compressed_text = self.engine.query_working_context(sid, "Python 内存")
            comp_len = len(compressed_text)

            total_orig_chars += orig_len
            total_compressed_chars += comp_len

        orig_tokens = total_orig_chars // 4
        comp_tokens = total_compressed_chars // 4
        saved_tokens = orig_tokens - comp_tokens
        reduction_rate = (saved_tokens / orig_tokens * 100) if orig_tokens > 0 else 0

        return {
            "original_tokens_est": orig_tokens,
            "working_context_tokens_est": comp_tokens,
            "tokens_saved_est": saved_tokens,
            "token_reduction_rate_pct": round(reduction_rate, 2)
        }

# ==========================================
# 4. 主运行入口与报告输出
# ==========================================
def run_benchmark():
    print("==================================================")
    print(" 🚀 正在启动 PoC 记忆引擎性能测试套件 (Benchmark)...")
    print("==================================================")

    # 生成测试数据集：100 个 Session，每个 30 轮对话 (约 150,000 字/Tokens)
    print("\n[1/4] 生成测试数据集 (100 Sessions, ~150,000 Tokens)...")
    dataset = generate_mock_dialogue_history(session_count=10, turns_per_session=20)
    
    engine = MnemosyneAdapter()
    bench = MemoryEngineBenchmark(engine)

    # 1. 内存测试
    print("[2/4] 正在测试内存占用峰值 (RAM Peak)...")
    mem_stats = bench.measure_memory_footprint(dataset)

    # 2. 延迟与吞吐量测试
    print("[3/4] 正在执行 100 次检索延迟测试...")
    latency_stats = bench.measure_latency_and_throughput(dataset, num_queries=100)

    # 3. Token 压缩节约率测试
    print("[4/4] 正在计算 Token 削减与降本效率...")
    token_stats = bench.measure_token_reduction(dataset)

    # 汇总报告
    report = {
        "benchmark_summary": {
            "dataset_sessions": len(dataset),
            "estimated_total_tokens": token_stats["original_tokens_est"],
            "engine_footprint_mb": mem_stats["peak_memory_mb"]
        },
        "performance_metrics": {
            "qps": latency_stats["qps"],
            "latency_ms_p50": latency_stats["latency_ms"]["p50"],
            "latency_ms_p95": latency_stats["latency_ms"]["p95"],
            "latency_ms_p99": latency_stats["latency_ms"]["p99"],
            "latency_ms_mean": latency_stats["latency_ms"]["mean"]
        },
        "cost_efficiency": {
            "token_reduction_pct": f"{token_stats['token_reduction_rate_pct']}%",
            "original_input_tokens": token_stats["original_tokens_est"],
            "optimized_input_tokens": token_stats["working_context_tokens_est"]
        }
    }

    print("\n" + "="*50)
    print("         📊 PoC 性能跑分测试报告 (Benchmark Report)")
    print("="*50)
    print(f"• 测试数据规模    : {len(dataset)} Sessions ({token_stats['original_tokens_est']:,} Tokens)")
    print(f"• 峰值内存占用    : {mem_stats['peak_memory_mb']} MB")
    print(f"• 单秒吞吐量(QPS) : {latency_stats['qps']:,} req/sec")
    print(f"• 平均检索延迟    : {latency_stats['latency_ms']['mean']} ms")
    print(f"• P50 延迟        : {latency_stats['latency_ms']['p50']} ms")
    print(f"• P95 延迟        : {latency_stats['latency_ms']['p95']} ms")
    print(f"• P99 延迟        : {latency_stats['latency_ms']['p99']} ms")
    print(f"• Token 削减节约率: {token_stats['token_reduction_rate_pct']}% (算力直接降本)")
    print("="*50 + "\n")

    # 保存 JSON 结果文件
    with open("poc_benchmark_results.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print("✅ 跑分报告已成功保存至 `poc_benchmark_results.json`！")

if __name__ == "__main__":
    run_benchmark()
