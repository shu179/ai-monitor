from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.schemas import SyncChangesRequest, SyncChangesResponse, SyncEventsRequest, SyncEventsResponse
from app.services.event_service import build_sync_changes
from app.services.sync_service import accept_sync_events

router = APIRouter()


@router.post("/events", response_model=SyncEventsResponse)
def post_events(payload: SyncEventsRequest, current_user: CurrentUser, db: DbSession) -> SyncEventsResponse:
    accepted, duplicates = accept_sync_events(db, current_user, payload.events)
    return SyncEventsResponse(accepted=accepted, duplicates=duplicates)


@router.post("/changes", response_model=SyncChangesResponse)
def sync_changes(payload: SyncChangesRequest, current_user: CurrentUser, db: DbSession) -> SyncChangesResponse:
    return SyncChangesResponse(
        **build_sync_changes(
            db,
            current_user,
            known_snapshot=payload.known_snapshot,
            task_cursors=payload.task_cursors,
            task_day_status_cursors=payload.task_day_status_cursors,
        )
    )
