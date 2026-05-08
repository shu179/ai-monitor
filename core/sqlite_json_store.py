from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


MISSING = object()


class SQLiteJsonDocumentStore:
    """Small SQLite-backed JSON document store for local app state."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def load(self, key: str, default: Any = MISSING) -> Any:
        normalized_key = self._normalize_key(key)
        if not normalized_key:
            return default
        if not self.db_path.exists():
            return default
        with self._connection() as conn:
            row = conn.execute(
                "SELECT value_json FROM json_documents WHERE key = ?",
                (normalized_key,),
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(str(row[0] or "null"))
        except Exception:
            return default

    def save(self, key: str, value: Any) -> None:
        normalized_key = self._normalize_key(key)
        if not normalized_key:
            return
        value_json = json.dumps(value, ensure_ascii=False, indent=2)
        updated_at_ns = time.time_ns()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO json_documents(key, value_json, updated_at_ns, byte_size)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at_ns = excluded.updated_at_ns,
                    byte_size = excluded.byte_size
                """,
                (normalized_key, value_json, updated_at_ns, len(value_json.encode("utf-8"))),
            )

    def exists(self, key: str) -> bool:
        normalized_key = self._normalize_key(key)
        if not normalized_key:
            return False
        if not self.db_path.exists():
            return False
        with self._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM json_documents WHERE key = ?",
                (normalized_key,),
            ).fetchone()
        return row is not None

    def signature(self, key: str) -> tuple[str, int, int]:
        normalized_key = self._normalize_key(key)
        source = f"{self.db_path}::{normalized_key}"
        if not normalized_key:
            return (source, 0, 0)
        if not self.db_path.exists():
            return (source, 0, 0)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT updated_at_ns, byte_size FROM json_documents WHERE key = ?",
                (normalized_key,),
            ).fetchone()
        if row is None:
            return (source, 0, 0)
        return (source, int(row[0] or 0), int(row[1] or 0))

    def list_keys(self, prefix: str = "") -> list[str]:
        normalized_prefix = str(prefix or "")
        if not self.db_path.exists():
            return []
        with self._connection() as conn:
            if normalized_prefix:
                rows = conn.execute(
                    "SELECT key FROM json_documents WHERE key LIKE ? ORDER BY key",
                    (f"{normalized_prefix}%",),
                ).fetchall()
            else:
                rows = conn.execute("SELECT key FROM json_documents ORDER BY key").fetchall()
        return [str(row[0]) for row in rows if str(row[0] or "")]

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS json_documents (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at_ns INTEGER NOT NULL,
                    byte_size INTEGER NOT NULL
                )
                """
            )
        except BaseException:
            conn.close()
            raise
        return conn

    @staticmethod
    def _normalize_key(key: str) -> str:
        return str(key or "").strip()
