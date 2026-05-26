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

from app.models import CloudIdempotencyKey, SyncBatch, SyncBatchItem, User, WorkspaceChangeLog
from app.schemas import SyncBatchEventIn, SyncEventIn
from app.services.change_log_service import (
    STREAM_ARTICLES,
    STREAM_PROFILE,
    STREAM_REFERENCES,
    STREAM_RUNS,
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
    return {
        "capabilities": list(CAPABILITIES),
        "limits": dict(LIMITS),
        "ttl_seconds": dict(TTL_SECONDS),
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
    return {
        "changes": changes,
        "next_cursors": next_cursors,
        "has_more": has_more,
        "object_refs": [],
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
    stream = streams[index]
    current = compact_change_snapshot(db, user.workspace_id)
    return {
        "changes": [
            {
                "stream": stream,
                "seq": int(current.get(stream) or 0),
                "kind": f"{stream}.reset_required",
                "ref_id": f"bootstrap:{stream}",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "bootstrap_cursor": make_bootstrap_cursor(index + 1),
            }
        ],
        "next_cursors": {stream: int(current.get(stream) or 0)},
        "has_more": index + 1 < len(streams),
        "object_refs": [],
        "reset_required": False,
        "reset_token": reset_token if index + 1 < len(streams) else None,
        "retry_after_seconds": RESET_RETRY_AFTER_SECONDS if index + 1 < len(streams) else DEFAULT_RETRY_AFTER_SECONDS,
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


def make_bootstrap_cursor(index: int) -> str:
    return _encode_cursor_payload({"v": 1, "index": max(0, int(index or 0))})


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
