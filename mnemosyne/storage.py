import json
import os
import time
import gzip
import logging

logger = logging.getLogger("mnemosyne.storage")

from .graph import (_upgrade_record,)
from .utils import (_now_iso, _stable_id, _tokenize,)

# === Cross-platform file lock ===
import os as _os
VERSION = "7.0.0"
INDEX_NAME = "index.jsonl"
GRAPH_NAME = "graph.jsonl"
META_NAME = "meta.json"
EMBEDDING_DIM = 128
PROJ_BUCKETS = 2048
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".mnemosyne")
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
# Part 5: 存储层（JSONL + 图Index）
# ============================================================================

class MemoryStore:
    """JSONL 追加式记忆主库。"""

    def __init__(self, base_dir=DEFAULT_DIR):
        self.base_dir = os.path.abspath(base_dir)
        self.index_path = os.path.join(self.base_dir, INDEX_NAME)
        self.meta_path = os.path.join(self.base_dir, META_NAME)
        self._cache = None  # 内存热缓存，消除检索时 JSONL 读盘
        self._meta_cache = None  # meta 内存缓存，消除 append 时 meta 读盘（v5.2）
        self._meta_pending = 0   # 未落盘的 meta 变更计数（v5.2 延迟持久化）

    def ensure_init(self):
        os.makedirs(self.base_dir, exist_ok=True)
        if not os.path.exists(self.index_path):
            open(self.index_path, "w", encoding="utf-8").close()
        if not os.path.exists(self.meta_path):
            meta = {"schema": "mnemosyne-v2", "created_at": _now_iso(),
                    "version": VERSION, "count": 0}
            self._write_meta(meta)

    def _write_meta(self, meta):
        self._meta_cache = meta
        self._meta_pending = 0
        tmp = self.meta_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
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

    def append(self, record, retries=3, durable=False):
        self.ensure_init()
        record["id"] = record.get("id") or _stable_id(record.get("content", ""), str(time.time()))
        line = json.dumps(record, ensure_ascii=False) + "\n"
        last_err = None
        for attempt in range(1, retries + 1):
            try:
                with open(self.index_path, "a", encoding="utf-8") as f:
                    _file_lock(f)
                    f.write(line)
                    if durable and hasattr(os, "fsync"):
                        f.flush()
                        os.fsync(f.fileno())
                break
            except OSError as e:
                last_err = e
                if attempt < retries:
                    time.sleep(0.05 * attempt)
        else:
            raise OSError(f"writes 记忆失败（已Retry {retries}  times）：{last_err}")
        meta = self.read_meta()
        if meta is None:
            meta = {"count": 0, "schema": "mnemosyne-v2", "version": VERSION, "created_at": _now_iso()}
            self._meta_cache = meta
        meta["count"] = meta.get("count", 0) + 1
        meta["updated_at"] = _now_iso()
        self._meta_pending += 1
        if self._meta_pending >= 100:  # v5.2: 每 100 次写盘一次，避免每次 append 都 IO
            try:
                self._write_meta(meta)
            except OSError:
                pass
        self._invalidate_cache()
        return record

    def append_batch(self, records, retries=3):
        """批量追加writes 。"""
        self.ensure_init()
        with open(self.index_path, "a", encoding="utf-8") as f:
            for rec in records:
                rec["id"] = rec.get("id") or _stable_id(rec.get("content", ""), str(time.time()))
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        meta = self.read_meta()
        if meta is None:
            meta = {"count": 0, "schema": "mnemosyne-v2", "version": VERSION, "created_at": _now_iso()}
            self._meta_cache = meta
        meta["count"] = meta.get("count", 0) + len(records)
        meta["updated_at"] = _now_iso()
        self._meta_pending += 1
        if self._meta_pending >= 20:  # v5.2: 批量写入时每 20 批写盘一次
            try:
                self._write_meta(meta)
            except OSError:
                pass
        self._invalidate_cache()
        return records

    def iter_records(self):
        if not os.path.exists(self.index_path):
            return
        with open(self.index_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    # v1→v2 Upgrade补齐
                    rec = _upgrade_record(rec)
                    yield rec
                except json.JSONDecodeError:
                    yield {"_corrupt": True, "line_no": line_no,
                           "content": f"[损坏line #{line_no} 已skips ]",
                           "id": f"corrupt-{line_no}"}

    def all_records(self):
        if self._cache is None:
            self._cache = list(self.iter_records())
        return self._cache

    def _invalidate_cache(self):
        self._cache = None

    def repair(self):
        """扫描并修复 JSONL 文件：移除损坏行，保留完好数据。返回 (removed, kept)。"""
        removed = 0; kept = 0; good = []
        for rec in self.iter_records():
            if rec.get("_corrupt"):
                removed += 1
            else:
                good.append(rec); kept += 1
        if removed > 0:
            self.rewrite(good)
        return (removed, kept)

    def rewrite(self, records):
        self.ensure_init()
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _file_lock(f)
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, self.index_path)
        meta = self.read_meta() or {}
        meta["count"] = len(records)
        meta["updated_at"] = _now_iso()
        self._write_meta(meta)
        self._invalidate_cache()

    def find_by_id(self, memory_id):
        for r in self.iter_records():
            if r.get("id") == memory_id:
                return r
        return None

    def update_by_id(self, memory_id, updates):
        """原地updates 某 memory records的Field。"""
        records = self.all_records()
        found = False
        for r in records:
            if r.get("id") == memory_id:
                r.update(updates)
                r["updated_at"] = _now_iso()
                found = True
        if found:
            self.rewrite(records)
        return found

    def archive(self):
        """L3 冷归档：把 deleted/过期记录移到 gzip 归档文件，从主 JSONL 移除。返回归档数。"""
        records = self.all_records()
        active = []
        cold = []
        for r in records:
            if r.get("status", "active") == "deleted":
                cold.append(r)
            else:
                active.append(r)
        if not cold:
            return 0
        import gzip
        archive_path = self.index_path + ".archive.jsonl.gz"
        with gzip.open(archive_path, "at", encoding="utf-8") as f:
            for r in cold:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.rewrite(active)
        return len(cold)

    def tier(self, memory_id):
        """返回记忆热度层级：L1(热/tier=hot或高频访问≥3)/L2(温/正常)/L3(冷/deleted)。不存在返回 None。"""
        r = None
        for rec in self.all_records():
            if rec.get("id") == memory_id:
                r = rec
                break
        if not r:
            return None
        if r.get("status", "active") == "deleted":
            return "L3"
        if r.get("tier") == "hot" or (r.get("access_count") or 0) >= 3:
            return "L1"
        return "L2"

    def promote(self, memory_id):
        """提升到 L1 热层：importance 置 5，标记 tier=hot。"""
        return self.update_by_id(memory_id, {"importance": 5, "tier": "hot"})

    def demote(self, memory_id):
        """降级到 L3 冷层：软删。"""
        return self.update_by_id(memory_id, {"status": "deleted", "deleted_at": _now_iso()})


    # ---- v7.0.0: Audit log ----
    
    def _audit_path(self):
        return os.path.join(self.base_dir, "audit_log.jsonl")
    
    def audit_log(self, entry):
        """Add an audit entry to the log."""
        path = self._audit_path()
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass
    
    def audit(self, memory_id):
        """Get the audit trail for a memory."""
        path = self._audit_path()
        if not os.path.exists(path):
            return []
        trail = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("target_id") == memory_id:
                            trail.append(entry)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
        return trail
    
    def add_confidence_history(self, memory_id, entry):
        """Add a confidence history entry for a memory."""
        path = os.path.join(self.base_dir, "confidence_history.jsonl")
        record = {"memory_id": memory_id, "ts": _now_iso(), **entry}
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass
    
    def get_confidence_history(self, memory_id):
        """Get the confidence history for a memory."""
        path = os.path.join(self.base_dir, "confidence_history.jsonl")
        if not os.path.exists(path):
            return []
        history = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        if record.get("memory_id") == memory_id:
                            history.append(record)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
        return history
    
    def close(self):
        """Close the store."""
        pass
