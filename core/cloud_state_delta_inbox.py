from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

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

    def claim_pending(
        self,
        *,
        limit: int = 100,
        streams: list[str] | tuple[str, ...] | set[str] | None = None,
        include_failed: bool = False,
        applying_timeout_seconds: float = 300.0,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 100), 1000))
        safe_streams = [str(stream or "").strip() for stream in (streams or []) if str(stream or "").strip()]
        conditions = ["status = 'pending'"]
        params: list[Any] = []
        if include_failed:
            conditions.append("status = 'failed'")
        if applying_timeout_seconds and float(applying_timeout_seconds) > 0:
            cutoff = (local_now() - timedelta(seconds=max(1.0, float(applying_timeout_seconds)))).isoformat(
                timespec="seconds"
            )
            conditions.append("(status = 'applying' AND updated_at <= ?)")
            params.append(cutoff)
        where_sql = "(" + " OR ".join(conditions) + ")"
        if safe_streams:
            placeholders = ",".join("?" for _ in safe_streams)
            where_sql += f" AND stream IN ({placeholders})"
            params.extend(safe_streams)
        params.append(safe_limit)
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT id, identity_key, stream, seq, ref_id, kind, attempts,
                       change_json, entity_json, object_refs_json, first_seen_at,
                       updated_at, last_error
                FROM state_delta_inbox
                WHERE {where_sql}
                ORDER BY stream ASC, seq ASC, id ASC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
            ids = [int(row[0] or 0) for row in rows if int(row[0] or 0) > 0]
            if ids:
                now = local_now().isoformat(timespec="seconds")
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"""
                    UPDATE state_delta_inbox
                    SET status = 'applying',
                        attempts = attempts + 1,
                        updated_at = ?,
                        last_error = ''
                    WHERE id IN ({placeholders})
                    """,
                    (now, *ids),
                )
        items = [_inbox_item_from_row(row) for row in rows]
        for item in items:
            item["status"] = "applying"
            item["attempts"] = int(item.get("attempts") or 0) + 1
        return items

    def mark_applied(self, ids: list[int] | tuple[int, ...] | set[int]) -> int:
        safe_ids = _safe_ids(ids)
        if not safe_ids:
            return 0
        now = local_now().isoformat(timespec="seconds")
        with self._connection() as conn:
            placeholders = ",".join("?" for _ in safe_ids)
            cursor = conn.execute(
                f"""
                UPDATE state_delta_inbox
                SET status = 'applied',
                    updated_at = ?,
                    last_error = ''
                WHERE id IN ({placeholders})
                """,
                (now, *safe_ids),
            )
        return int(cursor.rowcount or 0)

    def mark_failed(self, ids: list[int] | tuple[int, ...] | set[int], message: str) -> int:
        safe_ids = _safe_ids(ids)
        if not safe_ids:
            return 0
        now = local_now().isoformat(timespec="seconds")
        safe_message = str(message or "state-delta inbox processing failed").strip()[:1000]
        with self._connection() as conn:
            placeholders = ",".join("?" for _ in safe_ids)
            cursor = conn.execute(
                f"""
                UPDATE state_delta_inbox
                SET status = 'failed',
                    updated_at = ?,
                    last_error = ?
                WHERE id IN ({placeholders})
                """,
                (now, safe_message, *safe_ids),
            )
        return int(cursor.rowcount or 0)

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


def process_state_delta_inbox(
    *,
    inbox: CloudStateDeltaInbox | None = None,
    appliers: dict[str, Callable[[dict[str, Any]], Any]] | None = None,
    limit: int = 100,
    streams: list[str] | tuple[str, ...] | set[str] | None = None,
    include_failed: bool = False,
) -> dict[str, Any]:
    target_inbox = inbox or CloudStateDeltaInbox()
    applier_map = dict(appliers or {})
    requested_streams = [str(stream or "").strip() for stream in (streams or []) if str(stream or "").strip()]
    if requested_streams:
        claim_streams = [stream for stream in requested_streams if stream in applier_map]
        skipped_no_applier = len([stream for stream in requested_streams if stream not in applier_map])
    else:
        claim_streams = sorted(applier_map.keys())
        skipped_no_applier = 0
    if not claim_streams:
        return {
            "ok": True,
            "claimed": 0,
            "applied": 0,
            "failed": 0,
            "skipped_no_applier": skipped_no_applier,
            "streams": {},
        }
    items = target_inbox.claim_pending(
        limit=limit,
        streams=claim_streams,
        include_failed=include_failed,
    )
    applied = 0
    failed = 0
    by_stream: dict[str, dict[str, int]] = {}
    for item in items:
        stream = str(item.get("stream") or "")
        applier = applier_map.get(stream)
        by_stream.setdefault(stream, {"claimed": 0, "applied": 0, "failed": 0})
        by_stream[stream]["claimed"] += 1
        if not callable(applier):
            target_inbox.mark_failed([int(item.get("id") or 0)], f"no state-delta applier registered for {stream}")
            failed += 1
            by_stream[stream]["failed"] += 1
            continue
        try:
            applier(item)
        except Exception as exc:
            target_inbox.mark_failed([int(item.get("id") or 0)], str(exc))
            failed += 1
            by_stream[stream]["failed"] += 1
            continue
        target_inbox.mark_applied([int(item.get("id") or 0)])
        applied += 1
        by_stream[stream]["applied"] += 1
    return {
        "ok": failed == 0,
        "claimed": len(items),
        "applied": applied,
        "failed": failed,
        "skipped_no_applier": skipped_no_applier,
        "streams": by_stream,
    }


def _safe_ids(ids: list[int] | tuple[int, ...] | set[int]) -> list[int]:
    safe_ids: list[int] = []
    for raw_id in ids or []:
        try:
            item_id = int(raw_id or 0)
        except Exception:
            item_id = 0
        if item_id > 0:
            safe_ids.append(item_id)
    return safe_ids


def _inbox_item_from_row(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": int(row[0] or 0),
        "identity_key": str(row[1] or ""),
        "stream": str(row[2] or ""),
        "seq": int(row[3] or 0),
        "ref_id": str(row[4] or ""),
        "kind": str(row[5] or ""),
        "attempts": int(row[6] or 0),
        "change": _loads_json(row[7], {}),
        "entity": _loads_json(row[8], {}),
        "object_refs": _loads_json(row[9], []),
        "first_seen_at": str(row[10] or ""),
        "updated_at": str(row[11] or ""),
        "last_error": str(row[12] or ""),
    }


def _loads_json(value: Any, default: Any) -> Any:
    try:
        loaded = json.loads(str(value or ""))
    except Exception:
        return default
    return loaded if isinstance(loaded, type(default)) else default


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
