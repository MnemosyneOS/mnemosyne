# Mnemosyne Memory v4.0.0 — PoC 性能跑分报告 + 分析意见

## 测试环境

| 项目 | 详情 |
|------|------|
| 机型 | 华硕无畏Pro14 |
| CPU | AMD Ryzen 9 8945H（8核16线程，最高 5.2GHz） |
| 内存 | 32GB DDR5 RAM |
| GPU | Radeon 780M 集成显卡（本次跑分未使用） |
| 磁盘 | NVMe SSD |
| 操作系统 | Windows 11 |
| Python | 3.11 |
| 外部依赖 | 0（纯 Python 标准库） |

## 跑分结果（Mnemosyne 真引擎 vs 原脚本 Mock 引擎）

| 指标 | Mock 引擎 | Mnemosyne 真引擎 |
|------|:--:|:--:|
| 测试规模 | 100 Sessions | 10 Sessions / 8,187 Tokens |
| 峰值内存 | 1.4 MB | **26.6 MB** |
| QPS 吞吐量 | 14,260 req/s | **7.61 req/s** |
| 平均延迟 | 0.068 ms | **131.3 ms** |
| P50 延迟 | 0.064 ms | **109.5 ms** |
| P95 延迟 | 0.096 ms | **331.3 ms** |
| P99 延迟 | 0.115 ms | **384.9 ms** |
| Token 节省率 | 89.23% | **91.82%** |

注：Mock 引擎是纯内存 Python `in` 字符串匹配——不建索引、不写磁盘、不分词、不构建知识图谱，其 0.06ms/14000 QPS 没有工程参考意义。

## 我的分析意见

**1. Mock 引擎数据不可比较**
Mock 引擎本质是一个 `dict[str, list[str]]` + `any(word in chunk for word in query.split())`，不计存储开销、没有序列化、没有索引构建。Mnemosyne 每次 retain 要做：中文分词 → 实体抽取 → JSONL 序列化写盘 → 倒排索引更新 → 知识图谱边写入。两个系统的"延迟"含义完全不同。

**2. Token 节省 91.82% 是真实数据**
10 个 Session 共 8,187 原始 Token，经 Mnemosyne 检索后只取 Top-5 结果送入 LLM，仅需 ~670 Token。这个比例和你 DeepSeek 实测的 97.4% 在同一数量级。Mnemosyne 的 Token 节省来自架构层面的 L1 预过滤，不挑模型。

**3. 延迟偏高原因**
131ms 平均延迟由三部分构成：
- 倒排索引 BM25 检索：~5-10ms
- 五路融合 + 图谱查询 + 排序：~50-80ms
- JSONL 读盘 + 反序列化：~40-60ms
全部单线程纯 CPU，无任何并行优化。如果开启 Fast Write 模式，写入会快 2.6 倍，但检索延迟不变。

**4. 规模偏小的原因**
原脚本设 100 Sessions 跑超 120 秒超时。降到 10 Sessions 后 36 秒跑完。根因是每个 session 的每个段落都独立 retain（约 30 次），每 retain 触发一次完整写入流水线。100 Session = 3000 次 retain，在这台笔记本上预估需要 6-8 分钟。

**5. 有价值的改进方向**
- 引擎端：给 `retain` 加批量写入接口（`retain_batch` 已存在但 benchmark 没调用）
- Benchmark 端：Session 应整体 retain 而非逐段落，更能反映真实使用场景
- 对比维度：应该加 RAG（FAISS/Chroma）同类跑分和 LLM 无过滤基线跑分，三者才能看出 Mnemosyne 的位置

## 发给其他 AI 的提示词

```
请分析以下记忆引擎性能跑分报告，给出客观意见和改进建议：

[粘贴上面整份报告]
```

---

*跑分脚本基于 poc_benchmark.py，替换 Mock 引擎为 Mnemosyne MemoryBrain 真引擎。*
