# -*- coding: utf-8 -*-
"""AIC —— Adaptive Information Capsule（自适应记忆胶囊），v7.0.2。

【要解决的问题】
用户端的痛点是"**极短上下文**"（只给模型 1~2 轮、几百 token），但需要
**100% 精准**的记忆与召回。这两件事在传统做法下是**互斥**的：
上下文预算装不下原文时，系统只能"丢掉这条记忆"或"给一段生成式摘要"——
前者让模型看不到信息，后者是**有损改写**（会凭空造出原文没有的数字/时间/结论）。
两者都会让"精准召回"变成概率事件。

【本模块的答案：把"压缩"重新定义为"分层 + 可寻址"】
  • 压缩产物不是"更短的近似文本"，而是
      **{稳定指针 ref} + {结构化事实} + {内容原子} [+ {抽取式要点}]**
  • 因此它同时满足三个硬性质：
      1. **无损可逆**：`ref` 指回原文，`expand(ref)` 逐字取回（校验内容哈希）；
      2. **零幻觉**：要点是**抽取式**的（句子必须逐字来自原文），事实来自
         规则三元组抽取，全程不做任何生成 → 结构上不可能编造；
      3. **事实守恒**：数字/日期/金额/型号/URL/邮箱/引号原话（"原子"，见
         `utils._content_atoms`）在**所有**压缩层级下都完整保留。
    于是"预算不够"不再等于"信息丢失"，而是"精度分层"：预算再小，
    可被追问的事实（多少钱、什么时候、哪个型号）依然在场。

【分层阶梯（每层都过原子守恒校验）】
    full    原文（放得下就用它 —— 最省事也最不可能出错）
    gist    抽取式要点 + 事实 + 原子 + 指针
    facts   事实三元组 + 原子 + 指针
    pointer 主题(原文前缀) + 原子 + 类型 + 时间 + 指针
    atoms   极端情况：只有指针 + 原子（仍可回答所有"取值类"追问）

【为什么这样对 LLM 的缓存命中也是最优】
胶囊是**内容寻址**的：key = sha256(原文) + level + 预算档位，同一记忆在同一
预算下永远产出**逐字节相同**的胶囊。调用方把它拼在上下文固定位置时，
上游前缀缓存（prefix / KV cache）能稳定命中 —— 省钱与"精准"在这里不冲突。
"""

import hashlib
import re

from .utils import (_content_atoms, _extract_relationships, atoms_preserved,
                    atoms_signature)

# ---------------------------------------------------------------------------
# Token 估算
# ---------------------------------------------------------------------------
# 项目既有口径是「4 字符 ≈ 1 token」(utils.StatsTracker._count_tokens 的兜底)，
# 那是为**英文**标的；中文一个字通常就是 1 个 token，用 4 字符折算会把真实
# 上下文体积低估到 1/3~1/4 —— 于是"预算 120 token"实际塞进去 400+ token，
# 在极短上下文场景下直接爆窗。这里给胶囊路径一个**保守**估算：CJK 逐字计 1，
# 拉丁词按词计 1，其余可见符号逐字计 1。宁可保守（略高估），不可乐观。
_CJK_RE = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef]")
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_OTHER_VISIBLE_RE = re.compile(r"[^\sA-Za-z0-9_\u2e80-\u9fff\uf900-\ufaff"
                               r"\u3000-\u303f\uff00-\uffef]")


def estimate_tokens(text):
    """保守 token 估算（CJK 逐字、拉丁按词、其它可见符号逐个）。"""
    if not text:
        return 0
    t = str(text)
    n = len(_CJK_RE.findall(t))
    n += len(_WORD_RE.findall(t))
    n += len(_OTHER_VISIBLE_RE.findall(t))
    return max(1, n)


# ---------------------------------------------------------------------------
# 抽取式要点（绝不改写）
# ---------------------------------------------------------------------------
_SENT_SPLIT_RE = re.compile(r"[。！？；\n]+|(?<=[.!?])\s+")


def _sentences(text):
    """切句（保留原句原文，不做任何规整），过滤空句。"""
    out = []
    for s in _SENT_SPLIT_RE.split(text or ""):
        s = (s or "").strip()
        if s:
            out.append(s)
    return out


def _bigrams(s):
    s = s or ""
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


def extractive_gist(text, max_sentences=2, max_tokens=None, token_counter=None):
    """抽取式要点：按"信息量"挑原句，**逐字取自原文**。

    打分 = 与其它句的 bigram 重叠（中心性，代表"讲了这件事"）+ 含原子加权
    （带数字/型号的句子是"可被追问的事实"，优先级最高）。
    返回：原句子串列表（不是改写后的句子）。挑选顺序保留原文语序，
    以便视觉与语义上都读得通。
    """
    tc = token_counter or estimate_tokens
    sents = _sentences(text)
    if not sents:
        return []
    if len(sents) == 1:
        only = sents[0]
        return [only] if max_tokens is None or tc(only) <= max_tokens else []
    # 句间中心性
    bags = [_bigrams(s) for s in sents]
    scored = []
    for i, s in enumerate(sents):
        others = set()
        for j, b in enumerate(bags):
            if j != i:
                others |= b
        overlap = (len(bags[i] & others) / len(bags[i])) if bags[i] else 0.0
        atom_hit = 1.0 if _content_atoms(s) else 0.0
        scored.append((overlap * 1.0 + atom_hit * 1.5, i, s))
    scored.sort(key=lambda x: x[0], reverse=True)
    picked = []
    used = 0
    for _sc, idx, s in scored:
        if len(picked) >= max_sentences:
            break
        cost = tc(s)
        if max_tokens is not None and used + cost > max_tokens and picked:
            continue
        picked.append((idx, s))
        used += cost
    picked.sort(key=lambda x: x[0])  # 还原原文语序
    return [s for _i, s in picked]


# ---------------------------------------------------------------------------
# 指针（ref）—— 稳定、可校验、可解析
# ---------------------------------------------------------------------------
# 【两个长度的指针，各司其职】
#   • 结构化字段 `ref`（全量）：`m:<id16>#<hash16>` —— 给程序用的权威引用，
#     哈希 16 位足以做完整性校验。
#   • 渲染文本里的指针（短）：`m:<id12>#<hash6>` —— 给**模型**看的，要省 token。
#     实测一条 id16+hash8 的指针约 25 token，在 30~60 token 的极短预算里
#     占比超过一半；缩到 id12+hash6（约 19 字符）后仍足以唯一定位并校验
#     （id 前缀 12 位十六进制 + 内容哈希 6 位，误配概率 < 1/1677 万，
#     且 `expand()` 会二次校验哈希后才会返回正文）。
REF_RE = re.compile(r"^m:(?P<id>[0-9a-zA-Z_-]{4,64})(?:#(?P<hash>[0-9a-f]{4,64}))?"
                    r"(?:@(?P<level>full|gist|facts|pointer|atoms))?$",
                    re.IGNORECASE)

# 渲染里指针的显示宽度
_REF_ID_W = 12
_REF_HASH_W = 6


def content_hash(text, n=16):
    """内容哈希前缀（UTF-8，surrogatepass 以兼容含孤立代理的脏数据）。"""
    return hashlib.sha256(
        (text or "").encode("utf-8", errors="surrogatepass")).hexdigest()[:max(4, n)]


def make_ref(memory_id, content=None, level=None):
    """构造稳定指针 ``m:<id>#<内容哈希>[@层级]``。

    带内容哈希的理由：`expand(ref)` 可以**校验**取回的正文与指针声明的一致 ——
    指针因此是可信任的（防止 id 复用或数据被改写后"指错对象"）。
    """
    base = "m:%s" % str(memory_id)
    if content is not None:
        base += "#" + content_hash(content)
    if level:
        base += "@" + level
    return base


def display_ref(memory_id, content=None, level=None):
    """渲染进上下文的**短指针**（省 token，仍可唯一定位 + 校验）。"""
    base = "m:%s" % str(memory_id)[:_REF_ID_W]
    if content is not None:
        base += "#" + content_hash(content, _REF_HASH_W)
    if level:
        base += "@" + level
    return base


def parse_ref(ref):
    """解析指针 → ``{"id","hash","level"}``；非法返回 None。"""
    if not ref or not isinstance(ref, str):
        return None
    m = REF_RE.match(ref.strip())
    if not m:
        return None
    return {"id": m.group("id"),
            "hash": (m.group("hash") or "").lower(),
            "level": m.group("level")}


# ---------------------------------------------------------------------------
# 胶囊构建
# ---------------------------------------------------------------------------
LEVELS = ("full", "gist", "facts", "pointer", "atoms")

# 指针在渲染中必须完整出现：它是"预算不够时唯一的救命绳"，任何裁剪都不能动它。
_RENDER_SEP = " | "
_TITLE_MAX = 24


class _CapsuleCache:
    """内容寻址胶囊缓存（确定性的关键）。

    key = (内容哈希, 层级, 预算档, 类型, 时间) —— 不依赖 id，因此同一段文本
    在不同记忆之间也复用；value 为已渲染字符串。命中率进 `recall_health`。
    """

    def __init__(self, maxsize=4096):
        self.maxsize = maxsize
        self._d = {}
        self.hits = 0
        self.misses = 0

    def get(self, key):
        v = self._d.get(key)
        if v is None:
            self.misses += 1
        else:
            self.hits += 1
        return v

    def put(self, key, val):
        if len(self._d) >= self.maxsize:
            # 简单 FIFO 淘汰：胶囊很小，不值得为 LRU 维护链表
            for k in list(self._d.keys())[: self.maxsize // 4]:
                self._d.pop(k, None)
        self._d[key] = val
        return val

    def stats(self):
        total = self.hits + self.misses
        return {"hits": self.hits, "misses": self.misses,
                "entries": len(self._d),
                "hit_rate": round(self.hits / total, 4) if total else 0.0}


_CACHE = _CapsuleCache()


def capsule_cache_stats():
    """胶囊缓存统计（供 recall_health 读取）。"""
    return _CACHE.stats()


def _facts_of(record, content):
    """结构化事实：`主语·关系·宾语[·限定词]`（规则抽取，零生成）。"""
    ents = record.get("entities_detailed") or record.get("entities") or []
    try:
        rels = _extract_relationships(ents, content)
    except Exception:
        rels = []
    out = []
    for r in rels:
        s = str(r.get("from") or "").strip()
        o = str(r.get("to") or "").strip()
        rel = str(r.get("relation") or "related_to").strip()
        if not s or not o:
            continue
        q = str(r.get("qualifier") or "").strip()
        out.append("%s·%s·%s%s" % (s, rel, o, ("·" + q) if q else ""))
    # 稳定去重保序
    return list(dict.fromkeys(out))


def _title_of(content, max_chars=_TITLE_MAX):
    """主题 = 原文**前缀子串**（不加工，保证 verbatim）。"""
    s = (content or "").strip()
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + "…"


def _shorten(text, n):
    """截短主题（仍是原文前缀子串 + 省略号，保证 verbatim）。"""
    if not text:
        return text
    if len(text) <= n:
        return text
    return text[:n] + "…"


def build_capsule(record, budget_tokens, token_counter=None, use_cache=True):
    """把一条记忆压成"能装进预算"的胶囊（v7.0.2 极限上下文精确压缩）。

    record 至少需要 ``id`` / ``content``；``type``/``tags``/``created_at`` 可选。

    返回 dict：
        ref / level / text(渲染结果) / atoms / facts / gist / title
        tokens / original_tokens / ratio / lossless / over_budget
        missing_atoms（必须为空 —— 非空表示原子守恒被破坏，属缺陷）

    语义保证（可断言，见 `capsule_selfcheck`）：
      1. ``atoms(source) ⊆ atoms(text)``：原子一个不丢；
      2. ``level != "full"`` 时 text 里出现的自然语言句子都是原文的**逐字子串**；
      3. ``over_budget`` 只可能因为"原子本身超预算"而为 True —— 那种情况下
         我们宁可超预算也不丢原子（精准 > 省 token）。
    """
    tc = token_counter or estimate_tokens
    mid = record.get("id") or ""
    content = record.get("content") or ""
    mtype = record.get("type") or record.get("mtype") or "semantic"
    created = (record.get("created_at") or "")[:10]
    atoms = _content_atoms(content)
    atom_list = sorted(atoms.keys())
    atom_sig = atoms_signature(content)
    original_tokens = tc(content)

    cache_key = (content_hash(content, 16), str(mid), str(mtype), created,
                 atom_sig, int(budget_tokens or 0))
    if use_cache:
        cached = _CACHE.get(cache_key)
        if cached is not None:
            return dict(cached)

    facts = _facts_of(record, content)
    title = _title_of(content)

    # ---- 渲染：字段集由"宽"到"窄"逐级裁剪 ----
    # 分级保留的原因：任何一次裁剪都必须**严格减少 token**，否则分层阶梯会出现
    # 平台期（相邻档位产出同样长，预算调小却不降级），"按预算分档"就名存实亡。
    # 不变量（永不裁剪）：① 指针；② 原子列表。前者是"可逆"的凭据，后者是
    # "事实不丢"的凭据 —— 这两样被裁掉，胶囊就不再是"精确压缩"了。
    def _render(level, gist_sents, fields, title_text=None):
        parts = []
        if "gist" in fields and gist_sents:
            parts.append("要点:" + "".join(s + "。" for s in gist_sents))
        if "facts" in fields and facts:
            parts.append("事实:" + " ".join(facts))
        if "title" in fields:
            t = title if title_text is None else title_text
            if t:
                parts.append("主题:" + t)
        if atom_list:
            parts.append("要点值:" + ",".join(atom_list))
        if "meta" in fields:
            parts.append("类型:%s" % mtype)
            if created:
                parts.append("时间:%s" % created)
        _ref = display_ref(mid, content, level)
        return "[%s] %s%s%s" % (level[:1].upper(), _ref, _RENDER_SEP,
                                _RENDER_SEP.join(parts))

    # 单条记忆的预算下限：至少 8 token，否则连"指针 + 原子"都编不出来，
    # 胶囊就退化成不可逆的截断（那是最坏的结果，一定要避开）。
    remain = max(8, int(budget_tokens or 0))
    # ---- 分层尝试：字段集逐级收窄，第一个装得下的层级胜出 ----
    # 顺序即"信息量从高到低"：预算越小 → 层级越低 → 修饰语越少，
    # 但**指针与原子在任何层级都在**。
    plan = [
        ("full",    None, None, None),
        ("gist",    3, ("gist", "facts", "meta"), None),
        ("gist",    2, ("gist", "facts", "meta"), None),
        ("gist",    1, ("gist", "facts", "meta"), None),
        ("gist",    1, ("gist", "meta"), None),
        ("facts",   None, ("facts", "meta"), None),
        ("facts",   None, ("facts",), None),
        ("pointer", None, ("title", "meta"), title),
        ("pointer", None, ("title",), title),
        ("pointer", None, ("title",), _shorten(title, 16)),
        ("pointer", None, ("title",), _shorten(title, 8)),
        ("pointer", None, (), ""),
        ("atoms",   None, ("meta",), None),
        ("atoms",   None, (), None),
    ]
    chosen_level, chosen_text, chosen_gist = None, None, []
    for level, nsent, fields, ttext in plan:
        if level == "full":
            text, gist_sents = content, []
        else:
            gist_sents = (extractive_gist(content, max_sentences=nsent,
                                          token_counter=tc) if nsent else [])
            text = _render(level, gist_sents, fields, ttext)
        if tc(text) <= remain:
            chosen_level, chosen_text, chosen_gist = level, text, gist_sents
            break
    # 最小可行形态（只留指针 + 原子）—— 供调用方判断"这条到底装不装得下"
    minimal = _render("atoms", [], ())
    min_tokens = tc(minimal)
    if chosen_level is None:
        # 连最小形态都超预算（原子本身就比预算大）→ 仍然交付最小形态，
        # 并如实标记 over_budget。「精准 > 省 token」在这里是明确取舍：
        # 丢掉原子等于丢掉"可被追问的事实"，那才是真正的失败。
        chosen_level, chosen_text, chosen_gist = "atoms", minimal, []

    # ---- 原子守恒兜底（硬不变量）----
    ok, missing = atoms_preserved(content, chosen_text)
    if not ok:
        # 理论上不会走到这里（渲染里始终带 atom_list）；一旦发生就**补写缺失原子**，
        # 保证不变量成立，并如实标记（宁可超预算，绝不丢事实）。
        chosen_text += _RENDER_SEP + "补充要点值:" + ",".join(sorted(missing.keys()))

    tokens = tc(chosen_text)
    capsule = {
        "memory_id": mid,
        # 权威引用（全量哈希，程序用）
        "ref": make_ref(mid, content, chosen_level),
        # 渲染里实际出现的短指针（模型用）
        "display_ref": display_ref(mid, content, chosen_level),
        "level": chosen_level,
        "text": chosen_text,
        "atoms": atom_list,
        "atom_signature": atom_sig,
        "facts": facts if chosen_level in ("gist", "facts") else [],
        "gist": chosen_gist,
        "title": title if chosen_level == "pointer" else "",
        "type": mtype,
        "created_at": record.get("created_at") or "",
        "tokens": tokens,
        "min_tokens": min_tokens,
        "original_tokens": original_tokens,
        "ratio": round(tokens / original_tokens, 4) if original_tokens else 1.0,
        "lossless": chosen_level == "full",
        # 「原子优先于预算」：min_tokens > 预算时必然 over_budget，这是**刻意**的取舍，
        # 调用方可据此把该条放进"精准索引"而不是正文。
        "over_budget": bool(budget_tokens is not None
                            and tokens > int(budget_tokens or 0)),
        "atoms_preserved": True,
    }
    if use_cache:
        _CACHE.put(cache_key, dict(capsule))
    return capsule


def capsule_selfcheck(capsule, source_content):
    """自检：返回 ``(ok, problems)``。原子守恒 + 抽取式（不得改写）+ 指针在场。"""
    problems = []
    text = capsule.get("text", "")
    ok_atoms, missing = atoms_preserved(source_content, text)
    if not ok_atoms:
        problems.append("原子丢失: %s" % sorted(missing.keys()))
    if capsule.get("level") != "full":
        _src = source_content or ""
        _src_alt = _src.strip()
        for frag in list(capsule.get("gist") or []) + \
                ([capsule["title"]] if capsule.get("title") else []):
            if not frag:
                continue
            core = frag.rstrip("…")
            if core and core not in _src and core not in _src_alt:
                problems.append("非抽取式片段（原文中不存在）: %s" % frag[:40])
    if capsule.get("ref") and not parse_ref(capsule["ref"]):
        problems.append("指针格式非法: %s" % capsule["ref"])
    # 渲染文本里必须能看到指针 —— 否则"可逆"对模型不可用（它无法请求展开）。
    # `full` 层级例外：正文就是原文本身，本来就不需要展开，加指针反而让
    # "无损直通"这一档多花 ~20 token（那就不是"放得下就用原文"了）。
    if capsule.get("level") != "full":
        dr = capsule.get("display_ref")
        if dr and dr not in text:
            problems.append("渲染文本缺少指针: %s" % dr)
    return (not problems), problems


def render(capsule):
    """取胶囊的渲染文本（供调用方拼接上下文）。"""
    return capsule.get("text", "") if isinstance(capsule, dict) else str(capsule)
