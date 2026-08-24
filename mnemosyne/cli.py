import argparse
import json
import os
import sys

from .brain import (MemoryBrain, _demo, _expire_old, _hindsights_bench, _benchmark,)
from .utils import (_fail, _ok,)

# === Constants (defined in package __init__) ===
import os as _os_init
VERSION = "7.0.0"
INDEX_NAME = "index.jsonl"
GRAPH_NAME = "graph.jsonl"
META_NAME = "meta.json"
EMBEDDING_DIM = 128
PROJ_BUCKETS = 2048
DEFAULT_DIR = _os_init.path.join(_os_init.path.expanduser("~"), ".mnemosyne")
MEMORY_TYPES = {
    "semantic", "episodic", "procedural", "preference", "lesson",
    "identity", "reflection", "strategy", "todo", "note",
    "conversation", "fact", "event",
}
MEMORY_LAYERS = {"working", "episodic", "semantic", "procedural", "reflective"}
FACT_TYPES = {"fact", "opinion", "belief", "observation", "inference", "hypothesis"}
SOURCE_TYPES = {"user", "system", "inference", "web_search", "file", "agent_generated", "external"}
VERIFY_STATUS = {"unverified", "verified", "contradicted", "outdated", "superseded"}


def _build_parser():
    p = argparse.ArgumentParser(prog="mnemosyne", description="Mnemosyne OS Engine v7.0.0")
    p.add_argument("--dir", default=None, help="记忆库目录（默认 ~/.mnemosyne）")
    p.add_argument("--no-embeddings", action="store_true", help="禁用向量检索")
    p.add_argument("--no-graph", action="store_true", help="禁用知识图谱")
    p.add_argument("--backend", default=None, choices=["jsonl", "sqlite"],
                   help="存储后端（默认 sqlite；jsonl 为兼容后端）")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("init", help="初始化记忆库")
    sub.add_parser("demo", help="运行演示验证")
    st = sub.add_parser("status", help="查看状态")
    st.add_argument("--json", action="store_true")
    sub.add_parser("stats", help="统计概览")
    dc = sub.add_parser("doctor", help="健康检查（完整性/磁盘余量）")
    dc.add_argument("--json", action="store_true")

    # 保留1.x兼容
    r = sub.add_parser("retain", help="存储一条记忆")
    r.add_argument("--content", required=True)
    r.add_argument("--type", default="semantic", choices=sorted(MEMORY_TYPES))
    r.add_argument("--layer", default=None, choices=sorted(MEMORY_LAYERS))
    r.add_argument("--tags", default="")
    r.add_argument("--source", default="")
    r.add_argument("--importance", type=int, default=None)
    r.add_argument("--expires", default="")
    r.add_argument("--context", default="")
    r.add_argument("--fact-type", default=None, choices=sorted(FACT_TYPES))
    r.add_argument("--confidence", type=float, default=None)
    r.add_argument("--source-type", default=None, choices=sorted(SOURCE_TYPES))

    c = sub.add_parser("recall", help="检索记忆")
    c.add_argument("query")
    c.add_argument("--k", type=int, default=5)
    c.add_argument("--layer", default=None)
    c.add_argument("--type", default=None)
    c.add_argument("--tag", default=None)
    c.add_argument("--from", dest="date_from", default=None)
    c.add_argument("--to", dest="date_to", default=None)
    c.add_argument("--multi-hop", action="store_true", help="启用多跳推理")
    c.add_argument("--json", action="store_true")
    c.add_argument("--budget-tokens", type=int, default=None, help="Token 预算约束")

    f = sub.add_parser("reflect", help="反思生成洞察")
    f.add_argument("question", nargs="?", default=None)
    f.add_argument("--deep", action="store_true", help="深度认知反思")
    f.add_argument("--json", action="store_true")

    # v2.0 新增命令
    cs = sub.add_parser("consolidate", help="记忆整合压缩")
    cs.add_argument("--dry-run", action="store_true")
    cs.add_argument("--min-similarity", type=float, default=0.6)

    sl = sub.add_parser("self-learn", help="自学习循环")
    sl.add_argument("--lookback", type=int, default=30, help="回溯天数")

    gq = sub.add_parser("graph", help="知识图谱查询")
    gq.add_argument("entity", nargs="?", default=None)
    gq.add_argument("--depth", type=int, default=2)
    gq.add_argument("--to", default=None, help="路径查询目标实体")
    gq.add_argument("--max-path", type=int, default=3)

    gq2 = sub.add_parser("graph-query", help="知识图谱查询（返回节点+边）")
    gq2.add_argument("entity")
    gq2.add_argument("--depth", type=int, default=2)
    gq2.add_argument("--json", action="store_true")

    vi = sub.add_parser("verify-integrity", help="校验账本链完整性")
    vi.add_argument("--json", action="store_true")

    la = sub.add_parser("ledger-audit", help="查看某记忆的账本审计链")
    la.add_argument("memory_id")
    la.add_argument("--json", action="store_true")

    hb = sub.add_parser("hindsights-bench", help="Hindsight 对标评测")
    hb.add_argument("--count", type=int, default=200)

    s = sub.add_parser("search-capture", help="沉淀联网搜索结果")
    s.add_argument("--query", required=True)
    s.add_argument("--results", required=True)
    s.add_argument("--urls", default="")
    s.add_argument("--title", default="")

    ch = sub.add_parser("should-research", help="检查是否需要联网搜索")
    ch.add_argument("query")
    ch.add_argument("--max-age", type=int, default=7)

    d = sub.add_parser("dedup", help="去重")
    d.add_argument("--dry-run", action="store_true")

    fo = sub.add_parser("forget", help="删除记忆")
    fo.add_argument("memory_id")
    fo.add_argument("--yes", action="store_true")

    e = sub.add_parser("export", help="导出记忆")
    e.add_argument("--format", default="json", choices=["json", "md"])
    e.add_argument("--out", default="")

    im = sub.add_parser("import", help="导入记忆")
    im.add_argument("path")

    rp = sub.add_parser("repair", help="修复损坏的记忆文件")
    rp.add_argument("--dry-run", action="store_true")

    bm = sub.add_parser("benchmark", help="性能基准测试")
    bm.add_argument("--count", type=int, default=2000)

    mg = sub.add_parser("migrate", help="从 JSONL 后端迁移到 SQLite 后端")
    mg.add_argument("--jsonl", default=None,
                    help="JSONL 文件路径（默认 <--dir>/index.jsonl）")

    return p


def main(argv=None):
    # Windows 控制台编码兼容：保留控制台编码，但无法编码的字符（如 ✓/✔）
    # 替换为 ? 而非抛 UnicodeEncodeError 崩溃（GBK 控制台上 CLI 退出码 1 的根因）。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    args = _build_parser().parse_args(argv)
    base_dir = args.dir or DEFAULT_DIR
    enable_emb = not getattr(args, "no_embeddings", False)
    enable_gr = not getattr(args, "no_graph", False)
    backend = getattr(args, "backend", None) or None

    if args.command == "migrate":
        # 迁移命令：JSONL → SQLite（原 JSONL 文件保留）
        from storage import SqliteBackend
        jsonl_path = args.jsonl or os.path.join(base_dir, "index.jsonl")
        if not os.path.exists(jsonl_path):
            return _fail(f"JSONL 文件不存在：{jsonl_path}",
                         fix="用 --jsonl 指定 index.jsonl 路径，或先初始化 JSONL 库。")
        try:
            n = SqliteBackend.migrate(jsonl_path, base_dir=base_dir)
        except Exception as e:
            return _fail(f"迁移失败：{e}")
        return _ok(f"已迁移 {n} 条记忆到 SQLite（{os.path.join(base_dir, 'memory.db')}），"
                   f"原 JSONL 文件已保留。")

    try:
        brain = MemoryBrain(base_dir, enable_embeddings=enable_emb,
                            enable_graph=enable_gr, store_backend=backend)
        brain.ensure_init()
    except OSError as e:
        return _fail(f"无法初始化记忆库：{e}", hint=f"目录 {base_dir} 不可写。",
                     fix="checks 权限后用 --dir 指定可写目录。")

    if args.command == "init":
        return _ok(f"记忆库已初始化：{brain.store.base_dir}")
    elif args.command == "demo":
        _demo(brain)
        return 0
    elif args.command == "status":
        info = brain._status_info()
        if getattr(args, "json", False):
            print(json.dumps(info, ensure_ascii=False, indent=2))
        else:
            print(f"记忆库目录：{brain.base_dir}")
            print(f"命名空间：{info.get('namespace')} | 后端：{info.get('backend')}")
            print(f"记忆总数：{info.get('total_memories')}（活跃 {info.get('active_count')}）")
            print(f"引擎Version：{VERSION}")
            print(f"容量：{info.get('percentage')}% (limit={info.get('limit')})")
        return 0
    elif args.command == "stats":
        ref = brain.reflect()
        print(json.dumps(ref, ensure_ascii=False, indent=2))
        return 0
    elif args.command == "doctor":
        info = brain.doctor()
        if getattr(args, "json", False):
            print(json.dumps(info, ensure_ascii=False, indent=2))
        else:
            print(f"状态：{info['status']}")
            print(f"总记录：{info['total_records']}（活跃 {info['active_records']}，"
                  f"损坏 {info['corrupt_records']}，已删 {info['deleted_records']}）")
            print(f"磁盘余量：{info['disk_free_mb']} MB")
            print(f"建议：{info['recommendation']}")
        return 0
    elif args.command == "retain":
        if not args.content or not args.content.strip():
            return _fail("记忆内容不能为空。", fix="--content 参数必须contains 有效文本。")
        _expire_old(brain.store)
        mid = brain.retain(
            args.content, mtype=args.type, layer=args.layer,
            tags=[t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None,
            source=json.loads(args.source) if args.source.startswith("{") else (
                {"url": args.source} if args.source else None),
            importance=args.importance, expires_at=args.expires or None,
            context=args.context, fact_type=args.fact_type,
            confidence=args.confidence, source_type=args.source_type,
        )
        rec = brain.store.find_by_id(mid) or {}
        return _ok(f"已存储：{rec.get('id')} [{rec.get('type')}/{rec.get('fact_type', '?')}] "
                   f"(importance={rec.get('importance', '?')}, "
                   f"confidence={rec.get('confidence', '?')})")
    elif args.command == "recall":
        budget = getattr(args, "budget_tokens", None)
        if budget is not None:
            results, cost_report = brain.recall(
                args.query, k=args.k, layer=args.layer, mtype=args.type,
                tag=args.tag, date_from=args.date_from, date_to=args.date_to,
                multi_hop=args.multi_hop, budget_tokens=budget,
            )
            if args.json:
                out = []
                for score, rec, reasons in results:
                    rec = dict(rec)
                    rec["_score"] = round(score, 4)
                    rec["_hit_reasons"] = reasons
                    out.append(rec)
                print(json.dumps({"results": out, "cost_report": cost_report},
                                 ensure_ascii=False, indent=2))
            else:
                for score, rec, reasons in results:
                    print(f"[{score:.3f}] ({rec['type']}/{rec.get('fact_type', '?')}) {rec['content'][:80]}")
                print(f"cost_report: Token消耗={cost_report['tokens_consumed']}, "
                      f"tokens_saved={cost_report['tokens_saved']}, "
                      f"query_tokens={cost_report['query_tokens']}, "
                      f"已选数量={cost_report['selected_count']}")
            return 0
        hits = brain.recall(
            args.query, k=args.k, layer=args.layer, mtype=args.type,
            tag=args.tag, date_from=args.date_from, date_to=args.date_to,
            multi_hop=args.multi_hop,
        )
        if args.json:
            out = []
            for score, rec, reasons in hits:
                rec = dict(rec)
                rec["_score"] = round(score, 4)
                rec["_hit_reasons"] = reasons
                out.append(rec)
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            if not hits:
                print("（无matches 记忆）")
            for score, rec, reasons in hits:
                print(f"[{score:.3f}] ({rec['type']}/{rec.get('fact_type', '?')}) {rec['content'][:80]}")
                print(f"      命中: {'+'.join(reasons)} | 可信度: {rec.get('confidence', '?')} "
                      f"| Time: {rec.get('created_at', '')} | id: {rec['id']}")
        return 0
    elif args.command == "reflect":
        ref = brain.reflect(question=args.question, deep=args.deep)
        if args.json:
            print(json.dumps(ref, ensure_ascii=False, indent=2))
        else:
            print(f"记忆总数：{ref['total']}")
            print(f"Type分布：{ref.get('by_type', {})}")
            print(f"事实Type分布：{ref.get('by_fact_type', {})}")
            print(f"ValidateStatus分布：{ref.get('by_verification', {})}")
            if ref.get("top_entities"):
                print(f"高频主题：{', '.join(e['entity'] for e in ref['top_entities'][:8])}")
            if ref.get("confidence_stats"):
                cs = ref["confidence_stats"]
                print(f"可信度：均Value {cs['mean']} | 最低 {cs['min']} | 最高 {cs['max']}")
            if ref.get("conflicts"):
                print(f"⚠ 潜在冲突：{len(ref['conflicts'])} ")
                for c in ref["conflicts"][:5]:
                    print(f"  - [{c.get('type', '?')}] {c['entity']}")
            if ref.get("cognitive_patterns"):
                print(f"\U0001f9e0 认知模式：{len(ref['cognitive_patterns'])} 类")
        return 0
    elif args.command == "consolidate":
        result = brain.consolidate(dry_run=args.dry_run, min_similarity=args.min_similarity)
        d = result.to_dict()
        if args.dry_run:
            print(f"预检：可巩固 {d['merges_planned']} 组记忆")
            for g in d.get("groups", [])[:5]:
                print(f"  - {g.get('size')}条 相似度{g.get('avg_similarity')} -> ids: {g.get('ids', [])[:3]}")
        else:
            print(f"✓ Memory Consolidation完成：{d['merges_executed']} 组")
        return 0
    elif args.command == "self-learn":
        result = brain.self_learn(lookback_days=args.lookback)
        print(f"✓ 自学习完成：generates  {result['learned']} 条策略")
        for s in result.get("strategies", []):
            print(f"  - [{s.get('tags')}] {s['content'][:80]}")
        return 0
    elif args.command == "graph":
        if args.to:
            path = brain.graph_path(args.entity, args.to, max_depth=args.max_path)
            if path:
                print(f"Path：{' -> '.join(path)}")
            else:
                print(f"未finds 从 {args.entity} 到 {args.to} 的Path")
        elif args.entity:
            result = brain.graph_query(args.entity, depth=args.depth)
            nodes = result.get("nodes", [])
            edges = result.get("edges", [])
            print(f"节点：{', '.join(nodes[:20])}")
            for e in edges:
                print(f"  {e.get('from')} -[{e.get('relation')}]-> {e.get('to')}")
        else:
            print("用法：graph <实体名> [--depth 2] [--to <目标>]")
        return 0
    elif args.command == "graph-query":
        result = brain.graph_query(args.entity, depth=args.depth)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"查询：{result.get('query')}")
            print(f"节点：{', '.join(result.get('nodes', []))}")
            for e in result.get("edges", []):
                print(f"  {e.get('from')} -[{e.get('relation')}]-> {e.get('to')}")
        return 0
    elif args.command == "verify-integrity":
        result = brain.verify_integrity()
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            status = "✓ 完整" if result.get("valid") else "✗ 损坏"
            print(f"{status} | 账本条目：{result.get('total')} | 首个断裂点：{result.get('first_broken_at')}")
        return 0
    elif args.command == "ledger-audit":
        trail = brain.ledger_audit(args.memory_id)
        if args.json:
            print(json.dumps(trail, ensure_ascii=False, indent=2, default=str))
        else:
            if not trail:
                print("（无账本记录）")
            for e in trail:
                print(f"[{e.get('seq')}] {e.get('action')} @ {e.get('timestamp')}")
        return 0
    elif args.command == "hindsights-bench":
        _hindsights_bench(brain, test_count=args.count)
        return 0
    elif args.command == "search-capture":
        urls = [u.strip() for u in args.urls.split(",") if u.strip()]
        res = brain.search_capture(args.query, args.results, urls=urls, title=args.title)
        verb = "updates " if res["updated"] else "新增"
        return _ok(f"已{verb}searches 记忆：{res['id']}（累计 {res.get('capture_count', 1)}  times）")
    elif args.command == "should-research":
        res = brain.should_research(args.query, max_age_days=args.max_age)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    elif args.command == "dedup":
        result = brain.dedup(dry_run=args.dry_run)
        if args.dry_run:
            print(f"预检：可merges  {result['merged']} 条；相似对 {len(result['similar_pairs'])} 组")
        else:
            print(f"✓ 去重完成：merges  {result['merged']} 条")
        return 0
    elif args.command == "forget":
        if not args.yes:
            rec = brain.store.find_by_id(args.memory_id)
            if rec:
                print(f"将deletes ：{rec.get('content', '')[:60]}")
                print("请加 --yes confirms ")
                return 0
            return _fail(f"未finds 记忆 {args.memory_id}")
        ok = brain.forget(args.memory_id)
        return _ok(f"已deletes ：{args.memory_id}") if ok else _fail(f"未finds ：{args.memory_id}")
    elif args.command == "export":
        out = brain.export(fmt=args.format, out_path=args.out or None)
        return _ok(f"已exports ：{out}")
    elif args.command == "import":
        if not os.path.exists(args.path):
            return _fail(f"imports File not found：{args.path}")
        n = brain.import_file(args.path)
        return _ok(f"Imported  {n}  memory records")
    elif args.command == "repair":
        result = brain.repair(dry_run=args.dry_run)
        if result["corrupt"] == 0:
            return _ok(f"Memory file intact（{result['kept']} 条）")
        if args.dry_run:
            print(f"🔍 Found  {result['corrupt']}  corrupt lines")
            return 0
        print(f"🔧 Repaired: removed  {result['corrupt']}  corrupt lines，保留 {result['kept']} 条")
        return _ok(f"Repair complete, backup: {result['backup']}")
    elif args.command == "benchmark":
        _benchmark(brain, count=args.count)
        return 0
    else:
        _build_parser().print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
