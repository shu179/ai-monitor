from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .app_paths import resolve_app_path
from .local_account_space import account_scoped_path
from .sqlite_tuning import apply_runtime_pragmas, perform_startup_maintenance, truncate_wal_if_oversized
from .time_utils import local_now


DEFAULT_CLOUD_STATE_DELTA_INBOX_PATH = resolve_app_path("user_data/cloud_state_delta_inbox.sqlite3")


class CloudStateDeltaInbox:
    """Durable local inbox for v2 state-delta changes before business appliers run."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._explicit_path = Path(db_path) if db_path is not None else None
        self._startup_maintenance_done = False

    @property
    def db_path(self) -> Path:
        if self._explicit_path is not None:
            return self._explicit_path
        return account_scoped_path(
            "user_data/cloud_state_delta_inbox.sqlite3",
            fallback=DEFAULT_CLOUD_STATE_DELTA_INBOX_PATH,
        )

    def record_changes(
        self,
        *,
        identity_key: str,
        changes: list[dict[str, Any]],
        object_refs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        safe_identity = str(identity_key or "").strip()
        normalized = [_normalize_change(change) for change in changes or [] if isinstance(change, dict)]
        if not safe_identity or not normalized:
            return {"requested": len(changes or []), "created": 0, "duplicates": 0, "streams": {}}
        refs_by_id = _object_refs_by_id(object_refs)
        now = local_now().isoformat(timespec="seconds")
        streams: dict[str, int] = {}
        created = 0
        duplicates = 0
        with self._connection() as conn:
            for change in normalized:
                stream = change["stream"]
                seq = change["seq"]
                ref_id = change["ref_id"]
                key = f"{safe_identity}|{stream}|{seq}|{ref_id}"
                entity = change.get("entity") if isinstance(change.get("entity"), dict) else {}
                object_refs_for_change = _object_refs_for_change(change, entity, refs_by_id)
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO state_delta_inbox(
                        identity_key, stream, seq, ref_id, kind, status,
                        change_json, entity_json, object_refs_json,
                        first_seen_at, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                    """,
                    (
                        safe_identity,
                        stream,
                        seq,
                        ref_id,
                        change["kind"],
                        json.dumps(change["raw"], ensure_ascii=False, sort_keys=True),
                        json.dumps(entity, ensure_ascii=False, sort_keys=True),
                        json.dumps(object_refs_for_change, ensure_ascii=False, sort_keys=True),
                        now,
                        now,
                    ),
                )
                if int(cursor.rowcount or 0) > 0:
                    created += 1
                    streams[stream] = streams.get(stream, 0) + 1
                else:
                    duplicates += 1
        return {
            "requested": len(normalized),
            "created": created,
            "duplicates": duplicates,
            "streams": streams,
        }

    def diagnostics(self, *, failed_limit: int = 10) -> dict[str, Any]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT status, stream, COUNT(*) AS count
                FROM state_delta_inbox
                GROUP BY status, stream
                ORDER BY status, stream
                """
            ).fetchall()
            newest = conn.execute(
                """
                SELECT identity_key, stream, seq, ref_id, kind, status, updated_at, last_error
                FROM state_delta_inbox
                ORDER BY updated_at DESC, id DESC
                LIMIT 10
                """
            ).fetchall()
            failed = conn.execute(
                """
                SELECT identity_key, stream, seq, ref_id, kind, attempts, updated_at, last_error
                FROM state_delta_inbox
                WHERE status = 'failed'
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (max(0, int(failed_limit or 0)),),
            ).fetchall()
        by_status: dict[str, int] = {}
        by_stream: dict[str, dict[str, int]] = {}
        for row in rows:
            status = str(row[0] or "")
            stream = str(row[1] or "")
            count = int(row[2] or 0)
            by_status[status] = by_status.get(status, 0) + count
            by_stream.setdefault(stream, {})[status] = count
        return {
            "path": str(self.db_path),
            "total": sum(by_status.values()),
            "by_status": by_status,
            "by_stream": by_stream,
            "newest": [_row_public(row) for row in newest],
            "failed": [_failed_row_public(row) for row in failed],
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
                CREATE TABLE IF NOT EXISTS state_delta_inbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    identity_key TEXT NOT NULL,
                    stream TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    ref_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    change_json TEXT NOT NULL,
                    entity_json TEXT NOT NULL,
                    object_refs_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_error TEXT NOT NULL DEFAULT '',
                    UNIQUE(identity_key, stream, seq, ref_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_state_delta_inbox_status_stream_seq
                ON state_delta_inbox(status, stream, seq)
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


def _normalize_change(change: dict[str, Any]) -> dict[str, Any]:
    stream = str(change.get("stream") or "").strip()[:64]
    kind = str(change.get("kind") or "").strip()[:128]
    ref_id = str(change.get("ref_id") or change.get("refId") or "").strip()[:256]
    try:
        seq = max(0, int(change.get("seq") or 0))
    except Exception:
        seq = 0
    if not ref_id:
        ref_id = f"{stream}:{seq}"
    if not kind:
        kind = f"{stream}.unknown" if stream else "unknown"
    return {
        "stream": stream or "unknown",
        "seq": seq,
        "kind": kind,
        "ref_id": ref_id,
        "entity": change.get("entity") if isinstance(change.get("entity"), dict) else {},
        "raw": dict(change),
    }


def _object_refs_by_id(object_refs: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for ref in object_refs or []:
        if not isinstance(ref, dict):
            continue
        for key in ("object_id", "objectId", "id"):
            value = str(ref.get(key) or "").strip()
            if value:
                out[value] = dict(ref)
                break
    return out


def _object_refs_for_change(
    change: dict[str, Any],
    entity: dict[str, Any],
    refs_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    object_ids: set[str] = set()
    for source in (change, entity):
        for key in ("object_id", "objectId", "object_ids", "objectIds"):
            raw = source.get(key) if isinstance(source, dict) else None
            if isinstance(raw, list):
                object_ids.update(str(item or "").strip() for item in raw if str(item or "").strip())
            elif str(raw or "").strip():
                object_ids.add(str(raw or "").strip())
    return [refs_by_id[object_id] for object_id in sorted(object_ids) if object_id in refs_by_id]


def _row_public(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    return {
        "identity_key": str(row[0] or ""),
        "stream": str(row[1] or ""),
        "seq": int(row[2] or 0),
        "ref_id": str(row[3] or ""),
        "kind": str(row[4] or ""),
        "status": str(row[5] or ""),
        "updated_at": str(row[6] or ""),
        "last_error": str(row[7] or ""),
    }


def _failed_row_public(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    return {
        "identity_key": str(row[0] or ""),
        "stream": str(row[1] or ""),
        "seq": int(row[2] or 0),
        "ref_id": str(row[3] or ""),
        "kind": str(row[4] or ""),
        "attempts": int(row[5] or 0),
        "updated_at": str(row[6] or ""),
        "last_error": str(row[7] or ""),
    }
