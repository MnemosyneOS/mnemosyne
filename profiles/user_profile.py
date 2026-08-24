"""User profile manager — explicit user profiles with highest priority.

Zero-dependency: uses only the Python standard library.

Profiles are stored as special memory records (``mtype="identity"``)
with a ``profile_key`` field, or optionally in a dedicated
``user_profiles`` table in the sqlite backend.

API:
  - set_profile(key, data)   -> store a profile attribute
  - get_profile(key)         -> retrieve a profile attribute
  - delete_profile(key)      -> remove a profile attribute
  - get_all_profiles()       -> dict of all profile key→value
"""
import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta

__all__ = ["UserProfile"]


def _now_iso():
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).isoformat(timespec="seconds")


class UserProfile:
    """Manages explicit user profiles with highest retrieval priority.

    Profiles are stored in a dedicated ``user_profiles`` table, separate
    from the main memory store, so they can be fetched efficiently and
    with the highest priority during retrieval.
    """

    def __init__(self, db_path=None):
        if db_path is None:
            db_path = os.path.join(
                os.path.expanduser("~"), ".mnemosyne", "user_profiles.db"
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
        """Create the user_profiles table if it doesn't exist."""
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL UNIQUE,
                value TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                priority INTEGER DEFAULT 999  -- highest priority by default
            );
            CREATE INDEX IF NOT EXISTS idx_profiles_key ON user_profiles(key);
        """)
        conn.commit()

    def set_profile(self, key, value):
        """Store a profile attribute.

        Parameters
        ----------
        key : str
            Profile key (e.g. "name", "preferred_language").
        value : any JSON-serializable
            Profile value.
        """
        self.ensure_init()
        now = _now_iso()
        if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
            val_json = json.dumps(value, ensure_ascii=False)
        else:
            val_json = json.dumps(str(value), ensure_ascii=False)
        conn = self._get_conn()
        conn.execute("""
            INSERT OR REPLACE INTO user_profiles (key, value, created_at, updated_at)
            VALUES (?, ?, ?, ?)
        """, (key, val_json, now, now))
        conn.commit()
        return key

    def get_profile(self, key):
        """Retrieve a profile attribute by key.

        Returns the deserialized value, or ``None`` if not found.
        """
        self.ensure_init()
        conn = self._get_conn()
        row = conn.execute(
            "SELECT value FROM user_profiles WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return row[0]

    def delete_profile(self, key):
        """Delete a profile attribute.

        Returns True if a row was deleted, False otherwise.
        """
        self.ensure_init()
        conn = self._get_conn()
        cur = conn.execute("DELETE FROM user_profiles WHERE key = ?", (key,))
        conn.commit()
        return cur.rowcount > 0

    def get_all_profiles(self):
        """Return all profile attributes as a dict {key: value}."""
        self.ensure_init()
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT key, value FROM user_profiles ORDER BY priority DESC, key"
        ).fetchall()
        result = {}
        for k, v in rows:
            try:
                result[k] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                result[k] = v
        return result

    def get_profiles_by_priority(self):
        """Return profiles sorted by priority (highest first)."""
        self.ensure_init()
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT key, value, priority FROM user_profiles ORDER BY priority DESC"
        ).fetchall()
        result = []
        for k, v, p in rows:
            try:
                val = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                val = v
            result.append({"key": k, "value": val, "priority": p})
        return result

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None
