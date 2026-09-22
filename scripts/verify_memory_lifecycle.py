#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆生命周期验收：写入(confidence/tags) -> 更正(supersedes) -> 遗忘(forget)
-> AIC 胶囊精确压缩（v7.0.2 新增第 6 节）。

为什么需要它：**规则写了不等于工具支持。** 7.0.0 的 MCP 面上，
`retain` 的 confidence 会被静默丢弃、`retain_batch` 丢弃逐项元数据、
`recall` 不回传 memory_id、且**根本没有任何删除工具**。
所以"用户说忘记 X"这类规则，在 7.0.1 之前是**结构性不可执行**的。

v7.0.2 变更（本脚本随之修订，两处）
-----------------------------------
1. 新增断言「无关查询受相关性下限约束」：7.0.2 引入相关性下限后，
   无关查询不再返回满 K 条无关记忆（7.0.1 的行为是无论如何都填满 k 条，
   且无关查询的 top1 融合分反而更高 —— 因为五路打分各自做了 x/max(x) 归一化，
   **分数没有绝对含义**）。
2. 修订「recall 回传 superseded_by」：该断言原先用同一个**无关查询**去召回被
   取代的记录，与上面第 1 条**语义直接冲突**（被取代的记录本就不该被无关查询
   召回）。现改用与目标内容相关的查询验证**投影**本身，把
   「投影是否完整」与「下限是否生效」两件事解耦、各自独立断言。

本脚本以 MCP stdio 方式起一个子进程，在**一次性命名空间**上跑断言，
跑完由调用方删除该命名空间目录与向量集合。

用法
----
    <python> scripts/verify_memory_lifecycle.py \\
        --python   <brain-python> \\
        --brain-dir <brain-dir> \\
        --src-root <含 mnemosyne/ 与 storage/ 的源码根> \\
        --namespace memlife_verify

退出码 0 = 全部通过；非 0 = 有断言失败（标准输出里能看到逐项 PASS/FAIL）。

收尾：删掉 `<brain-dir>/data/namespaces/<namespace>/`
与向量集合 `mnemosyne_<namespace>`。

设计取舍
--------
- 用**一次性命名空间**而不是临时 brain-dir：命名空间隔离本身就是被测对象之一，
  而且这样不会碰到生产库的记录。
- 子进程 env 与真实部署配置**逐键对齐** —— 少一个 `MNEMOSYNE_PLUGINS`
  就会得到"召回完全没有区分度"的假结论（见 docs/ACCEPTANCE_GUIDE.md 第 2 节）。
- 断言读的是**库内实际落盘的列**（隔离之后的真相），不只是工具返回值。
  工具返回是自述，库内状态才是事实。
- 不使用任何第三方依赖，只用标准库。
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys

FAIL = []


def check(cond, label, detail=None):
    """断言并打印。`detail` 可选：失败时把现场数据一起打出来，便于定位。

    为什么加第三个参数：断言失败时"只知道失败、不知道失败成什么样"会让排查
    多花一轮；把相关字段一起打印，一次就能看清（这也是两套验收脚本的统一签名）。
    """
    print(('  [PASS] ' if cond else '  [FAIL] ') + label
          + ('' if detail is None else '   | ' + str(detail)[:200]))
    if not cond:
        FAIL.append(label)


def show(label, obj):
    print('\n### ' + label)
    print(json.dumps(obj, ensure_ascii=False, indent=2)[:1500])


class Mcp:
    """极简 MCP stdio 客户端：只实现 initialize / tools/list / tools/call。"""

    def __init__(self, python, brain_dir, namespace, src_root, extra_env):
        env = dict(os.environ)
        env.update(extra_env)
        env.update({'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1'})
        self.namespace = namespace
        self.p = subprocess.Popen(
            [python, '-m', 'mnemosyne.webui.mcp_server',
             '--brain-dir', brain_dir, '--namespace', namespace],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=src_root, text=True, encoding='utf-8', bufsize=1,
        )
        self._id = 0

    def call(self, method, params=None):
        self._id += 1
        req = {'jsonrpc': '2.0', 'id': self._id, 'method': method}
        if params is not None:
            req['params'] = params
        self.p.stdin.write(json.dumps(req, ensure_ascii=False) + '\n')
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError('server closed; stderr=' +
                               (self.p.stderr.read() or '')[:2000])
        return json.loads(line)

    def tools(self):
        return self.call('tools/list')['result']['tools']

    def tool(self, name, args):
        r = self.call('tools/call', {'name': name, 'arguments': args})
        if 'error' in r:
            return {'__error__': r['error']}
        return json.loads(r['result']['content'][0]['text'])

    def stop(self):
        try:
            self.p.stdin.close()
        except Exception:
            pass
        self.p.terminate()


def main():
    ap = argparse.ArgumentParser(description='Mnemosyne 记忆生命周期验收')
    ap.add_argument('--python', default=sys.executable,
                    help='用来起 MCP 子进程的 python（应与部署一致）')
    ap.add_argument('--brain-dir', required=True, help='brain 数据目录')
    ap.add_argument('--src-root', required=True,
                    help='含 mnemosyne/ 与 storage/ 的源码根（子进程 cwd）')
    ap.add_argument('--namespace', default='memlife_verify',
                    help='一次性命名空间，跑完请删除')
    ap.add_argument('--no-vector-plugin', action='store_true',
                    help='不加载向量插件（仅用于对比实验，会让所有分数塌到常数底）')
    args = ap.parse_args()

    brain_dir, ns = args.brain_dir, args.namespace

    # 与真实部署配置逐键对齐。注意 MNEMOSYNE_QDRANT_COLLECTION 必须**不设**：
    # 设了会把所有命名空间钉死成一个集合（见 docs/KNOWN_DEFECTS.md 的 F2）。
    extra_env = {
        'MNEMOSYNE_QDRANT_URL': os.environ.get('MNEMOSYNE_QDRANT_URL', 'http://127.0.0.1:6333'),
        'MNEMOSYNE_EMBED_URL': os.environ.get('MNEMOSYNE_EMBED_URL', 'http://127.0.0.1:5080/v1/embeddings'),
        'MNEMOSYNE_EMBED_MODEL': os.environ.get('MNEMOSYNE_EMBED_MODEL', 'bge-m3'),
        'MNEMOSYNE_EMBED_DIM': os.environ.get('MNEMOSYNE_EMBED_DIM', '1024'),
    }
    if not args.no_vector_plugin:
        extra_env['MNEMOSYNE_PLUGINS'] = 'qdrant_backend'

    print('python     : %s' % args.python)
    print('brain-dir  : %s' % brain_dir)
    print('src-root   : %s' % args.src_root)
    print('namespace  : %s' % ns)
    print('vector plug: %s' % ('off' if args.no_vector_plugin else extra_env.get('MNEMOSYNE_PLUGINS')))

    m = Mcp(args.python, brain_dir, ns, args.src_root, extra_env)
    m.call('initialize', {'clientInfo': {'name': 'tenant:' + ns}})

    # ---------- 工具面 ----------
    tl = m.tools()
    tools = [t['name'] for t in tl]
    print('\ntools(%d) = %s' % (len(tools), tools))
    check('forget' in tools, '存在 forget 工具（7.0.0 没有 -> 遗忘规则无处落地）')
    retain_props = [t for t in tl if t['name'] == 'retain'][0]['inputSchema']['properties']
    check('tags' in retain_props, 'retain schema 含 tags')
    check('supersedes' in retain_props, 'retain schema 含 supersedes（更正路径）')
    rb = json.dumps([t for t in tl if t['name'] == 'retain_batch'][0]['inputSchema'])
    check('tags' in rb and 'confidence' in rb, 'retain_batch schema 含逐项 tags/confidence')

    # ---------- 1. 写入：confidence 与 tags 必须真的生效（F7 / F8）----------
    r1 = m.tool('retain', {
        'content': '验收样本：用户偏好先把结论说清楚，再给依据。',
        'mtype': 'preference', 'tags': ['偏好', '项目'],
        'confidence': 0.95, 'importance': 4,
    })
    show('retain(confidence=0.95, tags=[偏好,项目])', r1)
    check(r1.get('confidence') == 0.95,
          'retain 尊重显式 confidence=0.95（否则命中 F7：被 Notary 改写为 0.7）')
    mid = r1.get('memory_id')

    r3 = m.tool('retain_batch', {'items': [
        {'content': '验收样本：记忆栈装在 C 盘，向量库用 6333。', 'tags': ['项目'], 'confidence': 0.9},
        {'content': '验收样本：嵌入模型是 1024 维 bge-m3。', 'tags': ['项目', '事实'], 'confidence': 0.8},
    ]})
    show('retain_batch（逐项 tags/confidence）', r3)
    check(r3.get('with_tags') == 2 and r3.get('with_confidence') == 2,
          'retain_batch 逐项接收 tags/confidence（否则命中 F8）')
    check(r3.get('namespace') == ns,
          'retain_batch 报告真实命名空间（恒报 default -> 命中 F10）')

    # ---------- 2. 召回：必须回传 memory_id 与 verification（F9）----------
    # v7.0.2 说明：这条查询是**纯改写**（`我喜欢什么样的表达方式` ↔ 记忆
    # `用户偏好先把结论说清楚，再给依据。`），实测原始余弦仅 0.49 —— 低于
    # 相关性下限（0.55，标定见 docs/ACCEPTANCE_GUIDE.md 第 8 节）。它能被召回
    # 靠的是 **Path6 结构化标签通道**（`喜欢` → 标签 `偏好`），这正是该通道的
    # 设计用途：语义阈值负责"宁缺勿滥"，标签通道负责"结构性保证偏好类记忆可达"。
    # 因此本步同时验证了这两条路径的协同，而不是只验语义。
    r2 = m.tool('recall', {'query': '我喜欢什么样的表达方式', 'k': 3})
    show('recall（纯改写查询，靠标签通道命中）', r2)
    top = (r2.get('results') or [{}])[0]
    check(bool(top.get('memory_id')), 'recall 回传 memory_id（否则 forget/更正无法定位）')
    check('verification' in top, 'recall 回传 verification（否则分不清旧说法是否已被取代）')

    # ---------- 3. 更正：supersedes -> 旧记忆 superseded ----------
    r4 = m.tool('retain', {
        'content': '验收样本（更正）：用户偏好先给结论、再给依据，并且要可验证的证据。',
        'mtype': 'preference', 'tags': ['偏好'], 'confidence': 0.98,
        'supersedes': mid,
    })
    show('retain(supersedes=旧id)', r4)
    check(bool(r4.get('memory_id')) and r4.get('memory_id') != mid, '更正经 retain 写入新记忆')

    dbp = os.path.join(brain_dir, 'data', 'namespaces', ns, 'memory.db')
    if not os.path.exists(dbp):
        con = sqlite3.connect(os.path.join(brain_dir, 'memory.db'))
    else:
        con = sqlite3.connect(dbp)
    con.row_factory = sqlite3.Row
    old = con.execute('SELECT confidence,status,verification,superseded_by '
                      'FROM memories WHERE id=?', (mid,)).fetchone()
    show('旧记忆（库内直查）', dict(old) if old else None)
    check(old is not None and old['verification'] == 'superseded',
          '旧记忆 verification=superseded')
    check(old is not None and old['superseded_by'] == r4.get('memory_id'),
          '旧记忆 superseded_by 指向新记忆')

    # ---------- 4. 遗忘：confidence->0 + deleted，且不再被召回（F11）----------
    r5 = m.tool('forget', {'query': 'bge-m3 是多少维', 'k': 1})
    show('forget(query=...)', r5)
    check(r5.get('count', 0) >= 1, 'forget 执行成功')
    tgt = (r5.get('forgotten') or [{}])[0].get('memory_id')
    row = con.execute('SELECT confidence,status,deleted_at FROM memories WHERE id=?',
                      (tgt,)).fetchone()
    show('被遗忘记忆（库内直查）', dict(row) if row else None)
    check(row is not None and row['confidence'] == 0.0, 'confidence 已降为 0')
    check(row is not None and row['status'] == 'deleted', 'status 已置为 deleted')
    hist = [dict(x) for x in con.execute(
        'SELECT confidence,reason FROM confidence_history WHERE memory_id=?', (tgt,))]
    show('confidence_history', hist)
    check(any(x['reason'] == 'user_forget' for x in hist), '可信度轨迹记录了遗忘原因')
    logs = [x['action'] for x in con.execute(
        'SELECT action FROM audit_log WHERE target_id=?', (tgt,))]
    check('forget' in logs, 'audit_log 记录了 forget')

    after = m.tool('recall', {'query': 'bge-m3 嵌入模型是多少维', 'k': 5})
    show('遗忘后再召回（无关查询）', after)
    check(all(x.get('memory_id') != tgt for x in after.get('results', [])),
          '被遗忘的记忆不再出现在召回结果里（否则命中 F11：检索缓存未失效）')
    # v7.0.2 新增断言：相关性下限生效 —— 无关查询**不再**返回满 K 条无关记忆。
    # 7.0.1 的行为是"无论如何都填满 k 条"，于是 Agent 拿到 5 条看似证据的无关内容；
    # 实测无关查询 top1 融合分可达 1.0200（高于相关查询的 0.6922），因为五路打分
    # 每路都做了 x/max(x) 归一化 —— 最高分恒为 1.0，分数**没有绝对含义**。
    # 下限（REL_MIN_SEMANTIC=0.45，标定自实测：相关簇 min 0.5222 / 无关簇 max 0.4064）
    # 让"没有相关记忆"能被如实表达为接近空的结果。
    check(len(after.get('results', [])) <= 1,
          '无关查询受相关性下限约束（v7.0.2 新行为：宁缺勿滥，避免把无关记忆当证据）')

    # ---- F9-b：superseded_by 必须出现在 recall 输出投影里 ----
    # v7.0.2 修订说明：这条断言原先用**同一个无关查询**去召回被取代的记录，
    # 那与上面"下限生效"的语义**直接冲突** —— 被取代的记录本来就不该被无关查询
    # 召回（那正是下限要拦的东西）。原来能通过，只是因为 7.0.1 无论相关与否都
    # 填满 k 条。故此处改用与目标记录**内容相关**的查询来验证投影本身，
    # 把两件事彻底解耦：① 投影是否带出 superseded_by（本断言）；
    # ② 无关查询是否被下限抑制（上一断言）。
    rel = m.tool('recall', {'query': '用户偏好 先给结论 再给依据', 'k': 5})
    show('按相关内容召回（验证 superseded_by 投影）', rel)
    sup = [x for x in rel.get('results', []) if x.get('verification') == 'superseded']
    check(bool(sup) and all(x.get('superseded_by') == r4.get('memory_id') for x in sup),
          'recall 回传 superseded_by（恒为 null -> 输出投影漏列，命中 F9-b）')

    # ---------- 5. 按 id 遗忘 + dry_run ----------
    dr = m.tool('forget', {'query': '结论说清楚', 'k': 2, 'dry_run': True})
    show('forget(dry_run=True)', dr)
    check(dr.get('dry_run') is True and dr.get('targets'), 'dry_run 只解析候选、不做改动')
    r6 = m.tool('forget', {'memory_id': mid})
    check(r6.get('count', 0) == 1, 'forget(memory_id=...) 直接生效')

    # ---------- 6. AIC 记忆胶囊：极短上下文下的精确压缩（v7.0.2 新增） ----------
    # 验收目标：在"只给 1~2 轮、几百 token"的极短上下文里，仍然 100% 精准 ——
    # 即 (a) 原子（数字/日期/金额/型号/URL）一个不丢；
    #    (b) 需要原文时能**逐字**取回（无损可逆）；
    #    (c) 预算装不下时**降级而非丢弃**。
    # 这三条就是"压缩不以牺牲精准为代价"的可验证形式。
    tools6 = {t['name'] for t in m.call('tools/list',
                                        {}).get('result', {}).get('tools', [])}
    check('capsule' in tools6, '存在 capsule 工具（取单条记忆胶囊）')
    check('expand' in tools6, '存在 expand 工具（按指针精确取回原文）')

    # v7.0.2：**加长**这段样本，确保 40 token 的预算一定装不下正文 ——
    # 否则"降级为胶囊/精准索引"这条断言可能因为"恰好装得下"而**空转通过**
    # （第一版就踩了这个坑：原文 45 字正好 40 token，`truncated=false`、
    #  capsule_count/indexed_count 都是 0，断言失去意义）。
    CAP_TEXT = ('用户的生产库端口是 6333，嵌入模型是 bge-m3 共 1024 维，'
                '稀疏向量维度是 30522，预算上限 15000 元，交付日期 2026-12-31，'
                '联系人 example@example.com，部署区域 cn-hangzhou，延迟 12ms。')
    r7 = m.tool('retain', {'content': CAP_TEXT, 'mtype': 'semantic',
                           'tags': ['验收'], 'confidence': 0.9})
    capid = r7.get('memory_id')
    check(bool(capid), '胶囊验收样本已写入')

    capr = m.tool('capsule', {'memory_id': capid, 'budget_tokens': 40})
    show('capsule(budget_tokens=40)', capr)
    ctext = capr.get('text') or ''
    need_atoms = ('6333', 'bge-m3', '1024', '15000元', '2026-12-31', 'cn-hangzhou')
    flat = ''.join(ctext.casefold().split())
    check(capr.get('selfcheck_ok') is True,
          '胶囊自检通过（原子守恒 + 抽取式 + 指针在场）')
    check(all(a in flat for a in need_atoms),
          '胶囊保留全部关键原子（预算 40 token，实际 %s）' % capr.get('tokens'))
    check(bool(capr.get('ref')) and capr['ref'].startswith('m:'),
          '胶囊带稳定指针 ref（可逆的凭据）')
    check(capr.get('level') != 'full' and capr.get('lossless') is False,
          '原文超预算 → 确实发生了分层降级（不是无损直通）：level=%s tokens=%s/%s'
          % (capr.get('level'), capr.get('tokens'), capr.get('original_tokens')))

    exr = m.tool('expand', {'ref': capr.get('ref')})
    show('expand(ref)', {'verified': exr.get('verified'),
                         'chars': exr.get('content_chars'),
                         'content': (exr.get('content') or '')[:60]})
    stored_cap = con.execute('SELECT content FROM memories WHERE id=?',
                            (capid,)).fetchone()
    check(exr.get('verified') is True, 'expand 内容哈希校验通过')
    check(bool(stored_cap) and exr.get('content') == stored_cap['content'],
          'expand 逐字取回原文（无损可逆，压缩不丢信息）')

    # 极短预算召回：预算装不下原文时必须**降级为胶囊**，而不是丢弃该条记忆
    bt = m.tool('recall', {'query': '嵌入模型是几维', 'k': 5, 'budget_tokens': 40})
    show('recall(budget_tokens=40)', {'results': [
        {'band': x.get('confidence_band'), 'level': x.get('capsule_level'),
         'ref': x.get('ref'), 'content': (x.get('content') or '')[:70]}
        for x in (bt.get('results') or [])],
        'cost': {k: (bt.get('cost_report') or {}).get(k)
                 for k in ('budget_limit', 'budget_used', 'budget_respected',
                           'capsule_count', 'indexed_count', 'truncated')}})
    cr = bt.get('cost_report') or {}
    _flat = ''.join((''.join(x.get('content', '').casefold().split())
                     for x in (bt.get('results') or [])))
    _idx = ''.join((''.join(json.dumps(bt.get('precision_index') or [],
                                       ensure_ascii=False).casefold().split())))
    check('budget_limit' in cr and 'budget_used' in cr and 'budget_respected' in cr,
          '预算账目回传（budget_limit/budget_used/budget_respected）')
    check(cr.get('budget_used', 0) <= cr.get('budget_limit', 0) or not cr.get('budget_respected'),
          '预算被真正遵守（或已如实标注未能遵守）')
    check('1024' in _flat or '1024' in _idx,
          '极短预算下关键数值仍可见（正文胶囊或精准索引二者之一）')
    # v7.0.2 修订：这条原本无条件要求"必须有胶囊/索引项"，但只要本次**没有发生截断**
    # （候选本来就少、或都装得下），降级路径就无需触发 —— 无条件断言会变成假失败。
    # 改为**条件断言**：发生了截断 ⇒ 必须有胶囊或精准索引兜底，信息不消失。
    # 降级路径本身的确定性验证在上面的 `capsule(level != 'full')` 与
    # `scripts/verify_precision_recall.py` 的"分层阶梯"用例里（那里必然触发）。
    check((not cr.get('truncated'))
          or bool(cr.get('capsule_count') or cr.get('indexed_count')),
          '发生截断时必有胶囊/精准索引兜底（信息未消失）',
          (cr.get('truncated'), cr.get('capsule_count'), cr.get('indexed_count')))

    con.close()
    m.stop()

    print('\n================ 结果汇总 ================')
    print('失败项：', FAIL if FAIL else '无')
    print('收尾提醒：删除 %s 与向量集合 mnemosyne_%s'
          % (os.path.join(brain_dir, 'data', 'namespaces', ns), ns))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
