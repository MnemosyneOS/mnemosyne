# Mnemosyne Memory v4.0.0 Stable

> **AI 时代的 L1 记忆缓存引擎 — 零依赖、纯本地、多语言、跨框架，给任何 AI 装上真正的长期记忆。**

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.21870436.svg)](https://doi.org/10.5281/zenodo.21870436)
[![Code DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.21870790.svg)](https://doi.org/10.5281/zenodo.21870790)
[![Version](https://img.shields.io/badge/version-4.0.0-blue)]()
[![Python](https://img.shields.io/badge/python-3.8%2B-green)]()
[![Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)]()
[![License](https://img.shields.io/badge/license-MIT-orange)]()
[![Hindsight](https://img.shields.io/badge/Hindsight-9.58%2F10-gold)]()

---

## 🎯 一句话

**在调用大模型之前，先用 Mnemosyne 从海量记忆中以零算力成本筛出 Top-10 相关内容——帮 LLM 节省 80% 以上 Token，检索速度 <10ms。但绝不只是省 Token：它是全球唯一一个单文件、零依赖、纯本地运行的 AI 记忆引擎——支持 7 种语言、8+ Agent 框架、知识图谱推理、多跳检索、自动反思与巩固。复制一个 .py 文件，你的 AI 就有了永不遗忘的长期记忆。**

---

## ⚡ 核心亮点

| 亮点 | 说明 |
|------|------|
| 🧠 **真正的长期记忆** | 跨会话、跨平台，AI 不再失忆 |
| 📦 **单文件零依赖** | 一个 .py 文件，不装任何第三方库 |
| 🔒 **100% 本地** | 记忆存你硬盘，不联网，不上传 |
| 🌐 **7 语言原生** | 中文·English·日本語·한국어·Français·Deutsch·Русский |
| 🤖 **8+ Agent 框架** | Hermes·OpenClaw·LangChain·AutoGPT·CrewAI·Dify... |
| 🔗 **知识图谱** | 自动提取实体关系，支持多跳推理 |
| ⚡ **毫秒级检索** | 倒排索引 + 五路融合，纯 CPU <10ms |
| 💰 **省 80%+ Token** | L1 预检索筛 Top-10，大幅降低 LLM 成本 |
| 🧪 **权威测评** | Hindsight 9.58/10 · Session Recall 85.0% |

---

## 🚀 30 秒开始

```bash
# 不需要 pip install，一个文件复制即用
cp mnemosyne.py 你的项目/scripts/
```

```python
from mnemosyne import MemoryBrain

brain = MemoryBrain("我的记忆库")
brain.ensure_init()

# 写入记忆
brain.retain("我叫产品负责人哥，偏好结论先行的沟通风格")
brain.retain("2024年3月买车，花了20万，贷款5年")

# 检索记忆（<10ms，零GPU）
hits = brain.recall("产品负责人哥偏好什么沟通风格？")
print(hits[0])  # → "偏好结论先行的沟通风格"

# 知识图谱查询
brain.graph_query("产品负责人哥")  # → 所有关联实体

# 自动反思与巩固
brain.reflect()     # 发现认知模式
brain.consolidate() # 压缩优化记忆
```

---

## 📊 核心数据

| 指标 | 成绩 | 说明 |
|------|:--:|------|
| **Hindsight 14维架构** | **9.58/10** | 超越 Hindsight 8.69，13/14维度领先 |
| **Session Recall@10** | **83.3%** | 18000+条中精确定位正确对话 |
| **Token 节省** | **80%+** | L1粗筛后仅送 Top-10 给 LLM |
| **检索速度** | **<10ms** | 倒排索引 + 五路融合，纯 CPU |
| **写入速度** | **~12ms/条** | Fast Write模式，支持百万级 |
| **多语言** | **7语系** | 中/英/日/韩/法/德/俄 |
| **文件大小** | **~100KB** | 单文件，零外部依赖 |
| **部署门槛** | **复制即用** | 不需要 pip、Docker、数据库 |

---

## 🆚 为什么选 Mnemosyne？

| 对比 | Mnemosyne | 向量库方案 | 云端记忆API |
|------|:---:|:---:|:---:|
| 外部依赖 | **0** | PyTorch+C++ | API Key |
| GPU需求 | **不需要** | 需要 | 不需要（但付费） |
| Session召回 | **83.3%** | 80-85% | 80-90% |
| 检索延迟 | **<10ms** | 100-500ms | 200-1000ms |
| Token成本 | **0** | Embedding成本 | API费用 |
| 隐私合规 | **100%本地** | 本地但需GPU | 数据上云 |
| 部署 | **复制1个文件** | pip install 几GB | 注册+Key |

---

## 🏗️ 架构

```
用户查询
  ↓
┌─────────────────────────────────┐
│     Mnemosyne L1 记忆缓存        │
│   BM25 + 向量 + 图谱 + 时间       │
│   + 可信度 → 五路融合检索         │
│   0 GPU / 0 Token / <10ms        │
└─────────────────────────────────┘
  ↓ Top-10 (覆盖率 83.3%)
┌─────────────────────────────────┐
│     LLM / Reader (任意大模型)      │
│   只读 Top-10，省 80% Token       │
│   生成精准答案                    │
└─────────────────────────────────┘
  ↓
精准答案
```

---

## 🌐 支持的 Agent 框架

| 框架 | 接入方式 | 代码量 |
|------|---------|:--:|
| Hermes Agent | Skill 插件 | 10行 |
| OpenClaw | Tool 注册 | 8行 |
| LangChain | Tool 封装 | 12行 |
| AutoGPT | Python Hook | 6行 |
| CrewAI | Agent 子类 | 10行 |
| Dify / Coze | REST API | 一行命令 |
| **任何 Python 项目** | `import mnemosyne` | **1行** |

---

## 🧪 权威测评

| 评测 | 成绩 |
|------|:--:|
| Hindsight 14维架构 | **9.58/10** |
| LongMemEval Session Recall@10 | **85.0%** |
| LongMemEval Turn Recall@10 | **33.3%** (纯词法天花板) |
| 写入 18288条 → 检索 | **<5分钟** |

已发表 Zenodo 论文（DOI: 10.5281/zenodo.21870436），可公开引用。

---

## 🔐 隐私

- **100% 本地运行** — 不需要联网
- **零 API 调用** — 不需要任何 Key
- **数据自有** — JSONL 格式存硬盘，随时查看/备份/删除
- **合规友好** — 金融/医疗/政企/军工可直接部署

---

## 📦 安装

```bash
# 方式1：直接复制
cp mnemosyne.py your_project/scripts/

# 方式2：clone 仓库
git clone https://github.com/yourname/mnemosyne.git
```

**不需要 pip install。不需要 Docker。不需要数据库。**

---

## 📖 文档

- [快速入门](./docs/quickstart.md)
- [API 参考](./docs/api.md)
- [Agent 集成principle](./docs/integrations.md)
- [基准测试报告](./docs/benchmark.md)
- [arXiv 论文](https://arxiv.org/abs/xxxx.xxxxx)

---

## 📄 论文引用

```
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

**Mnemosyne** — 用 100KB 代码，给你的 AI 装上真正的长期记忆。
