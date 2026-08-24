"""Memory Ledger — Append-Only Memory Ledger (Module 7.1 Innovation 1).

A tamper-evident append-only chain that records every write operation
(retain, consolidate/merge, forget/delete, demote/migrate, import).

Each entry is hash-chained: ``chain_hash = sha256(prev_hash || payload_hash)``
so that any modification to a historical entry — or to the underlying
memory record — breaks the chain and is detectable at the exact position.

The ledger is pure-stdlib (hashlib, json, sqlite3) — zero dependencies.

Usage (standalone):
    from storage.ledger import MemoryLedger
    ledger = MemoryLedger(base_dir="/path/to/ledger.db")
    receipt = ledger.append("retain", memory_id, {"content_preview": "..."})
    result = ledger.verify_chain()        # {valid, first_broken_at}
    trail = ledger.audit(memory_id)
    proof = ledger.prove_integrity(10)   # last 10 records' hash chain
"""
import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone, timedelta

try:
    from .bigram import _now_iso
except Exception:
    def _now_iso():
        tz = timezone(timedelta(hours=8))
        return datetime.now(tz).isoformat(timespec="seconds")

__all__ = ["MemoryLedger", "LedgerReceipt"]


def _compute_hash(prev_hash, payload_hash):
    """Compute the chain hash for this entry.

    ``chain_hash = sha256(prev_hash || payload_hash)``.
    When *prev_hash* is empty (genesis block), it is treated as "".
    """
    h = hashlib.sha256()
    h.update((prev_hash or "").encode("utf-8"))
    h.update(payload_hash.encode("utf-8"))
    return h.hexdigest()


def _compute_payload_hash(payload):
    """Compute a stable SHA-256 hash of the normalised payload JSON.

    The payload dict is serialised with ``sort_keys=True`` and no extra
    whitespace so the hash is deterministic.
    """
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class LedgerReceipt:
    """A lightweight, JSON-serialisable receipt for a ledger append.

    Attributes
    ----------
    seq       : int   – 1-based sequence number in the chain
    hash      : str   – the chain hash recorded for this entry
    prev_hash : str   – hash of the preceding entry ("" for genesis)
    ts        : str   – ISO-8601 timestamp of the append
    memory_id : str   – memory this entry pertains to
    action    : str   – retain / consolidate / forget / demote / import
    """

    __slots__ = ("seq", "hash", "prev_hash", "ts", "memory_id", "action")

    def __init__(self, seq, hash_, prev_hash, ts, memory_id, action):
        self.seq = seq
        self.hash = hash_
        self.prev_hash = prev_hash
        self.ts = ts
        self.memory_id = memory_id
        self.action = action

    def to_dict(self):
        return {
            "seq": self.seq,
            "hash": self.hash,
            "prev_hash": self.prev_hash,
            "ts": self.ts,
            "memory_id": self.memory_id,
            "action": self.action,
        }

    def __repr__(self):
        return (
            f"LedgerReceipt(seq={self.seq}, action={self.action!r}, "
            f"memory_id={self.memory_id!r}, hash={self.hash[:12]}…)"
        )


class MemoryLedger:
    """Append-only hash-chain ledger backed by a SQLite database.

    Parameters
    ----------
    db_path  : str   – explicit path to the ledger database file.
    base_dir : str   – directory that will hold ``ledger.db`` (used when
                       *db_path* is not given).

    The ledger table schema:

        ledger(seq INTEGER PK AUTOINCREMENT,
               prev_hash  TEXT,
               record_hash TEXT,
               payload    TEXT,      -- JSON: {memory_id, action, timestamp, data_summary}
               ts         TEXT)

    Entry structure
    ---------------
    * **seq**        — monotonically increasing integer (1-based)
    * **prev_hash**  — chain hash of the previous entry (``""`` for genesis)
    * **record_hash**— ``sha256(prev_hash || payload_hash)``
    * **payload**    — JSON ``{memory_id, action, timestamp, data_summary}``
    * **ts**         — ISO-8601 timestamp of the append
    """

    def __init__(self, db_path=None, base_dir=None):
        if db_path is not None:
            self.db_path = db_path
        elif base_dir is not None:
            self.db_path = os.path.join(os.path.abspath(base_dir), "ledger.db")
        else:
            self.db_path = os.path.join(
                os.path.expanduser("~/.mnemosyne"), "ledger.db"
            )
        self._lock = threading.RLock()
        self._conn = None

    # ------------------------------------------------------------------
    # Connection / schema
    # ------------------------------------------------------------------

    def _get_conn(self):
        if self._conn is None:
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
            self._conn = sqlite3.connect(
                self.db_path, check_same_thread=False, timeout=30
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA temp_store=MEMORY")
        return self._conn

    @property
    def conn(self):
        return self._get_conn()

    def ensure_init(self):
        """Create the ``ledger`` table if it does not yet exist."""
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ledger (
                    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
                    prev_hash   TEXT,
                    record_hash TEXT,
                    payload     TEXT,
                    ts          TEXT
                )
                """
            )
            conn.commit()

    def close(self):
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.commit()
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    # ------------------------------------------------------------------
    # Core: append
    # ------------------------------------------------------------------

    def append(self, action, memory_id=None, data_summary=None):
        """Append a ledger entry and return a :class:`LedgerReceipt`.

        Parameters
        ----------
        action        : str   – one of ``retain``, ``consolidate``,
                                ``forget``, ``demote``, ``import``.
        memory_id     : str   – the memory this entry pertains to (may be
                                ``None`` for import-level events).
        data_summary  : dict  – compact payload describing the operation.
        """
        self.ensure_init()
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
        payload_hash = _compute_payload_hash(payload)

        with self._lock:
            conn = self._get_conn()
            # Fetch the previous record's chain hash (prev_hash) and seq
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

            chain_hash = _compute_hash(prev_hash, payload_hash)

            conn.execute(
                """
                INSERT INTO ledger (seq, prev_hash, record_hash, payload, ts)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    seq,
                    prev_hash,
                    chain_hash,
                    json.dumps(payload, sort_keys=True, ensure_ascii=False),
                    payload["timestamp"],
                ),
            )
            conn.commit()

        return LedgerReceipt(
            seq=seq,
            hash_=chain_hash,
            prev_hash=prev_hash,
            ts=payload["timestamp"],
            memory_id=memory_id,
            action=action,
        )

    # ------------------------------------------------------------------
    # Core: verify_chain
    # ------------------------------------------------------------------

    def verify_chain(self):
        """Verify the integrity of the entire hash chain.

        Returns a dict with keys:
            ``valid``           – bool, True if every link checks out
            ``first_broken_at`` – int or None, the 1-based seq of the first
                                   broken link (``None`` when valid)
            ``total``           – int, total number of entries verified
            ``details``         – optional str describing the failure
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

        prev_hash = ""
        for row in rows:
            seq = row["seq"]
            expected_prev = prev_hash

            # 1. Check prev_hash linkage
            if row["prev_hash"] != expected_prev:
                return {
                    "valid": False,
                    "first_broken_at": seq,
                    "total": len(rows),
                    "details": (
                        f"prev_hash mismatch at seq {seq}: "
                        f"expected {expected_prev[:16]!r}, "
                        f"got {(row['prev_hash'] or '')[:16]!r}"
                    ),
                }

            # 2. Recompute the chain hash from the payload
            try:
                payload = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError):
                payload = {}
            payload_hash = _compute_payload_hash(payload)
            expected_chain = _compute_hash(expected_prev, payload_hash)

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

    # ------------------------------------------------------------------
    # Core: audit
    # ------------------------------------------------------------------

    def audit(self, memory_id):
        """Return the complete lifecycle trail for *memory_id*.

        Each entry in the returned list is a dict with keys:
        ``seq``, ``record_hash``, ``action``, ``timestamp``,
        ``data_summary``, ``ts``.
        """
        self.ensure_init()
        conn = self._get_conn()
        with self._lock:
            rows = conn.execute(
                """
                SELECT seq, record_hash, payload, ts
                FROM ledger
                WHERE json_extract(payload, '$.memory_id') = ?
                ORDER BY seq ASC
                """,
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

    # ------------------------------------------------------------------
    # Core: prove_integrity
    # ------------------------------------------------------------------

    def prove_integrity(self, n):
        """Return a Merkle-style proof for the latest *n* records.

        This is a simplified proof: a list of consecutive (seq, record_hash,
        payload_hash) tuples for the last *n* entries, plus the genesis
        and final chain hash so an external verifier can recompute the
        entire chain from ``payload`` alone.

        Returns a dict suitable for JSON serialisation.
        """
        self.ensure_init()
        conn = self._get_conn()
        with self._lock:
            # Latest *n* records (oldest-first for chain-order)
            rows = conn.execute(
                "SELECT seq, prev_hash, record_hash, payload, ts "
                "FROM ledger ORDER BY seq ASC "
                "LIMIT -1 OFFSET (SELECT MAX(0, (SELECT COUNT(*) FROM ledger) - ?))"
                , (n,)
            ).fetchall()
            genesis = conn.execute(
                "SELECT seq, prev_hash, record_hash, payload, ts "
                "FROM ledger ORDER BY seq ASC LIMIT 1"
            ).fetchone()
            total = conn.execute(
                "SELECT COUNT(*) FROM ledger"
            ).fetchone()[0]

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
            proof["genesis"] = {
                "seq": genesis["seq"],
                "prev_hash": genesis["prev_hash"],
                "record_hash": genesis["record_hash"],
                "payload_hash": _compute_payload_hash(g_payload),
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
                "payload_hash": _compute_payload_hash(payload),
                "payload": payload,
                "ts": row["ts"],
            })
        return proof

    # ------------------------------------------------------------------
    # Convenience: total count
    # ------------------------------------------------------------------

    def count(self):
        """Return the total number of ledger entries."""
        conn = self._get_conn()
        with self._lock:
            return conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]

    def get_entries(self, limit=100, offset=0):
        """Return a list of ledger entries as dicts.

        Parameters
        ----------
        limit : int
            Maximum number of entries to return (most recent first).
        offset : int
            Number of entries to skip.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT seq, prev_hash, record_hash, payload, ts "
            "FROM ledger ORDER BY seq DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        entries = []
        for row in rows:
            seq, prev_hash, record_hash, payload_str, ts = row
            try:
                payload = json.loads(payload_str) if payload_str else {}
            except (json.JSONDecodeError, TypeError):
                payload = {}
            entries.append({
                "seq": seq,
                "hash": record_hash,
                "prev_hash": prev_hash,
                "timestamp": ts,
                "memory_id": payload.get("memory_id"),
                "action": payload.get("action"),
                "data_summary": payload.get("data_summary", {}),
            })
        return entries

    def __enter__(self):
        self.ensure_init()
        return self

    def __exit__(self, *exc):
        self.close()
        return None
