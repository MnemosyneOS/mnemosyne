#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆生命周期验收：写入(confidence/tags) -> 更正(supersedes) -> 遗忘(forget)。

为什么需要它：**规则写了不等于工具支持。** 7.0.0 的 MCP 面上，
`retain` 的 confidence 会被静默丢弃、`retain_batch` 丢弃逐项元数据、
`recall` 不回传 memory_id、且**根本没有任何删除工具**。
所以"用户说忘记 X"这类规则，在 7.0.1 之前是**结构性不可执行**的。

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


def check(cond, label):
    print(('  [PASS] ' if cond else '  [FAIL] ') + label)
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
    r2 = m.tool('recall', {'query': '我喜欢什么样的表达方式', 'k': 3})
    show('recall', r2)
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
    show('遗忘后再召回', after)
    check(all(x.get('memory_id') != tgt for x in after.get('results', [])),
          '被遗忘的记忆不再出现在召回结果里（否则命中 F11：检索缓存未失效）')

    # ---------- 5. 按 id 遗忘 + dry_run ----------
    dr = m.tool('forget', {'query': '结论说清楚', 'k': 2, 'dry_run': True})
    show('forget(dry_run=True)', dr)
    check(dr.get('dry_run') is True and dr.get('targets'), 'dry_run 只解析候选、不做改动')
    r6 = m.tool('forget', {'memory_id': mid})
    check(r6.get('count', 0) == 1, 'forget(memory_id=...) 直接生效')

    con.close()
    m.stop()

    print('\n================ 结果汇总 ================')
    print('失败项：', FAIL if FAIL else '无')
    print('收尾提醒：删除 %s 与向量集合 mnemosyne_%s'
          % (os.path.join(brain_dir, 'data', 'namespaces', ns), ns))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
