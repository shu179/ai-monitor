from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .app_paths import resolve_app_path
from .local_account_space import account_scoped_path
from .sqlite_tuning import apply_runtime_pragmas, perform_startup_maintenance, truncate_wal_if_oversized
from .time_utils import local_now


DEFAULT_CLOUD_OBJECT_TRANSFER_DB_PATH = resolve_app_path("user_data/cloud_object_transfers.sqlite3")
DEFAULT_RETRYABLE_TRANSFER_LIMIT = 50
DEFAULT_TRANSFER_MAX_ATTEMPTS = 5
DEFAULT_STALE_RUNNING_SECONDS = 10 * 60
_TRANSFER_RETRY_BACKOFF_SECONDS = (60.0, 300.0, 1800.0, 7200.0)


class CloudObjectTransferStore:
    """Local durable ledger for cloud object upload/download transfers.

    It is intentionally small: no object bytes, only status, path, sha, and
    error metadata. The future sync daemon can use this as a retry/diagnostic
    source without coupling large payloads to SQLite.
    """

    _change_event = threading.Event()

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._explicit_path = Path(db_path) if db_path is not None else None
        self._startup_maintenance_done = False

    @classmethod
    def notify_changed(cls) -> None:
        cls._change_event.set()

    @classmethod
    def wait_for_change(cls, timeout: float) -> bool:
        changed = cls._change_event.wait(max(0.0, float(timeout or 0.0)))
        if changed:
            cls._change_event.clear()
        return changed

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
        storage_size_bytes: int = 0,
        path: str = "",
        content_type: str = "",
        compression: str = "",
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
                    transfer_id, direction, object_id, sha256, size_bytes,
                    storage_size_bytes, path, content_type, compression,
                    status, attempts, trace_id, last_error,
                    started_at, updated_at, completed_at, next_attempt_ts, next_attempt_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', 1, ?, '', ?, ?, '', 0, '')
                ON CONFLICT(transfer_id) DO UPDATE SET
                    direction=excluded.direction,
                    object_id=excluded.object_id,
                    sha256=excluded.sha256,
                    size_bytes=excluded.size_bytes,
                    storage_size_bytes=excluded.storage_size_bytes,
                    path=excluded.path,
                    content_type=excluded.content_type,
                    compression=excluded.compression,
                    status='running',
                    attempts=object_transfers.attempts + 1,
                    trace_id=excluded.trace_id,
                    last_error='',
                    updated_at=excluded.updated_at,
                    completed_at='',
                    next_attempt_ts=0,
                    next_attempt_at=''
                """,
                (
                    safe_transfer_id,
                    safe_direction,
                    str(object_id or "").strip(),
                    str(sha256 or "").strip(),
                    max(0, int(size_bytes or 0)),
                    max(0, int(storage_size_bytes or 0)),
                    str(path or "").strip(),
                    str(content_type or "").strip(),
                    str(compression or "").strip(),
                    str(trace_id or "").strip(),
                    now,
                    now,
                ),
            )
        self.notify_changed()
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
                    next_attempt_ts = 0,
                    next_attempt_at = '',
                    updated_at = ?,
                    completed_at = ?
                WHERE transfer_id = ?
                """,
                (safe_status, str(object_id or "").strip(), str(path or "").strip(), now, now, safe_transfer_id),
            )
        if int(cursor.rowcount or 0) > 0:
            self.notify_changed()
        return {"ok": int(cursor.rowcount or 0) > 0, "transfer_id": safe_transfer_id, "status": safe_status}

    def update_transfer_metadata(
        self,
        transfer_id: str,
        *,
        object_id: str = "",
        sha256: str = "",
        size_bytes: int | None = None,
        storage_size_bytes: int | None = None,
        path: str = "",
        content_type: str = "",
        compression: str = "",
    ) -> dict[str, Any]:
        safe_transfer_id = str(transfer_id or "").strip()
        if not safe_transfer_id:
            return {"ok": False, "message": "transfer_id is required"}
        now = local_now().isoformat(timespec="seconds")
        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE object_transfers
                SET object_id = COALESCE(NULLIF(?, ''), object_id),
                    sha256 = COALESCE(NULLIF(?, ''), sha256),
                    size_bytes = CASE WHEN ? >= 0 THEN ? ELSE size_bytes END,
                    storage_size_bytes = CASE WHEN ? >= 0 THEN ? ELSE storage_size_bytes END,
                    path = COALESCE(NULLIF(?, ''), path),
                    content_type = COALESCE(NULLIF(?, ''), content_type),
                    compression = COALESCE(NULLIF(?, ''), compression),
                    updated_at = ?
                WHERE transfer_id = ?
                """,
                (
                    str(object_id or "").strip(),
                    str(sha256 or "").strip(),
                    int(size_bytes) if size_bytes is not None else -1,
                    max(0, int(size_bytes or 0)),
                    int(storage_size_bytes) if storage_size_bytes is not None else -1,
                    max(0, int(storage_size_bytes or 0)),
                    str(path or "").strip(),
                    str(content_type or "").strip(),
                    str(compression or "").strip(),
                    now,
                    safe_transfer_id,
                ),
            )
        if int(cursor.rowcount or 0) > 0:
            self.notify_changed()
        return {"ok": int(cursor.rowcount or 0) > 0, "transfer_id": safe_transfer_id}

    def fail_transfer(
        self,
        transfer_id: str,
        message: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> dict[str, Any]:
        safe_transfer_id = str(transfer_id or "").strip()
        if not safe_transfer_id:
            return {"ok": False, "message": "transfer_id is required"}
        safe_message = str(message or "object transfer failed").strip()[:1000]
        now = local_now().isoformat(timespec="seconds")
        now_ts = time.time()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT attempts FROM object_transfers WHERE transfer_id = ?",
                (safe_transfer_id,),
            ).fetchone()
            attempts = max(1, int((row[0] if row else 0) or 0))
            next_attempt_ts = now_ts + _transfer_retry_delay(attempts, retry_after_seconds=retry_after_seconds)
            next_attempt_at = datetime.fromtimestamp(next_attempt_ts, tz=timezone.utc).isoformat(timespec="seconds")
            cursor = conn.execute(
                """
                UPDATE object_transfers
                SET status = 'failed',
                    last_error = ?,
                    next_attempt_ts = ?,
                    next_attempt_at = ?,
                    updated_at = ?
                WHERE transfer_id = ?
                """,
                (safe_message, next_attempt_ts, next_attempt_at, now, safe_transfer_id),
            )
        if int(cursor.rowcount or 0) > 0:
            self.notify_changed()
        return {"ok": int(cursor.rowcount or 0) > 0, "transfer_id": safe_transfer_id, "status": "failed"}

    def diagnostics(self, *, failed_limit: int = 10) -> dict[str, Any]:
        retryable = self.retryable_transfers(limit=failed_limit)
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
                       storage_size_bytes, path, content_type, compression,
                       attempts, updated_at, last_error, next_attempt_at, next_attempt_ts
                FROM object_transfers
                ORDER BY updated_at DESC, transfer_id DESC
                LIMIT 10
                """
            ).fetchall()
            failed = conn.execute(
                """
                SELECT transfer_id, direction, status, object_id, sha256, size_bytes,
                       storage_size_bytes, path, content_type, compression,
                       attempts, updated_at, last_error, next_attempt_at, next_attempt_ts
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
        retry_status_by_direction = {
            "upload": self.retry_status(direction="upload"),
            "download": self.retry_status(direction="download"),
        }
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
            "retryable_count": len(retryable),
            "retry_status_by_direction": retry_status_by_direction,
            "retryable": retryable,
            "newest": [_row_public(row) for row in newest],
            "failed": [_row_public(row) for row in failed],
        }

    def retryable_transfers(
        self,
        *,
        limit: int = DEFAULT_RETRYABLE_TRANSFER_LIMIT,
        direction: str = "",
        max_attempts: int = DEFAULT_TRANSFER_MAX_ATTEMPTS,
        stale_running_seconds: float = DEFAULT_STALE_RUNNING_SECONDS,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit or DEFAULT_RETRYABLE_TRANSFER_LIMIT), 500))
        safe_max_attempts = max(1, int(max_attempts or DEFAULT_TRANSFER_MAX_ATTEMPTS))
        safe_direction = _normalize_optional_direction(direction)
        now_ts = time.time()
        cutoff = (local_now() - timedelta(
            seconds=max(1.0, float(stale_running_seconds or DEFAULT_STALE_RUNNING_SECONDS))
        )).isoformat(timespec="seconds")
        params: list[Any] = [safe_max_attempts, now_ts, cutoff, safe_max_attempts]
        direction_sql = ""
        if safe_direction:
            direction_sql = "AND direction = ?"
            params.append(safe_direction)
        params.append(safe_limit)
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT transfer_id, direction, status, object_id, sha256, size_bytes,
                       storage_size_bytes, path, content_type, compression,
                       attempts, updated_at, last_error, next_attempt_at, next_attempt_ts
                FROM object_transfers
                WHERE (
                    (status = 'failed' AND attempts < ? AND (next_attempt_ts <= 0 OR next_attempt_ts <= ?))
                    OR (status = 'running' AND updated_at <= ? AND attempts < ?)
                )
                {direction_sql}
                ORDER BY updated_at ASC, transfer_id ASC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [_row_public(row) for row in rows]

    def retry_status(
        self,
        *,
        direction: str = "",
        max_attempts: int = DEFAULT_TRANSFER_MAX_ATTEMPTS,
        stale_running_seconds: float = DEFAULT_STALE_RUNNING_SECONDS,
    ) -> dict[str, Any]:
        safe_max_attempts = max(1, int(max_attempts or DEFAULT_TRANSFER_MAX_ATTEMPTS))
        safe_direction = _normalize_optional_direction(direction)
        now_ts = time.time()
        cutoff = (local_now() - timedelta(
            seconds=max(1.0, float(stale_running_seconds or DEFAULT_STALE_RUNNING_SECONDS))
        )).isoformat(timespec="seconds")
        params: list[Any] = []
        direction_sql = ""
        if safe_direction:
            direction_sql = "WHERE direction = ?"
            params.append(safe_direction)
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT status, attempts, updated_at, next_attempt_ts
                FROM object_transfers
                {direction_sql}
                """,
                tuple(params),
            ).fetchall()
        retry_ready_count = 0
        retry_waiting_count = 0
        running_stale_count = 0
        failed_count = 0
        next_retry_ts: float | None = None
        for status, attempts, updated_at, next_attempt_ts in rows:
            safe_status = str(status or "")
            safe_attempts = max(0, int(attempts or 0))
            if safe_attempts >= safe_max_attempts:
                continue
            if safe_status == "failed":
                failed_count += 1
                safe_next_attempt_ts = _safe_float(next_attempt_ts)
                if safe_next_attempt_ts <= 0 or safe_next_attempt_ts <= now_ts:
                    retry_ready_count += 1
                    continue
                retry_waiting_count += 1
                if next_retry_ts is None or safe_next_attempt_ts < next_retry_ts:
                    next_retry_ts = safe_next_attempt_ts
                continue
            if safe_status == "running" and str(updated_at or "") <= cutoff:
                running_stale_count += 1
                retry_ready_count += 1
        next_retry_after_seconds = (
            int(max(0.0, next_retry_ts - now_ts) + 0.999)
            if next_retry_ts is not None
            else 0
        )
        if retry_ready_count > 0:
            wait_reason = "ready"
        elif retry_waiting_count > 0:
            wait_reason = "waiting_retry_backoff"
        elif failed_count > 0:
            wait_reason = "attempt_budget_exhausted"
        else:
            wait_reason = "idle"
        return {
            "direction": safe_direction or "all",
            "retry_ready_count": retry_ready_count,
            "retry_waiting_count": retry_waiting_count,
            "running_stale_count": running_stale_count,
            "failed_count": failed_count,
            "next_retry_after_seconds": next_retry_after_seconds,
            "wait_reason": wait_reason,
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
                    storage_size_bytes INTEGER NOT NULL DEFAULT 0,
                    path TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    compression TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    trace_id TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT '',
                    next_attempt_ts REAL NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            _ensure_object_transfer_column(conn, "storage_size_bytes", "INTEGER NOT NULL DEFAULT 0")
            _ensure_object_transfer_column(conn, "compression", "TEXT NOT NULL DEFAULT ''")
            _ensure_object_transfer_column(conn, "next_attempt_ts", "REAL NOT NULL DEFAULT 0")
            _ensure_object_transfer_column(conn, "next_attempt_at", "TEXT NOT NULL DEFAULT ''")
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


def _normalize_optional_direction(value: str) -> str:
    direction = str(value or "").strip().lower()
    return direction if direction in {"upload", "download"} else ""


def _row_public(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    next_attempt_at = str(row[13] or "") if len(row) > 13 else ""
    next_attempt_ts = _safe_float(row[14]) if len(row) > 14 else 0.0
    next_attempt_after_seconds = (
        int(max(0.0, next_attempt_ts - time.time()) + 0.999)
        if next_attempt_ts > 0
        else 0
    )
    return {
        "transfer_id": str(row[0] or ""),
        "direction": str(row[1] or ""),
        "status": str(row[2] or ""),
        "object_id": str(row[3] or ""),
        "sha256": str(row[4] or ""),
        "size_bytes": int(row[5] or 0),
        "storage_size_bytes": int(row[6] or 0),
        "path": str(row[7] or ""),
        "content_type": str(row[8] or ""),
        "compression": str(row[9] or ""),
        "attempts": int(row[10] or 0),
        "updated_at": str(row[11] or ""),
        "last_error": str(row[12] or ""),
        "next_attempt_at": next_attempt_at,
        "next_attempt_after_seconds": next_attempt_after_seconds,
    }


def _ensure_object_transfer_column(conn: sqlite3.Connection, name: str, definition: str) -> None:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(object_transfers)").fetchall()}
    if name not in columns:
        conn.execute(f"ALTER TABLE object_transfers ADD COLUMN {name} {definition}")


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _transfer_retry_delay(attempts: int, *, retry_after_seconds: float | None = None) -> float:
    if retry_after_seconds is not None:
        try:
            return max(0.0, float(retry_after_seconds))
        except Exception:
            return 0.0
    safe_attempts = max(1, int(attempts or 1))
    index = min(safe_attempts - 1, len(_TRANSFER_RETRY_BACKOFF_SECONDS) - 1)
    return float(_TRANSFER_RETRY_BACKOFF_SECONDS[index])
