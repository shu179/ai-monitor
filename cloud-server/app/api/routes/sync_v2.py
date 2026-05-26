from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Response, status

from app.api.deps import CurrentUser, DbSession
from app.schemas import (
    CloudCapabilityResponse,
    StateDeltaRequest,
    StateDeltaResponse,
    SyncBatchRequest,
    SyncBatchResponse,
)
from app.services.sync_v2_service import (
    SyncBackpressureError,
    accept_sync_batch,
    build_state_delta,
    cloud_capabilities,
    throttle_headers,
)

router = APIRouter()


@router.get("/capabilities", response_model=CloudCapabilityResponse)
def capabilities(
    response: Response,
    x_cloud_capability: str = Header(default="", alias="X-Cloud-Capability"),
) -> CloudCapabilityResponse:
    del x_cloud_capability
    payload = cloud_capabilities()
    response.headers["X-Cloud-Capability"] = ",".join(payload["capabilities"])
    return CloudCapabilityResponse(**payload)


@router.post("/sync/batches", response_model=SyncBatchResponse)
def sync_batch(payload: SyncBatchRequest, current_user: CurrentUser, db: DbSession, response: Response) -> SyncBatchResponse:
    try:
        result = accept_sync_batch(
            db,
            current_user,
            idempotency_key=payload.idempotency_key,
            events=payload.events,
        )
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
    for key, value in throttle_headers(
        retry_after_seconds=int(result.get("retry_after_seconds") or 0),
        queue_depth_hint=int(result.get("queue_depth_hint") or 0),
    ).items():
        response.headers[key] = value
    return SyncBatchResponse(**result)


@router.post("/sync/state-delta", response_model=StateDeltaResponse)
def state_delta(payload: StateDeltaRequest, current_user: CurrentUser, db: DbSession) -> StateDeltaResponse:
    return StateDeltaResponse(
        **build_state_delta(
            db,
            current_user,
            cursors=payload.cursors,
            limit=payload.limit,
        )
    )
