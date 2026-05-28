from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models import ObjectManifest, ObjectUploadPart, ObjectUploadSession
from app.services.object_storage_diagnostics import build_object_storage_report
from app.services.object_storage_service import (
    OBJECT_UPLOAD_STATUS_INITIATED,
    OBJECT_MANIFEST_STATUS_DELETING,
    local_object_path,
)
from app.services.sync_v2_service import TTL_SECONDS

logger = logging.getLogger(__name__)


def run_cloud_maintenance(
    db: Session,
    *,
    dry_run: bool = True,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Run bounded cloud cleanup tasks.

    The default dry-run mode is safe for doctor/ops calls. Passing
    ``dry_run=False`` deletes only expired housekeeping data or local files that
    are not referenced by active manifests.
    """
    resolved_settings = settings or get_settings()
    now = datetime.now(timezone.utc)
    expired_uploads = _cleanup_expired_upload_sessions(db, now=now, dry_run=dry_run)
    dead_letters = _cleanup_expired_dead_letters(db, now=now, dry_run=dry_run)
    change_logs = _cleanup_expired_change_log(db, now=now, dry_run=dry_run)
    soft_deleted_objects = _cleanup_soft_deleted_objects(db, now=now, settings=resolved_settings, dry_run=dry_run)
    orphan_files = _cleanup_orphan_local_files(db, settings=resolved_settings, dry_run=dry_run)
    if not dry_run:
        db.commit()
    result = {
        "dry_run": bool(dry_run),
        "expired_upload_sessions": expired_uploads,
        "dead_letters": dead_letters,
        "change_log": change_logs,
        "soft_deleted_objects": soft_deleted_objects,
        "orphan_files": orphan_files,
    }
    logger.info(
        "[CloudMaintenance] dry_run=%s expired_upload_sessions=%s upload_parts=%s "
        "dead_letters=%s change_log_rows=%s soft_deleted_objects=%s soft_deleted_bytes=%s "
        "orphan_files=%s orphan_bytes=%s",
        bool(dry_run),
        expired_uploads["sessions"],
        expired_uploads["parts"],
        dead_letters["dead_letters"],
        change_logs["rows"],
        soft_deleted_objects["objects"],
        soft_deleted_objects["bytes"],
        orphan_files["files"],
        orphan_files["bytes"],
    )
    return result


def _cleanup_expired_upload_sessions(db: Session, *, now: datetime, dry_run: bool) -> dict[str, int]:
    session_ids = [
        str(item)
        for item in db.scalars(
            select(ObjectUploadSession.id).where(
                ObjectUploadSession.status == OBJECT_UPLOAD_STATUS_INITIATED,
                ObjectUploadSession.expires_at < now,
            )
        )
    ]
    if not session_ids:
        return {"sessions": 0, "parts": 0}
    parts_count = int(
        db.scalar(
            select(func.count())
            .select_from(ObjectUploadPart)
            .where(ObjectUploadPart.session_id.in_(session_ids))
        )
        or 0
    )
    if not dry_run:
        db.execute(delete(ObjectUploadSession).where(ObjectUploadSession.id.in_(session_ids)))
    return {"sessions": len(session_ids), "parts": parts_count}


def _cleanup_expired_dead_letters(db: Session, *, now: datetime, dry_run: bool) -> dict[str, int]:
    cutoff = now - timedelta(seconds=int(TTL_SECONDS["dead_letter_retention"]))
    count = int(
        db.execute(
            text(
                """
                SELECT count(*)
                FROM sync_dead_letters
                WHERE created_at < :cutoff
                """
            ),
            {"cutoff": cutoff},
        ).scalar_one()
        or 0
    )
    if count and not dry_run:
        db.execute(
            text(
                """
                DELETE FROM sync_dead_letters
                WHERE created_at < :cutoff
                """
            ),
            {"cutoff": cutoff},
        )
    return {"dead_letters": count}


def _cleanup_expired_change_log(db: Session, *, now: datetime, dry_run: bool) -> dict[str, int]:
    cutoff = now - timedelta(seconds=int(TTL_SECONDS["change_log_retention"]))
    count = int(
        db.execute(
            text(
                """
                SELECT count(*)
                FROM workspace_change_log
                WHERE created_at < :cutoff
                """
            ),
            {"cutoff": cutoff},
        ).scalar_one()
        or 0
    )
    if count and not dry_run:
        db.execute(
            text(
                """
                DELETE FROM workspace_change_log
                WHERE created_at < :cutoff
                """
            ),
            {"cutoff": cutoff},
        )
    return {"rows": count}


def _cleanup_soft_deleted_objects(
    db: Session,
    *,
    now: datetime,
    settings: Settings,
    dry_run: bool,
) -> dict[str, int]:
    manifests = list(
        db.scalars(
            select(ObjectManifest).where(
                ObjectManifest.status == OBJECT_MANIFEST_STATUS_DELETING,
                ObjectManifest.deleted_after.is_not(None),
                ObjectManifest.deleted_after <= now,
                ObjectManifest.ref_count <= 0,
            )
        )
    )
    deleted_objects = 0
    deleted_bytes = 0
    root = Path(str(settings.object_storage_local_dir or "/opt/surfaced/object-data")).resolve()
    for manifest in manifests:
        path = local_object_path(str(manifest.storage_key), settings=settings)
        if root not in path.parents and path != root:
            continue
        size_bytes = int(manifest.storage_size_bytes or 0)
        if not dry_run:
            # Re-check the mutable fields immediately before deletion. This
            # keeps the 7-day grace period safe if a reference was restored
            # after this maintenance batch selected the row.
            db.refresh(manifest)
            if int(manifest.ref_count or 0) > 0 or str(manifest.status or "") != OBJECT_MANIFEST_STATUS_DELETING:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            db.delete(manifest)
        deleted_objects += 1
        deleted_bytes += size_bytes
    return {"objects": deleted_objects, "bytes": deleted_bytes}


def _cleanup_orphan_local_files(db: Session, *, settings: Settings, dry_run: bool) -> dict[str, int]:
    report = build_object_storage_report(db, settings=settings)
    deleted_files = 0
    deleted_bytes = 0
    root = Path(str(settings.object_storage_local_dir or "/opt/surfaced/object-data")).resolve()
    for item in report.get("orphan_files", []):
        path = Path(str(item.get("path") or "")).resolve()
        if root not in path.parents and path != root:
            continue
        size_bytes = int(item.get("size_bytes") or 0)
        if not dry_run:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        deleted_files += 1
        deleted_bytes += size_bytes
    return {"files": deleted_files, "bytes": deleted_bytes}
