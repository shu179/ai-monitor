from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import UpdatePackage


def build_update_manifest(db: Session, channel: str = "stable") -> dict:
    rows = list(
        db.scalars(
            select(UpdatePackage)
            .where(UpdatePackage.channel == channel)
            .order_by(UpdatePackage.created_at.desc())
        )
    )
    latest_by_channel: dict[str, dict] = defaultdict(lambda: {"packages": {}})
    selected_version = rows[0].version if rows else ""
    for row in rows:
        if selected_version and row.version != selected_version:
            continue
        bucket = latest_by_channel[row.channel]
        bucket["version"] = row.version
        bucket["channel"] = row.channel
        bucket["published_at"] = row.published_at.isoformat() if row.published_at else None
        bucket["notes"] = row.notes
        package = {
            "url": row.download_url,
            "sha256": row.sha256,
        }
        if row.signature:
            package["signature"] = row.signature
        bucket["packages"][row.platform_key] = package
    return {"app_name": "Surfaced", "channels": dict(latest_by_channel)}

