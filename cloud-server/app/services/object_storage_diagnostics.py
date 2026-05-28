from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models import ObjectManifest
from app.services.object_storage_service import (
    OBJECT_MANIFEST_STATUS_ACTIVE,
    OBJECT_MANIFEST_STATUS_DELETING,
    local_object_path,
)


def build_object_storage_report(db: Session, *, settings: Settings | None = None) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    root = Path(str(resolved_settings.object_storage_local_dir or "/opt/surfaced/object-data")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(root)
    local_size = _directory_size(root)
    active_manifests = _active_manifest_rows(db)
    manifest_total = sum(int(row["storage_size_bytes"]) for row in active_manifests)
    workspace_usage = _workspace_usage(db)
    missing_files, manifest_paths = _missing_files(active_manifests, settings=resolved_settings)
    orphan_files = _orphan_files(root, manifest_paths)
    limits = {
        "total_quota_bytes": int(resolved_settings.object_storage_total_quota_bytes or 0),
        "workspace_quota_bytes": int(resolved_settings.object_storage_workspace_quota_bytes or 0),
        "max_file_bytes": int(resolved_settings.object_storage_max_file_bytes or 0),
        "min_free_bytes": int(resolved_settings.object_storage_min_free_bytes or 0),
    }
    status = _status(
        disk_free_bytes=int(usage.free),
        local_size_bytes=local_size,
        manifest_total_bytes=manifest_total,
        limits=limits,
        missing_count=len(missing_files),
        orphan_count=len(orphan_files),
    )
    return {
        "status": status,
        "root": str(root),
        "disk": {
            "total_bytes": int(usage.total),
            "used_bytes": int(usage.used),
            "free_bytes": int(usage.free),
        },
        "limits": limits,
        "local_size_bytes": local_size,
        "manifest_total_bytes": manifest_total,
        "manifest_count": len(active_manifests),
        "workspace_usage": workspace_usage,
        "missing_files": missing_files,
        "orphan_files": orphan_files,
    }


def format_object_storage_report(report: dict[str, Any]) -> str:
    disk = report["disk"]
    limits = report["limits"]
    lines = [
        f"Object storage doctor: status={report['status']}",
        f"root={report['root']}",
        "disk="
        f"total={_format_bytes(disk['total_bytes'])} "
        f"used={_format_bytes(disk['used_bytes'])} "
        f"free={_format_bytes(disk['free_bytes'])}",
        "limits="
        f"total_quota={_format_bytes(limits['total_quota_bytes'])} "
        f"workspace_quota={_format_bytes(limits['workspace_quota_bytes'])} "
        f"max_file={_format_bytes(limits['max_file_bytes'])} "
        f"min_free={_format_bytes(limits['min_free_bytes'])}",
        "usage="
        f"files={_format_bytes(report['local_size_bytes'])} "
        f"manifests={_format_bytes(report['manifest_total_bytes'])} "
        f"manifest_count={report['manifest_count']}",
    ]
    if report["workspace_usage"]:
        lines.append("workspace_usage:")
        for row in report["workspace_usage"]:
            lines.append(
                "  "
                f"workspace={row['workspace_id']} "
                f"bytes={_format_bytes(row['storage_size_bytes'])} "
                f"objects={row['object_count']}"
            )
    else:
        lines.append("workspace_usage: none")
    if report["missing_files"]:
        lines.append("missing_files:")
        for item in report["missing_files"][:20]:
            lines.append(f"  object={item['object_id']} workspace={item['workspace_id']} path={item['path']}")
        if len(report["missing_files"]) > 20:
            lines.append(f"  ... {len(report['missing_files']) - 20} more")
    else:
        lines.append("missing_files: none")
    if report["orphan_files"]:
        lines.append("orphan_files:")
        for item in report["orphan_files"][:20]:
            lines.append(f"  bytes={_format_bytes(item['size_bytes'])} path={item['path']}")
        if len(report["orphan_files"]) > 20:
            lines.append(f"  ... {len(report['orphan_files']) - 20} more")
    else:
        lines.append("orphan_files: none")
    return "\n".join(lines)


def _active_manifest_rows(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        select(
            ObjectManifest.id,
            ObjectManifest.workspace_id,
            ObjectManifest.storage_key,
            ObjectManifest.storage_size_bytes,
        ).where(ObjectManifest.status.in_((OBJECT_MANIFEST_STATUS_ACTIVE, OBJECT_MANIFEST_STATUS_DELETING)))
    ).all()
    return [
        {
            "object_id": str(row.id),
            "workspace_id": int(row.workspace_id),
            "storage_key": str(row.storage_key),
            "storage_size_bytes": int(row.storage_size_bytes),
        }
        for row in rows
    ]


def _workspace_usage(db: Session) -> list[dict[str, int]]:
    rows = db.execute(
        select(
            ObjectManifest.workspace_id,
            func.coalesce(func.sum(ObjectManifest.storage_size_bytes), 0).label("storage_size_bytes"),
            func.count(ObjectManifest.id).label("object_count"),
        )
        .where(ObjectManifest.status == OBJECT_MANIFEST_STATUS_ACTIVE)
        .group_by(ObjectManifest.workspace_id)
        .order_by(func.coalesce(func.sum(ObjectManifest.storage_size_bytes), 0).desc())
    ).all()
    return [
        {
            "workspace_id": int(row.workspace_id),
            "storage_size_bytes": int(row.storage_size_bytes or 0),
            "object_count": int(row.object_count or 0),
        }
        for row in rows
    ]


def _missing_files(manifests: list[dict[str, Any]], *, settings: Settings) -> tuple[list[dict[str, Any]], set[Path]]:
    missing: list[dict[str, Any]] = []
    paths: set[Path] = set()
    for row in manifests:
        path = local_object_path(str(row["storage_key"]), settings=settings)
        paths.add(path)
        if not path.exists() or not path.is_file():
            missing.append(
                {
                    "object_id": row["object_id"],
                    "workspace_id": row["workspace_id"],
                    "path": str(path),
                }
            )
    return missing, paths


def _orphan_files(root: Path, manifest_paths: set[Path]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not root.exists():
        return out
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved in manifest_paths:
            continue
        out.append({"path": str(resolved), "size_bytes": int(path.stat().st_size)})
    out.sort(key=lambda item: str(item["path"]))
    return out


def _directory_size(root: Path) -> int:
    if not root.exists():
        return 0
    total = 0
    for path in root.rglob("*"):
        if path.is_file():
            total += int(path.stat().st_size)
    return total


def _status(
    *,
    disk_free_bytes: int,
    local_size_bytes: int,
    manifest_total_bytes: int,
    limits: dict[str, int],
    missing_count: int,
    orphan_count: int,
) -> str:
    if missing_count:
        return "error"
    if disk_free_bytes < int(limits["min_free_bytes"]):
        return "error"
    if int(limits["total_quota_bytes"]) > 0 and local_size_bytes > int(limits["total_quota_bytes"]):
        return "error"
    if orphan_count or local_size_bytes != manifest_total_bytes:
        return "warn"
    return "ok"


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
        if size < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(size)}B"
            return f"{size:.2f}{unit}"
        size /= 1024
