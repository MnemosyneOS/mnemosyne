#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""精准记忆 / 精准召回 / 极限压缩 的**离线**回归（7.0.2 起随包交付）。

与 `verify_memory_lifecycle.py` 的分工
--------------------------------------
- `verify_memory_lifecycle.py`：走 **MCP stdio 子进程**，验"工具面 / 库内落盘 / 生命周期
  闭环"，**需要** Qdrant + 嵌入服务（真实向量栈）。
- 本脚本：**纯离线**（标准库 + 进程内嵌入引擎，不连任何服务），验"**机制性保证**"——
  那些"一旦回归就必然导致'记忆不准 / 压缩丢信息'的硬不变量"。
  它是改代码后的**第一道预检**：几秒内就能发现打分、阈值、压缩、去重的机制被改坏。

为什么要把这些性质写成断言
--------------------------
"精准"不是一个可以靠"感觉召回挺准"来验收的形容词。它必须落到**可断言的结构性质**上：

* **原子守恒**：数字/日期/金额/型号/URL/邮箱/引号原话，在任何压缩层级都不得丢失
  （丢了就等于丢了"可被追问的事实"）。
* **零幻觉**：压缩产物的自然语言片段必须是原文的**逐字子串**（抽取式，不作任何生成）。
* **无损可逆**：任何降级都必须能用指针逐字取回原文。
* **预算诚实**：预算口径必须与真实 token 量同量级，否则"极致短上下文"是空话。
* **原子不同绝不合并**：只差一个数字的两条记忆相似度可以到 0.98+，误并会**改写事实**。

用法
----
    <python> scripts/verify_precision_recall.py

退出码 0 = 全部通过；非 0 = 有断言失败（标准输出逐项 PASS/FAIL）。
不需要网络、不需要外部服务、不改动任何既有记忆库（全部跑在临时目录里，跑完自删）。
"""
import os
import shutil
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")

# 允许从源码根直接运行（`python scripts/xxx.py` 时把上一级加进 sys.path）
_SRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

FAIL = []


def check(cond, label, detail=None):
    print(("  [PASS] " if cond else "  [FAIL] ") + label
          + ("" if detail is None else "   | " + str(detail)[:220]))
    if not cond:
        FAIL.append(label)


def main():
    from mnemosyne import MemoryBrain
    from mnemosyne.capsule import (build_capsule, capsule_cache_stats,
                                   capsule_selfcheck, parse_ref)
    from mnemosyne.retrieval import (REL_HIGH_SEMANTIC, REL_MIN_NGRAM,
                                     REL_MIN_SEMANTIC)
    from mnemosyne.utils import (_content_atoms, _extract_relationships,
                                 atoms_preserved, encode_replaced_stats,
                                 sanitize_str)

    tmp = tempfile.mkdtemp(prefix="mnemo_prec_")
    try:
        b = MemoryBrain(tmp, namespace="precision", actor="selfcheck")
        b.ensure_init()

        # ---------------- 1. 内容原子 ----------------
        print("\n### 1. 内容原子（「改一个字就是另一个事实」的片段）")
        check(_content_atoms("用户的年假是 5 天") != _content_atoms("用户的年假是 15 天"),
              "只差一个数字的两句 → 原子多重集不同")
        check(_content_atoms("3 个 5 分钟") != _content_atoms("5 个 3 分钟"),
              "重复次数参与比较（多重集，不是集合）")
        a = _content_atoms("端口 6333，模型 bge-m3 共 1024 维，预算 15000 元，期限 2026-12-31")
        check(all(k in a for k in ("6333", "bge-m3", "1024", "15000元", "2026-12-31")),
              "数字/型号/维度/金额/日期均被识别", sorted(a))
        ok, missing = atoms_preserved("预算 3.5 万元，涨幅 12%", "预算 3.5 万元")
        check((not ok) and "12%" in missing, "缺原子能被检出（12%）", missing)
        ok2, _m2 = atoms_preserved("预算 3.5 万元，涨幅 12%", "预算 3.5万元，涨幅 12%")
        check(ok2, "空白差异不算丢原子（口径：大小写与空白均不敏感）")

        # ---------------- 2. 关系抽取 / 图通道 ----------------
        print("\n### 2. 关系抽取与图通道（P1-1）")
        r1 = _extract_relationships([], "用户的显卡是 RTX 4090")
        check(any(x["from"] == "用户" and x["relation"] == "is_a"
                  and x["to"] == "RTX 4090" and x.get("qualifier") == "显卡" for x in r1),
              "A的B是C → 主语取 A、属性进 qualifier", r1)
        check(any(x["relation"] == "prefers"
                  for x in _extract_relationships([], "用户喜欢喝大窑汽水")),
              "偏好句 → prefers")
        check(any(x["relation"] == "located_in"
                  for x in _extract_relationships([], "用户在上海工作")),
              "居所句 → located_in")

        b.retain("用户的显卡是 RTX 4090", mtype="semantic")
        b.retain("用户喜欢喝大窑汽水", mtype="preference")
        g = b.graph_query("用户")
        check(bool(g.get("edges")), "graph_query('用户') 非空（7.0.1 实测恒空）",
              len(g.get("edges") or []))
        check(any(e.get("qualifier") for e in g.get("edges") or []),
              "输出带 qualifier（按属性反问可被回答）")

        # 模拟 7.0.1 存量库（无图边 + 无回填标记）→ 回填必须补齐
        for p_ in (os.path.join(tmp, "graph.jsonl"),
                   os.path.join(tmp, ".graph_edges_backfilled_v702")):
            if os.path.exists(p_):
                os.remove(p_)
        b2 = MemoryBrain(tmp, namespace="precision", actor="selfcheck2")
        stat = b2._backfill_graph_edges(force=True)
        check((stat.get("edges_written") or 0) > 0 and
              bool(b2.graph_query("用户").get("edges")),
              "存量数据图边懒回填生效（旧记忆也能进图谱）", stat)
        b2.close()

        # ---------------- 3. 相关性下限 ----------------
        print("\n### 3. 相关性下限（P0-2）：宁缺勿滥")
        check(REL_MIN_SEMANTIC < REL_HIGH_SEMANTIC and REL_MIN_NGRAM > 0,
              "阈值自洽（min < high）",
              (REL_MIN_SEMANTIC, REL_HIGH_SEMANTIC, REL_MIN_NGRAM))
        rel = b.recall("用户的显卡是什么", k=5)
        m_rel = dict(b.retrieval.last_recall_metrics)
        bands_rel = [x[1].get("_relevance_band") for x in rel if isinstance(x[1], dict)]
        check(len(rel) > 0 and m_rel.get("top1_band") == "high",
              "相关查询有结果且 top1=high", (m_rel.get("top1_band"), bands_rel))
        irr = b.recall("舒芙蕾怎么做", k=5)
        m_irr = dict(b.retrieval.last_recall_metrics)
        check((m_irr.get("returned") or 0) <= 1 and (m_irr.get("floor_dropped") or 0) > 0,
              "无关查询被下限清空（旧行为是填满 k 条）",
              (m_irr.get("returned"), m_irr.get("floor_dropped"), len(irr)))
        # 跨调用隔离：无关查询之后，先前结果的 band 不得被改写
        bands_after = [x[1].get("_relevance_band") for x in rel if isinstance(x[1], dict)]
        check(all(x != "low" for x in bands_after),
              "跨调用不污染已返回结果的 band（输出边界快照）", bands_after)
        # 目标定位：绕过下限（否则含糊描述无法遗忘）
        tgt = b.resolve_targets(query="舒芙蕾怎么做", k=1)
        check(bool(tgt), "目标定位 apply_floor=False（含糊描述仍可定位）",
              (tgt[0][0] if tgt else None))

        # ---------------- 4. access_boost 正反馈 ----------------
        print("\n### 4. access_boost 不构成正反馈（P0-1）")
        for _ in range(12):
            b.recall("用户的显卡是什么", k=3)
        recs = {r["id"]: r for r in b.store.all_records()}
        caps = [r.get("access_count") or 0 for r in recs.values()]
        check(all(c <= 5 for c in caps), "access_count 封顶 5（打分侧上限对齐）", max(caps))

        # ---------------- 5. 预算诚实 + 分档 ----------------
        print("\n### 5. budget_tokens：诚实、分档、不丢信息")
        topics = ["量子计算", "南极科考", "咖啡烘焙", "帆船航行", "陶艺拉坯",
                  "山地骑行", "爵士乐理", "天文观测", "蜂箱管理", "漆器修复"]
        filler = ("该主题下逐条记录可独立验证的事实，用于预算装箱与降级路径的回归；"
                  "每条都写得足够长，确保在小预算下必然放不进正文而触发胶囊降级。")
        sizes = {}
        for i, t in enumerate(topics):
            b.retain("关于%s的记录第%d条：%s" % (t, i, filler), mtype="note")
        for bt in (40, 80, 160, 320):
            _sel, cost = b.recall("记录", k=20, budget_tokens=bt)
            sizes[bt] = cost["tokens_consumed"]
            check(cost["tokens_consumed"] <= bt or not cost["budget_respected"],
                  "budget=%d 正文不超预算" % bt,
                  (cost["tokens_consumed"], bt, cost["budget_respected"]))
        seq = [sizes[40], sizes[80], sizes[160], sizes[320]]
        check(all(seq[i] <= seq[i + 1] for i in range(len(seq) - 1)),
              "预算单调不减（参数真的参与取舍）", sizes)
        check(len(set(seq)) > 1, "至少一次严格增长", sizes)
        _sel, cost = b.recall("记录", k=20, budget_tokens=40)
        check(all(k in cost for k in ("budget_limit", "budget_used", "budget_respected",
                                      "capsule_count", "indexed_count",
                                      "precision_index", "index_tokens", "truncated")),
              "cost_report 回传完整预算与降级账目")
        check(bool(cost["capsule_count"] or cost["indexed_count"]),
              "放不下正文时降级为胶囊/精准索引（信息未消失）",
              (cost["capsule_count"], cost["indexed_count"]))

        # ---------------- 6. AIC 胶囊三条硬性质 ----------------
        print("\n### 6. AIC 胶囊：原子守恒 / 零幻觉 / 无损可逆")
        # 6a) 原子丰富的短文本：验证"原子一个不丢"（含 URL / 邮箱 / 金额 / 日期）
        LONG = ("用户的显卡是 RTX 4090，购于 2026-03-15，花了 15999 元。"
                "用户的年假是 15 天，必须在 2026-12-31 前用完。"
                "项目地址 https://github.com/FrankHu-HK/mnemosyne")
        sid = b.retain(LONG, mtype="semantic")
        stored = b.store.find_by_id(sid)["content"]
        seen_levels = set()
        for bt in (500, 200, 120, 80, 60, 45, 30, 20, 12, 8):
            cap = build_capsule({"id": sid, "content": stored, "type": "semantic",
                                 "created_at": "2026-09-15T10:00:00+08:00"}, bt)
            seen_levels.add(cap["level"])
            ok, probs = capsule_selfcheck(cap, stored)
            check(ok, "  budget=%-4d level=%-8s 自检通过" % (bt, cap["level"]), probs)
            check(parse_ref(cap["ref"]) is not None,
                  "  budget=%-4d 指针可解析" % bt, cap["ref"])
            check(cap["tokens"] <= max(bt, cap["min_tokens"]),
                  "  budget=%-4d 不超预算或仅因原子本身超预算" % bt,
                  (cap["tokens"], bt, cap["min_tokens"]))
            norm = "".join(cap["text"].casefold().split())
            check(all(x in norm for x in ("rtx4090", "15天", "2026-12-31", "15999元")),
                  "  budget=%-4d 关键原子在场" % bt, norm[:110])
            if cap["level"] != "full":
                check(cap["display_ref"] in cap["text"],
                      "  budget=%-4d 渲染里带指针（可请求展开）" % bt)

        # 6b) 分层阶梯：用"原子稀疏、句子多"的文本才能压出中间档
        #     （若用带 URL 的文本，原子列表本身就 ~80 token，`full` 与 `atoms`
        #      两档会直接贴在一起，中间档无从体现 —— 那是文本特性，不是缺陷）
        LADDER = ("用户在 2026 年负责量子计算平台的迁移工作。"
                  "该平台原先部署在自建机房，迁移后改由托管集群承载。"
                  "迁移过程中需要保证旧接口在过渡期内继续可用。"
                  "用户认为评估重点应放在迁移风险与回滚方案上。"
                  "相关讨论记录已经归档，供后续复盘参考。")
        lid = b.retain(LADDER, mtype="note")
        ltext = b.store.find_by_id(lid)["content"]
        ladder_levels = []
        for bt in (400, 200, 120, 90, 70, 50, 35, 25, 15, 8):
            c2 = build_capsule({"id": lid, "content": ltext, "type": "note",
                                "created_at": "2026-09-15T10:00:00+08:00"}, bt)
            ladder_levels.append(c2["level"])
            ok2, p2 = capsule_selfcheck(c2, ltext)
            check(ok2, "  阶梯 budget=%-4d level=%-8s 自检通过" % (bt, c2["level"]), p2)
        check(len(set(ladder_levels)) >= 3,
              "分层阶梯产生多档（降级机制真的在工作）", ladder_levels)

        # 6c) 可逆：逐字取回 + 哈希校验
        cap = b.capsule(sid, budget_tokens=40)
        ex = b.expand(cap["ref"])
        check(ex.get("content") == stored and ex.get("verified") is True,
              "expand 逐字取回原文且哈希校验通过",
              (len(ex.get("content") or ""), len(stored), ex.get("verified")))
        bad = b.expand(str(cap["ref"]).split("#")[0] + "#deadbeef@atoms")
        check(bad.get("verified") is False, "错指针被拒（verified=False）")
        # 6d) 确定性（利于上游前缀缓存命中）
        check(build_capsule({"id": sid, "content": stored, "type": "semantic",
                             "created_at": "2026-09-15T10:00:00+08:00"}, 40)["text"]
              == build_capsule({"id": sid, "content": stored, "type": "semantic",
                                "created_at": "2026-09-15T10:00:00+08:00"}, 40)["text"],
              "同输入产出逐字节相同（内容寻址 / 可缓存）")
        check(capsule_cache_stats()["hits"] > 0, "胶囊缓存有命中",
              capsule_cache_stats())

        # ---------------- 7. 写入侧去重：原子守恒硬约束 ----------------
        print("\n### 7. 写入侧去重（P2-3）：原子不同绝不合并")
        y1 = b.retain("用户的年假是 15 天", mtype="semantic")
        y2 = b.retain("用户的年假是 25 天", mtype="semantic")
        check(y1 != y2, "只差一个数字 → 绝不合并（否则改写事实）", (y1, y2))
        data = {r["id"]: r for r in b.store.all_records()}
        check("用户的年假是 15 天" in [data[i].get("content") for i in (y1,) if i in data]
              and "用户的年假是 25 天" in [data[i].get("content") for i in (y2,) if i in data],
              "两条事实都在库里")
        chain = [b.retain("用户的工位号是 A-1024", mtype="semantic") for _ in range(3)]
        data = {r["id"]: r for r in b.store.all_records()}
        check([data[i].get("version") for i in chain] == [1, 2, 3],
              "同文本重复写入 → 版本链 1/2/3（不是无界堆积）",
              [data[i].get("version") for i in chain])
        check(data[chain[0]].get("superseded_by") == chain[1]
              and data[chain[1]].get("superseded_by") == chain[2]
              and data[chain[2]].get("superseded_by") is None,
              "superseded_by 逐级串联，末端为 None")

        # ---------------- 8. 编码单一收口 ----------------
        print("\n### 8. 编码单一收口（P2-4）")
        before = encode_replaced_stats()["total"]
        s = sanitize_str("脏数据\udcac\udcea\udcad尾部", counter="selfcheck")
        check("?" in s and "\udcac" not in s,
              "孤立代理码点被替换为合法 UTF-8（有损，但不再抛异常）", repr(s))
        check(encode_replaced_stats()["total"] > before,
              "替换按来源计数（可从 recall_health 读出）", encode_replaced_stats())

        # ---------------- 9. 可观测性 ----------------
        print("\n### 9. 可观测性（没有指标 = 发现不了退化）")
        h = b.recall_health()
        check(all(k in h for k in ("recall_calls", "empty_rate", "floor_dropped_items",
                                   "avg_top1", "bands", "channels", "latency_ms",
                                   "vector_backend_ops", "encoding_replacements",
                                   "capsule_cache", "graph_edges", "thresholds")),
              "recall_health 含全部指标段", sorted(h.keys()))
        d = b.doctor()
        check(all(k in d for k in ("vector_ops", "vector_backend", "graph")),
              "doctor 暴露向量写删记账 / 插件健康 / 图边规模")
        check(all(k in d for k in ("status", "total_records", "disk_free_mb")),
              "doctor 旧键保持（向后兼容）")

        b.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n================ 结果汇总 ================")
    print("失败项：", FAIL if FAIL else "无")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
