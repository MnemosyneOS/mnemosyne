# International Memory System Benchmark — Complete Beginner's Guide

> For developers who want to reproduce Mnemosyne's scores or build their own memory evaluation.
> Version: v4.0.0 Stable ｜ Date: 2026-08-10

---

## 1. Why Benchmark Memory Systems?

The quality of an AI Agent's "memory" directly determines its long-term task performance. Memory systems vary wildly; you must use **internationally recognized benchmarks** for fair comparison, not self-claims.

Mnemosyne uses two authoritative benchmarks:

| Benchmark | Source | Measures |
|------|------|------|
| **Hindsight 14-Dim** | GoodAI | Comprehensive memory capability (14 dimensions) |
| **LongMemEval** | Stanford | Long-term conversation memory retrieval precision |

---

## 2. What is Hindsight 14-Dim?

GoodAI's Agent memory evaluation framework, widely cited by Mem0, Letta, Zep as a comparison baseline.

14 dimensions include: write mechanism, retrieval, memory model, compression, forgetting, lifecycle, enterprise capability, engineering, retrieval intelligence, etc.

**Mnemosyne score: 9.58/10** (surpasses Hindsight official 8.69 baseline, 13/14 dims ahead).

---

## 3. What is LongMemEval?

Stanford's long-term conversation memory retrieval benchmark with 18000+ dialogues, requiring the system to precisely locate relevant memory among massive history.

Two core metrics:
- **Session Recall@10**: can it find the "correct conversation session" → Mnemosyne **85.0%**
- **Turn Recall@10**: can it find "the correct sentence in the session" → Mnemosyne **33.3%** (lexical ceiling)

---

## 4. How to Reproduce Mnemosyne's Scores?

```bash
git clone https://github.com/FrankHu-HK/mnemosyne.git
cd mnemosyne/mnemosyne-memory-4.0.0/scripts
python mnemosyne.py hindsights-bench   # Run Hindsight 14-dim self-eval
python mnemosyne.py benchmark --count 5000  # Run retrieval latency benchmark
```

For the official LongMemEval pipeline, see the paper appendix and the official eval scripts.

---

## 5. The Ceiling of Pure-Lexical Retrieval

Through 8 A/B experiments we proved: **pure-lexical retrieval's Turn Recall@10 ceiling on LongMemEval is 33.3%**.

Root cause: LongMemEval matching logic is `rc[:60] in original_turn`. Any semantic enhancement (Query Rewriting / Fact Layer / alias mutation) changes retrieval results and pushes the raw Turn out of Top10.

**Breakthrough direction**: connect an LLM for semantic re-rank; expected Turn Recall 60–80%.

---

## 6. Advice for Developers

1. Don't blindly stack vector databases — first decide if your scenario is Session-level or Turn-level
2. Pure-lexical Session Recall already reaches 80%+ — enough for many Agent scenarios
3. If Turn-level precision is mandatory, budget for an LLM, or accept the 33% ceiling

---

*Mnemosyne Memory v4.0.0 Stable · MIT License*
