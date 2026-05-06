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
    TaskMember,
    TaskAssignmentEvent,
    User,
    UserRole,
)
from app.sync_event_types import (
    EVENT_ASSIGNMENT_CHANGED,
    EVENT_REFERENCE_CHANGED,
    EVENT_RUN_RECORD_CHANGED,
    EVENT_TASK_CHANGED,
    EVENT_TASK_DAY_STATUS,
    EVENT_TASK_DAY_STATUS_CHANGED,
    EVENT_WORKSPACE_CHANGED,
    FULL_TASK_PULL_EVENT_NAMES,
)


def build_workspace_event_snapshot(db: Session, user: User) -> dict[str, Any]:
    workspace_id = user.workspace_id
    visible_task_state = _visible_task_state(db, user)
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
        "task_day_status_event_id": _int_version(
            db.scalar(
                select(func.max(SyncEvent.id)).where(
                    SyncEvent.workspace_id == workspace_id,
                    SyncEvent.event_type == EVENT_TASK_DAY_STATUS,
                )
            )
        ),
        "reference_event_id": _int_version(
            db.scalar(
                select(func.max(ArticleReferenceEvent.id)).where(ArticleReferenceEvent.workspace_id == workspace_id)
            )
        ),
        "sync_event_id": _int_version(
            db.scalar(select(func.max(SyncEvent.id)).where(SyncEvent.workspace_id == workspace_id))
        ),
        "visible_task_count": visible_task_state["count"],
        "visible_task_signature": visible_task_state["signature"],
    }


def diff_workspace_event_names(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    if not previous:
        return []
    events: list[str] = []
    has_visible_signature = "visible_task_signature" in previous and "visible_task_signature" in current
    visible_changed = (
        current.get("visible_task_signature") != previous.get("visible_task_signature")
        or current.get("visible_task_count") != previous.get("visible_task_count")
    )
    assignment_changed = _int_version(current.get("assignment_event_id")) > _int_version(previous.get("assignment_event_id"))
    workspace_tasks_changed = (
        current.get("task_updated_at") != previous.get("task_updated_at")
        or current.get("task_count") != previous.get("task_count")
    )
    if has_visible_signature:
        if visible_changed:
            events.append(EVENT_ASSIGNMENT_CHANGED if assignment_changed and not workspace_tasks_changed else EVENT_TASK_CHANGED)
    else:
        if workspace_tasks_changed:
            events.append(EVENT_TASK_CHANGED)
        if assignment_changed:
            events.append(EVENT_ASSIGNMENT_CHANGED)
    if _int_version(current.get("run_record_id")) > _int_version(previous.get("run_record_id")):
        events.append(EVENT_RUN_RECORD_CHANGED)
    if _int_version(current.get("task_day_status_event_id")) > _int_version(previous.get("task_day_status_event_id")):
        events.append(EVENT_TASK_DAY_STATUS_CHANGED)
    if _int_version(current.get("reference_event_id")) > _int_version(previous.get("reference_event_id")):
        events.append(EVENT_REFERENCE_CHANGED)
    if _int_version(current.get("sync_event_id")) > _int_version(previous.get("sync_event_id")) and not events:
        events.append(EVENT_WORKSPACE_CHANGED)
    return events


def event_id_for_snapshot(snapshot: dict[str, Any]) -> str:
    parts = [
        str(snapshot.get("task_updated_at") or "0"),
        str(snapshot.get("task_count") or 0),
        str(snapshot.get("assignment_event_id") or 0),
        str(snapshot.get("run_record_id") or 0),
        str(snapshot.get("task_day_status_event_id") or 0),
        str(snapshot.get("reference_event_id") or 0),
        str(snapshot.get("sync_event_id") or 0),
        str(snapshot.get("visible_task_count") or 0),
        str(snapshot.get("visible_task_signature") or ""),
    ]
    return ".".join(parts)


def build_sync_changes(
    db: Session,
    user: User,
    *,
    known_snapshot: dict[str, Any] | None,
    task_cursors: dict[int, int] | None,
    task_day_status_cursors: dict[int, int] | None = None,
) -> dict[str, Any]:
    snapshot = build_workspace_event_snapshot(db, user)
    previous = known_snapshot if isinstance(known_snapshot, dict) else {}
    events = diff_workspace_event_names(previous, snapshot)
    run_record_max_ids = _run_record_max_ids_for_visible_tasks(db, user, task_cursors or {})
    changed_run_record_task_ids = [
        task_id
        for task_id, max_id in sorted(run_record_max_ids.items())
        if max_id > _int_version((task_cursors or {}).get(task_id))
    ]
    task_day_status_max_ids = _task_day_status_max_ids_for_visible_tasks(db, user, task_day_status_cursors or {})
    changed_task_day_status_task_ids = [
        task_id
        for task_id, max_id in sorted(task_day_status_max_ids.items())
        if max_id > _int_version((task_day_status_cursors or {}).get(task_id))
    ]
    task_or_assignment_changed = any(event in FULL_TASK_PULL_EVENT_NAMES for event in events)
    return {
        "snapshot": snapshot,
        "event_id": event_id_for_snapshot(snapshot),
        "events": events,
        "full_task_pull_required": bool(not previous or task_or_assignment_changed),
        "run_record_task_ids": changed_run_record_task_ids,
        "run_record_max_ids": run_record_max_ids,
        "task_day_status_task_ids": changed_task_day_status_task_ids,
        "task_day_status_max_ids": task_day_status_max_ids,
        "reference_changed": EVENT_REFERENCE_CHANGED in events,
    }


def _run_record_max_ids_for_visible_tasks(db: Session, user: User, task_cursors: dict[int, int]) -> dict[int, int]:
    task_ids = []
    seen = set()
    for raw_task_id in (task_cursors or {}).keys():
        try:
            task_id = int(raw_task_id)
        except Exception:
            continue
        if task_id <= 0 or task_id in seen:
            continue
        seen.add(task_id)
        task_ids.append(task_id)
        if len(task_ids) >= 200:
            break
    if not task_ids:
        return {}

    visible_ids = _visible_task_ids(db, user, task_ids)
    if not visible_ids:
        return {}
    rows = db.execute(
        select(RunRecord.task_id, func.max(RunRecord.id))
        .where(
            RunRecord.workspace_id == user.workspace_id,
            RunRecord.task_id.in_(visible_ids),
        )
        .group_by(RunRecord.task_id)
    )
    return {int(task_id): _int_version(max_id) for task_id, max_id in rows}


def _task_day_status_max_ids_for_visible_tasks(db: Session, user: User, task_cursors: dict[int, int]) -> dict[int, int]:
    task_ids = []
    seen = set()
    for raw_task_id in (task_cursors or {}).keys():
        try:
            task_id = int(raw_task_id)
        except Exception:
            continue
        if task_id <= 0 or task_id in seen:
            continue
        seen.add(task_id)
        task_ids.append(task_id)
        if len(task_ids) >= 200:
            break
    if not task_ids:
        return {}

    visible_ids = _visible_task_ids(db, user, task_ids)
    if not visible_ids:
        return {}
    task_id_expr = SyncEvent.payload_json["task_id"].as_integer()
    rows = db.execute(
        select(task_id_expr, func.max(SyncEvent.id))
        .where(
            SyncEvent.workspace_id == user.workspace_id,
            SyncEvent.event_type == EVENT_TASK_DAY_STATUS,
            task_id_expr.in_(visible_ids),
        )
        .group_by(task_id_expr)
    )
    return {int(task_id): _int_version(max_id) for task_id, max_id in rows if task_id is not None}


def _visible_task_ids(db: Session, user: User, task_ids: list[int]) -> list[int]:
    if user.role == UserRole.viewer and bool(getattr(user, "view_all_tasks", False)):
        rows = db.scalars(
            select(BrandTask.id).where(
                BrandTask.workspace_id == user.workspace_id,
                BrandTask.id.in_(task_ids),
                BrandTask.deleted_at.is_(None),
            )
        )
        return [int(item) for item in rows]
    retention_clause = (
        (BrandTask.deleted_at.is_(None))
        | (BrandTask.delete_expires_at.is_(None))
        | (BrandTask.delete_expires_at > datetime.now(timezone.utc))
    )
    if user.role == UserRole.admin:
        rows = db.scalars(
            select(BrandTask.id).where(
                BrandTask.workspace_id == user.workspace_id,
                BrandTask.id.in_(task_ids),
                retention_clause,
            )
        )
        return [int(item) for item in rows]
    rows = db.scalars(
        select(BrandTask.id)
        .join(TaskMember, TaskMember.task_id == BrandTask.id)
        .where(
            TaskMember.workspace_id == user.workspace_id,
            TaskMember.user_id == user.id,
            BrandTask.workspace_id == user.workspace_id,
            BrandTask.id.in_(task_ids),
            retention_clause,
        )
    )
    return [int(item) for item in rows]


def _visible_task_state(db: Session, user: User) -> dict[str, Any]:
    if user.role == UserRole.admin:
        rows = db.execute(
            select(BrandTask.id, BrandTask.config_version, BrandTask.enabled, BrandTask.updated_at)
            .where(BrandTask.workspace_id == user.workspace_id, BrandTask.deleted_at.is_(None))
            .order_by(BrandTask.id)
        ).all()
        task_ids = [int(row.id) for row in rows]
        members_by_task: dict[int, list[str]] = {task_id: [] for task_id in task_ids}
        if task_ids:
            member_rows = db.execute(
                select(TaskMember.task_id, TaskMember.user_id, TaskMember.access_level)
                .where(TaskMember.workspace_id == user.workspace_id, TaskMember.task_id.in_(task_ids))
                .order_by(TaskMember.task_id, TaskMember.access_level, TaskMember.user_id)
            ).all()
            for member in member_rows:
                members_by_task.setdefault(int(member.task_id), []).append(
                    f"{member.access_level.value}:{int(member.user_id)}"
                )
        parts = [
            f"{int(row.id)}:{int(row.config_version or 0)}:{int(bool(row.enabled))}:"
            f"{_datetime_version(row.updated_at)}:{','.join(members_by_task.get(int(row.id), []))}"
            for row in rows
        ]
        viewer_all_rows = db.scalars(
            select(User.id)
            .where(
                User.workspace_id == user.workspace_id,
                User.role == UserRole.viewer,
                User.deleted_at.is_(None),
                User.view_all_tasks.is_(True),
            )
            .order_by(User.id)
        )
        viewer_all_parts = [f"viewer_all:{int(user_id)}" for user_id in viewer_all_rows]
        if viewer_all_parts:
            parts.append(f"viewer_scope:{','.join(viewer_all_parts)}")
        return {"count": len(rows), "signature": "|".join(parts)}
    if user.role == UserRole.viewer and bool(getattr(user, "view_all_tasks", False)):
        rows = db.execute(
            select(BrandTask.id, BrandTask.config_version, BrandTask.enabled, BrandTask.updated_at)
            .where(BrandTask.workspace_id == user.workspace_id, BrandTask.deleted_at.is_(None))
            .order_by(BrandTask.id)
        ).all()
        parts = [
            f"all:{len(rows)}"
        ]
        parts.extend(
            f"{int(row.id)}:{int(row.config_version or 0)}:{int(bool(row.enabled))}:"
            f"{_datetime_version(row.updated_at)}:view_all"
            for row in rows
        )
        return {"count": len(parts) - 1, "signature": "|".join(parts)}

    rows = db.execute(
        select(
            BrandTask.id,
            BrandTask.config_version,
            BrandTask.enabled,
            BrandTask.updated_at,
            TaskMember.access_level,
        )
        .join(TaskMember, TaskMember.task_id == BrandTask.id)
        .where(
            TaskMember.workspace_id == user.workspace_id,
            TaskMember.user_id == user.id,
            BrandTask.workspace_id == user.workspace_id,
            BrandTask.deleted_at.is_(None),
        )
        .order_by(BrandTask.id, TaskMember.access_level)
    ).all()
    parts = [
        f"{int(row.id)}:{int(row.config_version or 0)}:{int(bool(row.enabled))}:"
        f"{_datetime_version(row.updated_at)}:{row.access_level.value}"
        for row in rows
    ]
    return {"count": len(parts), "signature": "|".join(parts)}


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
