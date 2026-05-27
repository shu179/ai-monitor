from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import WorkspaceChangeLog, WorkspaceChangeSequence

CHANGE_NOTIFY_CHANNEL = "workspace_changes"
CHANGE_LOG_RETENTION_DAYS = 60
STREAM_TASKS = "tasks"
STREAM_RUNS = "runs"
STREAM_ARTICLES = "articles"
STREAM_REFERENCES = "references"
STREAM_PROFILE = "profile"
STREAM_AGENT_STATUS = "agent_status"

LEGACY_EVENT_STREAMS = {
    "task_changed": STREAM_TASKS,
    "assignment_changed": STREAM_TASKS,
    "run_record_changed": STREAM_RUNS,
    "task_day_status_changed": STREAM_RUNS,
    "article_changed": STREAM_ARTICLES,
    "reference_changed": STREAM_REFERENCES,
    "workspace_changed": STREAM_TASKS,
}


def compact_change_snapshot(db: Session, workspace_id: int) -> dict[str, int]:
    rows = db.execute(
        select(WorkspaceChangeSequence.stream, WorkspaceChangeSequence.seq)
        .where(WorkspaceChangeSequence.workspace_id == int(workspace_id))
        .order_by(WorkspaceChangeSequence.stream)
    )
    return {str(stream): int(seq or 0) for stream, seq in rows}


def event_id_for_change_snapshot(snapshot: dict[str, int]) -> str:
    if not snapshot:
        return "changes:0"
    parts = [f"{stream}:{int(seq or 0)}" for stream, seq in sorted(snapshot.items())]
    return "changes:" + ",".join(parts)


def diff_change_streams(previous: dict[str, int], current: dict[str, int]) -> list[str]:
    changed: list[str] = []
    for stream, seq in sorted((current or {}).items()):
        if int(seq or 0) > int((previous or {}).get(stream) or 0):
            changed.append(stream)
    return changed


def record_workspace_change(
    db: Session,
    *,
    workspace_id: int,
    stream: str,
    kind: str,
    ref_id: str,
    notify: bool = True,
) -> int:
    safe_workspace_id = int(workspace_id)
    safe_stream = str(stream or "").strip()[:32]
    safe_kind = str(kind or "").strip()[:64]
    safe_ref_id = str(ref_id or "").strip()[:128]
    if not safe_workspace_id or not safe_stream or not safe_kind or not safe_ref_id:
        raise ValueError("workspace_id, stream, kind, and ref_id are required")

    stmt = (
        insert(WorkspaceChangeSequence)
        .values(workspace_id=safe_workspace_id, stream=safe_stream, seq=1, updated_at=func.now())
        .on_conflict_do_update(
            index_elements=["workspace_id", "stream"],
            set_={
                "seq": WorkspaceChangeSequence.seq + 1,
                "updated_at": func.now(),
            },
        )
        .returning(WorkspaceChangeSequence.seq)
    )
    result = db.execute(stmt)
    seq_value: Any = None
    scalar_one_or_none = getattr(result, "scalar_one_or_none", None)
    if callable(scalar_one_or_none):
        seq_value = scalar_one_or_none()
    elif callable(getattr(result, "scalar", None)):
        seq_value = result.scalar()
    seq = _coerce_change_seq(seq_value)
    db.execute(
        insert(WorkspaceChangeLog).values(
            workspace_id=safe_workspace_id,
            stream=safe_stream,
            seq=seq,
            kind=safe_kind,
            ref_id=safe_ref_id,
        )
    )
    if notify:
        _notify_workspace_change(db, workspace_id=safe_workspace_id, stream=safe_stream, seq=seq)
    return seq


def list_workspace_changes(
    db: Session,
    *,
    workspace_id: int,
    stream: str,
    after_seq: int,
    limit: int = 500,
    retention_days: int = CHANGE_LOG_RETENTION_DAYS,
) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(retention_days or CHANGE_LOG_RETENTION_DAYS)))
    rows = db.execute(
        select(
            WorkspaceChangeLog.stream,
            WorkspaceChangeLog.seq,
            WorkspaceChangeLog.kind,
            WorkspaceChangeLog.ref_id,
            WorkspaceChangeLog.created_at,
        )
        .where(
            WorkspaceChangeLog.workspace_id == int(workspace_id),
            WorkspaceChangeLog.stream == str(stream or "").strip(),
            WorkspaceChangeLog.seq > int(after_seq or 0),
            WorkspaceChangeLog.created_at >= cutoff,
        )
        .order_by(WorkspaceChangeLog.seq.asc())
        .limit(max(1, min(int(limit or 500), 1000)))
    )
    return [
        {
            "stream": str(row.stream),
            "seq": int(row.seq or 0),
            "kind": str(row.kind),
            "ref_id": str(row.ref_id),
            "created_at": row.created_at.isoformat() if row.created_at else "",
        }
        for row in rows
    ]


def wait_for_workspace_change_notifications(
    *,
    workspace_id: int,
    timeout_seconds: float,
    database_url: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield NOTIFY payloads for one workspace.

    Payloads are intentionally tiny: only workspace_id, stream, and seq. Business
    data must be fetched via state-delta so pg_notify never approaches its 8KB
    payload limit.
    """
    deadline = time.monotonic() + max(0.1, float(timeout_seconds or 0.1))
    url = database_url or get_settings().database_url
    try:
        with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://"), autocommit=True) as conn:
            conn.execute(f"LISTEN {CHANGE_NOTIFY_CHANNEL}")
            while time.monotonic() < deadline:
                remaining = max(0.0, deadline - time.monotonic())
                for notify in conn.notifies(timeout=remaining, stop_after=1):
                    payload = _decode_notify_payload(notify.payload)
                    if int(payload.get("workspace_id") or 0) == int(workspace_id):
                        yield payload
                    break
                else:
                    break
    except Exception:
        return


def _notify_workspace_change(db: Session, *, workspace_id: int, stream: str, seq: int) -> None:
    payload = json.dumps(
        {
            "workspace_id": int(workspace_id),
            "stream": str(stream),
            "seq": int(seq),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    db.execute(text(f"SELECT pg_notify('{CHANGE_NOTIFY_CHANNEL}', :payload)"), {"payload": payload})


def _decode_notify_payload(payload: str) -> dict[str, Any]:
    try:
        decoded = json.loads(str(payload or "{}"))
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _coerce_change_seq(value: Any) -> int:
    try:
        seq = int(value or 0)
    except Exception:
        return 0
    return max(0, seq)
