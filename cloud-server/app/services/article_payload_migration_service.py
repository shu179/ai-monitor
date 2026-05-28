from __future__ import annotations

import hashlib
import json
import logging
import os
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models import Article, ArticleVersion, ArticlesMigrationState, ObjectManifest
from app.services.object_storage_service import (
    OBJECT_MANIFEST_STATUS_ACTIVE,
    local_object_path,
    object_storage_key,
)
from app.services.sync_v2_service import LIMITS

logger = logging.getLogger(__name__)

MIGRATION_STATUS_COMPLETED = "completed"
MIGRATION_STATUS_FAILED = "failed"
MIGRATION_STATUS_SKIPPED = "skipped"
ARTICLE_TEXT_CONTENT_TYPE = "text/plain"
ARTICLE_TEXT_COMPRESSION = "zlib"
ARTICLE_MIGRATION_MAX_ATTEMPTS = 5
ARTICLE_MIGRATION_CANDIDATE_KEYS = (
    "content",
    "body",
    "text",
    "full_text",
    "article_text",
    "markdown",
    "html",
    "raw_content",
)

# Background backfill must yield to live traffic. These bound the adaptive
# throttle the migration runner applies between batches.
MIGRATION_DEFAULT_MAX_ACTIVE_BACKENDS = 20
MIGRATION_MAX_BACKOFF_SECONDS = 30.0
MIGRATION_SECONDS_PER_BACKEND_OVER_BUDGET = 1.0


@dataclass(frozen=True)
class ArticlePayloadText:
    text: str
    source_key: str


def migrate_article_payloads_once(
    db: Session,
    *,
    limit: int = 100,
    inline_threshold_bytes: int | None = None,
    settings: Settings | None = None,
) -> dict[str, int]:
    """Migrate one bounded batch of article payload text into article_versions.

    The migration is resumable via ``articles_migration_state``. It never clears
    ``articles.payload_json``; existing v1 read paths remain a fallback until
    shadow checks say the migration is complete.
    """
    safe_limit = max(1, min(int(limit or 100), 1000))
    safe_inline_threshold = int(inline_threshold_bytes or LIMITS["inline_blob_max_bytes"])
    rows = _candidate_articles(db, limit=safe_limit)
    stats = {"scanned": len(rows), "completed": 0, "skipped": 0, "failed": 0}
    for article in rows:
        try:
            outcome = _migrate_one_article(
                db,
                article,
                inline_threshold_bytes=safe_inline_threshold,
                settings=settings,
            )
            stats[outcome] += 1
            db.commit()
        except Exception as exc:
            db.rollback()
            _mark_article_migration_failed(db, article_id=int(article.id), error=str(exc))
            db.commit()
            stats["failed"] += 1
    if stats["scanned"]:
        logger.info("[ArticlePayloadMigration] stats=%s", stats)
    return stats


def _candidate_articles(db: Session, *, limit: int) -> list[Article]:
    rows = db.scalars(
        select(Article)
        .outerjoin(ArticlesMigrationState, ArticlesMigrationState.article_id == Article.id)
        .where(
            ArticlesMigrationState.article_id.is_(None)
            | ArticlesMigrationState.status.in_(("pending", MIGRATION_STATUS_FAILED)),
            func.coalesce(ArticlesMigrationState.attempts, 0) < ARTICLE_MIGRATION_MAX_ATTEMPTS,
        )
        .order_by(Article.updated_at.asc(), Article.id.asc())
        .limit(limit)
    )
    return list(rows)


def _migrate_one_article(
    db: Session,
    article: Article,
    *,
    inline_threshold_bytes: int,
    settings: Settings | None,
) -> str:
    payload_text = extract_article_payload_text(article.payload_json)
    if payload_text is None:
        _upsert_migration_state(
            db,
            article_id=int(article.id),
            status=MIGRATION_STATUS_SKIPPED,
            sha256=None,
            error="no migratable article text in payload_json",
        )
        return "skipped"
    encoded = payload_text.text.encode("utf-8")
    sha256 = hashlib.sha256(encoded).hexdigest()
    existing_version = db.scalar(
        select(ArticleVersion.id).where(
            ArticleVersion.article_id == int(article.id),
            ArticleVersion.version == 1,
        )
    )
    if existing_version is None:
        if len(encoded) <= int(inline_threshold_bytes):
            db.add(
                ArticleVersion(
                    workspace_id=int(article.workspace_id),
                    article_id=int(article.id),
                    version=1,
                    inline_text=payload_text.text,
                    content_object_id=None,
                    content_sha256=sha256,
                    size_bytes=len(encoded),
                )
            )
        else:
            manifest = _ensure_article_text_object(
                db,
                workspace_id=int(article.workspace_id),
                sha256=sha256,
                raw_bytes=encoded,
                settings=settings,
            )
            db.add(
                ArticleVersion(
                    workspace_id=int(article.workspace_id),
                    article_id=int(article.id),
                    version=1,
                    inline_text=None,
                    content_object_id=str(manifest.id),
                    content_sha256=sha256,
                    size_bytes=len(encoded),
                )
            )
    _upsert_migration_state(
        db,
        article_id=int(article.id),
        status=MIGRATION_STATUS_COMPLETED,
        sha256=sha256,
        error=None,
    )
    return "completed"


def extract_article_payload_text(payload_json: Any) -> ArticlePayloadText | None:
    payload = payload_json if isinstance(payload_json, dict) else {}
    for key in ARTICLE_MIGRATION_CANDIDATE_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return ArticlePayloadText(text=value, source_key=key)
    nested = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    for key in ARTICLE_MIGRATION_CANDIDATE_KEYS:
        value = nested.get(key)
        if isinstance(value, str) and value.strip():
            return ArticlePayloadText(text=value, source_key=f"payload.{key}")
    return None


def _ensure_article_text_object(
    db: Session,
    *,
    workspace_id: int,
    sha256: str,
    raw_bytes: bytes,
    settings: Settings | None,
) -> ObjectManifest:
    manifest = db.scalar(
        select(ObjectManifest).where(
            ObjectManifest.workspace_id == int(workspace_id),
            ObjectManifest.sha256 == sha256,
            ObjectManifest.status == OBJECT_MANIFEST_STATUS_ACTIVE,
        )
    )
    if manifest is not None:
        return manifest
    storage_bytes = zlib.compress(raw_bytes)
    storage_key = object_storage_key(workspace_id, sha256)
    _write_local_article_object(storage_key, storage_bytes, settings=settings)
    manifest = ObjectManifest(
        id=str(uuid4()),
        workspace_id=int(workspace_id),
        sha256=sha256,
        size_bytes=len(raw_bytes),
        storage_size_bytes=len(storage_bytes),
        content_type=ARTICLE_TEXT_CONTENT_TYPE,
        storage_key=storage_key,
        compression=ARTICLE_TEXT_COMPRESSION,
        ref_count=1,
        status=OBJECT_MANIFEST_STATUS_ACTIVE,
    )
    db.add(manifest)
    return manifest


def _write_local_article_object(storage_key: str, data: bytes, *, settings: Settings | None) -> None:
    path = local_object_path(storage_key, settings=settings or get_settings())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + f".tmp-{uuid4().hex}")
    try:
        tmp_path.write_bytes(data)
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _upsert_migration_state(
    db: Session,
    *,
    article_id: int,
    status: str,
    sha256: str | None,
    error: str | None,
) -> None:
    stmt = (
        insert(ArticlesMigrationState)
        .values(
            article_id=int(article_id),
            status=status,
            sha256=sha256,
            attempts=1,
            last_error=error,
            completed_at=func.now() if status in {MIGRATION_STATUS_COMPLETED, MIGRATION_STATUS_SKIPPED} else None,
            updated_at=func.now(),
        )
        .on_conflict_do_update(
            index_elements=["article_id"],
            set_={
                "status": status,
                "sha256": sha256,
                "attempts": ArticlesMigrationState.attempts + 1,
                "last_error": error,
                "completed_at": func.now() if status in {MIGRATION_STATUS_COMPLETED, MIGRATION_STATUS_SKIPPED} else None,
                "updated_at": func.now(),
            },
        )
    )
    db.execute(stmt)


def _mark_article_migration_failed(db: Session, *, article_id: int, error: str) -> None:
    _upsert_migration_state(
        db,
        article_id=int(article_id),
        status=MIGRATION_STATUS_FAILED,
        sha256=None,
        error=str(error or "")[:4000],
    )


def build_article_payload_migration_report(db: Session) -> dict[str, Any]:
    rows = db.execute(
        text(
            """
            SELECT status, count(*) AS count
            FROM articles_migration_state
            GROUP BY status
            ORDER BY status
            """
        )
    ).all()
    versions = int(db.scalar(select(func.count()).select_from(ArticleVersion)) or 0)
    objects = int(
        db.scalar(
            select(func.count())
            .select_from(ArticleVersion)
            .where(ArticleVersion.content_object_id.is_not(None))
        )
        or 0
    )
    return {
        "status_counts": {str(row.status): int(row.count or 0) for row in rows},
        "article_versions": versions,
        "object_backed_versions": objects,
    }


def measure_active_backends(db: Session) -> int:
    """Return how many other Postgres backends are currently executing.

    Used by the article backfill runner to back off when live traffic is busy.
    Degrades to 0 (no throttle) on engines without ``pg_stat_activity`` or on
    any error, so the migration never crashes because of the probe itself.
    """
    try:
        value = db.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE state = 'active' AND pid <> pg_backend_pid()"
            )
        ).scalar_one()
    except Exception:  # pragma: no cover - defensive: sqlite/permission/etc.
        return 0
    return max(0, int(value or 0))


def compute_throttle_delay(
    active_backends: int,
    *,
    max_active: int = MIGRATION_DEFAULT_MAX_ACTIVE_BACKENDS,
    max_backoff_seconds: float = MIGRATION_MAX_BACKOFF_SECONDS,
    seconds_per_backend: float = MIGRATION_SECONDS_PER_BACKEND_OVER_BUDGET,
) -> float:
    """Map current DB load to a delay (seconds) before the next backfill batch.

    At or below ``max_active`` the backfill runs full speed (delay 0). Above it
    the delay grows linearly with how far over budget we are, capped at
    ``max_backoff_seconds`` so a busy database simply pauses the backfill instead
    of competing with live queries.
    """
    safe_max_active = max(1, int(max_active))
    safe_active = max(0, int(active_backends))
    if safe_active <= safe_max_active:
        return 0.0
    over = safe_active - safe_max_active
    delay = float(over) * max(0.0, float(seconds_per_backend))
    return min(delay, max(0.0, float(max_backoff_seconds)))
