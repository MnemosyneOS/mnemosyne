# Mnemosyne Memory v4.0.0 — Development & Evaluation Record

> Written for other AI systems and developers, seeking new solutions and third-party reproduction.
> 📄 Paper DOI: `10.5281/zenodo.21870436` ｜ 💻 Code DOI: `10.5281/zenodo.21870790`
> 🐙 GitHub: `github.com/FrankHu-HK/mnemosyne` ｜ ⏱️ Timestamp: 2026-08-10

---

## 1. Design Philosophy

Mnemosyne is **the world's only AI Agent memory engine built entirely on the Python standard library with zero external dependencies**.

Non-negotiable constraints:
- Zero third-party libraries (no numpy / torch / transformers / jieba)
- Zero GPU, zero vector database, zero external API
- Single-file deployment (~100KB), copy-and-run
- 100% local, data never leaves your machine

Under these extreme constraints, we pursue **world-class** memory retrieval scores.

---

## 2. Benchmark Background

| Framework | Description | Status |
|------|------|------|
| **Hindsight 14-Dim** | GoodAI's authoritative Agent memory evaluation | Industry-standard baseline, cited by Mem0/Letta/Zep |
| **LongMemEval** | Stanford long-term conversation memory retrieval (18000+ records) | Gold standard for retrieval |
| **Custom Token Savings** | L1 pre-filter → only Top-10 to LLM | Engineering measurement |

---

## 3. Eight Success Methods (verified effective)

1. **Inverted Index + BM25**: O(n)→O(log n), pure CPU <10ms/query
2. **5-Way Fusion**: BM25 + vector hash + knowledge graph + time decay + confidence weighting
3. **Memory Alias Expansion**: expand index at write time, never alter raw content → no match breakage
4. **Session-level Local Re-rank**: two-stage re-rank within session → Session Recall 85% vs 21%
5. **Shared Database Mode**: single instance, multi-session → 60%+ performance gain
6. **Fast Write Mode**: 2.6× faster writes, ~12ms/record
7. **Auto Reflection + Consolidation**: discover cognitive patterns, compress redundancy, resolve conflicts
8. **Knowledge Graph Multi-hop**: entity-relation extraction + same-sentence edges + conflict detection

---

## 4. Eight Failed Methods (A/B validated, disproven)

| Approach | Turn@10 | Session | Conclusion |
|------|:--:|:--:|------|
| S1 Fact Layer | 10% | collapse | Semantic enhancement shifts results, pushes raw Turn out of Top10 |
| S2 Query Rewriting | 21.7% | collapse | same |
| S3 Cross Encoder re-rank | crash | collapse | Rule re-rank rewards long text; evidence Turn is short |
| S4 Pseudo-Embedding | no change | no change | Hash projection adds no semantic gain |
| S5 Timeline Index | marginal | marginal | Temporal info already covered by BM25 |
| S6 Alias Expansion (mutate) | 10% | collapse | Breaks raw Turn matching |
| S7 Graph Bridge | marginal | marginal | Graph gain absorbed by BM25 |
| S8 8-layer rule engine | 35% ceiling | no change | Pure-rule Turn Recall ceiling ≈ 35% |

**Key finding**: LongMemEval matching logic is `rc[:60] in original_turn`. Any semantic enhancement changes retrieval results and pushes the raw Turn out of Top10. **Pure-lexical Turn Recall@10 ceiling = 33.3%**; breaking it requires an LLM.

---

## 5. Current Scores (v4.0.0 Stable)

| Metric | Score | Note |
|------|:--:|------|
| Hindsight 14-Dim Architecture | **9.58/10** | Surpasses Hindsight 8.69 baseline, 13/14 dims ahead |
| LongMemEval Session Recall@10 | **85.0%** | Locating correct conversation among 18000+ records |
| LongMemEval Turn Recall@10 | **33.3%** | Pure-lexical ceiling (disproven) |
| LLM Token Savings | **80%+** | Only Top-10 sent to LLM after L1 filter |
| Retrieval Latency | **<10ms** | Pure CPU, zero GPU |
| Write Speed | **~12ms/record** | Fast Write mode |

---

## 6. Bottlenecks & Call for Help

1. Can Turn-level precision break 33.3% without breaking raw matching?
2. Is there a better pure-rule fusion than 5-way?
3. Third-party reproduction via official LongMemEval pipeline welcome.

---

*Mnemosyne Memory v4.0.0 Stable · MIT License · Data per paper and official benchmarks*
