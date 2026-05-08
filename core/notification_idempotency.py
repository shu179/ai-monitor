from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .app_paths import resolve_app_path
from .local_account_space import account_scoped_path
from .time_utils import local_now, local_today


DEFAULT_DB_PATH = resolve_app_path("user_data/notification_idempotency.sqlite3")


def build_payload_hash(payload: Any) -> str:
    text = json.dumps(_json_safe(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def notification_already_sent(
    *,
    webhook_url: str,
    task_id: str = "",
    task_name: str = "",
    channel: str,
    run_date: str = "",
    round_id: str = "",
    payload_hash: str = "",
) -> bool:
    key = _idempotency_key(
        webhook_url=webhook_url,
        task_id=task_id,
        task_name=task_name,
        channel=channel,
        run_date=run_date,
        round_id=round_id,
        payload_hash=payload_hash,
    )
    if not key:
        return False
    try:
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
                row = conn.execute(
                    "SELECT 1 FROM notification_sends WHERE idempotency_key = ? LIMIT 1",
                    (key,),
                ).fetchone()
                return row is not None
    except Exception as exc:
        print(f"[NotificationIdempotency] 查询通知幂等记录失败: {exc}")
        return False


def record_notification_sent(
    *,
    webhook_url: str,
    task_id: str = "",
    task_name: str = "",
    channel: str,
    run_date: str = "",
    round_id: str = "",
    payload_hash: str = "",
) -> None:
    key = _idempotency_key(
        webhook_url=webhook_url,
        task_id=task_id,
        task_name=task_name,
        channel=channel,
        run_date=run_date,
        round_id=round_id,
        payload_hash=payload_hash,
    )
    if not key:
        return
    try:
        sent_at = local_now().isoformat(timespec="seconds")
        with closing(_connect()) as conn:
            with conn:
                _ensure_schema(conn)
                _prune_old_rows(conn)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO notification_sends (
                        idempotency_key,
                        webhook_hash,
                        task_id,
                        task_name,
                        channel,
                        run_date,
                        round_id,
                        payload_hash,
                        sent_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        _webhook_hash(webhook_url),
                        str(task_id or "").strip(),
                        str(task_name or "").strip(),
                        str(channel or "").strip(),
                        _run_date(run_date),
                        str(round_id or "").strip(),
                        str(payload_hash or "").strip(),
                        sent_at,
                    ),
                )
    except Exception as exc:
        print(f"[NotificationIdempotency] 写入通知幂等记录失败: {exc}")


def _db_path() -> Path:
    return account_scoped_path(
        "user_data/notification_idempotency.sqlite3",
        fallback=DEFAULT_DB_PATH,
    )


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notification_sends (
            idempotency_key TEXT PRIMARY KEY,
            webhook_hash TEXT NOT NULL,
            task_id TEXT NOT NULL DEFAULT '',
            task_name TEXT NOT NULL DEFAULT '',
            channel TEXT NOT NULL,
            run_date TEXT NOT NULL,
            round_id TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            sent_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notification_sends_sent_at ON notification_sends(sent_at)"
    )


def _prune_old_rows(conn: sqlite3.Connection, keep_days: int = 45) -> None:
    cutoff = local_now().timestamp() - max(1, int(keep_days or 45)) * 86400
    conn.execute(
        "DELETE FROM notification_sends WHERE strftime('%s', sent_at) < ?",
        (int(cutoff),),
    )


def _idempotency_key(
    *,
    webhook_url: str,
    task_id: str,
    task_name: str,
    channel: str,
    run_date: str,
    round_id: str,
    payload_hash: str,
) -> str:
    if not str(webhook_url or "").strip() or not str(channel or "").strip():
        return ""
    identity = {
        "webhook_hash": _webhook_hash(webhook_url),
        "task_id": str(task_id or "").strip(),
        "task_name": str(task_name or "").strip(),
        "channel": str(channel or "").strip(),
        "run_date": _run_date(run_date),
        "round_id": str(round_id or "").strip(),
        "payload_hash": str(payload_hash or "").strip(),
    }
    text = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _webhook_hash(webhook_url: str) -> str:
    return hashlib.sha256(str(webhook_url or "").strip().encode("utf-8")).hexdigest()


def _run_date(value: str = "") -> str:
    return str(value or "").strip() or local_today().isoformat()


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(value[key]) for key in sorted(value.keys(), key=str)}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
