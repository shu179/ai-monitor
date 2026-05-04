from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ArticleReferenceEvent,
    BrandTask,
    RunRecord,
    SyncEvent,
    TaskAssignmentEvent,
    User,
)


def build_workspace_event_snapshot(db: Session, user: User) -> dict[str, Any]:
    workspace_id = user.workspace_id
    return {
        "task_updated_at": _datetime_version(
            db.scalar(
                select(func.max(BrandTask.updated_at)).where(
                    BrandTask.workspace_id == workspace_id,
                    BrandTask.deleted_at.is_(None),
                )
            )
        ),
        "task_count": int(
            db.scalar(
                select(func.count(BrandTask.id)).where(
                    BrandTask.workspace_id == workspace_id,
                    BrandTask.deleted_at.is_(None),
                )
            )
            or 0
        ),
        "assignment_event_id": _int_version(
            db.scalar(
                select(func.max(TaskAssignmentEvent.id)).where(TaskAssignmentEvent.workspace_id == workspace_id)
            )
        ),
        "run_record_id": _int_version(
            db.scalar(select(func.max(RunRecord.id)).where(RunRecord.workspace_id == workspace_id))
        ),
        "reference_event_id": _int_version(
            db.scalar(
                select(func.max(ArticleReferenceEvent.id)).where(ArticleReferenceEvent.workspace_id == workspace_id)
            )
        ),
        "sync_event_id": _int_version(
            db.scalar(select(func.max(SyncEvent.id)).where(SyncEvent.workspace_id == workspace_id))
        ),
    }


def diff_workspace_event_names(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    if not previous:
        return []
    events: list[str] = []
    if (
        current.get("task_updated_at") != previous.get("task_updated_at")
        or current.get("task_count") != previous.get("task_count")
    ):
        events.append("task_changed")
    if _int_version(current.get("assignment_event_id")) > _int_version(previous.get("assignment_event_id")):
        events.append("assignment_changed")
    if _int_version(current.get("run_record_id")) > _int_version(previous.get("run_record_id")):
        events.append("run_record_changed")
    if _int_version(current.get("reference_event_id")) > _int_version(previous.get("reference_event_id")):
        events.append("reference_changed")
    if _int_version(current.get("sync_event_id")) > _int_version(previous.get("sync_event_id")) and not events:
        events.append("workspace_changed")
    return events


def event_id_for_snapshot(snapshot: dict[str, Any]) -> str:
    parts = [
        str(snapshot.get("task_updated_at") or "0"),
        str(snapshot.get("task_count") or 0),
        str(snapshot.get("assignment_event_id") or 0),
        str(snapshot.get("run_record_id") or 0),
        str(snapshot.get("reference_event_id") or 0),
        str(snapshot.get("sync_event_id") or 0),
    ]
    return ".".join(parts)


def _datetime_version(value: Any) -> str:
    if not isinstance(value, datetime):
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _int_version(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except Exception:
        return 0
