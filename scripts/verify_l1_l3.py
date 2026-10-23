#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L1 / L3 会话摘要 + 指针链 功能实测
=====================================

三层摘要：

    L0 原始消息 → L1 轮级(3-5轮) → L2 会话级 → L3 主题级

验收点：

1. ``turn_summary`` 每 4 轮滚一条，200 字内，**只用原文句子**（抽取式）
2. ``turn_record`` 的 ``meta.pointer`` 含 turn_start/turn_end/message_ids
3. ``topic_summary`` 跨会话合并：goal 取最新、decisions 累积、pending 只留最新
4. ``trace_chain`` 从 L3 沿 children 回溯到 L1，``reaches_l0=True``

用法::

    python scripts/verify_l1_l3.py
"""

import json
import os
import shutil
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


# 一段 12 轮的真实形状对话（含决策、未决、失败、约束、路径锚点）
DIALOGUE = [
    {"role": "user", "content": "帮我看看记忆系统的召回为什么不准。"},
    {"role": "assistant", "content": "先跑一次检索诊断，看是向量侧还是词法侧的问题。"},
    {"role": "user", "content": "我决定用 RRF 融合，因为分数尺度不可比。"},
    {"role": "assistant", "content": "好的，RRF 的 k 值取 60，这是常用默认值。"},
    {"role": "user", "content": "向量库必须用 sqlite-vec，不能引入外部服务。"},
    {"role": "assistant", "content": "明白，那就走 sqlite-vec 扩展，不引入 qdrant。"},
    {"role": "user", "content": "上次试过用余弦阈值过滤，结果失败了，召回率掉到 0.3。"},
    {"role": "assistant", "content": "那这次改用 RRF 后重排，不再做硬阈值过滤。"},
    {"role": "user", "content": "还有一件事没定：时间衰减的半衰期取多少？"},
    {"role": "assistant", "content": "principle建议 90 天，可以先用 90 天，后续再调。"},
    {"role": "user", "content": "配置文件在 C:/work/memory/config.yaml，改这里。"},
    {"role": "assistant", "content": "已记录路径，后续都改这个文件。"},
]


def main():
    from mnemosyne import session as S
    from mnemosyne.brain import MemoryBrain

    print("L1/L3 摘要与指针链 功能实测")
    print("-" * 60)

    tmp = tempfile.mkdtemp(prefix="mnemo_l13_")
    try:
        brain = MemoryBrain(base_dir=tmp, enable_embeddings=False)

        # ---------- 1. L1 轮级摘要 ----------
        print("\n[1] L1 轮级摘要（每 %d 轮一条，≤%d 字）" % (S.TURN_WINDOW, S.TURN_SUMMARY_MAX))
        l1_records = []
        for start in range(0, len(DIALOGUE), S.TURN_WINDOW):
            ts = S.turn_summary(DIALOGUE, start, start + S.TURN_WINDOW)
            l1_records.append(ts)
            print("    L1[%d-%d] %s" % (ts["turn_start"], ts["turn_end"], ts["text"][:90]))

        check("生成了 3 条 L1（12 轮 / 4）", len(l1_records) == 3, len(l1_records))
        check("每条 L1 文本 ≤ 200 字",
              all(len(r["text"]) <= S.TURN_SUMMARY_MAX for r in l1_records),
              [len(r["text"]) for r in l1_records])
        check("每条 L1 非空", all(r["text"] for r in l1_records))
        # 抽取式：文本片段必须能在原文里找到（去掉「问：」「定：」等前缀后）
        corpus = "".join(m["content"] for m in DIALOGUE)
        frag_ok = True
        for r in l1_records:
            for piece in r["text"].replace("…", "").split("｜"):
                body = piece.split("：", 1)[-1]
                for sub in body.split("；"):
                    sub = sub.strip()
                    if len(sub) >= 6 and sub not in corpus:
                        frag_ok = False
                        print("      非原文片段: %r" % sub)
        check("L1 只用原文句子（无改写）", frag_ok)
        # 锚点：路径必须被原样钉住
        all_anchors = json.dumps([r.get("anchors") for r in l1_records], ensure_ascii=False)
        check("路径锚点被原样保留", "C:/work/memory/config.yaml" in all_anchors
              or "config.yaml" in all_anchors, all_anchors[:200])

        # ---------- 2. L1 落库 + pointer ----------
        print("\n[2] L1 落库与 pointer 结构")
        l1_ids = []
        for ts in l1_records:
            rec = S.turn_record(ts, session_id="sess-A", project="mnemosyne")
            mid = brain.retain(**rec)
            l1_ids.append(mid)
        check("3 条 L1 已落库", all(l1_ids) and len(set(l1_ids)) == 3, l1_ids)

        got = brain.store.find_by_id(l1_ids[0])
        ptr = ((got or {}).get("meta") or {}).get("pointer") or {}
        check("meta.pointer.level == L1", ptr.get("level") == "L1", ptr)
        check("pointer 含 turn_start/turn_end",
              ptr.get("turn_start") == 0 and ptr.get("turn_end") == 4, ptr)
        check("pointer 含 message_ids（4 条）",
              len(ptr.get("message_ids") or []) == 4, ptr.get("message_ids"))
        check("pointer 含 session_id", ptr.get("session_id") == "sess-A", ptr)

        # ---------- 3. L2 会话摘要（复用既有 extract/merge）----------
        print("\n[3] L2 会话摘要（既有能力，验证与 L1 不冲突）")
        s2 = S.extract(DIALOGUE)
        l2_rec = S.summary_record(s2, session_id="sess-A", project="mnemosyne")
        l2_id = brain.retain(**l2_rec)
        check("L2 已落库", bool(l2_id), l2_id)
        print("    L2 goal: %s" % (s2.get("goal") or "")[:80])
        print("    L2 decisions: %s" % s2.get("decisions"))
        check("L2 抽到决策（RRF）",
              any("RRF" in str(x) for x in (s2.get("decisions") or [])), s2.get("decisions"))
        check("L2 抽到失败（余弦阈值）",
              any("余弦" in str(x) or "阈值" in str(x) for x in (s2.get("failures") or [])),
              s2.get("failures"))
        check("L2 抽到未决（半衰期）",
              any("半衰期" in str(x) for x in (s2.get("pending") or [])), s2.get("pending"))

        # ---------- 4. L3 主题摘要 ----------
        print("\n[4] L3 主题摘要（跨会话合并）")
        s2b = {
            "session_id": "sess-B",
            "created_at": "2026-10-20T10:00:00Z",
            "factors": {
                "goal": "把召回准确率提到 0.85 以上",
                "decisions": ["采用 sqlite-vec 做向量后端"],   # 与 sess-A 不同
                "pending": ["灰度放量到 10%"],                 # 只应保留这一条
                "failures": [],
                "constraints": [],
                "artifacts": [],
                "preferences": [],
            },
        }
        s2a = {
            "session_id": "sess-A",
            "created_at": "2026-10-19T10:00:00Z",
            "factors": s2,
        }
        s3 = S.topic_summary([s2a, s2b])
        print("    L3 goal: %s" % s3["goal"])
        print("    L3 decisions: %s" % s3["decisions"])
        print("    L3 pending: %s" % s3["pending"])
        check("L3 goal 取最新会话（sess-B）",
              s3["goal"] == "把召回准确率提到 0.85 以上", s3["goal"])
        check("L3 decisions 累积（含两会话的决策）",
              any("RRF" in d for d in s3["decisions"]) and
              any("sqlite-vec" in d for d in s3["decisions"]), s3["decisions"])
        check("L3 pending 只留最新（灰度放量，不含半衰期）",
              any("灰度" in p for p in s3["pending"]) and
              not any("半衰期" in p for p in s3["pending"]), s3["pending"])
        check("L3 session_count == 2", s3["session_count"] == 2, s3["session_count"])

        l3_rec = S.topic_record(s3, "mnemosyne", children=[l2_id])
        l3_id = brain.retain(**l3_rec)
        got3 = brain.store.find_by_id(l3_id)
        ptr3 = ((got3 or {}).get("meta") or {}).get("pointer") or {}
        check("L3 已落库", bool(l3_id), l3_id)
        check("L3 pointer.children 指向 L2", l2_id in (ptr3.get("children") or []), ptr3)
        check("L3 pointer.level == L3", ptr3.get("level") == "L3", ptr3)

        # ---------- 5. 指针链回溯 L3 → L2 → L1 → L0 ----------
        print("\n[5] trace_chain：L3 → L2 → L1 → L0 回溯")
        # 补一条 L2 → L1 的指针，让链条完整
        brain.store.update_by_id(l2_id, {
            **((got or {}).get("meta") or {}),
            "kind": "session_summary",
            "level": "L2",
            "session_id": "sess-A",
            "pointer": {"level": "L2", "session_id": "sess-A",
                        "children": l1_ids, "message_ids": []},
        })
        res = S.trace_chain(brain, l3_id, max_depth=3)
        nodes = res["chain"]
        print("    链长: %d  层级: %s" % (res["depth"], [c["level"] for c in nodes]))
        for c in nodes:
            print("      - %s %s turn=%s" % (c["level"], c["id"][:12], c["turn_range"]))
        levels = [c["level"] for c in nodes]
        check("链上出现 L3", "L3" in levels, levels)
        check("链上出现 L2", "L2" in levels, levels)
        check("链上出现 L1", "L1" in levels, levels)
        check("reaches_l0 == True（能定位到原始轮次）", res["reaches_l0"] is True, res)
        check("L1 节点带 turn_range", any(c["turn_range"] for c in nodes),
              [(c["level"], c["turn_range"]) for c in nodes])

        # ---------- 6. 摘要可被召回 ----------
        print("\n[6] 摘要参与召回（不是只写不读）")
        # recall 返回 [(score, record), ...]
        hits = brain.recall("RRF 融合 半衰期", k=8) or []
        recs = [h[1] for h in hits if isinstance(h, (list, tuple)) and len(h) > 1]
        hit_ids = [r.get("id") for r in recs]
        print("    召回命中 %d 条: %s" % (len(hit_ids), [str(i)[:10] for i in hit_ids]))
        for r in recs[:4]:
            print("      %.3f  %s" % (hits[recs.index(r)][0], str(r.get("content"))[:70]))
        check("摘要能被召回（命中 L1/L2/L3 任一层）",
              any(i in hit_ids for i in ([l1_ids[0], l1_ids[1], l2_id, l3_id])),
              hit_ids)
        check("召回内容含关键实体（RRF 或 半衰期）",
              any("RRF" in str(r.get("content")) or "半衰期" in str(r.get("content"))
                  for r in recs),
              [str(r.get("content"))[:40] for r in recs])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    print("通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项: " + ", ".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
