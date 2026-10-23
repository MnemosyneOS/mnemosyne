#!/usr/bin/env python3
"""
MCP (Model Context Protocol) Server for Mnemosyne OS
Zero external dependencies — pure stdlib JSON-RPC over stdio.

Multi-tenant support (Module 5.4 item 9 / Module 7.3):
  The client may pass a namespace via:
    - The ``initialize`` request's ``clientInfo.name`` field (e.g. "tenant:acme")
    - OR a per-request ``namespace`` argument on any tool call
  When set, the brain routes all I/O through an isolated sqlite file at
  ``<brain-dir>/data/namespaces/<namespace>/memory.db``.  Tenants are
  physically isolated — two namespaces see completely separate data.

Authentication (v7.0.0 阶段4):
  When ``MNEMOSYNE_MCP_TOKEN`` is set (or ``--token`` is passed), every
  request must carry the matching token, otherwise it is rejected with a
  JSON-RPC error (code -32001). The token may be provided via:
    - ``_meta.authToken`` on the JSON-RPC request (MCP standard)
    - ``params._meta.authToken``
    - a top-level ``authToken`` / ``token`` field
  When no token is configured, authentication is disabled (open mode),
  preserving backward compatibility.
"""
import sys, json, os, argparse, hashlib, hmac
from mnemosyne import MemoryBrain

# 订阅门控（v8.1.1 · SaaS 层）。**防御性导入**：门控模块缺失或语法错误
# 绝不能把 52 个工具一起带下线 —— 自部署用户（MNEMOSYNE_TIER_GATE 未设）
# 根本不该感知到它的存在。
try:
    from . import tiers as _tiers
except Exception:                                    # pragma: no cover
    try:
        import tiers as _tiers                       # 直接按文件跑（stdio 场景）
    except Exception:
        _tiers = None

DEFAULT_BRAIN = os.path.expanduser("~/.mnemosyne")
brain = None
_brain_dir = None
_default_namespace = "default"
_auth_token = None


def _get_expected_token():
    """Return the configured MCP token, or None if auth is disabled.

    🔴 2026-10-22 安全加固：``MNEMOSYNE_TOKEN``（控制台门面用的那个）与
    ``MNEMOSYNE_MCP_TOKEN``（Agent 侧用的那个）在本机约定同值，任一存在即启用鉴权。
    此前只读 ``MNEMOSYNE_MCP_TOKEN``，于是「只配了 MNEMOSYNE_TOKEN」时
    源码层会认为「未启用鉴权」而放行 —— 与网关层的判定不一致。
    现在两个变量都认，口径与 mcp_http.py 一致。
    """
    return (os.environ.get("MNEMOSYNE_MCP_TOKEN")
            or os.environ.get("MNEMOSYNE_TOKEN")
            or _auth_token)


def _request_token(req):
    """Extract the client-supplied auth token from a JSON-RPC request."""
    if not isinstance(req, dict):
        return None
    # 1) MCP 标准：请求级 _meta.authToken
    meta = req.get("_meta")
    if isinstance(meta, dict) and meta.get("authToken"):
        return meta["authToken"]
    # 2) params._meta.authToken
    params = req.get("params")
    if isinstance(params, dict):
        pm = params.get("_meta")
        if isinstance(pm, dict) and pm.get("authToken"):
            return pm["authToken"]
    # 3) 顶层 authToken / token 字段
    for key in ("authToken", "token"):
        val = req.get(key)
        if val:
            return val
    return None


def _authorized(req):
    """Return True if the request is authorized.

    🔴 2026-10-22 安全加固（fail-open → fail-closed）：
    原实现是「没配令牌就当已授权」—— 一次变量名笔误或 secrets 被误删，
    整个 MCP 面就变成无鉴权公网端口。现在没有令牌 = 一律拒绝。
    宁可服务不可用，也不能静默裸奔。
    """
    expected = _get_expected_token()
    if not expected:
        print("[warn] no MCP token configured — refusing the request (fail-closed).",
              flush=True)
        return False
    supplied = _request_token(req)
    return bool(supplied) and hmac.compare_digest(str(supplied), str(expected))

def _ensure_brain(brain_dir=None, namespace=None, actor="mcp"):
    """Ensure a brain is initialised for *namespace*.

    Different namespaces get different brain instances (and different Db files).
    """
    global brain
    if brain_dir is None:
        brain_dir = _brain_dir or DEFAULT_BRAIN
    ns = namespace or _default_namespace
    key = (brain_dir, ns)
    cache_key = getattr(_ensure_brain, "_cache_key", None)
    # Use namespace-scoped brain cache
    if not hasattr(_ensure_brain, "_brains"):
        _ensure_brain._brains = {}
    if key not in _ensure_brain._brains:
        b = MemoryBrain(brain_dir, namespace=ns, actor=actor)
        b.ensure_init()
        _ensure_brain._brains[key] = b
    brain = _ensure_brain._brains[key]
    return brain

def _get_ns(arguments):
    """Extract namespace from per-request arguments."""
    return arguments.get("namespace") if isinstance(arguments, dict) else None

# ---------- 18 tools (includes audit + forget + namespace + recall_health) ----------
TOOLS = [
    {"name":"retain","description":"写入记忆。content: 内容; mtype: 类型; tags: 标签数组（如 [\"偏好\",\"项目\"]）; confidence: 可信度0-1; importance: 重要性1-5; supersedes: 本记忆所更正的旧记忆id（更正场景，写入后旧记忆被标记 superseded）; namespace: 多租户隔离; project: 可选项目隔离",
     "inputSchema":{"type":"object","properties":{"content":{"type":"string"},"mtype":{"type":"string","enum":["semantic","episodic","procedural","preference","identity","lesson","strategy","reflective"],"default":"semantic"},"tags":{"type":"array","items":{"type":"string"}},"confidence":{"type":"number"},"importance":{"type":"integer"},"supersedes":{"type":"string"},"project":{"type":"string"},"namespace":{"type":"string"}},"required":["content"]}},
    {"name":"recall","description":"检索记忆。query: 查询; k: 返回条数; namespace: 多租户隔离; project: 可选项目隔离; budget_tokens: 可选上下文预算（token），设定后返回 {results, cost_report}，cost_report 含 budget_limit/budget_used/dropped_count/truncated；compress/compress_level: 可选智能压缩。返回项含 memory_id（可直接用于 forget / retain(supersedes=)）、verification（superseded/outdated 表示该条已被更新的说法取代，不得当作当前事实引用）与 confidence_band（high/normal/low，low 已在服务端被相关性下限剔除，正常不会出现）",
     "inputSchema":{"type":"object","properties":{"query":{"type":"string"},"k":{"type":"integer","default":5},"namespace":{"type":"string"},"project":{"type":"string"},"mtype":{"type":"string","description":"按记忆类型过滤（如 preference/semantic/identity）"},"tags":{"type":"array","items":{"type":"string"},"description":"标签数组过滤（任一命中即保留，OR 语义；与 mtype 为 AND 关系）"},"budget_tokens":{"type":"integer","description":"上下文预算（token）。设定后返回 {results, cost_report}，按分数降序逐条装箱，绝不超预算"},"compress":{"type":"boolean","default":False},"compress_level":{"type":"integer","default":2}},"required":["query"]}},
    {"name":"forget","description":"遗忘/删除记忆：把目标记忆的 confidence 降为 0 并标记 status=deleted（默认软遗忘，审计链与可信度轨迹保留、可回溯）。memory_id 与 query 二选一：给 query 时先按相关性解析目标。建议先用 dry_run=true 把候选报给用户确认，再执行。evict=true 为物理删除。",
     "inputSchema":{"type":"object","properties":{"memory_id":{"type":"string"},"query":{"type":"string"},"k":{"type":"integer","default":3},"dry_run":{"type":"boolean","default":False},"evict":{"type":"boolean","default":False},"reason":{"type":"string"},"namespace":{"type":"string"}},"required":[]}},
    {"name":"stats","description":"运行统计——写入/召回/Token节省等全维度；namespace: 可选",
     "inputSchema":{"type":"object","properties":{"namespace":{"type":"string"}}}},
    {"name":"graph_query","description":"知识图谱查询；namespace: 可选",
     "inputSchema":{"type":"object","properties":{"entity":{"type":"string"},"namespace":{"type":"string"}},"required":["entity"]}},
    {"name":"retain_batch","description":"批量写入（15x加速）；items 每项可为 {content, mtype, tags, confidence, importance}；namespace: 可选",
     "inputSchema":{"type":"object","properties":{"items":{"type":"array","items":{"type":"object","properties":{"content":{"type":"string"},"mtype":{"type":"string","default":"semantic"},"tags":{"type":"array","items":{"type":"string"}},"confidence":{"type":"number"},"importance":{"type":"integer"}},"required":["content"]}},"namespace":{"type":"string"},"project":{"type":"string"}},"required":["items"]}},
    {"name":"doctor","description":"健康检查——扫描记忆库完整性、记录数、磁盘，并给出向量后端写/删成败记账（vector_ops）、向量插件实时健康度与熔断器状态（vector_backend）、图边规模与回填状态（graph）；namespace: 可选",
     "inputSchema":{"type":"object","properties":{"namespace":{"type":"string"}}}},
    {"name":"recall_health","description":"召回质量只读指标——调用次数/空结果率/触发相关性下限被剔除的条数/平均返回条数/平均top1分/置信带分布/通道命中分布/延迟分位/向量后端成败/非法字符替换计数/胶囊缓存命中率/图边规模/最近20轮明细。用于判断'召回是否随会话时长退化'；namespace: 可选",
     "inputSchema":{"type":"object","properties":{"namespace":{"type":"string"}}}},
    {"name":"capsule","description":"取单条记忆的【记忆胶囊】：在给定预算内把一条记忆压成 指针+结构化事实+内容原子（数字/日期/金额/型号/URL/邮箱/引号原话）。原子在任何层级都完整保留，因此预算极小时仍能回答'多少钱/什么时候/哪个型号'这类追问；需要原文再用 expand。memory_id 或 ref 二选一；budget_tokens 默认 60",
     "inputSchema":{"type":"object","properties":{"memory_id":{"type":"string"},"ref":{"type":"string"},"budget_tokens":{"type":"integer","default":60},"namespace":{"type":"string"}},"required":[]}},
    {"name":"expand","description":"按指针取回原文（精确展开）——压缩的逆运算，逐字回传原始内容并校验内容哈希。ref 形如 m:<id>#<hash>[@级别]，来自 recall/capsule 返回的 ref 或显示指针；namespace: 可选",
     "inputSchema":{"type":"object","properties":{"ref":{"type":"string"},"namespace":{"type":"string"}},"required":["ref"]}},
    {"name":"temporal_query","description":"时序查询——按时间排序返回版本链；namespace: 可选",
     "inputSchema":{"type":"object","properties":{"entity":{"type":"string"},"namespace":{"type":"string"}},"required":[]}},
    {"name":"list_projects","description":"列出所有项目名（多项目隔离）；namespace: 可选",
     "inputSchema":{"type":"object","properties":{"namespace":{"type":"string"}}}},
    {"name":"audit","description":"Audit trail 查询；memory_id: 目标id; namespace: 可选",
     "inputSchema":{"type":"object","properties":{"memory_id":{"type":"string"},"namespace":{"type":"string"}},"required":["memory_id"]}},
    {"name":"confidence_history","description":"Confidence trajectory 查询；memory_id: 目标id; namespace: 可选",
     "inputSchema":{"type":"object","properties":{"memory_id":{"type":"string"},"namespace":{"type":"string"}},"required":["memory_id"]}},
    # 导入导出三个工具的名字在 8.1.1 由 `memory/xxx` 改为 `memory_xxx`。
    # 为什么改：DeepSeek Harness 的 dsh-mcp-client 把 MCP 工具名投影成公开名时，
    # 只允许 [A-Za-z0-9_-]，`/` 会被换成 `_` 并**追加 12 位 SHA-256 后缀**
    # （见 publicToolName）。于是客户端看到的是 `memory_export-v1_251be4a7ac40`
    # 这种名字，与文档、控制台目录里的 `memory/export-v1` 对不上，数工具时
    # 会以为「少了一个」。
    # 改名是**纯增量**：旧名继续注册为别名，两个名字都接受，老客户端不破。
    # 详见 src/CHANGELOG.md 8.1.1。
    {"name":"memory_export-v1","description":"Export all active memories to JSONL + manifest.json (Memory Exchange Protocol)。namespace: 可选; filepath: 导出目录路径",
     "inputSchema":{"type":"object","properties":{"namespace":{"type":"string"},"filepath":{"type":"string"}},"required":["filepath"]}},
    {"name":"memory/export-v1","description":"Export all active memories to JSONL + manifest.json (Memory Exchange Protocol)。namespace: 可选; filepath: 导出目录路径。Deprecated alias of memory_export-v1 —— 保留以兼容旧客户端",
     "inputSchema":{"type":"object","properties":{"namespace":{"type":"string"},"filepath":{"type":"string"}},"required":["filepath"]}},
    {"name":"memory_import-v1","description":"Import memories from JSONL + manifest.json (Memory Exchange Protocol)。namespace: 可选; filepath: 导入目录路径",
     "inputSchema":{"type":"object","properties":{"namespace":{"type":"string"},"filepath":{"type":"string"}},"required":["filepath"]}},
    {"name":"memory/import-v1","description":"Import memories from JSONL + manifest.json (Memory Exchange Protocol)。namespace: 可选; filepath: 导入目录路径。Deprecated alias of memory_import-v1 —— 保留以兼容旧客户端",
     "inputSchema":{"type":"object","properties":{"namespace":{"type":"string"},"filepath":{"type":"string"}},"required":["filepath"]}},
    {"name":"memory_claim","description":"Claim memories from an external export (import-and-merge logic)。namespace: 可选; filepath: 导入目录路径",
     "inputSchema":{"type":"object","properties":{"namespace":{"type":"string"},"filepath":{"type":"string"}},"required":["filepath"]}},
    {"name":"memory/claim","description":"Claim memories from an external export (import-and-merge logic)。namespace: 可选; filepath: 导入目录路径。Deprecated alias of memory_claim —— 保留以兼容旧客户端",
     "inputSchema":{"type":"object","properties":{"namespace":{"type":"string"},"filepath":{"type":"string"}},"required":["filepath"]}},
    {"name":"consolidate","description":"压缩引擎·记忆合并：把高度相似/同义的记忆聚合并合并成一条代表性记忆，原记忆标记 consolidated。min_similarity: 相似度阈值(默认0.6); max_group: 每组最大条数(默认5); generate_summary: 是否生成合并摘要(默认true); dry_run: 仅统计不执行(默认false); namespace: 可选",
     "inputSchema":{"type":"object","properties":{"dry_run":{"type":"boolean","default":False},"min_similarity":{"type":"number","default":0.6},"max_group":{"type":"integer","default":5},"generate_summary":{"type":"boolean","default":True},"namespace":{"type":"string"}},"required":[]}},
    {"name":"reflect","description":"压缩引擎·增强反思：统计记忆总量/类型/层/事实类型/可信度分布、抽取高频实体、检测事实冲突、时序密度；deep=true 额外做认知模式发现。question: 可选聚焦问题; namespace: 可选",
     "inputSchema":{"type":"object","properties":{"question":{"type":"string"},"deep":{"type":"boolean","default":False},"namespace":{"type":"string"}},"required":[]}},
    {"name":"dedup","description":"压缩引擎·去重：基于内容指纹+向量/词频相似度检测重复与近似记忆，dry_run=true 仅报告不删除(默认false); namespace: 可选",
     "inputSchema":{"type":"object","properties":{"dry_run":{"type":"boolean","default":False},"namespace":{"type":"string"}},"required":[]}},
    # ---- v8.1.0 记忆形成层（9 个新工具）------------------------------------
    {"name":"session_summarize","description":"生成或增量更新【会话摘要】——七要素（目标/约束/决策/未决/产物/失败/偏好）+ 锚点原文保留；未决事项会独立成条。解决 8.0.0「只存原子事实、无法回答上次聊到哪」的空白。session_id: 会话标识; messages: 消息数组 [{role,content}] 或整段文本; rebuild=true 丢弃旧摘要从原文重建（抗漂移）",
     "inputSchema":{"type":"object","properties":{"session_id":{"type":"string"},"messages":{"type":["array","string"]},"rebuild":{"type":"boolean","default":False},"write_pending":{"type":"boolean","default":True},"project":{"type":"string"},"namespace":{"type":"string"}},"required":["session_id","messages"]}},
    {"name":"session_recall","description":"召回会话摘要与未决事项。query 为空时返回最近摘要；include_pending=true 时同时返回未决条目（用于下次对话主动追问）",
     "inputSchema":{"type":"object","properties":{"query":{"type":"string"},"k":{"type":"integer","default":3},"include_pending":{"type":"boolean","default":True},"project":{"type":"string"},"namespace":{"type":"string"}},"required":[]}},
    {"name":"conflict_resolve","description":"【冲突消解】六类判定（时间更新/偏好变更/作用域差异/精度差异/逻辑矛盾/来源冲突）+ 四操作（ADD/NOOP/UPDATE/KEEP_BOTH）。默认 dry_run=true 只给计划；apply=true 时把旧条目置 superseded（不删除）。scope=audit 时全库审计，只报真冲突（共享实体且共享谓词），修复 8.0.0 把实体共现误判为冲突的问题",
     "inputSchema":{"type":"object","properties":{"content":{"type":"string"},"mtype":{"type":"string","default":"semantic"},"dry_run":{"type":"boolean","default":True},"apply":{"type":"boolean","default":False},"scope":{"type":"string","enum":["single","audit"],"default":"single"},"project":{"type":"string"},"namespace":{"type":"string"}},"required":[]}},
    {"name":"task_record","description":"记录【任务事实】（Working Memory 事实层，只追加不改写）。goal: 目标; goal_type: 任务类别（同类检索的关键）; context: {constraints, preconditions}; steps: [{action,result,reason}]; outcome: success/partial/failed/rolled_back",
     "inputSchema":{"type":"object","properties":{"goal":{"type":"string"},"goal_type":{"type":"string","default":"general"},"context":{"type":"object"},"steps":{"type":"array","items":{"type":"object"}},"artifacts":{"type":["array","string"]},"outcome":{"type":"string","default":"success"},"duration_min":{"type":"number"},"rework_count":{"type":"integer"},"user_feedback":{"type":"string","default":"unknown"},"project":{"type":"string"},"namespace":{"type":"string"}},"required":["goal"]}},
    {"name":"playbook_save","description":"沉淀【经验】（Working Memory 经验层）。pattern: 可复用方法; preconditions 必填三件套 {must, must_not, typical_env}——不带适用条件的经验会在条件不匹配时把 Agent 带沟里; failure_modes: [{when,why}] 已知失效场景; anti_patterns: 反模式。\n\n【回写模式】传 memory_id 时不新建，而是记一次应用结果：success 更新成功率，deviation 记录「这次为什么没按它做」（principle 7.4 第 05 步：允许偏离并把偏离点作为经验进化的数据源）。返回 should_demote=true 表示成功率已跌破阈值，应降级不再主动注入。",
     "inputSchema":{"type":"object","properties":{"pattern":{"type":"string"},"goal_type":{"type":"string","default":"general"},"preconditions":{"type":"object"},"steps":{"type":"array","items":{"type":"string"}},"failure_modes":{"type":"array","items":{"type":"object"}},"anti_patterns":{"type":"array","items":{"type":"string"}},"scope":{"type":"object"},"evidence":{"type":"array","items":{"type":"string"}},"memory_id":{"type":"string","description":"回写模式：要回写的经验记忆 id"},"success":{"type":"boolean","default":True,"description":"回写模式：本次应用是否成功"},"deviation":{"type":"string","description":"回写模式：偏离点与原因（没按建议做的部分）"},"project":{"type":"string"},"namespace":{"type":"string"}},"required":[]}},
    {"name":"playbook_recall","description":"检索候选经验并做【前置条件校验】。返回项带 applicable/blocked_by：只应把 applicable=true 的注入上下文，其余丢弃（防负迁移）。context: 当前任务环境描述，用于 must/must_not 比对",
     "inputSchema":{"type":"object","properties":{"goal_type":{"type":"string"},"context":{"type":"object"},"k":{"type":"integer","default":3},"project":{"type":"string"},"namespace":{"type":"string"}},"required":[]}},
    {"name":"code_change_record","description":"记录【代码变更事件】（Coding Memory）——存 git 缺失的三样东西：意图、约束、放弃的方案。intent: 这次改动想解决什么; constraints: 当时约束; files/symbols: 影响面（符号级锚点）; rejected_approaches: [{what,why_not}]; outcome: merged/reverted/superseded; lesson: 一句话教训",
     "inputSchema":{"type":"object","properties":{"intent":{"type":"string"},"repo":{"type":"string"},"commit":{"type":"string"},"parent":{"type":"string","description":"上一个 change_id，构成演进链"},"intent_type":{"type":"string","enum":["feature","bugfix","refactor","perf","chore","revert"],"default":"feature"},"constraints":{"type":["array","string"]},"linked":{"type":"object"},"files":{"type":["array","string"]},"symbols":{"type":["array","string"]},"impact":{"type":["array","string"]},"diff_summary":{"type":"string"},"rejected_approaches":{"type":"array","items":{"type":"object"}},"review_notes":{"type":"array","items":{"type":"object"}},"outcome":{"type":"string","default":"merged"},"lesson":{"type":"string"},"project":{"type":"string"},"namespace":{"type":"string"}},"required":["intent"]}},
    {"name":"code_archaeology","description":"【代码考古 / 坑位预警】。target 传文件路径或符号名：返回该锚点的变更链、最近 3 次意图、是否被回滚过、已知约束。intent 传任务意图时转为坑位预警，返回历史失败与放弃方案。改动前先跑一次，成本约 500 token",
     "inputSchema":{"type":"object","properties":{"target":{"type":"string"},"intent":{"type":"string"},"files":{"type":["array","string"]},"repo":{"type":"string"},"k":{"type":"integer","default":8},"project":{"type":"string"},"namespace":{"type":"string"}},"required":[]}},
    {"name":"memory_policy","description":"【记忆策略】敏感度分级与来源闸门。action=classify 判定一段内容的敏感度（normal/private/secret）与是否允许写入长期偏好; action=check 做写入准入判定; action=audit 扫描全库，列出以 secret 级内容明文入库的条目; action=filter 查看或开关召回闸门（默认开启，只拦 secret 级）",
     "inputSchema":{"type":"object","properties":{"action":{"type":"string","enum":["classify","check","audit","filter"],"default":"classify"},"content":{"type":"string"},"source_type":{"type":"string"},"mtype":{"type":"string"},"allow_secret":{"type":"boolean","default":False},"enabled":{"type":"boolean"},"namespace":{"type":"string"}},"required":[]}},
    # ---- v8.1.0 记忆运行层（9 个新工具）------------------------------------
    # 与上面"形成层"（写入/摘要/冲突/任务/经验/变更/策略）互补，
    # 这一组覆盖**读取与维护**：画像、注入、目录、信封、巩固、度量、
    # 级联删除、掩码、主题摘要与回溯。
    {"name":"profile_card","description":"【用户画像卡】三段分级（稳定身份 / 偏好与约束 / 当前情境）+ 未决事项，可直接拼进 system prompt。max_tokens 为硬上限：超了按「情境→未决→偏好」逐段裁，绝不截断单条（半句事实比没有更危险）。include_context=false 得到约 200 token 的精简版",
     "inputSchema":{"type":"object","properties":{"include_context":{"type":"boolean","default":True},"include_pending":{"type":"boolean","default":True},"max_tokens":{"type":"integer"},"namespace":{"type":"string"}},"required":[]}},
    {"name":"memory_inject","description":"【上下文注入流水线】五步：查询构造→四路召回→RRF 融合→重排→预算内注入。返回可直接注入的 text + 四段配额账目（画像30% / 任务相关35% / 经验20% / 未决15%）。超预算的条目不截断、直接不注入。比 recall 更适合「给模型准备上下文」",
     "inputSchema":{"type":"object","properties":{"query":{"type":"string"},"budget_tokens":{"type":"integer","default":1200},"goal":{"type":"string"},"recent_turns":{"type":"array","items":{"type":"string"}},"entities":{"type":"array","items":{"type":"string"}},"project":{"type":"string"},"mode":{"type":"string","enum":["rrf","weighted"],"default":"rrf"},"include_profile":{"type":"boolean","default":True},"namespace":{"type":"string"}},"required":["query"]}},
    {"name":"recall_catalog","description":"【渐进披露·目录层】只返回标题+一句话+展开成本，不返回正文。先看目录判断哪几条相关，再对 1~2 条调 expand_memory 取全文——注入成本从 O(全部) 降到 O(目录+少量)。预算紧张时优先用这个而不是 recall",
     "inputSchema":{"type":"object","properties":{"query":{"type":"string"},"k":{"type":"integer","default":10},"project":{"type":"string"},"namespace":{"type":"string"}},"required":["query"]}},
    {"name":"expand_memory","description":"【渐进披露·展开】取一条记忆的正文 + 统一信封 + 图邻居 + 证据链。与 recall_catalog 配对使用。邻居来自知识图谱：回答「当时这个决定影响了哪些模块」时，答案在邻居里而不在正文里",
     "inputSchema":{"type":"object","properties":{"memory_id":{"type":"string"},"include_neighbors":{"type":"boolean","default":True},"namespace":{"type":"string"}},"required":["memory_id"]}},
    {"name":"memory_envelope","description":"【统一记忆信封】单条记忆的八类分类（semantic/episodic/procedural/working/resource/anti_pattern/meta/relational）+ 四维 scope（user/project/agent/session）+ 证据链 + 双时间轴（t_valid/t_invalid）+ TTL + SPO 结构。memory_id 省略时返回全库八类分布统计",
     "inputSchema":{"type":"object","properties":{"memory_id":{"type":"string"},"namespace":{"type":"string"}},"required":[]}},
    {"name":"memory_consolidate","description":"【巩固三路径】① 情景→语义（同一事实在 ≥3 个不同会话出现）② 情景→程序性（同类任务成功 ≥2 次 → 归纳带前置条件的 playbook）③ 失败→负记忆（failed/rolled_back/reverted → 禁忌条目）。幂等：同一批源不会重复生成。paths 可只跑其中几条",
     "inputSchema":{"type":"object","properties":{"dry_run":{"type":"boolean","default":False},"paths":{"type":"array","items":{"type":"string","enum":["semantic","procedural","anti_pattern"]}},"namespace":{"type":"string"}},"required":[]}},
    {"name":"memory_metrics","description":"【度量指标】Chat 5 项 + Working 4 项 + Coding 4 项。**诚实原则**：数据不足的指标返回 {value:null, status:\"insufficient_data\", need:\"...\"} 而不是填 0——0 和「没数据」是两件事。domain 可选 chat/working/coding，省略返回全部",
     "inputSchema":{"type":"object","properties":{"domain":{"type":"string","enum":["chat","working","coding"]},"namespace":{"type":"string"}},"required":[]}},
    {"name":"memory_cascade_delete","description":"【级联删除】四动作：软删原始 → 失效派生结论（置信度减半 + superseded）→ 清检索缓存 → 依赖摘要标 stale。删除前建议先跑 dry_run=true 看 dependents。reason 会写进审计。hard=true 才物理删除",
     "inputSchema":{"type":"object","properties":{"memory_id":{"type":"string"},"hard":{"type":"boolean","default":False},"dry_run":{"type":"boolean","default":False},"reason":{"type":"string"},"namespace":{"type":"string"}},"required":["memory_id"]}},
    {"name":"memory_mask","description":"【掩码】把正文替换为占位符但保留记录骨架——比删除更温和，且保持上下文长度与前缀不变（不破坏 KV-Cache 复用）。用户说「别记这个」时优先用掩码。action=unmask 可撤销（需当初 keep_original=true）",
     "inputSchema":{"type":"object","properties":{"memory_id":{"type":"string"},"action":{"type":"string","enum":["mask","unmask"],"default":"mask"},"replacement":{"type":"string"},"keep_original":{"type":"boolean","default":True},"namespace":{"type":"string"}},"required":["memory_id"]}},
    {"name":"topic_summarize","description":"【L3 主题摘要】把同一 project 下的多条 L2 会话摘要合并成「这个项目现在处于什么状态」。合并规则确定性：goal 取最新、decisions 累积、pending 只留最新。persist=true 落库并记录 pointer.children，供 memory_trace 回溯",
     "inputSchema":{"type":"object","properties":{"project":{"type":"string"},"persist":{"type":"boolean","default":False},"max_sessions":{"type":"integer","default":20},"namespace":{"type":"string"}},"required":[]}},
    {"name":"memory_trace","description":"【指针链回溯】沿 pointer 链 L3→L2→L1 回溯，返回每层内容、覆盖的轮次区间与 message_ids。回答「这条结论是从哪句话来的」。记忆库不存 L0 原文，调用方拿 message_ids 去自己的对话历史取",
     "inputSchema":{"type":"object","properties":{"memory_id":{"type":"string"},"max_depth":{"type":"integer","default":3},"namespace":{"type":"string"}},"required":["memory_id"]}},
    {"name":"memory_hot_path","description":"【热路径缓冲】action=enable 开启异步写入（热路径只入队，抽取/冲突/巩固交后台）；action=status 查看队列与统计；action=flush 等队列落盘（读一致性屏障——写完立刻读之前必须 flush）",
     "inputSchema":{"type":"object","properties":{"action":{"type":"string","enum":["enable","status","flush"],"default":"status"},"max_queue":{"type":"integer","default":2000},"batch_size":{"type":"integer","default":32},"namespace":{"type":"string"}},"required":[]}},
]

# ---- client API tools -------------------------------------------------
# Appended rather than interleaved so the native tool indices stay stable for any
# client that cached tools/list.  Imported defensively: a problem in the
# compatibility module must not take the native tools down with it.
try:
    from .mcp_api import TOOLS as _API_TOOLS
    from .mcp_api import TOOL_NAMES as _API_TOOL_NAMES
except Exception as _api_import_error:  # pragma: no cover - defensive
    _API_TOOLS, _API_TOOL_NAMES = [], frozenset()

TOOLS = TOOLS + list(_API_TOOLS)
NATIVE_TOOL_COUNT = len(TOOLS) - len(_API_TOOLS)


def handle_tools_list():
    return {"tools": TOOLS}


# ═══════════════════════════════════════════════════════════════════════════
# 订阅门控（v8.1.1 SaaS 层）—— **入口准入 + 出口过滤**，引擎零改动
# ═══════════════════════════════════════════════════════════════════════════
# 设计取舍（为什么用「包一层」而不是「在每个分支里插 if」）：
#
#   · 40 个工具分支里逐个插判断 = 40 个可能写漏的地方，且新加工具必然忘记；
#   · 包一层只有**一个**判定点，新工具自动被覆盖（默认放行，符合"trial period
#     要让人写得进去"的策略）；
#   · 出口过滤只碰**返回值**，不碰检索参数、不碰 SQL、不碰存储 ——
#     即使门控逻辑写错，最坏结果是"少返回几条"，绝不会删数据或污染索引。
#
# 三层判定：
#   ① 准入（check_tier_gate）：目前只有 Free 层跨 namespace 会被硬拒绝；
#   ② 出口过滤（filter_*）：召回/审计/导出按窗口收窄；
#   ③ 数据信号（upgrade_hint）：告诉调用方"有多少条被窗口排除"，不是广告。
#
# 总开关 MNEMOSYNE_TIER_GATE：未设置 = 完全跳过（自部署 / PyPI 用户行为不变）。

def _tier_context(arguments):
    """从本次调用参数里取出身份上下文。

    线上由 8788 网关把内部头（X-Mnemosyne-Tier 等）注入成参数；
    直连 stdio 的用户不会带这些键，于是 resolve_tier 回落到环境变量/Free。
    """
    if not isinstance(arguments, dict):
        return {}
    ctx = {}
    for key in ("_tier", "_namespace", "_allowed_namespaces"):
        if key in arguments:
            ctx[key[1:]] = arguments[key]
    return ctx


def _extract_records_for_gate(result, tool_name):
    """从工具返回值里取出「可以被窗口过滤的记录列表」。

    不同工具的返回形状差异很大，这里只处理**已知且确定**的几种；
    认不出的形状原样放行 —— 门控宁可少拦，也不能把正常结果吞掉。
    """
    if not isinstance(result, dict):
        return None
    if isinstance(result.get("results"), list):
        return "results"
    if isinstance(result.get("items"), list):
        return "items"
    if isinstance(result.get("memories"), list):
        return "memories"
    return None


def _make_record_lookup(arguments):
    """构造「按 memory_id 回查真实记录」的闭包。

    为什么需要它：召回投影里**没有 tier 字段**（见 tiers.filter_recall_items
    的文档）。门控要判断"这条是不是冷层"，只能回存储拿原始记录。

    命名空间必须与本次调用一致 —— 用 arguments['namespace']，
    而不是全局 _default_namespace，否则跨 ns 调用会查到错的库。
    """
    if _tiers is None:
        return None
    ns = None
    if isinstance(arguments, dict):
        ns = arguments.get("namespace")
    try:
        brain = _ensure_brain(namespace=ns)
        store = brain.store
    except Exception:
        return None
    if store is None or not hasattr(store, "find_by_id"):
        return None

    def _lookup(memory_id):
        try:
            return store.find_by_id(memory_id)
        except Exception:
            return None

    return _lookup


def _apply_tier_gate(name, arguments, result):
    """出口门控：按层级收窄返回值。返回（可能被修改的）result。"""
    if _tiers is None or not _tiers.gate_enabled():
        return result
    if not isinstance(result, dict) or result.get("error"):
        return result

    ctx = _tier_context(arguments)
    tier = _tiers.resolve_tier(ctx)

    # ---- ① 召回类：冷层时间窗口 ----
    if name in _tiers.RECALL_TOOLS:
        key = _extract_records_for_gate(result, name)
        if key:
            # 投影不回传 tier，必须按 memory_id 回查存储 —— 见 tiers.py
            # filter_recall_items 的文档（2026-10-23 实测踩过这个坑）。
            lookup = _make_record_lookup(arguments)
            kept, dropped = _tiers.filter_recall_items(
                result[key], tier, record_lookup=lookup)
            if dropped:
                result[key] = kept
                hint = _tiers.upgrade_hint(tier, dropped, "cold")
                if hint:
                    # PROMO_STRATEGY: 不是广告，是数据信号 —— 只陈述"被排除了几条"
                    result["upgrade_hint"] = hint
        return result

    # ---- ② 审计类：按时间窗口 ----
    if name in _tiers.AUDIT_TOOLS:
        for key in ("entries", "history", "trail"):
            if isinstance(result.get(key), list):
                kept, dropped = _tiers.filter_audit_entries(result[key], tier)
                if dropped:
                    result[key] = kept
                    if isinstance(result.get("count"), int):
                        result["count"] = len(kept)
                    hint = _tiers.upgrade_hint(tier, dropped, "audit")
                    if hint:
                        result["upgrade_hint"] = hint
                break
        return result

    # ---- ③ 导出类：热层 + 窗口内冷层 ----
    # 导出的过滤发生在**文件写出之前**，所以不能在这里做 —— 见
    # `_export_with_tier_gate()`：它先过滤记录再交给引擎落盘。
    return result


def _export_with_tier_gate(b, arguments, ns):
    """带门控的导出。

    为什么不能复用 brain.export_memories()：它内部直接遍历
    store.all_records() 写 JSONL，没有任何注入点。所以这里自己做同样的
    落盘动作（同一 schema、同一 manifest 字段），只在写之前过滤记录。
    **不修改引擎**，也不产生第二种导出格式 —— manifest 里额外写
    `tier_gate` 字段，让用户明确知道"这份导出被窗口收窄过"。
    """
    import time as _time
    filepath = arguments.get("filepath")
    if not filepath:
        return {"error": "filepath parameter required"}

    ctx = _tier_context(arguments)
    tier = _tiers.resolve_tier(ctx)
    cfg = _tiers.tier_config(tier)

    os.makedirs(filepath, exist_ok=True)
    records = [r for r in b.store.all_records()
               if r.get("status") in (None, "active")]
    total_before = len(records)
    kept, dropped = _tiers.filter_export_records(records, tier)

    jsonl_path = os.path.join(filepath, "memories.jsonl")
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for r in kept:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    ledger_hash = ""
    if getattr(b, "ledger", None) is not None:
        try:
            v = b.ledger.verify_chain()
            ledger_hash = "valid:%s:%s" % (v.get("valid"), v.get("total", 0))
        except Exception:
            pass

    manifest = {
        "schema_version": "1.0",
        "export_timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S"),
        "namespace": ns or _default_namespace,
        "latest_ledger_hash": ledger_hash,
        "record_count": len(kept),
        "format": "jsonl",
    }
    if dropped:
        # FREE_TIER: export = hot/warm + cold within 30d; nothing was deleted
        manifest["tier_gate"] = {
            "tier": tier,
            "window_days": cfg.get("export_window_days"),
            "excluded_count": dropped,
            "total_before_gate": total_before,
            "note": ("%d cold-tier memories fall outside the %s-day Free export "
                     "window. They remain stored and were not deleted."
                     % (dropped, cfg.get("export_window_days"))),
        }
    manifest_path = os.path.join(filepath, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    out = {"result": "success", "record_count": len(kept),
           "exported_path": jsonl_path, "manifest_path": manifest_path}
    if dropped:
        out["excluded_count"] = dropped
        out["upgrade_hint"] = _tiers.upgrade_hint(tier, dropped, "export")
    return out


def handle_tools_call(name, arguments):
    """工具调用入口 —— 门控包装层。

    准入 → 执行 → 出口过滤，三段式。任何一段抛异常都不能让工具整体失败：
    门控是**附加价值**，不是可用性的前置条件。
    """
    if not isinstance(arguments, dict):
        arguments = {}

    # ---- ① 准入 ----
    if _tiers is not None:
        try:
            allowed, reason = _tiers.check_tier_gate(
                name, arguments, _tier_context(arguments))
            if not allowed:
                return {"error": reason or "tier_gate_denied",
                        "code": "TIER_GATE_001", "tool": name}
        except Exception:
            pass  # 门控自身故障 → 放行（宁可多给，不可误拦）

    # ---- ② 执行 ----
    result = _handle_tools_call_raw(name, arguments)

    # ---- ③ 出口过滤 ----
    try:
        return _apply_tier_gate(name, arguments, result)
    except Exception:
        return result


def _handle_tools_call_raw(name, arguments):
    # client API tools route through the compatibility layer, which keeps its
    # own Memory instance and its own four-dimension physical scoping.  Checked
    # first because the compatible names are exact and unambiguous.
    if name in _API_TOOL_NAMES:
        try:
            from .mcp_api import handle as _api_handle
            return _api_handle(name, arguments if isinstance(arguments, dict) else {})
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}",
                    "code": "INTERNAL_001", "tool": name}

    ns = _get_ns(arguments)
    b = _ensure_brain(namespace=ns)
    project = arguments.get("project", "")
    actor = arguments.get("actor", "mcp")

    if name == "retain":
        content = arguments["content"]
        mtype = arguments.get("mtype", "semantic")
        kwargs = {}
        for kw in ("confidence", "importance", "source", "context", "fact_type",
                    "source_type", "expires_at", "tags", "supersedes"):
            if kw in arguments and arguments[kw] is not None:
                kwargs[kw] = arguments[kw]
        mid = b.retain(content, mtype=mtype, fast=True, project=project, **kwargs)
        rec = b.store.find_by_id(mid)
        result = {"memory_id": mid, "result": "已记住"}
        if rec:
            result["confidence"] = rec.get("confidence")
            result["flags"] = rec.get("flags", [])
        return result

    elif name == "recall":
        query, k = arguments["query"], arguments.get("k", 5)
        # v7.0.2（P2-1）：budget_tokens 必须在这里透传。
        # 7.0.1 的 MCP 层**根本没有把 budget_tokens 传给 brain** —— 于是
        # "按上下文预算取记忆"这条能力从工具侧完全不可达（工具 schema 里也没写），
        # brain 里那套预算装箱代码等于死代码。compress / compress_level 同理。
        _budget = arguments.get("budget_tokens")
        _kw = {}
        if _budget is not None:
            try:
                _kw["budget_tokens"] = max(1, int(_budget))
            except (TypeError, ValueError):
                _kw["budget_tokens"] = None
        if arguments.get("compress") is not None:
            _kw["compress"] = bool(arguments.get("compress"))
        if arguments.get("compress_level") is not None:
            try:
                _kw["compress_level"] = int(arguments.get("compress_level"))
            except (TypeError, ValueError):
                pass
        _raw = b.recall(query, k=k, project=project,
                        mtype=arguments.get("mtype"),
                        tags=arguments.get("tags"),
                        **_kw)
        # budget_tokens 设定时 brain 返回 (results, cost_report)；否则返回 list。
        # 两者都必须能正常走下面的投影，不能因为多了个元组就整段失效。
        if isinstance(_raw, tuple) and len(_raw) == 2 and isinstance(_raw[1], dict):
            results, _cost = _raw
        else:
            results, _cost = _raw, None
        out = []
        for r in results:
            try:
                # memory_id 必须回传：forget / retain(supersedes=) 都要用它定位目标。
                # 原实现不回传 id，导致 Agent 拿到结果也无法执行遗忘或更正。
                out.append({"memory_id": r[1].get("id") if isinstance(r[1], dict) else None,
                            "score": round(float(r[0]), 4),
                            "content": (r[1].get("content","") if isinstance(r[1], dict) else str(r[1]))[:300],
                            "type": r[1].get("type","semantic") if isinstance(r[1], dict) else "unknown",
                            "tags": r[1].get("tags", []) if isinstance(r[1], dict) else [],
                            "created_at": r[1].get("created_at","") if isinstance(r[1], dict) else "",
                            "version": r[1].get("version", 1) if isinstance(r[1], dict) else 1,
                            "confidence": r[1].get("confidence") if isinstance(r[1], dict) else None,
                            # verification 必须回传：更正后的旧记忆在库里是
                            # verification=superseded（检索层降权但不删除），
                            # 不回传的话 Agent 无从分辨"哪条说法已被取代"。
                            "verification": r[1].get("verification", "unverified") if isinstance(r[1], dict) else None,
                            "superseded_by": r[1].get("superseded_by") if isinstance(r[1], dict) else None,
                            # v7.0.2（P0-2）：置信带必须回传。7.0.1 只有归一化后的
                            # score（最高分恒为 1.0，无绝对含义），Agent 无法判断
                            # "这条到底有多相关"。confidence_band 由相关下限判定给出
                            # 语义（high = 语义距离足够近），可作引用门槛。
                            "confidence_band": (r[1].get("_relevance_band")
                                                if isinstance(r[1], dict) else None),
                            # v7.0.2（AIC）：胶囊信息。budget_tokens 生效时，放不进
                            # 预算的条目会被压成胶囊而非丢弃 —— ref 用于 expand 取原文。
                            "ref": ((r[1].get("_capsule") or {}).get("ref")
                                    if isinstance(r[1], dict) else None),
                            "capsule_level": ((r[1].get("_capsule") or {}).get("level")
                                              if isinstance(r[1], dict) else None),
                            "capsule_tokens": ((r[1].get("_capsule") or {}).get("tokens")
                                               if isinstance(r[1], dict) else None),
                            "lossless": ((r[1].get("_capsule") or {}).get("lossless")
                                         if isinstance(r[1], dict) else None),
                            "flags": r[1].get("flags", []) if isinstance(r[1], dict) else []})
            except (ValueError, TypeError, IndexError):
                pass
        # 预算账目随结果回传（P2-1）：让 Agent 知道"是不是被预算砍了"。
        if _cost is not None:
            _idx = _cost.get("precision_index") or []
            return {"results": out,
                    "cost_report": _cost,
                    # v7.0.2（AIC）：装不进正文预算的条目以**精准索引**形式回传
                    # （指针 + 关键原子 + 预览），调用方按需 expand，不占正文预算。
                    "precision_index": _idx,
                    "budget_note": ("预算已生效：按分数降序逐条装箱，"
                                    "tokens_consumed=%s/%s；原文装不下的条目已"
                                    "压成胶囊 %d 条、索引 %d 条%s"
                                    % (_cost.get("budget_used"), _cost.get("budget_limit"),
                                       _cost.get("capsule_count") or 0, len(_idx),
                                       "（受预算截断）" if _cost.get("truncated") else "")),
                    "capsule_note": ("胶囊只含指针/事实/原子，原子（数字·日期·金额·"
                                     "型号·URL）在任何层级都完整保留；需要原文请用 "
                                     "expand(ref)。")}
        return {"results": out}

    elif name == "capsule":
        # v7.0.2（AIC）：单条记忆的胶囊 —— 比 recall 更省（无需检索），
        # 比 expand 更省 token，且原子守恒。
        key = arguments.get("memory_id") or arguments.get("ref")
        if not key:
            return {"error": "memory_id 与 ref 至少提供一个"}
        try:
            return b.capsule(key, budget_tokens=arguments.get("budget_tokens", 60))
        except Exception as e:
            return {"error": str(e)}

    elif name == "expand":
        ref = arguments.get("ref")
        if not ref:
            return {"error": "ref 参数必填"}
        try:
            return b.expand(ref)
        except Exception as e:
            return {"error": str(e)}

    elif name == "recall_health":
        # v7.0.2（P2-2）：召回质量只读指标。
        # 为什么必须暴露给 Agent/运维：7.0.1 的"越聊越偏"是靠人工跑对照实验才挖出来的，
        # 没有指标面就只能靠用户感觉"最近不太准"。有了这个工具，任何一次会话结束后
        # 都能直接读到空结果率、top1 分趋势、相关性下限剔除量、延迟分位。
        try:
            health = b.recall_health()
            health["namespace"] = ns or _default_namespace
            return health
        except Exception as e:
            return {"error": str(e)}

    elif name == "stats":
        return b.stats_tracker.summary() if b.stats_tracker else {}

    elif name == "graph_query":
        try:
            entity = arguments.get("entity")
            result = b.graph_query(entity)
            return {"results": result} if not isinstance(result, dict) else result
        except Exception as e:
            return {"error": str(e)}

    elif name == "retain_batch":
        items = arguments["items"]
        # v7.0.0-MCP 修复：原实现把第三个元素写死成 {}，逐项的 confidence / tags /
        # importance 全部被丢弃 —— 于是"批量写入时设定 confidence 和 tags"这条规则
        # 在批量路径上静默失效。这里逐项透传给 _build_record。
        batch = []
        for it in items:
            kw = {}
            for k in ("confidence", "importance", "tags"):
                if it.get(k) is not None:
                    kw[k] = it[k]
            batch.append((it["content"], it.get("mtype", "semantic"), kw))
        b.retain_batch(batch, fast=True)
        return {"result": f"批量写入 {len(items)} 条",
                "with_tags": sum(1 for it in items if it.get("tags")),
                "with_confidence": sum(1 for it in items if it.get("confidence") is not None),
                "namespace": ns or _default_namespace}

    elif name == "forget":
        memory_id = arguments.get("memory_id")
        query = arguments.get("query")
        if not memory_id and not query:
            return {"error": "memory_id 与 query 至少提供一个"}
        try:
            report = b.forget_targets(
                memory_id=memory_id,
                query=query,
                k=arguments.get("k", 3),
                dry_run=bool(arguments.get("dry_run", False)),
                evict=bool(arguments.get("evict", False)),
                reason=arguments.get("reason") or "user_forget",
            )
            report["namespace"] = ns or _default_namespace
            return report
        except Exception as e:
            return {"error": str(e)}

    elif name == "doctor":
        try:
            return b.doctor()
        except Exception as e:
            return {"error": str(e)}

    elif name == "temporal_query":
        entity = arguments.get("entity")
        try:
            return {"results": b.temporal_query(entity=entity)}
        except Exception as e:
            return {"error": str(e)}

    elif name == "list_projects":
        try:
            projs = b.list_projects()
            return {"projects": projs, "count": len(projs), "namespace": ns or _default_namespace}
        except Exception as e:
            return {"error": str(e)}

    elif name == "audit":
        memory_id = arguments.get("memory_id")
        try:
            trail = b.audit(memory_id)
            return {"memory_id": memory_id, "namespace": ns or _default_namespace, "entries": trail, "count": len(trail)}
        except Exception as e:
            return {"error": str(e)}

    elif name == "confidence_history":
        memory_id = arguments.get("memory_id")
        try:
            history = b.store.get_confidence_history(memory_id) if hasattr(b.store, "get_confidence_history") else []
            return {"memory_id": memory_id, "namespace": ns or _default_namespace, "history": history, "count": len(history)}
        except Exception as e:
            return {"error": str(e)}

    # 8.1.1 起正名是 memory_export-v1 / memory_import-v1 / memory_claim；
    # 带斜杠的旧名作为 deprecated 别名继续接受（见上面 TOOLS 里的注释）。
    elif name in ("memory_export-v1", "memory/export-v1"):
        filepath = arguments.get("filepath")
        if not filepath:
            return {"error": "filepath parameter required"}
        # FREE_TIER: export = hot/warm + cold within 30d —— 门控在**写盘之前**
        # 过滤记录（引擎的 export_memories 没有注入点，见 _export_with_tier_gate）。
        # 未开启门控时（自部署）走原引擎实现，行为逐字节不变。
        if _tiers is not None and _tiers.gate_enabled():
            try:
                return _export_with_tier_gate(b, arguments, ns)
            except Exception as e:
                return {"error": str(e)}
        try:
            result = b.export_memories(filepath, namespace=ns or _default_namespace)
            return {"result": "success", **result}
        except Exception as e:
            return {"error": str(e)}

    elif name in ("memory_import-v1", "memory/import-v1"):
        filepath = arguments.get("filepath")
        if not filepath:
            return {"error": "filepath parameter required"}
        try:
            result = b.import_memories(filepath, namespace=ns or _default_namespace)
            return {"result": "success", **result}
        except Exception as e:
            return {"error": str(e)}

    elif name in ("memory_claim", "memory/claim"):
        filepath = arguments.get("filepath")
        if not filepath:
            return {"error": "filepath parameter required"}
        try:
            result = b.claim(filepath, namespace=ns or _default_namespace)
            return {"result": "success", **result}
        except Exception as e:
            return {"error": str(e)}

    elif name == "consolidate":
        try:
            report = b.consolidate(
                dry_run=bool(arguments.get("dry_run", False)),
                min_similarity=float(arguments.get("min_similarity", 0.6)),
                max_group=int(arguments.get("max_group", 5)),
                generate_summary=bool(arguments.get("generate_summary", True)),
            )
            result = report.to_dict() if hasattr(report, "to_dict") else dict(report)
            result["namespace"] = ns or _default_namespace
            return result
        except Exception as e:
            return {"error": str(e)}

    elif name == "reflect":
        try:
            result = b.reflect(
                question=arguments.get("question"),
                deep=bool(arguments.get("deep", False)),
            )
            result["namespace"] = ns or _default_namespace
            return result
        except Exception as e:
            return {"error": str(e)}

    elif name == "dedup":
        try:
            result = b.dedup(dry_run=bool(arguments.get("dry_run", False)))
            if isinstance(result, dict):
                result["namespace"] = ns or _default_namespace
            return result
        except Exception as e:
            return {"error": str(e)}

    # ---- v8.1.0 记忆形成层 ------------------------------------------------
    elif name == "session_summarize":
        try:
            from ..session import SessionMemory
            out = SessionMemory(b).summarize(
                session_id=arguments["session_id"],
                messages=arguments.get("messages"),
                project=project or None,
                rebuild=bool(arguments.get("rebuild", False)),
                write_pending=bool(arguments.get("write_pending", True)),
            )
            out["namespace"] = ns or _default_namespace
            return out
        except Exception as e:
            return {"error": str(e)}

    elif name == "session_recall":
        try:
            from ..session import SessionMemory
            out = SessionMemory(b).recall(
                query=arguments.get("query", ""),
                project=project or None,
                k=int(arguments.get("k", 3)),
                include_pending=bool(arguments.get("include_pending", True)),
            )
            out["namespace"] = ns or _default_namespace
            return out
        except Exception as e:
            return {"error": str(e)}

    elif name == "conflict_resolve":
        try:
            from ..conflict import ConflictResolver
            cr = ConflictResolver(b)
            if arguments.get("scope") == "audit":
                return cr.audit()
            if not arguments.get("content"):
                return {"error": "scope=single 时必须提供 content"}
            return cr.resolve(
                content=arguments["content"],
                mtype=arguments.get("mtype", "semantic"),
                project=project or None,
                dry_run=bool(arguments.get("dry_run", True)),
                apply=bool(arguments.get("apply", False)),
            )
        except Exception as e:
            return {"error": str(e)}

    elif name == "task_record":
        try:
            from ..working import WorkingMemory
            out = WorkingMemory(b).record_task(
                goal=arguments["goal"],
                goal_type=arguments.get("goal_type", "general"),
                context=arguments.get("context") or {},
                steps=arguments.get("steps") or [],
                artifacts=arguments.get("artifacts") or [],
                outcome=arguments.get("outcome", "success"),
                duration_min=float(arguments.get("duration_min") or 0),
                rework_count=int(arguments.get("rework_count") or 0),
                user_feedback=arguments.get("user_feedback", "unknown"),
                project=project or None,
            )
            out["namespace"] = ns or _default_namespace
            return out
        except Exception as e:
            return {"error": str(e)}

    elif name == "playbook_save":
        try:
            from ..working import WorkingMemory
            # 回写分支：带 memory_id 表示「这条经验刚被用过一次」，
            # 不是新建。合并进同一个工具而不是另开一个 playbook_feedback，
            # 是为了不改变对外工具数（52）——工具面已经对外承诺过。
            _mid = str(arguments.get("memory_id") or "")
            if _mid:
                out = WorkingMemory(b).update_playbook_stats(
                    playbook_id=str(arguments.get("playbook_id") or ""),
                    success=bool(arguments.get("success", True)),
                    memory_id=_mid,
                    deviation=str(arguments.get("deviation") or ""),
                )
                out["namespace"] = ns or _default_namespace
                return out
            out = WorkingMemory(b).save_playbook(
                pattern=arguments["pattern"],
                goal_type=arguments.get("goal_type", "general"),
                preconditions=arguments.get("preconditions") or {},
                steps=arguments.get("steps") or [],
                failure_modes=arguments.get("failure_modes") or [],
                anti_patterns=arguments.get("anti_patterns") or [],
                scope=arguments.get("scope") or {},
                evidence=arguments.get("evidence") or [],
                project=project or None,
            )
            out["namespace"] = ns or _default_namespace
            return out
        except Exception as e:
            return {"error": str(e)}

    elif name == "playbook_recall":
        try:
            from ..working import WorkingMemory
            hits = WorkingMemory(b).recall_playbooks(
                goal_type=arguments.get("goal_type", ""),
                context=arguments.get("context") or {},
                k=int(arguments.get("k", 3)),
                project=project or None,
            )
            return {"playbooks": hits, "count": len(hits),
                    "applicable_count": sum(1 for h in hits if h.get("applicable")),
                    "namespace": ns or _default_namespace}
        except Exception as e:
            return {"error": str(e)}

    elif name == "code_change_record":
        try:
            from ..coding import CodingMemory
            out = CodingMemory(b).record_change(
                intent=arguments["intent"],
                repo=arguments.get("repo", ""),
                commit=arguments.get("commit", ""),
                parent=arguments.get("parent"),
                intent_type=arguments.get("intent_type", "feature"),
                constraints=arguments.get("constraints") or [],
                linked=arguments.get("linked") or {},
                files=arguments.get("files") or [],
                symbols=arguments.get("symbols") or [],
                impact=arguments.get("impact") or [],
                diff_summary=arguments.get("diff_summary", ""),
                rejected_approaches=arguments.get("rejected_approaches") or [],
                review_notes=arguments.get("review_notes") or [],
                outcome=arguments.get("outcome", "merged"),
                lesson=arguments.get("lesson", ""),
                project=project or None,
            )
            out["namespace"] = ns or _default_namespace
            return out
        except Exception as e:
            return {"error": str(e)}

    elif name == "code_archaeology":
        try:
            from ..coding import CodingMemory
            cm = CodingMemory(b)
            if arguments.get("intent"):
                return cm.pitfall_check(
                    intent=arguments["intent"],
                    files=arguments.get("files") or [],
                    k=int(arguments.get("k", 5)),
                    project=project or None,
                )
            if not arguments.get("target"):
                return {"error": "需要 target（文件或符号）或 intent"}
            return cm.archaeology(
                target=arguments["target"],
                repo=arguments.get("repo", ""),
                k=int(arguments.get("k", 8)),
                project=project or None,
            )
        except Exception as e:
            return {"error": str(e)}

    elif name == "memory_policy":
        try:
            from ..policy import MemoryPolicy
            action = arguments.get("action", "classify")
            if action == "audit":
                return MemoryPolicy.audit(b)
            if action == "filter":
                # v8.1.0：召回闸门开关。默认开启；传 enabled=false 可完全
                # 恢复 8.0.0 的无过滤行为（给确实需要原样取回 secret 的运维场景）。
                if "enabled" in arguments:
                    b._policy_filter = bool(arguments.get("enabled"))
                return {"recall_filter": bool(getattr(b, "_policy_filter", True)),
                        "blocks": ["secret"],
                        "note": "只拦 secret 级；private/normal 在内部上下文照常放行"}
            if action == "check":
                return MemoryPolicy.check(
                    content=arguments.get("content", ""),
                    source_type=arguments.get("source_type"),
                    mtype=arguments.get("mtype"),
                    allow_secret=bool(arguments.get("allow_secret", False)),
                )
            return MemoryPolicy.classify(
                content=arguments.get("content", ""),
                source_type=arguments.get("source_type"),
                mtype=arguments.get("mtype"),
            )
        except Exception as e:
            return {"error": str(e)}

    # ---- v8.1.0 记忆运行层 ----------------------------------------------
    elif name == "profile_card":
        try:
            return b.profile_card(
                include_context=bool(arguments.get("include_context", True)),
                include_pending=bool(arguments.get("include_pending", True)),
                max_tokens=arguments.get("max_tokens"),
            )
        except Exception as e:
            return {"error": str(e)}

    elif name == "memory_inject":
        try:
            return b.inject_context(
                arguments["query"],
                budget_tokens=int(arguments.get("budget_tokens", 1200)),
                goal=arguments.get("goal", ""),
                recent_turns=arguments.get("recent_turns"),
                entities=arguments.get("entities"),
                project=project or None,
                mode=arguments.get("mode", "rrf"),
                include_profile=bool(arguments.get("include_profile", True)),
            )
        except Exception as e:
            return {"error": str(e)}

    elif name == "recall_catalog":
        try:
            return b.recall_catalog(arguments["query"],
                                    k=int(arguments.get("k", 10)),
                                    project=project or None)
        except Exception as e:
            return {"error": str(e)}

    elif name == "expand_memory":
        try:
            out = b.expand_memory(arguments["memory_id"],
                                  include_neighbors=bool(
                                      arguments.get("include_neighbors", True)))
            return out if out is not None else {"error": "memory not found"}
        except Exception as e:
            return {"error": str(e)}

    elif name == "memory_envelope":
        try:
            mid = arguments.get("memory_id")
            if not mid:
                return b.memory_type_stats()
            env = b.memory_envelope(mid)
            return env if env is not None else {"error": "memory not found"}
        except Exception as e:
            return {"error": str(e)}

    elif name == "memory_consolidate":
        try:
            return b.consolidate_memories(
                dry_run=bool(arguments.get("dry_run", False)),
                paths=arguments.get("paths"),
            )
        except Exception as e:
            return {"error": str(e)}

    elif name == "memory_metrics":
        try:
            return b.memory_metrics(domain=arguments.get("domain"))
        except Exception as e:
            return {"error": str(e)}

    elif name == "memory_cascade_delete":
        try:
            mid = arguments["memory_id"]
            if arguments.get("dry_run"):
                dep = b.memory_dependents(mid)
                dep["dry_run"] = True
                dep["note"] = "以上为将被失效的派生项；确认后去掉 dry_run 执行"
                return dep
            return b.cascade_delete(mid,
                                    hard=bool(arguments.get("hard", False)),
                                    reason=arguments.get("reason"))
        except Exception as e:
            return {"error": str(e)}

    elif name == "memory_mask":
        try:
            mid = arguments["memory_id"]
            if arguments.get("action", "mask") == "unmask":
                return b.unmask_memory(mid)
            return b.mask_memory(
                mid,
                replacement=arguments.get("replacement"),
                keep_original=bool(arguments.get("keep_original", True)),
            )
        except Exception as e:
            return {"error": str(e)}

    elif name == "topic_summarize":
        try:
            return b.topic_summarize(
                project=project or arguments.get("project") or None,
                persist=bool(arguments.get("persist", False)),
                max_sessions=int(arguments.get("max_sessions", 20)),
            )
        except Exception as e:
            return {"error": str(e)}

    elif name == "memory_trace":
        try:
            return b.trace_memory(arguments["memory_id"],
                                  max_depth=int(arguments.get("max_depth", 3)))
        except Exception as e:
            return {"error": str(e)}

    elif name == "memory_hot_path":
        try:
            act = arguments.get("action", "status")
            if act == "enable":
                return b.enable_hot_path(
                    max_queue=int(arguments.get("max_queue", 2000)),
                    batch_size=int(arguments.get("batch_size", 32)),
                )
            if act == "flush":
                return b.flush_hot_path()
            return b.hot_path_status()
        except Exception as e:
            return {"error": str(e)}

    return {"error": f"Unknown tool: {name}"}

# ---------- JSON-RPC ----------
def handle_request(req):
    method = req.get("method", ""); rid = req.get("id")
    # v7.0.0 阶段4：MCP 鉴权——配置 token 时，未授权请求直接拒绝。
    if not _authorized(req):
        if rid is None:
            return None  # 无 id 的通知类请求：静默丢弃
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32001,
                          "message": "Unauthorized: invalid or missing MCP token"}}
    if method == "initialize":
        params = req.get("params", {})
        # The client may declare a default namespace in clientInfo.name
        client_info = params.get("clientInfo", {})
        ns_hint = None
        if isinstance(client_info, dict):
            cn = client_info.get("name", "")
            if cn and cn.startswith("tenant:"):
                ns_hint = cn.split(":", 1)[1]
        # Eagerly initialise the brain for the requested namespace
        _ensure_brain(namespace=ns_hint)
        # ── 协议版本协商（2026-10-21 修）──────────────────────────────
        # 原来这里写死 "2024-11-05"：客户端提议什么版本，服务端都回同一个字符串。
        # 按 MCP 规范，服务端应当回**自己支持的**版本：客户端提议的版本在支持
        # 列表里就照它回，否则回自己最高的那个。写死会导致：
        #   · 新版客户端（2025-06-18）看到服务端版本更旧，日志里永远是同一个值；
        #   · 演示给技术方看时，一眼就发现"版本从来不变"= 不是真协商。
        _MCP_SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")
        proposed = ""
        try:
            proposed = str((params or {}).get("protocolVersion") or "").strip()
        except Exception:
            proposed = ""
        accepted = proposed if proposed in _MCP_SUPPORTED else _MCP_SUPPORTED[0]
        result = {"protocolVersion": accepted,
                  "serverInfo":{"name":"mnemosyne-memory","version":"8.1.0"},
                  "capabilities":{"tools":{}},
                  "protocol": {"proposed": proposed or None,
                               "accepted": accepted,
                               "supported": list(_MCP_SUPPORTED)}}
        if ns_hint:
            result["namespace"] = ns_hint
        return {"jsonrpc":"2.0","id":rid,"result":result}
    elif method == "tools/list":
        return {"jsonrpc":"2.0","id":rid,"result":handle_tools_list()}
    elif method == "tools/call":
        params = req.get("params", {})
        name = params.get("name","")
        arguments = params.get("arguments", {})
        result = handle_tools_call(name, arguments)
        return {"jsonrpc":"2.0","id":rid,"result":{"content":[{"type":"text","text":json.dumps(result,ensure_ascii=False)}]}}
    elif method == "notifications/initialized":
        return None
    return {"jsonrpc":"2.0","id":rid,"error":{"code":-32601,"message":f"Method not found: {method}"}}

def main():
    global DEFAULT_BRAIN, brain, _brain_dir, _default_namespace, _auth_token
    p = argparse.ArgumentParser(description="Mnemosyne MCP Server")
    p.add_argument("--brain-dir", default=DEFAULT_BRAIN, help="记忆库目录")
    p.add_argument("--namespace", default=None,
                   help="Default tenant namespace (default: 'default')")
    p.add_argument("--token", default=None,
                   help="MCP 鉴权令牌（等价于环境变量 MNEMOSYNE_MCP_TOKEN）")
    args = p.parse_args()
    _brain_dir = args.brain_dir
    _default_namespace = args.namespace or "default"
    _auth_token = args.token or os.environ.get("MNEMOSYNE_MCP_TOKEN")
    _ensure_brain(namespace=_default_namespace)
    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        try:
            resp = handle_request(json.loads(line))
            if resp:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except json.JSONDecodeError:
            pass

if __name__ == "__main__":
    main()
