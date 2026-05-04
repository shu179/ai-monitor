from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from .app_paths import resolve_app_path
from .time_utils import local_now


DEFAULT_CLOUD_SESSION_PATH = resolve_app_path("user_data/cloud_session.json")


def normalize_cloud_base_url(value: Any) -> str:
    text = str(value or "").strip()
    return text.rstrip("/")


class CloudSessionChangedError(RuntimeError):
    """Raised when an in-flight cloud request no longer belongs to the active local login."""


def cloud_session_identity(session: dict[str, Any] | None) -> dict[str, str]:
    payload = session if isinstance(session, dict) else {}
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    return {
        "base_url": normalize_cloud_base_url(payload.get("base_url")),
        "workspace_id": str(user.get("workspace_id") or "").strip(),
        "user_id": str(user.get("id") or "").strip(),
    }


def cloud_session_identity_key(session: dict[str, Any] | None) -> str:
    identity = cloud_session_identity(session)
    return "|".join([identity["base_url"], identity["workspace_id"], identity["user_id"]])


class CloudSessionStore:
    """Persist cloud login state locally.

    Tokens are intentionally kept out of config.yaml so they do not get copied
    into settings exports or legacy bundle sync payloads.
    """

    _refresh_lock = threading.RLock()

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_CLOUD_SESSION_PATH
        self._lock = threading.RLock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                return data if isinstance(data, dict) else {}
            except FileNotFoundError:
                return {}
            except Exception:
                return {}

    def save(self, session: dict[str, Any]) -> dict[str, Any]:
        payload = dict(session or {})
        payload["base_url"] = normalize_cloud_base_url(payload.get("base_url"))
        payload["saved_at"] = local_now().isoformat(timespec="seconds")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=str(self.path.parent), prefix=".cloud_session_", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                os.replace(tmp_path, self.path)
                _chmod_user_only(self.path)
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        return payload

    def save_login(self, *, base_url: str, token_pair: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "base_url": normalize_cloud_base_url(base_url),
            "access_token": str((token_pair or {}).get("access_token") or "").strip(),
            "refresh_token": str((token_pair or {}).get("refresh_token") or "").strip(),
            "token_type": str((token_pair or {}).get("token_type") or "bearer").strip() or "bearer",
            "user": (token_pair or {}).get("user") if isinstance((token_pair or {}).get("user"), dict) else {},
        }
        return self.save(payload)

    def clear(self) -> None:
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def clear_if_current(
        self,
        *,
        base_url: str,
        access_token: str = "",
        refresh_token: str = "",
        workspace_id: Any = "",
        user_id: Any = "",
    ) -> bool:
        """Clear the saved session only if it still matches the caller's stale tokens."""
        normalized_base_url = normalize_cloud_base_url(base_url)
        expected_access_token = str(access_token or "").strip()
        expected_refresh_token = str(refresh_token or "").strip()
        expected_workspace_id = str(workspace_id or "").strip()
        expected_user_id = str(user_id or "").strip()
        with self._refresh_lock:
            session = self.load()
            identity = cloud_session_identity(session)
            if normalized_base_url and identity["base_url"] != normalized_base_url:
                return False
            if expected_workspace_id and identity["workspace_id"] != expected_workspace_id:
                return False
            if expected_user_id and identity["user_id"] != expected_user_id:
                return False
            if expected_access_token and str(session.get("access_token") or "").strip() != expected_access_token:
                return False
            if expected_refresh_token and str(session.get("refresh_token") or "").strip() != expected_refresh_token:
                return False
            self.clear()
            return True

    def refresh_login_if_current(
        self,
        *,
        base_url: str,
        access_token: str,
        refresh_token: str,
        refresh: Callable[[str], dict[str, Any]],
        workspace_id: Any = "",
        user_id: Any = "",
    ) -> dict[str, Any]:
        """Refresh tokens once, reusing another thread's newer login if it already won.

        The cloud server rotates refresh tokens. Without this guard, two background
        workers can refresh the same stale token at the same time: one succeeds,
        the other receives 401 for the now-revoked token and clears the valid
        session saved by the first worker.
        """
        normalized_base_url = normalize_cloud_base_url(base_url)
        expected_access_token = str(access_token or "").strip()
        expected_refresh_token = str(refresh_token or "").strip()
        expected_workspace_id = str(workspace_id or "").strip()
        expected_user_id = str(user_id or "").strip()
        with self._refresh_lock:
            session = self.load()
            current_identity = cloud_session_identity(session)
            current_base_url = current_identity["base_url"]
            current_access_token = str(session.get("access_token") or "").strip()
            current_refresh_token = str(session.get("refresh_token") or "").strip()
            if current_base_url != normalized_base_url:
                raise CloudSessionChangedError("云端账号已切换，本次请求已中止")
            if expected_workspace_id and current_identity["workspace_id"] != expected_workspace_id:
                raise CloudSessionChangedError("云端账号已切换，本次请求已中止")
            if expected_user_id and current_identity["user_id"] != expected_user_id:
                raise CloudSessionChangedError("云端账号已切换，本次请求已中止")
            if not current_refresh_token:
                raise CloudSessionChangedError("云端账号已退出，本次请求已中止")
            if (
                current_access_token
                and expected_access_token
                and current_access_token != expected_access_token
            ):
                return session
            if (
                current_refresh_token
                and expected_refresh_token
                and current_refresh_token != expected_refresh_token
            ):
                return session
            token_pair = refresh(expected_refresh_token or current_refresh_token)
            return self.save_login(base_url=normalized_base_url, token_pair=token_pair)

    def is_logged_in(self) -> bool:
        session = self.load()
        return bool(session.get("base_url") and session.get("access_token") and session.get("refresh_token"))


def _chmod_user_only(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        path.chmod(0o600)
    except Exception:
        pass
