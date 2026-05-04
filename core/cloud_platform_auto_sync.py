from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .cloud_client import CloudClientError, SurfacedCloudClient
from .cloud_outbox import CloudOutbox
from .cloud_run_sync import flush_cloud_outbox
from .cloud_session_store import CloudSessionChangedError, CloudSessionStore, cloud_session_identity
from .time_utils import local_now


class CloudPlatformAutoSync:
    """Background sync loop for the account-based Surfaced cloud platform."""

    def __init__(
        self,
        *,
        pull_tasks: Callable[[], dict[str, Any]],
        session_store: CloudSessionStore | None = None,
        outbox: CloudOutbox | None = None,
        upload_retry_interval_seconds: float = 20.0,
        pull_interval_seconds: float = 300.0,
        idle_interval_seconds: float = 1.0,
        event_stream_enabled: bool = True,
        event_reconnect_seconds: float = 5.0,
        client_factory: Callable[[str], SurfacedCloudClient] | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self._pull_tasks = pull_tasks
        self._session_store = session_store or CloudSessionStore()
        self._outbox = outbox or CloudOutbox()
        self._upload_retry_interval_seconds = max(5.0, float(upload_retry_interval_seconds or 20.0))
        self._pull_interval_seconds = max(30.0, float(pull_interval_seconds or 300.0))
        self._idle_interval_seconds = max(0.2, float(idle_interval_seconds or 1.0))
        self._event_stream_enabled = bool(event_stream_enabled)
        self._event_reconnect_seconds = max(1.0, float(event_reconnect_seconds or 5.0))
        self._client_factory = client_factory or (lambda base_url: SurfacedCloudClient(base_url))
        self._logger = logger or print
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._event_thread: threading.Thread | None = None
        self._last_upload_started_at = 0.0
        self._last_pull_started_at = 0.0
        self._last_logged_in_key = ""
        self._status: dict[str, Any] = {
            "running": False,
            "logged_in": False,
            "event_stream_connected": False,
            "last_event_at": "",
            "last_upload_at": "",
            "last_pull_at": "",
            "last_error": "",
            "last_error_at": "",
        }

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="cloud-platform-auto-sync")
            self._thread.start()
            if self._event_stream_enabled:
                self._event_thread = threading.Thread(
                    target=self._event_stream_loop,
                    daemon=True,
                    name="cloud-platform-event-stream",
                )
                self._event_thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop_event.set()
            thread = self._thread
            event_thread = self._event_thread
            self._thread = None
            self._event_thread = None
        if thread:
            thread.join(timeout=2.0)
        if event_thread:
            event_thread.join(timeout=2.0)

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def _update_status(self, **patch: Any) -> None:
        with self._lock:
            self._status.update(patch)

    def _log(self, message: str) -> None:
        try:
            self._logger(f"[CloudPlatformAutoSync] {message}")
        except Exception:
            pass

    def _record_error(self, message: str) -> None:
        text = str(message or "云端自动同步失败").strip()
        self._update_status(last_error=text, last_error_at=local_now().isoformat(timespec="seconds"))
        self._log(text)

    def _clear_error(self) -> None:
        self._update_status(last_error="", last_error_at="")

    def _logged_in_key(self, session: dict[str, Any]) -> str:
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        return "|".join([
            str(session.get("base_url") or "").strip(),
            str(user.get("workspace_id") or "").strip(),
            str(user.get("id") or "").strip(),
        ])

    def _loop(self) -> None:
        self._update_status(running=True)
        try:
            while not self._stop_event.is_set():
                session = self._session_store.load()
                logged_in = bool(session.get("base_url") and session.get("access_token") and session.get("refresh_token"))
                self._update_status(logged_in=logged_in)
                if not logged_in:
                    self._last_logged_in_key = ""
                    self._stop_event.wait(self._idle_interval_seconds)
                    continue

                now = time.time()
                login_key = self._logged_in_key(session)
                first_sync_for_login = bool(login_key and login_key != self._last_logged_in_key)
                if first_sync_for_login:
                    self._last_logged_in_key = login_key
                    self._last_upload_started_at = 0.0
                    self._last_pull_started_at = 0.0

                active_outbox = self._outbox.bind_to_session(session)
                stats = active_outbox.stats()
                has_pending_upload = int(stats.get("pending") or 0) + int(stats.get("failed") or 0) > 0
                if has_pending_upload and (
                    first_sync_for_login
                    or self._last_upload_started_at <= 0
                    or now - self._last_upload_started_at >= self._upload_retry_interval_seconds
                ):
                    self._last_upload_started_at = now
                    upload_result = flush_cloud_outbox(outbox=active_outbox)
                    if upload_result.get("ok"):
                        self._update_status(last_upload_at=local_now().isoformat(timespec="seconds"))
                        self._clear_error()
                    else:
                        self._record_error(str(upload_result.get("message") or "运行数据自动上传失败"))

                if first_sync_for_login or now - self._last_pull_started_at >= self._pull_interval_seconds:
                    self._last_pull_started_at = now
                    pull_result = self._pull_tasks()
                    if pull_result.get("ok"):
                        self._update_status(last_pull_at=local_now().isoformat(timespec="seconds"))
                        self._clear_error()
                    else:
                        self._record_error(str(pull_result.get("message") or "云端任务自动拉取失败"))

                if self._stop_event.wait(0.1):
                    break
                CloudOutbox.wait_for_change(self._idle_interval_seconds)
        finally:
            self._update_status(running=False, event_stream_connected=False)

    def _event_stream_loop(self) -> None:
        last_event_id = ""
        while not self._stop_event.is_set():
            session = self._session_store.load()
            logged_in = bool(session.get("base_url") and session.get("access_token") and session.get("refresh_token"))
            if not logged_in:
                last_event_id = ""
                self._update_status(event_stream_connected=False)
                self._stop_event.wait(self._idle_interval_seconds)
                continue

            login_key = self._logged_in_key(session)
            base_url = str(session.get("base_url") or "").strip()
            access_token = str(session.get("access_token") or "").strip()
            refresh_token = str(session.get("refresh_token") or "").strip()
            identity = cloud_session_identity(session)
            try:
                client = self._client_factory(base_url)
                for event in client.stream_events(access_token, last_event_id=last_event_id):
                    if self._stop_event.is_set():
                        break
                    current_session = self._session_store.load()
                    if self._logged_in_key(current_session) != login_key:
                        last_event_id = ""
                        self._update_status(event_stream_connected=False)
                        break
                    event_name = str(event.get("event") or "message").strip()
                    event_id = str(event.get("id") or "").strip()
                    if event_id:
                        last_event_id = event_id
                    self._update_status(
                        event_stream_connected=True,
                        last_event_at=local_now().isoformat(timespec="seconds"),
                    )
                    if event_name == "session_revoked":
                        self._session_store.clear_if_current(
                            base_url=base_url,
                            access_token=access_token,
                            refresh_token=refresh_token,
                            workspace_id=identity["workspace_id"],
                            user_id=identity["user_id"],
                        )
                        self._update_status(logged_in=False, event_stream_connected=False)
                        break
                    if event_name in {"task_changed", "assignment_changed", "run_record_changed", "reference_changed", "workspace_changed"}:
                        self._pull_now_from_event(event_name)
                self._update_status(event_stream_connected=False)
            except CloudClientError as exc:
                self._update_status(event_stream_connected=False)
                if exc.status_code == 401 and refresh_token:
                    if self._refresh_event_token(
                        base_url=base_url,
                        access_token=access_token,
                        refresh_token=refresh_token,
                        workspace_id=identity["workspace_id"],
                        user_id=identity["user_id"],
                    ):
                        continue
                    self._session_store.clear_if_current(
                        base_url=base_url,
                        access_token=access_token,
                        refresh_token=refresh_token,
                        workspace_id=identity["workspace_id"],
                        user_id=identity["user_id"],
                    )
                    self._update_status(logged_in=False)
                else:
                    self._record_error(str(exc))
            except Exception as exc:
                self._update_status(event_stream_connected=False)
                self._record_error(f"云端事件监听失败：{exc}")

            self._stop_event.wait(self._event_reconnect_seconds)

    def _refresh_event_token(
        self,
        *,
        base_url: str,
        access_token: str,
        refresh_token: str,
        workspace_id: str = "",
        user_id: str = "",
    ) -> bool:
        try:
            client = self._client_factory(base_url)
            refreshed_session = self._session_store.refresh_login_if_current(
                base_url=base_url,
                access_token=access_token,
                refresh_token=refresh_token,
                refresh=client.refresh,
                workspace_id=workspace_id,
                user_id=user_id,
            )
            return bool(refreshed_session.get("access_token"))
        except CloudSessionChangedError as exc:
            self._record_error(str(exc))
            return False
        except CloudClientError as exc:
            if exc.status_code != 401:
                self._record_error(str(exc))
            return False

    def _pull_now_from_event(self, event_name: str) -> None:
        pull_result = self._pull_tasks()
        self._last_pull_started_at = time.time()
        if pull_result.get("ok"):
            self._update_status(last_pull_at=local_now().isoformat(timespec="seconds"))
            self._clear_error()
        else:
            self._record_error(str(pull_result.get("message") or f"云端事件同步失败：{event_name}"))
