from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import (
    Article,
    ArticleReferenceEvent,
    ArticleTaskLink,
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
from app.services.article_classification_service import sync_article_classification_state
from app.services.change_log_service import (
    STREAM_ARTICLES,
    STREAM_PROFILE,
    STREAM_REFERENCES,
    STREAM_RUNS,
    record_workspace_change,
)
from app.sync_event_types import (
    EVENT_ARTICLE_REFERENCE,
    EVENT_ARTICLE_TASK_LINKS,
    EVENT_ARTICLE_UPSERT,
    EVENT_PROFILE_UPDATE,
    EVENT_RUN_RECORD,
    EVENT_TASK_DAY_STATUS,
)

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
TASK_DAY_STATUS_TEXT_LIMITS = {
    "source": 64,
    "message": 256,
    "updated_at": 64,
    "run_started_at": 64,
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


def _datetime_to_iso(value: datetime | None) -> str:
    if value is None:
        return ""
    return _aware_datetime(value).isoformat()


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
    if user.role == UserRole.viewer and bool(getattr(user, "view_all_tasks", False)):
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


def list_visible_tasks(db: Session, user: User, task_ids: list[int] | None = None) -> list[dict]:
    scoped_task_ids = _normalize_requested_task_ids(task_ids)
    id_clause = BrandTask.id.in_(scoped_task_ids) if scoped_task_ids else True
    if user.role == UserRole.admin:
        tasks = db.scalars(
            select(BrandTask)
            .where(BrandTask.workspace_id == user.workspace_id, BrandTask.deleted_at.is_(None), id_clause)
            .order_by(BrandTask.id)
        )
        return [_task_to_visible_payload(task, "admin") for task in tasks]
    if user.role == UserRole.viewer and bool(getattr(user, "view_all_tasks", False)):
        tasks = db.scalars(
            select(BrandTask)
            .where(BrandTask.workspace_id == user.workspace_id, BrandTask.deleted_at.is_(None), id_clause)
            .order_by(BrandTask.id)
        )
        return [_task_to_visible_payload(task, TaskAccessLevel.view.value) for task in tasks]

    rows = db.execute(
        select(BrandTask, TaskMember.access_level)
        .join(TaskMember, TaskMember.task_id == BrandTask.id)
        .where(
            TaskMember.workspace_id == user.workspace_id,
            TaskMember.user_id == user.id,
            BrandTask.workspace_id == user.workspace_id,
            id_clause,
            BrandTask.deleted_at.is_(None),
        )
        .order_by(BrandTask.id)
    )
    return [_task_to_visible_payload(task, access_level.value) for task, access_level in rows]


def _normalize_requested_task_ids(task_ids: list[int] | None) -> list[int]:
    normalized: list[int] = []
    seen: set[int] = set()
    for raw_task_id in task_ids or []:
        try:
            task_id = int(raw_task_id)
        except Exception:
            continue
        if task_id <= 0 or task_id in seen:
            continue
        seen.add(task_id)
        normalized.append(task_id)
        if len(normalized) >= 200:
            break
    return normalized


def _visible_task_ids_for_user(db: Session, user: User) -> list[int]:
    retention_clause = (
        (BrandTask.deleted_at.is_(None))
        | (BrandTask.delete_expires_at.is_(None))
        | (BrandTask.delete_expires_at > utc_now())
    )
    if user.role == UserRole.admin or (user.role == UserRole.viewer and bool(getattr(user, "view_all_tasks", False))):
        rows = db.scalars(
            select(BrandTask.id).where(
                BrandTask.workspace_id == user.workspace_id,
                retention_clause,
            )
        )
        return [int(item) for item in rows]

    rows = db.scalars(
        select(TaskMember.task_id)
        .join(BrandTask, BrandTask.id == TaskMember.task_id)
        .where(
            TaskMember.workspace_id == user.workspace_id,
            TaskMember.user_id == user.id,
            BrandTask.workspace_id == user.workspace_id,
            retention_clause,
        )
    )
    return [int(item) for item in rows]


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
    if user.role == UserRole.viewer and bool(getattr(user, "view_all_tasks", False)):
        tasks = db.scalars(
            select(BrandTask)
            .where(
                BrandTask.workspace_id == user.workspace_id,
                BrandTask.deleted_at.is_not(None),
                (BrandTask.delete_expires_at.is_(None)) | (BrandTask.delete_expires_at > utc_now()),
            )
            .order_by(BrandTask.deleted_at.desc(), BrandTask.id.desc())
        )
        return [_task_to_visible_payload(task, TaskAccessLevel.view.value) for task in tasks]

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


def list_visible_articles(
    db: Session,
    user: User,
    *,
    limit: int = 5000,
    updated_after: str = "",
    updated_after_id: int = 0,
) -> dict[str, Any]:
    visible_task_ids = _visible_task_ids_for_user(db, user)
    if not visible_task_ids:
        return {"articles": [], "count": 0, "max_updated_at": "", "max_article_id": 0}

    safe_limit = min(max(int(limit or 5000), 1), 10000)
    updated_after_dt = _parse_payload_datetime(updated_after)
    article_stmt = (
        select(Article)
        .join(ArticleTaskLink, ArticleTaskLink.article_id == Article.id)
        .where(
            Article.workspace_id == user.workspace_id,
            ArticleTaskLink.workspace_id == user.workspace_id,
            ArticleTaskLink.task_id.in_(visible_task_ids),
        )
        .distinct()
        .order_by(Article.updated_at.asc(), Article.id.asc())
        .limit(safe_limit)
    )
    if updated_after_dt is not None:
        safe_updated_after_id = max(int(updated_after_id or 0), 0)
        article_stmt = article_stmt.where(
            or_(
                Article.updated_at > updated_after_dt,
                and_(Article.updated_at == updated_after_dt, Article.id > safe_updated_after_id),
            )
        )
    articles = list(db.scalars(article_stmt))
    if not articles:
        return {"articles": [], "count": 0, "max_updated_at": "", "max_article_id": 0}

    article_ids = [int(article.id) for article in articles]
    link_rows = db.scalars(
        select(ArticleTaskLink)
        .where(
            ArticleTaskLink.workspace_id == user.workspace_id,
            ArticleTaskLink.article_id.in_(article_ids),
            ArticleTaskLink.task_id.in_(visible_task_ids),
        )
        .order_by(ArticleTaskLink.article_id.asc(), ArticleTaskLink.task_id.asc())
    )
    links_by_article_id: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for link in link_rows:
        links_by_article_id[int(link.article_id)].append({
            "task_id": int(link.task_id),
            "source": link.source,
            "confidence": int(link.confidence or 0),
            "reason_json": link.reason_json or {},
            "created_at": link.created_at,
        })

    items: list[dict[str, Any]] = []
    max_updated_at = ""
    max_article_id = 0
    for article in articles:
        updated_at_text = _datetime_to_iso(article.updated_at)
        if updated_at_text > max_updated_at or (updated_at_text == max_updated_at and int(article.id) > max_article_id):
            max_updated_at = updated_at_text
            max_article_id = int(article.id)
        items.append({
            "id": int(article.id),
            "workspace_id": int(article.workspace_id),
            "canonical_url": article.canonical_url,
            "url_hash": article.url_hash,
            "title": article.title,
            "source": article.source,
            "media_type": article.media_type,
            "published_at": article.published_at,
            "payload_json": article.payload_json or {},
            "created_at": article.created_at,
            "updated_at": article.updated_at,
            "task_links": links_by_article_id.get(int(article.id), []),
        })
    return {"articles": items, "count": len(items), "max_updated_at": max_updated_at, "max_article_id": max_article_id}


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
    from app.services.sync_v2_service import accept_legacy_sync_events_as_batch

    return accept_legacy_sync_events_as_batch(db, user, events)


def _materialize_known_event(db: Session, user: User, event: SyncEventIn) -> None:
    if event.event_type == EVENT_RUN_RECORD:
        _materialize_run_record(db, user, event)
        _record_materialized_change(db, user, stream=STREAM_RUNS, kind="run.record", ref_id=event.idempotency_key)
    elif event.event_type == EVENT_ARTICLE_UPSERT:
        _materialize_article_upsert(db, user, event)
        _record_materialized_change(db, user, stream=STREAM_ARTICLES, kind="article.upsert", ref_id=event.idempotency_key)
    elif event.event_type == EVENT_ARTICLE_TASK_LINKS:
        _materialize_article_task_links(db, user, event)
        _record_materialized_change(db, user, stream=STREAM_ARTICLES, kind="article.link", ref_id=event.idempotency_key)
    elif event.event_type == EVENT_ARTICLE_REFERENCE:
        _materialize_article_reference_event(db, user, event)
        _record_materialized_change(db, user, stream=STREAM_REFERENCES, kind="article.reference", ref_id=event.idempotency_key)
    elif event.event_type == EVENT_PROFILE_UPDATE:
        _materialize_profile_update(user, event)
        _record_materialized_change(db, user, stream=STREAM_PROFILE, kind="profile.update", ref_id=event.idempotency_key)
    elif event.event_type == EVENT_TASK_DAY_STATUS:
        _validate_task_day_status_event(db, user, event)
        _record_materialized_change(db, user, stream=STREAM_RUNS, kind="task.day_status", ref_id=event.idempotency_key)


def _record_materialized_change(db: Session, user: User, *, stream: str, kind: str, ref_id: str) -> None:
    record_workspace_change(
        db,
        workspace_id=user.workspace_id,
        stream=stream,
        kind=kind,
        ref_id=ref_id,
    )


def _materialize_profile_update(user: User, event: SyncEventIn) -> None:
    payload = event.payload if isinstance(event.payload, dict) else {}
    if "avatar" in payload:
        avatar = str(payload.get("avatar") or "").strip()
        user.avatar = avatar[:1_000_000] or None
    if user.role == UserRole.admin:
        if "display_name" in payload:
            display_name = str(payload.get("display_name") or "").strip()
            user.display_name = display_name[:128] or None
        if "birthday" in payload:
            user.birthday = _parse_profile_date(payload.get("birthday"))
        if "hire_date" in payload:
            user.hire_date = _parse_profile_date(payload.get("hire_date"))
    user.updated_at = utc_now()


def _parse_profile_date(value: object) -> date | None:
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _validate_task_day_status_event(db: Session, user: User, event: SyncEventIn) -> None:
    payload = event.payload if isinstance(event.payload, dict) else {}
    task_id = int(payload.get("task_id") or 0)
    if not task_id or not can_operate_task(db, user, task_id, run_started_at=payload.get("updated_at") or payload.get("run_started_at")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No operate access to task")


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
        .on_conflict_do_nothing(index_elements=["workspace_id", "idempotency_key"])
    )
    db.execute(stmt)


def _materialize_article_upsert(db: Session, user: User, event: SyncEventIn) -> None:
    payload = event.payload if isinstance(event.payload, dict) else {}
    canonical_url, url_hash = _article_identity_from_payload(payload)
    if not canonical_url or not url_hash:
        return

    now = utc_now()
    article_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    stmt = (
        insert(Article)
        .values(
            workspace_id=user.workspace_id,
            canonical_url=canonical_url,
            url_hash=url_hash,
            title=str(payload.get("title") or "").strip()[:512] or None,
            source=str(payload.get("source") or "").strip()[:128] or None,
            media_type=_normalize_article_media_type(payload.get("media_type")),
            published_at=_parse_payload_datetime(payload.get("published_at")),
            payload_json=_sanitize_article_payload_json(article_payload),
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=["workspace_id", "url_hash"],
            set_={
                "canonical_url": canonical_url,
                "title": str(payload.get("title") or "").strip()[:512] or None,
                "source": str(payload.get("source") or "").strip()[:128] or None,
                "media_type": _normalize_article_media_type(payload.get("media_type")),
                "published_at": _parse_payload_datetime(payload.get("published_at")),
                "payload_json": _sanitize_article_payload_json(article_payload),
                "updated_at": now,
            },
        )
        .returning(Article.id)
    )
    article_id = db.scalar(stmt)
    if article_id:
        db.execute(
            update(ArticleReferenceEvent)
            .where(
                ArticleReferenceEvent.workspace_id == user.workspace_id,
                ArticleReferenceEvent.url_hash == url_hash,
                ArticleReferenceEvent.article_id.is_(None),
            )
            .values(article_id=article_id)
        )
        sync_article_classification_state(
            db,
            workspace_id=user.workspace_id,
            article_id=int(article_id),
            actor_user_id=user.id,
            reason={
                "source": "article_upsert",
                "sync_event": event.idempotency_key,
                "uploaded_by": user.id,
                "local_article_id": str(payload.get("local_article_id") or "").strip()[:128],
            },
        )


def _materialize_article_task_links(db: Session, user: User, event: SyncEventIn) -> None:
    payload = event.payload if isinstance(event.payload, dict) else {}
    canonical_url, url_hash = _article_identity_from_payload(payload)
    if not canonical_url or not url_hash:
        return

    article_id = _ensure_cloud_article(db, user, canonical_url=canonical_url, url_hash=url_hash)
    if not article_id:
        return

    requested_task_ids = _safe_int_list(payload.get("task_ids") or payload.get("taskIds"), max_items=200)
    allowed_task_ids = [
        task_id
        for task_id in requested_task_ids
        if can_operate_task(db, user, task_id)
    ]
    allowed_task_set = set(allowed_task_ids)
    partial = bool(payload.get("partial"))
    replace = bool(payload.get("replace", True))
    if replace and not partial:
        scope_task_ids = _operable_task_ids_for_user(db, user)
        delete_stmt = (
            ArticleTaskLink.__table__.delete()
            .where(
                ArticleTaskLink.workspace_id == user.workspace_id,
                ArticleTaskLink.article_id == article_id,
                ArticleTaskLink.task_id.in_(scope_task_ids or [-1]),
            )
        )
        if allowed_task_set:
            delete_stmt = delete_stmt.where(ArticleTaskLink.task_id.notin_(allowed_task_set))
        db.execute(delete_stmt)

    source = str(payload.get("source") or "local_rule").strip()[:32] or "local_rule"
    confidence = _clamp_int(payload.get("confidence"), default=100, min_value=0, max_value=100)
    reason_json = payload.get("reason_json") if isinstance(payload.get("reason_json"), dict) else {}
    for task_id in allowed_task_ids:
        task_reason = reason_json.get(str(task_id)) if isinstance(reason_json.get(str(task_id)), list) else reason_json
        stmt = (
            insert(ArticleTaskLink)
            .values(
                workspace_id=user.workspace_id,
                article_id=article_id,
                task_id=task_id,
                source=source,
                confidence=confidence,
                reason_json=_sanitize_article_reason_json(task_reason),
                confirmed_by=user.id,
            )
            .on_conflict_do_update(
                index_elements=["article_id", "task_id"],
                set_={
                    "source": source,
                    "confidence": confidence,
                    "reason_json": _sanitize_article_reason_json(task_reason),
                    "confirmed_by": user.id,
                },
            )
        )
        db.execute(stmt)
    db.execute(
        update(Article)
        .where(
            Article.workspace_id == user.workspace_id,
            Article.id == article_id,
        )
        .values(updated_at=utc_now())
    )
    sync_article_classification_state(
        db,
        workspace_id=user.workspace_id,
        article_id=int(article_id),
        actor_user_id=user.id,
        reason={
            "source": "article_task_links",
            "sync_event": event.idempotency_key,
            "partial": partial,
            "linked_task_ids": allowed_task_ids[:50],
            "unresolved_task_names": payload.get("unresolved_task_names") or [],
        },
    )


def _materialize_article_reference_event(db: Session, user: User, event: SyncEventIn) -> None:
    payload = event.payload
    task_id = int(payload.get("task_id") or 0)
    if not task_id or not can_operate_task(db, user, task_id, run_started_at=payload.get("run_started_at")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No operate access to task")
    normalized_url = str(payload.get("normalized_url") or payload.get("url") or "").strip()
    if not normalized_url:
        return
    url_hash = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
    article_id = db.scalar(
        select(Article.id).where(
            Article.workspace_id == user.workspace_id,
            Article.url_hash == url_hash,
        )
    )
    stmt = (
        insert(ArticleReferenceEvent)
        .values(
            workspace_id=user.workspace_id,
            task_id=task_id,
            article_id=article_id,
            normalized_url=normalized_url,
            url_hash=url_hash,
            platform=str(payload.get("platform") or "unknown"),
            record_day=str(payload.get("record_day") or "")[:10],
            source_record_key=str(payload.get("source_record_key") or event.idempotency_key),
            idempotency_key=event.idempotency_key,
            event_json=payload,
        )
        .on_conflict_do_nothing(index_elements=["workspace_id", "idempotency_key"])
    )
    db.execute(stmt)


def _article_identity_from_payload(payload: dict) -> tuple[str, str]:
    canonical_url = str(payload.get("canonical_url") or payload.get("url") or "").strip()
    url_hash = str(payload.get("url_hash") or "").strip()
    if canonical_url and not url_hash:
        url_hash = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()
    if url_hash and len(url_hash) != 64:
        url_hash = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest() if canonical_url else ""
    return canonical_url, url_hash


def _ensure_cloud_article(db: Session, user: User, *, canonical_url: str, url_hash: str) -> int | None:
    existing_id = db.scalar(
        select(Article.id).where(
            Article.workspace_id == user.workspace_id,
            Article.url_hash == url_hash,
        )
    )
    if existing_id:
        return int(existing_id)
    stmt = (
        insert(Article)
        .values(
            workspace_id=user.workspace_id,
            canonical_url=canonical_url,
            url_hash=url_hash,
            payload_json={},
            updated_at=utc_now(),
        )
        .on_conflict_do_update(
            index_elements=["workspace_id", "url_hash"],
            set_={"canonical_url": canonical_url, "updated_at": utc_now()},
        )
        .returning(Article.id)
    )
    inserted_id = db.scalar(stmt)
    return int(inserted_id) if inserted_id else None


def _normalize_article_media_type(value: object) -> str | None:
    text = str(value or "").strip().lower().replace("-", "")
    if text in {"authority", "media", "official"}:
        return "authority"
    if text in {"selfmedia", "self", "ugc"}:
        return "selfmedia"
    return None


def _sanitize_article_payload_json(payload: dict) -> dict:
    source = payload if isinstance(payload, dict) else {}
    clean: dict[str, Any] = {}
    text_limits = {
        "local_article_id": 128,
        "platform": 128,
        "media_name": 128,
        "account_name": 128,
        "account_url": 512,
        "excerpt": 500,
        "imported_at": 64,
        "fetch_method": 64,
        "import_status": 32,
        "import_confirmed_at": 64,
    }
    for key, limit in text_limits.items():
        if key not in source:
            continue
        text = str(source.get(key) or "").strip()[:limit]
        if text:
            clean[key] = text
    return clean


def _sanitize_article_reason_json(value: object) -> dict:
    if isinstance(value, list):
        reasons = _safe_text_list(value, limit=256, max_items=5)
        return {"reasons": reasons} if reasons else {}
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, raw in value.items():
            key_text = str(key or "").strip()[:64]
            if not key_text:
                continue
            if isinstance(raw, list):
                values = _safe_text_list(raw, limit=256, max_items=5)
                if values:
                    clean[key_text] = values
            else:
                text = str(raw or "").strip()[:256]
                if text:
                    clean[key_text] = text
            if len(clean) >= 50:
                break
        return clean
    return {}


def _safe_int_list(value: object, *, max_items: int = 200) -> list[int]:
    source = value if isinstance(value, list) else []
    result: list[int] = []
    seen: set[int] = set()
    for item in source:
        try:
            number = int(item)
        except Exception:
            continue
        if number <= 0 or number in seen:
            continue
        seen.add(number)
        result.append(number)
        if len(result) >= max_items:
            break
    return result


def _clamp_int(value: object, *, default: int, min_value: int, max_value: int) -> int:
    try:
        number = int(value)
    except Exception:
        number = default
    return min(max(number, min_value), max_value)


def _operable_task_ids_for_user(db: Session, user: User) -> list[int]:
    if user.role == UserRole.admin:
        rows = db.scalars(
            select(BrandTask.id).where(
                BrandTask.workspace_id == user.workspace_id,
                BrandTask.deleted_at.is_(None),
            )
        )
        return [int(item) for item in rows]
    if user.role != UserRole.operator:
        return []
    rows = db.scalars(
        select(TaskMember.task_id)
        .join(BrandTask, BrandTask.id == TaskMember.task_id)
        .where(
            TaskMember.workspace_id == user.workspace_id,
            TaskMember.user_id == user.id,
            TaskMember.access_level == TaskAccessLevel.operate,
            BrandTask.workspace_id == user.workspace_id,
            BrandTask.deleted_at.is_(None),
        )
    )
    return [int(item) for item in rows]


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
    return _list_task_run_records_unchecked(db, user, task_id, limit=limit, since_id=since_id)


def list_task_run_records_batch(
    db: Session,
    user: User,
    task_cursors: dict[int, int],
    *,
    limit_per_task: int = 6000,
) -> dict[int, list[RunRecord]]:
    safe_limit = min(max(int(limit_per_task or 6000), 1), 10000)
    results: dict[int, list[RunRecord]] = {}
    seen_task_ids: set[int] = set()
    for raw_task_id, raw_cursor in (task_cursors or {}).items():
        try:
            task_id = int(raw_task_id)
        except Exception:
            continue
        if task_id <= 0 or task_id in seen_task_ids:
            continue
        seen_task_ids.add(task_id)
        if len(seen_task_ids) > 200:
            break
        if not can_view_task(db, user, task_id):
            continue
        try:
            since_id = int(raw_cursor or 0)
        except Exception:
            since_id = 0
        results[task_id] = _list_task_run_records_unchecked(
            db,
            user,
            task_id,
            limit=safe_limit,
            since_id=since_id,
        )
    return results


def _list_task_run_records_unchecked(
    db: Session,
    user: User,
    task_id: int,
    *,
    limit: int = 50,
    since_id: int | None = None,
) -> list[RunRecord]:
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


def list_task_day_status_events_batch(
    db: Session,
    user: User,
    task_cursors: dict[int, int],
    *,
    limit_per_task: int = 500,
) -> dict[int, list[dict]]:
    safe_limit = min(max(int(limit_per_task or 500), 1), 2000)
    results: dict[int, list[dict]] = {}
    seen_task_ids: set[int] = set()
    for raw_task_id, raw_cursor in (task_cursors or {}).items():
        try:
            task_id = int(raw_task_id)
        except Exception:
            continue
        if task_id <= 0 or task_id in seen_task_ids:
            continue
        seen_task_ids.add(task_id)
        if len(seen_task_ids) > 200:
            break
        if not can_view_task(db, user, task_id):
            continue
        try:
            since_id = int(raw_cursor or 0)
        except Exception:
            since_id = 0
        stmt = (
            select(SyncEvent)
            .where(
                SyncEvent.workspace_id == user.workspace_id,
                SyncEvent.event_type == EVENT_TASK_DAY_STATUS,
                SyncEvent.id > max(since_id, 0),
                SyncEvent.payload_json["task_id"].as_integer() == task_id,
            )
            .order_by(SyncEvent.id.asc())
            .limit(safe_limit)
        )
        results[task_id] = [_task_day_status_event_payload(item) for item in db.scalars(stmt)]
    return results


def _task_day_status_event_payload(event: SyncEvent) -> dict:
    payload = dict(event.payload_json or {}) if isinstance(event.payload_json, dict) else {}
    payload["id"] = int(event.id or 0)
    payload["created_at"] = event.created_at
    return payload


def _sanitize_sync_event_payload(event_type: str, payload: dict) -> dict:
    normalized_type = str(event_type or "").strip()
    if normalized_type == EVENT_TASK_DAY_STATUS:
        return _sanitize_task_day_status_payload(payload)
    if normalized_type == EVENT_ARTICLE_UPSERT:
        return _sanitize_article_upsert_payload(payload)
    if normalized_type == EVENT_ARTICLE_TASK_LINKS:
        return _sanitize_article_task_links_payload(payload)
    return dict(payload or {}) if isinstance(payload, dict) else {}


def _sanitize_article_upsert_payload(payload: dict) -> dict:
    source = payload if isinstance(payload, dict) else {}
    canonical_url, url_hash = _article_identity_from_payload(source)
    if not canonical_url or not url_hash:
        return {}
    article_payload = source.get("payload") if isinstance(source.get("payload"), dict) else {}
    return {
        "local_article_id": str(source.get("local_article_id") or "").strip()[:128],
        "url": canonical_url,
        "canonical_url": canonical_url,
        "url_hash": url_hash,
        "title": str(source.get("title") or "").strip()[:512],
        "source": str(source.get("source") or "").strip()[:128],
        "media_type": _normalize_article_media_type(source.get("media_type")),
        "published_at": str(source.get("published_at") or "").strip()[:64],
        "payload": _sanitize_article_payload_json(article_payload),
    }


def _sanitize_article_task_links_payload(payload: dict) -> dict:
    source = payload if isinstance(payload, dict) else {}
    canonical_url, url_hash = _article_identity_from_payload(source)
    if not canonical_url or not url_hash:
        return {}
    return {
        "local_article_id": str(source.get("local_article_id") or "").strip()[:128],
        "url": canonical_url,
        "canonical_url": canonical_url,
        "url_hash": url_hash,
        "task_ids": _safe_int_list(source.get("task_ids") or source.get("taskIds"), max_items=200),
        "source": str(source.get("source") or "local_rule").strip()[:32] or "local_rule",
        "confidence": _clamp_int(source.get("confidence"), default=100, min_value=0, max_value=100),
        "reason_json": _sanitize_article_reason_json(source.get("reason_json")),
        "replace": bool(source.get("replace", True)),
        "partial": bool(source.get("partial")),
        "unresolved_task_names": _safe_text_list(source.get("unresolved_task_names"), limit=128, max_items=50),
    }


def _sanitize_task_day_status_payload(payload: dict) -> dict:
    source = payload if isinstance(payload, dict) else {}
    try:
        task_id = int(source.get("task_id") or source.get("taskId") or 0)
    except Exception:
        task_id = 0
    if task_id <= 0:
        return {}
    task_day = str(source.get("task_day") or source.get("taskDay") or source.get("date") or "").strip()[:10]
    clean: dict[str, Any] = {
        "task_id": task_id,
        "task_day": task_day,
        "status": str(source.get("status") or "success").strip()[:32] or "success",
    }
    for key, limit in TASK_DAY_STATUS_TEXT_LIMITS.items():
        if key in source:
            clean[key] = str(source.get(key) or "").strip()[:limit]
    for key in ("brands", "completed_keywords", "detected_platforms", "supplemented_keywords"):
        values = _safe_text_list(source.get(key) or source.get(_camel_case_key(key)), limit=128)
        if values:
            clean[key] = values
    for key in ("image_count", "actual_screenshot_count", "fixed_screenshot_target"):
        if key not in source and _camel_case_key(key) not in source:
            continue
        try:
            clean[key] = max(0, int(source.get(key, source.get(_camel_case_key(key))) or 0))
        except Exception:
            clean[key] = 0
    for key in ("notification_success", "forced_ignore_failure", "completed_by_quota"):
        if key in source or _camel_case_key(key) in source:
            clean[key] = bool(source.get(key, source.get(_camel_case_key(key))))
    return clean


def _safe_text_list(value: object, *, limit: int = 128, max_items: int = 200) -> list[str]:
    source = value if isinstance(value, list) else []
    items: list[str] = []
    seen: set[str] = set()
    for item in source:
        text = str(item or "").strip()[:limit]
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
        if len(items) >= max_items:
            break
    return items


def _camel_case_key(value: str) -> str:
    parts = str(value or "").split("_")
    if not parts:
        return value
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


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
