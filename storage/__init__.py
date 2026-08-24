"""Mnemosyne storage backends package.

Zero-dependency sub-package providing pluggable storage backends.
Each backend implements the same interface as MemoryStore (JSONL).

Backends:
 - SqliteBackend: sqlite3 + FTS5 (default for v7.0.0)
 - MemoryStore: original JSONL append store (in mnemosyne.py)
"""
from .sqlite_backend import SqliteBackend
from .session_store import SessionStore
from .ledger import MemoryLedger, LedgerReceipt

__all__ = ["SqliteBackend", "SessionStore", "MemoryLedger", "LedgerReceipt"]
