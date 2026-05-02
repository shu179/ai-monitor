from __future__ import annotations

import hashlib
from collections import defaultdict

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

REFERENCE_ALGORITHM_VERSION = "article_ref_weight_v1"


def can_operate_task(db: Session, user: User, task_id: int) -> bool:
    if user.role == UserRole.admin:
        return bool(db.scalar(select(BrandTask.id).where(BrandTask.id == task_id, BrandTask.workspace_id == user.workspace_id)))
    return bool(
        db.scalar(
            select(TaskMember.id).where(
                TaskMember.workspace_id == user.workspace_id,
                TaskMember.task_id == task_id,
                TaskMember.user_id == user.id,
                TaskMember.access_level == TaskAccessLevel.operate,
            )
        )
    )


def can_view_task(db: Session, user: User, task_id: int) -> bool:
    if user.role == UserRole.admin:
        return bool(db.scalar(select(BrandTask.id).where(BrandTask.id == task_id, BrandTask.workspace_id == user.workspace_id)))
    return bool(
        db.scalar(
            select(TaskMember.id).where(
                TaskMember.workspace_id == user.workspace_id,
                TaskMember.task_id == task_id,
                TaskMember.user_id == user.id,
            )
        )
    )


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
    if not task_id or not can_operate_task(db, user, task_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No operate access to task")
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
            result_json=payload.get("result") if isinstance(payload.get("result"), dict) else payload,
            idempotency_key=event.idempotency_key,
            executed_at=payload.get("executed_at") or None,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
    )
    db.execute(stmt)


def _materialize_article_reference_event(db: Session, user: User, event: SyncEventIn) -> None:
    payload = event.payload
    task_id = int(payload.get("task_id") or 0)
    if not task_id or not can_operate_task(db, user, task_id):
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

