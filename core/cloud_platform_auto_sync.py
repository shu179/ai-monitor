from __future__ import annotations

import inspect
import random
import threading
import time
from typing import Any, Callable

from .cloud_client import CloudClientError, SurfacedCloudClient
from .cloud_event_types import (
    EVENT_SESSION_REVOKED,
    PULL_TRIGGER_EVENT_NAMES,
)
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
        recover_upload_candidates: Callable[[], dict[str, Any]] | None = None,
        session_store: CloudSessionStore | None = None,
        outbox: CloudOutbox | None = None,
        upload_retry_interval_seconds: float = 5.0,
        pull_interval_seconds: float = 300.0,
        idle_interval_seconds: float = 1.0,
        event_stream_enabled: bool = True,
        event_reconnect_seconds: float = 5.0,
        event_reconnect_max_seconds: float = 30.0,
        event_reconnect_jitter_ratio: float = 0.2,
        client_factory: Callable[[str], SurfacedCloudClient] | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self._pull_tasks = pull_tasks
        self._recover_upload_candidates = recover_upload_candidates
        self._session_store = session_store or CloudSessionStore()
        self._outbox = outbox or CloudOutbox()
        self._upload_retry_interval_seconds = max(5.0, float(upload_retry_interval_seconds or 20.0))
        self._pull_interval_seconds = max(30.0, float(pull_interval_seconds or 300.0))
        self._idle_interval_seconds = max(0.2, float(idle_interval_seconds or 1.0))
        self._event_stream_enabled = bool(event_stream_enabled)
        self._event_reconnect_seconds = max(1.0, float(event_reconnect_seconds or 5.0))
        self._event_reconnect_max_seconds = max(
            self._event_reconnect_seconds,
            float(event_reconnect_max_seconds or 30.0),
        )
        self._event_reconnect_jitter_ratio = min(
            0.5,
            max(0.0, float(event_reconnect_jitter_ratio or 0.0)),
        )
        self._client_factory = client_factory or (lambda base_url: SurfacedCloudClient(base_url, timeout_seconds=30.0))
        self._logger = logger or print
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._event_thread: threading.Thread | None = None
        self._last_upload_started_at = 0.0
        self._last_pull_started_at = 0.0
        self._last_logged_in_key = ""
        self._last_error_log_text = ""
        self._last_error_log_at = 0.0
        self._last_transient_error_log_at = 0.0
        self._status: dict[str, Any] = {
            "running": False,
            "logged_in": False,
            "event_stream_connected": False,
            "event_reconnect_attempts": 0,
            "next_event_reconnect_seconds": 0.0,
            "last_event_at": "",
            "last_upload_at": "",
            "last_pull_at": "",
            "last_error": "",
            "last_error_at": "",
            "last_upload_metrics": {},
            "last_pull_metrics": {},
            "last_pull_summary": {},
            "startup_recovery_running": False,
            "last_startup_recovery_at": "",
            "last_startup_recovery_metrics": {},
            "last_startup_recovery_error": "",
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
        text = _normalize_auto_sync_error(message)
        self._update_status(last_error=text, last_error_at=local_now().isoformat(timespec="seconds"))
        now = time.monotonic()
        if _is_transient_auto_sync_error(text):
            if now - self._last_transient_error_log_at >= 300.0:
                self._last_transient_error_log_at = now
                self._log(text)
            return
        if text != self._last_error_log_text or now - self._last_error_log_at >= 60.0:
            self._last_error_log_text = text
            self._last_error_log_at = now
            self._log(text)

    def _clear_error(self) -> None:
        self._update_status(last_error="", last_error_at="")

    def _event_reconnect_delay(self, failure_count: int) -> float:
        failures = max(1, int(failure_count or 1))
        delay = min(
            self._event_reconnect_max_seconds,
            self._event_reconnect_seconds * (2 ** (failures - 1)),
        )
        if self._event_reconnect_jitter_ratio > 0:
            spread = delay * self._event_reconnect_jitter_ratio
            delay = random.uniform(max(0.1, delay - spread), delay + spread)
        return max(0.1, min(self._event_reconnect_max_seconds, delay))

    def _finish_startup_recovery(
        self,
        recovery_result: dict[str, Any] | None,
        upload_result: dict[str, Any] | None,
        pull_result: dict[str, Any] | None,
    ) -> None:
        upload_metrics = (
            upload_result.get("metrics")
            if isinstance(upload_result, dict) and isinstance(upload_result.get("metrics"), dict)
            else {}
        )
        pull_metrics = _pull_metrics(pull_result) if isinstance(pull_result, dict) else {}
        errors = []
        if isinstance(recovery_result, dict) and not recovery_result.get("ok"):
            errors.append(str(recovery_result.get("message") or "历史运行数据恢复失败"))
        if isinstance(upload_result, dict) and not upload_result.get("ok"):
            errors.append(str(upload_result.get("message") or "上传恢复失败"))
        if isinstance(pull_result, dict) and not pull_result.get("ok"):
            errors.append(str(pull_result.get("message") or "下放恢复失败"))
        self._update_status(
            startup_recovery_running=False,
            last_startup_recovery_at=local_now().isoformat(timespec="seconds"),
            last_startup_recovery_metrics={
                "history_recovery": dict(recovery_result or {}),
                "upload": upload_metrics,
                "pull": pull_metrics,
            },
            last_startup_recovery_error="；".join([item for item in errors if item]),
        )

    def _logged_in_key(self, session: dict[str, Any]) -> str:
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        return "|".join([
            str(session.get("base_url") or "").strip(),
            str(user.get("workspace_id") or "").strip(),
            str(user.get("id") or "").strip(),
        ])

    def _loop(self) -> None:
        self._update_status(running=True)
        upload_wake_requested = False
        try:
            while not self._stop_event.is_set():
                session = self._session_store.load()
                logged_in = bool(session.get("base_url") and session.get("access_token") and session.get("refresh_token"))
                self._update_status(logged_in=logged_in)
                if not logged_in:
                    self._last_logged_in_key = ""
                    self._update_status(startup_recovery_running=False)
                    self._stop_event.wait(self._idle_interval_seconds)
                    continue

                now = time.monotonic()
                login_key = self._logged_in_key(session)
                first_sync_for_login = bool(login_key and login_key != self._last_logged_in_key)
                if first_sync_for_login:
                    self._last_logged_in_key = login_key
                    self._last_upload_started_at = 0.0
                    self._last_pull_started_at = 0.0
                    self._update_status(
                        startup_recovery_running=True,
                        last_startup_recovery_error="",
                    )

                recovery_result: dict[str, Any] | None = None
                if first_sync_for_login and self._recover_upload_candidates is not None:
                    try:
                        recovery_result = self._recover_upload_candidates()
                    except Exception as exc:
                        recovery_result = {"ok": False, "message": f"历史运行数据恢复失败：{exc}"}

                active_outbox = self._outbox.bind_to_session(session)
                stats = active_outbox.stats()
                has_pending_upload = int(stats.get("pending") or 0) + int(stats.get("failed") or 0) > 0
                upload_result: dict[str, Any] | None = None
                if first_sync_for_login:
                    self._last_upload_started_at = now
                    try:
                        upload_result = flush_cloud_outbox(outbox=active_outbox, limit=500)
                    except Exception as exc:
                        upload_result = {"ok": False, "message": f"运行数据恢复上传失败：{exc}", "metrics": {}}
                elif has_pending_upload and (
                    upload_wake_requested
                    or self._last_upload_started_at <= 0
                    or now - self._last_upload_started_at >= self._upload_retry_interval_seconds
                ):
                    self._last_upload_started_at = now
                    try:
                        upload_result = flush_cloud_outbox(outbox=active_outbox, limit=500)
                    except Exception as exc:
                        upload_result = {"ok": False, "message": f"运行数据自动上传失败：{exc}", "metrics": {}}

                if upload_result is not None:
                    upload_wake_requested = False
                    if upload_result.get("ok"):
                        self._update_status(
                            last_upload_at=local_now().isoformat(timespec="seconds"),
                            last_upload_metrics=upload_result.get("metrics") if isinstance(upload_result.get("metrics"), dict) else {},
                        )
                        self._clear_error()
                    else:
                        self._update_status(
                            last_upload_metrics=upload_result.get("metrics") if isinstance(upload_result.get("metrics"), dict) else {},
                        )
                        self._record_error(str(upload_result.get("message") or "运行数据自动上传失败"))

                pull_result: dict[str, Any] | None = None
                if first_sync_for_login or now - self._last_pull_started_at >= self._pull_interval_seconds:
                    self._last_pull_started_at = now
                    try:
                        pull_result = self._invoke_pull_tasks(force=False)
                    except Exception as exc:
                        pull_result = {"ok": False, "message": f"云端任务自动拉取失败：{exc}", "summary": {}}
                    if pull_result.get("ok"):
                        pull_metrics = _pull_metrics(pull_result)
                        self._update_status(
                            last_pull_at=local_now().isoformat(timespec="seconds"),
                            last_pull_metrics=pull_metrics,
                            last_pull_summary=_pull_summary(pull_metrics),
                        )
                        self._clear_error()
                    else:
                        pull_metrics = _pull_metrics(pull_result)
                        self._update_status(last_pull_metrics=pull_metrics, last_pull_summary=_pull_summary(pull_metrics))
                        self._record_error(str(pull_result.get("message") or "云端任务自动拉取失败"))

                if first_sync_for_login:
                    self._finish_startup_recovery(recovery_result, upload_result, pull_result)

                if self._stop_event.wait(0.1):
                    break
                upload_wake_requested = CloudOutbox.wait_for_change(self._idle_interval_seconds)
        finally:
            self._update_status(running=False, event_stream_connected=False)

    def _event_stream_loop(self) -> None:
        last_event_id = ""
        last_pulled_event_id = ""
        reconnect_failures = 0
        while not self._stop_event.is_set():
            session = self._session_store.load()
            logged_in = bool(session.get("base_url") and session.get("access_token") and session.get("refresh_token"))
            if not logged_in:
                last_event_id = ""
                last_pulled_event_id = ""
                reconnect_failures = 0
                self._update_status(
                    event_stream_connected=False,
                    event_reconnect_attempts=0,
                    next_event_reconnect_seconds=0.0,
                )
                self._stop_event.wait(self._idle_interval_seconds)
                continue

            login_key = self._logged_in_key(session)
            base_url = str(session.get("base_url") or "").strip()
            access_token = str(session.get("access_token") or "").strip()
            refresh_token = str(session.get("refresh_token") or "").strip()
            identity = cloud_session_identity(session)
            stream_saw_event = False
            session_changed = False
            try:
                client = self._client_factory(base_url)
                for event in client.stream_events(access_token, last_event_id=last_event_id):
                    if self._stop_event.is_set():
                        break
                    current_session = self._session_store.load()
                    if self._logged_in_key(current_session) != login_key:
                        last_event_id = ""
                        last_pulled_event_id = ""
                        self._update_status(event_stream_connected=False)
                        session_changed = True
                        break
                    event_name = str(event.get("event") or "message").strip()
                    event_id = str(event.get("id") or "").strip()
                    if event_id:
                        last_event_id = event_id
                    stream_saw_event = True
                    reconnect_failures = 0
                    self._update_status(
                        event_stream_connected=True,
                        event_reconnect_attempts=0,
                        next_event_reconnect_seconds=0.0,
                        last_event_at=local_now().isoformat(timespec="seconds"),
                    )
                    if event_name == EVENT_SESSION_REVOKED:
                        self._session_store.clear_if_current(
                            base_url=base_url,
                            access_token=access_token,
                            refresh_token=refresh_token,
                            workspace_id=identity["workspace_id"],
                            user_id=identity["user_id"],
                        )
                        self._update_status(logged_in=False, event_stream_connected=False)
                        session_changed = True
                        break
                    pull_event_key = event_id or f"{event_name}:{time.monotonic()}"
                    if event_name in PULL_TRIGGER_EVENT_NAMES and pull_event_key != last_pulled_event_id:
                        last_pulled_event_id = pull_event_key
                        self._pull_now_from_event(event_name)
                self._update_status(event_stream_connected=False)
                if session_changed:
                    reconnect_failures = 0
                elif not stream_saw_event:
                    reconnect_failures += 1
            except CloudClientError as exc:
                self._update_status(event_stream_connected=False)
                if exc.status_code == 401 and refresh_token:
                    refreshed, should_clear_session = self._refresh_event_token(
                        base_url=base_url,
                        access_token=access_token,
                        refresh_token=refresh_token,
                        workspace_id=identity["workspace_id"],
                        user_id=identity["user_id"],
                    )
                    if refreshed:
                        reconnect_failures = 0
                        continue
                    if should_clear_session:
                        self._session_store.clear_if_current(
                            base_url=base_url,
                            access_token=access_token,
                            refresh_token=refresh_token,
                            workspace_id=identity["workspace_id"],
                            user_id=identity["user_id"],
                        )
                        self._update_status(logged_in=False)
                        reconnect_failures = 0
                    else:
                        reconnect_failures += 1
                else:
                    reconnect_failures += 1
                    self._record_error(str(exc))
            except Exception as exc:
                self._update_status(event_stream_connected=False)
                reconnect_failures += 1
                self._record_error(f"云端事件监听失败：{exc}")

            if self._stop_event.is_set():
                break
            delay = self._event_reconnect_delay(reconnect_failures)
            self._update_status(
                event_reconnect_attempts=max(0, reconnect_failures),
                next_event_reconnect_seconds=round(delay, 2),
            )
            self._stop_event.wait(delay)

    def _refresh_event_token(
        self,
        *,
        base_url: str,
        access_token: str,
        refresh_token: str,
        workspace_id: str = "",
        user_id: str = "",
    ) -> tuple[bool, bool]:
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
            return bool(refreshed_session.get("access_token")), False
        except CloudSessionChangedError as exc:
            self._record_error(str(exc))
            return False, False
        except CloudClientError as exc:
            if exc.status_code != 401:
                self._record_error(str(exc))
            return False, exc.status_code == 401

    def _pull_now_from_event(self, event_name: str) -> None:
        pull_result = self._invoke_pull_tasks(force=False)
        self._last_pull_started_at = time.monotonic()
        if pull_result.get("ok"):
            pull_metrics = _pull_metrics(pull_result)
            self._update_status(
                last_pull_at=local_now().isoformat(timespec="seconds"),
                last_pull_metrics=pull_metrics,
                last_pull_summary=_pull_summary(pull_metrics),
            )
            self._clear_error()
        else:
            pull_metrics = _pull_metrics(pull_result)
            self._update_status(last_pull_metrics=pull_metrics, last_pull_summary=_pull_summary(pull_metrics))
            self._record_error(str(pull_result.get("message") or f"云端事件同步失败：{event_name}"))

    def _invoke_pull_tasks(self, *, force: bool = False) -> dict[str, Any]:
        if force and _callable_accepts_keyword(self._pull_tasks, "force"):
            return self._pull_tasks(force=True)
        return self._pull_tasks()


def _callable_accepts_keyword(callback: Callable[..., Any], keyword: str) -> bool:
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD or name == keyword
        for name, parameter in signature.parameters.items()
    )


def _normalize_auto_sync_error(message: Any) -> str:
    text = str(message or "云端自动同步失败").strip() or "云端自动同步失败"
    lower_text = text.lower()
    if "read timed out" in lower_text or "read timeout" in lower_text:
        if "事件" in text:
            return "云端事件连接超时，正在自动重连"
        return "云端请求超时，正在自动重试"
    text = text.replace("连接连接失败", "连接失败")
    text = text.replace("连接连接超时", "连接超时")
    return text


def _is_transient_auto_sync_error(message: Any) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    return (
        "自动重试" in text
        or "自动重连" in text
        or "read timed out" in text
        or "timeout" in text
        or "connection" in text
    )


def _pull_metrics(result: dict[str, Any]) -> dict[str, Any]:
    summary = result.get("summary") if isinstance(result, dict) and isinstance(result.get("summary"), dict) else {}
    metrics = summary.get("metrics") if isinstance(summary.get("metrics"), dict) else {}
    run_records = summary.get("run_records") if isinstance(summary.get("run_records"), dict) else {}
    task_day_status = summary.get("task_day_status_events") if isinstance(summary.get("task_day_status_events"), dict) else {}
    articles = summary.get("articles") if isinstance(summary.get("articles"), dict) else {}
    payload = dict(metrics)
    payload.update({
        "run_record_mode": str(run_records.get("mode") or ""),
        "run_record_requests": int(run_records.get("request_count") or 0),
        "run_record_tasks": int(run_records.get("tasks") or 0),
        "run_record_fetched": int(run_records.get("fetched") or 0),
        "run_record_imported": int(run_records.get("imported") or 0),
        "task_day_status_tasks": int(task_day_status.get("tasks") or 0),
        "task_day_status_fetched": int(task_day_status.get("fetched") or 0),
        "task_day_status_applied": int(task_day_status.get("applied") or 0),
        "task_day_status_cursor_updates": int(task_day_status.get("cursor_updates") or 0),
        "article_mode": str(articles.get("mode") or ""),
        "article_fetched": int(articles.get("fetched") or 0),
        "article_imported": int(articles.get("imported") or 0),
        "article_cursor_updates": int(articles.get("cursor_updates") or 0),
    })
    return payload


def _pull_summary(metrics: dict[str, Any]) -> dict[str, Any]:
    mode = str(metrics.get("mode") or "").strip()
    if mode == "unknown":
        mode = ""
    changed_task_ids = metrics.get("changed_task_ids")
    changed_task_count = len(changed_task_ids) if isinstance(changed_task_ids, list) else 0
    received_tasks = _safe_int(metrics.get("received_tasks"))
    pulled_task_count = received_tasks if received_tasks > 0 else changed_task_count
    return {
        "mode": mode,
        "duration_ms": _safe_int(metrics.get("total_ms") or metrics.get("duration_ms")),
        "task_pull_ms": _safe_int(metrics.get("task_pull_ms")),
        "pulled_task_count": pulled_task_count,
        "changed_task_count": changed_task_count,
        "run_record_imported": _safe_int(metrics.get("run_record_imported")),
        "run_record_fetched": _safe_int(metrics.get("run_record_fetched")),
        "task_day_status_applied": _safe_int(metrics.get("task_day_status_applied")),
        "request_count": (
            _safe_int(metrics.get("run_record_requests"))
            + (1 if _safe_int(metrics.get("received_tasks")) or changed_task_count else 0)
        ),
    }


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0
