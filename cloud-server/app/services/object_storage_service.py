from __future__ import annotations

import hashlib
import hmac
import logging
import math
import os
import re
import shutil
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote, urlencode, urlsplit
from uuid import uuid4

import httpx
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models import ObjectManifest, ObjectUploadPart, ObjectUploadSession, User
from app.services.sync_v2_service import LIMITS, TTL_SECONDS, workspace_bucket_advisory_lock_key

OBJECT_UPLOAD_STATUS_INITIATED = "initiated"
OBJECT_UPLOAD_STATUS_COMPLETED = "completed"
OBJECT_MANIFEST_STATUS_ACTIVE = "active"
INLINE_STRATEGY = "inline"
ALREADY_EXISTS_STRATEGY = "already_exists"
SINGLE_PUT_STRATEGY = "single_put"
MULTIPART_STRATEGY = "multipart"
MAX_PARTS_TO_PRESIGN = 100
LOCAL_STORAGE_PROVIDER_PREFIX = "local:"
DEFAULT_TOTAL_OBJECT_QUOTA_BYTES = 10 * 1024 * 1024 * 1024
DEFAULT_WORKSPACE_OBJECT_QUOTA_BYTES = 5 * 1024 * 1024 * 1024
DEFAULT_MAX_FILE_BYTES = 512 * 1024 * 1024
DEFAULT_MIN_FREE_BYTES = 8 * 1024 * 1024 * 1024
CONTENT_MAGIC_SAMPLE_BYTES = 512

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_CONTENT_TYPES = {
    "application/json",
    "application/octet-stream",
    "application/pdf",
    "application/x-ndjson",
    "application/zstd",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
    "text/csv",
    "text/markdown",
    "text/plain",
}
logger = logging.getLogger(__name__)


class ObjectStorageError(RuntimeError):
    status_code = 400


class ObjectStorageUnavailable(ObjectStorageError):
    status_code = 503


class ObjectStorageNotFound(ObjectStorageError):
    status_code = 404


class ObjectStorageQuotaExceeded(ObjectStorageError):
    status_code = 429


def create_object_upload(
    db: Session,
    user: User,
    *,
    sha256: str,
    size_bytes: int,
    content_type: str,
    storage_size_bytes: int | None = None,
    compression: str | None = None,
) -> dict[str, Any]:
    safe_sha256 = normalize_sha256(sha256)
    safe_size_bytes = _positive_int(size_bytes, field="size_bytes")
    safe_storage_size_bytes = _positive_int(storage_size_bytes or safe_size_bytes, field="storage_size_bytes")
    safe_content_type = normalize_content_type(content_type)
    safe_compression = normalize_compression(compression, content_type=safe_content_type, size_bytes=safe_size_bytes)
    storage_key = object_storage_key(user.workspace_id, safe_sha256)

    existing_manifest = db.scalar(
        select(ObjectManifest).where(
            ObjectManifest.workspace_id == user.workspace_id,
            ObjectManifest.sha256 == safe_sha256,
            ObjectManifest.status == OBJECT_MANIFEST_STATUS_ACTIVE,
        )
    )
    if existing_manifest is not None:
        return _upload_response(
            strategy=ALREADY_EXISTS_STRATEGY,
            sha256=safe_sha256,
            size_bytes=safe_size_bytes,
            storage_size_bytes=int(existing_manifest.storage_size_bytes or safe_storage_size_bytes),
            content_type=safe_content_type,
            compression=str(existing_manifest.compression or safe_compression),
            storage_key=str(existing_manifest.storage_key),
            object_id=str(existing_manifest.id),
        )

    strategy = upload_strategy_for_size(safe_storage_size_bytes)
    if strategy == INLINE_STRATEGY:
        return _upload_response(
            strategy=INLINE_STRATEGY,
            sha256=safe_sha256,
            size_bytes=safe_size_bytes,
            storage_size_bytes=safe_storage_size_bytes,
            content_type=safe_content_type,
            compression=safe_compression,
            storage_key=storage_key,
        )

    client = object_storage_client()
    if client.is_local:
        if safe_storage_size_bytes != safe_size_bytes:
            raise ObjectStorageError("local object storage requires storage_size_bytes to equal size_bytes")
        safe_compression = "none"
        strategy = SINGLE_PUT_STRATEGY

    _enforce_object_limits(
        db,
        workspace_id=user.workspace_id,
        incoming_storage_size_bytes=safe_storage_size_bytes,
        local_backend=client.is_local,
    )
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=TTL_SECONDS["upload_presigned_url"])
    upload_session = _get_or_reset_upload_session(
        db,
        workspace_id=user.workspace_id,
        sha256=safe_sha256,
        size_bytes=safe_size_bytes,
        content_type=safe_content_type,
        strategy=strategy,
        storage_size_bytes=safe_storage_size_bytes,
        storage_key=storage_key,
        now=now,
        client=client,
    )
    db.commit()
    db.refresh(upload_session)

    if strategy == SINGLE_PUT_STRATEGY:
        upload_url = client.upload_url(str(upload_session.id), storage_key, expires_seconds=TTL_SECONDS["upload_presigned_url"])
        return _upload_response(
            strategy=SINGLE_PUT_STRATEGY,
            sha256=safe_sha256,
            size_bytes=safe_size_bytes,
            storage_size_bytes=safe_storage_size_bytes,
            content_type=safe_content_type,
            compression=safe_compression,
            storage_key=storage_key,
            session_id=str(upload_session.id),
            expires_at=expires_at,
            upload={
                "method": "PUT",
                "url": upload_url,
                "headers": {},
            },
        )

    return _upload_response(
        strategy=MULTIPART_STRATEGY,
        sha256=safe_sha256,
        size_bytes=safe_size_bytes,
        storage_size_bytes=safe_storage_size_bytes,
        content_type=safe_content_type,
        compression=safe_compression,
        storage_key=storage_key,
        session_id=str(upload_session.id),
        expires_at=expires_at,
        part_size_bytes=int(upload_session.part_size_bytes),
        parts_total=int(upload_session.parts_total),
    )


async def store_local_object_upload_content(
    db: Session,
    user: User,
    *,
    session_id: str,
    chunks: AsyncIterator[bytes],
) -> dict[str, Any]:
    upload_session = _load_upload_session(db, user, session_id=session_id, allow_single_put=True)
    if not _is_local_upload_session(upload_session):
        raise ObjectStorageError("upload session is not local-backed")
    expected_size = int(upload_session.size_bytes)
    settings = get_settings()
    if expected_size > _max_file_bytes(settings):
        raise ObjectStorageQuotaExceeded("object exceeds local max file size")
    _enforce_local_disk_headroom(expected_size, settings=settings)
    storage_key = object_storage_key(user.workspace_id, str(upload_session.sha256))
    path = local_object_path(storage_key, settings=settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + f".tmp-{uuid4().hex}")
    digest = hashlib.sha256()
    magic_sample = bytearray()
    actual_size = 0
    try:
        with tmp_path.open("wb") as handle:
            async for chunk in chunks:
                if not chunk:
                    continue
                actual_size += len(chunk)
                if actual_size > expected_size:
                    raise ObjectStorageError(f"upload size exceeds expected {expected_size}")
                if actual_size > _max_file_bytes(settings):
                    raise ObjectStorageQuotaExceeded("object exceeds local max file size")
                if len(magic_sample) < CONTENT_MAGIC_SAMPLE_BYTES:
                    remaining_sample_bytes = CONTENT_MAGIC_SAMPLE_BYTES - len(magic_sample)
                    magic_sample.extend(chunk[:remaining_sample_bytes])
                digest.update(chunk)
                handle.write(chunk)
                _enforce_local_disk_headroom(max(0, expected_size - actual_size), settings=settings)
        if actual_size != expected_size:
            raise ObjectStorageError(f"upload size mismatch: expected {expected_size}, got {actual_size}")
        if digest.hexdigest() != str(upload_session.sha256):
            raise ObjectStorageError("upload sha256 mismatch")
        _validate_content_magic(str(upload_session.content_type), bytes(magic_sample))
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    return complete_object_upload(
        db,
        user,
        session_id=session_id,
        storage_size_bytes=actual_size,
        compression="none",
    )


def presign_object_upload_parts(
    db: Session,
    user: User,
    *,
    session_id: str,
    part_numbers: list[int],
) -> dict[str, Any]:
    upload_session = _load_upload_session(db, user, session_id=session_id)
    if not part_numbers:
        raise ObjectStorageError("part_numbers is required")
    safe_part_numbers = sorted({int(item) for item in part_numbers})
    if len(safe_part_numbers) > MAX_PARTS_TO_PRESIGN:
        raise ObjectStorageError(f"cannot presign more than {MAX_PARTS_TO_PRESIGN} parts at once")
    for part_number in safe_part_numbers:
        if part_number < 1 or part_number > int(upload_session.parts_total):
            raise ObjectStorageError(f"part_number out of range: {part_number}")
    storage_key = object_storage_key(user.workspace_id, str(upload_session.sha256))
    client = object_storage_client()
    urls = [
        {
            "part_number": part_number,
            "method": "PUT",
            "url": client.presign_upload_part(
                storage_key,
                upload_id=str(upload_session.storage_provider_upload_id),
                part_number=part_number,
                expires_seconds=TTL_SECONDS["upload_presigned_url"],
            ),
            "headers": {},
        }
        for part_number in safe_part_numbers
    ]
    return {
        "session_id": str(upload_session.id),
        "part_size_bytes": int(upload_session.part_size_bytes),
        "parts_total": int(upload_session.parts_total),
        "upload_urls": urls,
        "expires_at": datetime.now(timezone.utc) + timedelta(seconds=TTL_SECONDS["upload_presigned_url"]),
    }


def record_object_upload_part(
    db: Session,
    user: User,
    *,
    session_id: str,
    part_number: int,
    etag: str,
    size_bytes: int,
    sha256: str | None = None,
) -> dict[str, Any]:
    upload_session = _load_upload_session(db, user, session_id=session_id)
    safe_part_number = int(part_number)
    if safe_part_number < 1 or safe_part_number > int(upload_session.parts_total):
        raise ObjectStorageError(f"part_number out of range: {safe_part_number}")
    safe_size_bytes = _positive_int(size_bytes, field="size_bytes")
    safe_etag = str(etag or "").strip()
    if not safe_etag:
        raise ObjectStorageError("etag is required")
    safe_sha256 = normalize_sha256(sha256) if sha256 else None

    part = db.scalar(
        select(ObjectUploadPart).where(
            ObjectUploadPart.session_id == upload_session.id,
            ObjectUploadPart.part_number == safe_part_number,
        )
    )
    if part is None:
        part = ObjectUploadPart(session_id=upload_session.id, part_number=safe_part_number)
        db.add(part)
    part.etag = safe_etag
    part.size_bytes = safe_size_bytes
    part.sha256 = safe_sha256
    part.completed_at = datetime.now(timezone.utc)
    db.flush()
    completed = db.scalar(
        select(func.count())
        .select_from(ObjectUploadPart)
        .where(
            ObjectUploadPart.session_id == upload_session.id,
            ObjectUploadPart.completed_at.is_not(None),
        )
    )
    upload_session.parts_completed = int(completed or 0)
    db.commit()
    return {
        "session_id": str(upload_session.id),
        "part_number": safe_part_number,
        "parts_completed": int(upload_session.parts_completed),
        "parts_total": int(upload_session.parts_total),
    }


def complete_object_upload(
    db: Session,
    user: User,
    *,
    session_id: str,
    storage_size_bytes: int | None = None,
    compression: str | None = None,
) -> dict[str, Any]:
    upload_session = _load_upload_session(db, user, session_id=session_id, allow_single_put=True)
    safe_storage_size_bytes = _positive_int(storage_size_bytes or upload_session.size_bytes, field="storage_size_bytes")
    safe_content_type = normalize_content_type(str(upload_session.content_type))
    safe_compression = normalize_compression(
        compression,
        content_type=safe_content_type,
        size_bytes=int(upload_session.size_bytes),
    )
    storage_key = object_storage_key(user.workspace_id, str(upload_session.sha256))
    client = object_storage_client()
    local_path: Path | None = None
    if _is_local_upload_session(upload_session):
        path = local_object_path(storage_key)
        if not path.exists() or not path.is_file():
            raise ObjectStorageNotFound("local object file not found")
        if path.stat().st_size != safe_storage_size_bytes:
            raise ObjectStorageError("local object size mismatch")
        local_path = path
    if int(upload_session.parts_total) > 1:
        parts = list(
            db.scalars(
                select(ObjectUploadPart)
                .where(ObjectUploadPart.session_id == upload_session.id)
                .order_by(ObjectUploadPart.part_number)
            )
        )
        if len(parts) != int(upload_session.parts_total):
            raise ObjectStorageError("multipart upload is missing completed parts")
        client.complete_multipart_upload(
            storage_key,
            upload_id=str(upload_session.storage_provider_upload_id),
            parts=[{"part_number": int(part.part_number), "etag": str(part.etag or "")} for part in parts],
        )

    _lock_object_quota(db, workspace_id=user.workspace_id)
    manifest = db.scalar(
        select(ObjectManifest).where(
            ObjectManifest.workspace_id == user.workspace_id,
            ObjectManifest.sha256 == upload_session.sha256,
            ObjectManifest.status == OBJECT_MANIFEST_STATUS_ACTIVE,
        )
    )
    if manifest is None:
        try:
            _enforce_object_limits(
                db,
                workspace_id=user.workspace_id,
                incoming_storage_size_bytes=safe_storage_size_bytes,
                local_backend=client.is_local,
            )
            manifest = ObjectManifest(
                id=str(uuid4()),
                workspace_id=user.workspace_id,
                sha256=str(upload_session.sha256),
                size_bytes=int(upload_session.size_bytes),
                storage_size_bytes=safe_storage_size_bytes,
                content_type=safe_content_type,
                storage_key=storage_key,
                compression=safe_compression,
                ref_count=0,
                status=OBJECT_MANIFEST_STATUS_ACTIVE,
            )
            db.add(manifest)
        except ObjectStorageQuotaExceeded:
            if local_path is not None:
                local_path.unlink(missing_ok=True)
            raise
    upload_session.status = OBJECT_UPLOAD_STATUS_COMPLETED
    db.commit()
    db.refresh(manifest)
    return {
        "object_id": str(manifest.id),
        "status": str(manifest.status),
        "storage_key": str(manifest.storage_key),
        "sha256": str(manifest.sha256),
        "size_bytes": int(manifest.size_bytes),
        "storage_size_bytes": int(manifest.storage_size_bytes),
        "content_type": str(manifest.content_type),
        "compression": str(manifest.compression),
    }


def create_object_download(db: Session, user: User, *, object_id: str) -> dict[str, Any]:
    manifest = db.scalar(
        select(ObjectManifest).where(
            ObjectManifest.workspace_id == user.workspace_id,
            ObjectManifest.id == str(object_id),
            ObjectManifest.status == OBJECT_MANIFEST_STATUS_ACTIVE,
        )
    )
    if manifest is None:
        raise ObjectStorageNotFound("object not found")
    client = object_storage_client()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=TTL_SECONDS["download_presigned_url"])
    download_url = client.download_url(str(manifest.id), str(manifest.storage_key), expires_seconds=TTL_SECONDS["download_presigned_url"])
    return {
        "object_id": str(manifest.id),
        "download_url": download_url,
        "expires_at": expires_at,
        "content_type": str(manifest.content_type),
        "size_bytes": int(manifest.size_bytes),
        "storage_size_bytes": int(manifest.storage_size_bytes),
        "compression": str(manifest.compression),
    }


def object_storage_key(workspace_id: int, sha256: str) -> str:
    safe_sha256 = normalize_sha256(sha256)
    return f"{int(workspace_id)}/{safe_sha256[:2]}/{safe_sha256[2:4]}/{safe_sha256}"


def upload_strategy_for_size(storage_size_bytes: int) -> str:
    safe_size = _positive_int(storage_size_bytes, field="storage_size_bytes")
    if safe_size <= int(LIMITS["inline_blob_max_bytes"]):
        return INLINE_STRATEGY
    if safe_size <= int(LIMITS["single_put_max_bytes"]):
        return SINGLE_PUT_STRATEGY
    return MULTIPART_STRATEGY


def normalize_sha256(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if not _SHA256_RE.match(text):
        raise ObjectStorageError("sha256 must be 64 lowercase hex characters")
    return text


def normalize_content_type(value: str | None) -> str:
    text = str(value or "").split(";", 1)[0].strip().lower()
    if text not in _ALLOWED_CONTENT_TYPES:
        raise ObjectStorageError(f"unsupported content_type: {text or '<empty>'}")
    return text


def normalize_compression(value: str | None, *, content_type: str, size_bytes: int) -> str:
    text = str(value or "").strip().lower()
    if text in {"", "auto"}:
        if content_type.startswith("text/") or content_type in {"application/json", "application/x-ndjson"}:
            return "zstd" if int(size_bytes or 0) > int(LIMITS["inline_blob_max_bytes"]) else "none"
        return "none"
    if text not in {"none", "zstd"}:
        raise ObjectStorageError("compression must be none, zstd, or auto")
    return text


def _validate_content_magic(content_type: str, sample: bytes) -> None:
    safe_content_type = normalize_content_type(content_type)
    payload = bytes(sample or b"")
    mismatch = False
    if safe_content_type == "image/png":
        mismatch = not payload.startswith(b"\x89PNG\r\n\x1a\n")
    elif safe_content_type == "image/jpeg":
        mismatch = not payload.startswith(b"\xff\xd8\xff")
    elif safe_content_type == "image/gif":
        mismatch = not (payload.startswith(b"GIF87a") or payload.startswith(b"GIF89a"))
    elif safe_content_type == "image/webp":
        mismatch = len(payload) < 12 or not (payload.startswith(b"RIFF") and payload[8:12] == b"WEBP")
    elif safe_content_type == "application/pdf":
        mismatch = not payload.lstrip().startswith(b"%PDF-")
    elif safe_content_type == "application/zstd":
        mismatch = not payload.startswith(b"\x28\xb5\x2f\xfd")
    elif safe_content_type in {"text/csv", "text/markdown", "text/plain", "application/json", "application/x-ndjson"}:
        mismatch = b"\x00" in payload

    if mismatch:
        logger.warning(
            "[ObjectStorage] reject_upload reason=content_type_mismatch content_type=%s sample_bytes=%s",
            safe_content_type,
            len(payload),
        )
        raise ObjectStorageError("upload content does not match declared content_type")


def object_storage_client(settings: Settings | None = None) -> "S3CompatibleObjectStorageClient":
    resolved_settings = settings or get_settings()
    if _s3_configured(resolved_settings):
        return S3CompatibleObjectStorageClient(resolved_settings)
    return LocalDiskObjectStorageClient(resolved_settings)


def local_object_path(storage_key: str, settings: Settings | None = None) -> Path:
    resolved_settings = settings or get_settings()
    root = Path(str(resolved_settings.object_storage_local_dir or "/opt/surfaced/object-data")).resolve()
    path = (root / str(storage_key).strip("/")).resolve()
    if root not in path.parents and path != root:
        raise ObjectStorageError("invalid local object path")
    return path


def read_local_object(manifest: ObjectManifest) -> Path:
    path = local_object_path(str(manifest.storage_key))
    if not path.exists() or not path.is_file():
        raise ObjectStorageNotFound("local object file not found")
    return path


class LocalDiskObjectStorageClient:
    is_local = True

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = str(settings.object_storage_local_base_url or "").rstrip("/")

    def require_configured(self) -> None:
        Path(str(self.settings.object_storage_local_dir or "/opt/surfaced/object-data")).mkdir(parents=True, exist_ok=True)

    def upload_url(self, session_id: str, key: str, *, expires_seconds: int) -> str:
        del key, expires_seconds
        if self.base_url:
            return f"{self.base_url}/api/v2/objects/uploads/{quote(str(session_id), safe='')}/content"
        return f"/api/v2/objects/uploads/{quote(str(session_id), safe='')}/content"

    def download_url(self, object_id: str, key: str, *, expires_seconds: int) -> str:
        del key, expires_seconds
        if self.base_url:
            return f"{self.base_url}/api/v2/objects/{quote(str(object_id), safe='')}/content"
        return f"/api/v2/objects/{quote(str(object_id), safe='')}/content"

    def create_multipart_upload(self, key: str, *, content_type: str) -> str:
        del key, content_type
        raise ObjectStorageUnavailable("local object storage does not support multipart")

    def presign_upload_part(self, key: str, *, upload_id: str, part_number: int, expires_seconds: int) -> str:
        del key, upload_id, part_number, expires_seconds
        raise ObjectStorageUnavailable("local object storage does not support multipart")

    def complete_multipart_upload(self, key: str, *, upload_id: str, parts: list[dict[str, Any]]) -> None:
        del key, upload_id, parts
        raise ObjectStorageUnavailable("local object storage does not support multipart")


class S3CompatibleObjectStorageClient:
    is_local = False

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.endpoint_url = str(settings.object_storage_endpoint_url or "").rstrip("/")
        self.bucket = str(settings.object_storage_bucket or "").strip()
        self.region = str(settings.object_storage_region or "auto").strip() or "auto"
        self.access_key_id = str(settings.object_storage_access_key_id or "").strip()
        self.secret_access_key = str(settings.object_storage_secret_access_key or "").strip()
        self.force_path_style = bool(settings.object_storage_force_path_style)

    def require_configured(self) -> None:
        if not all([self.endpoint_url, self.bucket, self.access_key_id, self.secret_access_key]):
            raise ObjectStorageUnavailable("object storage is not configured")

    def presign_put_object(self, key: str, *, expires_seconds: int) -> str:
        self.require_configured()
        return self._presign("PUT", key, expires_seconds=expires_seconds)

    def upload_url(self, session_id: str, key: str, *, expires_seconds: int) -> str:
        del session_id
        return self.presign_put_object(key, expires_seconds=expires_seconds)

    def presign_get_object(self, key: str, *, expires_seconds: int) -> str:
        self.require_configured()
        return self._presign("GET", key, expires_seconds=expires_seconds)

    def download_url(self, object_id: str, key: str, *, expires_seconds: int) -> str:
        del object_id
        return self.presign_get_object(key, expires_seconds=expires_seconds)

    def presign_upload_part(self, key: str, *, upload_id: str, part_number: int, expires_seconds: int) -> str:
        self.require_configured()
        return self._presign(
            "PUT",
            key,
            expires_seconds=expires_seconds,
            query={"partNumber": str(int(part_number)), "uploadId": str(upload_id)},
        )

    def create_multipart_upload(self, key: str, *, content_type: str) -> str:
        self.require_configured()
        response = self._signed_request(
            "POST",
            key,
            query={"uploads": ""},
            headers={"content-type": content_type},
            body=b"",
        )
        if response.status_code >= 400:
            raise ObjectStorageUnavailable(f"multipart initiate failed: {response.status_code}")
        root = ET.fromstring(response.text)
        upload_id = _find_xml_text(root, "UploadId")
        if not upload_id:
            raise ObjectStorageUnavailable("multipart initiate response did not include UploadId")
        return upload_id

    def complete_multipart_upload(self, key: str, *, upload_id: str, parts: list[dict[str, Any]]) -> None:
        self.require_configured()
        body = _complete_multipart_xml(parts)
        response = self._signed_request(
            "POST",
            key,
            query={"uploadId": str(upload_id)},
            headers={"content-type": "application/xml"},
            body=body,
        )
        if response.status_code >= 400:
            raise ObjectStorageUnavailable(f"multipart complete failed: {response.status_code}")

    def _signed_request(
        self,
        method: str,
        key: str,
        *,
        query: dict[str, str],
        headers: dict[str, str],
        body: bytes,
    ) -> httpx.Response:
        url, signed_headers = self._signed_request_url_and_headers(method, key, query=query, headers=headers, body=body)
        with httpx.Client(timeout=30.0) as client:
            return client.request(method, url, headers=signed_headers, content=body)

    def _signed_request_url_and_headers(
        self,
        method: str,
        key: str,
        *,
        query: dict[str, str],
        headers: dict[str, str],
        body: bytes,
    ) -> tuple[str, dict[str, str]]:
        now = datetime.now(timezone.utc)
        base_url, host, canonical_uri = self._object_base(key)
        payload_hash = hashlib.sha256(body).hexdigest()
        signed_headers = {name.lower(): value for name, value in headers.items() if value}
        signed_headers["host"] = host
        signed_headers["x-amz-content-sha256"] = payload_hash
        signed_headers["x-amz-date"] = now.strftime("%Y%m%dT%H%M%SZ")
        signed_header_names = sorted(signed_headers)
        authorization = self._authorization_header(
            method,
            canonical_uri,
            query,
            signed_headers,
            signed_header_names,
            payload_hash=payload_hash,
            now=now,
        )
        signed_headers["authorization"] = authorization
        url = f"{base_url}{canonical_uri}"
        if query:
            url = f"{url}?{_canonical_query(query)}"
        return url, signed_headers

    def _presign(self, method: str, key: str, *, expires_seconds: int, query: dict[str, str] | None = None) -> str:
        now = datetime.now(timezone.utc)
        base_url, host, canonical_uri = self._object_base(key)
        date_stamp = now.strftime("%Y%m%d")
        credential_scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        params = dict(query or {})
        params.update(
            {
                "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
                "X-Amz-Credential": f"{self.access_key_id}/{credential_scope}",
                "X-Amz-Date": now.strftime("%Y%m%dT%H%M%SZ"),
                "X-Amz-Expires": str(max(1, int(expires_seconds))),
                "X-Amz-SignedHeaders": "host",
            }
        )
        canonical_query = _canonical_query(params)
        canonical_request = "\n".join(
            [
                method.upper(),
                canonical_uri,
                canonical_query,
                f"host:{host}\n",
                "host",
                "UNSIGNED-PAYLOAD",
            ]
        )
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                now.strftime("%Y%m%dT%H%M%SZ"),
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signature = hmac.new(
            _signing_key(self.secret_access_key, date_stamp, self.region),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{base_url}{canonical_uri}?{canonical_query}&X-Amz-Signature={signature}"

    def _authorization_header(
        self,
        method: str,
        canonical_uri: str,
        query: dict[str, str],
        headers: dict[str, str],
        signed_header_names: list[str],
        *,
        payload_hash: str,
        now: datetime,
    ) -> str:
        date_stamp = now.strftime("%Y%m%d")
        credential_scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        canonical_headers = "".join(f"{name}:{headers[name].strip()}\n" for name in signed_header_names)
        signed_headers = ";".join(signed_header_names)
        canonical_request = "\n".join(
            [
                method.upper(),
                canonical_uri,
                _canonical_query(query),
                canonical_headers,
                signed_headers,
                payload_hash,
            ]
        )
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                now.strftime("%Y%m%dT%H%M%SZ"),
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signature = hmac.new(
            _signing_key(self.secret_access_key, date_stamp, self.region),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return (
            "AWS4-HMAC-SHA256 "
            f"Credential={self.access_key_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )

    def _object_base(self, key: str) -> tuple[str, str, str]:
        parsed = urlsplit(self.endpoint_url)
        if not parsed.scheme or not parsed.netloc:
            raise ObjectStorageUnavailable("object storage endpoint URL is invalid")
        safe_key = "/".join(quote(part, safe="-_.~") for part in str(key).strip("/").split("/"))
        if self.force_path_style:
            return f"{parsed.scheme}://{parsed.netloc}", parsed.netloc, f"/{quote(self.bucket, safe='-_.~')}/{safe_key}"
        host = f"{self.bucket}.{parsed.netloc}"
        return f"{parsed.scheme}://{host}", host, f"/{safe_key}"


def _get_or_reset_upload_session(
    db: Session,
    *,
    workspace_id: int,
    sha256: str,
    size_bytes: int,
    content_type: str,
    strategy: str,
    storage_size_bytes: int,
    storage_key: str,
    now: datetime,
    client: S3CompatibleObjectStorageClient,
) -> ObjectUploadSession:
    part_size_bytes = int(LIMITS["multipart_part_bytes"])
    parts_total = 1 if strategy == SINGLE_PUT_STRATEGY else max(1, math.ceil(int(storage_size_bytes) / part_size_bytes))
    existing = db.scalar(
        select(ObjectUploadSession).where(
            ObjectUploadSession.workspace_id == workspace_id,
            ObjectUploadSession.sha256 == sha256,
            ObjectUploadSession.size_bytes == size_bytes,
        )
    )
    provider_upload_id = f"single:{uuid4()}"
    if client.is_local:
        provider_upload_id = f"{LOCAL_STORAGE_PROVIDER_PREFIX}{uuid4()}"
    if strategy == MULTIPART_STRATEGY:
        provider_upload_id = client.create_multipart_upload(storage_key, content_type=content_type)
    expires_at = now + timedelta(seconds=TTL_SECONDS["multipart_upload_session"])
    if existing is None:
        existing = ObjectUploadSession(
            id=str(uuid4()),
            workspace_id=workspace_id,
            sha256=sha256,
            size_bytes=size_bytes,
            content_type=content_type,
            storage_provider_upload_id=provider_upload_id,
            status=OBJECT_UPLOAD_STATUS_INITIATED,
            part_size_bytes=part_size_bytes,
            parts_total=parts_total,
            parts_completed=0,
            expires_at=expires_at,
        )
        db.add(existing)
    else:
        db.execute(delete(ObjectUploadPart).where(ObjectUploadPart.session_id == existing.id))
        existing.content_type = content_type
        existing.storage_provider_upload_id = provider_upload_id
        existing.status = OBJECT_UPLOAD_STATUS_INITIATED
        existing.part_size_bytes = part_size_bytes
        existing.parts_total = parts_total
        existing.parts_completed = 0
        existing.expires_at = expires_at
    return existing


def _load_upload_session(
    db: Session,
    user: User,
    *,
    session_id: str,
    allow_single_put: bool = False,
) -> ObjectUploadSession:
    upload_session = db.scalar(
        select(ObjectUploadSession).where(
            ObjectUploadSession.id == str(session_id),
            ObjectUploadSession.workspace_id == user.workspace_id,
            ObjectUploadSession.status == OBJECT_UPLOAD_STATUS_INITIATED,
        )
    )
    if upload_session is None:
        raise ObjectStorageNotFound("upload session not found")
    if upload_session.expires_at < datetime.now(timezone.utc):
        raise ObjectStorageError("upload session expired")
    if not allow_single_put and int(upload_session.parts_total) <= 1:
        raise ObjectStorageError("upload session is not multipart")
    return upload_session


def _enforce_object_limits(
    db: Session,
    *,
    workspace_id: int,
    incoming_storage_size_bytes: int,
    local_backend: bool,
) -> None:
    settings = get_settings()
    incoming_size = int(incoming_storage_size_bytes)
    if local_backend:
        if incoming_size > _max_file_bytes(settings):
            _reject_upload(
                "max_file_exceeded",
                workspace_id=workspace_id,
                incoming_size=incoming_size,
                limit_bytes=_max_file_bytes(settings),
            )
        _enforce_local_disk_headroom(incoming_size, settings=settings)
        total_quota = int(settings.object_storage_total_quota_bytes or DEFAULT_TOTAL_OBJECT_QUOTA_BYTES)
        if total_quota > 0:
            total_used = db.scalar(
                select(func.coalesce(func.sum(ObjectManifest.storage_size_bytes), 0)).where(
                    ObjectManifest.status == OBJECT_MANIFEST_STATUS_ACTIVE,
                )
            )
            if int(total_used or 0) + incoming_size > total_quota:
                _reject_upload(
                    "total_quota_exceeded",
                    workspace_id=workspace_id,
                    incoming_size=incoming_size,
                    used_bytes=int(total_used or 0),
                    limit_bytes=total_quota,
                )
    quota = int(settings.object_storage_workspace_quota_bytes or DEFAULT_WORKSPACE_OBJECT_QUOTA_BYTES)
    if quota <= 0:
        return
    used = db.scalar(
        select(func.coalesce(func.sum(ObjectManifest.storage_size_bytes), 0)).where(
            ObjectManifest.workspace_id == int(workspace_id),
            ObjectManifest.status == OBJECT_MANIFEST_STATUS_ACTIVE,
        )
    )
    if int(used or 0) + incoming_size > quota:
        _reject_upload(
            "workspace_quota_exceeded",
            workspace_id=workspace_id,
            incoming_size=incoming_size,
            used_bytes=int(used or 0),
            limit_bytes=quota,
        )


def _lock_object_quota(db: Session, *, workspace_id: int) -> None:
    """Serialize final object quota checks across concurrent upload completions."""
    try:
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": workspace_bucket_advisory_lock_key(int(workspace_id), "object_storage")},
        )
    except Exception:
        # SQLite and mocked unit sessions do not support advisory locks. The
        # aggregate quota checks still run; production Postgres gets the lock.
        return


def _enforce_local_disk_headroom(incoming_size: int, *, settings: Settings) -> None:
    root = Path(str(settings.object_storage_local_dir or "/opt/surfaced/object-data"))
    root.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(root)
    min_free = int(settings.object_storage_min_free_bytes or DEFAULT_MIN_FREE_BYTES)
    if usage.free - int(incoming_size) < min_free:
        _reject_upload(
            "disk_headroom_exceeded",
            workspace_id=None,
            incoming_size=int(incoming_size),
            free_bytes=int(usage.free),
            min_free_bytes=min_free,
            root=str(root),
        )


def _reject_upload(reason: str, **fields: Any) -> None:
    details = " ".join(f"{key}={value}" for key, value in sorted(fields.items()) if value is not None)
    logger.warning("[ObjectStorage] reject_upload reason=%s %s", reason, details)
    messages = {
        "disk_headroom_exceeded": "server disk free space is below object storage safety threshold",
        "max_file_exceeded": "object exceeds local max file size",
        "total_quota_exceeded": "server object storage quota exceeded",
        "workspace_quota_exceeded": "workspace object storage quota exceeded",
    }
    raise ObjectStorageQuotaExceeded(messages.get(reason, reason))


def _is_local_upload_session(upload_session: ObjectUploadSession) -> bool:
    return str(upload_session.storage_provider_upload_id or "").startswith(LOCAL_STORAGE_PROVIDER_PREFIX)


def _max_file_bytes(settings: Settings | None = None) -> int:
    resolved_settings = settings or get_settings()
    return int(resolved_settings.object_storage_max_file_bytes or DEFAULT_MAX_FILE_BYTES)


def _s3_configured(settings: Settings) -> bool:
    return bool(
        str(settings.object_storage_endpoint_url or "").strip()
        and str(settings.object_storage_bucket or "").strip()
        and str(settings.object_storage_access_key_id or "").strip()
        and str(settings.object_storage_secret_access_key or "").strip()
    )


def _upload_response(
    *,
    strategy: str,
    sha256: str,
    size_bytes: int,
    storage_size_bytes: int,
    content_type: str,
    compression: str,
    storage_key: str,
    object_id: str | None = None,
    session_id: str | None = None,
    expires_at: datetime | None = None,
    upload: dict[str, Any] | None = None,
    part_size_bytes: int | None = None,
    parts_total: int | None = None,
) -> dict[str, Any]:
    return {
        "strategy": strategy,
        "object_id": object_id,
        "session_id": session_id,
        "sha256": sha256,
        "size_bytes": size_bytes,
        "storage_size_bytes": storage_size_bytes,
        "content_type": content_type,
        "compression": compression,
        "storage_key": storage_key,
        "upload": upload,
        "part_size_bytes": part_size_bytes,
        "parts_total": parts_total,
        "expires_at": expires_at,
    }


def _positive_int(value: int | None, *, field: str) -> int:
    safe_value = int(value or 0)
    if safe_value <= 0:
        raise ObjectStorageError(f"{field} must be greater than 0")
    return safe_value


def _canonical_query(query: dict[str, str]) -> str:
    return urlencode(sorted((str(key), str(value)) for key, value in query.items()), quote_via=quote, safe="-_.~")


def _signing_key(secret_access_key: str, date_stamp: str, region: str) -> bytes:
    key = f"AWS4{secret_access_key}".encode("utf-8")
    date_key = hmac.new(key, date_stamp.encode("utf-8"), hashlib.sha256).digest()
    date_region_key = hmac.new(date_key, region.encode("utf-8"), hashlib.sha256).digest()
    date_region_service_key = hmac.new(date_region_key, b"s3", hashlib.sha256).digest()
    return hmac.new(date_region_service_key, b"aws4_request", hashlib.sha256).digest()


def _find_xml_text(root: ET.Element, tag_name: str) -> str:
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == tag_name and element.text:
            return element.text
    return ""


def _complete_multipart_xml(parts: list[dict[str, Any]]) -> bytes:
    root = ET.Element("CompleteMultipartUpload")
    for part in parts:
        item = ET.SubElement(root, "Part")
        ET.SubElement(item, "PartNumber").text = str(int(part["part_number"]))
        ET.SubElement(item, "ETag").text = str(part["etag"])
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
