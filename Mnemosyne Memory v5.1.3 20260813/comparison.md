# Mnemosyne Memory vs mnemosyne-oss — 诚实技术对比

> 最后更新：2026-08-12 · Mnemosyne v5.1.3 Stable

两款同名项目，定位不同。选谁，取决于你的需求。

---

## 一句话差异

| | Mnemosyne Memory（我们） | mnemosyne-oss（Abdias J） |
|------|------|------|
| 核心哲学 | 零依赖纯标准库，复制即用 | 功能丰富，pip install 生态 |

---

## 架构对比

| 维度 | 我们 | mnemosyne-oss |
|------|------|------|
| 代码规模 | 单文件 ~3700 行 | 60+ 文件，模块化 |
| 依赖数 | **0**（纯 Python 标准库） | 3+（SQLite, fastembed, ctransformers） |
| 存储引擎 | JSONL（纯文本，可手动编辑） | SQLite（WAL 模式，事务安全） |
| 向量检索 | 无（纯词法 BM25 五路融合） | fastembed ONNX + sqlite-vec |
| 安装方式 | 复制一个 .py 文件 | `pip install mnemosyne-memory` |
| 离线可用 | ✅ 完全离线 | ✅ 本地运行 |
| 磁盘体积 | ~170KB | ~67MB（含 ONNX 模型） |
| 启动时间 | 瞬时 | 1-3s（模型加载） |

---

## 功能对比

| 功能 | 我们 | mnemosyne-oss |
|------|:--:|:--:|
| 写入/检索 | ✅ | ✅ |
| 批量写入（15x 加速） | ✅ | ✅ |
| 知识图谱 | ✅ | ✅ TripleStore |
| 记忆巩固/压缩 | ✅ reflect + consolidate | ✅ sleep() |
| 自动统计监控 | ✅ 8维三列表 | ✅ |
| Token 1:1 账单对齐 | ✅ DeepSeek-V2 原生 | ❌ |
| MCP Server | ✅ v4.0.0 新增 | ✅ |
| 健康检查 doctor | ✅ v4.0.0 新增 | ✅ 70KB doctor.py |
| 多项目隔离 | ✅ project 参数 | ✅ project 参数 |
| 时序版本追踪 | ✅ template_hash + temporal_query | ✅ TripleStore 时间图 |
| LLM 冲突检测 | ❌ 无 | ✅ LLM backends |
| 向量语义检索 | ❌ 纯词法 | ✅ embedding + vector |
| Docker 部署 | ❌ | ✅ |
| Obsidian/VSCode 插件 | ❌ | ✅ |
| PyPI 发布 | 开发中 | ✅ mnemosyne-memory v3.15.1 |

---

## 性能对比（同硬件：AMD Ryzen 9 8945H）

| 指标 | 我们 | mnemosyne-oss |
|------|:--:|:--:|
| Token 节省率 | **91.7%**（三组独立验证，σ=0.07%） | 未公开验证数据 |
| 检索延迟（P50） | 91.7 ms（50 Session） | 宣称 sub-ms（未提供复现脚本） |
| 检索延迟（P50 热缓存） | 353 ms | — |
| 内存占用 | 44.6 MB（50 Session） | 10-20 MB per session |
| Token 统计精度 | DeepSeek-V2 原生 1:1 账单对齐 | tiktoken cl100k_base |

---

## 各自优势

### 我们的优势
- **零依赖**：不需要 pip install、不需要模型下载、不需要 Docker
- **1:1 账单对齐**：搭载 DeepSeek-V2 原生分词器，统计与计费完全一致
- **可复现性**：Benchmark Lab 公开脚本 + 三轮 σ=0.07% 验证
- **Zenodo DOI**：学术时间戳已锁死
- **纯词法确定性**：无向量漂移，检索结果 100% 可复现

### mnemosyne-oss 的优势
- **生态完整**：PyPI + MCP + Docker + Obsidian/VSCode 插件
- **向量检索**：语义搜索比纯词法更灵活
- **社区规模**：2388 Stars，ProductHunt，Discord
- **时序追踪**：TripleStore 时间图 + 版本管理
- **文档丰富**：独立网站 + 完整 API 文档

---

## 如何选择

| 你的场景 | 建议 |
|------|------|
| 需要零依赖、离线、单文件部署 | **我们** |
| 需要语义搜索、向量检索 | mnemosyne-oss |
| 需要与 Hermes Agent 深度集成 | **我们**（原生 SkillHub 集成） |
| 需要 MCP + Docker + 多 IDE 集成 | mnemosyne-oss |
| 需要 Token 成本 100% 精确统计 | **我们**（DeepSeek-V2 原生） |
| 需要大规模社区和支持 | mnemosyne-oss（2388 Stars） |

---

## 我们正在追赶的

1. PyPI 包发布（计划中）
2. 时序版本追踪（借鉴 TripleStore 思路，用 JSONL 实现）
3. 多项目隔离（`project` 参数）
4. 社区推广（ProductHunt, HN）

> 两个项目都在快速迭代。上述对比基于 2026-08-12 公开数据，如有偏差欢迎指正。
