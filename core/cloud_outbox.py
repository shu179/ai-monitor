from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .app_paths import resolve_app_path
from .file_lock import CrossProcessRLock
from .local_account_space import account_profile_dir_from_session, account_scoped_path
from .sqlite_tuning import apply_runtime_pragmas, perform_startup_maintenance, truncate_wal_if_oversized
from .time_utils import local_now

DEFAULT_CLOUD_OUTBOX_PATH = resolve_app_path("user_data/cloud_outbox.json")
DEFAULT_CLOUD_OUTBOX_DB_PATH = resolve_app_path("user_data/cloud_outbox.sqlite3")
DEFAULT_MAX_ITEMS = 10_000
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_SENT_ITEMS = 1_000
CLOUD_OUTBOX_SCHEMA_VERSION = 1

DEFAULT_MAX_ATTEMPTS = 5
_RETRY_BACKOFF_SECONDS = (60, 300, 1800, 7200)


def _backoff_delay(attempts: int) -> float:
    """Return backoff delay in seconds for the given attempt count (1-indexed)."""
    if attempts <= 0:
        return 0.0
    idx = min(attempts - 1, len(_RETRY_BACKOFF_SECONDS) - 1)
    return float(_RETRY_BACKOFF_SECONDS[idx])


class CloudOutbox:
    _change_event = threading.Event()

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        max_items: int | None = None,
        max_bytes: int | None = None,
        max_sent_items: int | None = None,
    ) -> None:
        self._explicit_path = Path(path) if path is not None else None
        self._lock = CrossProcessRLock(lambda: self._lock_path())
        self._max_items = max(1, int(max_items or DEFAULT_MAX_ITEMS))
        self._max_bytes = max(1024, int(max_bytes or DEFAULT_MAX_BYTES))
        self._max_sent_items = max(0, int(DEFAULT_MAX_SENT_ITEMS if max_sent_items is None else max_sent_items))
        self._startup_maintenance_done = False

    @property
    def path(self) -> Path:
        if self._explicit_path is not None:
            return self._explicit_path
        return account_scoped_path("user_data/cloud_outbox.json", fallback=DEFAULT_CLOUD_OUTBOX_PATH)

    @property
    def db_path(self) -> Path:
        if self._explicit_path is not None:
            return _sqlite_path_for_legacy_json_path(self._explicit_path)
        return account_scoped_path("user_data/cloud_outbox.sqlite3", fallback=DEFAULT_CLOUD_OUTBOX_DB_PATH)

    @staticmethod
    def path_for_session(session: dict[str, Any] | None) -> Path:
        profile_dir = account_profile_dir_from_session(session)
        if profile_dir is None:
            return DEFAULT_CLOUD_OUTBOX_PATH
        return profile_dir / "user_data/cloud_outbox.json"

    @staticmethod
    def db_path_for_session(session: dict[str, Any] | None) -> Path:
        return _sqlite_path_for_legacy_json_path(CloudOutbox.path_for_session(session))

    def _lock_path(self) -> Path:
        return self.db_path.with_name(f"{self.db_path.name}.lock")

    def bind_to_session(self, session: dict[str, Any] | None) -> "CloudOutbox":
        if self._explicit_path is not None:
            return self
        return CloudOutbox(
            self.path_for_session(session),
            max_items=self._max_items,
            max_bytes=self._max_bytes,
            max_sent_items=self._max_sent_items,
        )

    @classmethod
    def notify_changed(cls) -> None:
        cls._change_event.set()

    @classmethod
    def wait_for_change(cls, timeout: float) -> bool:
        changed = cls._change_event.wait(max(0.0, float(timeout or 0.0)))
        if changed:
            cls._change_event.clear()
        return changed

    def enqueue(self, *, event_type: str, idempotency_key: str, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        event_type = str(event_type or "").strip()
        idempotency_key = str(idempotency_key or "").strip()
        if not event_type or not idempotency_key:
            raise ValueError("event_type and idempotency_key are required")
        with self._lock, self._connection() as conn:
            existing = self._item_by_key_locked(conn, idempotency_key)
            if existing is not None:
                return existing, False
            now = local_now().isoformat(timespec="seconds")
            item = {
                "event_type": event_type,
                "idempotency_key": idempotency_key,
                "payload": dict(payload or {}),
                "status": "pending",
                "attempts": 0,
                "created_at": now,
                "updated_at": now,
                "last_error": "",
            }
            self._insert_item_locked(conn, item)
            self._compact_locked(conn)
            self.notify_changed()
            return dict(item), True

    def enqueue_many(self, events: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> dict[str, Any]:
        """Enqueue multiple events with one SQLite transaction."""
        normalized_events: list[dict[str, Any]] = []
        for event in events or []:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("event_type") or "").strip()
            idempotency_key = str(event.get("idempotency_key") or "").strip()
            if not event_type or not idempotency_key:
                raise ValueError("event_type and idempotency_key are required")
            normalized_events.append({
                "event_type": event_type,
                "idempotency_key": idempotency_key,
                "payload": dict(event.get("payload") or {}),
            })
        if not normalized_events:
            return {"items": [], "created": 0, "requested": 0, "dropped": _empty_dropped()}

        with self._lock, self._connection() as conn:
            by_key = self._items_by_keys_locked(
                conn,
                [event["idempotency_key"] for event in normalized_events],
            )
            queued_items: list[dict[str, Any]] = []
            created = 0
            now = local_now().isoformat(timespec="seconds")
            for event in normalized_events:
                existing = by_key.get(event["idempotency_key"])
                if existing is not None:
                    queued_items.append(dict(existing))
                    continue
                item = {
                    "event_type": event["event_type"],
                    "idempotency_key": event["idempotency_key"],
                    "payload": dict(event.get("payload") or {}),
                    "status": "pending",
                    "attempts": 0,
                    "created_at": now,
                    "updated_at": now,
                    "last_error": "",
                }
                self._insert_item_locked(conn, item)
                by_key[event["idempotency_key"]] = item
                queued_items.append(dict(item))
                created += 1
            dropped = self._compact_locked(conn) if created else _empty_dropped()
            if created:
                self.notify_changed()
            return {
                "items": queued_items,
                "created": created,
                "requested": len(normalized_events),
                "dropped": dropped,
            }

    def pending(self, *, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = min(max(int(limit or 100), 1), 500)
        now = time.time()
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM cloud_outbox_items
                WHERE status = 'pending'
                   OR (status = 'failed' AND (next_attempt_ts IS NULL OR next_attempt_ts <= ?))
                ORDER BY created_at ASC, rowid ASC
                LIMIT ?
                """,
                (now, safe_limit),
            ).fetchall()
        return [_item_from_row(row) for row in rows]

    def mark_sent(self, idempotency_keys: list[str] | set[str] | tuple[str, ...]) -> None:
        keys = {str(key or "").strip() for key in idempotency_keys if str(key or "").strip()}
        if not keys:
            return
        now = local_now().isoformat(timespec="seconds")
        with self._lock, self._connection() as conn:
            placeholders = ",".join("?" for _ in keys)
            conn.execute(
                f"""
                UPDATE cloud_outbox_items
                SET status = 'sent',
                    updated_at = ?,
                    last_error = '',
                    next_attempt_ts = NULL,
                    next_attempt_at = ''
                WHERE idempotency_key IN ({placeholders})
                """,
                (now, *sorted(keys)),
            )
            self._compact_locked(conn)

    def mark_failed(
        self,
        idempotency_keys: list[str] | set[str] | tuple[str, ...],
        message: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        keys = {str(key or "").strip() for key in idempotency_keys if str(key or "").strip()}
        if not keys:
            return
        now_iso = local_now().isoformat(timespec="seconds")
        now_ts = time.time()
        dead_letter_events: list[dict[str, Any]] = []
        with self._lock, self._connection() as conn:
            for item in self._items_by_keys_locked(conn, sorted(keys)).values():
                if _outbox_status(item) == "sent":
                    continue
                attempts = int(item.get("attempts") or 0) + 1
                item["attempts"] = attempts
                item["updated_at"] = now_iso
                item["last_error"] = str(message or "上传失败").strip()
                if attempts >= DEFAULT_MAX_ATTEMPTS:
                    item["status"] = "dead_letter"
                    item["dead_lettered_at"] = now_iso
                    item["dead_letter_reason"] = item["last_error"]
                    dead_letter_events.append(dict(item))
                else:
                    item["status"] = "failed"
                    next_ts = now_ts + _failure_retry_delay(attempts, retry_after_seconds=retry_after_seconds)
                    item["next_attempt_ts"] = next_ts
                    item["next_attempt_at"] = datetime.fromtimestamp(next_ts, tz=timezone.utc).isoformat(timespec="seconds")
                    item["dead_lettered_at"] = ""
                    item["dead_letter_reason"] = ""
                self._update_item_locked(conn, item)
            self._compact_locked(conn)
        for evt in dead_letter_events:
            try:
                from .diagnostics import record_event
                record_event(
                    "cloud_sync",
                    f"事件达到最大重试次数，进入 dead-letter: {evt.get('idempotency_key', '')}",
                    level="warning",
                    details={
                        "idempotency_key": evt.get("idempotency_key", ""),
                        "event_type": evt.get("event_type", ""),
                        "attempts": evt.get("attempts", 0),
                        "last_error": evt.get("last_error", ""),
                    },
                )
            except Exception:
                pass

    def stats(self, *, include_retry: bool = False) -> dict[str, int]:
        now = time.time()
        with self._lock, self._connection() as conn:
            return self._stats_locked(conn, now, include_retry=include_retry)

    def diagnostics(self, *, failed_limit: int = 10) -> dict[str, Any]:
        """Return local outbox health details without exposing event payloads."""
        safe_limit = min(max(int(failed_limit or 10), 0), 100)
        now = time.time()
        with self._lock, self._connection() as conn:
            failed_items = [
                self._diagnostic_item(_item_from_row(row), now)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM cloud_outbox_items
                    WHERE status = 'failed'
                    ORDER BY updated_at DESC, rowid DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()
            ]
            dead_letter_items = [
                self._diagnostic_item(_item_from_row(row), now)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM cloud_outbox_items
                    WHERE status = 'dead_letter'
                    ORDER BY updated_at DESC, rowid DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()
            ]
            stats = self._stats_locked(conn, now, include_retry=True)
        return {
            "path": str(self.path),
            "db_path": str(self.db_path),
            "legacy_json_path": str(self.path),
            "stats": stats,
            "failed": failed_items,
            "dead_letter": dead_letter_items,
            "sample_limit": safe_limit,
        }

    @staticmethod
    def _diagnostic_item(item: dict[str, Any], now: float) -> dict[str, Any]:
        next_ts = _next_attempt_ts(item)
        next_after = 0
        if next_ts is not None:
            next_after = int(max(0.0, next_ts - now) + 0.999)
        return {
            "idempotency_key": str(item.get("idempotency_key") or ""),
            "event_type": str(item.get("event_type") or ""),
            "status": _outbox_status(item),
            "attempts": _safe_int(item.get("attempts"), 0),
            "last_error": str(item.get("last_error") or ""),
            "next_attempt_at": str(item.get("next_attempt_at") or ""),
            "next_attempt_after_seconds": next_after,
            "retry_ready": _is_retry_ready(item, now) if _outbox_status(item) == "failed" else False,
            "updated_at": str(item.get("updated_at") or ""),
            "created_at": str(item.get("created_at") or ""),
            "dead_lettered_at": str(item.get("dead_lettered_at") or ""),
            "dead_letter_reason": str(item.get("dead_letter_reason") or ""),
        }

    def _stats_locked(self, conn: sqlite3.Connection, now: float, *, include_retry: bool = False) -> dict[str, int]:
        stats: dict[str, int] = {"total": 0, "pending": 0, "failed": 0, "sent": 0, "dead_letter": 0}
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM cloud_outbox_items
            GROUP BY status
            """
        ).fetchall()
        for row in rows:
            status = str(row["status"] or "pending")
            count = int(row["count"] or 0)
            stats["total"] += count
            if status in stats:
                stats[status] += count
        if include_retry:
            retry_ready = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM cloud_outbox_items
                    WHERE status = 'failed'
                      AND (next_attempt_ts IS NULL OR next_attempt_ts <= ?)
                    """,
                    (now,),
                ).fetchone()[0]
                or 0
            )
            next_retry_row = conn.execute(
                """
                SELECT MIN(next_attempt_ts)
                FROM cloud_outbox_items
                WHERE status = 'failed'
                  AND next_attempt_ts IS NOT NULL
                  AND next_attempt_ts > ?
                """,
                (now,),
            ).fetchone()
            next_retry_ts = float(next_retry_row[0]) if next_retry_row and next_retry_row[0] is not None else None
            stats["retry_ready"] = retry_ready
            stats["upload_ready"] = int(stats.get("pending") or 0) + retry_ready
            stats["next_retry_after_seconds"] = (
                int(max(0.0, next_retry_ts - now) + 0.999)
                if next_retry_ts is not None
                else 0
            )
        return stats

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            self._migrate_legacy_json_if_needed(conn)
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
        conn.row_factory = sqlite3.Row
        try:
            apply_runtime_pragmas(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cloud_outbox_items (
                    idempotency_key TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_error TEXT NOT NULL DEFAULT '',
                    next_attempt_ts REAL,
                    next_attempt_at TEXT NOT NULL DEFAULT '',
                    dead_lettered_at TEXT NOT NULL DEFAULT '',
                    dead_letter_reason TEXT NOT NULL DEFAULT '',
                    byte_size INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cloud_outbox_status_created
                ON cloud_outbox_items(status, created_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cloud_outbox_updated
                ON cloud_outbox_items(updated_at)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cloud_outbox_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self._ensure_schema_columns(conn)
            conn.execute(
                """
                INSERT OR REPLACE INTO cloud_outbox_meta(key, value)
                VALUES('schema_version', ?)
                """,
                (str(CLOUD_OUTBOX_SCHEMA_VERSION),),
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

    def _ensure_schema_columns(self, conn: sqlite3.Connection) -> None:
        existing = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(cloud_outbox_items)").fetchall()
        }
        columns = {
            "next_attempt_ts": "REAL",
            "next_attempt_at": "TEXT NOT NULL DEFAULT ''",
            "dead_lettered_at": "TEXT NOT NULL DEFAULT ''",
            "dead_letter_reason": "TEXT NOT NULL DEFAULT ''",
            "byte_size": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE cloud_outbox_items ADD COLUMN {name} {definition}")

    def _load_locked(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM cloud_outbox_items
                ORDER BY created_at ASC, rowid ASC
                """
            ).fetchall()
        return [_item_from_row(row) for row in rows]

    def _save_locked(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        with self._connection() as conn:
            conn.execute("DELETE FROM cloud_outbox_items")
            for item in items:
                if isinstance(item, dict):
                    self._insert_item_locked(conn, dict(item))
            return self._compact_locked(conn)

    def _migrate_legacy_json_if_needed(self, conn: sqlite3.Connection) -> None:
        marker_key = f"legacy_json_migrated:{self.path}"
        marker = conn.execute(
            "SELECT value FROM cloud_outbox_meta WHERE key = ?",
            (marker_key,),
        ).fetchone()
        if marker is not None:
            return
        items = _load_legacy_json_items(self.path)
        migrated = 0
        for item in items:
            normalized = _normalize_item(item)
            if normalized is None:
                continue
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO cloud_outbox_items(
                    idempotency_key, event_type, payload_json, status, attempts,
                    created_at, updated_at, last_error, next_attempt_ts,
                    next_attempt_at, dead_lettered_at, dead_letter_reason, byte_size
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _item_values(normalized),
            )
            if int(cursor.rowcount or 0) > 0:
                migrated += 1
        now = local_now().isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT OR REPLACE INTO cloud_outbox_meta(key, value)
            VALUES(?, ?)
            """,
            (
                marker_key,
                json.dumps(
                    {"migrated_at": now, "legacy_json_path": str(self.path), "items": migrated},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )
        if items:
            self._write_migration_marker(migrated=migrated, seen=len(items), migrated_at=now)

    def _write_migration_marker(self, *, migrated: int, seen: int, migrated_at: str) -> None:
        marker_path = self.path.with_name(f"{self.path.name}.migrated-to-sqlite")
        payload = {
            "migrated_at": migrated_at,
            "legacy_json_path": str(self.path),
            "sqlite_path": str(self.db_path),
            "seen": int(seen or 0),
            "migrated": int(migrated or 0),
        }
        try:
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        except Exception:
            pass

    def _item_by_key_locked(self, conn: sqlite3.Connection, idempotency_key: str) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM cloud_outbox_items WHERE idempotency_key = ?",
            (str(idempotency_key or "").strip(),),
        ).fetchone()
        return _item_from_row(row) if row is not None else None

    def _items_by_keys_locked(self, conn: sqlite3.Connection, keys: list[str] | tuple[str, ...]) -> dict[str, dict[str, Any]]:
        safe_keys = [str(key or "").strip() for key in keys if str(key or "").strip()]
        if not safe_keys:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for chunk_start in range(0, len(safe_keys), 500):
            chunk = safe_keys[chunk_start:chunk_start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT * FROM cloud_outbox_items WHERE idempotency_key IN ({placeholders})",
                tuple(chunk),
            ).fetchall()
            for row in rows:
                item = _item_from_row(row)
                out[str(item.get("idempotency_key") or "")] = item
        return out

    def _insert_item_locked(self, conn: sqlite3.Connection, item: dict[str, Any]) -> None:
        normalized = _normalize_item(item)
        if normalized is None:
            return
        conn.execute(
            """
            INSERT OR REPLACE INTO cloud_outbox_items(
                idempotency_key, event_type, payload_json, status, attempts,
                created_at, updated_at, last_error, next_attempt_ts,
                next_attempt_at, dead_lettered_at, dead_letter_reason, byte_size
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _item_values(normalized),
        )

    def _update_item_locked(self, conn: sqlite3.Connection, item: dict[str, Any]) -> None:
        normalized = _normalize_item(item)
        if normalized is None:
            return
        values = _item_values(normalized)
        conn.execute(
            """
            UPDATE cloud_outbox_items
            SET event_type = ?,
                payload_json = ?,
                status = ?,
                attempts = ?,
                created_at = ?,
                updated_at = ?,
                last_error = ?,
                next_attempt_ts = ?,
                next_attempt_at = ?,
                dead_lettered_at = ?,
                dead_letter_reason = ?,
                byte_size = ?
            WHERE idempotency_key = ?
            """,
            (
                values[1],
                values[2],
                values[3],
                values[4],
                values[5],
                values[6],
                values[7],
                values[8],
                values[9],
                values[10],
                values[11],
                values[12],
                values[0],
            ),
        )

    def _compact_locked(self, conn: sqlite3.Connection) -> dict[str, Any]:
        dropped = _empty_dropped()

        sent_count = int(
            conn.execute("SELECT COUNT(*) FROM cloud_outbox_items WHERE status = 'sent'").fetchone()[0]
            or 0
        )
        if sent_count > self._max_sent_items:
            dropped_count = self._delete_oldest_sent_locked(conn, sent_count - self._max_sent_items)
            dropped["total"] += dropped_count
            dropped["sent"] += dropped_count

        total_count = int(conn.execute("SELECT COUNT(*) FROM cloud_outbox_items").fetchone()[0] or 0)
        if total_count > self._max_items:
            dropped_count = self._delete_oldest_sent_locked(conn, total_count - self._max_items)
            dropped["total"] += dropped_count
            dropped["sent"] += dropped_count

        total_bytes = self._total_byte_size_locked(conn)
        if total_bytes > self._max_bytes:
            dropped_count = self._delete_oldest_sent_until_size_locked(conn)
            dropped["total"] += dropped_count
            dropped["sent"] += dropped_count
            total_bytes = self._total_byte_size_locked(conn)

        protected_count = int(
            conn.execute("SELECT COUNT(*) FROM cloud_outbox_items WHERE status != 'sent'").fetchone()[0]
            or 0
        )
        total_count = int(conn.execute("SELECT COUNT(*) FROM cloud_outbox_items").fetchone()[0] or 0)
        overflow = protected_count > 0 and (total_count > self._max_items or total_bytes > self._max_bytes)
        dropped["overflow"] = overflow
        dropped["overflow_items"] = max(0, total_count - self._max_items) if overflow else 0
        dropped["overflow_bytes"] = max(0, total_bytes - self._max_bytes) if overflow else 0
        dropped["active_retained"] = protected_count if overflow else 0
        if overflow:
            self._record_overflow_diagnostic(
                total_items=total_count,
                active_retained=protected_count,
                overflow_items=int(dropped["overflow_items"]),
                overflow_bytes=int(dropped["overflow_bytes"]),
            )
        return dropped

    def _delete_oldest_sent_locked(self, conn: sqlite3.Connection, count: int) -> int:
        safe_count = max(0, int(count or 0))
        if safe_count <= 0:
            return 0
        rows = conn.execute(
            """
            SELECT idempotency_key
            FROM cloud_outbox_items
            WHERE status = 'sent'
            ORDER BY created_at ASC, updated_at ASC, rowid ASC
            LIMIT ?
            """,
            (safe_count,),
        ).fetchall()
        keys = [str(row[0] or "") for row in rows if str(row[0] or "")]
        if not keys:
            return 0
        placeholders = ",".join("?" for _ in keys)
        cursor = conn.execute(
            f"DELETE FROM cloud_outbox_items WHERE idempotency_key IN ({placeholders})",
            tuple(keys),
        )
        return int(cursor.rowcount or 0)

    def _delete_oldest_sent_until_size_locked(self, conn: sqlite3.Connection) -> int:
        dropped = 0
        rows = conn.execute(
            """
            SELECT idempotency_key, byte_size
            FROM cloud_outbox_items
            WHERE status = 'sent'
            ORDER BY created_at ASC, updated_at ASC, rowid ASC
            """
        ).fetchall()
        total_bytes = self._total_byte_size_locked(conn)
        for row in rows:
            if total_bytes <= self._max_bytes:
                break
            key = str(row["idempotency_key"] or "")
            byte_size = int(row["byte_size"] or 0)
            if not key:
                continue
            cursor = conn.execute(
                "DELETE FROM cloud_outbox_items WHERE idempotency_key = ?",
                (key,),
            )
            if int(cursor.rowcount or 0) > 0:
                dropped += 1
                total_bytes = max(0, total_bytes - byte_size)
        return dropped

    @staticmethod
    def _total_byte_size_locked(conn: sqlite3.Connection) -> int:
        return int(conn.execute("SELECT COALESCE(SUM(byte_size), 0) FROM cloud_outbox_items").fetchone()[0] or 0)

    @staticmethod
    def _encoded_size(items: list[dict[str, Any]]) -> int:
        return len(json.dumps(items, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))

    def _record_overflow_diagnostic(
        self,
        *,
        total_items: int,
        active_retained: int,
        overflow_items: int,
        overflow_bytes: int,
    ) -> None:
        try:
            from .diagnostics import record_event
            record_event(
                "cloud_sync",
                "本地 Outbox SQLite 队列超过容量上限，已保留所有未发送事件",
                level="warning",
                details={
                    "path": str(self.path),
                    "db_path": str(self.db_path),
                    "total_items": int(total_items or 0),
                    "active_retained": int(active_retained or 0),
                    "overflow_items": int(overflow_items or 0),
                    "overflow_bytes": int(overflow_bytes or 0),
                },
            )
        except Exception:
            pass

    def vacuum_wal_if_needed(self) -> bool:
        """Future daily-maintenance hook; startup maintenance is the current safety net."""
        with self._lock, self._connection() as conn:
            return truncate_wal_if_oversized(conn, self.db_path)


def _outbox_status(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "pending").strip() or "pending"
    if status not in {"pending", "failed", "sent", "dead_letter"}:
        return "pending"
    return status


def _is_retry_ready(item: dict[str, Any], now: float) -> bool:
    """Check if a failed item is ready for retry. Treats bad/missing next_attempt_ts as ready."""
    next_ts = _next_attempt_ts(item)
    if next_ts is None:
        return True
    return next_ts <= now


def _next_attempt_ts(item: dict[str, Any]) -> float | None:
    next_ts = item.get("next_attempt_ts")
    if next_ts is None:
        return None
    try:
        return float(next_ts)
    except (TypeError, ValueError):
        return None


def _empty_dropped() -> dict[str, Any]:
    return {"total": 0, "active": 0, "sent": 0, "dead_letter": 0, "overflow": False, "overflow_items": 0, "overflow_bytes": 0, "active_retained": 0}


def _failure_retry_delay(attempts: int, *, retry_after_seconds: float | None = None) -> float:
    try:
        if retry_after_seconds is not None:
            return max(0.0, float(retry_after_seconds))
    except Exception:
        pass
    return _backoff_delay(attempts)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _outbox_sort_key(item: dict[str, Any]) -> str:
    return str(item.get("created_at") or item.get("updated_at") or "").strip()


def _sqlite_path_for_legacy_json_path(path: str | Path) -> Path:
    target = Path(path)
    if target.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
        return target
    if target.suffix:
        return target.with_suffix(".sqlite3")
    return target.with_name(f"{target.name}.sqlite3")


def _load_legacy_json_items(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return []
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _normalize_item(item: dict[str, Any]) -> dict[str, Any] | None:
    event_type = str(item.get("event_type") or "").strip()
    idempotency_key = str(item.get("idempotency_key") or "").strip()
    if not event_type or not idempotency_key:
        return None
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    created_at = str(item.get("created_at") or item.get("updated_at") or "").strip()
    updated_at = str(item.get("updated_at") or created_at or "").strip()
    if not created_at:
        created_at = local_now().isoformat(timespec="seconds")
    if not updated_at:
        updated_at = created_at
    normalized = {
        "event_type": event_type,
        "idempotency_key": idempotency_key,
        "payload": dict(payload),
        "status": _outbox_status(item),
        "attempts": max(0, _safe_int(item.get("attempts"), 0)),
        "created_at": created_at,
        "updated_at": updated_at,
        "last_error": str(item.get("last_error") or ""),
        "next_attempt_ts": _next_attempt_ts(item),
        "next_attempt_at": str(item.get("next_attempt_at") or ""),
        "dead_lettered_at": str(item.get("dead_lettered_at") or ""),
        "dead_letter_reason": str(item.get("dead_letter_reason") or ""),
    }
    normalized["byte_size"] = _item_byte_size(normalized)
    return normalized


def _item_values(item: dict[str, Any]) -> tuple[Any, ...]:
    payload_json = json.dumps(
        item.get("payload") if isinstance(item.get("payload"), dict) else {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        str(item.get("idempotency_key") or ""),
        str(item.get("event_type") or ""),
        payload_json,
        _outbox_status(item),
        max(0, _safe_int(item.get("attempts"), 0)),
        str(item.get("created_at") or ""),
        str(item.get("updated_at") or ""),
        str(item.get("last_error") or ""),
        _next_attempt_ts(item),
        str(item.get("next_attempt_at") or ""),
        str(item.get("dead_lettered_at") or ""),
        str(item.get("dead_letter_reason") or ""),
        max(0, _safe_int(item.get("byte_size"), _item_byte_size(item))),
    )


def _item_from_row(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    payload_json = row["payload_json"] if isinstance(row, sqlite3.Row) else row[2]
    try:
        payload = json.loads(str(payload_json or "{}"))
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "event_type": str(row["event_type"] or ""),
        "idempotency_key": str(row["idempotency_key"] or ""),
        "payload": payload,
        "status": str(row["status"] or "pending"),
        "attempts": max(0, _safe_int(row["attempts"], 0)),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "last_error": str(row["last_error"] or ""),
        "next_attempt_ts": row["next_attempt_ts"],
        "next_attempt_at": str(row["next_attempt_at"] or ""),
        "dead_lettered_at": str(row["dead_lettered_at"] or ""),
        "dead_letter_reason": str(row["dead_letter_reason"] or ""),
    }


def _item_byte_size(item: dict[str, Any]) -> int:
    return len(
        json.dumps(
            {
                "event_type": str(item.get("event_type") or ""),
                "idempotency_key": str(item.get("idempotency_key") or ""),
                "payload": item.get("payload") if isinstance(item.get("payload"), dict) else {},
                "status": _outbox_status(item),
                "attempts": max(0, _safe_int(item.get("attempts"), 0)),
                "created_at": str(item.get("created_at") or ""),
                "updated_at": str(item.get("updated_at") or ""),
                "last_error": str(item.get("last_error") or ""),
                "next_attempt_ts": _next_attempt_ts(item),
                "next_attempt_at": str(item.get("next_attempt_at") or ""),
                "dead_lettered_at": str(item.get("dead_lettered_at") or ""),
                "dead_letter_reason": str(item.get("dead_letter_reason") or ""),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
