# 国际记忆系统测评完全入门principle

> 写给想复现 Mnemosyne 成绩、或自建记忆系统评测的开发者。
> 适用版本：v4.0.0 Stable ｜ 日期：2026-08-10

---

## 一、为什么要测评记忆系统？

AI Agent 的"记忆"质量直接决定其长期任务表现。但记忆系统鱼龙混杂，必须用**国际公认基准**做横向对比，而不是自说自话。

Mnemosyne 选择两大权威基准：

| 基准 | 来源 | 测什么 |
|------|------|------|
| **Hindsight 14维** | GoodAI | 记忆系统综合能力（14 个维度打分） |
| **LongMemEval** | 斯坦福 | 长时对话记忆检索精度 |

---

## 二、Hindsight 14维是什么？

GoodAI 发布的 Agent 记忆系统评测框架，被 Mem0、Letta、Zep 等主流记忆系统广泛引用为对标基线。

14 个维度包括：写入机制、检索能力、记忆模型、压缩机制、遗忘机制、记忆生命周期、企业级能力、工程实现、检索智能等。

**Mnemosyne 成绩：9.58/10**（超越 Hindsight 官方 8.69 基线，13/14 维度领先）。

---

## 三、LongMemEval 是什么？

斯坦福发布的长时对话记忆检索基准，包含 18000+ 条对话记录，要求系统在海量历史中精确定位相关记忆。

两个核心指标：
- **Session Recall@10**：能否找到"正确的对话会话" → Mnemosyne **85.0%**
- **Turn Recall@10**：能否找到"会话里正确的那句话" → Mnemosyne **33.3%**（纯词法天花板）

---

## 四、如何复现 Mnemosyne 成绩？

```bash
git clone https://github.com/FrankHu-HK/mnemosyne.git
cd mnemosyne/mnemosyne-memory-4.0.0/scripts
python mnemosyne.py hindsights-bench   # 跑 Hindsight 14维自评
python mnemosyne.py benchmark --count 5000  # 跑检索延迟基准
```

LongMemEval 官方流程请参考论文附录，需接入官方评测脚本。

---

## 五、纯词法检索的天花板

我们通过 8 组 A/B 实验证明：**纯词法检索在 LongMemEval 的 Turn Recall@10 上限为 33.3%**。

根因：LongMemEval 匹配逻辑是 `rc[:60] in original_turn`，任何语义增强（Query Rewriting / Fact Layer / 别名改写）都会改变检索结果、把原始 Turn 挤出 Top10。

**突破方向**：接入 LLM 做语义重排，预计 Turn Recall 可提升至 60–80%。

---

## 六、给开发者的建议

1. 不要盲目堆向量数据库——先想清楚你的场景是 Session 级还是 Turn 级
2. 纯词法方案 Session Recall 已能到 80%+，足够很多 Agent 场景
3. 如果必须 Turn 级精度，准备 LLM 预算，或接受 33% 天花板

---

*Mnemosyne Memory v4.0.0 Stable · MIT License*
