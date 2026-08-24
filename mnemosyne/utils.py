import collections
import hashlib
import json
import math
import os
import random
import re
import time
from datetime import datetime, timezone, timedelta
import functools
import threading
import logging

logger = logging.getLogger("mnemosyne.utils")

# === Constants ===
INDEX_NAME = "index.jsonl"
GRAPH_NAME = "graph.jsonl"
META_NAME = "meta.json"
EMBEDDING_DIM = 128
PROJ_BUCKETS = 2048
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".mnemosyne")


# === Cross-platform file lock ===
import os as _os
if hasattr(_os, "O_BINARY"):
    import msvcrt as _msvcrt
    def _file_lock(f, exclusive=True):
        _msvcrt.locking(f.fileno(), _msvcrt.LK_LOCK if exclusive else _msvcrt.LK_NBLCK, 1)
    def _file_unlock(f):
        try: _msvcrt.locking(f.fileno(), _msvcrt.LK_UNLCK, 1)
        except OSError as exc:
            logger.debug("文件解锁失败：%s", exc)
else:
    import fcntl as _fcntl
    def _file_lock(f, exclusive=True):
        _fcntl.flock(f.fileno(), _fcntl.LOCK_EX if exclusive else _fcntl.LOCK_NB)
    def _file_unlock(f):
        try: _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)
        except OSError as exc:
            logger.debug("文件解锁失败：%s", exc)


# ============================================================================
# Part 1: 工具函数
# ============================================================================

# 北京时间（UTC+8）——所有时间戳以中国时区显示
_BEIJING_TZ = timezone(timedelta(hours=8))

# 时间戳字符串驻留：同一秒内大量写入时共享同一字符串对象（内存优化）
_TS_CACHE = {}


def _now_iso():
    ts = datetime.now(_BEIJING_TZ).isoformat(timespec="seconds")
    if len(_TS_CACHE) > 10000:
        _TS_CACHE.clear()  # 防长期进程无限增长
    return _TS_CACHE.setdefault(ts, ts)


def _today_str():
    return datetime.now(_BEIJING_TZ).strftime("%Y-%m-%d")


def _utcnow_ts():
    return time.time()


_ID_SEQ = 0
_ID_SEQ_LOCK = threading.Lock()


def _unique_salt():
    """生成进程内唯一盐（time_ns + 单调递增计数器），保证每次调用唯一。

    Windows 上 time.time() 精度仅 ~15.6ms，相邻 retain 可能生成相同 id，
    导致 version-tracking 的 rewrite 按 id 过滤时误删记录。此函数彻底规避。
    """
    global _ID_SEQ
    with _ID_SEQ_LOCK:
        _ID_SEQ += 1
        return f"{time.time_ns()}-{_ID_SEQ}"


def _stable_id(content, salt=""):
    return hashlib.sha256((salt + "|" + (content or "")).encode("utf-8")).hexdigest()[:16]


def _fail(msg, hint=None, fix=None, code=1):
    print(f"\u274c {msg}")
    if hint:
        print(f"\U0001f4a1 可能原因：{hint}")
    if fix:
        print(f"\U0001f527 解决办法：{fix}")
    return code


def _ok(msg):
    print(f"\u2705 {msg}")
    return 0


def _softmax(scores, temp=1.0):
    """Softmax归一化，temp<1 增强区分度。"""
    if not scores:
        return []
    mx = max(scores)
    exps = [math.exp((s - mx) / max(temp, 1e-8)) for s in scores]
    total = sum(exps)
    return [e / max(total, 1e-8) for e in exps]


# ============================================================================
# Part 2: 分词与实体抽取（多语言增强版 + Turn Recall Optimize）
# ============================================================================
# 5步Optimize方案，纯标准库零依赖
# ============================================================================

# Step 1: 分词预processes  — Date标准化 + 语义词仿英文伪装
def _tokenize_preprocess(text):
    """在 N-gram 切分前做正则预processes ，使Date和语义词能完整命中现有模式。"""
    # 1.1 Date归一化
    text = re.sub(
        r'(\d{4})年(\d{1,2})月(\d{1,2})日',
        lambda m: f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}",
        text
    )
    text = re.sub(
        r'(\d{1,2})月(\d{1,2})日',
        lambda m: f"{int(m.group(1)):02d}-{int(m.group(2)):02d}",
        text
    )
    # 1.2 核心语义词 → 英文伪装（使其被 \w{2,} 完整抓取，不被 N-gram 碎切）
    surrogate_map = {
        "什么时候": " __WH_TIME__ ",
        "什么Time": " __WH_TIME__ ",
        "几月几号": " __WH_TIME__ ",
        "今天": " __T_TODAY__ ",
        "昨天": " __T_YESTERDAY__ ",
        "前天": " __T_DBYESTERDAY__ ",
        "明天": " __T_TOMORROW__ ",
        "在哪里": " __WH_WHERE__ ",
        "哪里": " __WH_WHERE__ ",
        "是谁": " __WH_WHO__ ",
        "哪": " __WH_WHICH__ ",
        "多少钱": " __WH_HOWMUCH__ ",
    }
    for zh_word, eng_surrogate in surrogate_map.items():
        text = text.replace(zh_word, eng_surrogate)
    return text

# Step 2: Time注入（Index阶段用）
from datetime import datetime as _dt_pre, timedelta as _td_pre

def inject_time_expressions(turn_text, session_date_str):
    """parses  Session Date，将相对Time词扩展为多种格式的绝对Date附加在文本后。"""
    if not session_date_str or not isinstance(session_date_str, str):
        return turn_text
    try:
        clean_date = re.sub(r'[^\d-]', '', session_date_str.replace('/', '-'))
        dt = _dt_pre.strptime(clean_date, "%Y-%m-%d")
    except Exception:
        return turn_text

    added_tokens = []
    if re.search(r'今天|现在|此刻|刚|__T_TODAY__', turn_text):
        added_tokens.extend([
            dt.strftime("%Y-%m-%d"),
            f"{dt.month:02d}-{dt.day:02d}",
            f"{dt.year}年{dt.month}月{dt.day}日",
            f"{dt.month}月{dt.day}日"
        ])
    if re.search(r'昨天|昨晚|昨儿|__T_YESTERDAY__', turn_text):
        y_dt = dt - _td_pre(days=1)
        added_tokens.extend([
            y_dt.strftime("%Y-%m-%d"),
            f"{y_dt.month:02d}-{y_dt.day:02d}",
            f"{y_dt.year}年{y_dt.month}月{y_dt.day}日",
            f"{y_dt.month}月{y_dt.day}日"
        ])
    if re.search(r'前天|__T_DBYESTERDAY__', turn_text):
        by_dt = dt - _td_pre(days=2)
        added_tokens.extend([
            by_dt.strftime("%Y-%m-%d"),
            f"{by_dt.month:02d}-{by_dt.day:02d}",
            f"{by_dt.year}年{by_dt.month}月{by_dt.day}日",
            f"{by_dt.month}月{by_dt.day}日"
        ])
    if added_tokens:
        return turn_text + " " + " ".join(set(added_tokens))
    return turn_text

# Step 3: 分数平滑
def smooth_session_turns(turn_scores, alpha=0.35, beta=0.15):
    """一维卷积平滑：相邻 Turn 互相传递分数。"""
    n = len(turn_scores)
    if n <= 1:
        return turn_scores[:]
    smoothed = list(turn_scores)
    for i in range(n):
        bonus = 0.0
        if i > 0: bonus += alpha * turn_scores[i - 1]
        if i < n - 1: bonus += alpha * turn_scores[i + 1]
        if i > 1: bonus += beta * turn_scores[i - 2]
        if i < n - 2: bonus += beta * turn_scores[i + 2]
        smoothed[i] += bonus
    return smoothed

# Step 4: Session-Turn 联合融合
def fuse_session_and_turn_scores(session_scores, turn_scores_map, session_weight=0.35):
    """利用 Session Recall 先验优势，将 Session 得分注入 Turn 排名。"""
    if not turn_scores_map:
        return {}
    max_sess = max(session_scores.values()) if session_scores and max(session_scores.values()) > 0 else 1.0
    raw_turn_vals = [v[1] for v in turn_scores_map.values()]
    max_turn = max(raw_turn_vals) if raw_turn_vals and max(raw_turn_vals) > 0 else 1.0

    final_turn_scores = {}
    for turn_id, (sess_id, t_score) in turn_scores_map.items():
        s_score = session_scores.get(sess_id, 0.0)
        norm_s = s_score / max_sess
        norm_t = t_score / max_turn
        final_turn_scores[turn_id] = session_weight * norm_s + (1.0 - session_weight) * norm_t
    return final_turn_scores

# Step 5: 伪相关反馈 PRF
_STOPWORDS_PRF = {"的","了","在","是","我","有","和","就","不","人","都","一","上","也","很","到","说","要","去","你","会","着","没有","看","好","这","哪","什么","怎么","哪","知道","记得","请问","那"}

def expand_query_prf(query, top_session_turns_text, top_k=3):
    """从 Top-1 Session 提取高频词扩展Query。"""
    from collections import Counter
    words = []
    for text in top_session_turns_text:
        tokens = re.findall(r'[\u4e00-\u9fa5]{2,}|[a-zA-Z0-9]+', text)
        for w in tokens:
            if w not in _STOPWORDS_PRF and w not in query:
                words.append(w)
    if not words:
        return query
    counts = Counter(words)
    top_words = [w for w, _ in counts.most_common(top_k)]
    return query + " " + " ".join(top_words)


# Step 6: Index/Output解耦 + 上下文窗口拼接
def format_retrieval_output(top_turn_indices, session_raw_turns):
    """
    Index用增强文本，Output用原始文本 + 前后 Turn 拼接。

    top_turn_indices: 检索 Top-K 的 turn Index列Table
    session_raw_turns: [{raw_text: ...}, ...] per  Turn 的原始文本
    """
    formatted = []
    for turn_idx in top_turn_indices:
        if turn_idx < 0 or turn_idx >= len(session_raw_turns):
            continue
        start_idx = max(0, turn_idx - 1)
        end_idx = min(len(session_raw_turns), turn_idx + 2)
        t = session_raw_turns[turn_idx]
        raw_key = 'raw_text' if 'raw_text' in t else 'text'
        expanded = "".join([session_raw_turns[k].get(raw_key, '') for k in range(start_idx, end_idx)])
        formatted.append({
            "turn_id": t.get('turn_id', turn_idx),
            "text": expanded
        })
    return formatted


# Step 7: 会话内两阶段局域精排 (In-Session Reranking)
def rerank_in_session(query, top_session_ids, session_turns_map, top_k_turns=10):
    """
    针对 Top Session 内部的 Turn 进line：
      A) 角色对齐加权 (User vs Assistant)
      B) 连续 N-gram 硬matches 加分
    纯标准库，无外部依赖。

    session_turns_map: {session_id: [{'turn_id': int, 'text': str, 'role': str, 'raw_score': float}, ...]}
    """
    import re as _re
    from collections import Counter as _Counter

    # 1. identifies  Query 的角色倾向
    is_user_query = bool(_re.search(r'我|我的|我提|我买|我喜欢|我有|我曾', query))
    is_asst_query = bool(_re.search(r'你|你的|你建议|你推荐|你说|助手', query))

    # 2. 收集候选 Turn
    candidate_turns = []
    for sess_id in top_session_ids[:10]:
        candidate_turns.extend(session_turns_map.get(sess_id, []))

    if not candidate_turns:
        return []

    # 3. 提取 Query 的 n-gram 特征（2-4 gram）用于硬matches 
    query_ngrams = []
    clean_q = _re.sub(r'[^\w\u4e00-\u9fa5]', '', query)
    for n in range(2, 5):
        for i in range(len(clean_q) - n + 1):
            query_ngrams.append(clean_q[i:i+n])

    # 4. 局部打分重排
    scored_turns = []
    for turn in candidate_turns:
        score = turn.get('raw_score', 0.0)
        text = turn.get('text', '')
        role = turn.get('role', 'user')

        # A. 角色加权
        if is_user_query and role == 'user':
            score *= 1.35
        elif is_asst_query and role == 'assistant':
            score *= 1.35

        # B. 连续子串硬matches 加分
        ngram_bonus = 0.0
        for gram in query_ngrams:
            if gram in text:
                ngram_bonus += (len(gram) ** 2) * 0.5
        score += ngram_bonus

        scored_turns.append((turn['turn_id'], score))

    # 5. by 新分数sorts returns  Top K
    scored_turns.sort(key=lambda x: x[1], reverse=True)
    return [t[0] for t in scored_turns[:top_k_turns]]



# Unicode 字符范围
#           拉丁/西里尔→词级切分 | 数字/Date→模式matches 

# Unicode 字符范围
_RE_CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")           # 中日韩统一汉字
_RE_HIRAGANA = re.compile(r"[\u3040-\u309f]+")                                  # 平假名序列
_RE_KATAKANA = re.compile(r"[\u30a0-\u30ff\u31f0-\u31ff]+")                    # 片假名序列
_RE_HANGUL = re.compile(r"[\uac00-\ud7af]+")                                    # 韩文音节序列
_RE_LATIN = re.compile(r"[a-z\u00e0-\u024f\u1e00-\u1eff][a-z\u00e0-\u024f\u1e00-\u1eff0-9_]*", re.IGNORECASE)  # 拉丁/扩展拉丁词
_RE_CYRILLIC = re.compile(r"[\u0400-\u04ff]+")                                  # 西里尔字母序列


# ============ token 级压缩（方向2，对标 LLMLingua，零 LLM 确定性）============
_CN_PARTICLES = frozenset("嗯啊哦呃哈嘛呢吧呀啦唉哟呵嘿哎呦噢哦诶")
_CN_FUNCTION = frozenset("的了着过是在和与及或但而就把被向由等")
_EN_STOPWORDS = frozenset("""the a an is are was were be been being am of to in on at
for with by as that this it its and or but not no if then than so such from into over
under again more most other some only own same too very just can will would should
could may might must""".split())
_CN_REDUNDANT = ("非常", "真的", "其实", "就是", "然后", "那个", "这个", "但是", "因为", "所以")


def compress_text(text, level=2):
    """确定性 token 级压缩（零 LLM、可复现、纯标准库）。

    对标 LLMLingua 的 token 级压缩，但 Mnemosyne 走「确定性低损」路线——
    不调 LLM、不猜概率，只用规则删除确定冗余的词/字：

    - level=1：仅压缩空白 + 冗余标点（绝对无损）
    - level=2：+ 删语气词 + 英文停用词 + 中文高频虚词（低损，推荐）
    - level=3：+ 删冗余副词/程度词（中损）

    用于 recall 后压缩召回内容，节省送入 LLM 的输入 Token。
    只删虚词，保留所有实词/数字/实体，不影响语义完整性。
    """
    if not text:
        return text
    if level == 1:
        return re.sub(r"\s+", " ", text).strip()
    # 英文停用词删除（按空白切分，只删整词）
    parts = re.split(r"(\s+)", text)
    out = []
    for p in parts:
        if p and p[0].isalpha() and p.isascii() and p.lower() in _EN_STOPWORDS:
            continue
        out.append(p)
    text = "".join(out)
    # 中文语气词 + 高频虚词删除
    text = "".join(ch for ch in text if ch not in _CN_PARTICLES and ch not in _CN_FUNCTION)
    if level >= 3:
        for w in _CN_REDUNDANT:
            text = text.replace(w, "")
    # 压缩空白 + 清理残留标点
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"[，。；：、!！?？]{2,}", lambda m: m.group(0)[0], text)
    return text


@functools.lru_cache(maxsize=512)
def _tokenize(text):
    """多语言分词 + 预processes Optimize。

    LRU 仅保留最近 512 次结果（内存优化：10k 级缓存占 ~40MB，512 级 <2MB）。
    """
    text = _tokenize_preprocess(text)  # Step 1: Date标准化 + 语义词伪装
    text = text.lower()
    tokens = []

    # 1) CJK 汉字：unigram + bigram（中文/日文漢字 共用策略）
    cjk_chars = _RE_CJK.findall(text)
    tokens.extend(cjk_chars)
    for i in range(len(cjk_chars) - 1):
        tokens.append(cjk_chars[i] + cjk_chars[i + 1])

    # 2) 日文平假名：by 连续序列切词（如 "おはよう" → 3-gram滑动窗口）
    for seq in _RE_HIRAGANA.findall(text):
        tokens.append(seq)                       # 全序列
        if len(seq) >= 2:
            for i in range(len(seq) - 1):
                tokens.append(seq[i:i + 2])      # bigram
            if len(seq) >= 3:
                for i in range(len(seq) - 2):
                    tokens.append(seq[i:i + 3])  # trigram

    # 3) 日文片假名：同平假名策略
    for seq in _RE_KATAKANA.findall(text):
        tokens.append(seq)
        if len(seq) >= 2:
            for i in range(len(seq) - 1):
                tokens.append(seq[i:i + 2])
            if len(seq) >= 3:
                for i in range(len(seq) - 2):
                    tokens.append(seq[i:i + 3])

    # 4) 韩文：by 音节块 + bigram（한글 → ["한글", "한", "글"]）
    for seq in _RE_HANGUL.findall(text):
        tokens.append(seq)
        chars = list(seq)
        tokens.extend(chars)                     # 单音节
        for i in range(len(chars) - 1):
            tokens.append(chars[i] + chars[i + 1])  # bigram

    # 5) 西里尔 (俄文等)：序列 + bigram
    for seq in _RE_CYRILLIC.findall(text):
        tokens.append(seq)
        if len(seq) >= 2:
            for i in range(len(seq) - 1):
                tokens.append(seq[i:i + 2])

    # 6) 拉丁/扩展拉丁词（英/法/德/西/意/葡 等）
    latin_words = _RE_LATIN.findall(text)
    tokens.extend([w.lower() for w in latin_words])

    # 7) 数字 + Date模式（跨语言通用）
    tokens.extend(re.findall(r"\d{4}-\d{2}-\d{2}", text))   # ISO Date
    tokens.extend(re.findall(r"\d+\.\d+", text))            # 小数
    tokens.extend(re.findall(r"\b\d{2,}\b", text))          # 多位数

    return tokens


def _tf_vector(tokens):
    return collections.Counter(tokens)


# 多语言实体identifies 模式
_ENTITY_PATTERNS = [
    # 中文书名号/引号 · 日文「」 · 韩文《》
    (r"[《\u300c\u2018\u201c]([^\u300d\u300b\u2019\u201d]{2,30})[\u300d\u300b\u2019\u201d]", "quoted"),
    # 英文大写专有名词
    (r"\b[A-Z][A-Za-z0-9_&.-]{2,}(?:\s+[A-Z][A-Za-z0-9_&.-]{2,}){0,3}\b", "proper_noun"),
    # 日文片假名专有名词（カタカナ词 = 外来语/公司名）
    (r"[\u30a0-\u30ff\u31f0-\u31ff]{2,}", "katakana_word"),
    # 韩文专有名词（한글 2-6字符连续词）
    (r"\b[\uac00-\ud7af]{2,6}\b", "hangul_word"),
    # 邮箱
    (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", "email"),
    # URL
    (r"https?://[^\s,，。；;]+", "url"),
    # Version号
    (r"\bv?\d+\.\d+(?:\.\d+)?(?:-[a-z]+\d*)?\b", "version"),
    # 金额（多币种）
    (r"\b\d+[\d,]*\.?\d*\s*(?:元|万|亿|円|원|₩|¥|€|£|USD|CNY|JPY|KRW|RUB|EUR|RMB)\b", "money"),
    # 百分比
    (r"\b\d+\.?\d*\s*%\b", "percentage"),
    # 俄文大写专有名词
    (r"\b[А-Я][а-яё]{2,}(?:\s+[А-Я][а-яё]{2,}){0,3}\b", "cyrillic_proper"),
]

# 多语言停用词（中/日/韩/英/法/德/西/俄）
_ENTITY_STOP_WORDS = {
    # 中文
    "我们", "你们", "他们", "这", "那", "什么", "怎么", "可以", "需要",
    "时候", "一", "没有", "不是", "自己", "现在", "已经", "因为", "所以",
    "如果", "但是", "还是", "就是", "这样", "那样", "进line", "via ", "对于",
    "关于", "以及", "或者", "能够", "必须", "可能", "应该", "已经", "正在",
    "一种", "这些", "那些", "所有", "per ", "任何", "其他", "其中", "之后",
    "之前", "之间", "之后", "以后", "以上", "以下", "以内",
    # 日文
    "これ", "それ", "あれ", "この", "その", "あの", "ここ", "そこ", "あそこ",
    "私", "僕", "俺", "何", "誰", "どこ", "いつ", "どう", "なぜ", "はい",
    "いいえ", "こと", "もの", "よう", "ため", "ほか", "それぞれ",
    # 韩文
    "이것", "그것", "저것", "우리", "여기", "거기", "저기", "무엇", "언제",
    "어디", "누구", "어떻게", "왜", "네", "아니요", "있다", "없다", "하다",
    # 英文
    "the", "a", "an", "this", "that", "these", "those", "is", "are", "was",
    "were", "be", "been", "being", "have", "has", "had", "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "can", "shall",
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her", "us", "them",
    "my", "your", "his", "its", "our", "their",
    "and", "or", "but", "if", "then", "else", "when", "where", "why", "how",
    "not", "no", "yes", "just", "only", "also", "very", "too", "so", "than",
    # 法文
    "le", "la", "les", "un", "une", "des", "de", "du", "ce", "cette", "ces",
    "je", "tu", "il", "elle", "nous", "vous", "ils", "elles",
    "est", "sont", "être", "avoir", "faire", "pouvoir", "vouloir", "devoir",
    "pas", "ne", "plus", "que", "qui", "quoi", "dans", "sur", "avec", "pour",
    # 德文
    "der", "die", "das", "ein", "eine", "einen", "dem", "den", "des",
    "ich", "du", "er", "sie", "es", "wir", "ihr",
    "ist", "sind", "sein", "haben", "werden", "können", "müssen", "wollen",
    "nicht", "kein", "auch", "noch", "schon", "nur", "aber", "oder", "und",
    # 西班牙文
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del",
    "yo", "tú", "él", "ella", "nosotros", "vosotros", "ellos", "ellas",
    "es", "son", "ser", "estar", "tener", "hacer", "poder", "querer", "saber",
    "no", "sí", "más", "menos", "muy", "mucho", "poco", "también", "pero",
    # 俄文
    "и", "в", "не", "на", "я", "что", "тот", "быть", "с", "он", "а", "как",
    "это", "она", "они", "мы", "от", "для", "по", "из", "у", "за", "же",
    "все", "ещё", "уже", "или", "если", "только", "даже", "нет", "может",
}


def _extract_entities(text):
    """增强实体抽取：多模式+关系推断+停用词filters 。returns  {entity, type, positions}。"""
    entities = []
    seen = set()

    for pattern, etype in _ENTITY_PATTERNS:
        for m in re.finditer(pattern, text):
            entity = m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)
            entity = entity.strip()
            if entity and entity not in seen and len(entity) >= 1:
                entities.append({
                    "entity": entity, "type": etype,
                    "start": m.start(), "end": m.end(),
                })
                seen.add(entity)

    # 中文词（filters 停用词）
    words = re.findall(r"[\u4e00-\u9fff]{2,6}", text)
    for m in re.finditer(r"[\u4e00-\u9fff]{2,6}", text):
        w = m.group(0)
        if w not in _ENTITY_STOP_WORDS and w not in seen:
            entities.append({
                "entity": w, "type": "cjk_word",
                "start": m.start(), "end": m.end(),
            })
            seen.add(w)

    return entities[:30]


def _extract_entity_names(text):
    """兼容1.x接口：returns 实体Name列Table。

    直接正则抽取实体名（不构建 entities_detailed dict），
    快速写入路径（fast=True）的实体内存/CPU 关键优化。
    """
    if not text:
        return []
    names = []
    seen = set()
    for pattern, etype in _ENTITY_PATTERNS:
        for m in re.finditer(pattern, text):
            entity = m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)
            entity = entity.strip()
            if entity and entity not in seen:
                names.append(entity)
                seen.add(entity)
    for m in re.finditer(r"[\u4e00-\u9fff]{2,6}", text):
        w = m.group(0)
        if w not in _ENTITY_STOP_WORDS and w not in seen:
            names.append(w)
            seen.add(w)
    return names[:30]


# 关系动词规则（关系名, 动词列表, 强度）
_RELATION_VERBS = [
    ("responsible_for", ["负责"], 0.6),
    ("participates_in", ["参与", "参加"], 0.5),
    ("belongs_to", ["属于", "位于"], 0.5),
    ("leads", ["领导", "带领"], 0.55),
    ("creates", ["创建", "创立", "建立", "创造"], 0.5),
    ("manages", ["管理", "经营"], 0.45),
    ("is_a", ["是"], 0.3),
]

# 助词/语气词（宾语提取时跳过）
_RELATION_PARTICLES = "了着过呢吧吗啊呀嘛的地得"


def _subject_before(clause, v_start):
    """取动词前紧邻的实体词（词字符连续段，覆盖中文与拉丁/数字）。"""
    i = v_start
    while i > 0 and clause[i - 1] in " \t":
        i -= 1
    if i == 0:
        return ""
    end = i
    j = end - 1
    while j >= 0 and clause[j].isalnum():
        j -= 1
    return clause[j + 1:end].strip()


def _object_after(clause, v_end):
    """取动词后紧邻的实体词（跳过助词与空格）。"""
    i = v_end
    n = len(clause)
    while i < n and (clause[i] in " \t" or clause[i] in _RELATION_PARTICLES):
        i += 1
    if i >= n:
        return ""
    j = i
    while j < n and clause[j].isalnum():
        j += 1
    return clause[i:j].strip()


def _extract_relationships(entities, text):
    """从文本提取动词引导的关系三元组（零依赖规则法）。

    在每个子句（以标点分隔）内，对每个关系动词取动词前紧邻实体为主语、
    动词后紧邻实体为宾语。覆盖：负责/参与/属于/领导/创建/管理/是 等。
    """
    rels = []
    seen = set()
    clauses = re.split(r"[，。！？；：,\n]", text or "")
    for clause in clauses:
        if not clause.strip():
            continue
        for rel_name, verbs, strength in _RELATION_VERBS:
            for verb in verbs:
                start = 0
                while True:
                    idx = clause.find(verb, start)
                    if idx == -1:
                        break
                    v_start, v_end = idx, idx + len(verb)
                    subject = _subject_before(clause, v_start)
                    object_ = _object_after(clause, v_end)
                    if subject and object_ and subject != object_:
                        key = (subject, rel_name, object_)
                        if key not in seen:
                            seen.add(key)
                            rels.append({
                                "from": subject,
                                "to": object_,
                                "relation": rel_name,
                                "strength": strength,
                                "memory_id": None,
                            })
                    start = v_end
    return rels


# ============================================================================
# 语义辅助（零依赖）：同义归一化 / 内容签名 / 配对相似度 / 注入评分
# ============================================================================

_DEGREE_FILLERS = ["非常", "特别", "十分", "尤其", "挺", "蛮", "很", "真", "超", "极"]


@functools.lru_cache(maxsize=1)
def _synonym_canonical_map():
    """构建 变体→规范形 的反向映射（并查集消歧双向同义词 + 时间同义词补充）。"""
    try:
        from lexical.synonyms import SYNONYMS
    except Exception:
        SYNONYMS = {}
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # 1. 同义词词典（按 key 顺序，首个 key 成为规范根）
    for canonical, variants in SYNONYMS.items():
        if canonical:
            union(canonical, canonical)
            for v in variants:
                if v:
                    union(canonical, v)
    # 2. 时间同义词：锚定「今天/昨天/明天」为规范根（覆盖 今日/今儿/昨日/前天/明日）
    for anchor, variants in (("今天", ("今日", "今儿")),
                             ("昨天", ("昨日", "前天")),
                             ("明天", ("明日",))):
        for v in variants:
            union(anchor, v)

    # 规范形 = 各连通分量的根（单向定义下，根即首个 canonical key）
    reverse = {node: find(node) for node in list(parent.keys())}
    return reverse


@functools.lru_cache(maxsize=8192)
def _normalize_template_hash(text):
    """同义归一并去除程度副词/停用词，得到可比较的模板串。

    「今天」/「今日」→「今天」；「天气」/「气候」→「天气」；
    「很好」/「真好」→「好」（先去除程度副词，再同义归一）。
    """
    if not text:
        return ""
    norm = str(text).strip()
    # 1. 先去除程度副词（避免「很好/非常好」被同义词表反向干扰）
    for filler in sorted(_DEGREE_FILLERS, key=len, reverse=True):
        norm = norm.replace(filler, "")
    # 2. 同义归一到规范形
    reverse = _synonym_canonical_map()
    for variant in sorted(reverse.keys(), key=len, reverse=True):
        canonical = reverse[variant]
        if variant != canonical:
            norm = norm.replace(variant, canonical)
    # 3. 去标点/语气词/结构助词 + 高频功能词（是/会/一个/多/从/出来/行为 等，改写对归一化用）
    norm = re.sub(r"[\s，。！？、；：\"\"''（）《》【】\[\]{}的了着过吗呢啊呀吧嘛]", "", norm)
    for _w in ("一个", "出来", "行为", "是", "会", "多", "从"):
        norm = norm.replace(_w, "")
    return norm


def _content_signature(text):
    """粗粒度内容签名：用于聚类，相似内容共享同一签名桶。

    在 _normalize_template_hash 基础上，额外去除结构虚词并把字符排序成
    多重集，使「成立于1976年」与「在1976年成立」这类词序/虚词变化也能归入
    同一桶（阶段3 修复：原签名对词序敏感，同义改写合并率仅 9%）。桶内仍经
    _compute_pair_similarity 精细判定，避免误合并。
    """
    norm = _normalize_template_hash(text)
    if not norm:
        return ""
    # 额外去除结构虚词（仅用于分组粗筛，不参与最终相似度判定）
    for _w in ("于", "在", "到", "使", "令", "被", "把", "与", "及",
               "并", "且", "或", "而", "则", "之", "其", "所", "等",
               "这", "那", "就", "都", "也", "又", "还", "已", "将"):
        norm = norm.replace(_w, "")
    if not norm:
        return ""
    # 字符多重集排序 → 词序无关的稳定签名
    return "".join(sorted(norm))


def _cosine_sim(a, b):
    """稀疏向量余弦相似度（a/b 为 dict: token->weight）。"""
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    if not common:
        return 0.0
    dot = sum(a[k] * b[k] for k in common)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _compute_pair_similarity(r1, r2, enc, embed_engine=None, store=None):
    """两条记忆的配对相似度（向量 + 同义归一化 + 可选嵌入 取最大）。"""
    c1 = r1.get("content", "") if isinstance(r1, dict) else ""
    c2 = r2.get("content", "") if isinstance(r2, dict) else ""
    sims = []
    if enc:
        v1 = enc.get(r1.get("id", ""), {}) if isinstance(r1, dict) else {}
        v2 = enc.get(r2.get("id", ""), {}) if isinstance(r2, dict) else {}
        sims.append(_cosine_sim(v1, v2))
    n1 = _normalize_template_hash(c1)
    n2 = _normalize_template_hash(c2)
    if n1 and n2:
        sims.append(_cosine_sim(_tf_vector(_tokenize(n1)), _tf_vector(_tokenize(n2))))
    if embed_engine is not None:
        try:
            sims.append(embed_engine.similarity(c1, c2))
        except Exception:
            pass
    return max(sims) if sims else 0.0


# 敏感凭据/注入特征（与 notary.py 的注入检测互补，聚焦凭据与隐写）
_CREDENTIAL_PATTERNS = [
    (r"\bAKIA[0-9A-Z]{8,}\b", "credential:aws_access_key"),
    (r"\bAKIA\b", "credential:aws_access_key"),
    (r"aws[_-]?secret", "credential:aws_secret_key"),
    (r"secret[_-]?key", "credential:secret_key"),
    (r"-----BEGIN[ A-Z]*PRIVATE KEY-----", "credential:private_key"),
    (r"password\s*[=:]\s*\S+", "credential:password_assignment"),
    (r"(密码|口令)\s*[=:：是]?\s*[A-Za-z0-9_@#$%^&*+.\-]{4,}", "credential:password_assignment"),
    (r"api[_-]?key\s*[=:]\s*\S+", "credential:api_key"),
    (r"token\s*[=:]\s*[A-Za-z0-9]{8,}", "credential:token_assignment"),
    (r"\bsk-[A-Za-z0-9]{8,}", "credential:openai_key"),
    (r"[A-Za-z0-9+/]{40,}={0,2}", "credential:base64_secret"),
]
_INVISIBLE_CHARS = "\u200b\u200c\u200d\u2060\ufeff\u202e\u202d"


def _injection_score(content):
    """对内容做注入/敏感信息评分，返回 (score, reasons)。

    覆盖：AWS/私钥/密码/API key/token 凭据、不可见 Unicode 隐写、
    HTML 注释注入。
    """
    if not content:
        return 0.0, []
    score = 0.0
    reasons = []
    for pattern, reason in _CREDENTIAL_PATTERNS:
        if re.search(pattern, content):
            score += 0.4
            reasons.append(reason)
    invisible_count = sum(1 for c in content if c in _INVISIBLE_CHARS)
    if invisible_count:
        score += min(0.5, 0.3 * invisible_count)
        reasons.append("invisible_unicode")
    if re.search(r"<!--[\s\S]*?-->", content):
        score += 0.5
        reasons.append("hidden_html_comment")
    if re.search(r"(ignore\s+(all\s+)?previous|system\s*:\s*overwrite|jailbreak)", content, re.I):
        score += 0.5
        reasons.append("instruction_keywords")
    return min(score, 1.0), reasons


# 字段级脱敏正则（v7.0.0 阶段4）：密码/邮箱/卡号/API key/token/私钥等敏感值
# 在写入前替换为掩码，确保敏感字段不以明文落盘。零依赖，纯标准库正则。
_REDACT_RULES = [
    # (正则, 脱敏类型, 替换回调) —— 密码类（英文，值以非空白为界，整体打码）
    (r"(?i)\b(password|passwd|pwd)\b\s*[=:：]\s*\S+",
     "credential:password",
     lambda m: m.group(1) + "=***"),
    # 中文密码/口令：密码=xxx / 密码：xxx
    (r"(密码|口令)\s*[=:：]\s*\S+",
     "credential:password",
     lambda m: m.group(1) + "=***"),
    # 中文密码/口令：密码是xxx / 密码为xxx（值限密钥类 token，避免误伤中文正文）
    (r"(密码|口令)\s*(是|为)\s*[A-Za-z0-9_@#$%^&*+.\-]{1,}",
     "credential:password",
     lambda m: m.group(1) + m.group(2) + "***"),
    # 邮箱：保留首字符 + 域名（等保/PIPL 标准脱敏）
    (r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b",
     "pii:email",
     lambda m: m.group(1) + "***@" + m.group(2)),
    # 银行卡（16 位，允许空格/连字符，保留后 4 位）
    (r"\b(\d{4})[\s-]?(\d{4})[\s-]?(\d{4})[\s-]?(\d{4})\b",
     "pii:card",
     lambda m: "****-****-****-" + m.group(4)),
    # API key / token / secret key / access key（值整体打码）
    (r"(?i)\b(api[_-]?key|token|secret[_-]?key|access[_-]?key)\b\s*[=:：]\s*\S+",
     "credential:api_key",
     lambda m: m.group(1) + "=***"),
    # OpenAI sk- 前缀密钥
    (r"\bsk-[A-Za-z0-9]{8,}",
     "credential:openai_key",
     lambda m: "sk-***"),
    # AWS access key（AKIA + 16 位大写字母数字）
    (r"\bAKIA[0-9A-Z]{16}\b",
     "credential:aws_access_key",
     lambda m: "AKIA***"),
    # 私钥 PEM 头
    (r"-----BEGIN[ A-Z]*PRIVATE KEY-----",
     "credential:private_key",
     lambda m: "[私钥已脱敏]"),
]


def _redact_sensitive_fields(content):
    """字段级脱敏：将密码/邮箱/卡号/API key/token/私钥等敏感值替换为掩码。

    用于 ``retain()`` 写入前，确保敏感字段不以明文存储。返回
    ``(redacted_text, redactions)``，其中 ``redactions`` 为脱敏汇总列表
    （每项 ``{type, count}``），供审计与安全报告使用。

    与 :func:`_injection_score` 的关系：后者负责「检测并打分」，
    本函数负责「脱敏改写」。零依赖，纯标准库。
    """
    if not content:
        return content, []
    text = content
    seen = []

    def _mask(match, rtype, repl):
        seen.append(rtype)
        return repl(match)

    for pattern, rtype, repl in _REDACT_RULES:
        text = re.sub(pattern, lambda m, rtype=rtype, repl=repl: _mask(m, rtype, repl), text)

    summary = [{"type": t, "count": c}
               for t, c in collections.Counter(seen).items()]
    return text, summary


def _memory_value(record):
    """记忆价值函数（遗忘经济学）：重要性/访问次数/可信度/新鲜度/命中率加权。

    返回值越大越「值得保留」。用于 demote_cycle 的降级排序。
    命中率（hit_rate）= 访问次数 / 存续天数（访问频率代理指标）。
    """
    importance = float(record.get("importance", 0) or 0)
    access = float(record.get("access_count", 0) or 0)
    confidence = float(record.get("confidence", 0.5) or 0.5)
    last = (record.get("last_accessed_at") or record.get("last_accessed")
            or record.get("created_at") or "")
    recency = 0.0
    age_days = 1.0
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = max((now - dt).total_seconds() / 86400.0, 1.0)
        recency = max(0.0, 1.0 - age_days / 365.0)
    except (ValueError, TypeError):
        recency = 0.0
    hit_rate = min(access / age_days, 5.0)  # 命中率：次/天，封顶 5
    return (importance * 2.0) + (access * 0.05) + (confidence * 3.0) \
        + (recency * 2.0) + hit_rate


# ============================================================================
# Part 2.5: BloomFilter（量级扩展粗筛 · 纯标准库）
# ============================================================================


class BloomFilter:
    """纯标准库布隆过滤器——量级扩展的粗筛基础设施。

用于大规模（千万级+）时快速排除不含某 token 的分片/记录，
避免全量加载。零依赖，bytearray 位数组 + 双 hash 技巧。"""

    def __init__(self, capacity=10000, error_rate=0.01):
        import math
        self.capacity = int(capacity)
        self.error_rate = float(error_rate)
        m = int(-self.capacity * math.log(self.error_rate) / (math.log(2) ** 2))
        k = int((m / max(self.capacity, 1)) * math.log(2))
        self.size = max(m, 64)
        self.num_hashes = max(k, 1)
        self.bits = bytearray((self.size + 7) // 8)

    def _hashes(self, item):
        import hashlib
        if isinstance(item, str):
            item = item.encode("utf-8")
        h1 = int.from_bytes(hashlib.md5(item).digest()[:8], "little")
        h2 = int.from_bytes(hashlib.sha256(item).digest()[:8], "little")
        for i in range(self.num_hashes):
            yield (h1 + i * h2) % self.size

    def add(self, item):
        for h in self._hashes(item):
            self.bits[h // 8] |= (1 << (h % 8))

    def __contains__(self, item):
        for h in self._hashes(item):
            if not (self.bits[h // 8] & (1 << (h % 8))):
                return False
        return True


class BaseVectorBackend:
    """向量库后端抽象接口（分层协同 L2）。

实现 add/search 即可接入 FAISS/Milvus 等外部向量库。
Mnemosyne 保持零依赖：向量库作为可选 extras，核心不依赖。"""

    def add(self, memory_id, vector):
        """写入一个向量（memory_id → vector）。"""
        raise NotImplementedError

    def search(self, query_vector, top_k=5):
        """检索最相似的 top_k 个，返回 [(memory_id, score), ...]。"""
        raise NotImplementedError


# ============================================================================
# Part 3: Embedding Engine（随机投影向量searches  · 零依赖）
# ============================================================================

class EmbeddingEngine:
    """零依赖向量嵌入引擎。

    基于 Johnson-Lindenstrauss 引理，固定种子的随机投影矩阵将 TF-IDF
    高维稀疏向量投影到低维稠密空间（默认128维），保留Cosine similarity结构。
    内存Cache投影矩阵，首 times初始化约 0.5s 后即实时。
    """

    def __init__(self, dim=EMBEDDING_DIM, seed=42):
        self.dim = dim
        self.seed = seed
        self._proj = None
        self._vocab = {}
        self._vocab_size = 0
        self._next_idx = 0

    def _ensure_proj(self, vocab_size=None):
        """惰性生成固定大小的随机投影矩阵（hash 桶，只生成一次，不复建）。"""
        if self._proj is not None:
            return
        random.seed(self.seed)
        self._proj = [
            [random.gauss(0, 1.0 / math.sqrt(self.dim)) for _ in range(self.dim)]
            for _ in range(PROJ_BUCKETS)
        ]

    def _token_to_idx(self, token):
        # hash 映射到固定桶，避免 vocab 无限增长导致投影矩阵反复重建
        h = hashlib.md5(token.encode('utf-8')).digest()
        return int.from_bytes(h[:4], 'little') % PROJ_BUCKETS

    def encode(self, text_or_tokens):
        """将文本或token列Table编码为稠密向量。"""
        if isinstance(text_or_tokens, str):
            tokens = _tokenize(text_or_tokens)
        else:
            tokens = text_or_tokens
        tf = _tf_vector(tokens)
        return self._encode_tf(tf)

    def encode_batch(self, texts):
        return [self.encode(t) for t in texts]

    def _encode_tf(self, tf_counter):
        token_indices = [self._token_to_idx(t) for t in tf_counter if t in self._vocab or True]
        if not token_indices:
            return [0.0] * self.dim
        self._ensure_proj(max(token_indices) + 1)
        vec = [0.0] * self.dim
        total = sum(tf_counter.values()) or 1
        for tok, freq in tf_counter.items():
            idx = self._token_to_idx(tok)
            if idx < len(self._proj):
                w = freq / total
                row = self._proj[idx]
                for d in range(self.dim):
                    vec[d] += w * row[d]
        # L2归一化
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    def similarity(self, vec_a, vec_b):
        """Cosine similarity。"""
        if not vec_a or not vec_b:
            return 0.0
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        return max(0.0, dot)  # 钳制非负


# ============================================================================
# Part 4: 数据模型（双Time + 可信度 + 事实/观点分离）
# ============================================================================



class StatsTracker:
    """Tracks per-day retain/recall counts, hit rate, latency, and estimated token savings.
    Auto-saves to stats.json in the brain's base directory."""
    def __init__(self, base_dir, tokenizer_backend="simple", tokenizer_model=None):
        self.base_dir = base_dir
        self.path = os.path.join(base_dir, "stats.json")
        self.data = self._load()
        self._today = _today_str()
        self._ensure_day()
        # 当前对话级（不持久化，每次构造重置）
        self._session = {"retain": 0, "recall": 0, "hit": 0, "miss": 0,
                         "total_memory_chars": 0, "total_recalled_chars": 0,
                         "total_potential_chars": 0, "total_latency_ms": 0,
                         "write_tokens": 0, "recall_tokens": 0}
        # Tokenizer：默认 "simple"（纯标准库启发式，零依赖、零联网）。
        # "tiktoken" / "transformers" 仅显式传参时启用（需自行安装对应包）。
        self._tokenizer = self._load_tokenizer(backend=tokenizer_backend, model_id=tokenizer_model)

    @staticmethod
    def _load_tokenizer(backend="simple", model_id=None):
        """加载 token 计数器。

        - "simple"（默认）：返回 None，使用「4 字符 ≈ 1 Token」纯标准库启发式。
        - "tiktoken"：显式启用，注意首次调用会联网下载词表，须自行确保离线可用。
        - "transformers"：显式启用，需传入 model_id 并安装 transformers。
        """
        if backend == "simple":
            return None
        if backend == "transformers" and model_id:
            try:
                from transformers import AutoTokenizer
                return AutoTokenizer.from_pretrained(model_id)
            except Exception:
                return None
        if backend == "tiktoken":
            try:
                import tiktoken
                return tiktoken.get_encoding("cl100k_base")
            except Exception:
                return None
        return None

    @staticmethod
    def _count_tokens(text, tokenizer):
        """用 tiktoken 或回退 4 字符≈1 Token 计算 token 数。"""
        if tokenizer and text:
            try:
                return len(tokenizer.encode(text))
            except Exception:
                pass
        return max(1, len(text) // 4)

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {"daily": {}, "totals": {"retain": 0, "recall": 0, "hit": 0, "miss": 0,
                "total_memory_chars": 0, "total_recalled_chars": 0,
                "total_potential_chars": 0, "total_latency_ms": 0}}

    def _save(self):
        d = os.path.dirname(self.path)
        os.makedirs(d, exist_ok=True)
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def _ensure_day(self):
        if self._today not in self.data["daily"]:
            self.data["daily"][self._today] = {
                "retain": 0, "recall": 0, "hit": 0, "miss": 0,
                "total_memory_chars": 0, "total_recalled_chars": 0,
                "total_potential_chars": 0, "total_latency_ms": 0}

    def track_retain(self, content_len, content=None):
        self._ensure_day()
        d = self.data["daily"][self._today]; t = self.data["totals"]; s = self._session
        d["retain"] += 1; d["total_memory_chars"] += content_len
        t["retain"] += 1; t["total_memory_chars"] += content_len
        s["retain"] += 1; s["total_memory_chars"] += content_len
        if content:
            tk = self._count_tokens(content, self._tokenizer)
            d.setdefault("write_tokens", 0); t.setdefault("write_tokens", 0)
            d["write_tokens"] += tk; t["write_tokens"] += tk; s["write_tokens"] += tk
        self._save()

    def track_recall(self, hit, recalled_chars, latency_ms, potential_chars=0,
                     recalled_text="", potential_text=""):
        """recalled_text/potential_text 用于精确 token 计数（可选 tiktoken）。"""
        self._ensure_day()
        d = self.data["daily"][self._today]; t = self.data["totals"]; s = self._session
        d["recall"] += 1; d["total_recalled_chars"] += recalled_chars
        d.setdefault("total_potential_chars", 0)
        d["total_potential_chars"] += potential_chars
        d["total_latency_ms"] += latency_ms
        t["recall"] += 1; t["total_recalled_chars"] += recalled_chars
        t.setdefault("total_potential_chars", 0)
        t["total_potential_chars"] += potential_chars
        t["total_latency_ms"] += latency_ms
        s["recall"] += 1; s["total_recalled_chars"] += recalled_chars
        s.setdefault("total_potential_chars", 0)
        s["total_potential_chars"] += potential_chars
        s["total_latency_ms"] += latency_ms
        if hit: d["hit"] += 1; t["hit"] += 1; s["hit"] += 1
        else: d["miss"] += 1; t["miss"] += 1; s["miss"] += 1
        # Token 精确计数
        if recalled_text:
            rt = self._count_tokens(recalled_text, self._tokenizer)
            d.setdefault("recall_tokens", 0); t.setdefault("recall_tokens", 0)
            d["recall_tokens"] += rt; t["recall_tokens"] += rt; s["recall_tokens"] += rt
        if potential_text:
            pt = self._count_tokens(potential_text, self._tokenizer)
            d.setdefault("potential_tokens", 0); t.setdefault("potential_tokens", 0)
            d["potential_tokens"] += pt; t["potential_tokens"] += pt
        self._save()

    def summary(self):
        t = self.data["totals"]; d = self.data["daily"].get(self._today, {})
        total_mem = t.get("total_memory_chars", 0)
        total_rec = t.get("total_recalled_chars", 0)
        total_potential = t.get("total_potential_chars", total_mem)
        recalls = t.get("recall", 0)
        day_recalls = d.get("recall", 0)
        if recalls == 0: recalls = 1  # 仅用于平均延迟等兜底
        if day_recalls == 0: day_recalls = 1
        today_mem = d.get("total_memory_chars", 0)
        today_rec = d.get("total_recalled_chars", 0)
        today_potential = d.get("total_potential_chars", today_mem)
        # Token 计数：优先 tiktoken，回退 4字符≈1Token
        today_wt = d.get("write_tokens", today_mem // 4)
        today_rt = d.get("recall_tokens", today_rec // 4)
        today_pt = d.get("potential_tokens", today_potential // 4)
        total_wt = t.get("write_tokens", total_mem // 4)
        total_rt = t.get("recall_tokens", total_rec // 4)
        total_pt = t.get("potential_tokens", total_potential // 4)
        session_wt = self._session.get("write_tokens", self._session["total_memory_chars"] // 4)
        session_rt = self._session.get("recall_tokens", self._session["total_recalled_chars"] // 4)
        sess_potential = max(self._session.get("total_potential_chars", 0), self._session["total_memory_chars"])
        est_saved = max(0, total_pt - total_rt)
        return {
            "today": self._today,
            "today_retain": d.get("retain", 0), "today_recall": d.get("recall", 0),
            "today_hit": d.get("hit", 0), "today_miss": d.get("miss", 0),
            "today_hit_rate": round(d.get("hit", 0) / day_recalls, 3) if d.get("recall", 0) > 0 else 0.0,
            "today_avg_latency_ms": round(d.get("total_latency_ms", 0) / day_recalls, 1) if d.get("recall", 0) > 0 else 0.0,
            # --- Token 全维度（今日） ---
            "today_write_chars": today_mem,
            "today_write_tokens": today_wt,
            "today_recall_chars": today_rec,
            "today_recall_tokens": today_rt,
            "today_sent_to_llm_tokens": today_rt,
            "today_potential_tokens": today_pt,
            "today_saved_tokens": max(0, today_pt - today_rt),
            "today_llm_feed_pct": round(today_rt / max(today_pt, 1) * 100, 1),
            # --- Token 全维度（当前对话） ---
            "session_retain": self._session["retain"],
            "session_recall": self._session["recall"],
            "session_hit": self._session["hit"],
            "session_miss": self._session["miss"],
            "session_hit_rate": round(self._session["hit"] / max(self._session["recall"], 1), 3) if self._session["recall"] > 0 else 0.0,
            "session_write_tokens": self._session.get("write_tokens", self._session["total_memory_chars"] // 4),
            "session_recall_tokens": self._session.get("recall_tokens", self._session["total_recalled_chars"] // 4),
            "session_sent_to_llm_tokens": self._session.get("recall_tokens", self._session["total_recalled_chars"] // 4),
            "session_saved_tokens": max(0, (self._session.get("total_potential_chars", self._session["total_memory_chars"]) // 4)),
            "session_llm_feed_pct": round(self._session["total_recalled_chars"] / max(self._session.get("total_potential_chars", self._session["total_memory_chars"]), 1) * 100, 1),
            # --- 累计 ---
            "total_retain": t.get("retain", 0), "total_recall": t.get("recall", 0),
            "total_hit": t.get("hit", 0), "total_miss": t.get("miss", 0),
            "total_hit_rate": round(t.get("hit", 0) / recalls, 3) if t.get("recall", 0) > 0 else 0.0,
            "total_avg_latency_ms": round(t.get("total_latency_ms", 0) / recalls, 1),
            "total_memory_chars": total_mem, "total_recalled_chars": total_rec,
            "total_potential_chars": total_potential,
            "estimated_tokens_saved": est_saved,
            # --- Token 全维度（累计） ---
            "write_tokens": total_wt,
            "recall_tokens": total_rt,
            "sent_to_llm_tokens": total_rt,
            "potential_tokens": total_pt,
            "saved_tokens": est_saved,
            "llm_feed_pct": round(total_rt / max(total_pt, 1) * 100, 1),
            # --- 元信息 ---
            "active_days": len(self.data.get("daily", {})),
        }

    def print_summary(self, price_per_million=None):
        s = self.summary()
        # 当前对话的送入比例
        sess_w = max(s['session_write_tokens'], 1)
        sess_potential = s.get('session_write_tokens', sess_w) + s.get('session_saved_tokens', 0)
        sess_potential = max(sess_potential, 1)
        sess_feed_pct = round(s['session_recall_tokens'] / sess_potential * 100, 1)
        print(f"\n📊 Mnemosyne OS 记忆系统监控数据\n")
        print(f"| 维度 | 当前对话 | 今日 | 累计 |")
        print(f"|------|------|------|------|")
        print(f"| 📝写入Token | {s['session_write_tokens']} | {s['today_write_tokens']} | {s['write_tokens']} |")
        print(f"| 🔍召回Token | {s['session_recall_tokens']} | {s['today_recall_tokens']} | {s['recall_tokens']} |")
        print(f"| 🤖送入LLM | {s['session_sent_to_llm_tokens']} | {s['today_sent_to_llm_tokens']} | {s['sent_to_llm_tokens']} |")
        print(f"| 🛡️拦截Token | {s['session_saved_tokens']} | {s['today_saved_tokens']} | {s['saved_tokens']} |")
        print(f"| 🔢检索总次数 | {s['session_recall']} | {s['today_recall']} | {s['total_recall']} |")
        print(f"| 🎯命中(次) | {s['session_hit']} | {s['today_hit']} | {s['total_hit']} |")
        print(f"| ✅命中率 | {s['session_hit_rate']:.0%} | {s['today_hit_rate']:.0%} | {s['total_hit_rate']:.0%} |")
        print(f"| 📈LLM送入比例 | {s['session_llm_feed_pct']:.1f}% | {s['today_llm_feed_pct']:.1f}% | {s['llm_feed_pct']:.1f}% |")
        print(f"| 📦压缩率 | {100-s['session_llm_feed_pct']:.1f}% | {100-s['today_llm_feed_pct']:.1f}% | {100-s['llm_feed_pct']:.1f}% |")
        print()


# ═══════════════════════════════════════════════
INDEX_NAME = "index.jsonl"
GRAPH_NAME = "graph.jsonl"
META_NAME = "meta.json"

# 随机投影维度（零依赖向量searches ）
EMBEDDING_DIM = 128
# 随机投影矩阵固定桶数：token 经 hash 映射到 [0, PROJ_BUCKETS)，矩阵固定大小、只生成一次
PROJ_BUCKETS = 2048


