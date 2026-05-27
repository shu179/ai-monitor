from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, DbSession
from app.schemas import SyncChangesRequest, SyncChangesResponse, SyncEventsRequest, SyncEventsResponse
from app.services.event_service import build_sync_changes
from app.services.sync_service import accept_sync_events
from app.services.sync_v2_service import SyncBackpressureError, throttle_headers

router = APIRouter()


@router.post("/events", response_model=SyncEventsResponse)
def post_events(payload: SyncEventsRequest, current_user: CurrentUser, db: DbSession) -> SyncEventsResponse:
    try:
        accepted, duplicates = accept_sync_events(db, current_user, payload.events)
    except SyncBackpressureError as exc:
        headers = throttle_headers(
            retry_after_seconds=exc.retry_after_seconds,
            queue_depth_hint=exc.queue_depth_hint,
            throttle_bucket=exc.throttle_bucket,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "message": "sync queue is temporarily overloaded",
                "retry_after_seconds": exc.retry_after_seconds,
                "queue_depth_hint": exc.queue_depth_hint,
                "throttle_bucket": exc.throttle_bucket,
            },
            headers=headers,
        ) from exc
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
