from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import (
    ArticleReferenceEvent,
    BrandTask,
    RunRecord,
    SyncEvent,
    TaskAccessLevel,
    TaskMember,
    User,
    UserRole,
)
from app.schemas import SyncEventIn
from app.core.security import utc_now

REFERENCE_ALGORITHM_VERSION = "article_ref_weight_v1"
RUN_RESULT_ALLOWED_KEYS = {
    "rank",
    "success",
    "review_status",
    "highlight_count",
    "reference_count",
    "body_reference_count",
    "total_reference_count",
    "error_message",
    "diagnostic_id",
}
RUN_RESULT_TEXT_LIMITS = {
    "error_message": 500,
    "diagnostic_id": 128,
    "review_status": 64,
}


def can_operate_task(db: Session, user: User, task_id: int, *, run_started_at: object = None) -> bool:
    if user.role == UserRole.admin:
        task = db.scalar(
            select(BrandTask).where(
                BrandTask.id == task_id,
                BrandTask.workspace_id == user.workspace_id,
            )
        )
        return bool(task and _task_allows_operation(task, run_started_at=run_started_at))

    task = db.scalar(
        select(BrandTask)
        .join(TaskMember, BrandTask.id == TaskMember.task_id)
        .where(
            TaskMember.workspace_id == user.workspace_id,
            TaskMember.task_id == task_id,
            TaskMember.user_id == user.id,
            TaskMember.access_level == TaskAccessLevel.operate,
            BrandTask.workspace_id == user.workspace_id,
        )
    )
    return bool(task and _task_allows_operation(task, run_started_at=run_started_at))


def _task_allows_operation(task: BrandTask, *, run_started_at: object = None) -> bool:
    if task.deleted_at is None:
        return True
    now = utc_now()
    if task.delete_expires_at is not None and _aware_datetime(task.delete_expires_at) <= now:
        return False
    started_at = _parse_payload_datetime(run_started_at)
    if started_at is None:
        return False
    return started_at <= _aware_datetime(task.deleted_at)


def _parse_payload_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return _aware_datetime(parsed)


def _aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def can_view_task(db: Session, user: User, task_id: int) -> bool:
    retention_clause = (
        (BrandTask.deleted_at.is_(None))
        | (BrandTask.delete_expires_at.is_(None))
        | (BrandTask.delete_expires_at > utc_now())
    )
    if user.role == UserRole.admin:
        return bool(
            db.scalar(
                select(BrandTask.id).where(
                    BrandTask.id == task_id,
                    BrandTask.workspace_id == user.workspace_id,
                    retention_clause,
                )
            )
        )
    return bool(
        db.scalar(
            select(TaskMember.id)
            .join(BrandTask, BrandTask.id == TaskMember.task_id)
            .where(
                TaskMember.workspace_id == user.workspace_id,
                TaskMember.task_id == task_id,
                TaskMember.user_id == user.id,
                retention_clause,
            )
        )
    )


def list_visible_tasks(db: Session, user: User) -> list[dict]:
    if user.role == UserRole.admin:
        tasks = db.scalars(
            select(BrandTask)
            .where(BrandTask.workspace_id == user.workspace_id, BrandTask.deleted_at.is_(None))
            .order_by(BrandTask.id)
        )
        return [_task_to_visible_payload(task, "admin") for task in tasks]

    rows = db.execute(
        select(BrandTask, TaskMember.access_level)
        .join(TaskMember, TaskMember.task_id == BrandTask.id)
        .where(
            TaskMember.workspace_id == user.workspace_id,
            TaskMember.user_id == user.id,
            BrandTask.workspace_id == user.workspace_id,
            BrandTask.deleted_at.is_(None),
        )
        .order_by(BrandTask.id)
    )
    return [_task_to_visible_payload(task, access_level.value) for task, access_level in rows]


def list_deleted_visible_tasks(db: Session, user: User) -> list[dict]:
    if user.role == UserRole.admin:
        tasks = db.scalars(
            select(BrandTask)
            .where(
                BrandTask.workspace_id == user.workspace_id,
                BrandTask.deleted_at.is_not(None),
                (BrandTask.delete_expires_at.is_(None)) | (BrandTask.delete_expires_at > utc_now()),
            )
            .order_by(BrandTask.deleted_at.desc(), BrandTask.id.desc())
        )
        return [_task_to_visible_payload(task, "admin") for task in tasks]

    rows = db.execute(
        select(BrandTask, TaskMember.access_level)
        .join(TaskMember, TaskMember.task_id == BrandTask.id)
        .where(
            TaskMember.workspace_id == user.workspace_id,
            TaskMember.user_id == user.id,
            BrandTask.workspace_id == user.workspace_id,
            BrandTask.deleted_at.is_not(None),
            (BrandTask.delete_expires_at.is_(None)) | (BrandTask.delete_expires_at > utc_now()),
        )
        .order_by(BrandTask.deleted_at.desc(), BrandTask.id.desc())
    )
    return [_task_to_visible_payload(task, access_level.value) for task, access_level in rows]


def _task_to_visible_payload(task: BrandTask, access_level: str) -> dict:
    return {
        "id": task.id,
        "workspace_id": task.workspace_id,
        "task_key": task.task_key,
        "name": task.name,
        "brand": task.brand,
        "config_json": task.config_json or {},
        "config_version": task.config_version,
        "enabled": task.enabled,
        "deleted_at": task.deleted_at,
        "delete_expires_at": task.delete_expires_at,
        "created_at": task.created_at,
        "access_level": access_level,
    }


def accept_sync_events(db: Session, user: User, events: list[SyncEventIn]) -> tuple[int, int]:
    accepted = 0
    duplicates = 0
    for event in events:
        stmt = (
            insert(SyncEvent)
            .values(
                workspace_id=user.workspace_id,
                user_id=user.id,
                event_type=event.event_type,
                idempotency_key=event.idempotency_key,
                payload_json=event.payload,
            )
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(SyncEvent.id)
        )
        inserted_id = db.scalar(stmt)
        if inserted_id is None:
            duplicates += 1
            continue
        accepted += 1
        _materialize_known_event(db, user, event)
    db.commit()
    return accepted, duplicates


def _materialize_known_event(db: Session, user: User, event: SyncEventIn) -> None:
    if event.event_type == "run_record":
        _materialize_run_record(db, user, event)
    elif event.event_type == "article_reference_event":
        _materialize_article_reference_event(db, user, event)


def _materialize_run_record(db: Session, user: User, event: SyncEventIn) -> None:
    payload = event.payload
    task_id = int(payload.get("task_id") or 0)
    if not task_id or not can_operate_task(db, user, task_id, run_started_at=payload.get("run_started_at")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No operate access to task")
    result_payload = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    stmt = (
        insert(RunRecord)
        .values(
            workspace_id=user.workspace_id,
            task_id=task_id,
            executed_by=user.id,
            platform=str(payload.get("platform") or ""),
            keyword=str(payload.get("keyword") or ""),
            brand=str(payload.get("brand") or ""),
            mode=str(payload.get("mode") or "browser"),
            result_json=_sanitize_run_result(result_payload),
            idempotency_key=event.idempotency_key,
            executed_at=payload.get("executed_at") or None,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
    )
    db.execute(stmt)


def _materialize_article_reference_event(db: Session, user: User, event: SyncEventIn) -> None:
    payload = event.payload
    task_id = int(payload.get("task_id") or 0)
    if not task_id or not can_operate_task(db, user, task_id, run_started_at=payload.get("run_started_at")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No operate access to task")
    normalized_url = str(payload.get("normalized_url") or payload.get("url") or "").strip()
    if not normalized_url:
        return
    url_hash = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
    stmt = (
        insert(ArticleReferenceEvent)
        .values(
            workspace_id=user.workspace_id,
            task_id=task_id,
            normalized_url=normalized_url,
            url_hash=url_hash,
            platform=str(payload.get("platform") or "unknown"),
            record_day=str(payload.get("record_day") or "")[:10],
            source_record_key=str(payload.get("source_record_key") or event.idempotency_key),
            idempotency_key=event.idempotency_key,
            event_json=payload,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
    )
    db.execute(stmt)


def build_reference_ranking(db: Session, user: User, task_id: int) -> list[dict]:
    if not can_view_task(db, user, task_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No view access to task")

    rows = db.execute(
        select(
            ArticleReferenceEvent.normalized_url,
            func.count(ArticleReferenceEvent.id).label("events"),
            func.count(func.distinct(ArticleReferenceEvent.platform)).label("platform_count"),
            func.count(func.distinct(ArticleReferenceEvent.record_day)).label("day_count"),
            func.array_agg(func.distinct(ArticleReferenceEvent.platform)).label("platforms"),
        )
        .where(
            ArticleReferenceEvent.workspace_id == user.workspace_id,
            ArticleReferenceEvent.task_id == task_id,
        )
        .group_by(ArticleReferenceEvent.normalized_url)
        .order_by(func.count(ArticleReferenceEvent.id).desc())
        .limit(100)
    )
    items = []
    for row in rows:
        events = int(row.events or 0)
        platform_count = int(row.platform_count or 0)
        day_count = int(row.day_count or 0)
        score = float(events + platform_count * 2 + day_count)
        items.append(
            {
                "url": row.normalized_url,
                "score": score,
                "platforms": sorted([item for item in (row.platforms or []) if item]),
                "days": day_count,
                "events": events,
            }
        )
    return items


def list_task_run_records(db: Session, user: User, task_id: int, *, limit: int = 50, since_id: int | None = None) -> list[RunRecord]:
    if not can_view_task(db, user, task_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No view access to task")
    safe_limit = min(max(int(limit or 50), 1), 10000)
    safe_since_id = max(int(since_id or 0), 0)
    stmt = select(RunRecord).where(
        RunRecord.workspace_id == user.workspace_id,
        RunRecord.task_id == task_id,
    )
    if safe_since_id > 0:
        stmt = stmt.where(RunRecord.id > safe_since_id).order_by(RunRecord.id.asc())
    else:
        stmt = stmt.order_by(RunRecord.executed_at.desc(), RunRecord.id.desc())
    return list(
        db.scalars(
            stmt.limit(safe_limit)
        )
    )


def _sanitize_run_result(result: dict) -> dict:
    sanitized = {key: result.get(key) for key in RUN_RESULT_ALLOWED_KEYS if key in result}
    for key, limit in RUN_RESULT_TEXT_LIMITS.items():
        if key in sanitized:
            sanitized[key] = str(sanitized.get(key) or "").strip()[:limit]
    for key in ("rank", "highlight_count", "reference_count", "body_reference_count", "total_reference_count"):
        if key in sanitized:
            try:
                sanitized[key] = int(sanitized.get(key) or 0)
            except Exception:
                sanitized[key] = 0
    if "success" in sanitized:
        sanitized["success"] = bool(sanitized.get("success"))
    return sanitized
