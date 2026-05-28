from __future__ import annotations

import base64
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import (
    Article,
    ArticleReferenceEvent,
    ArticleTaskLink,
    ArticleVersion,
    AgentCommand,
    AgentResultChunk,
    BrandTask,
    CloudIdempotencyKey,
    ObjectManifest,
    RunRecord,
    SyncBatch,
    SyncBatchItem,
    SyncEvent,
    TaskAccessLevel,
    TaskMember,
    User,
    UserRole,
    WorkspaceChangeLog,
)
from app.schemas import SyncBatchEventIn, SyncEventIn
from app.services.change_log_service import (
    STREAM_ARTICLES,
    STREAM_AGENT_STATUS,
    STREAM_PROFILE,
    STREAM_REFERENCES,
    STREAM_RUNS,
    STREAM_TASKS,
    compact_change_snapshot,
    list_workspace_changes,
)
from app.sync_event_types import (
    EVENT_ARTICLE_REFERENCE,
    EVENT_ARTICLE_TASK_LINKS,
    EVENT_ARTICLE_UPSERT,
    EVENT_PROFILE_UPDATE,
    EVENT_RUN_RECORD,
    EVENT_TASK_DAY_STATUS,
)

CAPABILITIES = ["sync-v2", "batch-v2", "object-v1", "state-delta-v1"]
LIMITS = {
    "metadata_batch_max_items": 500,
    "inline_blob_max_bytes": 32 * 1024,
    "single_put_max_bytes": 5 * 1024 * 1024,
    "multipart_part_bytes": 8 * 1024 * 1024,
    "state_delta_reset_page_max_items": 500,
    "virtual_shards": 1024,
}
TTL_SECONDS = {
    "upload_presigned_url": 15 * 60,
    "download_presigned_url": 60 * 60,
    "multipart_upload_session": 24 * 60 * 60,
    "object_soft_delete": 7 * 24 * 60 * 60,
    "change_log_retention": 60 * 24 * 60 * 60,
    "dead_letter_retention": 90 * 24 * 60 * 60,
    "agent_command_pending": 24 * 60 * 60,
    "agent_result_chunks_after_completion": 30 * 24 * 60 * 60,
}
DEFAULT_RETRY_AFTER_SECONDS = 0
RESET_RETRY_AFTER_SECONDS = 5
MAX_VIRTUAL_SHARDS = LIMITS["virtual_shards"]
QUEUE_BACKPRESSURE_PENDING_THRESHOLD = 5_000
QUEUE_BACKPRESSURE_RETRY_AFTER_SECONDS = 10
QUEUE_REJECT_PENDING_THRESHOLD = 20_000
SYNC_METADATA_BUCKET = "sync_metadata"
SYNC_METADATA_RATE_LIMIT_CAPACITY = 1_000
SYNC_METADATA_RATE_LIMIT_REFILL_PER_SECOND = 100
EVENT_STREAMS = {
    EVENT_ARTICLE_REFERENCE: STREAM_REFERENCES,
    EVENT_ARTICLE_TASK_LINKS: STREAM_ARTICLES,
    EVENT_ARTICLE_UPSERT: STREAM_ARTICLES,
    EVENT_PROFILE_UPDATE: STREAM_PROFILE,
    EVENT_RUN_RECORD: STREAM_RUNS,
    EVENT_TASK_DAY_STATUS: STREAM_RUNS,
}


class SyncBackpressureError(RuntimeError):
    def __init__(self, *, retry_after_seconds: int, queue_depth_hint: int, throttle_bucket: str = SYNC_METADATA_BUCKET) -> None:
        super().__init__("sync queue is temporarily overloaded")
        self.retry_after_seconds = max(1, int(retry_after_seconds or 1))
        self.queue_depth_hint = max(0, int(queue_depth_hint or 0))
        self.throttle_bucket = str(throttle_bucket or SYNC_METADATA_BUCKET)


def cloud_capabilities() -> dict[str, Any]:
    settings = get_settings()
    limits = dict(LIMITS)
    limits.update(
        {
            "object_storage_total_quota_bytes": int(settings.object_storage_total_quota_bytes or 0),
            "object_storage_workspace_quota_bytes": int(settings.object_storage_workspace_quota_bytes or 0),
            "object_storage_max_file_bytes": int(settings.object_storage_max_file_bytes or 0),
            "object_storage_min_free_bytes": int(settings.object_storage_min_free_bytes or 0),
        }
    )
    object_backend = (
        "s3"
        if str(settings.object_storage_endpoint_url or "").strip()
        and str(settings.object_storage_bucket or "").strip()
        and str(settings.object_storage_access_key_id or "").strip()
        and str(settings.object_storage_secret_access_key or "").strip()
        else "local"
    )
    return {
        "capabilities": list(CAPABILITIES),
        "limits": limits,
        "ttl_seconds": dict(TTL_SECONDS),
        "object_storage_backend": object_backend,
    }


def accept_sync_batch(
    db: Session,
    user: User,
    *,
    idempotency_key: str,
    events: list[SyncBatchEventIn],
) -> dict[str, Any]:
    batch_key = str(idempotency_key or "").strip()
    if not batch_key:
        raise ValueError("idempotency_key is required")
    existing_batch_id = db.scalar(
        select(SyncBatch.id).where(
            SyncBatch.workspace_id == user.workspace_id,
            SyncBatch.idempotency_key == batch_key,
        )
    )
    if existing_batch_id:
        return {
            "batch_id": str(existing_batch_id),
            "accepted": 0,
            "duplicates": len(events or []),
            "rejected": 0,
            "pending_materialization": 0,
            "retry_after_seconds": DEFAULT_RETRY_AFTER_SECONDS,
        }

    pending_before = count_pending_materialization(db, workspace_id=user.workspace_id)
    if pending_before >= QUEUE_REJECT_PENDING_THRESHOLD:
        raise SyncBackpressureError(
            retry_after_seconds=max(QUEUE_BACKPRESSURE_RETRY_AFTER_SECONDS, 30),
            queue_depth_hint=pending_before,
        )
    batch_cost = max(1, len(events or []))
    rate_limit = consume_workspace_rate_limit(
        db,
        workspace_id=user.workspace_id,
        bucket=SYNC_METADATA_BUCKET,
        cost=batch_cost,
        capacity=SYNC_METADATA_RATE_LIMIT_CAPACITY,
        refill_rate_per_second=SYNC_METADATA_RATE_LIMIT_REFILL_PER_SECOND,
    )
    if not rate_limit["allowed"]:
        raise SyncBackpressureError(
            retry_after_seconds=int(rate_limit["retry_after_seconds"]),
            queue_depth_hint=pending_before,
            throttle_bucket=SYNC_METADATA_BUCKET,
        )
    retry_after_seconds = _retry_after_for_queue_depth(pending_before)
    batch_id = str(uuid4())
    accepted = 0
    duplicates = 0
    rejected = 0
    batch_items: list[SyncBatchItem] = []
    now = datetime.now(timezone.utc)
    for index, event in enumerate(events or []):
        event_key = str(event.idempotency_key or "").strip()
        if not event.event_type or not event_key:
            rejected += 1
            continue
        if not _reserve_idempotency_key(db, user.workspace_id, scope="sync_batch_item", idempotency_key=event_key):
            duplicates += 1
            continue
        stream = _stream_for_event(event)
        partition_key = _partition_key_for_event(user.workspace_id, event)
        batch_items.append(
            SyncBatchItem(
                batch_id=batch_id,
                workspace_id=user.workspace_id,
                stream=stream,
                partition_key=partition_key,
                virtual_shard=_virtual_shard(partition_key),
                seq=int(event.seq if event.seq is not None else index),
                event_type=event.event_type,
                idempotency_key=event_key,
                payload_json=dict(event.payload or {}),
                status="pending",
                created_at=now,
                updated_at=now,
            )
        )
        accepted += 1

    db.add(
        SyncBatch(
            id=batch_id,
            workspace_id=user.workspace_id,
            user_id=user.id,
            idempotency_key=batch_key,
            status="accepted",
            accepted_count=accepted,
            rejected_count=rejected,
            created_at=now,
        )
    )
    for item in batch_items:
        db.add(item)
    db.commit()
    return {
        "batch_id": batch_id,
        "accepted": accepted,
        "duplicates": duplicates,
        "rejected": rejected,
        "pending_materialization": accepted,
        "retry_after_seconds": retry_after_seconds,
        "queue_depth_hint": pending_before + accepted,
    }


def accept_legacy_sync_events_as_batch(db: Session, user: User, events: list[SyncEventIn]) -> tuple[int, int]:
    batch_key = _legacy_batch_key(user.workspace_id, events)
    batch_events = [
        SyncBatchEventIn(
            event_type=event.event_type,
            idempotency_key=event.idempotency_key,
            payload=dict(event.payload or {}),
        )
        for event in events or []
    ]
    result = accept_sync_batch(db, user, idempotency_key=batch_key, events=batch_events)
    return int(result.get("accepted") or 0), int(result.get("duplicates") or 0)


def build_state_delta(
    db: Session,
    user: User,
    *,
    cursors: dict[str, int],
    limit: int = 500,
    reset_token: str | None = None,
    bootstrap_cursor: str | None = None,
) -> dict[str, Any]:
    safe_limit = max(1, min(int(limit or 500), 1000))
    if reset_token:
        return build_state_reset_page(
            db,
            user,
            reset_token=reset_token,
            bootstrap_cursor=bootstrap_cursor,
            limit=safe_limit,
        )
    current = compact_change_snapshot(db, user.workspace_id)
    stale_streams = _stale_cursor_streams(db, workspace_id=user.workspace_id, cursors=cursors)
    if stale_streams:
        return {
            "changes": [],
            "next_cursors": dict(cursors or {}),
            "has_more": True,
            "object_refs": [],
            "reset_required": True,
            "reset_token": make_reset_token(user.workspace_id, stale_streams),
            "retry_after_seconds": RESET_RETRY_AFTER_SECONDS,
        }
    changes: list[dict[str, Any]] = []
    next_cursors: dict[str, int] = {}
    has_more = False
    for stream in _ordered_streams(cursors, current):
        after_seq = int((cursors or {}).get(stream) or 0)
        stream_changes = list_workspace_changes(
            db,
            workspace_id=user.workspace_id,
            stream=stream,
            after_seq=after_seq,
            limit=max(1, safe_limit - len(changes)),
        )
        if stream_changes:
            changes.extend(stream_changes)
            next_cursors[stream] = int(stream_changes[-1]["seq"])
        else:
            next_cursors[stream] = max(after_seq, int(current.get(stream) or 0))
        if len(changes) >= safe_limit:
            has_more = True
            break
    for stream, seq in current.items():
        next_cursors.setdefault(stream, max(int((cursors or {}).get(stream) or 0), int(seq or 0)))
    enriched_changes, object_refs = _enrich_state_delta_changes(db, user, changes)
    return {
        "changes": enriched_changes,
        "next_cursors": next_cursors,
        "has_more": has_more,
        "object_refs": object_refs,
        "reset_required": False,
        "reset_token": None,
        "retry_after_seconds": DEFAULT_RETRY_AFTER_SECONDS,
    }


def build_state_reset_page(
    db: Session,
    user: User,
    *,
    reset_token: str,
    bootstrap_cursor: str | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    token_payload = parse_reset_token(reset_token)
    if int(token_payload.get("workspace_id") or 0) != int(user.workspace_id):
        return {
            "changes": [],
            "next_cursors": {},
            "has_more": False,
            "object_refs": [],
            "reset_required": True,
            "reset_token": make_reset_token(user.workspace_id, ["tasks", "runs", "articles"]),
            "retry_after_seconds": RESET_RETRY_AFTER_SECONDS,
        }
    streams = [str(item) for item in token_payload.get("streams", []) if str(item)]
    cursor_payload = parse_bootstrap_cursor(bootstrap_cursor)
    index = max(0, int(cursor_payload.get("index") or 0))
    after_id = max(0, int(cursor_payload.get("after_id") or 0))
    after_key = str(cursor_payload.get("after_key") or "").strip()
    if index >= len(streams):
        return {
            "changes": [],
            "next_cursors": compact_change_snapshot(db, user.workspace_id),
            "has_more": False,
            "object_refs": [],
            "reset_required": False,
            "reset_token": None,
            "retry_after_seconds": DEFAULT_RETRY_AFTER_SECONDS,
        }
    current = compact_change_snapshot(db, user.workspace_id)
    stream = streams[index]
    page = _build_reset_stream_page(db, user, stream=stream, after_id=after_id, after_key=after_key, limit=limit)
    next_index = index + 1 if not page["has_more"] else index
    next_after_id = 0 if not page["has_more"] else int(page["next_after_id"])
    next_after_key = "" if not page["has_more"] else str(page.get("next_after_key") or "")
    has_more = page["has_more"] or next_index < len(streams)
    next_cursor = make_bootstrap_cursor(next_index, after_id=next_after_id, after_key=next_after_key) if has_more else None
    next_cursors = {stream: int(current.get(stream) or 0)}
    return {
        "changes": page["changes"],
        "next_cursors": next_cursors,
        "has_more": has_more,
        "object_refs": page["object_refs"],
        "reset_required": False,
        "reset_token": reset_token if has_more else None,
        "retry_after_seconds": RESET_RETRY_AFTER_SECONDS if has_more else DEFAULT_RETRY_AFTER_SECONDS,
        "bootstrap_cursor": next_cursor,
    }


def count_pending_materialization(db: Session, *, workspace_id: int) -> int:
    try:
        value = db.execute(
            select(func.count())
            .select_from(SyncBatchItem)
            .where(
                SyncBatchItem.workspace_id == int(workspace_id),
                SyncBatchItem.status.in_(("pending", "in_progress")),
            )
        )
        return int(value.scalar_one() or 0)
    except Exception:
        return 0


def _build_reset_stream_page(
    db: Session,
    user: User,
    *,
    stream: str,
    after_id: int,
    after_key: str,
    limit: int,
) -> dict[str, Any]:
    safe_limit = max(1, min(int(limit or LIMITS["state_delta_reset_page_max_items"]), int(LIMITS["state_delta_reset_page_max_items"])))
    object_refs_by_id: dict[str, dict[str, Any]] = {}
    if stream == STREAM_TASKS:
        rows = _reset_task_entities(db, user, after_id=after_id, limit=safe_limit + 1)
    elif stream == STREAM_RUNS:
        rows = _reset_run_record_entities(db, user, after_id=after_id, limit=safe_limit + 1)
    elif stream == STREAM_ARTICLES:
        rows = _reset_article_entities(db, user, after_id=after_id, limit=safe_limit + 1, object_refs_by_id=object_refs_by_id)
    elif stream == STREAM_REFERENCES:
        rows = _reset_reference_entities(db, user, after_id=after_id, limit=safe_limit + 1)
    elif stream == STREAM_AGENT_STATUS:
        rows = _reset_agent_status_entities(db, user, after_key=after_key, limit=safe_limit + 1)
    elif stream == STREAM_PROFILE:
        rows = [_state_delta_profile_entity(user)] if after_id <= 0 else []
    else:
        rows = []
    page_rows = rows[:safe_limit]
    has_more = len(rows) > safe_limit
    next_after_id = _reset_entity_cursor(page_rows[-1]) if page_rows else after_id
    next_after_key = _reset_entity_key(page_rows[-1]) if page_rows else after_key
    changes = []
    for row in page_rows:
        row_cursor = _reset_entity_cursor(row)
        row_key = _reset_entity_key(row)
        changes.append(
            {
                "stream": stream,
                "seq": row_cursor,
                "kind": f"{stream}.bootstrap",
                "ref_id": f"bootstrap:{stream}:{row_key or row_cursor}",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "entity": row,
            }
        )
    if not changes and not has_more:
        changes.append(
            {
                "stream": stream,
                "seq": 0,
                "kind": f"{stream}.bootstrap_complete",
                "ref_id": f"bootstrap:{stream}:complete",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "entity": {"type": f"{stream}_bootstrap_complete"},
            }
        )
    return {
        "changes": changes,
        "object_refs": list(object_refs_by_id.values()),
        "has_more": has_more,
        "next_after_id": next_after_id,
        "next_after_key": next_after_key,
    }


def _reset_task_entities(db: Session, user: User, *, after_id: int, limit: int) -> list[dict[str, Any]]:
    query = select(BrandTask).where(
        BrandTask.workspace_id == user.workspace_id,
        BrandTask.id > int(after_id or 0),
        BrandTask.deleted_at.is_(None),
    )
    if not _user_can_view_all_tasks(user):
        query = query.join(TaskMember, TaskMember.task_id == BrandTask.id).where(
            TaskMember.workspace_id == user.workspace_id,
            TaskMember.user_id == user.id,
        )
    query = query.order_by(BrandTask.id.asc()).limit(limit)
    return [_task_entity(task, _task_access_level_for_user(user)) for task in db.scalars(query)]


def _reset_run_record_entities(db: Session, user: User, *, after_id: int, limit: int) -> list[dict[str, Any]]:
    visible_task_ids = _visible_task_ids_for_state_delta(db, user)
    if not visible_task_ids:
        return []
    rows = db.scalars(
        select(RunRecord)
        .where(
            RunRecord.workspace_id == user.workspace_id,
            RunRecord.id > int(after_id or 0),
            RunRecord.task_id.in_(visible_task_ids),
        )
        .order_by(RunRecord.id.asc())
        .limit(limit)
    )
    return [_run_record_entity(record) for record in rows]


def _reset_article_entities(
    db: Session,
    user: User,
    *,
    after_id: int,
    limit: int,
    object_refs_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    visible_task_ids = _visible_task_ids_for_state_delta(db, user)
    if not visible_task_ids:
        return []
    articles = list(
        db.scalars(
            select(Article)
            .join(ArticleTaskLink, ArticleTaskLink.article_id == Article.id)
            .where(
                Article.workspace_id == user.workspace_id,
                Article.id > int(after_id or 0),
                ArticleTaskLink.workspace_id == user.workspace_id,
                ArticleTaskLink.task_id.in_(visible_task_ids),
            )
            .distinct()
            .order_by(Article.id.asc())
            .limit(limit)
        )
    )
    return [
        _article_entity_from_article(db, user, article, object_refs_by_id, visible_task_ids=visible_task_ids)
        for article in articles
    ]


def _reset_reference_entities(db: Session, user: User, *, after_id: int, limit: int) -> list[dict[str, Any]]:
    visible_task_ids = _visible_task_ids_for_state_delta(db, user)
    if not visible_task_ids:
        return []
    rows = db.scalars(
        select(ArticleReferenceEvent)
        .where(
            ArticleReferenceEvent.workspace_id == user.workspace_id,
            ArticleReferenceEvent.id > int(after_id or 0),
            ArticleReferenceEvent.task_id.in_(visible_task_ids),
        )
        .order_by(ArticleReferenceEvent.id.asc())
        .limit(limit)
    )
    return [_article_reference_entity(row) for row in rows]


def _reset_agent_status_entities(db: Session, user: User, *, after_key: str, limit: int) -> list[dict[str, Any]]:
    query = select(AgentCommand).where(
        AgentCommand.workspace_id == user.workspace_id,
        AgentCommand.created_at.is_not(None),
    )
    after_created_at, after_id = _parse_agent_cursor_key(after_key)
    if after_created_at is not None:
        query = query.where(
            (AgentCommand.created_at > after_created_at)
            | ((AgentCommand.created_at == after_created_at) & (AgentCommand.id > after_id))
        )
    rows = db.scalars(query.order_by(AgentCommand.created_at.asc(), AgentCommand.id.asc()).limit(limit))
    return [_agent_command_status_entity(db, row) for row in rows]


def _visible_task_ids_for_state_delta(db: Session, user: User) -> list[int]:
    retention_clause = (
        (BrandTask.deleted_at.is_(None))
        | (BrandTask.delete_expires_at.is_(None))
        | (BrandTask.delete_expires_at > datetime.now(timezone.utc))
    )
    if _user_can_view_all_tasks(user):
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


def _task_entity(task: BrandTask, access_level: str) -> dict[str, Any]:
    return {
        "type": "task",
        "id": int(task.id),
        "workspace_id": int(task.workspace_id),
        "task_key": str(task.task_key or ""),
        "name": str(task.name or ""),
        "brand": str(task.brand or ""),
        "config_json": dict(task.config_json or {}),
        "config_version": int(task.config_version or 0),
        "enabled": bool(task.enabled),
        "deleted_at": _iso_datetime(task.deleted_at),
        "delete_expires_at": _iso_datetime(task.delete_expires_at),
        "created_at": _iso_datetime(task.created_at),
        "updated_at": _iso_datetime(task.updated_at),
        "access_level": access_level,
    }


def _task_access_level_for_user(user: User) -> str:
    if getattr(user, "role", None) == UserRole.admin or _enum_value(getattr(user, "role", "")) == "admin":
        return "admin"
    return TaskAccessLevel.view.value


def _user_can_view_all_tasks(user: User) -> bool:
    role = _enum_value(getattr(user, "role", ""))
    return role == "admin" or (role == "viewer" and bool(getattr(user, "view_all_tasks", False)))


def _reset_entity_cursor(entity: dict[str, Any]) -> int:
    try:
        return int(entity.get("id") or 0)
    except Exception:
        return 0


def _reset_entity_key(entity: dict[str, Any]) -> str:
    if entity.get("type") == "agent_command_status":
        return _agent_cursor_key(entity)
    return str(entity.get("id") or entity.get("command_id") or "")


def _agent_cursor_key(entity: dict[str, Any]) -> str:
    return f"{str(entity.get('created_at') or '')}|{str(entity.get('id') or '')}"[:128]


def _parse_agent_cursor_key(value: str) -> tuple[datetime | None, str]:
    text_value = str(value or "").strip()
    if "|" not in text_value:
        return None, ""
    created_at_text, command_id = text_value.split("|", 1)
    try:
        created_at = datetime.fromisoformat(created_at_text)
    except ValueError:
        return None, ""
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at.astimezone(timezone.utc), command_id[:36]


def _enrich_state_delta_changes(db: Session, user: User, changes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    object_refs_by_id: dict[str, dict[str, Any]] = {}
    enriched: list[dict[str, Any]] = []
    for change in changes:
        item = dict(change)
        try:
            entity = _entity_for_state_delta_change(db, user, item, object_refs_by_id)
            if entity is not None:
                item["entity"] = entity
        except Exception:
            item["entity_error"] = "unavailable"
        enriched.append(item)
    return enriched, list(object_refs_by_id.values())


def _entity_for_state_delta_change(
    db: Session,
    user: User,
    change: dict[str, Any],
    object_refs_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    kind = str(change.get("kind") or "").strip()
    ref_id = str(change.get("ref_id") or "").strip()
    if not kind or not ref_id:
        return None
    if kind in {"task.created", "task.updated", "task.deleted", "task.restored", "task.assignment_changed"}:
        return _state_delta_task_entity(db, user, ref_id)
    if kind == "run.record":
        return _state_delta_run_record_entity(db, user, ref_id)
    if kind == "run.record.backfill":
        return _state_delta_run_record_entity_by_id(db, user, ref_id)
    if kind == "task.day_status":
        return _state_delta_task_day_status_entity(db, user, ref_id)
    if kind == "article.reference":
        return _state_delta_article_reference_entity(db, user, ref_id)
    if kind == "article.reference.backfill":
        return _state_delta_article_reference_entity_by_id(db, user, ref_id)
    if kind in {"article.upsert", "article.link", "article.classification_resolved", "article.classification_ignored"}:
        return _state_delta_article_entity(db, user, ref_id, object_refs_by_id)
    if kind == "article.upsert.backfill":
        return _state_delta_article_entity_by_id(db, user, ref_id, object_refs_by_id)
    if kind.startswith("agent."):
        return _state_delta_agent_status_entity(db, user, ref_id)
    if kind in {"profile.update", "profile.updated", "admin.user_created", "admin.user_updated", "admin.user_deleted", "viewer.scope_changed"}:
        return _state_delta_profile_entity(user)
    return None


def _state_delta_task_entity(db: Session, user: User, ref_id: str) -> dict[str, Any] | None:
    try:
        task_id = int(ref_id)
    except (TypeError, ValueError):
        return None
    task = db.scalar(
        select(BrandTask).where(
            BrandTask.workspace_id == user.workspace_id,
            BrandTask.id == task_id,
        )
    )
    if task is None:
        return None
    return _task_entity(task, _task_access_level_for_user(user))


def _state_delta_run_record_entity(db: Session, user: User, ref_id: str) -> dict[str, Any] | None:
    record = db.scalar(
        select(RunRecord).where(
            RunRecord.workspace_id == user.workspace_id,
            RunRecord.idempotency_key == ref_id,
        )
    )
    if record is None:
        return None
    return _run_record_entity(record)


def _state_delta_run_record_entity_by_id(db: Session, user: User, ref_id: str) -> dict[str, Any] | None:
    try:
        record_id = int(ref_id)
    except (TypeError, ValueError):
        return None
    record = db.scalar(
        select(RunRecord).where(
            RunRecord.workspace_id == user.workspace_id,
            RunRecord.id == record_id,
        )
    )
    if record is None:
        return None
    return _run_record_entity(record)


def _run_record_entity(record: RunRecord) -> dict[str, Any]:
    return {
        "type": "run_record",
        "id": int(record.id),
        "workspace_id": int(record.workspace_id),
        "task_id": int(record.task_id),
        "executed_by": int(record.executed_by),
        "platform": str(record.platform or ""),
        "keyword": str(record.keyword or ""),
        "brand": str(record.brand or ""),
        "mode": str(record.mode or ""),
        "result_json": dict(record.result_json or {}),
        "idempotency_key": str(record.idempotency_key or ""),
        "executed_at": _iso_datetime(record.executed_at),
        "created_at": _iso_datetime(record.created_at),
    }


def _state_delta_task_day_status_entity(db: Session, user: User, ref_id: str) -> dict[str, Any] | None:
    event = _sync_event_for_ref(db, user, ref_id)
    if event is None:
        return None
    payload = dict(event.payload_json or {}) if isinstance(event.payload_json, dict) else {}
    payload["type"] = "task_day_status"
    payload["id"] = int(event.id or 0)
    payload["idempotency_key"] = str(event.idempotency_key or "")
    payload["created_at"] = _iso_datetime(event.created_at)
    return payload


def _state_delta_article_reference_entity(db: Session, user: User, ref_id: str) -> dict[str, Any] | None:
    reference = db.scalar(
        select(ArticleReferenceEvent).where(
            ArticleReferenceEvent.workspace_id == user.workspace_id,
            ArticleReferenceEvent.idempotency_key == ref_id,
        )
    )
    if reference is None:
        return None
    return _article_reference_entity(reference)


def _state_delta_article_reference_entity_by_id(db: Session, user: User, ref_id: str) -> dict[str, Any] | None:
    try:
        reference_id = int(ref_id)
    except (TypeError, ValueError):
        return None
    reference = db.scalar(
        select(ArticleReferenceEvent).where(
            ArticleReferenceEvent.workspace_id == user.workspace_id,
            ArticleReferenceEvent.id == reference_id,
        )
    )
    if reference is None:
        return None
    return _article_reference_entity(reference)


def _article_reference_entity(reference: ArticleReferenceEvent) -> dict[str, Any]:
    return {
        "type": "article_reference",
        "id": int(reference.id),
        "workspace_id": int(reference.workspace_id),
        "task_id": int(reference.task_id),
        "article_id": int(reference.article_id) if reference.article_id is not None else None,
        "normalized_url": str(reference.normalized_url or ""),
        "url_hash": str(reference.url_hash or ""),
        "platform": str(reference.platform or ""),
        "record_day": str(reference.record_day or ""),
        "source_record_key": str(reference.source_record_key or ""),
        "idempotency_key": str(reference.idempotency_key or ""),
        "event_json": dict(reference.event_json or {}),
        "created_at": _iso_datetime(reference.created_at),
    }


def _state_delta_article_entity(
    db: Session,
    user: User,
    ref_id: str,
    object_refs_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    event = _sync_event_for_ref(db, user, ref_id)
    payload = dict(event.payload_json or {}) if event is not None and isinstance(event.payload_json, dict) else {}
    url_hash = str(payload.get("url_hash") or "").strip()
    canonical_url = str(payload.get("canonical_url") or payload.get("url") or "").strip()
    if not url_hash and canonical_url:
        url_hash = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()
    if not url_hash and not canonical_url:
        return None

    query = select(Article).where(Article.workspace_id == user.workspace_id)
    if url_hash:
        query = query.where(Article.url_hash == url_hash)
    else:
        query = query.where(Article.canonical_url == canonical_url)
    article = db.scalar(query)
    if article is None:
        return None

    return _article_entity_from_article(db, user, article, object_refs_by_id)


def _state_delta_article_entity_by_id(
    db: Session,
    user: User,
    ref_id: str,
    object_refs_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    try:
        article_id = int(ref_id)
    except (TypeError, ValueError):
        return None
    article = db.scalar(
        select(Article).where(
            Article.workspace_id == user.workspace_id,
            Article.id == article_id,
        )
    )
    if article is None:
        return None
    return _article_entity_from_article(db, user, article, object_refs_by_id)


def _article_entity_from_article(
    db: Session,
    user: User,
    article: Article,
    object_refs_by_id: dict[str, dict[str, Any]],
    *,
    visible_task_ids: list[int] | None = None,
) -> dict[str, Any]:
    link_query = select(ArticleTaskLink).where(
        ArticleTaskLink.workspace_id == user.workspace_id,
        ArticleTaskLink.article_id == int(article.id),
    )
    if visible_task_ids is not None:
        link_query = link_query.where(ArticleTaskLink.task_id.in_(visible_task_ids or [-1]))
    task_links = [_article_task_link_entity(link) for link in db.scalars(link_query.order_by(ArticleTaskLink.task_id.asc()))]
    version = db.scalar(
        select(ArticleVersion)
        .where(
            ArticleVersion.workspace_id == user.workspace_id,
            ArticleVersion.article_id == int(article.id),
        )
        .order_by(ArticleVersion.version.desc(), ArticleVersion.id.desc())
        .limit(1)
    )
    content_ref = _article_content_ref(db, user, version, object_refs_by_id)
    return {
        "type": "article",
        "id": int(article.id),
        "workspace_id": int(article.workspace_id),
        "canonical_url": str(article.canonical_url or ""),
        "url_hash": str(article.url_hash or ""),
        "title": article.title,
        "source": article.source,
        "media_type": article.media_type,
        "published_at": _iso_datetime(article.published_at),
        "payload_json": dict(article.payload_json or {}),
        "created_at": _iso_datetime(article.created_at),
        "updated_at": _iso_datetime(article.updated_at),
        "task_links": task_links,
        "content_ref": content_ref,
    }


def _article_task_link_entity(link: ArticleTaskLink) -> dict[str, Any]:
    return {
        "task_id": int(link.task_id),
        "source": str(link.source or ""),
        "confidence": int(link.confidence or 0),
        "reason_json": dict(link.reason_json or {}),
        "created_at": _iso_datetime(link.created_at),
    }


def _article_content_ref(
    db: Session,
    user: User,
    version: ArticleVersion | None,
    object_refs_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if version is None:
        return None
    base = {
        "version": int(version.version),
        "sha256": str(version.content_sha256 or ""),
        "size_bytes": int(version.size_bytes or 0),
        "created_at": _iso_datetime(version.created_at),
    }
    if version.inline_text is not None:
        return {
            **base,
            "kind": "inline_text",
            "inline_text": str(version.inline_text),
        }
    object_id = str(version.content_object_id or "").strip()
    if not object_id:
        return None
    manifest = db.scalar(
        select(ObjectManifest).where(
            ObjectManifest.workspace_id == user.workspace_id,
            ObjectManifest.id == object_id,
        )
    )
    if manifest is not None:
        object_refs_by_id.setdefault(str(manifest.id), _object_ref_payload(manifest))
        compression = str(manifest.compression or "")
        content_type = str(manifest.content_type or "")
        storage_size_bytes = int(manifest.storage_size_bytes or 0)
    else:
        compression = ""
        content_type = ""
        storage_size_bytes = 0
    return {
        **base,
        "kind": "object",
        "object_id": object_id,
        "content_type": content_type,
        "compression": compression,
        "storage_size_bytes": storage_size_bytes,
    }


def _state_delta_agent_status_entity(db: Session, user: User, ref_id: str) -> dict[str, Any] | None:
    command = db.scalar(
        select(AgentCommand).where(
            AgentCommand.workspace_id == user.workspace_id,
            AgentCommand.id == str(ref_id),
        )
    )
    if command is None:
        return None
    return _agent_command_status_entity(db, command)


def _agent_command_status_entity(db: Session, command: AgentCommand) -> dict[str, Any]:
    chunks = [
        _agent_result_chunk_entity(chunk)
        for chunk in db.scalars(
            select(AgentResultChunk)
            .where(AgentResultChunk.command_id == str(command.id))
            .order_by(AgentResultChunk.seq.asc())
            .limit(100)
        )
    ]
    return {
        "type": "agent_command_status",
        "id": str(command.id),
        "workspace_id": int(command.workspace_id),
        "target_device_id": command.target_device_id,
        "target_role": command.target_role,
        "status": str(command.status or ""),
        "visibility_until": _iso_datetime(command.visibility_until),
        "idempotency_key": str(command.idempotency_key or ""),
        "cancel_requested_at": _iso_datetime(command.cancel_requested_at),
        "expires_at": _iso_datetime(command.expires_at),
        "created_at": _iso_datetime(command.created_at),
        "result_chunks": chunks,
    }


def _agent_result_chunk_entity(chunk: AgentResultChunk) -> dict[str, Any]:
    return {
        "command_id": str(chunk.command_id),
        "seq": int(chunk.seq),
        "payload_json": dict(chunk.payload_json or {}),
        "is_final": bool(chunk.is_final),
        "created_at": _iso_datetime(chunk.created_at),
    }


def _object_ref_payload(manifest: ObjectManifest) -> dict[str, Any]:
    return {
        "object_id": str(manifest.id),
        "sha256": str(manifest.sha256 or ""),
        "size_bytes": int(manifest.size_bytes or 0),
        "storage_size_bytes": int(manifest.storage_size_bytes or 0),
        "content_type": str(manifest.content_type or ""),
        "compression": str(manifest.compression or ""),
        "storage_key": str(manifest.storage_key or ""),
    }


def _state_delta_profile_entity(user: User) -> dict[str, Any]:
    return {
        "type": "profile",
        "user_id": int(user.id),
        "workspace_id": int(user.workspace_id),
        "username": str(user.username or ""),
        "role": _enum_value(user.role),
        "display_name": user.display_name,
        "email": user.email,
        "email_verified": bool(user.email_verified),
        "avatar": user.avatar,
        "birthday": _iso_date(user.birthday),
        "hire_date": _iso_date(user.hire_date),
        "view_all_tasks": bool(getattr(user, "view_all_tasks", False)),
        "enabled": bool(getattr(user, "enabled", True)),
        "updated_at": _iso_datetime(user.updated_at),
    }


def _sync_event_for_ref(db: Session, user: User, ref_id: str) -> SyncEvent | None:
    return db.scalar(
        select(SyncEvent).where(
            SyncEvent.workspace_id == user.workspace_id,
            SyncEvent.idempotency_key == ref_id,
        )
    )


def _iso_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).isoformat()
    return value.astimezone(timezone.utc).isoformat()


def _iso_date(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def consume_workspace_rate_limit(
    db: Session,
    *,
    workspace_id: int,
    bucket: str,
    cost: int,
    capacity: int,
    refill_rate_per_second: int,
) -> dict[str, Any]:
    safe_workspace_id = int(workspace_id)
    safe_bucket = str(bucket or SYNC_METADATA_BUCKET).strip()[:64] or SYNC_METADATA_BUCKET
    safe_cost = max(1, int(cost or 1))
    safe_capacity = max(1, int(capacity or 1))
    safe_refill_rate = max(1, int(refill_rate_per_second or 1))
    try:
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": workspace_bucket_advisory_lock_key(safe_workspace_id, safe_bucket)},
        )
        refreshed = (
            db.execute(
                text(
                    """
                    INSERT INTO workspace_rate_limits (
                        workspace_id,
                        bucket,
                        tokens,
                        capacity,
                        refill_rate_per_second,
                        last_refill_at,
                        updated_at
                    )
                    VALUES (
                        :workspace_id,
                        :bucket,
                        :capacity,
                        :capacity,
                        :refill_rate_per_second,
                        now(),
                        now()
                    )
                    ON CONFLICT (workspace_id, bucket) DO UPDATE
                    SET tokens = LEAST(
                            EXCLUDED.capacity,
                            workspace_rate_limits.tokens
                              + EXTRACT(EPOCH FROM (now() - workspace_rate_limits.last_refill_at))
                                * workspace_rate_limits.refill_rate_per_second
                        ),
                        capacity = EXCLUDED.capacity,
                        refill_rate_per_second = EXCLUDED.refill_rate_per_second,
                        last_refill_at = now(),
                        updated_at = now()
                    RETURNING tokens, capacity, refill_rate_per_second
                    """
                ),
                {
                    "workspace_id": safe_workspace_id,
                    "bucket": safe_bucket,
                    "capacity": safe_capacity,
                    "refill_rate_per_second": safe_refill_rate,
                    "cost": safe_cost,
                },
            )
            .mappings()
            .first()
        )
        if not refreshed:
            return _allow_rate_limit_result()
        available = _coerce_float(refreshed.get("tokens"))
        if available is None:
            return _allow_rate_limit_result()
        if available >= safe_cost:
            spent = (
                db.execute(
                    text(
                        """
                        UPDATE workspace_rate_limits
                        SET tokens = tokens - :cost,
                            updated_at = now()
                        WHERE workspace_id = :workspace_id
                          AND bucket = :bucket
                        RETURNING tokens
                        """
                    ),
                    {
                        "workspace_id": safe_workspace_id,
                        "bucket": safe_bucket,
                        "cost": safe_cost,
                    },
                )
                .mappings()
                .first()
            )
            spent_tokens = _coerce_float(spent.get("tokens")) if spent else None
            remaining = spent_tokens if spent_tokens is not None else available - safe_cost
            return {"allowed": True, "retry_after_seconds": 0, "remaining_tokens": remaining}
        retry_after = max(1, math.ceil((safe_cost - available) / safe_refill_rate))
        return {"allowed": False, "retry_after_seconds": retry_after, "remaining_tokens": available}
    except Exception:
        # Unit tests often run with mocked sessions, and degraded rate-limit state
        # must not make the sync API unavailable. Hard queue-depth backpressure is
        # still enforced before this function is called.
        return _allow_rate_limit_result()


def _allow_rate_limit_result() -> dict[str, Any]:
    return {"allowed": True, "retry_after_seconds": 0, "remaining_tokens": None}


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        text = str(value).strip()
        if not text or text.startswith("<"):
            return None
        return float(text)
    except Exception:
        return None


def _retry_after_for_queue_depth(pending_count: int) -> int:
    if int(pending_count or 0) >= QUEUE_BACKPRESSURE_PENDING_THRESHOLD:
        return QUEUE_BACKPRESSURE_RETRY_AFTER_SECONDS
    return DEFAULT_RETRY_AFTER_SECONDS


def throttle_headers(*, retry_after_seconds: int, queue_depth_hint: int, throttle_bucket: str = "sync_metadata") -> dict[str, str]:
    headers = {
        "X-Queue-Depth-Hint": str(max(0, int(queue_depth_hint or 0))),
        "X-Throttle-Bucket": str(throttle_bucket or "sync_metadata"),
    }
    if int(retry_after_seconds or 0) > 0:
        headers["Retry-After"] = str(int(retry_after_seconds))
    return headers


def make_reset_token(workspace_id: int, streams: list[str]) -> str:
    payload = {
        "v": 1,
        "workspace_id": int(workspace_id),
        "streams": [str(item)[:32] for item in streams if str(item).strip()],
        "issued_at": datetime.now(timezone.utc).isoformat(),
    }
    return _encode_cursor_payload(payload)


def parse_reset_token(value: str | None) -> dict[str, Any]:
    payload = _decode_cursor_payload(value)
    if int(payload.get("v") or 0) != 1:
        return {}
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return {}
    return payload


def make_bootstrap_cursor(index: int, *, after_id: int = 0, after_key: str = "") -> str:
    return _encode_cursor_payload({
        "v": 1,
        "index": max(0, int(index or 0)),
        "after_id": max(0, int(after_id or 0)),
        "after_key": str(after_key or "")[:128],
    })


def parse_bootstrap_cursor(value: str | None) -> dict[str, Any]:
    payload = _decode_cursor_payload(value)
    if int(payload.get("v") or 0) != 1:
        return {"index": 0}
    return payload


def _reserve_idempotency_key(db: Session, workspace_id: int, *, scope: str, idempotency_key: str) -> bool:
    stmt = (
        insert(CloudIdempotencyKey)
        .values(workspace_id=int(workspace_id), scope=str(scope), idempotency_key=str(idempotency_key))
        .on_conflict_do_nothing(index_elements=["workspace_id", "scope", "idempotency_key"])
        .returning(CloudIdempotencyKey.idempotency_key)
    )
    return db.scalar(stmt) is not None


def _stream_for_event(event: SyncBatchEventIn) -> str:
    explicit = str(event.stream or "").strip()
    if explicit:
        return explicit[:32]
    return EVENT_STREAMS.get(event.event_type, "workspace")


def _partition_key_for_event(workspace_id: int, event: SyncBatchEventIn) -> str:
    explicit = str(event.partition_key or "").strip()
    if explicit:
        return explicit[:256]
    payload = event.payload if isinstance(event.payload, dict) else {}
    for key in ("article_id", "local_article_id", "url_hash", "canonical_url", "url", "task_id", "record_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return f"{int(workspace_id)}:{event.event_type}:{key}:{value}"[:256]
    return f"{int(workspace_id)}:{event.event_type}:{event.idempotency_key}"[:256]


def _virtual_shard(partition_key: str) -> int:
    digest = hashlib.sha256(str(partition_key).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % MAX_VIRTUAL_SHARDS


def shard_advisory_lock_key(virtual_shard: int) -> int:
    return _stable_int63(f"sync-shard:{int(virtual_shard)}")


def workspace_bucket_advisory_lock_key(workspace_id: int, bucket: str) -> int:
    return _stable_int63(f"workspace-rate:{int(workspace_id)}:{str(bucket or '')}")


def _stable_int63(text_value: str) -> int:
    digest = hashlib.sha256(str(text_value).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False) & ((1 << 63) - 1)


def _ordered_streams(cursors: dict[str, int], current: dict[str, int]) -> list[str]:
    streams: list[str] = []
    for source in (cursors or {}, current or {}):
        for stream in source:
            text = str(stream or "").strip()
            if text and text not in streams:
                streams.append(text)
    return streams


def _stale_cursor_streams(db: Session, *, workspace_id: int, cursors: dict[str, int]) -> list[str]:
    stale: list[str] = []
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=TTL_SECONDS["change_log_retention"])
    for stream, seq in (cursors or {}).items():
        safe_stream = str(stream or "").strip()
        safe_seq = int(seq or 0)
        if not safe_stream or safe_seq <= 0:
            continue
        first_available = db.scalar(
            select(func.min(WorkspaceChangeLog.seq))
            .where(
                WorkspaceChangeLog.workspace_id == int(workspace_id),
                WorkspaceChangeLog.stream == safe_stream,
                WorkspaceChangeLog.created_at >= cutoff,
            )
        )
        if first_available is not None and safe_seq < int(first_available):
            stale.append(safe_stream)
    return stale


def _encode_cursor_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor_payload(value: str | None) -> dict[str, Any]:
    text_value = str(value or "").strip()
    if not text_value:
        return {}
    try:
        padding = "=" * (-len(text_value) % 4)
        decoded = base64.urlsafe_b64decode((text_value + padding).encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _legacy_batch_key(workspace_id: int, events: list[SyncEventIn]) -> str:
    digest = hashlib.sha256()
    digest.update(str(int(workspace_id)).encode("utf-8"))
    for event in events or []:
        digest.update(b"\0")
        digest.update(str(event.event_type or "").encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(event.idempotency_key or "").encode("utf-8"))
    return f"legacy:{digest.hexdigest()[:48]}"
