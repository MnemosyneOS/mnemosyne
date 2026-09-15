#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""召回质量端到端对照（7.0.2 起随包交付）。

与另外两套验收的分工
--------------------
| 脚本 | 需要外部服务 | 验什么 |
|---|---|---|
| `verify_precision_recall.py` | 无 | 机制性硬不变量（原子守恒/可逆/预算/下限/隔离/去重） |
| `verify_memory_lifecycle.py` | Qdrant + 嵌入 | 工具面、库内落盘、生命周期闭环、胶囊端到端 |
| **本脚本** | Qdrant + 嵌入 | **召回准不准**（precision@1 + 抗退化 + 假阳性） |

**为什么必须有这一套**：7.0.2 的 D8/D8b/D8c/D9/D10 五个机制缺陷
**一个都不是读代码读出来的，全部是本脚本检出的** —— 只有"问题→答案"的标注集
配上**同主题干扰项**，才能照出排序机制的错。单看"召回非空、分数不为 0"永远
发现不了"正确答案排在第三位"。

测四件事（都是用户能感知的）
----------------------------
A. **precision@1**：冷启动 / 预热后 是否都 100%（含同主题干扰项的混淆）
B. **抗退化**：先问 4 个（再 28 个）无关问题，再问原题 —— 正确答案是否仍稳在 rank1
   （7.0.1 的 `access_boost` 正反馈正是在这里把答案从 rank1 挤到 rank3）
C. **假阳性**：无关查询是否被相关性下限清空（旧行为是无论如何填满 k 条）
D. **AIC**：预算 30 token 时，答案的关键取值是否仍可见（正文或精准索引）

用法
----
    <python> scripts/verify_recall_quality.py [--brain-dir DIR] [--namespace NS]

环境（与部署逐键对齐，缺一个就会得到"召回没区分度"的假结论）：
    MNEMOSYNE_PLUGINS=qdrant_backend
    MNEMOSYNE_QDRANT_URL / MNEMOSYNE_EMBED_URL / MNEMOSYNE_EMBED_MODEL / MNEMOSYNE_EMBED_DIM

跑完请删除该命名空间目录与 Qdrant 集合 `mnemosyne_<namespace>`。
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import warnings

warnings.filterwarnings("ignore")

_SRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

os.environ.setdefault("MNEMOSYNE_PLUGINS", "qdrant_backend")
os.environ.setdefault("MNEMOSYNE_QDRANT_URL", "http://127.0.0.1:6333")
os.environ.setdefault("MNEMOSYNE_EMBED_URL", "http://127.0.0.1:5080/v1/embeddings")
os.environ.setdefault("MNEMOSYNE_EMBED_MODEL", "bge-m3")
os.environ.setdefault("MNEMOSYNE_EMBED_DIM", "1024")

FAIL = []


def check(cond, label, detail=None):
    print(("  [PASS] " if cond else "  [FAIL] ") + label
          + ("" if detail is None else "   | " + str(detail)[:220]))
    if not cond:
        FAIL.append(label)


# 标注集：(答案, 关键原子, 自然提问, 同主题干扰项…)
CASES = [
    ("用户的生产库向量端口是 6333", "6333", "向量库监听在哪个端口",
     ["用户的备用库端口是 6334", "用户的监控端口是 9100"]),
    ("用户的嵌入模型是 bge-m3", "bge-m3", "用的哪个嵌入模型",
     ["用户的备用嵌入模型是 text-embedding-3-large", "用户的语音模型是 whisper"]),
    ("用户的嵌入向量维度是 1024", "1024", "嵌入向量是多少维",
     ["用户的图像向量维度是 768", "用户的稀疏向量维度是 30522"]),
    ("用户的年假是 15 天，必须在 2026-12-31 前用完", "15天", "我的年假有几天",
     ["用户的病假是 10 天", "用户的调休是 3 天"]),
    ("用户的显卡是 RTX 4090，购于 2026-03-15", "rtx4090", "我用的什么显卡",
     ["用户的备用显卡是 RTX 3060", "用户的显示器是 U2723QE"]),
    ("用户在上海工作，通勤单程 45 分钟", "45分钟", "我通勤要多久",
     ["用户在杭州出差每周 2 次", "用户的工位在 3 楼"]),
    ("用户的 API 配额上限是 15000 元每月", "15000元", "我的 API 预算是多少",
     ["用户的存储配额是 500 GB", "用户的上月账单是 8231 元"]),
    ("用户的部署区域是 cn-hangzhou，延迟 12ms", "cn-hangzhou", "部署在哪个区域",
     ["用户的备份区域是 cn-beijing", "用户的 CDN 节点数是 24"]),
]
UNRELATED = ["舒芙蕾怎么做才不塌", "法国大革命是哪一年", "汽车轮胎多久换一次",
             "如何练习自由泳打腿"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brain-dir", default=None, help="缺省用临时目录（跑完自删）")
    ap.add_argument("--namespace", default="recall_quality")
    ap.add_argument("--k", type=int, default=5)
    a = ap.parse_args()

    from mnemosyne import MemoryBrain

    tmp = a.brain_dir or tempfile.mkdtemp(prefix="mnemo_quality_")
    own_tmp = a.brain_dir is None
    try:
        b = MemoryBrain(tmp, namespace=a.namespace, actor="quality")
        b.ensure_init()
        print("向量后端: %s | has_search: %s"
              % (type(b.embed_engine).__name__, hasattr(b.embed_engine, "search")))
        if not hasattr(b.embed_engine, "search"):
            print("  [WARN] 未加载语义后端 —— 所有分数会塌到常数底，结论无效。"
                  "请确认 MNEMOSYNE_PLUGINS 与 Qdrant/嵌入服务。")

        ids = {}
        t0 = time.time()
        for ans, _atom, _q, distr in CASES:
            ids[ans] = b.retain(ans, mtype="semantic", confidence=0.95)
            for d in distr:
                b.retain(d, mtype="semantic", confidence=0.9)
        print("写入 %d 条（含同主题干扰项），耗时 %.1fs"
              % (len(b.store.all_records()), time.time() - t0))

        def prec_at_1(tag):
            hit, miss = 0, []
            for ans, _atom, q, _d in CASES:
                res = b.recall(q, k=a.k)
                if res and isinstance(res[0][1], dict) \
                        and res[0][1].get("id") == ids[ans]:
                    hit += 1
                else:
                    got = (res[0][1].get("content", "")[:26] if res else "(空)")
                    miss.append("%s -> %s" % (q, got))
            print("    %s precision@1 = %d/%d" % (tag, hit, len(CASES)))
            return hit, miss

        n = len(CASES)
        print("\n### A. precision@1")
        h1, m1 = prec_at_1("冷启动")
        check(h1 == n, "冷启动 precision@1 = %d/%d" % (n, n), m1)
        h2, m2 = prec_at_1("预热后")
        check(h2 == n,
              "预热后 precision@1 = %d/%d（7.0.1 同类实验此处退到 6/8）" % (n, n), m2)

        print("\n### B. 抗退化：先问无关问题，再问原题")
        for q in UNRELATED:
            b.recall(q, k=a.k)
        h3, m3 = prec_at_1("无关问题×4 之后")
        check(h3 == n, "无关问题×4 后 precision@1 仍 = %d/%d" % (n, n), m3)
        for _ in range(6):
            for q in UNRELATED:
                b.recall(q, k=a.k)
        h4, m4 = prec_at_1("无关问题×28 之后")
        check(h4 == n, "持续无关提问后 precision@1 仍 = %d/%d" % (n, n), m4)
        recs = {r["id"]: r for r in b.store.all_records()}
        ac = sorted((recs[i].get("access_count") or 0) for i in ids.values())
        check(all(x <= 5 for x in ac),
              "access_count 全部 ≤ 5（计数封顶生效，不构成正反馈）", ac)

        print("\n### C. 假阳性：无关查询应被下限清空")
        filled = [len(b.recall(q, k=a.k)) for q in UNRELATED]
        check(all(x <= 1 for x in filled),
              "全部无关查询返回 ≤1 条（7.0.1 行为是恒 %d 条）" % a.k, filled)

        print("\n### D. AIC：30 token 预算下关键取值仍可见")
        ok_cnt, bad = 0, []
        for ans, atom, q, _d in CASES:
            sel, cost = b.recall(q, k=a.k, budget_tokens=30)
            blob = "".join("".join((r[1].get("content") or "").split()) for r in sel)
            idx = "".join("".join(json.dumps(cost.get("precision_index") or [],
                                            ensure_ascii=False).split()))
            if atom.casefold() in blob.casefold() or atom.casefold() in idx.casefold():
                ok_cnt += 1
            else:
                bad.append((q, atom, blob[:40]))
            if cost["tokens_consumed"] > 30 and cost.get("budget_respected"):
                bad.append(("超预算", q, cost["tokens_consumed"]))
        check(ok_cnt == n, "%d/%d 问句在 30 token 内仍能看到关键取值" % (n, n), bad)

        print("\n### E. 体检")
        h = b.recall_health()
        print("    recall_calls=%s 空结果率=%s 平均top1=%s 延迟p50=%sms"
              % (h["recall_calls"], h["empty_rate"], h["avg_top1"],
                 h["latency_ms"]["p50"]))
        d = b.doctor()
        print("    qdrant=%s points=%s | graph.edges=%s | vector_ops=%s"
              % (d["vector_backend"].get("qdrant"), d["vector_backend"].get("points"),
                 d["graph"].get("edges"), d["vector_ops"]))
        check((h["latency_ms"]["p50"] or 0) < 1000,
              "延迟 p50 < 1000ms", h["latency_ms"])
        check(h["capsule_cache"]["hits"] > 0,
              "胶囊缓存命中率 > 0（内容寻址生效）", h["capsule_cache"])
        check(d["vector_backend"].get("qdrant") == "up"
              and (d["vector_backend"].get("points") or 0) > 0,
              "doctor 报 qdrant=up 且点数 > 0",
              (d["vector_backend"].get("qdrant"), d["vector_backend"].get("points")))
        b.close()
    finally:
        if own_tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    print("\n最终：precision@1 = %d/%d（冷） %d/%d（预热） %d/%d（无关×4 后） %d/%d（无关×28 后）"
          % (h1, n, h2, n, h3, n, h4, n))
    print("失败项：", FAIL if FAIL else "无")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
