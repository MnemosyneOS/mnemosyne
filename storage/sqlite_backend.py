"""SqliteBackend — sqlite3 + FTS5 storage backend for Mnemosyne OS.

Zero-dependency (sqlite3 is stdlib). Compatible with the MemoryStore
interface so it can be used as a drop-in replacement for the JSONL backend.

Key features:
  • WAL mode for concurrent read / write.
  • FTS5 virtual table (memories_fts) for full-text BM25 search.
  • Chinese bigram search via the unicode61 fallback:
      – Source column ``ft_content`` holds space-separated CJK bigrams.
      – Query is also converted to bigrams and used with MATCH.
  • Multi-condition filtering via SQL WHERE clauses.
  • Transaction rollback safety (all writes in transactions).
  • Incremental indexing (triggers keep FTS in sync automatically).
"""

import base64
import collections
import gzip as _gzip
import json
import os
import sqlite3
import struct
import threading
import time

from .bigram import _tokenize, _tf_vector

__all__ = ["SqliteBackend"]

# ---------------------------------------------------------------------------
# Constants (mirrors mnemosyne module)
# ---------------------------------------------------------------------------
INDEX_NAME = "index.jsonl"
META_NAME = "meta.json"
VERSION = "7.0.0"


def _m():
    """Lazy-import the mnemosyne module for helper functions."""
    import mnemosyne as _mnemosyne_mod
    return _mnemosyne_mod


def _now_iso():
    return _m()._now_iso()


def _stable_id(content, salt=""):
    return _m()._stable_id(content, salt)


def _upgrade_record(rec):
    return _m()._upgrade_record(rec)


# ---------------------------------------------------------------------------
# Column list for the memories table
# ---------------------------------------------------------------------------
# 'content' = full text (for display)
# 'ft_content' = bigram-tokenized text (for FTS5 external content table)
_MEMORIES_COLUMNS = [
    "id", "project", "session_id", "mtype", "layer", "content",
    "ft_content", "content_hash", "template_hash",
    "fact_type", "confidence", "importance",
    "event_time", "knowledge_time", "created_at",
    "updated_at", "tier", "access_count",
    "last_accessed", "status", "embedding",
    "summary",
    # Extended fields (stored for full record compatibility)
    "tags", "entities", "entities_detailed", "source", "context",
    "expires_at", "version", "parent_id", "supersedes", "superseded_by",
    "topic_tag", "consolidated_from", "consolidated_at",
    "verification", "source_type", "tool_name",
    "last_accessed_at", "deleted_at",
    "merged_ids",
    # Notary fields (Module 7.3 Innovation 3)
    "flags", "notary_evidence",
]


def _ft_content(text):
    """Produce an FTS5-storable string for *text*.

    Strategy (unicode61 fallback as described in the spec):
    Join all tokens with spaces.  CJK bigrams become individual tokens,
    so a query "苹果" → tokens ["苹", "果", "苹果"] stored as
    "苹 果 苹果" and matched by FTS5 unicode61 on those tokens.
    """
    tokens = _tokenize(text or "")
    return " ".join(tokens)


def _record_to_row(record):
    """Convert a memory record dict to a tuple matching _MEMORIES_COLUMNS."""
    # Embedding: pack as float32 struct if list, else store as text bytes
    emb = record.get("embedding")
    emb_blob = None
    if emb is not None:
        try:
            if isinstance(emb, (list, tuple)):
                emb_blob = struct.pack(
                    f"{len(emb)}f", *[float(x) for x in emb]
                )
            elif isinstance(emb, str):
                emb_blob = emb.encode("utf-8")
            elif isinstance(emb, (bytes, bytearray)):
                emb_blob = bytes(emb)
        except Exception:
            emb_blob = str(emb).encode("utf-8")

    content = record.get("content", "")
    ft = _ft_content(content)

    def _j(val):
        if val is None:
            return None
        return json.dumps(val, ensure_ascii=False)

    # template_hash may be in meta dict or top-level
    th = record.get("template_hash")
    if not th and isinstance(record.get("meta"), dict):
        th = record.get("meta", {}).get("template_hash")

    row = []
    for col in _MEMORIES_COLUMNS:
        if col == "content":
            val = content
        elif col == "ft_content":
            val = ft
        elif col == "template_hash":
            val = th
        elif col == "last_accessed_at":
            val = record.get("last_accessed_at")
        elif col == "mtype":
            val = record.get("type")
        elif col == "embedding":
            val = emb_blob
        elif col == "confidence":
            val = float(record.get("confidence", 0.7)) if record.get("confidence") is not None else None
        elif col == "importance":
            val = record.get("importance")
        elif col in ("tags", "entities", "entities_detailed", "consolidated_from",
                     "merged_ids", "flags", "notary_evidence"):
            val = _j(record.get(col))
        elif col == "source":
            val = _j(record.get(col)) if record.get(col) else None
        elif col == "entities_detailed":
            val = _j(record.get(col))
        elif col == "tier":
            val = record.get("tier") or "hot"
        elif col == "status":
            val = record.get("status") or "active"
        else:
            val = record.get(col)
        row.append(val)
    return tuple(row)


def _row_to_record(row, keys=None):
    """Convert a sqlite3.Row back to a full memory record dict.

    内存优化：值为 None 的列不写入 dict（消费方均以 .get() 访问，
    语义与 None 一致），100k 规模每条省 ~0.4KB。
    keys: 可选列白名单——只物化指定列（轻量记录），跳过无关列，
    轻量索引重建时避免完整记录 dict 双份驻留（100k 内存关键优化）。
    """
    cols = [c for c in _MEMORIES_COLUMNS if keys is None or c in keys]
    d = {}
    for col in cols:
        val = row[col] if col in row.keys() else None
        if val is not None:
            d[col] = val

    # Handle embedding BLOB → list of floats
    emb = d.get("embedding")
    d.pop("embedding", None)
    if emb is not None:
        try:
            n = len(emb) // 4
            d["embedding"] = list(struct.unpack(f"{n}f", emb))
        except Exception:
            try:
                d["embedding"] = json.loads(emb.decode("utf-8"))
            except Exception:
                d["embedding"] = emb.decode("utf-8", errors="replace")

    # Restore "type" from "mtype"
    d["type"] = d.get("mtype")
    d.pop("ft_content", None)  # not part of record dict

    # 数值字段归一化：version 历史表为 TEXT 列，读回时强制转 int，
    # 避免版本追踪/时序排序时 str+int 混用崩溃（见 retain 的 supersede 路径）。
    for _intcol in ("version", "importance", "access_count"):
        if d.get(_intcol) is not None:
            try:
                d[_intcol] = int(d[_intcol])
            except (TypeError, ValueError):
                pass

    # JSON-decode list/dict fields（值存在时才解码；None 时保持缺省）
    for col in ("tags", "entities", "entities_detailed", "consolidated_from",
                "merged_ids", "flags", "notary_evidence"):
        val = d.get(col)
        if val is None:
            continue  # 保持缺省（等价于 []）
        if isinstance(val, str):
            try:
                d[col] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                d[col] = []
        else:
            d[col] = val

    for col in ("source",):
        val = d.get(col)
        if val is None:
            continue  # 保持缺省（等价于 None）
        if isinstance(val, str):
            try:
                d[col] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                d[col] = val
        else:
            d[col] = val

    # Rebuild meta dict for compatibility with MemoryBrain
    d["meta"] = {
        "template_hash": d.get("template_hash"),
    }

    return d


class SqliteBackend:
    """sqlite3 + FTS5 storage backend, interface-compatible with MemoryStore.

    Drop-in replacement: expose the same public methods the rest of the
    codebase relies on (append, all_records, iter_records, find_by_id,
    update_by_id, rewrite, ensure_init, read_meta, close, repair, etc.)
    plus a *search* method backed by FTS5 BM25.

    Multi-tenant isolation (Module 5.4 item 9 / Module 7.3):
      When *namespace* is given, the database file lives under
      ``<base_dir>/data/namespaces/<namespace>/memory.db`` so that two
      namespaces are physically isolated — different files, no chance of
      cross-tenant data leakage at the SQL level.
    """

    # ------------------------------------------------------------------
    # namespace → directory helpers
    # ------------------------------------------------------------------
    NAMESPACE_ROOT = "data/namespaces"

    @staticmethod
    def _resolve_paths(base_dir, db_name=None, namespace=None):
        """Return (db_path, meta_path) for a given base_dir + optional namespace.

        When *namespace* is set, the database lives under
        ``base_dir/data/namespaces/<namespace>/`` and the meta file
        lives alongside it so that meta is also tenant-scoped.
        """
        base_dir = os.path.abspath(base_dir)
        if namespace:
            ns_dir = os.path.join(base_dir, SqliteBackend.NAMESPACE_ROOT, namespace)
            return (
                os.path.join(ns_dir, "memory.db"),
                os.path.join(ns_dir, META_NAME),
                ns_dir,
            )
        # legacy / default-namespace layout: flat
        return (
            os.path.join(base_dir, db_name or "memory.db"),
            os.path.join(base_dir, META_NAME),
            base_dir,
        )

    def __init__(self, base_dir=None, db_name=None, namespace=None,
                 hot_cache_size=1000):
        if base_dir is None:
            base_dir = os.path.join(os.path.expanduser("~"), ".mnemosyne")
        self.namespace = namespace
        self.db_path, self.meta_path, self.base_dir = self._resolve_paths(
            base_dir, db_name, namespace
        )
        self.index_path = self.db_path

        self._lock = threading.RLock()
        self._conn = None
        self._meta_cache = None
        self._meta_pending = 0
        self._cache = None
        self._initialized = False  # ensure_init 幂等标志：进程内仅执行一次 schema 检查
        # 热层内存 LRU 缓存（可配置，0 表示关闭）：
        # find_by_id / append / update 命中时更新，超容量按 LRU 淘汰。
        self.hot_cache_size = int(hot_cache_size or 0)
        self._hot_cache = collections.OrderedDict()
        self._bloom = None  # 冷层布隆过滤器（惰性加载）

    # -- connection management -----------------------------------------------

    def _get_conn(self):
        if self._conn is None:
            self._conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                isolation_level=None,
                timeout=30,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA temp_store=MEMORY")
            self._conn.execute("PRAGMA mmap_size=268435456")
            self._conn.row_factory = sqlite3.Row
        return self._conn

    @property
    def conn(self):
        return self._get_conn()

    # -- schema --------------------------------------------------------------

    def ensure_init(self):
        """Create tables, FTS5 virtual table, and triggers if missing.

        幂等：进程内首次调用执行完整 schema 检查，之后直接返回
        （此前每次 append/audit 都重复执行约 20 条 DDL，是写入延迟热点）。
        """
        if self._initialized:
            return
        os.makedirs(self.base_dir, exist_ok=True)
        conn = self._get_conn()
        with self._lock:
            cur = conn.cursor()

            # 1) memories table
            col_defs = []
            for col in _MEMORIES_COLUMNS:
                if col == "id":
                    col_defs.append("id TEXT PRIMARY KEY")
                elif col == "rowid":
                    col_defs.append("rowid INTEGER PRIMARY KEY AUTOINCREMENT")
                elif col == "confidence":
                    col_defs.append(f"{col} REAL")
                elif col == "importance":
                    col_defs.append(f"{col} INTEGER")
                elif col == "access_count":
                    col_defs.append(f"{col} INTEGER DEFAULT 0")
                elif col == "embedding":
                    col_defs.append(f"{col} BLOB")
                elif col == "tier":
                    col_defs.append(f"{col} TEXT DEFAULT 'hot'")
                elif col == "status":
                    col_defs.append(f"{col} TEXT DEFAULT 'active'")
                elif col == "version":
                    col_defs.append(f"{col} INTEGER DEFAULT 1")
                else:
                    col_defs.append(f"{col} TEXT")
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS memories ({', '.join(col_defs)})"
            )
            # 1b) Migrate existing databases: add flags / notary_evidence columns
            #     (Module 7.3 Innovation 3 — these may not exist in pre-7.3 DBs)
            existing_cols = {row[1] for row in cur.execute("PRAGMA table_info(memories)")}
            for _new_col in ("flags", "notary_evidence"):
                if _new_col not in existing_cols:
                    try:
                        cur.execute(f"ALTER TABLE memories ADD COLUMN {_new_col} TEXT")
                    except sqlite3.OperationalError:
                        pass  # column may already exist in a race

            # 2) entities
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS entities (
                    entity    TEXT,
                    memory_id TEXT,
                    PRIMARY KEY(entity, memory_id)
                )
                """
            )

            # 3) edges
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS edges (
                    from_entity TEXT,
                    to_entity   TEXT,
                    relation    TEXT,
                    memory_id   TEXT,
                    strength    REAL DEFAULT 0.5,
                    created_at  TEXT
                )
                """
            )

            # 4) audit_log
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT,
                    actor TEXT,
                    action TEXT,
                    target_id TEXT,
                    details TEXT
                )
                """
            )

            # 5) ledger (created only; logic not yet implemented)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ledger (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    prev_hash  TEXT,
                    record_hash TEXT,
                    payload    TEXT,
                    ts         TEXT
                )
                """
            )

            # 6) confidence_history — tracks every confidence update for the
            #    Memory Notary (Module 7.3 Innovation 3: dynamic confidence trajectory)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS confidence_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id TEXT,
                    ts          TEXT,
                    confidence  REAL,
                    reason      TEXT,
                    delta       REAL,
                    flags       TEXT
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_conf_history_mid "
                "ON confidence_history(memory_id)"
            )

            # 6) FTS5 contentless virtual table
            #    ft_content column holds bigram tokens for Chinese search.
            #    content='' makes it contentless (index-only, no content stored).
            #    Triggers manually keep the FTS index in sync.
            cur.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                USING fts5(
                    ft_content,
                    content='',
                    tokenize='unicode61'
                )
                """
            )

            # 7) Triggers to keep FTS in sync (contentless table)
            cur.execute(
                """
                CREATE TRIGGER IF NOT EXISTS memories_fts_ai
                AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid, ft_content)
                    VALUES (new.rowid, new.ft_content);
                END
                """
            )
            cur.execute(
                """
                CREATE TRIGGER IF NOT EXISTS memories_fts_ad
                AFTER DELETE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, ft_content)
                    VALUES('delete', old.rowid, old.ft_content);
                END
                """
            )
            cur.execute(
                """
                CREATE TRIGGER IF NOT EXISTS memories_fts_au
                AFTER UPDATE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, ft_content)
                    VALUES('delete', old.rowid, old.ft_content);
                    INSERT INTO memories_fts(rowid, ft_content)
                    VALUES (new.rowid, new.ft_content);
                END
                """
            )

            # 8) Performance indexes
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_session ON memories(session_id)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_tier ON memories(tier)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_entities_mem ON entities(memory_id)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_entity)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_entity)"
            )
            # Unique index for edge de-duplication (guarded: no-op if dupes exist).
            # Note: (from, to, relation) is the natural key — the same
            # relationship should not be stored twice regardless of how many
            # memories mention it.  memory_id is intentionally excluded.
            try:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_unique "
                    "ON edges(from_entity, to_entity, relation)"
                )
            except Exception:
                pass  # pre-existing duplicates — skip; write-time dedup still applies

            conn.commit()
            self._initialized = True

        # Meta file (mirrors MemoryStore)
        if not os.path.exists(self.meta_path):
            meta = {
                "schema": "mnemosyne-sqlite-v1",
                "created_at": _now_iso(),
                "version": VERSION,
                "count": 0,
                "backend": "sqlite",
            }
            self._write_meta(meta)

    # -- meta ----------------------------------------------------------------

    def _write_meta(self, meta):
        self._meta_cache = meta
        self._meta_pending = 0
        tmp = self.meta_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, ensure_ascii=False, indent=2, fp=f)
        os.replace(tmp, self.meta_path)

    def read_meta(self):
        if self._meta_cache is not None:
            return self._meta_cache
        if not os.path.exists(self.meta_path):
            return None
        try:
            with open(self.meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            self._meta_cache = meta
            return meta
        except Exception:
            return None

    def _update_meta_count(self, delta=1):
        """Increment the count in the in-memory meta cache; flush if pending
        threshold is reached."""
        meta = self._meta_cache
        if meta is None:
            meta = self.read_meta()
            if meta is None:
                meta = {
                    "schema": "mnemosyne-sqlite-v1",
                    "created_at": _now_iso(),
                    "version": VERSION,
                    "count": 0,
                    "backend": "sqlite",
                }
                self._meta_cache = meta
        meta["count"] = meta.get("count", 0) + delta
        meta["updated_at"] = _now_iso()
        self._meta_pending += 1
        if self._meta_pending >= 100:
            try:
                self._write_meta(meta)
            except OSError:
                pass

    # -- record-level operations (compatible with MemoryStore) ---------------

    def append(self, record, retries=3, durable=False):
        """Append a single memory record. Returns the record dict."""
        self.ensure_init()
        conn = self._get_conn()
        if not record.get("id"):
            record["id"] = _stable_id(
                record.get("content", ""), str(time.time())
            )
        last_err = None
        for attempt in range(1, retries + 1):
            try:
                with self._lock:
                    conn.execute("BEGIN")
                    try:
                        placeholders = ", ".join("?" * len(_MEMORIES_COLUMNS))
                        row = _record_to_row(record)
                        conn.execute(
                            f"INSERT OR REPLACE INTO memories "
                            f"({', '.join(_MEMORIES_COLUMNS)}) "
                            f"VALUES ({placeholders})",
                            row,
                        )
                        self._store_entities(conn, record)
                        conn.commit()
                        break
                    except Exception:
                        conn.rollback()
                        raise
                break
            except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
                last_err = e
                if attempt < retries:
                    time.sleep(0.05 * attempt)
        else:
            raise OSError(
                f"sqlite 写存记忆失败（已Retry {retries} 次）：{last_err}"
            )
        self._invalidate_cache()
        self._update_meta_count(delta=1)
        self._hot_put(record.get("id"), self._hot_normalize(record))
        return record

    def append_batch(self, records, retries=3):
        """Batch append records."""
        self.ensure_init()
        conn = self._get_conn()
        for rec in records:
            if not rec.get("id"):
                rec["id"] = _stable_id(
                    rec.get("content", ""), str(time.time())
                )
        with self._lock:
            conn.execute("BEGIN")
            try:
                placeholders = ", ".join("?" * len(_MEMORIES_COLUMNS))
                rows = [_record_to_row(rec) for rec in records]
                conn.executemany(
                    f"INSERT OR REPLACE INTO memories "
                    f"({', '.join(_MEMORIES_COLUMNS)}) "
                    f"VALUES ({placeholders})",
                    rows,
                )
                for rec in records:
                    self._store_entities(conn, rec)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        self._invalidate_cache()
        self._update_meta_count(delta=len(records))
        for rec in records:
            self._hot_put(rec.get("id"), self._hot_normalize(rec))
        return records

    @staticmethod
    def _store_entities(conn, record):
        """Store entities for a record into the entities table."""
        ent_col = record.get("entities") or []
        ent_rows = []
        for e in ent_col:
            if isinstance(e, str):
                ent_rows.append((e, record["id"]))
            elif isinstance(e, dict):
                ent_rows.append((e.get("entity", ""), record["id"]))
        if ent_rows:
            conn.executemany(
                "INSERT OR IGNORE INTO entities(entity, memory_id) VALUES (?, ?)",
                ent_rows,
            )

    def iter_records(self, keys=None):
        """Yield records one by one (streaming).

        keys: 可选列白名单——只 SELECT 指定列并物化轻量记录（内存优化）。
        """
        self.ensure_init()
        conn = self._get_conn()
        cols = ", ".join(keys) if keys else "*"
        with self._lock:
            cur = conn.execute(f"SELECT {cols} FROM memories ORDER BY rowid")
            for row in cur:
                yield _row_to_record(row, keys=keys)

    def all_records(self, keys=None):
        """Return a cached list of all records.

        keys: 可选列白名单——给定时不写缓存、直接流式构建轻量记录，
        供检索轻量索引重建使用（避免完整记录 dict 双份驻留）。
        """
        if keys is not None:
            return [r for r in self.iter_records(keys=keys)]
        if self._cache is None:
            self._cache = list(self.iter_records())
        return self._cache

    def _invalidate_cache(self):
        self._cache = None

    def find_by_id(self, memory_id):
        """Find a single record by id (hot LRU cache first)."""
        if self.hot_cache_size > 0 and memory_id in self._hot_cache:
            rec = self._hot_cache.pop(memory_id)
            self._hot_cache[memory_id] = rec  # move to end (recently used)
            return rec
        self.ensure_init()
        conn = self._get_conn()
        with self._lock:
            cur = conn.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            )
            row = cur.fetchone()
        if row is None:
            return None
        rec = _row_to_record(row)
        self._hot_put(memory_id, rec)
        return rec

    def _hot_put(self, memory_id, rec):
        """Insert/refresh a record into the hot LRU cache (no-op if disabled)."""
        if self.hot_cache_size <= 0:
            return
        if memory_id in self._hot_cache:
            self._hot_cache.pop(memory_id)
        self._hot_cache[memory_id] = rec
        while len(self._hot_cache) > self.hot_cache_size:
            self._hot_cache.popitem(last=False)  # 淘汰最久未用

    @staticmethod
    def _hot_normalize(record):
        """补全记录默认值，使缓存内容与数据库默认值一致（tier/status/access_count）。"""
        rec = dict(record)
        rec.setdefault("tier", "hot")
        rec.setdefault("status", "active")
        rec.setdefault("access_count", 0)
        return rec

    def _hot_drop(self, memory_id):
        """Remove a record from the hot LRU cache."""
        self._hot_cache.pop(memory_id, None)

    def update_by_id(self, memory_id, updates):
        """Update fields of a record by id. Returns True if found."""
        self.ensure_init()
        conn = self._get_conn()

        col_map = {
            "type": "mtype",
        }
        update_fields = {}
        for k, v in updates.items():
            col = col_map.get(k, k)
            if col in _MEMORIES_COLUMNS:
                # JSON-encode list/dict fields for sqlite TEXT columns
                if col == "embedding":
                    # Embedding: pack as float32 struct if list
                    if isinstance(v, (list, tuple)):
                        import struct as _struct
                        try:
                            update_fields[col] = _struct.pack(
                                f"{len(v)}f", *[float(x) for x in v]
                            )
                        except Exception:
                            update_fields[col] = None
                    elif v is None:
                        update_fields[col] = None
                    else:
                        update_fields[col] = v
                elif isinstance(v, (list, dict)):
                    update_fields[col] = json.dumps(v, ensure_ascii=False)
                else:
                    update_fields[col] = v

        # Recompute ft_content if content changed
        if "content" in update_fields:
            update_fields["ft_content"] = _ft_content(update_fields["content"])

        if not update_fields:
            return False

        set_clause = ", ".join(f"{k} = ?" for k in update_fields)
        params = list(update_fields.values()) + [memory_id]
        with self._lock:
            conn.execute("BEGIN")
            try:
                cur = conn.execute(
                    f"UPDATE memories SET {set_clause} WHERE id = ?", params
                )
                conn.commit()
                found = cur.rowcount > 0
            except Exception:
                conn.rollback()
                raise
        if found:
            self._invalidate_cache()
            self._hot_drop(memory_id)
        return found

    def rewrite(self, records):
        """Replace all records (used by dedup, forget, eviction)."""
        self.ensure_init()
        conn = self._get_conn()
        with self._lock:
            conn.execute("BEGIN")
            try:
                conn.execute("DELETE FROM memories")
                conn.execute("DELETE FROM entities")
                placeholders = ", ".join("?" * len(_MEMORIES_COLUMNS))
                rows = [_record_to_row(r) for r in records]
                conn.executemany(
                    f"INSERT OR REPLACE INTO memories "
                    f"({', '.join(_MEMORIES_COLUMNS)}) "
                    f"VALUES ({placeholders})",
                    rows,
                )
                for rec in records:
                    self._store_entities(conn, rec)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        self._invalidate_cache()
        self._hot_cache.clear()
        meta = self.read_meta() or {}
        meta["count"] = len(records)
        meta["updated_at"] = _now_iso()
        self._write_meta(meta)
        return records

    # -- FTS5 search ---------------------------------------------------------

    def search_ids(self, query, k=5, project=None, mtype=None, or_mode=False):
        """FTS5 检索仅返回 (id, rank) 列表——不构建完整记录。

        检索引擎的候选获取（top-500）只需 id + 排名，
        避免每次查询反序列化数百条完整记录（100k 规模 P50 的关键优化）。
        """
        self.ensure_init()
        conn = self._get_conn()

        ft_match = _ft_content(query).strip()
        if ft_match:
            for char in ["(", ")", "*", "\"", "\\"]:
                ft_match = ft_match.replace(char, f"\\{char}")
        if not ft_match:
            ft_match = query.strip()
        if not ft_match:
            return []
        for char in [".", ",", "(", ")", "*", "\"", "\\"]:
            ft_match = ft_match.replace(char, "")
        if or_mode:
            _tokens = [t for t in ft_match.split() if t]
            if not _tokens:
                return []
            ft_match = " OR ".join(_tokens)

        k_fetch = max(1, int(k))
        sql = """
            SELECT m.id, bm25(memories_fts) AS rank
            FROM memories_fts
            JOIN memories m ON m.rowid = memories_fts.rowid
            WHERE memories_fts MATCH ?
              AND (m.status NOT IN ('deleted', 'consolidated', 'pending_eviction') OR m.status IS NULL)
        """
        params = [fts_match]
        if project is not None:
            sql += " AND m.project = ?"
            params.append(project)
        if mtype is not None:
            sql += " AND m.mtype = ?"
            params.append(mtype)
        sql += " ORDER BY rank LIMIT ?"
        params.append(k_fetch)
        with self._lock:
            cur = conn.execute(sql, params)
            rows = cur.fetchall()
        return [(row[0], row[1]) for row in rows]

    def search(self, query, k=5, project=None, mtype=None, tag=None,
               date_from=None, date_to=None, min_confidence=0.0,
               or_mode=False):
        """FTS5-based search returning list of (record_dict, score) tuples.

        Chinese search: query is converted to bigram tokens and used
        to MATCH the ft_content column (which stores bigrams).
        or_mode=True 时 token 以 OR 连接（部分匹配召回，AND 无结果时的补充）。
        """
        self.ensure_init()
        conn = self._get_conn()

        ft_query = _ft_content(query)
        fts_match = ft_query.strip()
        # Escape FTS5 special characters in the MATCH expression
        if fts_match:
            for char in ["(", ")", "*", "\"", "\\"]:
                fts_match = fts_match.replace(char, f"\\{char}")
        if not fts_match:
            fts_match = query.strip()
        if not fts_match:
            # FTS5 cannot handle empty MATCH expressions
            return []
        # Remove problematic FTS5 syntax characters (e.g., "." in "365.25")
        for char in [".", ",", "(", ")", "*", "\"", "\\"]:
            fts_match = fts_match.replace(char, "")
        if or_mode:
            # OR 语义：所有 token 以 OR 连接（AND 无结果时的部分匹配召回）
            _tokens = [t for t in fts_match.split() if t]
            if not _tokens:
                return []
            fts_match = " OR ".join(_tokens)

        # FTS5 直接返回 top-k（尊重调用方 k，避免 max(k,500) 导致返回全部候选）
        k_fetch = max(1, int(k))

        sql = """
            SELECT m.*, bm25(memories_fts) AS rank
            FROM memories_fts
            JOIN memories m ON m.rowid = memories_fts.rowid
            WHERE memories_fts MATCH ?
              AND (m.status NOT IN ('deleted', 'consolidated', 'pending_eviction') OR m.status IS NULL)
        """
        params = [fts_match]

        if project is not None:
            sql += " AND m.project = ?"
            params.append(project)
        if mtype is not None:
            sql += " AND m.mtype = ?"
            params.append(mtype)
        if min_confidence > 0:
            sql += " AND m.confidence >= ?"
            params.append(min_confidence)
        if date_from:
            sql += " AND m.created_at >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND m.created_at <= ?"
            params.append(date_to)

        sql += " ORDER BY rank DESC LIMIT ?"
        params.append(k_fetch)

        with self._lock:
            cur = conn.execute(sql, params)
            rows = cur.fetchall()

        results = []
        for row in rows:
            rec = _row_to_record(row)
            rank = row["rank"] if "rank" in row.keys() else None
            score = -rank if rank is not None else 0.0
            results.append((rec, score))
        return results

    # -- entity / graph helpers ----------------------------------------------

    def get_by_entity(self, entity, k=5):
        """Return records associated with a given entity."""
        self.ensure_init()
        conn = self._get_conn()
        with self._lock:
            cur = conn.execute(
                """
                SELECT m.* FROM memories m
                JOIN entities e ON e.memory_id = m.id
                WHERE e.entity = ?
                  AND (m.status NOT IN ('deleted', 'consolidated', 'pending_eviction') OR m.status IS NULL)
                ORDER BY m.created_at DESC
                LIMIT ?
                """,
                (entity, k),
            )
            rows = cur.fetchall()
        return [_row_to_record(r) for r in rows]

    @property
    def exists(self):
        """True if the edges table has at least one row (graph store is non-empty).

        Mirrors ``MemoryGraphStore.exists`` so ``RetrievalEngine`` can query
        the sqlite backend as a drop-in graph store.
        """
        self.ensure_init()
        conn = self._get_conn()
        with self._lock:
            try:
                row = conn.execute("SELECT COUNT(*) FROM edges").fetchone()
                return bool(row and row[0] > 0)
            except Exception:
                return False

    def add_edges(self, edges, memory_id=None):
        """Add graph edges (compatible with MemoryGraphStore.add_edges)."""
        self.ensure_init()
        conn = self._get_conn()
        rows = []
        for edge in edges:
            if isinstance(edge, dict):
                e = edge
            else:
                e = {
                    "from": edge[0],
                    "to": edge[1],
                    "relation": edge[2] if len(edge) > 2 else "related_to",
                    "strength": edge[3] if len(edge) > 3 else 1.0,
                    "memory_id": edge[4] if len(edge) > 4 else memory_id,
                }
            rows.append((
                e.get("from", ""),
                e.get("to", ""),
                e.get("relation", "related_to"),
                e.get("memory_id", memory_id),
                float(e.get("strength", 1.0)),
                e.get("created_at") or _now_iso(),
            ))
        with self._lock:
            conn.execute("BEGIN")
            try:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO edges(from_entity, to_entity, relation, memory_id, strength, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def get_neighbors(self, entity, max_depth=2):
        """Return neighbors of an entity (compatible with graph API)."""
        self.ensure_init()
        conn = self._get_conn()
        result = {"entity": entity, "depth_0": [entity]}
        current = {entity}
        for depth in range(1, max_depth + 1):
            neighbors = set()
            placeholders = ", ".join("?" * len(current))
            params = list(current) * 4
            sql = f"""
                SELECT DISTINCT CASE
                    WHEN from_entity IN ({placeholders}) THEN to_entity
                    WHEN to_entity IN ({placeholders}) THEN from_entity
                END AS neighbor
                FROM edges
                WHERE (from_entity IN ({placeholders}) OR to_entity IN ({placeholders}))
            """
            with self._lock:
                cur = conn_execute_with_retry(conn, sql, params)
            for row in cur.fetchall():
                nb = row[0]
                if nb and nb not in current and nb not in neighbors:
                    neighbors.add(nb)
            result[f"depth_{depth}"] = sorted(neighbors)
            current = current | neighbors
        return result

    def all_edges(self):
        """Return all edges as list of dicts."""
        self.ensure_init()
        conn = self._get_conn()
        with self._lock:
            cur = conn.execute("SELECT * FROM edges ORDER BY rowid")
            rows = cur.fetchall()
        return [
            {
                "from": r["from_entity"],
                "to": r["to_entity"],
                "relation": r["relation"],
                "memory_id": r["memory_id"],
                "strength": r["strength"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # -- graph query (multi-hop, indexed) --------------------------------------
    def graph_query(self, entity, max_depth=2):
        """Return all nodes and edges related to *entity* via multi-hop traversal.

        Uses an indexed ``edges`` table with iterative frontier expansion
        (equivalent to ``WITH RECURSIVE``).  The ``idx_edges_from`` and
        ``idx_edges_to`` indexes make each hop an indexed lookup rather than a
        full table scan, fixing Module 5.4 item 7 performance regression.

        Returns a dict::

            {"query": entity, "nodes": [...], "edges": [...]}

        ``nodes`` is every entity reachable within ``max_depth`` undirected hops
        (including *entity* itself); ``edges`` are the directed edges among them.
        """
        self.ensure_init()
        conn = self._get_conn()
        with self._lock:
            # Temp table holds the closure of reachable entities.
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS _gq_nodes(entity TEXT PRIMARY KEY)")
            conn.execute("DELETE FROM _gq_nodes")
            conn.execute("INSERT OR IGNORE INTO _gq_nodes(entity) VALUES (?)", (entity,))
            # Iteratively expand the frontier: for each known node, pull in its
            # undirected neighbours (indexed lookups).
            for _ in range(max_depth):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO _gq_nodes(entity)
                    SELECT e.to_entity
                    FROM edges e
                    JOIN _gq_nodes n ON e.from_entity = n.entity
                    """
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO _gq_nodes(entity)
                    SELECT e.from_entity
                    FROM edges e
                    JOIN _gq_nodes n ON e.to_entity = n.entity
                    """
                )
            nodes = [r[0] for r in conn.execute(
                "SELECT entity FROM _gq_nodes"
            ).fetchall()]

            edge_rows = conn.execute(
                """
                SELECT from_entity, to_entity, relation, memory_id, strength, created_at
                FROM edges
                WHERE from_entity IN (SELECT entity FROM _gq_nodes)
                   OR to_entity  IN (SELECT entity FROM _gq_nodes)
                ORDER BY rowid
                """
            ).fetchall()
            edges = [
                {
                    "from": r[0], "to": r[1],
                    "relation": r[2], "memory_id": r[3],
                    "strength": r[4], "created_at": r[5],
                }
                for r in edge_rows
            ]
        return {"query": entity, "nodes": nodes, "edges": edges}

    # -- maintenance ---------------------------------------------------------

    def repair(self, dry_run=False):
        """Integrity check (sqlite equivalent of JSONL repair)."""
        self.ensure_init()
        conn = self._get_conn()
        with self._lock:
            corrupt = conn.execute("PRAGMA integrity_check").fetchall()
        ok = all(str(row[0]).lower() == "ok" for row in corrupt)
        if not ok:
            return {
                "ok": False, "corrupt": 1, "kept": 0,
                "broken": str(corrupt), "backup": None, "dry_run": dry_run,
            }
        active = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE status NOT IN ('deleted', 'consolidated', 'archived', 'pending_eviction') OR status IS NULL"
        ).fetchone()[0]
        deleted = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE status IN ('deleted', 'consolidated', 'archived', 'pending_eviction')"
        ).fetchone()[0]
        return {
            "ok": True, "corrupt": 0, "kept": active, "deleted": deleted,
            "backup": None, "dry_run": dry_run,
        }

    def archive(self):
        """Move deleted records to gzip archive."""
        import gzip
        self.ensure_init()
        conn = self._get_conn()
        with self._lock:
            cur = conn.execute("SELECT * FROM memories WHERE status IN ('deleted', 'consolidated', 'archived', 'pending_eviction')")
            rows = cur.fetchall()
        if not rows:
            return 0
        archive_path = self.index_path + ".archive.jsonl.gz"
        with gzip.open(archive_path, "at", encoding="utf-8") as f:
            for row in rows:
                rec = _row_to_record(row)
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        with self._lock:
            conn.execute("BEGIN")
            try:
                conn.execute("DELETE FROM memories WHERE status IN ('deleted', 'consolidated', 'archived', 'pending_eviction')")
                conn.execute(
                    "DELETE FROM entities WHERE memory_id NOT IN "
                    "(SELECT id FROM memories)"
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        self._invalidate_cache()
        return len(rows)

    # -- 冷层归档（遗忘经济学：迁移而非删除） --------------------------------

    def _bloom_path(self):
        """冷层布隆过滤器索引文件路径。"""
        return self.index_path + ".archive.bloom"

    def _load_bloom(self):
        """惰性加载冷层布隆过滤器索引（不存在则新建）。"""
        if self._bloom is not None:
            return self._bloom
        from mnemosyne.utils import BloomFilter  # 惰性导入避免循环依赖
        p = self._bloom_path()
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                bf = BloomFilter(capacity=max(int(data.get("capacity", 10000)), 1),
                                 error_rate=data.get("error_rate", 0.01))
                bf.bits = bytearray(base64.b64decode(data.get("bits", "")))
                bf.size = len(bf.bits) * 8
                self._bloom = bf
                return bf
            except (ValueError, TypeError, OSError, json.JSONDecodeError):
                pass  # 索引损坏 → 重建
        self._bloom = BloomFilter(capacity=10000, error_rate=0.01)
        return self._bloom

    def _save_bloom(self, bf):
        """持久化布隆过滤器索引。"""
        p = self._bloom_path()
        data = {
            "capacity": bf.capacity,
            "error_rate": bf.error_rate,
            "bits": base64.b64encode(bytes(bf.bits)).decode("ascii"),
        }
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, p)

    def archive_cold(self, memory_ids):
        """把指定记忆迁移到冷层：gzip 归档 + 布隆过滤器索引（迁移而非删除）。

        迁移后记录从主表移除（FTS 触发器同步清理索引），
        可经 lookup_cold / restore_cold 查询与恢复。
        返回迁移条数。
        """
        memory_ids = [m for m in (memory_ids or []) if m]
        if not memory_ids:
            return 0
        self.ensure_init()
        conn = self._get_conn()
        placeholders = ", ".join("?" * len(memory_ids))
        with self._lock:
            rows = conn.execute(
                f"SELECT * FROM memories WHERE id IN ({placeholders}) "
                "AND (status IS NULL OR status NOT IN "
                "('deleted', 'consolidated', 'archived', 'pending_eviction'))",
                memory_ids,
            ).fetchall()
        if not rows:
            return 0
        recs = [_row_to_record(r) for r in rows]
        archive_path = self.index_path + ".archive.jsonl.gz"
        with _gzip.open(archive_path, "at", encoding="utf-8") as f:
            for rec in recs:
                rec["tier"] = "cold"
                rec["archived_at"] = _now_iso()
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        bf = self._load_bloom()
        for rec in recs:
            bf.add(rec["id"])
        self._save_bloom(bf)
        archived_ids = [r["id"] for r in recs]
        with self._lock:
            conn.execute("BEGIN")
            try:
                conn.executemany(
                    "DELETE FROM memories WHERE id = ?",
                    [(i,) for i in archived_ids],
                )
                conn.executemany(
                    "DELETE FROM entities WHERE memory_id = ?",
                    [(i,) for i in archived_ids],
                )
                conn.executemany(
                    "DELETE FROM edges WHERE memory_id = ?",
                    [(i,) for i in archived_ids],
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        for i in archived_ids:
            self._hot_drop(i)
        self._invalidate_cache()
        self._update_meta_count(delta=-len(archived_ids))
        return len(archived_ids)

    def lookup_cold(self, memory_id):
        """在冷层归档中查找记忆：布隆过滤器粗筛 → gzip 扫描。"""
        bf = self._load_bloom()
        if memory_id not in bf:
            return None
        archive_path = self.index_path + ".archive.jsonl.gz"
        if not os.path.exists(archive_path):
            return None
        try:
            with _gzip.open(archive_path, "rt", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("id") == memory_id:
                        return rec
        except OSError:
            return None
        return None

    def restore_cold(self, memory_id):
        """把冷层记忆恢复到热层（迁移可逆）。"""
        rec = self.lookup_cold(memory_id)
        if rec is None:
            return None
        rec["status"] = "active"
        rec["tier"] = "hot"
        rec.pop("archived_at", None)
        self.append(rec)
        return rec

    def tier(self, memory_id):
        """Return cooling tier: L1 (hot), L2 (warm), L3 (cold/deleted)."""
        rec = self.find_by_id(memory_id)
        if not rec:
            return None
        if rec.get("status", "active") == "deleted":
            return "L3"
        if rec.get("tier") == "hot" or (rec.get("access_count") or 0) >= 3:
            return "L1"
        return "L2"

    def promote(self, memory_id):
        """Promote to hot tier."""
        return self.update_by_id(memory_id, {"importance": 5, "tier": "hot"})

    def demote(self, memory_id):
        """Demote (soft-delete) a memory."""
        return self.update_by_id(
            memory_id, {"status": "deleted", "last_accessed_at": _now_iso()}
        )

    def audit_log(self, entry):
        """Append an audit-log entry to the audit_log table.

        ``entry`` is a dict with keys: ts, actor, action, target_id, details.
        """
        self.ensure_init()
        conn = self._get_conn()
        with self._lock:
            conn.execute(
                "INSERT INTO audit_log (ts, actor, action, target_id, details) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    entry.get("ts", _now_iso()),
                    entry.get("actor", "system"),
                    entry.get("action", ""),
                    entry.get("target_id", ""),
                    json.dumps(entry.get("details", {}), ensure_ascii=False),
                ),
            )
            conn.commit()

    # -- audit query --------------------------------------------------------
    def get_audit_trail(self, memory_id):
        """Return the complete audit trail for *memory_id*.

        Returns a list of dicts ordered by ts ascending.  Each entry has
        keys: ts, actor, action, target_id, details (dict).
        """
        self.ensure_init()
        conn = self._get_conn()
        with self._lock:
            cur = conn.execute(
                "SELECT ts, actor, action, target_id, details "
                "FROM audit_log WHERE target_id = ? ORDER BY ts ASC, id ASC",
                (memory_id,),
            )
            rows = cur.fetchall()
        trail = []
        for row in rows:
            try:
                details = json.loads(row["details"]) if row["details"] else {}
            except (json.JSONDecodeError, TypeError):
                details = {"raw": row["details"]}
            trail.append({
                "ts": row["ts"],
                "actor": row["actor"],
                "action": row["action"],
                "target_id": row["target_id"],
                "details": details,
            })
        return trail

    def audit(self, memory_id):
        """Alias for :meth:`get_audit_trail` — return full audit trail."""
        return self.get_audit_trail(memory_id)

    # -- Memory Ledger (Module 7.1 Innovation 1) ------------------------------
    # The ledger table schema is created in ensure_init().  These methods
    # provide the SQL-level append / verify / audit / prove-integrity
    # operations on that table.  The higher-level MemoryLedger class in
    # mnemosyne/ledger.py wraps these (or the raw db) for a richer API.
    def ledger_append(self, action, memory_id=None, data_summary=None):
        """Append a ledger entry to the ``ledger`` table.

        Parameters
        ----------
        action        : str   – retain / consolidate / forget / demote / import
        memory_id     : str   – memory this entry pertains to (may be None for
                                import-level events)
        data_summary  : dict  – compact payload describing the operation

        Returns
        -------
        dict with seq, prev_hash, record_hash, ts, payload.
        """
        self.ensure_init()
        import hashlib as _hashlib
        if isinstance(data_summary, str):
            try:
                data_summary = json.loads(data_summary)
            except (json.JSONDecodeError, TypeError):
                data_summary = {"raw": data_summary}
        data_summary = data_summary or {}

        payload = {
            "memory_id": memory_id,
            "action": action,
            "timestamp": _now_iso(),
            "data_summary": data_summary,
        }
        payload_raw = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                 separators=(",", ":"))
        payload_hash = _hashlib.sha256(payload_raw.encode("utf-8")).hexdigest()

        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT seq, record_hash FROM ledger "
                "ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            if row is None:
                prev_hash = ""
                seq = 1
            else:
                prev_hash = row["record_hash"]
                seq = row["seq"] + 1

            chain_hash = _hashlib.sha256(
                (prev_hash + payload_hash).encode("utf-8")
            ).hexdigest()

            conn.execute(
                "INSERT INTO ledger (seq, prev_hash, record_hash, payload, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    seq,
                    prev_hash,
                    chain_hash,
                    json.dumps(payload, sort_keys=True, ensure_ascii=False),
                    payload["timestamp"],
                ),
            )
            conn.commit()
        return {
            "seq": seq,
            "prev_hash": prev_hash,
            "record_hash": chain_hash,
            "ts": payload["timestamp"],
            "payload": payload,
        }

    def ledger_count(self):
        """Return the total number of ledger entries."""
        self.ensure_init()
        conn = self._get_conn()
        with self._lock:
            return conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]

    def ledger_verify_chain(self):
        """Verify the integrity of the entire ledger hash chain.

        Returns ``{valid, first_broken_at, total, details}``.
        """
        self.ensure_init()
        conn = self._get_conn()
        with self._lock:
            rows = conn.execute(
                "SELECT seq, prev_hash, record_hash, payload "
                "FROM ledger ORDER BY seq ASC"
            ).fetchall()

        if not rows:
            return {"valid": True, "first_broken_at": None, "total": 0}

        import hashlib as _hashlib
        prev_hash = ""
        for row in rows:
            seq = row["seq"]
            expected_prev = prev_hash
            if row["prev_hash"] != expected_prev:
                return {
                    "valid": False,
                    "first_broken_at": seq,
                    "total": len(rows),
                    "details": (
                        f"prev_hash mismatch at seq {seq}: "
                        f"expected {expected_prev[:16]!r}, "
                        f"got {row['prev_hash'][:16] if row['prev_hash'] else ''!r}"
                    ),
                }
            try:
                payload = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError):
                payload = {}
            payload_raw = json.dumps(
                payload, sort_keys=True, ensure_ascii=False,
                separators=(",", ":"),
            )
            payload_hash = _hashlib.sha256(payload_raw.encode("utf-8")).hexdigest()
            expected_chain = _hashlib.sha256(
                (expected_prev + payload_hash).encode("utf-8")
            ).hexdigest()
            if row["record_hash"] != expected_chain:
                return {
                    "valid": False,
                    "first_broken_at": seq,
                    "total": len(rows),
                    "details": (
                        f"record_hash mismatch at seq {seq}: "
                        f"expected {expected_chain[:16]}…, "
                        f"got {row['record_hash'][:16]}…"
                    ),
                }
            prev_hash = row["record_hash"]

        return {"valid": True, "first_broken_at": None, "total": len(rows)}

    def ledger_audit(self, memory_id):
        """Return the complete ledger trail for *memory_id*."""
        self.ensure_init()
        conn = self._get_conn()
        with self._lock:
            rows = conn.execute(
                "SELECT seq, record_hash, payload, ts "
                "FROM ledger "
                "WHERE json_extract(payload, '$.memory_id') = ? "
                "ORDER BY seq ASC",
                (memory_id,),
            ).fetchall()
        trail = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError):
                payload = {}
            trail.append({
                "seq": row["seq"],
                "record_hash": row["record_hash"],
                "action": payload.get("action", ""),
                "timestamp": payload.get("timestamp", row["ts"]),
                "data_summary": payload.get("data_summary", {}),
                "ts": row["ts"],
            })
        return trail

    def ledger_prove_integrity(self, n):
        """Return a Merkle-style proof for the latest *n* ledger records."""
        self.ensure_init()
        import hashlib as _hashlib
        conn = self._get_conn()
        with self._lock:
            rows = conn.execute(
                "SELECT seq, prev_hash, record_hash, payload, ts "
                "FROM ledger ORDER BY seq DESC LIMIT ? ORDER BY seq ASC",
                (n,),
            ).fetchall()
            genesis = conn.execute(
                "SELECT seq, prev_hash, record_hash, payload, ts "
                "FROM ledger ORDER BY seq ASC LIMIT 1"
            ).fetchone()
            total = conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]

        proof = {
            "n": n,
            "total_chain_length": total,
            "genesis": None,
            "records": [],
        }
        if genesis:
            try:
                g_payload = json.loads(genesis["payload"])
            except (json.JSONDecodeError, TypeError):
                g_payload = {}
            g_raw = json.dumps(
                g_payload, sort_keys=True, ensure_ascii=False,
                separators=(",", ":"),
            )
            g_payload_hash = _hashlib.sha256(g_raw.encode("utf-8")).hexdigest()
            proof["genesis"] = {
                "seq": genesis["seq"],
                "prev_hash": genesis["prev_hash"],
                "record_hash": genesis["record_hash"],
                "payload_hash": g_payload_hash,
                "ts": genesis["ts"],
            }
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError):
                payload = {}
            proof["records"].append({
                "seq": row["seq"],
                "prev_hash": row["prev_hash"],
                "record_hash": row["record_hash"],
                "payload_hash": _hashlib.sha256(
                    json.dumps(payload, sort_keys=True,
                               ensure_ascii=False, separators=(",", ":"))
                    .encode("utf-8")
                ).hexdigest(),
                "payload": payload,
                "ts": row["ts"],
            })
        return proof

    # -- confidence history --------------------------------------------------

    def add_confidence_history(self, memory_id, entry):
        """追加一条可信度历史（与 MemoryStore.add_confidence_history 接口兼容）。

        ``entry`` 为 dict：{ts, confidence, reason, flags, ...}。
        """
        entry = entry or {}
        self.log_confidence(
            memory_id=memory_id,
            confidence=float(entry.get("confidence", 0.7)),
            reason=str(entry.get("reason", "notary_assess")),
            delta=float(entry.get("delta", 0.0)),
            flags=entry.get("flags") or [],
        )

    def log_confidence(self, memory_id, confidence, reason, delta=0.0,
                       flags=None):
        """Record a confidence update in the ``confidence_history`` table.

        Parameters
        ----------
        memory_id : str
        confidence : float   new confidence value (0–1)
        reason     : str     human-readable reason for the update
        delta      : float    change from the previous value (may be 0)
        flags      : list[str]  optional list of notary flags
        """
        self.ensure_init()
        conn = self._get_conn()
        with self._lock:
            conn.execute(
                "INSERT INTO confidence_history "
                "(memory_id, ts, confidence, reason, delta, flags) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    memory_id,
                    _now_iso(),
                    float(confidence),
                    reason,
                    float(delta),
                    json.dumps(flags or [], ensure_ascii=False),
                ),
            )
            conn.commit()

    def get_confidence_history(self, memory_id):
        """Return the confidence trajectory for *memory_id*.

        Each entry: {ts, confidence, reason, delta, flags}.
        """
        self.ensure_init()
        conn = self._get_conn()
        with self._lock:
            cur = conn.execute(
                "SELECT ts, confidence, reason, delta, flags "
                "FROM confidence_history WHERE memory_id = ? "
                "ORDER BY ts ASC, id ASC",
                (memory_id,),
            )
            rows = cur.fetchall()
        result = []
        for row in rows:
            try:
                flags = json.loads(row["flags"]) if row["flags"] else []
            except (json.JSONDecodeError, TypeError):
                flags = []
            result.append({
                "ts": row["ts"],
                "confidence": row["confidence"],
                "reason": row["reason"],
                "delta": row["delta"],
                "flags": flags,
            })
        return result

    # -- record deletion (eviction) ------------------------------------------
    def delete(self, memory_id):
        """Irreversibly delete a memory and its entity/edge rows.

        Returns True if a row was deleted.
        """
        self.ensure_init()
        conn = self._get_conn()
        with self._lock:
            conn.execute("BEGIN")
            try:
                cur = conn.execute(
                    "DELETE FROM memories WHERE id = ?", (memory_id,)
                )
                conn.execute(
                    "DELETE FROM entities WHERE memory_id = ?", (memory_id,)
                )
                conn.execute(
                    "DELETE FROM edges WHERE memory_id = ?", (memory_id,)
                )
                found = cur.rowcount > 0
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        if found:
            self._invalidate_cache()
            self._hot_drop(memory_id)
            self._update_meta_count(delta=-1)
        return found

    # -- lifecycle -----------------------------------------------------------

    def close(self):
        """Flush pending meta and close the connection."""
        if self._meta_pending > 0 and self._meta_cache is not None:
            try:
                self._write_meta(self._meta_cache)
            except OSError:
                pass
            self._meta_pending = 0
        self._cache = None
        self._meta_cache = None
        if self._conn is not None:
            try:
                self._conn.commit()
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def count(self):
        """Return total record count."""
        self.ensure_init()
        conn = self._get_conn()
        return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    # -- migration -----------------------------------------------------------

    @staticmethod
    def migrate(jsonl_path, db_path=None, base_dir=None, batch_size=1000):
        """Migrate records from a JSONL file into a sqlite database.

        The original JSONL file is preserved.  Returns the number of
        records migrated.
        """
        if db_path is None:
            db_path = os.path.join(
                base_dir or os.path.dirname(jsonl_path), "memory.db"
            )
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

        backend = SqliteBackend(
            base_dir=os.path.dirname(db_path),
            db_name=os.path.basename(db_path),
        )
        backend.ensure_init()
        conn = backend._get_conn()

        migrated = 0
        with open(jsonl_path, "r", encoding="utf-8") as f:
            batch = []
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rec = _upgrade_record(rec)
                if rec.get("_corrupt"):
                    continue
                batch.append(rec)
                if len(batch) >= batch_size:
                    migrated += SqliteBackend._insert_batch(conn, batch)
                    batch.clear()
            if batch:
                migrated += SqliteBackend._insert_batch(conn, batch)

        backend._invalidate_cache()
        # Set count directly (not incremental) since migrate may be re-run
        count = backend.count()
        meta = backend.read_meta() or {}
        meta["count"] = count
        meta["updated_at"] = _now_iso()
        backend._write_meta(meta)
        backend.close()
        return migrated

    @staticmethod
    def _insert_batch(conn, records):
        """Insert a batch of records into sqlite. Returns inserted count."""
        for rec in records:
            if not rec.get("id"):
                rec["id"] = _stable_id(rec.get("content", ""), str(time.time()))
        placeholders = ", ".join("?" * len(_MEMORIES_COLUMNS))
        rows = [_record_to_row(rec) for rec in records]
        conn.execute("BEGIN")
        try:
            conn.executemany(
                f"INSERT OR REPLACE INTO memories "
                f"({', '.join(_MEMORIES_COLUMNS)}) "
                f"VALUES ({placeholders})",
                rows,
            )
            for rec in records:
                SqliteBackend._store_entities(conn, rec)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return len(records)


def conn_execute_with_retry(conn, sql, params, max_retries=3):
    """Execute SQL with retry on 'database is locked' errors."""
    last_err = None
    for attempt in range(max_retries):
        try:
            return conn.execute(sql, params)
        except sqlite3.OperationalError as e:
            last_err = e
            if "locked" in str(e) and attempt < max_retries - 1:
                time.sleep(0.1 * (attempt + 1))
                continue
            raise
    raise last_err
