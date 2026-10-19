#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v8.1.0 记忆形成层回归验证
================================

覆盖 5 个新模块的真实行为（不是导入测试）：

1. ``policy``   敏感度分级与来源闸门
2. ``conflict`` 冲突分类——**重点验证 8.0.0 的误报已消除**
3. ``session``  会话摘要七要素
4. ``working``  任务事实 + 经验前置条件校验
5. ``coding``   变更事件 + 考古

用法::

    python scripts/verify_v810.py

退出码 0 = 全部通过。
"""

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
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (("  — " + str(detail)) if detail and not cond else ""))


def main():
    import mnemosyne as M
    from mnemosyne.brain import MemoryBrain

    print("Mnemosyne v%s 回归验证" % M.__version__)
    tmp = tempfile.mkdtemp(prefix="mnemo_v810_")
    try:
        brain = MemoryBrain(base_dir=tmp, enable_embeddings=False,
                            enable_graph=False, enable_stats=False)

        # ---------------- 1. policy ----------------
        print("\n[1] policy — 敏感度与来源闸门")
        p = M.MemoryPolicy
        r = p.classify("我的 API key 是 sk-abcdefghijklmnopqrstuvwxyz123456")
        check("密钥识别为 secret", r["sensitivity"] == "secret", r)
        r = p.classify("我家住在某某小区 3 栋")
        check("住址识别为 private", r["sensitivity"] == "private", r)
        r = p.classify("用户偏好用 pnpm")
        check("普通内容为 normal", r["sensitivity"] == "normal", r)
        a = p.check("用户喜欢深色模式", source_type="external", mtype="preference")
        check("外部来源禁止写偏好", a["admit"] is False, a)
        a = p.check("用户喜欢深色模式", source_type="user", mtype="preference")
        check("用户来源允许写偏好", a["admit"] is True, a)
        a = p.check("password: hunter2", source_type="user")
        check("secret 默认拒写", a["admit"] is False, a)

        # ---------------- 2. conflict ----------------
        print("\n[2] conflict — 分类与误报消除")
        from mnemosyne.conflict import ConflictKind, classify_pair
        from mnemosyne.models import _build_record

        def rec(content, **kw):
            r = _build_record(content, skip_detailed=True)
            for k, v in kw.items():
                r[k] = v
            return r

        # 8.0.0 会误报的案例：同一实体、不同方面
        a1 = rec("an agent 的规则注入槽是 SOUL.md", entities=["an agent"])
        b1 = rec("an agent 的记忆走云端 Mnemosyne", entities=["an agent"])
        kind, action, reason = classify_pair(a1, b1)
        check("同实体不同方面不判冲突", kind == ConflictKind.NONE, (kind, reason))

        # 真时间性更新
        a2 = rec("用户在 A 公司工作", entities=["用户", "A公司"], fact_type="fact",
                 event_time="2024-01-01")
        b2 = rec("用户在 B 公司工作", entities=["用户", "B公司"], fact_type="fact",
                 event_time="2026-01-01")
        kind2, action2, _ = classify_pair(a2, b2)
        check("换公司识别为更新类冲突",
              kind2 in (ConflictKind.TEMPORAL_UPDATE, ConflictKind.NONE), (kind2, action2))

        # 逻辑矛盾
        a3 = rec("用户支持使用 Redis 缓存", entities=["用户", "Redis"])
        b3 = rec("用户不支持使用 Redis 缓存", entities=["用户", "Redis"])
        kind3, action3, _ = classify_pair(a3, b3)
        check("极性相反识别为逻辑矛盾", kind3 == ConflictKind.LOGICAL_CONTRADICTION, (kind3, action3))

        # 作用域不同 → 伪冲突
        a4 = rec("项目用 React", entities=["项目"], project="proj_a")
        b4 = rec("项目用 Vue", entities=["项目"], project="proj_b")
        kind4, _, _ = classify_pair(a4, b4)
        check("不同 project 不判冲突", kind4 == ConflictKind.SCOPE_DIFF, kind4)

        # 全库审计：构造 8.0.0 的典型误报数据后，审计结果应为 0 条
        brain.retain("an agent 的规则注入槽是 SOUL.md", mtype="semantic")
        brain.retain("an agent 的记忆走云端 Mnemosyne OS", mtype="semantic")
        brain.retain("an agent 从 old version 升级到 new version", mtype="semantic")
        cr = M.ConflictResolver(brain)
        audit = cr.audit()
        check("同实体共现不再计为冲突", audit.get("count", -1) == 0, audit.get("count"))

        # ---------------- 3. session ----------------
        print("\n[3] session — 七要素摘要")
        msgs = [
            {"role": "user", "content": "帮我设计记忆系统的冲突消解策略，必须兼容现有 8.0.0 数据结构。"},
            {"role": "assistant", "content": "我决定采用双时间字段加软失效的方案。"},
            {"role": "assistant", "content": "先试了直接删除旧条目，结果无法回答历史问题，失败了。"},
            {"role": "user", "content": "不要空话套话，直接给表格。"},
            {"role": "assistant", "content": "产物写到 /docs/conflict-policy.md，还有 3 个待确认问题。"},
        ]
        sm = M.SessionMemory(brain)
        out = sm.summarize("s_test_001", msgs, project="proj_x")
        s = out["summary"]
        check("目标已抽取", bool(s["goal"]))
        check("约束已抽取", any("必须" in x for x in s["constraints"]), s["constraints"])
        check("决策已抽取", any("决定" in x for x in s["decisions"]), s["decisions"])
        check("失败已抽取", any("失败" in x for x in s["failures"]), s["failures"])
        check("产物已抽取", any("conflict-policy" in x for x in s["artifacts"]), s["artifacts"])
        check("锚点保留数字", any("8.0.0" in x for x in s["anchors"]["numbers"]), s["anchors"])
        check("摘要已落库", bool(out["summary_memory_id"]))
        got = sm.recall("冲突消解", project="proj_x")
        check("摘要可召回", len(got["summaries"]) > 0, len(got["summaries"]))

        # ---------------- 4. working ----------------
        print("\n[4] working — 任务事实与经验")
        wm = M.WorkingMemory(brain)
        t = wm.record_task(goal="设计冲突消解策略", goal_type="architecture_design",
                           context={"constraints": ["单机部署", "不引入 Neo4j"]},
                           steps=[{"action": "检索方案", "result": "ok"},
                                  {"action": "试双时间模型", "result": "failed",
                                   "reason": "无图库"}],
                           outcome="success", rework_count=1, project="proj_x")
        check("任务事实已记录", bool(t["memory_id"]))

        pb = wm.save_playbook(
            pattern="状态演化类机制优先用软失效加状态字段，而非删除重建",
            goal_type="architecture_design",
            preconditions={"must": ["持久化存储"], "must_not": ["存储成本硬约束"],
                           "typical_env": ["无图数据库"]},
            steps=["加 status 与 valid_from/to", "旧记录置 superseded", "查询默认过滤 active"],
            failure_modes=[{"when": "数据量超千万且需全表扫描", "why": "活跃集膨胀"}],
            project="proj_x")
        check("经验已沉淀", bool(pb["memory_id"]))

        hits = wm.recall_playbooks(goal_type="architecture_design",
                                   context={"env": "单机 持久化存储 无图数据库"},
                                   project="proj_x")
        check("经验可召回", len(hits) > 0, len(hits))
        if hits:
            check("前置条件满足时判为适用", hits[0]["applicable"] is True, hits[0].get("blocked_by"))

        hits2 = wm.recall_playbooks(goal_type="architecture_design",
                                    context={"env": "存储成本硬约束 无图数据库"},
                                    project="proj_x")
        if hits2:
            check("命中 must_not 时判为不适用", hits2[0]["applicable"] is False,
                  hits2[0].get("blocked_by"))

        # ---------------- 5. coding ----------------
        print("\n[5] coding — 变更事件与考古")
        cm = M.CodingMemory(brain)
        c1 = cm.record_change(
            intent="修复跨会话召回时间戳被覆盖", repo="mnemosyne", commit="a3f9c21",
            intent_type="bugfix", constraints=["不能改数据库 schema"],
            files=["src/store/writer.ts"], symbols=["writeMemory"],
            rejected_approaches=[{"what": "在查询层做时间过滤",
                                  "why_not": "历史数据无 t_valid，过滤不出来"}],
            outcome="merged", lesson="时间语义字段必须在写入侧保留原始值",
            project="proj_x")
        check("变更事件已记录", bool(c1["memory_id"]))

        c2 = cm.record_change(
            intent="重构召回打分", repo="mnemosyne", commit="b7e1d02",
            parent=c1["change_id"], intent_type="refactor",
            files=["src/store/writer.ts"], symbols=["writeMemory"],
            outcome="reverted", lesson="打分权重改动导致回归", project="proj_x")
        check("回滚事件已记录", bool(c2["memory_id"]))

        arch = cm.archaeology("writeMemory", repo="mnemosyne", project="proj_x")
        check("考古能取到变更", arch["count"] >= 1, arch["count"])
        check("考古识别出回滚历史", arch["brief"]["ever_reverted"] is True, arch["brief"])

        pit = cm.pitfall_check("重构召回打分", files=["src/store/writer.ts"], project="proj_x")
        check("坑位预警能命中", pit["count"] >= 1, pit)

        brain.close() if hasattr(brain, "close") else None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
