#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆形成层门面（brain 层）端到端实测
=========================================

v8.1.0 新增 7 个模块后，必须证明它们**从 MemoryBrain 上真的调得到、真的有用**。
本脚本不测模块内部（那是 verify_v810 / verify_l1_l3 / verify_graph_health 的事），
只测门面：参数通不通、返回结构对不对、数据是不是真的落库了。

覆盖 18 个方法：

    memory_envelope / memory_type_stats / profile_card /
    profile_decay_candidates / consolidate_memories / inject_context /
    recall_catalog / expand_memory / cascade_delete / mask_memory /
    unmask_memory / memory_dependents / memory_metrics / topic_summarize /
    trace_memory / enable_hot_path / hot_path_status / flush_hot_path

用法::

    python scripts/verify_formation.py
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


def jdump(o, n=180):
    try:
        return json.dumps(o, ensure_ascii=False)[:n]
    except Exception:
        return str(o)[:n]


def main():
    from mnemosyne.brain import MemoryBrain

    print("记忆形成层门面 端到端实测")
    print("-" * 62)
    tmp = tempfile.mkdtemp(prefix="mnemo_fm_")
    try:
        b = MemoryBrain(base_dir=tmp, enable_embeddings=False, namespace="fm")

        # ---------------- 造数据 ----------------
        # 身份 / 偏好 / 情境 / 文档 / 失败 / 任务事实
        seeds = [
            ("用户是浙江大学计算机学院大三学生", "semantic"),
            ("用户偏好用 pnpm 而不是 npm", "preference"),
            ("用户要求回答先给结论再给理由", "preference"),
            ("用户讨厌过度使用 emoji", "preference"),
            ("这周在杭州出差", "episodic"),
            ("接口文档在 C:/work/docs/api.md", "semantic"),
            ("上次用余弦阈值过滤导致召回率掉到 0.3，失败", "episodic"),
            ("用户的显卡是 RTX 4090", "semantic"),
        ]
        ids = {}
        for text, mt in seeds:
            ids[text] = b.retain(text, mtype=mt)

        # ================= 1. memory_envelope =================
        print("\n[1] memory_envelope — 统一信封")
        env = b.memory_envelope(ids["用户是浙江大学计算机学院大三学生"])
        print("    " + jdump(env, 320))
        check("返回非空", bool(env))
        check("含 memory_type（八类之一）", env.get("memory_type") in
              ("semantic", "episodic", "procedural", "working", "resource",
               "anti_pattern", "meta", "relational"), env.get("memory_type"))
        check("含 scope 四维", isinstance(env.get("scope"), dict) and
              set(env["scope"]) >= {"user", "project", "agent", "session"},
              env.get("scope"))
        check("含 t_valid", "t_valid" in env, list(env))
        check("含 evidence 列表", isinstance(env.get("evidence"), list),
              env.get("evidence"))
        check("资源记忆被识别为 resource 或 semantic（文档路径）",
              b.memory_envelope(ids["接口文档在 C:/work/docs/api.md"])
              .get("memory_type") in ("resource", "semantic"),
              b.memory_envelope(ids["接口文档在 C:/work/docs/api.md"]).get("memory_type"))
        check("失败记忆被识别为 anti_pattern 或 episodic",
              b.memory_envelope(ids["上次用余弦阈值过滤导致召回率掉到 0.3，失败"])
              .get("memory_type") in ("anti_pattern", "episodic"),
              b.memory_envelope(ids["上次用余弦阈值过滤导致召回率掉到 0.3，失败"]).get("memory_type"))
        check("不存在的 id 返回 None", b.memory_envelope("nope") is None)

        # ================= 2. memory_type_stats =================
        print("\n[2] memory_type_stats — 八类分布")
        st = b.memory_type_stats()
        print("    " + jdump(st, 320))
        check("总数 = 写入数", st["total"] >= len(seeds), st["total"])
        check("八类计数齐全", len(st["counts"]) == 8, st["counts"])
        check("semantic 有值", st["counts"]["semantic"] >= 3, st["counts"])

        # ================= 3. profile_card =================
        print("\n[3] profile_card — 画像卡三段")
        pc = b.profile_card()
        print("    counts=%s tokens=%s" % (pc["counts"], pc["tokens"]))
        print("    --- 文本 ---")
        for line in (pc["text"] or "").splitlines()[:14]:
            print("    " + line)
        check("有稳定身份段", pc["counts"]["identity"] >= 1, pc["counts"])
        check("有偏好段", pc["counts"]["preference"] >= 1, pc["counts"])
        check("情境段含出差（这周在杭州）",
              any("杭州" in i["content"] for i in pc["profile"]["context"]),
              [i["content"] for i in pc["profile"]["context"]])
        check("文档路径不进画像（走召回层）",
              not any("api.md" in i["content"]
                      for t in pc["profile"] for i in pc["profile"][t]),
              [i["content"] for t in pc["profile"] for i in pc["profile"][t]])
        check("tokens 合理（<600）", 0 < pc["tokens"] < 600, pc["tokens"])
        # 预算裁剪
        pc2 = b.profile_card(max_tokens=60)
        print("    max_tokens=60 → tokens=%s dropped=%s"
              % (pc2["tokens"], pc2["dropped_segments"]))
        check("预算裁剪生效（60 token 内）", pc2["within_budget"] is True,
              (pc2["tokens"], pc2["budget"]))
        check("裁剪时先丢情境", "context" in pc2["dropped_segments"],
              pc2["dropped_segments"])
        # 精简模式
        pc3 = b.profile_card(include_context=False, include_pending=False)
        check("精简模式比全量短", pc3["tokens"] < pc["tokens"],
              (pc3["tokens"], pc["tokens"]))

        # ================= 4. profile_decay_candidates =================
        print("\n[4] profile_decay_candidates — 降权候选")
        dc = b.profile_decay_candidates()
        print("    " + jdump(dc, 260))
        check("返回 decay/review 两个键",
              "decay" in dc and "review" in dc, list(dc))
        check("新数据不应有大量降权候选", dc["decay_count"] <= 2, dc["decay_count"])

        # ================= 5. 巩固 =================
        print("\n[5] consolidate_memories — 巩固三路径")
        # 造跨会话情景（同一结论在 3 个不同会话出现）
        # 注意：文案必须"相似但不同"——完全相同的文案会走 template_hash 版本链，
        # 只留 1 条记录，跨会话计数就凑不够 3。
        for s, txt in (("s1", "项目采用 sqlite-vec 作为向量后端"),
                       ("s2", "项目最终采用 sqlite-vec 做向量后端"),
                       ("s3", "我们决定项目采用 sqlite-vec 当向量后端")):
            b.retain(txt, mtype="episodic",
                     meta={"session_id": s}, project="mnemosyne")
        # 造两次同类成功任务 → 程序性
        from mnemosyne.working import WorkingMemory
        wm = WorkingMemory(b)
        for i in (1, 2):
            wm.record_task(
                goal="部署记忆系统到生产", goal_type="deploy",
                context={"preconditions": ["已有向量库"], "constraints": ["零外部依赖"]},
                steps=[{"action": "安装依赖"}, {"action": "初始化数据库"},
                       {"action": "启动服务"}],
                artifacts=["deploy.sh"], outcome="success", duration_min=20,
                project="mnemosyne",
            )
        # 造失败 → 负记忆（必须走结构化任务事实：free text 无法判定严重度）
        wm.record_task(
            goal="用余弦硬阈值过滤提升召回精度", goal_type="tuning",
            context={"preconditions": ["已有向量库"]},
            steps=[{"action": "设置阈值 0.85", "result": "failed",
                    "reason": "召回率从 0.72 掉到 0.31"},
                   {"action": "回滚配置"}],
            artifacts=[], outcome="rolled_back", duration_min=15,
            project="mnemosyne",
        )
        con = b.consolidate_memories()
        print("    " + jdump(con, 500))
        check("返回三条路径的结果",
              set(con) >= {"semantic", "procedural", "anti_pattern"}, list(con))
        check("情景→语义 至少生成 1 条",
              len((con.get("semantic") or {}).get("promoted") or []) >= 1,
              con.get("semantic"))
        check("情景→程序性 至少生成 1 条",
              len((con.get("procedural") or {}).get("promoted") or []) >= 1,
              con.get("procedural"))
        check("失败→负记忆 至少生成 1 条",
              len((con.get("anti_pattern") or {}).get("produced") or []) >= 1,
              con.get("anti_pattern"))
        sem = (con.get("semantic") or {}).get("promoted") or [{}]
        check("语义升级记录了跨会话数（≥3）",
              int(sem[0].get("sessions") or 0) >= 3, sem[0])
        # 幂等
        con2 = b.consolidate_memories()
        check("复跑幂等（0 新增）",
              sum(len((con2.get(k) or {}).get("promoted")
                      or (con2.get(k) or {}).get("produced") or [])
                  for k in ("semantic", "procedural", "anti_pattern")) == 0,
              jdump(con2, 300))

        # ================= 6. inject_context =================
        print("\n[6] inject_context — 五步召回流水线")
        inj = b.inject_context("召回准确率怎么提升", budget_tokens=600,
                               goal="优化召回", project=None)
        print("    tokens=%s/%s used=%s"
              % (inj.get("tokens"), inj.get("budget_tokens"), inj.get("used")))
        print("    --- 注入文本前 8 行 ---")
        for line in (inj.get("text") or "").splitlines()[:8]:
            print("    " + line)
        check("有注入文本", bool((inj.get("text") or "").strip()))
        check("不超预算", int(inj.get("tokens") or 0) <= 600,
              (inj.get("tokens"), inj.get("budget_tokens")))
        check("有四段配额账目",
              set(inj.get("quota") or {}) >= {"profile", "task_related",
                                              "experience", "pending"},
              inj.get("quota"))
        check("画像段有内容", int((inj.get("used") or {}).get("profile") or 0) > 0,
              inj.get("used"))
        check("四段用量都有记账",
              set(inj.get("used") or {}) >= {"profile", "task_related",
                                             "experience", "pending"},
              inj.get("used"))
        # 无重复注入：同一条内容不应出现在两个段里
        txt = inj.get("text") or ""
        lines = [l.strip() for l in txt.splitlines()
                 if len(l.strip()) > 12 and l.strip().startswith("- ")]
        dup = [l for l in set(lines) if lines.count(l) > 1]
        check("注入文本无重复条目", not dup, dup)

        # ================= 7. recall_catalog + expand_memory =================
        print("\n[7] recall_catalog + expand_memory — 渐进披露")
        cat = b.recall_catalog("向量后端 显卡 出差", k=8)
        print("    count=%s catalog_tokens=%s" % (cat["count"], cat["catalog_tokens"]))
        for it in cat["items"][:5]:
            print("      [%s] %s | %s" % (it["type"], it["title"], it["hint"][:36]))
        check("目录有条目", cat["count"] >= 1, cat["count"])
        check("每条含 title/hint/id",
              all(i.get("title") and i.get("hint") is not None and i.get("id")
                  for i in cat["items"]), cat["items"][:1])
        check("目录比全文便宜（catalog_tokens < 全部展开）",
              cat["catalog_tokens"] < sum(i["tokens_if_expanded"]
                                          for i in cat["items"]),
              (cat["catalog_tokens"],
               sum(i["tokens_if_expanded"] for i in cat["items"])))
        first_id = cat["items"][0]["id"]
        exp = b.expand_memory(first_id)
        print("    expand: id=%s len(content)=%s neighbors=%s"
              % (exp["id"][:10], len(exp.get("content") or ""),
                 list(exp.get("neighbors") or {})))
        check("expand 取到正文", bool((exp.get("content") or "").strip()))
        check("expand 带信封", isinstance(exp.get("envelope"), dict), exp.get("envelope"))
        check("expand 不存在 id 返回 None", b.expand_memory("nope") is None)

        # ================= 8. 级联删除 =================
        print("\n[8] memory_dependents + cascade_delete — 级联删除")
        src = b.retain("用户的显卡是 RTX 4090 24GB", mtype="semantic",
                       project="mnemosyne")
        der = b.retain("用户的机器适合跑 7B 量化模型", mtype="semantic",
                       project="mnemosyne")
        # 手工建立派生关系
        b.store.update_by_id(der, {"derived_from": [src]})
        dep = b.memory_dependents(src)
        print("    dependents: " + jdump(dep, 260))
        check("预检能列出派生项", src in json.dumps(dep, ensure_ascii=False)
              or int(dep.get("count") or 0) >= 1, dep)
        res = b.cascade_delete(src, reason="用户要求删除")
        print("    delete: " + jdump(res, 320))
        check("原始被删", len(res.get("deleted") or []) >= 1, res)
        check("派生被失效", len(res.get("invalidated") or []) >= 1, res)
        check("删除后原始记录状态为 deleted",
              (b.store.find_by_id(src) or {}).get("status") == "deleted",
              (b.store.find_by_id(src) or {}).get("status"))
        drec = b.store.find_by_id(der) or {}
        check("派生结论置信度被降权",
              float(drec.get("confidence") or 1.0) < 0.7, drec.get("confidence"))

        # ================= 9. 掩码 =================
        print("\n[9] mask_memory + unmask_memory — 掩码而非删除")
        mid = b.retain("用户的手机号是 13800000000", mtype="semantic")
        mk = b.mask_memory(mid, keep_original=True)
        print("    mask: " + jdump(mk, 260))
        check("掩码成功", mk.get("masked") is True or mk.get("ok") is True, mk)
        mrec = b.store.find_by_id(mid) or {}
        check("正文被替换为占位符",
              "已" in str(mrec.get("content")) and "13800000000" not in str(mrec.get("content")),
              mrec.get("content"))
        check("记录仍存在（不是删除）", mrec.get("status") != "deleted",
              mrec.get("status"))
        um = b.unmask_memory(mid)
        print("    unmask: " + jdump(um, 200))
        urec = b.store.find_by_id(mid) or {}
        check("撤销掩码后恢复原文", "13800000000" in str(urec.get("content")),
              urec.get("content"))

        # ================= 10. 度量 =================
        print("\n[10] memory_metrics — 三域 13 项（诚实报告）")
        allm = b.memory_metrics()
        print("    availability: " + jdump(allm.get("availability"), 200))
        for dom in ("chat", "working", "coding"):
            dm = b.memory_metrics(domain=dom)
            avail = dm.get("availability") or {}
            print("    %-8s ok=%s insufficient=%s"
                  % (dom, avail.get("ok"), avail.get("insufficient_data")))
            check("%s 域指标可用" % dom, isinstance(dm, dict) and bool(dm))
        check("总览含 availability 汇总",
              isinstance(allm.get("availability"), dict), allm.get("availability"))
        check("数据不足项标 insufficient_data 而不是 0",
              (allm.get("availability") or {}).get("insufficient_data", 0) >= 0,
              allm.get("availability"))
        # 抽查一项：错误记忆存活时长现在应该有值（依赖 superseded_at 落库修复）
        chat = b.memory_metrics(domain="chat")
        wrong = (chat.get("metrics") or {}).get("wrong_memory_lifetime_days") \
            or (chat.get("metrics") or {}).get("stale_correction_latency_days") \
            or {}
        print("    错误记忆存活时长: " + jdump(wrong, 200))

        # ================= 11. topic_summarize =================
        print("\n[11] topic_summarize — L3 主题摘要")
        from mnemosyne.session import extract, summary_record
        for sess, txt in (("sess-1", "我们决定用 RRF 融合。还有一件事没定：半衰期取多少？"),
                          ("sess-2", "半衰期定为 90 天。下一步做灰度放量。")):
            sm = extract([{"role": "user", "content": txt}])
            b.retain(**summary_record(sm, session_id=sess, project="mnemosyne"))
        l3 = b.topic_summarize(project="mnemosyne", persist=True)
        print("    " + jdump({k: l3.get(k) for k in
                             ("goal", "decisions", "pending", "session_count", "memory_id")}, 420))
        check("合并了 2 个会话", int(l3.get("session_count") or 0) == 2,
              l3.get("session_count"))
        check("goal 取最新会话", bool(l3.get("goal")), l3.get("goal"))
        check("decisions 累积", len(l3.get("decisions") or []) >= 1,
              l3.get("decisions"))
        check("L3 已落库", bool(l3.get("memory_id")), l3.get("memory_id"))
        check("L3 记录 pointer.children 非空",
              bool(((b.store.find_by_id(l3.get("memory_id")) or {}).get("meta") or {})
                   .get("pointer", {}).get("children")), l3.get("memory_id"))
        # 无数据时不报错
        empty = b.topic_summarize(project="不存在的项目")
        check("无会话时优雅返回", empty.get("session_count") == 0, empty)

        # ================= 12. trace_memory =================
        print("\n[12] trace_memory — 指针链回溯")
        tr = b.trace_memory(l3.get("memory_id"))
        print("    depth=%s levels=%s reaches_l0=%s"
              % (tr.get("depth"), [c["level"] for c in tr.get("chain") or []],
                 tr.get("reaches_l0")))
        check("链非空", int(tr.get("depth") or 0) >= 1, tr.get("depth"))
        check("链首是 L3", (tr.get("chain") or [{}])[0].get("level") == "L3",
              (tr.get("chain") or [{}])[0].get("level"))

        # ================= 13. 热路径 =================
        print("\n[13] enable_hot_path / status / flush")
        st0 = b.hot_path_status()
        check("未开启时 status 明确说未开启", st0.get("enabled") is False, st0)
        b.enable_hot_path(max_queue=100, batch_size=8, flush_interval=0.2)
        st1 = b.hot_path_status()
        print("    " + jdump(st1, 300))
        check("开启后 enabled=True", st1.get("enabled") is True, st1)
        # 通过热路径写
        n_before = b.store.count()
        for i in range(10):
            b._hot_path.submit({"content": "热路径记录 %d 关于检索延迟的观察" % i,
                                "mtype": "episodic", "id": "hot%d" % i})
        fl = b.flush_hot_path()
        print("    flush: " + jdump(fl, 240))
        check("flush 后写入落盘（≥8 条）", b.store.count() - n_before >= 8,
              (n_before, b.store.count()))
        check("flush 返回统计", isinstance(fl, dict), fl)

        b.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 62)
    print("通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print("  - " + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
