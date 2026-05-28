from __future__ import annotations

import hashlib
import math
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
    compression: str = "none",
    trace_id: str = "",
    chunk_bytes: int = DEFAULT_UPLOAD_CHUNK_BYTES,
    transfer_store: Any | None = None,
) -> dict[str, Any]:
    """Upload a local file through the cloud object API."""
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
            storage_size_bytes=size,
            path=str(path),
            content_type=content_type,
            compression=compression,
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
    if strategy == "multipart":
        return _upload_multipart_cloud_object_file(
            client,
            access_token,
            path,
            upload=upload,
            session_id=str(upload.get("session_id") or upload.get("sessionId") or "").strip(),
            size=size,
            content_type=content_type,
            compression=compression,
            trace_id=trace_id,
            transfer_store=transfer_store,
            transfer_id=transfer_id,
        )
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


def _upload_multipart_cloud_object_file(
    client: Any,
    access_token: str,
    path: Path,
    *,
    upload: dict[str, Any],
    session_id: str,
    size: int,
    content_type: str,
    compression: str,
    trace_id: str,
    transfer_store: Any | None,
    transfer_id: str,
) -> dict[str, Any]:
    if not session_id:
        raise CloudObjectTransferError("multipart object upload session is missing session_id")
    part_size = int(upload.get("part_size_bytes") or 0)
    parts_total = int(upload.get("parts_total") or 0)
    if part_size <= 0:
        raise CloudObjectTransferError("multipart object upload is missing part_size_bytes")
    expected_parts_total = max(1, math.ceil(size / part_size))
    if parts_total <= 0:
        parts_total = expected_parts_total
    if parts_total != expected_parts_total:
        raise CloudObjectTransferError("multipart object upload parts_total does not match file size")

    for part_number in range(1, parts_total + 1):
        presigned = client.presign_object_upload_parts(
            access_token,
            session_id,
            [part_number],
            trace_id=trace_id,
        )
        upload_urls = presigned.get("upload_urls") if isinstance(presigned.get("upload_urls"), list) else []
        upload_url_payload = _find_part_upload_url(upload_urls, part_number)
        upload_url = str(upload_url_payload.get("url") or "").strip()
        if not upload_url:
            raise CloudObjectTransferError(f"multipart object upload part {part_number} is missing upload URL")
        offset = (part_number - 1) * part_size
        part_bytes = min(part_size, max(0, size - offset))
        part_digest = hashlib.sha256()
        response = client.upload_object_part_content(
            access_token,
            upload_url,
            _read_file_range(path, offset=offset, size=part_bytes, digest=part_digest),
            content_type=str(upload.get("content_type") or content_type),
            headers=upload_url_payload.get("headers") if isinstance(upload_url_payload.get("headers"), dict) else None,
            trace_id=trace_id,
        )
        part_sha256 = part_digest.hexdigest()
        etag = str(response.get("etag") or response.get("ETag") or "").strip()
        if not etag:
            raise CloudObjectTransferError(f"multipart object upload part {part_number} did not return an ETag")
        client.record_object_upload_part(
            access_token,
            session_id,
            part_number=part_number,
            etag=etag,
            size_bytes=part_bytes,
            sha256=part_sha256,
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
    return {"ok": True, "uploaded": True, "strategy": "multipart", "object": completed}


def _find_part_upload_url(upload_urls: list[Any], part_number: int) -> dict[str, Any]:
    for item in upload_urls:
        if not isinstance(item, dict):
            continue
        if int(item.get("part_number") or item.get("partNumber") or 0) == int(part_number):
            return item
    return {}


def _read_file_range(path: Path, *, offset: int, size: int, digest: Any | None = None):
    with path.open("rb") as handle:
        handle.seek(max(0, int(offset)))
        for chunk in _read_limited_chunks(handle, remaining=max(0, int(size))):
            if digest is not None:
                digest.update(chunk)
            yield chunk


def _read_limited_chunks(handle: Any, *, remaining: int, chunk_bytes: int = DEFAULT_UPLOAD_CHUNK_BYTES):
    left = max(0, int(remaining))
    while left > 0:
        chunk = handle.read(min(max(1, int(chunk_bytes or DEFAULT_UPLOAD_CHUNK_BYTES)), left))
        if not chunk:
            return
        left -= len(chunk)
        yield chunk


def _read_chunks(handle: Any, *, chunk_bytes: int):
    while True:
        chunk = handle.read(chunk_bytes)
        if not chunk:
            return
        yield chunk
