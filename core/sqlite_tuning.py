"""Shared SQLite runtime PRAGMA helpers."""

from __future__ import annotations

import sqlite3
from typing import Optional


DEFAULT_CACHE_KIB = 65536
DEFAULT_MMAP_BYTES = 268_435_456
DEFAULT_WAL_AUTOCHECKPOINT_PAGES = 1000
DEFAULT_BUSY_TIMEOUT_MS = 30_000


def apply_runtime_pragmas(
    conn: sqlite3.Connection,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    cache_kib: int = DEFAULT_CACHE_KIB,
    mmap_bytes: int = DEFAULT_MMAP_BYTES,
    wal_autocheckpoint_pages: int = DEFAULT_WAL_AUTOCHECKPOINT_PAGES,
    foreign_keys: bool = True,
) -> None:
    """Apply standard runtime PRAGMAs to a fresh connection."""
    cursor = conn.cursor()
    try:
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        if foreign_keys:
            cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA temp_store = MEMORY")
        cursor.execute(f"PRAGMA cache_size = -{int(cache_kib)}")
        cursor.execute(f"PRAGMA mmap_size = {int(mmap_bytes)}")
        cursor.execute(f"PRAGMA wal_autocheckpoint = {int(wal_autocheckpoint_pages)}")
    finally:
        cursor.close()


def perform_startup_maintenance(conn: sqlite3.Connection) -> dict[str, Optional[str]]:
    """Run once-per-startup SQLite housekeeping."""
    results: dict[str, Optional[str]] = {}
    cursor = conn.cursor()
    try:
        try:
            row = cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            results["wal_checkpoint"] = str(row) if row is not None else None
        except sqlite3.DatabaseError as exc:
            results["wal_checkpoint_error"] = str(exc)
        try:
            cursor.execute("PRAGMA optimize")
            results["optimize"] = "ok"
        except sqlite3.DatabaseError as exc:
            results["optimize_error"] = str(exc)
    finally:
        cursor.close()
    return results
