#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图记忆 + 召回指标跨进程 功能实测
=====================================

针对两个「数据看不见」的缺陷：

1. **图记忆空转**
   - marker 写在 ``base_dir`` 根上 → 所有 namespace 共享，新建 namespace 永不回填
   - marker 是"一次性永久跳过" → 空库时写过一次（``scanned:0``），此后新写入永不补边

2. **``recall_health`` 跨进程恒 0**
   - 计数器在内存里，CLI / MCP / HTTP 每次调用都是新进程 → ``recall_calls`` 永远 0

用法::

    python scripts/verify_graph_health.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [OK]   " if cond else "  [FAIL] ") + name +
          (("  — " + str(detail)) if detail and not cond else ""))


TEXTS = [
    "用户的显卡是 RTX 4090",
    "用户在腾讯工作",
    "用户喜欢喝美式咖啡",
    "用户负责记忆系统的后端",
    "用户的管理者是张伟",
]


def main():
    from mnemosyne.brain import MemoryBrain

    print("图记忆 + 召回指标 功能实测")
    print("-" * 60)
    tmp = tempfile.mkdtemp(prefix="mnemo_gh_")
    try:
        # ================= 1. 图记忆：写入即建边 =================
        print("\n[1] 写入即建边（基础能力）")
        b = MemoryBrain(base_dir=tmp, enable_embeddings=False, namespace="ns_a")
        ids = [b.retain(t, mtype="semantic") for t in TEXTS]
        edges = list(b.graph_store.iter_edges())
        print("    写入 %d 条，产出 %d 条边" % (len(ids), len(edges)))
        for e in edges:
            print("      %s --%s--> %s" % (e.get("from"), e.get("relation"), e.get("to")))
        check("5 条陈述产出 ≥4 条边", len(edges) >= 4, len(edges))
        check("每条边带 memory_id", all(e.get("memory_id") for e in edges))
        gq = b.graph_query("用户")
        check("graph_query('用户') 有节点",
              len((gq or {}).get("nodes") or []) >= 3, (gq or {}).get("nodes"))
        b.close()

        # ================= 2. marker 按 namespace 隔离 =================
        print("\n[2] marker 按 namespace 隔离（新建库必须能回填）")
        # 模拟历史遗留：在 base_dir 根上留一个旧的共享 marker
        legacy = os.path.join(tmp, ".graph_edges_backfilled_v702b")
        with open(legacy, "w", encoding="utf-8") as f:
            json.dump({"at": "2026-09-25T14:12:27+08:00", "edges": 0,
                       "scanned": 0, "covered": 0, "purged_stale": 0}, f)

        b2 = MemoryBrain(base_dir=tmp, enable_embeddings=False, namespace="ns_b")
        # 先写一条**打不出三元组**的记忆，模拟"写入侧抽取失败"
        b2.retain("native path works", mtype="semantic")
        # 再手工把图清空，模拟存量数据没有边
        try:
            b2.store.purge_edges_without_memory_id()
            _conn = b2.store._get_conn()
            _conn.execute("DELETE FROM edges")
            _conn.commit()
        except Exception as exc:
            print("    清理边失败:", exc)
        # 现在补几条有关系的
        for t in TEXTS:
            b2.retain(t, mtype="semantic")
        # 直接删掉刚建的边，制造"存量无图"
        try:
            _conn = b2.store._get_conn()
            _conn.execute("DELETE FROM edges")
            _conn.commit()
            if os.path.exists(os.path.join(tmp, "graph.jsonl")):
                open(os.path.join(tmp, "graph.jsonl"), "w").close()
        except Exception:
            pass
        b2._graph_backfilled = False
        res = b2._backfill_graph_edges(force=False)
        print("    ns_b 回填结果: %s" % res)
        check("ns_b 未被 base_dir 旧 marker 挡住（真的执行了）",
              res.get("skipped") != "marker_present", res)
        check("ns_b 补出了边", int(res.get("edges_written") or 0) >= 4, res)
        ns_marker = os.path.join(tmp, ".graph_edges_backfilled_v702b.ns_b")
        check("marker 按 namespace 落盘", os.path.exists(ns_marker), ns_marker)

        # ================= 3. marker 会随记录增长重新触发 =================
        print("\n[3] marker 随记录增长重新触发（不是一次性永久跳过）")
        b2._graph_backfilled = False
        r1 = b2._backfill_graph_edges(force=False)
        print("    记录未增长时: %s" % r1)
        check("记录未显著增长时跳过（幂等，不重复扫）",
              r1.get("skipped") == "marker_fresh", r1)
        # 灌入 30 条新记忆，超过 max(20, 30%) 阈值
        for i in range(30):
            b2.retain("用户的同事%d负责测试模块%d" % (i, i), mtype="semantic")
        b2._graph_backfilled = False
        r2 = b2._backfill_graph_edges(force=False)
        print("    记录增长 30 条后: %s" % r2)
        check("记录显著增长后重新回填（不再永久跳过）",
              r2.get("skipped") != "marker_fresh", r2)
        check("重跑确实扫到了全部记录",
              int(r2.get("scanned") or 0) >= 36, r2)

        # 真正验证"补边能力"：把边清空，制造存量无图的局面
        _conn = b2.store._get_conn()
        _conn.execute("DELETE FROM edges")
        _conn.commit()
        gpath = os.path.join(tmp, "graph.jsonl")
        if os.path.exists(gpath):
            open(gpath, "w").close()
        b2._graph_backfilled = False
        r3 = b2._backfill_graph_edges(force=True)
        print("    清空边后强制回填: %s" % r3)
        check("存量无图时回填能补出边",
              int(r3.get("edges_written") or 0) >= 20, r3)
        check("回填后 graph_query 有节点",
              len((b2.graph_query("用户") or {}).get("nodes") or []) >= 10,
              len((b2.graph_query("用户") or {}).get("nodes") or []))
        b2.close()

        # ================= 4. recall_health 跨进程可见 =================
        print("\n[4] recall_health 跨进程可见")
        b3 = MemoryBrain(base_dir=tmp, enable_embeddings=False, namespace="ns_c")
        for t in TEXTS:
            b3.retain(t, mtype="semantic")
        before = b3.recall_health()
        print("    进程内首次: recall_calls=%s persisted=%s"
              % (before.get("recall_calls"), before.get("persisted")))
        for q in ("显卡", "腾讯", "咖啡", "后端", "管理者"):
            b3.recall(q, k=5)
        mid = b3.recall_health()
        print("    召回 5 次后: recall_calls=%s avg_top1=%s"
              % (mid.get("recall_calls"), mid.get("avg_top1")))
        check("进程内计数正确（5 次）", mid.get("recall_calls") == 5,
              mid.get("recall_calls"))
        b3.close()

        hpath = os.path.join(tmp, "metrics", "recall_health.ns_c.json")
        check("指标已落盘", os.path.exists(hpath), hpath)
        disk = json.load(open(hpath, encoding="utf-8")) if os.path.exists(hpath) else {}
        check("落盘内容含 5 次调用", disk.get("n") == 5, disk.get("n"))

        # 关键：**新进程** 读到的不是 0
        b4 = MemoryBrain(base_dir=tmp, enable_embeddings=False, namespace="ns_c")
        after = b4.recall_health()
        print("    新实例读取: recall_calls=%s persisted=%s"
              % (after.get("recall_calls"), after.get("persisted")))
        check("新实例能读到历史计数（跨进程可见）",
              after.get("recall_calls") == 5, after.get("recall_calls"))
        check("persisted 标记为 True", after.get("persisted") is True, after)
        check("avg_top1 也被保留", after.get("avg_top1") is not None, after.get("avg_top1"))

        # 继续累积（不是覆盖）
        for q in ("显卡", "腾讯"):
            b4.recall(q, k=5)
        b4.close()
        b5 = MemoryBrain(base_dir=tmp, enable_embeddings=False, namespace="ns_c")
        check("计数是累积而非覆盖（5+2=7）",
              b5.recall_health().get("recall_calls") == 7,
              b5.recall_health().get("recall_calls"))
        # namespace 隔离
        b6 = MemoryBrain(base_dir=tmp, enable_embeddings=False, namespace="ns_d")
        check("不同 namespace 指标互不污染",
              b6.recall_health().get("recall_calls") == 0,
              b6.recall_health().get("recall_calls"))
        b5.close()
        b6.close()

        # ================= 5. 真·独立进程 =================
        print("\n[5] 真·独立子进程（模拟 CLI 形态）")
        code = (
            "import sys; sys.path.insert(0, %r);"
            "from mnemosyne.brain import MemoryBrain;"
            "b = MemoryBrain(base_dir=%r, enable_embeddings=False, namespace='ns_c');"
            "b.recall('显卡', k=3);"
            "print(b.recall_health()['recall_calls']);"
            "b.close()" % (_ROOT, tmp)
        )
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, timeout=120)
        val = (out.stdout or "").strip().splitlines()[-1:] or [""]
        print("    子进程读到 recall_calls=%s" % val[0])
        check("独立子进程读到累计值（8）", val[0] == "8",
              (val[0], out.stderr[-300:]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    print("通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项: " + ", ".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
