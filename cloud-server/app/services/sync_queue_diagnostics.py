from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


def build_sync_queue_report(db: Session) -> dict[str, Any]:
    status_rows = db.execute(
        text(
            """
            SELECT status, count(*) AS count
            FROM sync_batch_items
            GROUP BY status
            ORDER BY status
            """
        )
    ).all()
    pending_age = db.execute(
        text(
            """
            SELECT EXTRACT(EPOCH FROM (now() - min(created_at)))::bigint AS age_seconds
            FROM sync_batch_items
            WHERE status = 'pending'
            """
        )
    ).scalar_one_or_none()
    in_progress_expired = db.execute(
        text(
            """
            SELECT count(*)
            FROM sync_batch_items
            WHERE status = 'in_progress'
              AND leased_until < now()
            """
        )
    ).scalar_one()
    dead_letter_rows = db.execute(
        text(
            """
            SELECT workspace_id, count(*) AS count, max(created_at) AS latest_created_at
            FROM sync_dead_letters
            GROUP BY workspace_id
            ORDER BY count DESC, workspace_id
            LIMIT 20
            """
        )
    ).all()
    shard_rows = db.execute(
        text(
            """
            SELECT
                count(*) AS total,
                count(*) FILTER (WHERE leased_until >= now()) AS active,
                count(*) FILTER (WHERE leased_until < now()) AS expired
            FROM sync_worker_shard_leases
            """
        )
    ).first()
    counts = {str(row.status): int(row.count or 0) for row in status_rows}
    return {
        "status": _status(counts=counts, expired_in_progress=int(in_progress_expired or 0)),
        "counts": {
            "pending": int(counts.get("pending") or 0),
            "in_progress": int(counts.get("in_progress") or 0),
            "done": int(counts.get("done") or 0),
            "dead_letter": int(counts.get("dead_letter") or 0),
        },
        "oldest_pending_age_seconds": int(pending_age or 0),
        "expired_in_progress": int(in_progress_expired or 0),
        "shard_leases": {
            "total": int(getattr(shard_rows, "total", 0) or 0),
            "active": int(getattr(shard_rows, "active", 0) or 0),
            "expired": int(getattr(shard_rows, "expired", 0) or 0),
        },
        "dead_letters_by_workspace": [
            {
                "workspace_id": int(row.workspace_id),
                "count": int(row.count or 0),
                "latest_created_at": row.latest_created_at.isoformat() if row.latest_created_at else "",
            }
            for row in dead_letter_rows
        ],
    }


def format_sync_queue_report(report: dict[str, Any]) -> str:
    counts = report["counts"]
    leases = report["shard_leases"]
    lines = [
        f"Sync queue doctor: status={report['status']}",
        "counts="
        f"pending={counts['pending']} "
        f"in_progress={counts['in_progress']} "
        f"done={counts['done']} "
        f"dead_letter={counts['dead_letter']}",
        f"oldest_pending_age_seconds={report['oldest_pending_age_seconds']}",
        f"expired_in_progress={report['expired_in_progress']}",
        "shard_leases="
        f"total={leases['total']} "
        f"active={leases['active']} "
        f"expired={leases['expired']}",
    ]
    if report["dead_letters_by_workspace"]:
        lines.append("dead_letters_by_workspace:")
        for row in report["dead_letters_by_workspace"]:
            lines.append(
                "  "
                f"workspace={row['workspace_id']} "
                f"count={row['count']} "
                f"latest={row['latest_created_at']}"
            )
    else:
        lines.append("dead_letters_by_workspace: none")
    return "\n".join(lines)


def _status(*, counts: dict[str, int], expired_in_progress: int) -> str:
    if int(counts.get("dead_letter") or 0) or int(expired_in_progress or 0):
        return "warn"
    return "ok"
