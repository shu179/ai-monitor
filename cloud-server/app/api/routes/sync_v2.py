from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Response, status

from app.api.deps import CurrentUser, DbSession
from app.schemas import (
    CloudCapabilityResponse,
    ObjectDownloadResponse,
    ObjectPartCompleteRequest,
    ObjectPartCompleteResponse,
    ObjectPartsPresignRequest,
    ObjectPartsPresignResponse,
    ObjectUploadCompleteRequest,
    ObjectUploadCompleteResponse,
    ObjectUploadCreateRequest,
    ObjectUploadResponse,
    StateDeltaRequest,
    StateDeltaResponse,
    SyncBatchRequest,
    SyncBatchResponse,
)
from app.services.object_storage_service import (
    ObjectStorageError,
    complete_object_upload,
    create_object_download,
    create_object_upload,
    presign_object_upload_parts,
    record_object_upload_part,
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
            reset_token=payload.reset_token,
            bootstrap_cursor=payload.bootstrap_cursor,
        )
    )


@router.post("/objects/uploads", response_model=ObjectUploadResponse)
def object_upload_create(
    payload: ObjectUploadCreateRequest,
    current_user: CurrentUser,
    db: DbSession,
) -> ObjectUploadResponse:
    try:
        return ObjectUploadResponse(
            **create_object_upload(
                db,
                current_user,
                sha256=payload.sha256,
                size_bytes=payload.size_bytes,
                content_type=payload.content_type,
                storage_size_bytes=payload.storage_size_bytes,
                compression=payload.compression,
            )
        )
    except ObjectStorageError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/objects/uploads/{session_id}/parts:presign", response_model=ObjectPartsPresignResponse)
def object_upload_parts_presign(
    session_id: str,
    payload: ObjectPartsPresignRequest,
    current_user: CurrentUser,
    db: DbSession,
) -> ObjectPartsPresignResponse:
    try:
        return ObjectPartsPresignResponse(
            **presign_object_upload_parts(
                db,
                current_user,
                session_id=session_id,
                part_numbers=payload.part_numbers,
            )
        )
    except ObjectStorageError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/objects/uploads/{session_id}/parts", response_model=ObjectPartCompleteResponse)
def object_upload_part_complete(
    session_id: str,
    payload: ObjectPartCompleteRequest,
    current_user: CurrentUser,
    db: DbSession,
) -> ObjectPartCompleteResponse:
    try:
        return ObjectPartCompleteResponse(
            **record_object_upload_part(
                db,
                current_user,
                session_id=session_id,
                part_number=payload.part_number,
                etag=payload.etag,
                size_bytes=payload.size_bytes,
                sha256=payload.sha256,
            )
        )
    except ObjectStorageError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/objects/uploads/{session_id}:complete", response_model=ObjectUploadCompleteResponse)
def object_upload_complete(
    session_id: str,
    payload: ObjectUploadCompleteRequest,
    current_user: CurrentUser,
    db: DbSession,
) -> ObjectUploadCompleteResponse:
    try:
        return ObjectUploadCompleteResponse(
            **complete_object_upload(
                db,
                current_user,
                session_id=session_id,
                storage_size_bytes=payload.storage_size_bytes,
                compression=payload.compression,
            )
        )
    except ObjectStorageError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/objects/{object_id}:download", response_model=ObjectDownloadResponse)
def object_download(object_id: str, current_user: CurrentUser, db: DbSession) -> ObjectDownloadResponse:
    try:
        return ObjectDownloadResponse(**create_object_download(db, current_user, object_id=object_id))
    except ObjectStorageError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
