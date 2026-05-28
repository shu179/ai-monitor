from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

REQUEUEABLE_STATUSES = ("blocked", "dead_letter")
logger = logging.getLogger(__name__)


def requeue_sync_queue_items(
    db: Session,
    *,
    workspace_id: int,
    statuses: list[str] | None = None,
    partition_key: str = "",
    limit: int = 100,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Requeue bounded sync-v2 materialization items for admin recovery.

    This is intentionally narrow: callers must scope by workspace, can further
    narrow to one partition_key, and dry_run remains the default so diagnostics
    tooling can show the blast radius before mutating queue state.
    """
    safe_workspace_id = int(workspace_id or 0)
    if safe_workspace_id <= 0:
        raise ValueError("workspace_id is required")
    safe_statuses = [
        str(item or "").strip()
        for item in (statuses or list(REQUEUEABLE_STATUSES))
        if str(item or "").strip() in REQUEUEABLE_STATUSES
    ]
    if not safe_statuses:
        safe_statuses = list(REQUEUEABLE_STATUSES)
    safe_partition_key = str(partition_key or "").strip()
    safe_limit = max(1, min(int(limit or 100), 1000))
    params: dict[str, Any] = {
        "workspace_id": safe_workspace_id,
        "statuses": safe_statuses,
        "partition_key": safe_partition_key,
        "limit": safe_limit,
    }
    partition_predicate = "AND partition_key = :partition_key" if safe_partition_key else ""
    rows = db.execute(
        text(
            f"""
            SELECT id, created_at, batch_id, status, partition_key
            FROM sync_batch_items
            WHERE workspace_id = :workspace_id
              AND status = ANY(:statuses)
              {partition_predicate}
            ORDER BY created_at, id
            LIMIT :limit
            """
        ),
        params,
    ).all()
    selected = [
        {
            "id": int(row.id),
            "created_at": row.created_at.isoformat() if row.created_at else "",
            "batch_id": str(row.batch_id or ""),
            "status": str(row.status or ""),
            "partition_key": str(row.partition_key or ""),
        }
        for row in rows
    ]
    if not selected or dry_run:
        return {
            "dry_run": bool(dry_run),
            "workspace_id": safe_workspace_id,
            "statuses": safe_statuses,
            "partition_key": safe_partition_key,
            "limit": safe_limit,
            "selected": len(selected),
            "requeued": 0,
            "batches_updated": 0,
            "items": selected,
        }
    values_sql = ", ".join(f"(:id_{idx}, :created_at_{idx})" for idx in range(len(rows)))
    update_params: dict[str, Any] = {}
    for idx, row in enumerate(rows):
        update_params[f"id_{idx}"] = int(row.id)
        update_params[f"created_at_{idx}"] = row.created_at
    result = db.execute(
        text(
            f"""
            UPDATE sync_batch_items AS item
            SET status = 'pending',
                worker_id = NULL,
                leased_until = NULL,
                last_error = NULL,
                updated_at = now()
            FROM (VALUES {values_sql}) AS selected(id, created_at)
            WHERE item.id = selected.id
              AND item.created_at = selected.created_at
            """
        ),
        update_params,
    )
    batch_ids = sorted({str(row.batch_id or "") for row in rows if str(row.batch_id or "").strip()})
    batches_updated = _mark_batches_requeued(db, batch_ids=batch_ids)
    db.commit()
    logger.info(
        "[CloudSyncQueueOps] requeue workspace_id=%s dry_run=%s statuses=%s partition_key=%s "
        "selected=%s requeued=%s batches=%s",
        safe_workspace_id,
        False,
        ",".join(safe_statuses),
        safe_partition_key or "*",
        len(selected),
        int(result.rowcount or 0),
        batches_updated,
    )
    return {
        "dry_run": False,
        "workspace_id": safe_workspace_id,
        "statuses": safe_statuses,
        "partition_key": safe_partition_key,
        "limit": safe_limit,
        "selected": len(selected),
        "requeued": int(result.rowcount or 0),
        "batches_updated": batches_updated,
        "items": selected,
    }


def _mark_batches_requeued(db: Session, *, batch_ids: list[str]) -> int:
    safe_ids = [str(item or "").strip() for item in batch_ids if str(item or "").strip()]
    if not safe_ids:
        return 0
    result = db.execute(
        text(
            """
            UPDATE sync_batches
            SET status = 'accepted'
            WHERE id = ANY(:batch_ids)
              AND status IN ('completed_with_errors', 'done')
            """
        ),
        {"batch_ids": safe_ids},
    )
    return int(result.rowcount or 0)
