from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .app_paths import resolve_app_path
from .local_account_space import account_scoped_path
from .sqlite_tuning import apply_runtime_pragmas, perform_startup_maintenance, truncate_wal_if_oversized
from .time_utils import local_now


DEFAULT_CLOUD_OBJECT_TRANSFER_DB_PATH = resolve_app_path("user_data/cloud_object_transfers.sqlite3")


class CloudObjectTransferStore:
    """Local durable ledger for cloud object upload/download transfers.

    It is intentionally small: no object bytes, only status, path, sha, and
    error metadata. The future sync daemon can use this as a retry/diagnostic
    source without coupling large payloads to SQLite.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._explicit_path = Path(db_path) if db_path is not None else None
        self._startup_maintenance_done = False

    @property
    def db_path(self) -> Path:
        if self._explicit_path is not None:
            return self._explicit_path
        return account_scoped_path(
            "user_data/cloud_object_transfers.sqlite3",
            fallback=DEFAULT_CLOUD_OBJECT_TRANSFER_DB_PATH,
        )

    def start_transfer(
        self,
        *,
        transfer_id: str,
        direction: str,
        object_id: str = "",
        sha256: str = "",
        size_bytes: int = 0,
        path: str = "",
        content_type: str = "",
        trace_id: str = "",
    ) -> dict[str, Any]:
        safe_transfer_id = str(transfer_id or "").strip()
        safe_direction = _normalize_direction(direction)
        if not safe_transfer_id:
            return {"ok": False, "message": "transfer_id is required"}
        now = local_now().isoformat(timespec="seconds")
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO object_transfers(
                    transfer_id, direction, object_id, sha256, size_bytes, path,
                    content_type, status, attempts, trace_id, last_error,
                    started_at, updated_at, completed_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, 'running', 1, ?, '', ?, ?, '')
                ON CONFLICT(transfer_id) DO UPDATE SET
                    direction=excluded.direction,
                    object_id=excluded.object_id,
                    sha256=excluded.sha256,
                    size_bytes=excluded.size_bytes,
                    path=excluded.path,
                    content_type=excluded.content_type,
                    status='running',
                    attempts=object_transfers.attempts + 1,
                    trace_id=excluded.trace_id,
                    last_error='',
                    updated_at=excluded.updated_at,
                    completed_at=''
                """,
                (
                    safe_transfer_id,
                    safe_direction,
                    str(object_id or "").strip(),
                    str(sha256 or "").strip(),
                    max(0, int(size_bytes or 0)),
                    str(path or "").strip(),
                    str(content_type or "").strip(),
                    str(trace_id or "").strip(),
                    now,
                    now,
                ),
            )
        return {"ok": True, "transfer_id": safe_transfer_id, "status": "running"}

    def finish_transfer(
        self,
        transfer_id: str,
        *,
        object_id: str = "",
        path: str = "",
        status: str = "completed",
    ) -> dict[str, Any]:
        safe_transfer_id = str(transfer_id or "").strip()
        if not safe_transfer_id:
            return {"ok": False, "message": "transfer_id is required"}
        safe_status = str(status or "completed").strip() or "completed"
        now = local_now().isoformat(timespec="seconds")
        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE object_transfers
                SET status = ?,
                    object_id = COALESCE(NULLIF(?, ''), object_id),
                    path = COALESCE(NULLIF(?, ''), path),
                    last_error = '',
                    updated_at = ?,
                    completed_at = ?
                WHERE transfer_id = ?
                """,
                (safe_status, str(object_id or "").strip(), str(path or "").strip(), now, now, safe_transfer_id),
            )
        return {"ok": int(cursor.rowcount or 0) > 0, "transfer_id": safe_transfer_id, "status": safe_status}

    def fail_transfer(self, transfer_id: str, message: str) -> dict[str, Any]:
        safe_transfer_id = str(transfer_id or "").strip()
        if not safe_transfer_id:
            return {"ok": False, "message": "transfer_id is required"}
        safe_message = str(message or "object transfer failed").strip()[:1000]
        now = local_now().isoformat(timespec="seconds")
        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE object_transfers
                SET status = 'failed',
                    last_error = ?,
                    updated_at = ?
                WHERE transfer_id = ?
                """,
                (safe_message, now, safe_transfer_id),
            )
        return {"ok": int(cursor.rowcount or 0) > 0, "transfer_id": safe_transfer_id, "status": "failed"}

    def diagnostics(self, *, failed_limit: int = 10) -> dict[str, Any]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT direction, status, COUNT(*), COALESCE(SUM(size_bytes), 0)
                FROM object_transfers
                GROUP BY direction, status
                ORDER BY direction, status
                """
            ).fetchall()
            newest = conn.execute(
                """
                SELECT transfer_id, direction, status, object_id, sha256, size_bytes,
                       path, attempts, updated_at, last_error
                FROM object_transfers
                ORDER BY updated_at DESC, transfer_id DESC
                LIMIT 10
                """
            ).fetchall()
            failed = conn.execute(
                """
                SELECT transfer_id, direction, status, object_id, sha256, size_bytes,
                       path, attempts, updated_at, last_error
                FROM object_transfers
                WHERE status = 'failed'
                ORDER BY updated_at DESC, transfer_id DESC
                LIMIT ?
                """,
                (max(0, int(failed_limit or 0)),),
            ).fetchall()
        by_status: dict[str, int] = {}
        by_direction: dict[str, dict[str, int]] = {}
        bytes_by_direction: dict[str, int] = {}
        for direction, status, count, total_bytes in rows:
            safe_direction = str(direction or "")
            safe_status = str(status or "")
            safe_count = int(count or 0)
            by_status[safe_status] = by_status.get(safe_status, 0) + safe_count
            by_direction.setdefault(safe_direction, {})[safe_status] = safe_count
            bytes_by_direction[safe_direction] = bytes_by_direction.get(safe_direction, 0) + int(total_bytes or 0)
        return {
            "path": str(self.db_path),
            "total": sum(by_status.values()),
            "by_status": by_status,
            "by_direction": by_direction,
            "bytes_by_direction": bytes_by_direction,
            "newest": [_row_public(row) for row in newest],
            "failed": [_row_public(row) for row in failed],
        }

    def vacuum_wal_if_needed(self) -> bool:
        """Future daily-maintenance hook; startup maintenance is the current safety net."""
        with self._connection() as conn:
            return truncate_wal_if_oversized(conn, self.db_path)

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
            apply_runtime_pragmas(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS object_transfers (
                    transfer_id TEXT PRIMARY KEY,
                    direction TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    path TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    trace_id TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_object_transfers_status_updated
                ON object_transfers(status, updated_at)
                """
            )
            if not self._startup_maintenance_done:
                try:
                    perform_startup_maintenance(conn, self.db_path)
                    self._startup_maintenance_done = True
                except Exception:
                    pass
        except BaseException:
            conn.close()
            raise
        return conn


def _normalize_direction(value: str) -> str:
    direction = str(value or "").strip().lower()
    if direction not in {"upload", "download"}:
        return "upload"
    return direction


def _row_public(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    return {
        "transfer_id": str(row[0] or ""),
        "direction": str(row[1] or ""),
        "status": str(row[2] or ""),
        "object_id": str(row[3] or ""),
        "sha256": str(row[4] or ""),
        "size_bytes": int(row[5] or 0),
        "path": str(row[6] or ""),
        "attempts": int(row[7] or 0),
        "updated_at": str(row[8] or ""),
        "last_error": str(row[9] or ""),
    }
