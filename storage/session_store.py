"""Session store — conversation history with FTS5 search.

Zero-dependency: uses only the Python standard library (sqlite3).

Tables:
  - sessions(session_id TEXT, role TEXT, content TEXT, ts TEXT, metadata TEXT)
  - sessions_fts: FTS5 virtual table for full-text search

Provides:
  - SessionStore.append(session_id, role, content, metadata=None)
  - SessionStore.search(query, session_id=None, k=10)
"""
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("mnemosyne.session_store")

__all__ = ["SessionStore"]


def _now_iso():
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).isoformat(timespec="seconds")


class SessionStore:
    """Stores and searches conversation history.

    Uses sqlite3 (stdlib) with FTS5 for fast keyword search.
    Supports multi-session isolation via ``session_id``.
    """

    def __init__(self, db_path=None):
        if db_path is None:
            db_path = os.path.join(
                os.path.expanduser("~"), ".mnemosyne", "sessions.db"
            )
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self._conn = None

    def _get_conn(self):
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    @property
    def conn(self):
        return self._get_conn()

    def ensure_init(self):
        """Create tables if they don't exist."""
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                ts TEXT NOT NULL,
                metadata TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_sid ON sessions(session_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_ts ON sessions(ts);
            CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
                content, content_rowid='id',
                tokenize='trigram'
            );
            CREATE TRIGGER IF NOT EXISTS sessions_ai AFTER INSERT ON sessions BEGIN
                INSERT INTO sessions_fts(rowid, content) VALUES (new.id, new.content);
            END;
            -- Note: we intentionally do NOT create an AFTER DELETE trigger.
            -- FTS5 deletions are handled manually in clear_session/clear_all
            -- because the 'delete' command can fail when the FTS index row
            -- is in an inconsistent state during bulk operations.
        """)
        conn.commit()

    def append(self, session_id, role, content, metadata=None):
        """Append a conversation turn.

        Parameters
        ----------
        session_id : str
            Identifier for the conversation session.
        role : str
            "user" or "assistant".
        content : str
            The text content of the turn.
        metadata : dict or None
            Optional metadata (stored as JSON).
        """
        self.ensure_init()
        conn = self._get_conn()
        ts = _now_iso()
        meta_json = json.dumps(metadata, ensure_ascii=False) if metadata else None
        conn.execute(
            "INSERT INTO sessions (session_id, role, content, ts, metadata) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, role, content, ts, meta_json),
        )
        conn.commit()
        return ts

    def search(self, query, session_id=None, k=10):
        """Search conversation history using FTS5 (trigram tokenizer).

        For queries shorter than 3 characters (too short for trigram),
        falls back to a LIKE-based substring search.

        Parameters
        ----------
        query : str
            Search query.
        session_id : str or None
            If set, restrict to this session only.
        k : int
            Maximum number of results.

        Returns
        -------
        list[dict] : Each dict has keys: session_id, role, content, ts, metadata
        """
        self.ensure_init()
        conn = self._get_conn()
        # Escape FTS5 special characters in the query
        safe_query = query.replace('"', '""').replace("'", "''")

        if len(query) >= 3:
            # Use FTS5 trigram search
            if session_id:
                rows = conn.execute("""
                    SELECT s.session_id, s.role, s.content, s.ts, s.metadata
                    FROM sessions_fts f
                    JOIN sessions s ON s.id = f.rowid
                    WHERE sessions_fts MATCH ? AND s.session_id = ?
                    ORDER BY s.ts DESC
                    LIMIT ?
                """, (safe_query, session_id, k)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT s.session_id, s.role, s.content, s.ts, s.metadata
                    FROM sessions_fts f
                    JOIN sessions s ON s.id = f.rowid
                    WHERE sessions_fts MATCH ?
                    ORDER BY s.ts DESC
                    LIMIT ?
                """, (safe_query, k)).fetchall()
        else:
            # Fallback: LIKE-based substring search for short queries
            like_pattern = f"%{query}%"
            if session_id:
                rows = conn.execute("""
                    SELECT session_id, role, content, ts, metadata
                    FROM sessions
                    WHERE content LIKE ? AND session_id = ?
                    ORDER BY ts DESC
                    LIMIT ?
                """, (like_pattern, session_id, k)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT session_id, role, content, ts, metadata
                    FROM sessions
                    WHERE content LIKE ?
                    ORDER BY ts DESC
                    LIMIT ?
                """, (like_pattern, k)).fetchall()

        results = []
        for row in rows:
            meta = None
            if row[4]:
                try:
                    meta = json.loads(row[4])
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append({
                "session_id": row[0],
                "role": row[1],
                "content": row[2],
                "ts": row[3],
                "metadata": meta,
            })
        return results

    def get_session(self, session_id, limit=100):
        """Retrieve the most recent *limit* turns for *session_id*."""
        self.ensure_init()
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT role, content, ts, metadata
            FROM sessions
            WHERE session_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (session_id, limit)).fetchall()
        return [
            {
                "role": r[0],
                "content": r[1],
                "ts": r[2],
                "metadata": json.loads(r[3]) if r[3] else None,
            }
            for r in reversed(rows)
        ]

    def clear_session(self, session_id):
        """Delete all turns for *session_id*."""
        self.ensure_init()
        conn = self._get_conn()
        # Get IDs first, then delete from FTS, then from sessions
        ids = conn.execute(
            "SELECT id FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchall()
        for (rid,) in ids:
            try:
                conn.execute(
                    "INSERT INTO sessions_fts(sessions_fts, rowid) VALUES('delete', ?)",
                    (rid,),
                )
            except Exception as exc:
                logger.debug("会话 FTS 索引清理失败：%s", exc)
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        conn.commit()

    def count(self, session_id=None):
        """Count total turns, optionally restricted to a session."""
        self.ensure_init()
        conn = self._get_conn()
        if session_id:
            cur = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE session_id = ?", (session_id,)
            )
        else:
            cur = conn.execute("SELECT COUNT(*) FROM sessions")
        return cur.fetchone()[0]

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None
