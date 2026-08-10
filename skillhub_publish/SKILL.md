---
name: mnemosyne-memory
slug: mnemosyne-memory
displayName: "Mnemosyne Memory | 永不遗忘的AI记忆系统 | 大模型节省80%+Token | 权威Hindsight 14 维评分全球顶级"
version: 4.0.2
description: "零依赖AI Agent记忆引擎。Hindsight 9.58/10全球最高分，Session Recall 85%，Token省80%+。"
author: 胡景产品负责人
license: MIT
tags: [memory, agent, retrieval, edge-ai, rag, llm, token-optimization, local-first, offline]
---

# Mnemosyne Memory v4.0.0 Stable

> **全球唯一单文件、零依赖、纯标准库 AI Agent 记忆引擎**
> 一个 .py 文件，给你的 AI 装上永不遗忘的长期记忆。

📄 论文 DOI: `10.5281/zenodo.21870436` | 💻 代码 DOI: `10.5281/zenodo.21870790` | 🐙 GitHub: `github.com/FrankHu-HK/mnemosyne`

---

## 30 秒上手（不必读全文）

**情况 1：只想试用**
```bash
pip install mnemosyne-memory  # 待上架 PyPI，当前用方案2
```

**情况 2：生产部署**
```bash
python install.py  # 自动下载 mnemosyne.py 到当前目录
python mnemosyne.py demo  # 运行演示
```

**情况 3：已有项目集成**
```python
from mnemosyne import MemoryBrain
brain = MemoryBrain("我的记忆库")
brain.ensure_init()
brain.retain("我叫产品负责人哥，偏好结论先行的沟通风格")
results = brain.recall("产品负责人哥偏好什么沟通风格？")
print(results[0])
```

---

## 核心数据

- 🏆 **Hindsight 14维：9.58/10**（全球最高，超越 Hindsight 8.69）
- 🎯 **LongMemEval Session Recall@10：85.0%**（平齐重型向量数据库）
- ⚡ **检索延迟：<10ms**（纯 CPU，零 GPU）
- 💰 **LLM Token 节省：80%+**（L1 缓存预过滤）
- 📦 **部署体积：单文件 ~100KB**，复制即用，零 pip install
- 🌐 **7 语言原生**：中文·English·日本語·한국어·Français·Deutsch·Русский
- 🤖 **8+ Agent 框架**：Hermes·OpenClaw·LangChain·AutoGPT·CrewAI·Dify·Coze·OpenAI

---

## 适用场景（Trigger Conditions）

**应该用 Mnemosyne 的情况：**
- 你的 AI Agent 需要跨会话记住用户偏好和历史
- 你需要一个纯本地、零依赖的记忆引擎（边缘设备/AI PC/离线环境）
- 你想在调用 LLM 之前做一层快速预检索，大幅降低 Token 成本
- 你的项目不能引入 GPU、向量数据库、外部 API（合规/隐私要求）
- 你需要在多个 Agent 框架间共享同一套记忆

**不应该用 Mnemosyne 的情况：**
- 你需要海量分布式全文搜索（用 Elasticsearch/Milvus）
- 你需要毫秒级亿级向量检索（用专用向量数据库）
- 你需要实时多人协作 ACID 事务（用 PostgreSQL）
- 你需要纯粹的端到端问答系统（Mnemosyne 是 L1 缓存，需搭配 LLM）
- 你的全部数据已经在云端向量数据库中（不需要本地引擎）

---

## 快速安装

```bash
# 方式1：直接复制（推荐，零网络依赖）
# 本 Skill 目录下已包含完整引擎
cp mnemosyne.py 你的项目目录/

# 方式2：从 GitHub 下载
python install.py

# 方式3：GitHub 镜像（国内加速）
git clone https://gitclone.com/github.com/FrankHu-HK/mnemosyne.git
```

**不需要 pip install。不需要 Docker。不需要数据库。**

---

## 使用示例

```python
from mnemosyne import MemoryBrain

# 初始化
brain = MemoryBrain("我的记忆库")
brain.ensure_init()

# 写入记忆
brain.retain("我叫张三，2024年3月入职Acme公司")
brain.retain("我偏好简洁的沟通风格，不喜欢啰嗦")
brain.retain("我的生日是3月15日")

# 检索记忆（<10ms，纯CPU）
hits = brain.recall("张三什么时候入职的？", k=5)
for score, record, reasons in hits:
    print(f"{score:.3f} | {record['content']}")

# 知识图谱查询
brain.graph_query("张三")  # → 所有关联实体和关系

# 自动反思与巩固
brain.reflect()       # 发现记忆中的认知模式
brain.consolidate()   # 自动压缩和整理记忆
```

---

## 集成到 Agent 框架

**Hermes Agent（Skill 插件，10 行）**
```python
@tool
def remember(content: str): brain.retain(content)

@tool
def recall(query: str) -> str:
    hits = brain.recall(query, k=5)
    return '\n'.join([h[1]['content'] for h in hits])
```

**OpenClaw（Tool 注册，8 行）** · **LangChain（Tool 封装，12 行）** · **AutoGPT（Python Hook，6 行）**
→ 完整集成principle见 `README.md` 或 GitHub 仓库。

---

## 能力边界

- ✅ **能做**：存储/检索/反思/巩固对话记忆、知识图谱推理、多语言分词与实体抽取、L1 缓存预过滤
- ❌ **不能做**：替代 LLM 做语义理解和推理、替代向量数据库做海量相似搜索、替代 PostgreSQL 做 ACID 事务
- ⚠️ **Turn 级精确定位**：33.3% Recall@10（纯词法天花板），接 LLM 可提升至 60-80%

---

## 常见问题（FAQ）

**Q: 这个 Skill 有可运行的代码吗？**
A: 有。本 Skill 目录下包含完整的 `mnemosyne.py` 引擎（v4.0.0 Stable）和 `install.py` 安装脚本。复制即用。

**Q: 为什么用 Zenodo 而不是 arXiv？**
A: arXiv 要求首次提交被已有作者背书（Endorsement），流程需数天。Zenodo（CERN 运营）零门槛即时发表，同等 DOI + 时间戳法律效力。arXiv 版本后续补充。

**Q: 可以商用吗？**
A: 可以。MIT 协议，自由使用、修改、分发。论文引用请保留 DOI。

**Q: 支持哪些语言？**
A: 中文、English、日本語、한국어、Français、Deutsch、Русский 共 7 种。分词器和实体抽取均为语言感知，无需外部 NLP 库。

---

## 已知限制

1. **Turn 级定位天花板**：33.3% Turn Recall@10，纯词法无法突破，需 LLM 辅助。
2. **单机运行**：当前不支持分布式/集群部署，适合单 Agent 或中小规模场景。
3. **内存占用**：索引开销约为原始数据 2 倍，百万级以上建议优化。
4. **非实时**：写入有 ~12ms 延迟（Fast 模式），不适合微秒级实时写入。
5. **纯 CPU**：不利用 GPU 加速，大规模向量检索时吞吐量低于 GPU 方案。

---

## 版本更新说明

| 版本 | 日期 | 变更 |
|------|------|------|
| **v4.0.0** | 2026-08-10 | 正式版：Hindsight 9.58/10, Session Recall 85.0%, Zenodo 论文发表 |
| v3.x | 2026-08 | 消融实验期：验证 5 种优化策略，确认纯词法 Turn Recall 天花板 |

---

## 反模式（新手避坑）

- ❌ **不要当 LLM 替代品用**：Mnemosyne 是检索引擎，不是推理引擎。检索结果需交给 LLM 做最终理解。
- ❌ **不要跳过 `ensure_init()`**：首次使用必须先初始化，否则读写会报错。
- ❌ **不要频繁创建新 MemoryBrain 实例**：共享数据库模式比每题独立模式性能高 60%+（Session Recall 85% vs 21%）。
- ❌ **不要用 `fast=False` 做大量写入**：Fast Write 模式（`brain.retain(..., fast=True)`）比完整模式快 2.6 倍。

---

## 论文引用

```bibtex
@article{hu2026mnemosyne,
  title={Mnemosyne: Pushing Lexical Retrieval to the LongMemEval Ceiling
         with a Zero-Dependency Architecture},
  author={Hu, Jingkun},
  year={2026},
  doi={10.5281/zenodo.21870436},
  url={https://doi.org/10.5281/zenodo.21870436}
}
```

---

*Mnemosyne Memory v4.0.0 Stable · MIT License · 评分口径以 SkillHub 官方 TRACE 为准*
