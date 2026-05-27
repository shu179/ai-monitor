from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.change_log_service import (
    STREAM_ARTICLES,
    STREAM_REFERENCES,
    STREAM_RUNS,
    STREAM_TASKS,
    record_workspace_change,
)


BACKFILL_TARGETS = (
    ("tasks", "brand_tasks", STREAM_TASKS, "task.updated"),
    ("runs", "run_records", STREAM_RUNS, "run.record.backfill"),
    ("articles", "articles", STREAM_ARTICLES, "article.upsert.backfill"),
    ("references", "article_reference_events", STREAM_REFERENCES, "article.reference.backfill"),
)


def backfill_workspace_change_logs(db: Session, *, workspace_id: int, limit: int = 500) -> dict[str, int]:
    """Backfill v2 change-log rows for existing v1-era data.

    The operation is intentionally idempotent for the chosen backfill kind/ref_id
    pairs. It records changes without NOTIFY fanout so a one-time historical
    catch-up does not flood active SSE clients.
    """
    safe_workspace_id = int(workspace_id)
    safe_limit = max(1, min(int(limit or 500), 5_000))
    stats: dict[str, int] = {}
    for name, table_name, stream, kind in BACKFILL_TARGETS:
        ref_ids = _missing_ref_ids(
            db,
            workspace_id=safe_workspace_id,
            table_name=table_name,
            stream=stream,
            kind=kind,
            limit=safe_limit,
        )
        for ref_id in ref_ids:
            record_workspace_change(
                db,
                workspace_id=safe_workspace_id,
                stream=stream,
                kind=kind,
                ref_id=ref_id,
                notify=False,
            )
        stats[name] = len(ref_ids)
    return stats


def _missing_ref_ids(
    db: Session,
    *,
    workspace_id: int,
    table_name: str,
    stream: str,
    kind: str,
    limit: int,
) -> list[str]:
    if table_name not in {target[1] for target in BACKFILL_TARGETS}:
        raise ValueError("unsupported backfill table")
    rows = db.execute(
        text(
            f"""
            SELECT e.id::text AS ref_id
            FROM {table_name} e
            WHERE e.workspace_id = :workspace_id
              AND NOT EXISTS (
                  SELECT 1
                  FROM workspace_change_log l
                  WHERE l.workspace_id = e.workspace_id
                    AND l.stream = :stream
                    AND l.kind = :kind
                    AND l.ref_id = e.id::text
              )
            ORDER BY e.id ASC
            LIMIT :limit
            """
        ),
        {
            "workspace_id": int(workspace_id),
            "stream": str(stream),
            "kind": str(kind),
            "limit": int(limit),
        },
    ).scalars()
    return [str(row) for row in rows]
