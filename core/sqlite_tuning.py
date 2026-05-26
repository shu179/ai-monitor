"""Shared SQLite runtime PRAGMA helpers."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional


DEFAULT_CACHE_KIB = 65536
DEFAULT_MMAP_BYTES = 268_435_456
DEFAULT_WAL_AUTOCHECKPOINT_PAGES = 1000
DEFAULT_BUSY_TIMEOUT_MS = 30_000
DEFAULT_WAL_TRUNCATE_THRESHOLD_BYTES = 32 * 1024 * 1024


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


def perform_startup_maintenance(
    conn: sqlite3.Connection,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Optional[str]]:
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
    try:
        resolved_db_path = db_path or _main_database_path(conn)
        if resolved_db_path is not None:
            results["wal_truncated_if_oversized"] = str(
                truncate_wal_if_oversized(conn, resolved_db_path)
            )
    except sqlite3.DatabaseError as exc:
        results["wal_truncate_error"] = str(exc)
    except OSError as exc:
        results["wal_truncate_error"] = str(exc)
    return results


def truncate_wal_if_oversized(
    conn: sqlite3.Connection,
    db_path: str | os.PathLike[str],
    *,
    threshold_bytes: int = DEFAULT_WAL_TRUNCATE_THRESHOLD_BYTES,
) -> bool:
    """Run a TRUNCATE checkpoint when the WAL sidecar exceeds the threshold."""
    wal_path = str(db_path) + "-wal"
    try:
        size = os.path.getsize(wal_path)
    except FileNotFoundError:
        return False
    if size < int(threshold_bytes):
        return False
    cursor = conn.cursor()
    try:
        cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        cursor.close()
    return True


def _main_database_path(conn: sqlite3.Connection) -> Path | None:
    cursor = conn.cursor()
    try:
        for row in cursor.execute("PRAGMA database_list").fetchall():
            if len(row) >= 3 and str(row[1] or "") == "main" and str(row[2] or ""):
                return Path(str(row[2]))
    finally:
        cursor.close()
    return None
