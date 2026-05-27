from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


def build_shadow_reconcile_report(db: Session, *, sample_limit: int = 20) -> dict[str, Any]:
    """Build a read-only v1/v2 consistency report for cutover checks."""
    safe_sample_limit = max(1, min(int(sample_limit or 20), 200))
    entity_counts = _entity_counts(db)
    change_counts = _change_counts(db)
    migration = _migration_summary(db)
    coverage = _coverage(entity_counts=entity_counts, change_counts=change_counts)
    samples = _sample_digests(db, limit=safe_sample_limit)
    mismatches = _mismatches(coverage=coverage, migration=migration)
    return {
        "status": "ok" if not mismatches else "warn",
        "entity_counts": entity_counts,
        "change_counts": change_counts,
        "coverage": coverage,
        "migration": migration,
        "samples": samples,
        "mismatches": mismatches,
    }


def format_shadow_reconcile_report(report: dict[str, Any]) -> str:
    lines = [f"Shadow reconcile: status={report['status']}"]
    counts = report["entity_counts"]
    lines.append(
        "entities="
        f"tasks={counts['tasks']} "
        f"runs={counts['runs']} "
        f"articles={counts['articles']} "
        f"article_links={counts['article_links']} "
        f"references={counts['references']} "
        f"agent_commands={counts['agent_commands']} "
        f"article_versions={counts['article_versions']}"
    )
    changes = report["change_counts"]
    lines.append(
        "changes="
        f"tasks={changes.get('tasks', 0)} "
        f"runs={changes.get('runs', 0)} "
        f"articles={changes.get('articles', 0)} "
        f"references={changes.get('references', 0)} "
        f"profile={changes.get('profile', 0)} "
        f"agent_status={changes.get('agent_status', 0)}"
    )
    migration = report["migration"]
    lines.append(
        "migration="
        f"states={migration['state_counts']} "
        f"payload_candidates={migration['payload_candidates']} "
        f"missing_versions={migration['missing_versions']} "
        f"fallback_required={migration['fallback_required']}"
    )
    if report["mismatches"]:
        lines.append("mismatches:")
        for item in report["mismatches"]:
            lines.append(f"  {item['code']}: {item['message']}")
    else:
        lines.append("mismatches: none")
    if report["samples"]:
        lines.append("samples:")
        for sample in report["samples"]:
            lines.append(
                "  "
                f"{sample['table']} workspace={sample['workspace_id']} "
                f"rows={sample['rows']} digest={sample['digest']}"
            )
    else:
        lines.append("samples: none")
    return "\n".join(lines)


def _entity_counts(db: Session) -> dict[str, int]:
    rows = db.execute(
        text(
            """
            SELECT 'tasks' AS name, count(*) AS count FROM brand_tasks
            UNION ALL SELECT 'runs', count(*) FROM run_records
            UNION ALL SELECT 'articles', count(*) FROM articles
            UNION ALL SELECT 'article_links', count(*) FROM article_task_links
            UNION ALL SELECT 'references', count(*) FROM article_reference_events
            UNION ALL SELECT 'agent_commands', count(*) FROM agent_commands
            UNION ALL SELECT 'article_versions', count(*) FROM article_versions
            """
        )
    ).all()
    return {str(row.name): int(row.count or 0) for row in rows}


def _change_counts(db: Session) -> dict[str, int]:
    rows = db.execute(
        text(
            """
            SELECT stream, count(*) AS count, max(seq) AS max_seq
            FROM workspace_change_log
            GROUP BY stream
            ORDER BY stream
            """
        )
    ).all()
    out: dict[str, int] = {}
    for row in rows:
        out[str(row.stream)] = int(row.count or 0)
        out[f"{row.stream}_max_seq"] = int(row.max_seq or 0)
    return out


def _migration_summary(db: Session) -> dict[str, Any]:
    state_rows = db.execute(
        text(
            """
            SELECT status, count(*) AS count
            FROM articles_migration_state
            GROUP BY status
            ORDER BY status
            """
        )
    ).all()
    payload_candidates = int(db.execute(text("SELECT count(*) FROM articles WHERE payload_json <> '{}'::jsonb")).scalar_one() or 0)
    pending_versions = int(
        db.execute(
            text(
                """
                SELECT count(*)
                FROM articles a
                LEFT JOIN article_versions v ON v.article_id = a.id
                LEFT JOIN articles_migration_state s ON s.article_id = a.id
                WHERE a.payload_json <> '{}'::jsonb
                  AND v.article_id IS NULL
                  AND coalesce(s.status, '') NOT IN ('completed', 'skipped')
                """
            )
        ).scalar_one()
        or 0
    )
    state_counts = {str(row.status): int(row.count or 0) for row in state_rows}
    return {
        "state_counts": state_counts,
        "payload_candidates": payload_candidates,
        "missing_versions": pending_versions,
        "fallback_required": pending_versions > 0,
    }


def _coverage(*, entity_counts: dict[str, int], change_counts: dict[str, int]) -> dict[str, dict[str, int | bool]]:
    checks = {
        "tasks": ("tasks", "tasks"),
        "runs": ("runs", "runs"),
        "articles": ("articles", "articles"),
        "references": ("references", "references"),
        "agent_status": ("agent_commands", "agent_status"),
    }
    out: dict[str, dict[str, int | bool]] = {}
    for name, (entity_key, stream) in checks.items():
        entities = int(entity_counts.get(entity_key) or 0)
        changes = int(change_counts.get(stream) or 0)
        out[name] = {
            "entities": entities,
            "changes": changes,
            "covered": entities == 0 or changes > 0,
        }
    return out


def _sample_digests(db: Session, *, limit: int) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for table, query in {
        "run_records": """
            SELECT workspace_id, id, idempotency_key, result_json::text AS payload
            FROM run_records
            ORDER BY id DESC
            LIMIT :limit
        """,
        "articles": """
            SELECT workspace_id, id, url_hash AS idempotency_key, payload_json::text AS payload
            FROM articles
            ORDER BY id DESC
            LIMIT :limit
        """,
        "article_reference_events": """
            SELECT workspace_id, id, idempotency_key, event_json::text AS payload
            FROM article_reference_events
            ORDER BY id DESC
            LIMIT :limit
        """,
    }.items():
        rows = db.execute(text(query), {"limit": limit}).all()
        if not rows:
            continue
        samples.append({
            "table": table,
            "workspace_id": int(getattr(rows[0], "workspace_id", 0) or 0),
            "rows": len(rows),
            "digest": _rows_digest(rows),
        })
    return samples


def _rows_digest(rows: list[Any]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        payload = {
            "workspace_id": int(getattr(row, "workspace_id", 0) or 0),
            "id": str(getattr(row, "id", "")),
            "idempotency_key": str(getattr(row, "idempotency_key", "")),
            "payload": str(getattr(row, "payload", "")),
        }
        digest.update(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _mismatches(*, coverage: dict[str, dict[str, int | bool]], migration: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for stream, row in sorted(coverage.items()):
        if not bool(row["covered"]):
            out.append({
                "code": f"{stream}.missing_change_log",
                "message": f"{stream} has {row['entities']} entity rows but no change log rows",
            })
    if int(migration["missing_versions"] or 0) > 0:
        out.append({
            "code": "articles.payload_migration_incomplete",
            "message": f"{migration['missing_versions']} article payload rows still need article_versions",
        })
    return out
