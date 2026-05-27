from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .cloud_client import CloudClientError


DEFAULT_UPLOAD_CHUNK_BYTES = 1024 * 1024


class CloudObjectTransferError(RuntimeError):
    """Raised when a local object transfer cannot be completed safely."""


def upload_cloud_object_file(
    client: Any,
    access_token: str,
    file_path: str | Path,
    *,
    content_type: str = "application/octet-stream",
    compression: str = "auto",
    trace_id: str = "",
    chunk_bytes: int = DEFAULT_UPLOAD_CHUNK_BYTES,
    transfer_store: Any | None = None,
) -> dict[str, Any]:
    """Upload a local file through the cloud object API.

    This helper intentionally supports the current production path first:
    local-server single PUT uploads and already-existing manifests. Multipart
    is left to the later object-storage phase where resumable parts are wired.
    """
    path = Path(file_path)
    if not path.is_file():
        raise CloudObjectTransferError("object file does not exist")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(max(1, int(chunk_bytes or DEFAULT_UPLOAD_CHUNK_BYTES)))
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    if size <= 0:
        raise CloudObjectTransferError("object file is empty")
    sha256 = digest.hexdigest()
    transfer_id = str(trace_id or f"upload:{sha256}").strip()
    store = transfer_store
    if store is not None:
        store.start_transfer(
            transfer_id=transfer_id,
            direction="upload",
            sha256=sha256,
            size_bytes=size,
            path=str(path),
            content_type=content_type,
            trace_id=trace_id,
        )
    try:
        return _upload_cloud_object_file(
            client,
            access_token,
            path,
            sha256=sha256,
            size=size,
            content_type=content_type,
            compression=compression,
            trace_id=trace_id,
            chunk_bytes=chunk_bytes,
            transfer_store=store,
            transfer_id=transfer_id,
        )
    except Exception as exc:
        if store is not None:
            store.fail_transfer(transfer_id, str(exc))
        raise


def _upload_cloud_object_file(
    client: Any,
    access_token: str,
    path: Path,
    *,
    sha256: str,
    size: int,
    content_type: str,
    compression: str,
    trace_id: str,
    chunk_bytes: int,
    transfer_store: Any | None,
    transfer_id: str,
) -> dict[str, Any]:
    upload = client.create_object_upload(
        access_token,
        sha256=sha256,
        size_bytes=size,
        content_type=content_type,
        storage_size_bytes=size,
        compression=compression,
        trace_id=trace_id,
    )
    strategy = str(upload.get("strategy") or "").strip()
    if strategy == "already_exists":
        if transfer_store is not None:
            transfer_store.finish_transfer(
                transfer_id,
                object_id=str(upload.get("object_id") or ""),
                path=str(path),
                status="completed",
            )
        return {"ok": True, "uploaded": False, "strategy": strategy, "object": upload}
    if strategy == "inline":
        if transfer_store is not None:
            transfer_store.finish_transfer(transfer_id, path=str(path), status="completed")
        return {"ok": True, "uploaded": False, "strategy": strategy, "object": upload}
    if strategy != "single_put":
        raise CloudObjectTransferError(f"unsupported object upload strategy: {strategy or 'unknown'}")
    session_id = str(upload.get("session_id") or upload.get("sessionId") or "").strip()
    upload_payload = upload.get("upload") if isinstance(upload.get("upload"), dict) else {}
    upload_url = str(upload_payload.get("url") or "").strip()
    if not session_id or not upload_url:
        raise CloudObjectTransferError("object upload session is missing upload URL")
    with path.open("rb") as handle:
        client.upload_object_content(
            access_token,
            upload_url,
            _read_chunks(handle, chunk_bytes=max(1, int(chunk_bytes or DEFAULT_UPLOAD_CHUNK_BYTES))),
            content_type=str(upload.get("content_type") or content_type),
            headers=upload_payload.get("headers") if isinstance(upload_payload.get("headers"), dict) else None,
            trace_id=trace_id,
        )
    completed = client.complete_object_upload(
        access_token,
        session_id,
        storage_size_bytes=int(upload.get("storage_size_bytes") or size),
        compression=str(upload.get("compression") or compression),
        trace_id=trace_id,
    )
    if transfer_store is not None:
        transfer_store.finish_transfer(
            transfer_id,
            object_id=str(completed.get("object_id") or completed.get("objectId") or ""),
            path=str(path),
            status=str(completed.get("status") or "completed"),
        )
    return {"ok": True, "uploaded": True, "strategy": strategy, "object": completed}


def _read_chunks(handle: Any, *, chunk_bytes: int):
    while True:
        chunk = handle.read(chunk_bytes)
        if not chunk:
            return
        yield chunk
