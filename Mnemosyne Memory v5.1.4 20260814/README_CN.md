# Mnemosyne Memory v5.1.4 Stable

[English](README.md) | [中文](README_CN.md)

> **AI 时代的 L1 记忆缓存引擎 — 零依赖、纯本地、多语言、跨框架，给任何 AI 装上真正的长期记忆。**

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.21870436.svg)](https://doi.org/10.5281/zenodo.21870436)
[![Code DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.21870790.svg)](https://doi.org/10.5281/zenodo.21870790)
[![Version](https://img.shields.io/badge/version-5.1.4-blue)]()
[![Python](https://img.shields.io/badge/python-3.8%2B-green)]()
[![Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)]()
[![License](https://img.shields.io/badge/license-MIT-orange)]()
[![Hindsight](https://img.shields.io/badge/Hindsight-9.58%2F10-gold)]()

---

## 一句话

**在调用大模型之前，先用 Mnemosyne 从海量记忆中以零算力成本筛出 Top-10 相关内容——帮 LLM 节省 80% 以上 Token，检索速度 <10ms。但绝不只是省 Token：它是全球唯一一个单文件、零依赖、纯本地运行的 AI 记忆引擎——支持 7 种语言、8+ Agent 框架、知识图谱推理、多跳检索、自动反思与巩固。复制一个 .py 文件，你的 AI 就有了永不遗忘的长期记忆。**

---

## 核心亮点

| 亮点 | 说明 |
|------|------|
| 🧠 真正长期记忆 | 跨会话、跨平台，AI 不再失忆 |
| 📦 单文件零依赖 | 一个 .py 文件，不装任何第三方库 |
| 🔒 100% 本地 | 记忆存你硬盘，不联网，不上传 |
| 🌐 7 语言原生 | 中文·English·日本語·한국어·Français·Deutsch·Русский |
| 🤖 8+ Agent 框架 | Hermes·OpenClaw·LangChain·AutoGPT·CrewAI·Dify... |
| 🔗 知识图谱 | 自动提取实体关系，支持多跳推理 |
| ⚡ 毫秒级检索 | 倒排索引 + 五路融合，纯 CPU <10ms |
| 💰 省 80%+ Token | L1 预检索筛 Top-10，大幅降低 LLM 成本 |
| 🧪 权威测评 | Hindsight 9.58/10 · Session Recall 85.0% |
| 🕐 时序查询 | `temporal_query()` 按时间查询记忆，还原事件版本链 |
| 🏥 健康检查 | `doctor()` 自检 + `memory_repair()` 自动修复 |
| 📂 多项目隔离 | `retain`/`recall` 支持 `project=` 参数 + `list_projects()` |
| 🔌 MCP Server | 8 工具 MCP 服务，任意 MCP 客户端即插即用 |

---

## 📊 测评成绩

| 评测 | 成绩 | 说明 |
|------|:--:|------|
| **Hindsight 14维架构** | **9.58/10** | 超越 Hindsight 8.69，13/14维度领先 |
| **Session Recall@10** | **85.0%** | 18000+条中精确定位正确对话 |
| **Token 节省** | **80%+** | L1粗筛后仅送 Top-10 给 LLM |
| **检索速度** | **<10ms** | 倒排索引 + 五路融合，纯 CPU |
| **写入速度** | **~12ms/条** | Fast Write模式，支持百万级 |

---

## 🚀 高级功能

```python
# 多项目隔离 — 同一引擎，各项目记忆互不干扰
brain.retain("Acme 使用 Kafka", project="acme")
brain.recall("消息队列?", project="acme")   # 只看 acme 项目的记忆
brain.list_projects()                       # -> ["acme", "default", ...]

# 时序查询 — 还原"什么时候发生了什么"
brain.temporal_query(entity="部署")         # -> 按时间排序的版本链

# 健康检查与自修复
brain.doctor()          # 扫描完整性、记录数、磁盘占用
brain.memory_repair()   # 自动从损坏中恢复

# 批量写入 — 15 倍加速
brain.retain_batch(["a", "b", "c"])
```

**MCP Server**（8 工具：`retain`、`recall`、`stats`、`graph_query`、`retain_batch`、`doctor`、`temporal_query`、`list_projects`）：
```bash
python mcp_server.py   # stdio MCP 服务，接入任意 MCP 客户端
```

---

## 🆚 竞品对比

| 能力 | Mnemosyne | 向量数据库方案 | 云端记忆API |
|------|:---:|:---:|:---:|
| 外部依赖 | **0** | PyTorch+C++ | API Key |
| GPU需求 | **不需要** | 需要 | 不需要（但付费） |
| Session召回 | **85.0%** | 80-85% | 80-90% |
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
  ↓ Top-10 (覆盖率 85.0%)
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
|------|------|:--:|
| Hermes Agent | Skill 插件 | 10行 |
| OpenClaw | Tool 注册 | 8行 |
| LangChain | Tool 封装 | 12行 |
| AutoGPT | Python Hook | 6行 |
| CrewAI | Agent 子类 | 10行 |
| Dify / Coze | REST API | 一行命令 |
| **任何 Python 项目** | `import mnemosyne` | **1行** |

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
git clone https://github.com/FrankHu-HK/mnemosyne.git
```

**不需要 pip install。不需要 Docker。不需要数据库。**

---

## 📄 论文引用

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

**Mnemosyne** — 用 100KB 代码，给你的 AI 装上真正的长期记忆。
