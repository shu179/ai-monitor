from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import SyncBatch, SyncEvent, User
from app.schemas import SyncEventIn
from app.services.sync_service import _materialize_known_event, _sanitize_sync_event_payload
from app.services.sync_v2_service import shard_advisory_lock_key

WORKER_LEASE_SECONDS = 60
WORKER_HEARTBEAT_SECONDS = 15
MAX_MATERIALIZE_ATTEMPTS = 5
logger = logging.getLogger(__name__)


def claim_sync_batch_items(
    db: Session,
    *,
    worker_id: str,
    shard_ids: list[int],
    limit: int = 100,
    lease_seconds: int = WORKER_LEASE_SECONDS,
) -> list[dict[str, Any]]:
    """Claim pending or expired items with a CTE, never UPDATE ... LIMIT.

    The virtual_shard predicate is the ordering guard: one shard should be owned
    by at most one worker lease group at a time, so a single partition_key cannot
    be processed concurrently by two workers.
    """
    safe_shards = [int(item) for item in shard_ids if int(item) >= 0]
    if not safe_shards:
        return []
    owned_shards = renew_sync_worker_shard_leases(
        db,
        worker_id=worker_id,
        shard_ids=safe_shards,
        lease_seconds=lease_seconds,
    )
    if not owned_shards:
        return []
    rows = db.execute(
        text(
            """
            WITH candidates AS (
                SELECT item.id, item.created_at
                FROM sync_batch_items AS item
                WHERE item.virtual_shard = ANY(:shard_ids)
                  AND (
                    item.status = 'pending'
                    OR (item.status = 'in_progress' AND item.leased_until < now())
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM sync_batch_items AS prior
                    WHERE prior.workspace_id = item.workspace_id
                      AND prior.partition_key = item.partition_key
                      AND prior.seq < item.seq
                      AND prior.status NOT IN ('done', 'dead_letter')
                  )
                ORDER BY item.virtual_shard, item.partition_key, item.seq, item.created_at, item.id
                FOR UPDATE OF item SKIP LOCKED
                LIMIT :limit
            )
            UPDATE sync_batch_items AS item
            SET status = 'in_progress',
                worker_id = :worker_id,
                leased_until = now() + (:lease_seconds * interval '1 second'),
                attempts = item.attempts + 1,
                updated_at = now()
            FROM candidates
            WHERE item.id = candidates.id
              AND item.created_at = candidates.created_at
            RETURNING item.id,
                      item.created_at,
                      item.batch_id,
                      item.workspace_id,
                      item.stream,
                      item.partition_key,
                      item.virtual_shard,
                      item.seq,
                      item.event_type,
                      item.idempotency_key,
                      item.payload_json,
                      item.status,
                      item.worker_id,
                      item.leased_until,
                      item.attempts
            """
        ),
        {
            "shard_ids": owned_shards,
            "limit": max(1, int(limit or 100)),
            "worker_id": str(worker_id or "").strip()[:128],
            "lease_seconds": max(1, int(lease_seconds or WORKER_LEASE_SECONDS)),
        },
    )
    return [dict(row._mapping) for row in rows]


def renew_sync_worker_shard_leases(
    db: Session,
    *,
    worker_id: str,
    shard_ids: list[int],
    lease_seconds: int = WORKER_LEASE_SECONDS,
) -> list[int]:
    """Renew DB-backed shard leases and return only shards this worker owns.

    Environment-based shard assignment is not enough in production: overlapping
    processes after deploys can otherwise claim the same virtual shard. The
    advisory lock serializes each shard's lease row update, while the lease row
    gives API/ops code visible queue ownership and expiry state.
    """
    owned: list[int] = []
    safe_worker_id = str(worker_id or "").strip()[:128]
    safe_lease_seconds = max(1, int(lease_seconds or WORKER_LEASE_SECONDS))
    for shard_id in [int(item) for item in shard_ids if int(item) >= 0]:
        lock_key = shard_advisory_lock_key(shard_id)
        locked = db.scalar(
            text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )
        if not locked:
            continue
        row = db.execute(
            text(
                """
                INSERT INTO sync_worker_shard_leases (
                    virtual_shard,
                    worker_id,
                    leased_until,
                    heartbeat_at,
                    updated_at
                )
                VALUES (
                    :virtual_shard,
                    :worker_id,
                    now() + (:lease_seconds * interval '1 second'),
                    now(),
                    now()
                )
                ON CONFLICT (virtual_shard) DO UPDATE
                SET worker_id = EXCLUDED.worker_id,
                    leased_until = EXCLUDED.leased_until,
                    heartbeat_at = now(),
                    updated_at = now()
                WHERE sync_worker_shard_leases.worker_id = :worker_id
                   OR sync_worker_shard_leases.leased_until < now()
                RETURNING virtual_shard
                """
            ),
            {
                "virtual_shard": shard_id,
                "worker_id": safe_worker_id,
                "lease_seconds": safe_lease_seconds,
            },
        ).fetchone()
        if row is not None:
            owned.append(int(row[0]))
    return owned


def heartbeat_sync_batch_items(
    db: Session,
    *,
    worker_id: str,
    item_keys: list[tuple[int, Any]],
    lease_seconds: int = WORKER_LEASE_SECONDS,
) -> int:
    if not item_keys:
        return 0
    values_sql = ", ".join(
        f"(:id_{idx}, :created_at_{idx})"
        for idx in range(len(item_keys))
    )
    params: dict[str, Any] = {
        "worker_id": str(worker_id or "").strip()[:128],
        "lease_seconds": max(1, int(lease_seconds or WORKER_LEASE_SECONDS)),
    }
    for idx, (item_id, created_at) in enumerate(item_keys):
        params[f"id_{idx}"] = int(item_id)
        params[f"created_at_{idx}"] = created_at
    result = db.execute(
        text(
            f"""
            UPDATE sync_batch_items AS item
            SET leased_until = now() + (:lease_seconds * interval '1 second'),
                updated_at = now()
            FROM (VALUES {values_sql}) AS claimed(id, created_at)
            WHERE item.id = claimed.id
              AND item.created_at = claimed.created_at
              AND item.worker_id = :worker_id
              AND item.status = 'in_progress'
            """
        ),
        params,
    )
    return int(result.rowcount or 0)


def process_sync_batch_items_once(
    db: Session,
    *,
    worker_id: str,
    shard_ids: list[int],
    limit: int = 100,
    max_attempts: int = MAX_MATERIALIZE_ATTEMPTS,
) -> dict[str, int]:
    started_at = time.monotonic()
    claimed = claim_sync_batch_items(db, worker_id=worker_id, shard_ids=shard_ids, limit=limit)
    stats = {"claimed": len(claimed), "done": 0, "retry": 0, "dead_letter": 0}
    if claimed:
        db.commit()
    for item in claimed:
        try:
            _materialize_claimed_item(db, item=item, worker_id=worker_id)
        except Exception as exc:
            db.rollback()
            attempts = int(item.get("attempts") or 0)
            if attempts >= max(1, int(max_attempts or MAX_MATERIALIZE_ATTEMPTS)):
                _mark_item_dead_letter(db, item=item, worker_id=worker_id, error=str(exc))
                stats["dead_letter"] += 1
            else:
                _release_item_for_retry(db, item=item, worker_id=worker_id, error=str(exc))
                stats["retry"] += 1
            db.commit()
            continue
        _mark_item_done(db, item=item, worker_id=worker_id)
        _refresh_batch_status(db, str(item.get("batch_id") or ""))
        db.commit()
        stats["done"] += 1
    if stats["claimed"] or stats["retry"] or stats["dead_letter"]:
        logger.info(
            "[CloudSyncWorker] batch worker_id=%s claimed=%s done=%s retry=%s dead_letter=%s elapsed_ms=%s",
            worker_id,
            stats["claimed"],
            stats["done"],
            stats["retry"],
            stats["dead_letter"],
            int(round((time.monotonic() - started_at) * 1000)),
        )
    return stats


def run_sync_v2_worker(
    *,
    session_factory,
    worker_id: str,
    shard_ids: list[int],
    poll_seconds: float = 1.0,
    batch_limit: int = 100,
    stop_after: int | None = None,
    statement_timeout_ms: int | None = None,
) -> None:
    iterations = 0
    while True:
        with session_factory() as db:
            if statement_timeout_ms is not None:
                _apply_statement_timeout(db, statement_timeout_ms)
            stats = process_sync_batch_items_once(
                db,
                worker_id=worker_id,
                shard_ids=shard_ids,
                limit=batch_limit,
            )
        iterations += 1
        if stop_after is not None and iterations >= int(stop_after):
            return
        if stats.get("claimed", 0) <= 0:
            time.sleep(max(0.1, float(poll_seconds or 1.0)))


def _apply_statement_timeout(db: Session, timeout_ms: int) -> None:
    """Set a per-session Postgres statement timeout for worker sessions.

    Postgres does not accept bind parameters in SET statements, so clamp to an
    integer before formatting the literal into SQL.
    """
    safe_timeout_ms = max(0, int(timeout_ms))
    db.execute(text(f"SET statement_timeout = {safe_timeout_ms}"))


def _materialize_claimed_item(db: Session, *, item: dict[str, Any], worker_id: str) -> None:
    batch = db.get(SyncBatch, str(item.get("batch_id") or ""))
    if batch is None:
        raise RuntimeError(f"sync batch not found: {item.get('batch_id')}")
    user = db.get(User, int(batch.user_id))
    if user is None:
        raise RuntimeError(f"sync batch user not found: {batch.user_id}")
    event_type = str(item.get("event_type") or "").strip()
    payload = item.get("payload_json") if isinstance(item.get("payload_json"), dict) else {}
    event = SyncEventIn(
        event_type=event_type,
        idempotency_key=str(item.get("idempotency_key") or "").strip(),
        payload=_sanitize_sync_event_payload(event_type, payload),
    )
    if not _mirror_legacy_sync_event(db, user=user, event=event):
        return
    _materialize_known_event(db, user, event)


def _mirror_legacy_sync_event(db: Session, *, user: User, event: SyncEventIn) -> bool:
    stmt = (
        insert(SyncEvent)
        .values(
            workspace_id=user.workspace_id,
            user_id=user.id,
            event_type=event.event_type,
            idempotency_key=event.idempotency_key,
            payload_json=event.payload,
        )
        .on_conflict_do_nothing(index_elements=["workspace_id", "idempotency_key"])
        .returning(SyncEvent.id)
    )
    return db.scalar(stmt) is not None


def _mark_item_done(db: Session, *, item: dict[str, Any], worker_id: str) -> int:
    result = db.execute(
        text(
            """
            UPDATE sync_batch_items
            SET status = 'done',
                worker_id = NULL,
                leased_until = NULL,
                last_error = NULL,
                updated_at = now()
            WHERE id = :id
              AND created_at = :created_at
              AND worker_id = :worker_id
              AND status = 'in_progress'
            """
        ),
        _item_key_params(item, worker_id=worker_id),
    )
    return int(result.rowcount or 0)


def _release_item_for_retry(db: Session, *, item: dict[str, Any], worker_id: str, error: str) -> int:
    result = db.execute(
        text(
            """
            UPDATE sync_batch_items
            SET status = 'pending',
                worker_id = NULL,
                leased_until = NULL,
                last_error = :error,
                updated_at = now()
            WHERE id = :id
              AND created_at = :created_at
              AND worker_id = :worker_id
              AND status = 'in_progress'
            """
        ),
        {**_item_key_params(item, worker_id=worker_id), "error": str(error or "")[:4000]},
    )
    return int(result.rowcount or 0)


def _mark_item_dead_letter(db: Session, *, item: dict[str, Any], worker_id: str, error: str) -> int:
    params = {
        **_item_key_params(item, worker_id=worker_id),
        "workspace_id": int(item.get("workspace_id") or 0),
        "batch_id": str(item.get("batch_id") or ""),
        "event_type": str(item.get("event_type") or ""),
        "idempotency_key": str(item.get("idempotency_key") or ""),
        "partition_key": str(item.get("partition_key") or ""),
        "attempts": int(item.get("attempts") or 0),
        "error": str(error or "")[:4000],
    }
    dead_letter_id = db.scalar(
        text(
            """
            INSERT INTO sync_dead_letters (
                item_id,
                item_created_at,
                workspace_id,
                batch_id,
                event_type,
                idempotency_key,
                partition_key,
                attempts,
                last_error
            )
            VALUES (
                :id,
                :created_at,
                :workspace_id,
                :batch_id,
                :event_type,
                :idempotency_key,
                :partition_key,
                :attempts,
                :error
            )
            RETURNING id
            """
        ),
        params,
    )
    if dead_letter_id is not None:
        db.execute(
            text(
                """
                INSERT INTO dead_letter_attempts (dead_letter_id, attempt_number, error)
                VALUES (:dead_letter_id, :attempts, :error)
                """
            ),
            {**params, "dead_letter_id": int(dead_letter_id)},
        )
    result = db.execute(
        text(
            """
            UPDATE sync_batch_items
            SET status = 'dead_letter',
                worker_id = NULL,
                leased_until = NULL,
                last_error = :error,
                updated_at = now()
            WHERE id = :id
              AND created_at = :created_at
              AND worker_id = :worker_id
              AND status = 'in_progress'
            """
        ),
        params,
    )
    _refresh_batch_status(db, str(item.get("batch_id") or ""))
    return int(result.rowcount or 0)


def _refresh_batch_status(db: Session, batch_id: str) -> None:
    if not batch_id:
        return
    db.execute(
        text(
            """
            UPDATE sync_batches
            SET status = CASE
                WHEN EXISTS (
                    SELECT 1 FROM sync_batch_items
                    WHERE batch_id = :batch_id
                      AND status = 'dead_letter'
                ) THEN 'completed_with_errors'
                ELSE 'done'
            END
            WHERE id = :batch_id
              AND NOT EXISTS (
                  SELECT 1 FROM sync_batch_items
                  WHERE batch_id = :batch_id
                    AND status IN ('pending', 'in_progress')
              )
            """
        ),
        {"batch_id": batch_id},
    )


def _item_key_params(item: dict[str, Any], *, worker_id: str) -> dict[str, Any]:
    return {
        "id": int(item.get("id") or 0),
        "created_at": item.get("created_at"),
        "worker_id": str(worker_id or "").strip()[:128],
    }
