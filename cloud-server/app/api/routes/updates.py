from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import DbSession
from app.services.update_service import build_update_manifest

router = APIRouter()


@router.get("/manifest")
def update_manifest(db: DbSession, channel: str = "stable") -> dict:
    return build_update_manifest(db, channel=channel)

