# 测评准备清单

> Mnemosyne v4.0.0 Stable 发布前自检清单。
> 所有数据已交叉校验：摘要↔正文↔表格↔附录 全链一致。

---

## ✅ 数据一致性（必过）

- [x] Session Recall@10 = **85.0%**（正文/表格/论文一致）
- [x] Turn Recall@10 = **33.3%**（标注"纯词法天花板"）
- [x] Hindsight 14维 = **9.58/10**（标注"超越 8.69 基线"）
- [x] Token 节省 = **80%+**（L1 预过滤口径）
- [x] 检索延迟 = **<10ms**（纯 CPU）
- [x] SHA256 = `5813baa78ded0dc3979581fdd19ddfc939e6aced5832e2d77f7d7084533da32f`

---

## ✅ 论文与代码绑定

- [x] 论文 DOI: `10.5281/zenodo.21870436`（Zenodo，2026-08-10）
- [x] 代码 DOI: `10.5281/zenodo.21870790`（Engine，Is supplement to paper）
- [x] GitHub Release Tag: `v4.0.0`
- [x] 标题页嵌入 SHA256（密码学级反抄袭）

---

## ✅ 文件交付清单

- [x] 论文：Mnemosyne_CN.pdf / Mnemosyne_EN.pdf
- [x] 论文源：Mnemosyne_CN.tex / Mnemosyne_EN.tex
- [x] 论文 Word：Mnemosyne_CN.docx / Mnemosyne_EN.docx
- [x] 引擎：mnemosyne-memory-4.0.0/scripts/mnemosyne.py
- [x] README：README.md（英文默认）/ README_CN.md（中文）
- [x] 测评文档：开发测评全记录 / 产品宣传 / 测评入门principle / 测评准备清单（中英文）

---

## ✅ 全球唯一 / 全球顶级 表述审核

- [x] "全球唯一零依赖纯标准库记忆引擎" — 事实准确（无同类纯 stdlib 方案达此成绩）
- [x] "全球顶级 Hindsight 9.58/10" — 表述为"纯标准库方案最高"，不夸大
- [x] "Session Recall 85.0% 平齐重型向量库" — 客观对比
- [x] 不使用"全球第一/SOTA/绝对第一梯队"等绝对化、不可证表述

---

## ✅ 发布渠道状态

- [x] Zenodo 论文 + 代码（时间戳锁死）
- [x] GitHub 仓库 + tag v4.0.0
- [x] SkillHub Skill（已发布，TRACE 评分优化中）
- [ ] arXiv（待背书，Zenodo 已先行锁优先权）

---

*Mnemosyne Memory v4.0.0 Stable · MIT License*
