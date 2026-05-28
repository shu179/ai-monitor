from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from .cloud_session_store import normalize_cloud_base_url

DEFAULT_CLOUD_CAPABILITY_HEADER = "sync-v2,batch-v2,object-v1,state-delta-v1"


class CloudClientError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: Any = None,
        retry_after_seconds: float | None = None,
        queue_depth_hint: int | None = None,
        throttle_bucket: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body
        self.retry_after_seconds = retry_after_seconds
        self.queue_depth_hint = queue_depth_hint
        self.throttle_bucket = str(throttle_bucket or "").strip()


class SurfacedCloudClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 15.0, session: requests.Session | None = None) -> None:
        self.base_url = normalize_cloud_base_url(base_url)
        self.timeout_seconds = float(timeout_seconds or 15.0)
        self._session = session or requests.Session()

    def login(
        self,
        *,
        username: str,
        password: str,
        device_id: str = "",
        app_version: str = "",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/auth/login",
            json_body={
                "username": username,
                "password": password,
                "device_id": device_id or None,
                "app_version": app_version or None,
            },
        )

    def register_admin(
        self,
        *,
        email: str,
        password: str,
        workspace_name: str,
        display_name: str = "",
    ) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/api/v1/auth/admin/register",
            json_body={
                "email": email,
                "password": password,
                "workspace_name": workspace_name,
                "display_name": display_name or None,
            },
        )
        return response if isinstance(response, dict) else {}

    def verify_email(
        self,
        *,
        email: str,
        code: str,
        device_id: str = "",
        app_version: str = "",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/auth/email/verify",
            json_body={
                "email": email,
                "code": code,
                "device_id": device_id or None,
                "app_version": app_version or None,
            },
        )

    def resend_email_verification(self, *, email: str) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/api/v1/auth/email/resend",
            json_body={"email": email},
        )
        return response if isinstance(response, dict) else {}

    def request_password_reset(self, *, email: str) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/api/v1/auth/password/reset/request",
            json_body={"email": email},
        )
        return response if isinstance(response, dict) else {}

    def reset_password(self, *, email: str, code: str, password: str) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/api/v1/auth/password/reset/confirm",
            json_body={"email": email, "code": code, "password": password},
        )
        return response if isinstance(response, dict) else {}

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/auth/refresh",
            json_body={"refresh_token": refresh_token},
        )

    def logout(self, refresh_token: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/auth/logout",
            json_body={"refresh_token": refresh_token},
        )

    def me(self, access_token: str) -> dict[str, Any]:
        return self._request("GET", "/api/v1/auth/me", access_token=access_token)

    def capabilities(
        self,
        access_token: str = "",
        *,
        capabilities_header: str = DEFAULT_CLOUD_CAPABILITY_HEADER,
    ) -> dict[str, Any]:
        response = self._request(
            "GET",
            "/api/v2/capabilities",
            access_token=access_token,
            extra_headers={"X-Cloud-Capability": capabilities_header} if capabilities_header else None,
        )
        return response if isinstance(response, dict) else {}

    def update_me_profile(self, access_token: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._request(
            "PATCH",
            "/api/v1/auth/me/profile",
            access_token=access_token,
            json_body=dict(payload or {}),
        )
        return response if isinstance(response, dict) else {}

    def post_events(self, access_token: str, events: list[dict[str, Any]], *, trace_id: str = "") -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/sync/events",
            access_token=access_token,
            json_body={"events": events},
            trace_id=trace_id,
        )

    def sync_changes(
        self,
        access_token: str,
        *,
        known_snapshot: dict[str, Any] | None = None,
        task_cursors: dict[int, int] | None = None,
        task_day_status_cursors: dict[int, int] | None = None,
    ) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/api/v1/sync/changes",
            access_token=access_token,
            json_body={
                "known_snapshot": dict(known_snapshot or {}),
                "task_cursors": {str(int(task_id)): int(cursor or 0) for task_id, cursor in (task_cursors or {}).items()},
                "task_day_status_cursors": {
                    str(int(task_id)): int(cursor or 0)
                    for task_id, cursor in (task_day_status_cursors or {}).items()
                },
            },
        )
        return response if isinstance(response, dict) else {}

    def state_delta(
        self,
        access_token: str,
        *,
        cursors: dict[str, int] | None = None,
        limit: int = 500,
        reset_token: str | None = None,
        bootstrap_cursor: str | None = None,
        device_id: str = "",
        priority: list[str] | None = None,
        trace_id: str = "",
    ) -> dict[str, Any]:
        safe_cursors: dict[str, int] = {}
        for key, value in (cursors or {}).items():
            stream = str(key or "").strip()
            if not stream:
                continue
            try:
                safe_cursors[stream] = max(0, int(value or 0))
            except Exception:
                safe_cursors[stream] = 0
        response = self._request(
            "POST",
            "/api/v2/sync/state-delta",
            access_token=access_token,
            json_body={
                "device_id": str(device_id or "").strip() or None,
                "cursors": safe_cursors,
                "limit": max(1, min(int(limit or 500), 1000)),
                "priority": [str(item) for item in (priority or []) if str(item or "").strip()],
                "reset_token": str(reset_token or "").strip() or None,
                "bootstrap_cursor": str(bootstrap_cursor or "").strip() or None,
            },
            trace_id=trace_id,
            extra_headers={"X-Cloud-Capability": DEFAULT_CLOUD_CAPABILITY_HEADER},
        )
        return response if isinstance(response, dict) else {}

    def create_object_download(
        self,
        access_token: str,
        object_id: str,
        *,
        trace_id: str = "",
    ) -> dict[str, Any]:
        safe_object_id = str(object_id or "").strip()
        if not safe_object_id:
            raise CloudClientError("object_id is required")
        response = self._request(
            "POST",
            f"/api/v2/objects/{safe_object_id}:download",
            access_token=access_token,
            trace_id=trace_id,
            extra_headers={"X-Cloud-Capability": DEFAULT_CLOUD_CAPABILITY_HEADER},
        )
        return response if isinstance(response, dict) else {}

    def create_object_upload(
        self,
        access_token: str,
        *,
        sha256: str,
        size_bytes: int,
        content_type: str,
        storage_size_bytes: int | None = None,
        compression: str = "auto",
        trace_id: str = "",
    ) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/api/v2/objects/uploads",
            access_token=access_token,
            json_body={
                "sha256": str(sha256 or "").strip(),
                "size_bytes": int(size_bytes or 0),
                "content_type": str(content_type or "").strip(),
                "storage_size_bytes": int(storage_size_bytes) if storage_size_bytes is not None else None,
                "compression": str(compression or "auto").strip() or "auto",
            },
            trace_id=trace_id,
            extra_headers={"X-Cloud-Capability": DEFAULT_CLOUD_CAPABILITY_HEADER},
        )
        return response if isinstance(response, dict) else {}

    def upload_object_content(
        self,
        access_token: str,
        upload_url: str,
        chunks: Iterable[bytes],
        *,
        content_type: str = "application/octet-stream",
        headers: dict[str, Any] | None = None,
        trace_id: str = "",
        read_timeout_seconds: float = 120.0,
    ) -> dict[str, Any]:
        body, _headers = self._put_object_content(
            access_token,
            upload_url,
            chunks,
            content_type=content_type,
            headers=headers,
            trace_id=trace_id,
            read_timeout_seconds=read_timeout_seconds,
        )
        return body if isinstance(body, dict) else {}

    def presign_object_upload_parts(
        self,
        access_token: str,
        session_id: str,
        part_numbers: list[int],
        *,
        trace_id: str = "",
    ) -> dict[str, Any]:
        safe_session_id = str(session_id or "").strip()
        if not safe_session_id:
            raise CloudClientError("session_id is required")
        response = self._request(
            "POST",
            f"/api/v2/objects/uploads/{safe_session_id}/parts:presign",
            access_token=access_token,
            json_body={"part_numbers": [int(item) for item in part_numbers]},
            trace_id=trace_id,
            extra_headers={"X-Cloud-Capability": DEFAULT_CLOUD_CAPABILITY_HEADER},
        )
        return response if isinstance(response, dict) else {}

    def upload_object_part_content(
        self,
        access_token: str,
        upload_url: str,
        chunks: Iterable[bytes],
        *,
        content_type: str = "application/octet-stream",
        headers: dict[str, Any] | None = None,
        trace_id: str = "",
        read_timeout_seconds: float = 120.0,
    ) -> dict[str, Any]:
        body, response_headers = self._put_object_content(
            access_token,
            upload_url,
            chunks,
            content_type=content_type,
            headers=headers,
            trace_id=trace_id,
            read_timeout_seconds=read_timeout_seconds,
        )
        result = body if isinstance(body, dict) else {}
        etag = _header_value(response_headers, "ETag") or result.get("etag") or result.get("ETag")
        if etag:
            result = dict(result)
            result["etag"] = str(etag).strip()
        return result

    def record_object_upload_part(
        self,
        access_token: str,
        session_id: str,
        *,
        part_number: int,
        etag: str,
        size_bytes: int,
        sha256: str | None = None,
        trace_id: str = "",
    ) -> dict[str, Any]:
        safe_session_id = str(session_id or "").strip()
        if not safe_session_id:
            raise CloudClientError("session_id is required")
        response = self._request(
            "POST",
            f"/api/v2/objects/uploads/{safe_session_id}/parts",
            access_token=access_token,
            json_body={
                "part_number": int(part_number),
                "etag": str(etag or "").strip(),
                "size_bytes": int(size_bytes or 0),
                "sha256": str(sha256).strip() if sha256 else None,
            },
            trace_id=trace_id,
            extra_headers={"X-Cloud-Capability": DEFAULT_CLOUD_CAPABILITY_HEADER},
        )
        return response if isinstance(response, dict) else {}

    def _put_object_content(
        self,
        access_token: str,
        upload_url: str,
        chunks: Iterable[bytes],
        *,
        content_type: str = "application/octet-stream",
        headers: dict[str, Any] | None = None,
        trace_id: str = "",
        read_timeout_seconds: float = 120.0,
    ) -> tuple[Any, Any]:
        safe_url = _resolve_transfer_url(self.base_url, upload_url)
        if not safe_url:
            raise CloudClientError("upload_url is required")
        request_headers = {
            "Accept": "application/json",
            "Content-Type": str(content_type or "application/octet-stream").strip() or "application/octet-stream",
        }
        token = str(access_token or "").strip()
        if token and _should_authorize_transfer_url(self.base_url, safe_url, upload_url):
            request_headers["Authorization"] = f"Bearer {token}"
        safe_trace_id = normalize_trace_id(trace_id)
        if safe_trace_id:
            request_headers["X-Trace-Id"] = safe_trace_id
        for key, value in (headers or {}).items():
            header_name = str(key or "").strip()
            header_value = str(value or "").strip()
            if header_name and header_value:
                request_headers[header_name] = header_value

        try:
            response = self._session.request(
                "PUT",
                safe_url,
                headers=request_headers,
                data=_iter_bytes(chunks),
                timeout=(min(10.0, self.timeout_seconds), max(30.0, float(read_timeout_seconds or 120.0))),
            )
        except requests.RequestException as exc:
            raise CloudClientError(_format_request_exception("云端对象上传", exc)) from exc

        body = _decode_response_body(response)
        if response.status_code >= 400:
            message = _extract_error_message(body) or f"云端对象上传失败：HTTP {response.status_code}"
            raise CloudClientError(
                message,
                status_code=response.status_code,
                response_body=body,
                **_extract_backpressure_metadata(response, body),
            )
        return body, getattr(response, "headers", {}) or {}

    def complete_object_upload(
        self,
        access_token: str,
        session_id: str,
        *,
        storage_size_bytes: int | None = None,
        compression: str = "auto",
        trace_id: str = "",
    ) -> dict[str, Any]:
        safe_session_id = str(session_id or "").strip()
        if not safe_session_id:
            raise CloudClientError("session_id is required")
        response = self._request(
            "POST",
            f"/api/v2/objects/uploads/{safe_session_id}:complete",
            access_token=access_token,
            json_body={
                "storage_size_bytes": int(storage_size_bytes) if storage_size_bytes is not None else None,
                "compression": str(compression or "auto").strip() or "auto",
            },
            trace_id=trace_id,
            extra_headers={"X-Cloud-Capability": DEFAULT_CLOUD_CAPABILITY_HEADER},
        )
        return response if isinstance(response, dict) else {}

    def iter_object_content(
        self,
        access_token: str,
        download_url: str,
        *,
        trace_id: str = "",
        chunk_size: int = 1024 * 1024,
        read_timeout_seconds: float = 120.0,
    ) -> Iterator[bytes]:
        safe_url = _resolve_transfer_url(self.base_url, download_url)
        if not safe_url:
            raise CloudClientError("download_url is required")
        headers = {"Accept": "application/octet-stream"}
        token = str(access_token or "").strip()
        if token and _should_authorize_transfer_url(self.base_url, safe_url, download_url):
            headers["Authorization"] = f"Bearer {token}"
        safe_trace_id = normalize_trace_id(trace_id)
        if safe_trace_id:
            headers["X-Trace-Id"] = safe_trace_id

        try:
            response = self._session.request(
                "GET",
                safe_url,
                headers=headers,
                stream=True,
                timeout=(min(10.0, self.timeout_seconds), max(30.0, float(read_timeout_seconds or 120.0))),
            )
        except requests.RequestException as exc:
            raise CloudClientError(_format_request_exception("云端对象下载", exc)) from exc

        try:
            body = None
            if response.status_code >= 400:
                body = _decode_response_body(response)
                message = _extract_error_message(body) or f"云端对象下载失败：HTTP {response.status_code}"
                raise CloudClientError(
                    message,
                    status_code=response.status_code,
                    response_body=body,
                    **_extract_backpressure_metadata(response, body),
                )
            safe_chunk_size = max(1, int(chunk_size or 1024 * 1024))
            try:
                for chunk in response.iter_content(chunk_size=safe_chunk_size):
                    if chunk:
                        yield bytes(chunk)
            except requests.RequestException as exc:
                raise CloudClientError(_format_request_exception("云端对象下载", exc)) from exc
        finally:
            try:
                response.close()
            except Exception:
                pass

    def list_tasks(self, access_token: str) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/v1/tasks", access_token=access_token)
        return payload if isinstance(payload, list) else []

    def list_tasks_by_ids(self, access_token: str, task_ids: list[int]) -> list[dict[str, Any]]:
        params: list[tuple[str, str]] = []
        seen: set[int] = set()
        for raw_task_id in task_ids or []:
            task_id = int(raw_task_id or 0)
            if task_id <= 0 or task_id in seen:
                continue
            seen.add(task_id)
            params.append(("ids", str(task_id)))
            if len(params) >= 200:
                break
        if not params:
            return []
        payload = self._request("GET", "/api/v1/tasks", access_token=access_token, params=params)
        return payload if isinstance(payload, list) else []

    def list_admin_tasks(self, access_token: str) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/v1/admin/tasks", access_token=access_token)
        return payload if isinstance(payload, list) else []

    def list_admin_users(self, access_token: str) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/v1/admin/users", access_token=access_token)
        return payload if isinstance(payload, list) else []

    def create_admin_user(self, access_token: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/api/v1/admin/users",
            access_token=access_token,
            json_body=dict(payload or {}),
        )
        return response if isinstance(response, dict) else {}

    def update_admin_user(self, access_token: str, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._request(
            "PATCH",
            f"/api/v1/admin/users/{int(user_id)}",
            access_token=access_token,
            json_body=dict(payload or {}),
        )
        return response if isinstance(response, dict) else {}

    def delete_admin_user(self, access_token: str, user_id: int) -> dict[str, Any]:
        response = self._request(
            "DELETE",
            f"/api/v1/admin/users/{int(user_id)}",
            access_token=access_token,
        )
        return response if isinstance(response, dict) else {}

    def create_admin_task(self, access_token: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/api/v1/admin/tasks",
            access_token=access_token,
            json_body=dict(payload or {}),
        )
        return response if isinstance(response, dict) else {}

    def update_admin_task(self, access_token: str, task_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._request(
            "PATCH",
            f"/api/v1/admin/tasks/{int(task_id)}",
            access_token=access_token,
            json_body=dict(payload or {}),
        )
        return response if isinstance(response, dict) else {}

    def delete_admin_task(self, access_token: str, task_id: int) -> dict[str, Any]:
        response = self._request(
            "DELETE",
            f"/api/v1/admin/tasks/{int(task_id)}",
            access_token=access_token,
        )
        return response if isinstance(response, dict) else {}

    def restore_admin_task(self, access_token: str, task_id: int) -> dict[str, Any]:
        response = self._request(
            "POST",
            f"/api/v1/admin/tasks/{int(task_id)}/restore",
            access_token=access_token,
            json_body={},
        )
        return response if isinstance(response, dict) else {}

    def list_deleted_tasks(self, access_token: str) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/v1/tasks/deleted", access_token=access_token)
        return payload if isinstance(payload, list) else []

    def assign_admin_task_member(
        self,
        access_token: str,
        task_id: int,
        *,
        user_id: int,
        access_level: str,
        note: str = "",
    ) -> dict[str, Any]:
        response = self._request(
            "POST",
            f"/api/v1/admin/tasks/{int(task_id)}/members",
            access_token=access_token,
            json_body={"user_id": int(user_id), "access_level": access_level, "note": note or None},
        )
        return response if isinstance(response, dict) else {}

    def clear_admin_task_operator(self, access_token: str, task_id: int) -> dict[str, Any]:
        response = self._request(
            "DELETE",
            f"/api/v1/admin/tasks/{int(task_id)}/members/operator",
            access_token=access_token,
        )
        return response if isinstance(response, dict) else {}

    def admin_object_storage_report(self, access_token: str) -> dict[str, Any]:
        response = self._request(
            "GET",
            "/api/v1/admin/ops/object-storage",
            access_token=access_token,
        )
        return response if isinstance(response, dict) else {}

    def admin_sync_queue_report(self, access_token: str) -> dict[str, Any]:
        response = self._request(
            "GET",
            "/api/v1/admin/ops/sync-queue",
            access_token=access_token,
        )
        return response if isinstance(response, dict) else {}

    def list_admin_article_classification_jobs(
        self,
        access_token: str,
        *,
        status: str = "unresolved",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/api/v1/admin/articles/classification-jobs",
            access_token=access_token,
            params={
                "status": str(status or "unresolved").strip()[:32] or "unresolved",
                "limit": int(limit or 200),
            },
        )
        return payload if isinstance(payload, list) else []

    def resolve_admin_article_classification_job(
        self,
        access_token: str,
        job_id: int,
        *,
        task_id: int,
        reason: str = "",
    ) -> dict[str, Any]:
        response = self._request(
            "POST",
            f"/api/v1/admin/articles/classification-jobs/{int(job_id)}/resolve",
            access_token=access_token,
            json_body={
                "task_id": int(task_id),
                "reason": str(reason or "").strip() or None,
            },
        )
        return response if isinstance(response, dict) else {}

    def ignore_admin_article_classification_job(
        self,
        access_token: str,
        job_id: int,
        *,
        reason: str = "",
    ) -> dict[str, Any]:
        response = self._request(
            "POST",
            f"/api/v1/admin/articles/classification-jobs/{int(job_id)}/ignore",
            access_token=access_token,
            json_body={"reason": str(reason or "").strip() or None},
        )
        return response if isinstance(response, dict) else {}

    def task_run_records(
        self,
        access_token: str,
        task_id: int,
        *,
        limit: int = 50,
        since_id: int | None = None,
    ) -> list[dict[str, Any]]:
        params = {"limit": int(limit or 50)}
        if since_id is not None and int(since_id or 0) > 0:
            params["since_id"] = int(since_id or 0)
        payload = self._request(
            "GET",
            f"/api/v1/tasks/{int(task_id)}/run-records",
            access_token=access_token,
            params=params,
        )
        return payload if isinstance(payload, list) else []

    def task_run_records_batch(
        self,
        access_token: str,
        task_cursors: dict[int, int],
        *,
        limit_per_task: int = 6000,
    ) -> dict[int, list[dict[str, Any]]]:
        response = self._request(
            "POST",
            "/api/v1/tasks/run-records/batch",
            access_token=access_token,
            json_body={
                "task_cursors": {str(int(task_id)): int(cursor or 0) for task_id, cursor in (task_cursors or {}).items()},
                "limit_per_task": int(limit_per_task or 6000),
            },
        )
        source = response.get("records") if isinstance(response, dict) else {}
        if not isinstance(source, dict):
            return {}
        records: dict[int, list[dict[str, Any]]] = {}
        for raw_task_id, raw_items in source.items():
            try:
                task_id = int(raw_task_id)
            except Exception:
                continue
            if not isinstance(raw_items, list):
                records[task_id] = []
                continue
            records[task_id] = [item for item in raw_items if isinstance(item, dict)]
        return records

    def task_day_status_events_batch(
        self,
        access_token: str,
        task_cursors: dict[int, int],
        *,
        limit_per_task: int = 500,
    ) -> dict[int, list[dict[str, Any]]]:
        response = self._request(
            "POST",
            "/api/v1/tasks/day-status-events/batch",
            access_token=access_token,
            json_body={
                "task_cursors": {str(int(task_id)): int(cursor or 0) for task_id, cursor in (task_cursors or {}).items()},
                "limit_per_task": int(limit_per_task or 500),
            },
        )
        source = response.get("events") if isinstance(response, dict) else {}
        if not isinstance(source, dict):
            return {}
        events: dict[int, list[dict[str, Any]]] = {}
        for raw_task_id, raw_items in source.items():
            try:
                task_id = int(raw_task_id)
            except Exception:
                continue
            if not isinstance(raw_items, list):
                events[task_id] = []
                continue
            events[task_id] = [item for item in raw_items if isinstance(item, dict)]
        return events

    def task_articles(
        self,
        access_token: str,
        *,
        updated_after: str = "",
        updated_after_id: int = 0,
        limit: int = 5000,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": int(limit or 5000)}
        cursor = str(updated_after or "").strip()
        if cursor:
            params["updated_after"] = cursor
            if int(updated_after_id or 0) > 0:
                params["updated_after_id"] = int(updated_after_id or 0)
        response = self._request(
            "GET",
            "/api/v1/tasks/articles",
            access_token=access_token,
            params=params,
        )
        return response if isinstance(response, dict) else {"articles": [], "count": 0, "max_updated_at": "", "max_article_id": 0}

    def stream_events(
        self,
        access_token: str,
        *,
        last_event_id: str = "",
        read_timeout_seconds: float = 75.0,
    ) -> Iterator[dict[str, Any]]:
        if not self.base_url:
            raise CloudClientError("未配置云端地址")
        token = str(access_token or "").strip()
        if not token:
            raise CloudClientError("未登录云端", status_code=401)
        headers = {
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
            "Authorization": f"Bearer {token}",
        }
        params: dict[str, Any] = {}
        safe_last_event_id = str(last_event_id or "").strip()
        if safe_last_event_id and len(safe_last_event_id) <= 256:
            params["last_event_id"] = safe_last_event_id

        try:
            response = self._session.request(
                "GET",
                f"{self.base_url}/api/v1/events/stream",
                headers=headers,
                params=params or None,
                stream=True,
                timeout=(min(10.0, self.timeout_seconds), max(15.0, float(read_timeout_seconds or 75.0))),
            )
        except requests.RequestException as exc:
            raise CloudClientError(_format_request_exception("云端事件连接", exc)) from exc

        try:
            body = None
            if response.status_code >= 400:
                body = _decode_response_body(response)
                message = _extract_error_message(body) or f"云端事件连接失败：HTTP {response.status_code}"
                raise CloudClientError(
                    message,
                    status_code=response.status_code,
                    response_body=body,
                    **_extract_backpressure_metadata(response, body),
                )
            try:
                for event in iter_sse_events(response.iter_lines(decode_unicode=True)):
                    yield event
            except requests.RequestException as exc:
                raise CloudClientError(_format_request_exception("云端事件连接", exc)) from exc
        finally:
            try:
                response.close()
            except Exception:
                pass

    def _request(
        self,
        method: str,
        path: str,
        *,
        access_token: str = "",
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | list[tuple[str, str]] | None = None,
        trace_id: str = "",
        extra_headers: dict[str, Any] | None = None,
    ) -> Any:
        if not self.base_url:
            raise CloudClientError("未配置云端地址")
        headers = {"Accept": "application/json"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        token = str(access_token or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        safe_trace_id = normalize_trace_id(trace_id)
        if safe_trace_id:
            headers["X-Trace-Id"] = safe_trace_id
        if isinstance(extra_headers, dict):
            for key, value in extra_headers.items():
                header_name = str(key or "").strip()
                header_value = str(value or "").strip()
                if header_name and header_value:
                    headers[header_name] = header_value

        try:
            response = self._session.request(
                method.upper(),
                f"{self.base_url}{path}",
                headers=headers,
                json=json_body,
                params=params,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise CloudClientError(_format_request_exception("云端请求", exc)) from exc

        body = _decode_response_body(response)
        if response.status_code >= 400:
            message = _extract_error_message(body) or f"云端请求失败：HTTP {response.status_code}"
            raise CloudClientError(
                message,
                status_code=response.status_code,
                response_body=body,
                **_extract_backpressure_metadata(response, body),
            )
        return body


def new_trace_id(prefix: str = "local") -> str:
    safe_prefix = "".join(ch for ch in str(prefix or "local").lower() if ch.isalnum() or ch in {"-", "_"})[:24]
    return f"{safe_prefix or 'local'}-{uuid.uuid4().hex[:24]}"


def normalize_trace_id(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return "".join(ch for ch in text if ch.isalnum() or ch in {"-", "_", "."})[:128]


def _decode_response_body(response: requests.Response) -> Any:
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return response.text


def _extract_error_message(body: Any) -> str:
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, dict):
            message = detail.get("message")
            if isinstance(message, str):
                return message
        if isinstance(detail, list):
            return _format_validation_errors(detail)
        if detail:
            return str(detail)
        message = body.get("message")
        if isinstance(message, str):
            return message
    return ""


def _extract_backpressure_metadata(response: requests.Response, body: Any) -> dict[str, Any]:
    headers = getattr(response, "headers", {}) or {}
    retry_after_seconds = _parse_retry_after(_header_value(headers, "Retry-After"))
    queue_depth_hint = _parse_int(_header_value(headers, "X-Queue-Depth-Hint"))
    throttle_bucket = str(_header_value(headers, "X-Throttle-Bucket") or "").strip()

    detail = body.get("detail") if isinstance(body, dict) else None
    sources = [body if isinstance(body, dict) else {}, detail if isinstance(detail, dict) else {}]
    if retry_after_seconds is None:
        for source in sources:
            retry_after_seconds = _parse_float(
                source.get("retry_after_seconds")
                or source.get("retry_after")
                or source.get("retryAfterSeconds")
            )
            if retry_after_seconds is not None:
                break
    if queue_depth_hint is None:
        for source in sources:
            queue_depth_hint = _parse_int(
                source.get("queue_depth_hint")
                or source.get("queueDepthHint")
            )
            if queue_depth_hint is not None:
                break
    if not throttle_bucket:
        for source in sources:
            throttle_bucket = str(
                source.get("throttle_bucket")
                or source.get("throttleBucket")
                or ""
            ).strip()
            if throttle_bucket:
                break

    return {
        "retry_after_seconds": retry_after_seconds,
        "queue_depth_hint": queue_depth_hint,
        "throttle_bucket": throttle_bucket,
    }


def _header_value(headers: Any, key: str) -> Any:
    try:
        return headers.get(key)
    except Exception:
        pass
    lower_key = key.lower()
    try:
        for raw_key, value in headers.items():
            if str(raw_key).lower() == lower_key:
                return value
    except Exception:
        return None
    return None


def _resolve_transfer_url(base_url: str, transfer_url: str) -> str:
    text = str(transfer_url or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme and parsed.netloc:
        return text
    if not base_url:
        return text
    if text.startswith("/"):
        return f"{base_url}{text}"
    return urljoin(f"{base_url}/", text)


def _should_authorize_transfer_url(base_url: str, resolved_url: str, raw_url: str) -> bool:
    raw_text = str(raw_url or "").strip()
    if not raw_text:
        return False
    raw_parsed = urlparse(raw_text)
    if not raw_parsed.scheme and not raw_parsed.netloc:
        return True
    base = urlparse(base_url)
    target = urlparse(resolved_url)
    return bool(base.scheme and base.netloc and base.scheme == target.scheme and base.netloc == target.netloc)


def _iter_bytes(chunks: Iterable[bytes]) -> Iterator[bytes]:
    for chunk in chunks:
        if chunk:
            yield bytes(chunk)


def _parse_retry_after(value: Any) -> float | None:
    seconds = _parse_float(value)
    if seconds is not None:
        return max(0.0, seconds)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        when = parsedate_to_datetime(text)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


def _parse_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _parse_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except Exception:
        return None


def _format_request_exception(prefix: str, exc: requests.RequestException) -> str:
    text = str(exc or "")
    if isinstance(exc, (requests.ReadTimeout, requests.Timeout)) or "read timed out" in text.lower():
        if "事件" in str(prefix or ""):
            return f"{prefix}超时，正在自动重连"
        return f"{prefix}超时，将自动重试"
    if isinstance(exc, requests.ConnectionError):
        if str(prefix or "").endswith("连接"):
            return f"{prefix}失败，将自动重试"
        return f"{prefix}连接失败，将自动重试"
    return f"{prefix}失败：{exc}"


def _format_validation_errors(errors: list[Any]) -> str:
    messages: list[str] = []
    for error in errors[:3]:
        if not isinstance(error, dict):
            continue
        loc = error.get("loc")
        field = ""
        if isinstance(loc, list) and loc:
            field = str(loc[-1] or "").strip()
        error_type = str(error.get("type") or "").strip().lower()
        msg = str(error.get("msg") or "").strip()
        msg_lower = msg.lower()
        if field and (
            "too long" in msg_lower
            or error_type == "string_too_long"
            or ("at most" in msg_lower and "character" in msg_lower)
        ):
            messages.append(f"{field} 超出长度限制")
        elif field and msg:
            messages.append(f"{field}：{msg}")
        elif msg:
            messages.append(msg)
    return f"云端参数校验失败：{'；'.join(messages)}" if messages else "云端参数校验失败"


def iter_sse_events(lines: Iterable[str | bytes]) -> Iterator[dict[str, Any]]:
    event_type = "message"
    event_id = ""
    data_lines: list[str] = []

    def flush() -> dict[str, Any] | None:
        nonlocal event_type, event_id, data_lines
        if not data_lines:
            event_type = "message"
            event_id = ""
            return None
        raw_data = "\n".join(data_lines)
        parsed_data: Any = raw_data
        try:
            parsed_data = json.loads(raw_data)
        except ValueError:
            pass
        event = {"event": event_type or "message", "data": parsed_data, "id": event_id, "raw_data": raw_data}
        event_type = "message"
        event_id = ""
        data_lines = []
        return event

    for raw_line in lines:
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8", errors="replace")
        else:
            line = str(raw_line)
        line = line.rstrip("\r")
        if line == "":
            event = flush()
            if event is not None:
                yield event
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_type = value
        elif field == "id":
            event_id = value
        elif field == "data":
            data_lines.append(value)

    event = flush()
    if event is not None:
        yield event
