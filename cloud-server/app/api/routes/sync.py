from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.schemas import SyncEventsRequest, SyncEventsResponse
from app.services.sync_service import accept_sync_events

router = APIRouter()


@router.post("/events", response_model=SyncEventsResponse)
def post_events(payload: SyncEventsRequest, current_user: CurrentUser, db: DbSession) -> SyncEventsResponse:
    accepted, duplicates = accept_sync_events(db, current_user, payload.events)
    return SyncEventsResponse(accepted=accepted, duplicates=duplicates)


