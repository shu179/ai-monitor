from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any

import requests

from .cloud_session_store import normalize_cloud_base_url


class CloudClientError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, response_body: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


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

    def post_events(self, access_token: str, events: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/sync/events",
            access_token=access_token,
            json_body={"events": events},
        )

    def list_tasks(self, access_token: str) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/v1/tasks", access_token=access_token)
        return payload if isinstance(payload, list) else []

    def list_admin_tasks(self, access_token: str) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/v1/admin/tasks", access_token=access_token)
        return payload if isinstance(payload, list) else []

    def list_admin_users(self, access_token: str) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/v1/admin/users", access_token=access_token)
        return payload if isinstance(payload, list) else []

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
        if str(last_event_id or "").strip():
            params["last_event_id"] = str(last_event_id or "").strip()

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
            raise CloudClientError(f"云端事件连接失败：{exc}") from exc

        try:
            body = None
            if response.status_code >= 400:
                body = _decode_response_body(response)
                message = _extract_error_message(body) or f"云端事件连接失败：HTTP {response.status_code}"
                raise CloudClientError(message, status_code=response.status_code, response_body=body)
            for event in iter_sse_events(response.iter_lines(decode_unicode=True)):
                yield event
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
        params: dict[str, Any] | None = None,
    ) -> Any:
        if not self.base_url:
            raise CloudClientError("未配置云端地址")
        headers = {"Accept": "application/json"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        token = str(access_token or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"

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
            raise CloudClientError(f"云端请求失败：{exc}") from exc

        body = _decode_response_body(response)
        if response.status_code >= 400:
            message = _extract_error_message(body) or f"云端请求失败：HTTP {response.status_code}"
            raise CloudClientError(message, status_code=response.status_code, response_body=body)
        return body


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
        if detail:
            return str(detail)
        message = body.get("message")
        if isinstance(message, str):
            return message
    return ""


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
