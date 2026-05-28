"""Small facade for local cloud sync runtime wiring.

This module is intentionally thin for now. It centralizes the manager/auto-sync
construction so the next step can move cloud sync into a daemon without further
spreading web_backend.py dependencies.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core.cloud_article_sync import (
    merge_cloud_article_entity_into_store,
    merge_cloud_article_reference_entity_into_store,
)
from core.cloud_agent_status_store import CloudAgentStatusStore
from core.cloud_client import CloudClientError, SurfacedCloudClient
from core.cloud_content_state_store import CloudContentStateStore
from core.cloud_object_cache import CloudObjectCache, CloudObjectCacheError, normalize_object_ref
from core.cloud_object_transfer import upload_cloud_object_file
from core.cloud_object_transfer_store import CloudObjectTransferStore
from core.cloud_sync_daemon import InProcessCloudSyncCommandClient
from core.cloud_event_types import EVENT_PROFILE_UPDATE
from core.cloud_outbox import CloudOutbox
from core.cloud_run_sync import (
    enqueue_cloud_articles,
    enqueue_recent_cloud_run_records_from_history,
    flush_cloud_outbox,
)
from core.cloud_state_delta import CloudStateDeltaStore, pull_cloud_state_delta
from core.cloud_state_delta_inbox import CloudStateDeltaInbox, process_state_delta_inbox
from core.cloud_task_sync import (
    merge_cloud_run_record_entity_into_config,
    merge_cloud_task_day_status_entity_into_config,
    merge_cloud_tasks_into_config,
)
from core.cloud_session_store import (
    CloudSessionChangedError,
    CloudSessionStore,
    cloud_session_identity,
    cloud_session_identity_key,
    normalize_cloud_base_url,
)
from core.cloud_platform_auto_sync import CloudPlatformAutoSync
from core.cloud_sync import CloudSyncManager
from core.local_account_space import account_profile_dir_from_session, current_account_config_path, ensure_account_space
from core.sync_service import build_sync_bundle


@dataclass
class LocalCloudSyncRuntime:
    manager: CloudSyncManager
    platform_auto_sync: CloudPlatformAutoSync

    def start(self) -> None:
        self.manager.start()
        self.platform_auto_sync.start()

    def stop(self) -> None:
        self.manager.stop()
        self.platform_auto_sync.stop()

    def sync_status(self) -> dict[str, Any]:
        return self.manager.get_status()

    def auto_sync_status(self) -> dict[str, Any]:
        return self.platform_auto_sync.get_status()


def create_in_process_cloud_sync_command_client(
    command_handler: Callable[[str, dict[str, Any] | None], dict[str, Any]],
) -> InProcessCloudSyncCommandClient:
    """Return the transport-shaped client used before daemon IPC takes over."""
    return InProcessCloudSyncCommandClient(command_handler)


def create_local_cloud_sync_runtime(
    *,
    config_getter: Callable[[], dict[str, Any]],
    bundle_applier: Callable[..., Any],
    config_updater: Callable[[dict[str, Any]], Any],
    pull_tasks: Callable[..., dict[str, Any]],
    recover_upload_candidates: Callable[[], dict[str, Any]],
    logger: Callable[[str], None],
    pull_state_delta: Callable[..., dict[str, Any]] | None = None,
    process_state_delta_inbox: Callable[..., dict[str, Any]] | None = None,
    retry_object_downloads: Callable[..., dict[str, Any]] | None = None,
    retry_object_uploads: Callable[..., dict[str, Any]] | None = None,
) -> LocalCloudSyncRuntime:
    manager = CloudSyncManager(
        config_getter=config_getter,
        bundle_builder=lambda include_secrets=False: build_sync_bundle(
            config_getter(),
            include_secrets=include_secrets,
        ),
        bundle_applier=bundle_applier,
        config_updater=config_updater,
        logger=logger,
    )
    platform_auto_sync = CloudPlatformAutoSync(
        pull_tasks=pull_tasks,
        recover_upload_candidates=recover_upload_candidates,
        pull_state_delta=pull_state_delta,
        process_state_delta_inbox=process_state_delta_inbox,
        retry_object_downloads=retry_object_downloads,
        retry_object_uploads=retry_object_uploads,
        upload_burst_interval_seconds=_env_float("AIBRANDMONITOR_CLOUD_UPLOAD_BURST_INTERVAL_SECONDS", 1.0),
        upload_burst_pending_threshold=_env_int("AIBRANDMONITOR_CLOUD_UPLOAD_BURST_PENDING_THRESHOLD", 100),
        logger=logger,
    )
    return LocalCloudSyncRuntime(manager=manager, platform_auto_sync=platform_auto_sync)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except Exception:
        return int(default)


class AppCloudRuntimeSupport:
    """In-process facade for cloud runtime orchestration inside AppRuntime.

    Phase 3a keeps cloud sync in-process, but moves thread/session/request
    orchestration out of web_backend.py so later daemon extraction has one
    boundary to replace.
    """

    def __init__(
        self,
        *,
        owner: Any,
        session_store_factory: Callable[[], Any] = CloudSessionStore,
        outbox_factory: Callable[[], Any] = CloudOutbox,
        article_enqueue_fn: Callable[..., dict[str, Any] | None] | None = None,
        flush_outbox_fn: Callable[..., dict[str, Any]] | None = None,
        thread_factory: Callable[..., Any] = threading.Thread,
        sleep_fn: Callable[[float], None] = time.sleep,
        status_client_factory: Callable[[str], Any] | None = None,
        request_client_factory: Callable[[str], Any] | None = None,
        object_cache_factory: Callable[[], Any] | None = None,
        object_transfer_store_factory: Callable[[], Any] | None = None,
        auto_sync_status_getter: Callable[[], dict[str, Any]] | None = None,
        current_account_config_path_getter: Callable[[], Any] = current_account_config_path,
        account_profile_dir_getter: Callable[[dict[str, Any] | None], Any] = account_profile_dir_from_session,
        account_space_ensurer: Callable[..., Any] = ensure_account_space,
        has_pending_profile_update: Callable[[dict[str, Any]], bool] | None = None,
        state_delta_appliers: dict[str, Callable[[dict[str, Any]], Any]] | None = None,
        article_snapshot_startup_delay_seconds: float = 5.0,
        article_deferred_retry_seconds: float = 2.0,
    ) -> None:
        self._owner = owner
        self._session_store_factory = session_store_factory
        self._outbox_factory = outbox_factory
        self._article_enqueue_fn = article_enqueue_fn or enqueue_cloud_articles
        self._flush_outbox_fn = flush_outbox_fn or flush_cloud_outbox
        self._thread_factory = thread_factory
        self._sleep_fn = sleep_fn
        self._status_client_factory = status_client_factory or self._default_status_client_factory
        self._request_client_factory = request_client_factory or SurfacedCloudClient
        self._object_cache_factory = object_cache_factory or CloudObjectCache
        self._object_transfer_store_factory = object_transfer_store_factory or CloudObjectTransferStore
        self._auto_sync_status_getter = auto_sync_status_getter or (lambda: {})
        self._current_account_config_path_getter = current_account_config_path_getter
        self._account_profile_dir_getter = account_profile_dir_getter
        self._account_space_ensurer = account_space_ensurer
        self._has_pending_profile_update = has_pending_profile_update or self._default_has_pending_profile_update
        self._state_delta_appliers = self._build_state_delta_appliers(state_delta_appliers)
        self._article_snapshot_startup_delay_seconds = float(article_snapshot_startup_delay_seconds or 0.0)
        self._article_deferred_retry_seconds = float(article_deferred_retry_seconds or 0.0)
        self._stop_event = threading.Event()
        self._article_cloud_enqueue_lock = threading.RLock()
        self._last_article_cloud_enqueue_key: tuple[Any, ...] | None = None
        self._article_cloud_enqueue_requested = False
        self._article_cloud_enqueue_thread: Any = None
        self._article_cloud_enqueue_retry_thread: Any = None
        self._cloud_status_validation_lock = threading.RLock()
        self._cloud_status_validated_identity = ""
        self._cloud_status_validated_at = 0.0
        self._cloud_status_validation_error = ""

    @property
    def last_article_cloud_enqueue_key(self) -> tuple[Any, ...] | None:
        return self._last_article_cloud_enqueue_key

    @property
    def article_snapshot_requested(self) -> bool:
        with self._article_cloud_enqueue_lock:
            return self._article_cloud_enqueue_requested

    @property
    def validation_error(self) -> str:
        with self._cloud_status_validation_lock:
            return self._cloud_status_validation_error

    def reset_transient_state(self) -> None:
        self._reset_article_snapshot_state()
        self._reset_validation_state()

    def stop(self) -> None:
        self._stop_event.set()
        with self._article_cloud_enqueue_lock:
            self._article_cloud_enqueue_requested = False
            worker_thread = self._article_cloud_enqueue_thread
            retry_thread = self._article_cloud_enqueue_retry_thread
        self._join_thread_if_possible(worker_thread)
        self._join_thread_if_possible(retry_thread)
        with self._article_cloud_enqueue_lock:
            self._article_cloud_enqueue_thread = None
            self._article_cloud_enqueue_retry_thread = None

    def schedule_article_snapshot(self) -> None:
        if self._stop_event.is_set():
            return
        with self._article_cloud_enqueue_lock:
            self._article_cloud_enqueue_requested = True
            if self._article_cloud_enqueue_thread and self._article_cloud_enqueue_thread.is_alive():
                return
            self._article_cloud_enqueue_thread = self._thread_factory(
                target=self.run_article_snapshot_worker,
                name="cloud-article-snapshot-enqueue",
                daemon=True,
            )
            self._article_cloud_enqueue_thread.start()

    def run_article_snapshot_worker(self) -> None:
        startup_delay_seconds = self._read_article_snapshot_startup_delay_seconds()
        if startup_delay_seconds > 0:
            self._sleep_fn(startup_delay_seconds)
        if self._stop_event.is_set():
            with self._article_cloud_enqueue_lock:
                if self._article_cloud_enqueue_thread is threading.current_thread():
                    self._article_cloud_enqueue_thread = None
            return
        while True:
            if self._stop_event.is_set():
                with self._article_cloud_enqueue_lock:
                    if self._article_cloud_enqueue_thread is threading.current_thread():
                        self._article_cloud_enqueue_thread = None
                return
            with self._article_cloud_enqueue_lock:
                if not self._article_cloud_enqueue_requested:
                    self._article_cloud_enqueue_thread = None
                    return
                self._article_cloud_enqueue_requested = False
            self.enqueue_article_snapshot()
            with self._article_cloud_enqueue_lock:
                if not self._article_cloud_enqueue_requested:
                    self._article_cloud_enqueue_thread = None
                    return

    def enqueue_article_snapshot(self, config: dict[str, Any] | None = None) -> None:
        try:
            session = self._session_store_factory().load()
            user = session.get("user") if isinstance(session.get("user"), dict) else {}
            role = str(user.get("role") or "").strip()
            if role == "viewer" or not str(session.get("access_token") or "").strip():
                return

            resolved_config = config if isinstance(config, dict) else self._load_runtime_config()
            articles, deferred_refresh = self._owner._get_cloud_article_upload_snapshot_with_refresh_state(
                resolved_config,
                session=session,
            )
            if deferred_refresh:
                self.schedule_article_snapshot_retry()
                return
            snapshot_key = (
                cloud_session_identity_key(session),
                self._owner._article_store_version_key(),
                self._owner._article_cloud_task_map_key(resolved_config),
            )
            with self._article_cloud_enqueue_lock:
                if self._last_article_cloud_enqueue_key == snapshot_key:
                    return

            result = self._article_enqueue_fn(articles, resolved_config) or {}
            with self._article_cloud_enqueue_lock:
                self._last_article_cloud_enqueue_key = snapshot_key
            queued = int(result.get("queued") or 0)
            if queued > 0:
                print(f"[WebBackend] 文章云端同步已入队: articles={result.get('articles', 0)}, queued={queued}")
        except Exception as exc:
            print(f"[WebBackend] 文章云端同步入队失败，将等待下次本地变更重试: {exc}")

    def schedule_article_snapshot_retry(self, *, delay_seconds: float | None = None) -> None:
        if self._stop_event.is_set():
            return
        delay = self._article_deferred_retry_seconds if delay_seconds is None else delay_seconds
        try:
            delay = max(0.0, float(delay))
        except Exception:
            delay = self._article_deferred_retry_seconds
        with self._article_cloud_enqueue_lock:
            retry_thread = self._article_cloud_enqueue_retry_thread
            if retry_thread and retry_thread.is_alive():
                return
            retry_thread = self._thread_factory(
                target=self.run_article_snapshot_retry,
                args=(delay,),
                name="cloud-article-snapshot-retry",
                daemon=True,
            )
            self._article_cloud_enqueue_retry_thread = retry_thread
            retry_thread.start()

    def run_article_snapshot_retry(self, delay_seconds: float) -> None:
        try:
            if delay_seconds > 0:
                self._sleep_fn(delay_seconds)
        finally:
            with self._article_cloud_enqueue_lock:
                if self._article_cloud_enqueue_retry_thread is threading.current_thread():
                    self._article_cloud_enqueue_retry_thread = None
        if self._stop_event.is_set():
            return
        self.schedule_article_snapshot()

    def recover_cloud_run_history_uploads(self) -> dict[str, Any]:
        """Recover local run/article sync candidates into the current account outbox."""
        try:
            config = self._load_runtime_config_locked()
            session = self._session_store_factory().load()
            session_outbox = self._outbox_factory().bind_to_session(session)
            run_metrics = enqueue_recent_cloud_run_records_from_history(config, outbox=session_outbox, days=7)
            articles, deferred_refresh = self._owner._get_cloud_article_upload_snapshot_with_refresh_state(
                config,
                session=session,
            )
            if deferred_refresh:
                self.schedule_article_snapshot_retry()
                article_metrics = {
                    "deferred": True,
                    "reason": "match_refresh_deferred",
                    "articles": len(articles),
                    "queued": 0,
                }
            else:
                article_metrics = self._article_enqueue_fn(
                    articles,
                    config,
                    outbox=session_outbox,
                )
            return {
                "ok": True,
                **run_metrics,
                "run_records": run_metrics,
                "articles_sync": article_metrics,
            }
        except Exception as exc:
            return {"ok": False, "message": f"本地运行历史恢复失败：{exc}"}

    def logout_cloud_account(self) -> dict[str, Any]:
        store = self._session_store_factory()
        session = store.load()
        base_url = str(session.get("base_url") or "").strip()
        refresh_token = str(session.get("refresh_token") or "").strip()
        self._flush_bound_outbox_if_needed(
            session,
            failure_message="[WebBackend] 退出前运行数据补传失败，将继续退出: {exc}",
        )
        if base_url and refresh_token:
            try:
                self._request_client_factory(base_url).logout(refresh_token)
            except CloudClientError:
                pass
        store.clear()
        self.reset_transient_state()
        activate_account_space = getattr(self._owner, "_activate_current_account_space", None)
        if callable(activate_account_space):
            activate_account_space(copy_legacy=False)
        return {"ok": True, "message": "已退出云端", "cloud": self.current_cloud_status().get("cloud")}

    def flush_cloud_outbox(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        limit = _safe_int(request_payload.get("limit", 100), 100)
        return self._flush_outbox_fn(limit=limit)

    def cloud_outbox_diagnostics(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        failed_limit = _safe_int(request_payload.get("failed_limit", request_payload.get("failedLimit", 10)), 10)
        session = self._session_store_factory().load()
        outbox = self._outbox_factory().bind_to_session(session)
        diagnostics = outbox.diagnostics(failed_limit=failed_limit)
        return {"ok": True, "outbox": diagnostics}

    def cloud_sync_health(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        failed_limit = _safe_int(request_payload.get("failed_limit", request_payload.get("failedLimit", 10)), 10)
        session = self._session_store_factory().load()
        outbox = self._outbox_factory().bind_to_session(session).diagnostics(failed_limit=failed_limit)
        state_delta = CloudStateDeltaStore().diagnostics(session)
        inbox = CloudStateDeltaInbox().diagnostics(failed_limit=failed_limit)
        agent_status = CloudAgentStatusStore().diagnostics(session)
        content_state = CloudContentStateStore().diagnostics(session)
        object_cache = self._object_cache_factory().diagnostics()
        object_transfers = self._object_transfer_store_factory().diagnostics(failed_limit=failed_limit)
        auto_sync = self._safe_auto_sync_status()
        return {
            "ok": True,
            "sync_health": {
                "summary": _cloud_sync_health_summary(
                    session=session,
                    auto_sync=auto_sync,
                    outbox=outbox,
                    inbox=inbox,
                    agent_status=agent_status,
                    content_state=content_state,
                    object_cache=object_cache,
                    object_transfers=object_transfers,
                ),
                "auto_sync": auto_sync,
                "outbox": outbox,
                "state_delta": state_delta,
                "inbox": inbox,
                "agent_status": agent_status,
                "content_state": content_state,
                "object_cache": object_cache,
                "object_transfers": object_transfers,
            },
        }

    def handle_command(self, command: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Handle a cloud-sync runtime command through the future daemon boundary."""
        normalized = str(command or "").strip()
        request_payload = payload if isinstance(payload, dict) else {}
        if normalized in {"cloud.status", "status"}:
            return self.get_cloud_status()
        if normalized in {"cloud.current_status", "current_status"}:
            return self.current_cloud_status()
        if normalized in {"cloud.status_from_session", "status_from_session"}:
            return self.cloud_status_from_session(request_payload.get("session"))
        if normalized in {"cloud.list_admin_tasks_with_local_sync", "list_admin_tasks_with_local_sync"}:
            return self._run_cloud_task_sync_request(request_payload)
        if normalized in {"cloud.sync_admin_task", "sync_admin_task"}:
            return self._run_sync_admin_task_request(request_payload)
        if normalized in {"cloud.list_admin_users", "list_admin_users"}:
            return self._run_cloud_api_request("list_admin_users", request_payload)
        if normalized in {"cloud.list_admin_article_classification_jobs", "list_admin_article_classification_jobs"}:
            return self._run_cloud_api_request("list_admin_article_classification_jobs", request_payload)
        if normalized in {"cloud.resolve_admin_article_classification_job", "resolve_admin_article_classification_job"}:
            return self._run_cloud_api_request("resolve_admin_article_classification_job", request_payload)
        if normalized in {"cloud.ignore_admin_article_classification_job", "ignore_admin_article_classification_job"}:
            return self._run_cloud_api_request("ignore_admin_article_classification_job", request_payload)
        if normalized in {"cloud.create_admin_user", "create_admin_user"}:
            return self._run_cloud_api_request("create_admin_user", request_payload)
        if normalized in {"cloud.update_admin_user", "update_admin_user"}:
            return self._run_cloud_api_request("update_admin_user", request_payload)
        if normalized in {"cloud.delete_admin_user", "delete_admin_user"}:
            return self._run_cloud_api_request("delete_admin_user", request_payload)
        if normalized in {"cloud.update_admin_task", "update_admin_task"}:
            return self._run_cloud_api_request("update_admin_task", request_payload)
        if normalized in {"cloud.delete_admin_task", "delete_admin_task"}:
            return self._run_cloud_api_request("delete_admin_task", request_payload)
        if normalized in {"cloud.restore_admin_task", "restore_admin_task"}:
            return self._run_cloud_api_request("restore_admin_task", request_payload)
        if normalized in {"cloud.flush_outbox", "flush_outbox"}:
            return self.flush_cloud_outbox(request_payload)
        if normalized in {"cloud.outbox_diagnostics", "outbox_diagnostics"}:
            return self.cloud_outbox_diagnostics(request_payload)
        if normalized in {"cloud.sync_health", "sync_health"}:
            return self.cloud_sync_health(request_payload)
        if normalized in {"cloud.object_cache_diagnostics", "object_cache_diagnostics"}:
            return {"ok": True, "object_cache": self._object_cache_factory().diagnostics()}
        if normalized in {"cloud.object_transfer_diagnostics", "object_transfer_diagnostics"}:
            failed_limit = _safe_int(request_payload.get("failed_limit") or request_payload.get("failedLimit"), 10)
            return {"ok": True, "object_transfers": self._object_transfer_store_factory().diagnostics(failed_limit=failed_limit)}
        if normalized in {"cloud.object_transfer_retry_candidates", "object_transfer_retry_candidates"}:
            return self.object_transfer_retry_candidates(request_payload)
        if normalized in {"cloud.retry_object_downloads", "retry_object_downloads"}:
            return self.retry_object_downloads(request_payload)
        if normalized in {"cloud.retry_object_uploads", "retry_object_uploads"}:
            return self.retry_object_uploads(request_payload)
        if normalized in {"cloud.prune_object_cache", "prune_object_cache"}:
            return self.prune_cloud_object_cache(request_payload)
        if normalized in {"cloud.cache_object", "cache_object"}:
            return self.cache_cloud_object(request_payload)
        if normalized in {"cloud.pull_state_delta", "pull_state_delta"}:
            return self.pull_cloud_state_delta(request_payload)
        if normalized in {"cloud.state_delta_diagnostics", "state_delta_diagnostics"}:
            return self.cloud_state_delta_diagnostics(request_payload)
        if normalized in {"cloud.process_state_delta_inbox", "process_state_delta_inbox"}:
            return self.process_cloud_state_delta_inbox(request_payload)
        if normalized in {"cloud.logout", "logout"}:
            return self.logout_cloud_account()
        if normalized in {"cloud.recover_uploads", "recover_uploads"}:
            return self.recover_cloud_run_history_uploads()
        if normalized in {"cloud.schedule_article_snapshot", "schedule_article_snapshot"}:
            self.schedule_article_snapshot()
            return {"ok": True, "message": "文章云端同步已调度"}
        if normalized in {"cloud.run_article_snapshot_worker", "run_article_snapshot_worker"}:
            self.run_article_snapshot_worker()
            return {"ok": True, "message": "文章云端同步 worker 已退出"}
        if normalized in {"cloud.enqueue_article_snapshot", "enqueue_article_snapshot"}:
            self.enqueue_article_snapshot(request_payload.get("config"))
            return {"ok": True, "message": "文章云端同步快照已触发"}
        if normalized in {"cloud.schedule_article_snapshot_retry", "schedule_article_snapshot_retry"}:
            self.schedule_article_snapshot_retry(delay_seconds=request_payload.get("delay_seconds"))
            return {"ok": True, "message": "文章云端同步重试已调度"}
        if normalized in {"cloud.run_article_snapshot_retry", "run_article_snapshot_retry"}:
            self.run_article_snapshot_retry(_safe_float(request_payload.get("delay_seconds"), 0.0))
            return {"ok": True, "message": "文章云端同步重试 worker 已退出"}
        if normalized in {"cloud.validate_session", "validate_session"}:
            self.validate_cloud_session_if_needed(force=bool(request_payload.get("force")))
            return self.current_cloud_status()
        if normalized in {"cloud.login_account_space", "login_account_space"}:
            saved_session = self.login_cloud_account_space(
                base_url=str(request_payload.get("base_url") or request_payload.get("baseUrl") or ""),
                token_pair=request_payload.get("token_pair") or request_payload.get("tokenPair") or {},
            )
            return {"ok": True, "message": "云端账号空间已切换", "session": saved_session}
        return {"ok": False, "message": f"未知云同步命令: {normalized}"}

    def daemon_handle_command(self, command: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        normalized = str(command or "").strip()
        request_payload = payload if isinstance(payload, dict) else {}
        if normalized in {"cloud.status", "status"}:
            return self._daemon_get_cloud_status()
        if normalized in {"cloud.current_status", "current_status"}:
            return self._daemon_current_cloud_status()
        if normalized in {"cloud.status_from_session", "status_from_session"}:
            return self._daemon_cloud_status_from_session(request_payload.get("session"))
        if normalized in {"cloud.validate_session", "validate_session"}:
            self.validate_cloud_session_if_needed(force=bool(request_payload.get("force")))
            return self._daemon_current_cloud_status()
        if normalized in {"cloud.outbox_diagnostics", "outbox_diagnostics"}:
            return self.cloud_outbox_diagnostics(request_payload)
        if normalized in {"cloud.sync_health", "sync_health"}:
            return self.cloud_sync_health(request_payload)
        if normalized in {"cloud.object_cache_diagnostics", "object_cache_diagnostics"}:
            return {"ok": True, "object_cache": self._object_cache_factory().diagnostics()}
        if normalized in {"cloud.object_transfer_diagnostics", "object_transfer_diagnostics"}:
            failed_limit = _safe_int(request_payload.get("failed_limit") or request_payload.get("failedLimit"), 10)
            return {"ok": True, "object_transfers": self._object_transfer_store_factory().diagnostics(failed_limit=failed_limit)}
        if normalized in {"cloud.object_transfer_retry_candidates", "object_transfer_retry_candidates"}:
            return self.object_transfer_retry_candidates(request_payload)
        if normalized in {"cloud.retry_object_downloads", "retry_object_downloads"}:
            return self.retry_object_downloads(request_payload)
        if normalized in {"cloud.retry_object_uploads", "retry_object_uploads"}:
            return self.retry_object_uploads(request_payload)
        if normalized in {"cloud.prune_object_cache", "prune_object_cache"}:
            return self.prune_cloud_object_cache(request_payload)
        if normalized in {"cloud.cache_object", "cache_object"}:
            return self.cache_cloud_object(request_payload)
        if normalized in {"cloud.pull_state_delta", "pull_state_delta"}:
            return self.pull_cloud_state_delta(request_payload)
        if normalized in {"cloud.state_delta_diagnostics", "state_delta_diagnostics"}:
            return self.cloud_state_delta_diagnostics(request_payload)
        if normalized in {"cloud.process_state_delta_inbox", "process_state_delta_inbox"}:
            return self.process_cloud_state_delta_inbox(request_payload)
        delegate = getattr(self, "handle_command_for_main", None)
        if callable(delegate):
            return delegate(normalized, request_payload)
        return {"ok": False, "message": f"未知云同步命令: {normalized}"}

    def _daemon_get_cloud_status(self) -> dict[str, Any]:
        self.validate_cloud_session_if_needed()
        return self._daemon_current_cloud_status()

    def _daemon_current_cloud_status(self) -> dict[str, Any]:
        session = self._session_store_factory().load()
        return self._daemon_cloud_status_from_session(session)

    def _daemon_cloud_status_from_session(self, session: dict[str, Any] | None) -> dict[str, Any]:
        result = self.cloud_status_from_session(session)
        if isinstance(result, dict) and isinstance(result.get("cloud"), dict):
            cloud = dict(result["cloud"])
            cloud["autoSync"] = self._safe_auto_sync_status()
            result = dict(result)
            result["cloud"] = cloud
        return result

    def _safe_auto_sync_status(self) -> dict[str, Any]:
        try:
            status = self._auto_sync_status_getter()
        except Exception as exc:
            return {"last_error": f"autoSync status unavailable: {exc}"}
        return dict(status) if isinstance(status, dict) else {}

    def login_cloud_account_space(
        self,
        *,
        base_url: str,
        token_pair: dict[str, Any],
    ) -> dict[str, Any]:
        preview_session = self._build_login_session_preview(base_url=base_url, token_pair=token_pair)
        store = self._session_store_factory()
        previous_session = store.load()
        previous_key = cloud_session_identity_key(previous_session)
        next_key = cloud_session_identity_key(preview_session)
        if previous_key and next_key and previous_key != next_key:
            self._flush_bound_outbox_if_needed(
                previous_session,
                failure_message="[WebBackend] 切换账号前旧账号运行数据补传失败，将继续登录新账号: {exc}",
            )

        profile_dir = self._account_profile_dir_getter(preview_session)
        marker_exists = bool(profile_dir and (profile_dir / "profile_meta.json").exists())
        copy_legacy = self._cloud_role(preview_session) == "admin" and not marker_exists
        self._account_space_ensurer(preview_session, copy_legacy=copy_legacy)

        saved_session = store.save_login(base_url=base_url, token_pair=token_pair)
        self.reset_transient_state()
        activate_account_space = getattr(self._owner, "_activate_current_account_space", None)
        if callable(activate_account_space):
            activate_account_space(copy_legacy=False)
        isolate_account_config = getattr(self._owner, "_isolate_ordinary_cloud_account_config", None)
        if callable(isolate_account_config):
            isolate_account_config(saved_session)
        close_viewer_runtime = getattr(self._owner, "_close_execution_runtime_for_viewer", None)
        if callable(close_viewer_runtime):
            close_viewer_runtime()
        return saved_session

    def validate_cloud_session_if_needed(self, *, force: bool = False) -> None:
        store = self._session_store_factory()
        session = store.load()
        identity_key = cloud_session_identity_key(session)
        base_url = str(session.get("base_url") or "").strip()
        access_token = str(session.get("access_token") or "").strip()
        refresh_token = str(session.get("refresh_token") or "").strip()
        if not base_url or not access_token or not refresh_token:
            return
        if self._has_pending_profile_update(session):
            return

        now_ts = time.monotonic()
        with self._cloud_status_validation_lock:
            if (
                not force
                and identity_key
                and identity_key == self._cloud_status_validated_identity
                and now_ts - self._cloud_status_validated_at < 60.0
            ):
                return
            self._cloud_status_validated_identity = identity_key
            self._cloud_status_validated_at = now_ts

        identity = cloud_session_identity(session)
        client = self._status_client_factory(base_url)
        try:
            me_payload = client.me(access_token)
            if isinstance(me_payload, dict) and cloud_session_identity_key(store.load()) == identity_key:
                store.update_user(me_payload)
            with self._cloud_status_validation_lock:
                self._cloud_status_validation_error = ""
            return
        except CloudClientError as exc:
            if exc.status_code != 401:
                with self._cloud_status_validation_lock:
                    self._cloud_status_validation_error = str(exc)
                return

        try:
            refreshed_session = store.refresh_login_if_current(
                base_url=base_url,
                access_token=access_token,
                refresh_token=refresh_token,
                refresh=client.refresh,
                workspace_id=identity["workspace_id"],
                user_id=identity["user_id"],
            )
            refreshed_access_token = str(refreshed_session.get("access_token") or "").strip()
            if refreshed_access_token:
                try:
                    me_payload = client.me(refreshed_access_token)
                    if (
                        isinstance(me_payload, dict)
                        and cloud_session_identity_key(store.load()) == cloud_session_identity_key(refreshed_session)
                    ):
                        store.update_user(me_payload)
                except CloudClientError as verify_exc:
                    with self._cloud_status_validation_lock:
                        self._cloud_status_validation_error = str(verify_exc)
                    return
            with self._cloud_status_validation_lock:
                self._cloud_status_validation_error = ""
        except CloudSessionChangedError as exc:
            with self._cloud_status_validation_lock:
                self._cloud_status_validation_error = str(exc)
        except CloudClientError as refresh_exc:
            with self._cloud_status_validation_lock:
                self._cloud_status_validation_error = str(refresh_exc)

    def get_cloud_status(self) -> dict[str, Any]:
        self.validate_cloud_session_if_needed()
        return self.current_cloud_status()

    def current_cloud_status(self) -> dict[str, Any]:
        session = self._session_store_factory().load()
        return self.cloud_status_from_session(session)

    def cloud_status_from_session(self, session: dict[str, Any] | None) -> dict[str, Any]:
        session_payload = session if isinstance(session, dict) else {}
        user = session_payload.get("user") if isinstance(session_payload.get("user"), dict) else {}
        with self._cloud_status_validation_lock:
            validation_error = self._cloud_status_validation_error
        return {
            "ok": True,
            "cloud": {
                "loggedIn": bool(
                    session_payload.get("base_url")
                    and session_payload.get("access_token")
                    and session_payload.get("refresh_token")
                ),
                "baseUrl": str(session_payload.get("base_url") or ""),
                "user": {
                    "id": user.get("id"),
                    "workspace_id": user.get("workspace_id"),
                    "username": user.get("username"),
                    "role": user.get("role"),
                    "display_name": user.get("display_name"),
                    "email": user.get("email"),
                    "avatar": user.get("avatar"),
                    "birthday": user.get("birthday"),
                    "hire_date": user.get("hire_date"),
                    "enabled": user.get("enabled"),
                    "token_version": user.get("token_version"),
                    "created_at": user.get("created_at"),
                    "deleted_at": user.get("deleted_at"),
                },
                "savedAt": str(session_payload.get("saved_at") or ""),
                "localProfile": {
                    "configPath": str(self._current_account_config_path_getter()),
                },
                "outbox": self._outbox_factory().stats(include_retry=True),
                "autoSync": self._safe_auto_sync_status(),
                "validationError": validation_error,
            },
        }

    def pull_cloud_state_delta(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        result = pull_cloud_state_delta(
            limit=_safe_int(request_payload.get("limit"), 500),
            max_pages=_safe_int(request_payload.get("max_pages") or request_payload.get("maxPages"), 5),
        )
        return {"ok": bool(result.get("ok")), "state_delta": result, "message": str(result.get("message") or "")}

    def cloud_state_delta_diagnostics(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        session = self._session_store_factory().load()
        diagnostics = CloudStateDeltaStore().diagnostics(session)
        inbox_diagnostics = CloudStateDeltaInbox().diagnostics(
            failed_limit=_safe_int(request_payload.get("failed_limit") or request_payload.get("failedLimit"), 10)
        )
        agent_status_diagnostics = CloudAgentStatusStore().diagnostics(session)
        content_state_diagnostics = CloudContentStateStore().diagnostics(session)
        return {
            "ok": True,
            "state_delta": diagnostics,
            "inbox": inbox_diagnostics,
            "agent_status": agent_status_diagnostics,
            "content_state": content_state_diagnostics,
        }

    def process_cloud_state_delta_inbox(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        streams = request_payload.get("streams") if isinstance(request_payload.get("streams"), list) else []
        cached_refs: list[dict[str, Any]] = []
        appliers = dict(self._state_delta_appliers)
        appliers["answers"] = self._wrap_content_object_cache_applier(appliers["answers"], cached_refs)
        appliers["assets"] = self._wrap_content_object_cache_applier(appliers["assets"], cached_refs)
        result = process_state_delta_inbox(
            inbox=CloudStateDeltaInbox(),
            appliers=appliers,
            limit=_safe_int(request_payload.get("limit"), 100),
            streams=[str(stream) for stream in streams],
            include_failed=bool(request_payload.get("include_failed") or request_payload.get("includeFailed")),
        )
        if cached_refs:
            result = dict(result)
            result["object_cache"] = _summarize_object_cache_results(cached_refs)
        return {"ok": bool(result.get("ok")), "state_delta_inbox": result}

    def _wrap_content_object_cache_applier(
        self,
        applier: Callable[[dict[str, Any]], Any],
        cache_results: list[dict[str, Any]],
    ) -> Callable[[dict[str, Any]], Any]:
        def wrapped(item: dict[str, Any]) -> Any:
            result = applier(item)
            for object_ref in _object_refs_from_inbox_item(item):
                try:
                    cache_result = self.cache_cloud_object({"object_ref": object_ref})
                except Exception as exc:
                    cache_result = {"ok": False, "message": str(exc)}
                cache_results.append(
                    {
                        "object_id": str(object_ref.get("object_id") or object_ref.get("objectId") or object_ref.get("id") or ""),
                        "sha256": str(object_ref.get("sha256") or ""),
                        "ok": bool(cache_result.get("ok")) if isinstance(cache_result, dict) else False,
                        "downloaded": bool(cache_result.get("downloaded")) if isinstance(cache_result, dict) else False,
                        "message": str(cache_result.get("message") or "") if isinstance(cache_result, dict) else "",
                    }
                )
            return result

        return wrapped

    def cache_cloud_object(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        object_ref_payload = request_payload.get("object_ref") or request_payload.get("objectRef")
        try:
            object_ref = normalize_object_ref(object_ref_payload if isinstance(object_ref_payload, dict) else request_payload)
        except CloudObjectCacheError as exc:
            return {"ok": False, "message": str(exc)}
        object_id = str(object_ref.get("object_id") or "").strip()
        if not object_id:
            return {"ok": False, "message": "object_id is required"}
        try:
            cache = self._object_cache_factory()
            cached = cache.cached_object(object_ref)
            if bool(cached.get("valid")) and not bool(request_payload.get("force")):
                return {"ok": True, "cached": cached, "downloaded": False, "message": ""}
        except CloudObjectCacheError as exc:
            return {"ok": False, "message": str(exc)}

        trace_id = str(request_payload.get("trace_id") or request_payload.get("traceId") or "").strip()
        transfer_id = trace_id or f"download:{object_id}:{object_ref.get('sha256') or ''}"
        transfer_store = self._object_transfer_store_factory()
        transfer_store.start_transfer(
            transfer_id=transfer_id,
            direction="download",
            object_id=object_id,
            sha256=str(object_ref.get("sha256") or ""),
            size_bytes=_safe_int(object_ref.get("size_bytes"), 0),
            content_type=str(object_ref.get("content_type") or ""),
            trace_id=trace_id,
        )

        def operation(client: Any, token: str) -> Any:
            download = client.create_object_download(token, object_id, trace_id=trace_id)
            download_url = str(download.get("download_url") or download.get("downloadUrl") or "").strip()
            if not download_url:
                raise CloudClientError("object download_url missing")
            merged_ref = dict(object_ref)
            for key in ("size_bytes", "storage_size_bytes", "content_type", "compression"):
                if not merged_ref.get(key) and download.get(key) is not None:
                    merged_ref[key] = download.get(key)
            cached_result = cache.cache_bytes(
                merged_ref,
                client.iter_object_content(token, download_url, trace_id=trace_id),
                trace_id=trace_id,
            )
            transfer_store.finish_transfer(
                transfer_id,
                object_id=object_id,
                path=str(cached_result.get("path") or ""),
                status="completed",
            )
            return cached_result

        try:
            ok, response_payload, message = self.cloud_request_with_refresh(operation)
        except CloudObjectCacheError as exc:
            transfer_store.fail_transfer(transfer_id, str(exc))
            return {"ok": False, "message": str(exc)}
        if not ok:
            transfer_store.fail_transfer(transfer_id, message)
            return {"ok": False, "message": message}
        return {"ok": True, "cached": response_payload, "downloaded": True, "message": ""}

    def prune_cloud_object_cache(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        target = request_payload.get("target_bytes") or request_payload.get("targetBytes")
        target_bytes = _safe_int(target, -1)
        result = self._object_cache_factory().prune(target_bytes=target_bytes if target_bytes >= 0 else None)
        return {"ok": bool(result.get("ok", True)), "object_cache": result}

    def object_transfer_retry_candidates(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        transfers = self._object_transfer_store_factory().retryable_transfers(
            limit=_safe_int(request_payload.get("limit"), 50),
            direction=str(request_payload.get("direction") or ""),
            max_attempts=_safe_int(request_payload.get("max_attempts") or request_payload.get("maxAttempts"), 5),
            stale_running_seconds=_safe_float(
                request_payload.get("stale_running_seconds") or request_payload.get("staleRunningSeconds"),
                600.0,
            ),
        )
        return {"ok": True, "retryable_transfers": transfers, "count": len(transfers)}

    def retry_object_downloads(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        limit = _safe_int(request_payload.get("limit"), 10)
        transfers = self._object_transfer_store_factory().retryable_transfers(
            limit=limit,
            direction="download",
            max_attempts=_safe_int(request_payload.get("max_attempts") or request_payload.get("maxAttempts"), 5),
            stale_running_seconds=_safe_float(
                request_payload.get("stale_running_seconds") or request_payload.get("staleRunningSeconds"),
                600.0,
            ),
        )
        attempted = 0
        recovered = 0
        failed = 0
        failures: list[dict[str, Any]] = []
        for item in transfers:
            object_id = str(item.get("object_id") or "").strip()
            sha256 = str(item.get("sha256") or "").strip()
            if not object_id or not sha256:
                failed += 1
                failures.append(
                    {
                        "transfer_id": str(item.get("transfer_id") or ""),
                        "object_id": object_id,
                        "message": "missing object_id or sha256",
                    }
                )
                continue
            attempted += 1
            result = self.cache_cloud_object(
                {
                    "trace_id": str(item.get("transfer_id") or ""),
                    "object_ref": {
                        "object_id": object_id,
                        "sha256": sha256,
                        "size_bytes": _safe_int(item.get("size_bytes"), 0),
                        "content_type": str(item.get("content_type") or ""),
                    },
                    "force": True,
                }
            )
            if bool(result.get("ok")):
                recovered += 1
                continue
            failed += 1
            failures.append(
                {
                    "transfer_id": str(item.get("transfer_id") or ""),
                    "object_id": object_id,
                    "message": str(result.get("message") or "download retry failed"),
                }
            )
        return {
            "ok": failed == 0,
            "attempted": attempted,
            "recovered": recovered,
            "failed": failed,
            "skipped": max(0, len(transfers) - attempted),
            "failures": failures[:10],
        }

    def retry_object_uploads(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = payload if isinstance(payload, dict) else {}
        limit = _safe_int(request_payload.get("limit"), 10)
        transfers = self._object_transfer_store_factory().retryable_transfers(
            limit=limit,
            direction="upload",
            max_attempts=_safe_int(request_payload.get("max_attempts") or request_payload.get("maxAttempts"), 5),
            stale_running_seconds=_safe_float(
                request_payload.get("stale_running_seconds") or request_payload.get("staleRunningSeconds"),
                600.0,
            ),
        )
        attempted = 0
        recovered = 0
        failed = 0
        skipped = 0
        failures: list[dict[str, Any]] = []
        transfer_store = self._object_transfer_store_factory()
        for item in transfers:
            transfer_id = str(item.get("transfer_id") or "").strip()
            path = Path(str(item.get("path") or "").strip())
            expected_sha256 = str(item.get("sha256") or "").strip().lower()
            expected_size = _safe_int(item.get("size_bytes"), 0)
            content_type = str(item.get("content_type") or "application/octet-stream").strip() or "application/octet-stream"
            if not transfer_id or not expected_sha256 or expected_size <= 0 or not path.is_file():
                skipped += 1
                message = "upload source is missing or incomplete"
                if transfer_id:
                    transfer_store.fail_transfer(transfer_id, message)
                failures.append({"transfer_id": transfer_id, "path": str(path), "message": message})
                continue
            try:
                actual_sha256, actual_size = _file_sha256_and_size(path)
            except Exception as exc:
                skipped += 1
                message = f"upload source cannot be read: {exc}"
                transfer_store.fail_transfer(transfer_id, message)
                failures.append({"transfer_id": transfer_id, "path": str(path), "message": message})
                continue
            if actual_sha256 != expected_sha256 or actual_size != expected_size:
                skipped += 1
                message = "upload source changed since failed transfer"
                transfer_store.fail_transfer(transfer_id, message)
                failures.append({"transfer_id": transfer_id, "path": str(path), "message": message})
                continue
            attempted += 1

            def operation(client: Any, token: str) -> dict[str, Any]:
                return upload_cloud_object_file(
                    client,
                    token,
                    path,
                    content_type=content_type,
                    compression="auto",
                    trace_id=transfer_id,
                    transfer_store=transfer_store,
                )

            try:
                ok, response_payload, message = self.cloud_request_with_refresh(operation)
            except Exception as exc:
                failed += 1
                failure_message = str(exc or "upload retry failed")
                transfer_store.fail_transfer(transfer_id, failure_message)
                failures.append({"transfer_id": transfer_id, "path": str(path), "message": failure_message})
                continue
            if ok and isinstance(response_payload, dict) and bool(response_payload.get("ok")):
                recovered += 1
                continue
            failed += 1
            response_dict = response_payload if isinstance(response_payload, dict) else {}
            failure_message = str(message or response_dict.get("message") or "upload retry failed")
            transfer_store.fail_transfer(transfer_id, failure_message)
            failures.append({"transfer_id": transfer_id, "path": str(path), "message": failure_message})
        return {
            "ok": failed == 0,
            "attempted": attempted,
            "recovered": recovered,
            "failed": failed,
            "skipped": skipped,
            "failures": failures[:10],
        }

    def _build_state_delta_appliers(
        self,
        custom_appliers: dict[str, Callable[[dict[str, Any]], Any]] | None = None,
    ) -> dict[str, Callable[[dict[str, Any]], Any]]:
        appliers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "profile": self._apply_profile_state_delta,
            "tasks": self._apply_task_state_delta,
            "runs": self._apply_run_state_delta,
            "articles": self._apply_article_state_delta,
            "references": self._apply_reference_state_delta,
            "answers": self._apply_answer_state_delta,
            "assets": self._apply_asset_state_delta,
            "agent_status": self._apply_agent_status_state_delta,
        }
        appliers.update(custom_appliers or {})
        return appliers

    def _apply_profile_state_delta(self, item: dict[str, Any]) -> None:
        entity = item.get("entity") if isinstance(item.get("entity"), dict) else {}
        if str(item.get("stream") or "") != "profile" or str(entity.get("type") or "") != "profile":
            raise ValueError("state-delta profile item has invalid shape")
        store = self._session_store_factory()
        session = store.load()
        current_user = session.get("user") if isinstance(session.get("user"), dict) else {}
        current_workspace_id = str(current_user.get("workspace_id") or "").strip()
        current_user_id = str(current_user.get("id") or "").strip()
        incoming_workspace_id = str(entity.get("workspace_id") or "").strip()
        incoming_user_id = str(entity.get("user_id") or entity.get("id") or "").strip()
        if not current_workspace_id or not current_user_id:
            raise ValueError("未登录云端，无法应用 profile state-delta")
        if incoming_workspace_id != current_workspace_id or incoming_user_id != current_user_id:
            raise ValueError("profile state-delta 与当前云端账号不匹配")
        merged_user = dict(current_user)
        merged_user.update(
            {
                "id": _safe_int(entity.get("user_id") or entity.get("id"), _safe_int(current_user.get("id"), 0)),
                "workspace_id": _safe_int(
                    entity.get("workspace_id"),
                    _safe_int(current_user.get("workspace_id"), 0),
                ),
                "username": str(entity.get("username") or current_user.get("username") or ""),
                "role": str(entity.get("role") or current_user.get("role") or ""),
                "display_name": entity.get("display_name"),
                "email": entity.get("email"),
                "email_verified": bool(entity.get("email_verified")),
                "avatar": entity.get("avatar"),
                "birthday": entity.get("birthday"),
                "hire_date": entity.get("hire_date"),
                "view_all_tasks": bool(entity.get("view_all_tasks")),
                "enabled": bool(entity.get("enabled", True)),
                "updated_at": str(entity.get("updated_at") or current_user.get("updated_at") or ""),
            }
        )
        store.update_user(merged_user)

    def _apply_task_state_delta(self, item: dict[str, Any]) -> None:
        entity = item.get("entity") if isinstance(item.get("entity"), dict) else {}
        if str(item.get("stream") or "") != "tasks" or str(entity.get("type") or "") != "task":
            raise ValueError("state-delta task item has invalid shape")
        task_id = _safe_int(entity.get("id"), 0)
        workspace_id = _safe_int(entity.get("workspace_id"), 0)
        if task_id <= 0 or workspace_id <= 0:
            raise ValueError("state-delta task item is missing id/workspace_id")
        session = self._session_store_factory().load()
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        current_workspace_id = _safe_int(user.get("workspace_id"), 0)
        if current_workspace_id and workspace_id != current_workspace_id:
            raise ValueError("task state-delta 与当前云端工作区不匹配")
        config = self._load_runtime_config()
        deleted_at = str(entity.get("deleted_at") or "").strip()
        delete_expires_at = str(entity.get("delete_expires_at") or "").strip()
        if deleted_at or delete_expires_at:
            summary = merge_cloud_tasks_into_config(
                config,
                [],
                deleted_cloud_tasks=[entity],
                cloud_user=user,
                base_url=str(session.get("base_url") or ""),
                match_by_name=False,
                disable_missing=False,
                missing_cloud_task_ids=[task_id],
            )
        else:
            summary = merge_cloud_tasks_into_config(
                config,
                [entity],
                cloud_user=user,
                base_url=str(session.get("base_url") or ""),
                match_by_name=False,
                disable_missing=False,
                missing_cloud_task_ids=[task_id],
            )
        changed = (
            int(summary.get("added") or 0)
            + int(summary.get("updated") or 0)
            + int(summary.get("revoked") or 0)
            + int(summary.get("deleted") or 0)
            + int(summary.get("deleted_backups") or 0)
            + int(summary.get("deleted_pending") or 0)
        )
        if changed:
            self._save_runtime_config(config)
            self._invalidate_owner_task_views()

    def _apply_article_state_delta(self, item: dict[str, Any]) -> None:
        entity = item.get("entity") if isinstance(item.get("entity"), dict) else {}
        if str(item.get("stream") or "") != "articles" or str(entity.get("type") or "") != "article":
            raise ValueError("state-delta article item has invalid shape")
        session = self._session_store_factory().load()
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        workspace_id = _safe_int(entity.get("workspace_id") or entity.get("workspaceId"), 0)
        current_workspace_id = _safe_int(user.get("workspace_id"), 0)
        if workspace_id > 0 and current_workspace_id and workspace_id != current_workspace_id:
            raise ValueError("article state-delta 与当前云端工作区不匹配")
        config = self._load_runtime_config()
        summary = merge_cloud_article_entity_into_store(config, entity, cloud_user=user)
        if not bool(summary.get("ok")):
            raise ValueError(str(summary.get("message") or "article state-delta apply failed"))
        if int(summary.get("state_updated") or 0):
            self._invalidate_owner_article_views()

    def _apply_reference_state_delta(self, item: dict[str, Any]) -> None:
        entity = item.get("entity") if isinstance(item.get("entity"), dict) else {}
        if str(item.get("stream") or "") != "references" or str(entity.get("type") or "") != "article_reference":
            raise ValueError("state-delta reference item has invalid shape")
        session = self._session_store_factory().load()
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        workspace_id = _safe_int(entity.get("workspace_id") or entity.get("workspaceId"), 0)
        current_workspace_id = _safe_int(user.get("workspace_id"), 0)
        if workspace_id > 0 and current_workspace_id and workspace_id != current_workspace_id:
            raise ValueError("reference state-delta 与当前云端工作区不匹配")
        config = self._load_runtime_config()
        summary = merge_cloud_article_reference_entity_into_store(config, entity, cloud_user=user)
        if not bool(summary.get("ok")):
            raise ValueError(str(summary.get("message") or "reference state-delta apply failed"))
        if int(summary.get("state_updated") or 0):
            self._invalidate_owner_article_views()

    def _apply_agent_status_state_delta(self, item: dict[str, Any]) -> None:
        entity = item.get("entity") if isinstance(item.get("entity"), dict) else {}
        if str(item.get("stream") or "") != "agent_status" or str(entity.get("type") or "") != "agent_command_status":
            raise ValueError("state-delta agent_status item has invalid shape")
        session = self._session_store_factory().load()
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        workspace_id = _safe_int(entity.get("workspace_id") or entity.get("workspaceId"), 0)
        current_workspace_id = _safe_int(user.get("workspace_id"), 0)
        if workspace_id > 0 and current_workspace_id and workspace_id != current_workspace_id:
            raise ValueError("agent_status state-delta 与当前云端工作区不匹配")
        identity_key = str(item.get("identity_key") or cloud_session_identity_key(session)).strip()
        result = CloudAgentStatusStore().apply_status(identity_key, entity)
        if not bool(result.get("ok")):
            raise ValueError(str(result.get("message") or "agent_status state-delta apply failed"))

    def _apply_answer_state_delta(self, item: dict[str, Any]) -> None:
        entity = item.get("entity") if isinstance(item.get("entity"), dict) else {}
        if str(item.get("stream") or "") != "answers":
            raise ValueError("state-delta answers item has invalid stream")
        session = self._session_store_factory().load()
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        workspace_id = _safe_int(entity.get("workspace_id") or entity.get("workspaceId"), 0)
        current_workspace_id = _safe_int(user.get("workspace_id"), 0)
        if workspace_id > 0 and current_workspace_id and workspace_id != current_workspace_id:
            raise ValueError("answer state-delta 与当前云端工作区不匹配")
        identity_key = str(item.get("identity_key") or cloud_session_identity_key(session)).strip()
        object_refs = item.get("object_refs") if isinstance(item.get("object_refs"), list) else []
        result = CloudContentStateStore().apply_answer(
            identity_key,
            entity,
            object_refs=object_refs,
            ref_id=str(item.get("ref_id") or ""),
        )
        if not bool(result.get("ok")):
            raise ValueError(str(result.get("message") or "answer state-delta apply failed"))

    def _apply_asset_state_delta(self, item: dict[str, Any]) -> None:
        entity = item.get("entity") if isinstance(item.get("entity"), dict) else {}
        if str(item.get("stream") or "") != "assets":
            raise ValueError("state-delta assets item has invalid stream")
        session = self._session_store_factory().load()
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        workspace_id = _safe_int(entity.get("workspace_id") or entity.get("workspaceId"), 0)
        current_workspace_id = _safe_int(user.get("workspace_id"), 0)
        if workspace_id > 0 and current_workspace_id and workspace_id != current_workspace_id:
            raise ValueError("asset state-delta 与当前云端工作区不匹配")
        identity_key = str(item.get("identity_key") or cloud_session_identity_key(session)).strip()
        object_refs = item.get("object_refs") if isinstance(item.get("object_refs"), list) else []
        result = CloudContentStateStore().apply_asset(
            identity_key,
            entity,
            object_refs=object_refs,
            ref_id=str(item.get("ref_id") or ""),
        )
        if not bool(result.get("ok")):
            raise ValueError(str(result.get("message") or "asset state-delta apply failed"))

    def _apply_run_state_delta(self, item: dict[str, Any]) -> None:
        entity = item.get("entity") if isinstance(item.get("entity"), dict) else {}
        if str(item.get("stream") or "") != "runs":
            raise ValueError("state-delta runs item has invalid stream")
        entity_type = str(entity.get("type") or "").strip()
        if entity_type not in {"run_record", "task_day_status"}:
            raise ValueError("state-delta runs item has invalid shape")
        workspace_id = _safe_int(entity.get("workspace_id") or entity.get("workspaceId"), 0)
        session = self._session_store_factory().load()
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        current_workspace_id = _safe_int(user.get("workspace_id"), 0)
        if workspace_id > 0 and current_workspace_id and workspace_id != current_workspace_id:
            raise ValueError("runs state-delta 与当前云端工作区不匹配")

        config = self._load_runtime_config()
        common_kwargs = {
            "cloud_user": user,
            "base_url": str(session.get("base_url") or ""),
        }
        if entity_type == "run_record":
            summary = merge_cloud_run_record_entity_into_config(config, entity, **common_kwargs)
            changed = (
                int(summary.get("imported") or 0)
                + int(summary.get("cursor_updates") or 0)
                + int(summary.get("state_updated") or 0)
                + int(summary.get("backfilled") or 0)
            )
        else:
            summary = merge_cloud_task_day_status_entity_into_config(config, entity, **common_kwargs)
            changed = (
                int(summary.get("applied") or 0)
                + int(summary.get("cursor_updates") or 0)
                + int(summary.get("state_updated") or 0)
            )
        if int(summary.get("failed") or 0):
            raise ValueError(str(summary.get("error") or "runs state-delta apply failed"))
        if changed:
            self._save_runtime_config(config)
            self._invalidate_owner_task_views()

    def _save_runtime_config(self, config: dict[str, Any]) -> None:
        save_config = getattr(self._owner, "save_config", None)
        if callable(save_config):
            save_config(config)

    def _invalidate_owner_task_views(self) -> None:
        invalidate_tasks = getattr(self._owner, "_invalidate_tasks_full_cache", None)
        if callable(invalidate_tasks):
            invalidate_tasks()
        refresh_runtime = getattr(self._owner, "_refresh_monitoring_runtime", None)
        if callable(refresh_runtime):
            refresh_runtime(restart_scheduler=True)

    def _invalidate_owner_article_views(self) -> None:
        invalidate_articles = getattr(self._owner, "_invalidate_article_cache", None)
        if callable(invalidate_articles):
            invalidate_articles()

    def cloud_request_with_refresh(self, operation: Callable[[Any, str], Any]) -> tuple[bool, Any, str]:
        store = self._session_store_factory()
        session = store.load()
        base_url = str(session.get("base_url") or "").strip()
        access_token = str(session.get("access_token") or "").strip()
        refresh_token = str(session.get("refresh_token") or "").strip()
        if not base_url or not access_token:
            return False, None, "未登录云端"
        initial_identity_key = cloud_session_identity_key(session)
        identity = cloud_session_identity(session)
        client = self._request_client_factory(base_url)
        try:
            payload = operation(client, access_token)
            if cloud_session_identity_key(store.load()) != initial_identity_key:
                return False, None, "云端账号已切换，本次操作已中止"
            return True, payload, ""
        except CloudClientError as exc:
            if exc.status_code != 401 or not refresh_token:
                return False, None, str(exc)
        try:
            refreshed_session = store.refresh_login_if_current(
                base_url=base_url,
                access_token=access_token,
                refresh_token=refresh_token,
                refresh=client.refresh,
                workspace_id=identity["workspace_id"],
                user_id=identity["user_id"],
            )
        except CloudSessionChangedError as changed_exc:
            return False, None, str(changed_exc)
        except CloudClientError as refresh_exc:
            if refresh_exc.status_code == 401:
                store.clear_if_current(
                    base_url=base_url,
                    access_token=access_token,
                    refresh_token=refresh_token,
                    workspace_id=identity["workspace_id"],
                    user_id=identity["user_id"],
                )
            return False, None, str(refresh_exc)
        refreshed_access_token = str(refreshed_session.get("access_token") or "").strip()
        if not refreshed_access_token:
            return False, None, "未登录云端"
        try:
            payload = operation(client, refreshed_access_token)
            if cloud_session_identity_key(store.load()) != initial_identity_key:
                return False, None, "云端账号已切换，本次操作已中止"
            return True, payload, ""
        except CloudClientError as retry_exc:
            refreshed_identity = cloud_session_identity(refreshed_session)
            if retry_exc.status_code == 401:
                store.clear_if_current(
                    base_url=base_url,
                    access_token=refreshed_access_token,
                    refresh_token=str(refreshed_session.get("refresh_token") or "").strip(),
                    workspace_id=refreshed_identity["workspace_id"],
                    user_id=refreshed_identity["user_id"],
                )
            return False, None, str(retry_exc)

    def _run_cloud_api_request(self, operation_name: str, request_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = request_payload if isinstance(request_payload, dict) else {}
        body = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}

        def operation(client: Any, token: str) -> Any:
            if operation_name == "list_admin_users":
                return client.list_admin_users(token)
            if operation_name == "list_admin_article_classification_jobs":
                return client.list_admin_article_classification_jobs(
                    token,
                    status=str(payload.get("status") or "unresolved").strip() or "unresolved",
                    limit=_safe_int(payload.get("limit"), 200),
                )
            if operation_name == "resolve_admin_article_classification_job":
                return client.resolve_admin_article_classification_job(
                    token,
                    _safe_int(payload.get("job_id") or payload.get("jobId"), 0),
                    task_id=_safe_int(payload.get("task_id") or payload.get("taskId"), 0),
                    reason=str(payload.get("reason") or "").strip(),
                )
            if operation_name == "ignore_admin_article_classification_job":
                return client.ignore_admin_article_classification_job(
                    token,
                    _safe_int(payload.get("job_id") or payload.get("jobId"), 0),
                    reason=str(payload.get("reason") or "").strip(),
                )
            if operation_name == "create_admin_user":
                return client.create_admin_user(token, body)
            if operation_name == "update_admin_user":
                return client.update_admin_user(token, _safe_int(payload.get("user_id") or payload.get("userId"), 0), body)
            if operation_name == "delete_admin_user":
                return client.delete_admin_user(token, _safe_int(payload.get("user_id") or payload.get("userId"), 0))
            if operation_name == "update_admin_task":
                return client.update_admin_task(token, _safe_int(payload.get("task_id") or payload.get("taskId"), 0), body)
            if operation_name == "delete_admin_task":
                return client.delete_admin_task(token, _safe_int(payload.get("task_id") or payload.get("taskId"), 0))
            if operation_name == "restore_admin_task":
                return client.restore_admin_task(token, _safe_int(payload.get("task_id") or payload.get("taskId"), 0))
            raise ValueError(f"unsupported cloud api request: {operation_name}")

        ok, response_payload, message = self.cloud_request_with_refresh(operation)
        return {"ok": bool(ok), "payload": response_payload, "message": message}

    def _run_cloud_task_sync_request(self, request_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = request_payload if isinstance(request_payload, dict) else {}
        local_snapshots = payload.get("local_snapshots") if isinstance(payload.get("local_snapshots"), list) else []

        def operation(client: Any, token: str) -> Any:
            ensure_local = getattr(self._owner, "_ensure_local_admin_tasks_in_cloud", None)
            if not callable(ensure_local):
                raise RuntimeError("cloud task sync helper unavailable")
            return ensure_local(client, token, local_snapshots)

        ok, response_payload, message = self.cloud_request_with_refresh(operation)
        return {"ok": bool(ok), "payload": response_payload, "message": message}

    def _run_sync_admin_task_request(self, request_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = request_payload if isinstance(request_payload, dict) else {}
        local_task_id = str(payload.get("local_task_id") or payload.get("task_id") or "").strip()
        local_task = payload.get("local_task") if isinstance(payload.get("local_task"), dict) else {}
        cloud_payload = payload.get("cloud_payload") if isinstance(payload.get("cloud_payload"), dict) else {}
        existing_cloud_task_id = _safe_int(payload.get("existing_cloud_task_id") or payload.get("existingCloudTaskId"), 0)
        operator_user_id = _safe_int(payload.get("operator_user_id") or payload.get("operatorUserId"), 0)

        def operation(client: Any, token: str) -> Any:
            if existing_cloud_task_id > 0:
                saved = client.update_admin_task(token, existing_cloud_task_id, cloud_payload)
            else:
                saved = client.create_admin_task(
                    token,
                    {
                        "task_key": self._owner._cloud_task_key_for_local_task(local_task, local_task_id),
                        **cloud_payload,
                    },
                )
            saved_task_id = _safe_int(saved.get("id") if isinstance(saved, dict) else 0, existing_cloud_task_id)
            if operator_user_id > 0 and saved_task_id > 0:
                client.assign_admin_task_member(
                    token,
                    saved_task_id,
                    user_id=operator_user_id,
                    access_level="operate",
                    note="品牌编辑页分配",
                )
            if saved_task_id > 0:
                try:
                    for task in client.list_admin_tasks(token):
                        if _safe_int(task.get("id") if isinstance(task, dict) else 0, 0) == saved_task_id:
                            return task
                except Exception as exc:
                    print(f"[WebBackend] 云端任务分配后刷新任务快照失败，将使用保存结果: {exc}")
            return saved

        ok, response_payload, message = self.cloud_request_with_refresh(operation)
        return {"ok": bool(ok), "payload": response_payload, "message": message}

    def _load_runtime_config(self) -> dict[str, Any]:
        if callable(getattr(self._owner, "load_config", None)):
            loaded = self._owner.load_config()
            return loaded if isinstance(loaded, dict) else {}
        config_provider = getattr(self._owner, "config_provider", None)
        if config_provider is not None and callable(getattr(config_provider, "load", None)):
            loaded = config_provider.load()
            return loaded if isinstance(loaded, dict) else {}
        return {}

    def _load_runtime_config_locked(self) -> dict[str, Any]:
        lock = getattr(self._owner, "_lock", None)
        if lock is None:
            return self._load_runtime_config()
        with lock:
            return self._load_runtime_config()

    def _flush_bound_outbox_if_needed(self, session: dict[str, Any], *, failure_message: str) -> None:
        try:
            current_outbox = self._outbox_factory().bind_to_session(session)
            current_stats = current_outbox.stats()
            if current_stats.get("pending", 0) or current_stats.get("failed", 0):
                self._flush_outbox_fn(outbox=current_outbox)
        except Exception as exc:
            print(failure_message.format(exc=exc))

    def _read_article_snapshot_startup_delay_seconds(self) -> float:
        try:
            return max(
                0.0,
                float(
                    os.environ.get(
                        "AIBRANDMONITOR_CLOUD_ARTICLES_SNAPSHOT_STARTUP_DELAY_SECONDS",
                        self._article_snapshot_startup_delay_seconds,
                    )
                ),
            )
        except Exception:
            return max(0.0, self._article_snapshot_startup_delay_seconds)

    def _reset_article_snapshot_state(self) -> None:
        with self._article_cloud_enqueue_lock:
            self._last_article_cloud_enqueue_key = None
            self._article_cloud_enqueue_requested = False

    def _reset_validation_state(self) -> None:
        with self._cloud_status_validation_lock:
            self._cloud_status_validated_identity = ""
            self._cloud_status_validated_at = 0.0
            self._cloud_status_validation_error = ""

    @staticmethod
    def _join_thread_if_possible(thread: Any) -> None:
        if (
            thread is None
            or thread is threading.current_thread()
            or not callable(getattr(thread, "join", None))
        ):
            return
        try:
            thread.join(timeout=1.0)
        except Exception:
            pass

    @staticmethod
    def _build_login_session_preview(*, base_url: str, token_pair: dict[str, Any]) -> dict[str, Any]:
        user = token_pair.get("user") if isinstance(token_pair.get("user"), dict) else {}
        return {
            "base_url": normalize_cloud_base_url(base_url),
            "access_token": str(token_pair.get("access_token") or "").strip(),
            "refresh_token": str(token_pair.get("refresh_token") or "").strip(),
            "token_type": str(token_pair.get("token_type") or "bearer").strip() or "bearer",
            "user": dict(user or {}),
        }

    @staticmethod
    def _cloud_role(session: dict[str, Any] | None) -> str:
        user = session.get("user") if isinstance(session, dict) and isinstance(session.get("user"), dict) else {}
        return str(user.get("role") or "").strip()

    @staticmethod
    def _default_has_pending_profile_update(session: dict[str, Any]) -> bool:
        try:
            queue = CloudOutbox().bind_to_session(session)
            return any(
                str(item.get("event_type") or "") == EVENT_PROFILE_UPDATE
                for item in queue.pending(limit=50)
            )
        except Exception:
            return False

    @staticmethod
    def _default_status_client_factory(base_url: str) -> Any:
        try:
            return SurfacedCloudClient(base_url, timeout_seconds=3.0)
        except TypeError:
            return SurfacedCloudClient(base_url)


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _file_sha256_and_size(path: Path, *, chunk_bytes: int = 1024 * 1024) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(max(1, int(chunk_bytes or 1024 * 1024)))
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _object_refs_from_inbox_item(item: dict[str, Any]) -> list[dict[str, Any]]:
    refs = list(item.get("object_refs") if isinstance(item.get("object_refs"), list) else [])
    entity = item.get("entity") if isinstance(item.get("entity"), dict) else {}
    if _looks_like_cacheable_object_ref(entity):
        refs.append(entity)
    if entity:
        nested_refs = entity.get("object_refs") if isinstance(entity.get("object_refs"), list) else []
        refs.extend(ref for ref in nested_refs if isinstance(ref, dict))
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        object_id = str(ref.get("object_id") or ref.get("objectId") or ref.get("id") or "").strip()
        sha256 = str(ref.get("sha256") or "").strip()
        key = object_id or sha256
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(ref)
    return normalized


def _looks_like_cacheable_object_ref(ref: Any) -> bool:
    if not isinstance(ref, dict):
        return False
    sha256 = str(ref.get("sha256") or "").strip()
    object_id = str(ref.get("object_id") or ref.get("objectId") or "").strip()
    return bool(sha256 and object_id)


def _summarize_object_cache_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    attempted = len(results)
    cached = sum(1 for item in results if bool(item.get("ok")))
    downloaded = sum(1 for item in results if bool(item.get("downloaded")))
    failed = attempted - cached
    failures = [
        {
            "object_id": str(item.get("object_id") or ""),
            "sha256": str(item.get("sha256") or ""),
            "message": str(item.get("message") or ""),
        }
        for item in results
        if not bool(item.get("ok"))
    ]
    return {
        "attempted": attempted,
        "cached": cached,
        "downloaded": downloaded,
        "failed": failed,
        "failures": failures[:10],
    }


def _cloud_sync_health_summary(
    *,
    session: dict[str, Any] | None,
    auto_sync: dict[str, Any],
    outbox: dict[str, Any],
    inbox: dict[str, Any],
    agent_status: dict[str, Any],
    content_state: dict[str, Any],
    object_cache: dict[str, Any] | None = None,
    object_transfers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    session_payload = session if isinstance(session, dict) else {}
    outbox_stats = outbox.get("stats") if isinstance(outbox.get("stats"), dict) else {}
    inbox_by_status = inbox.get("by_status") if isinstance(inbox.get("by_status"), dict) else {}
    pending_outbox = _safe_int(outbox_stats.get("pending"), 0)
    failed_outbox = _safe_int(outbox_stats.get("failed"), 0)
    dead_letter = _safe_int(outbox_stats.get("dead_letter"), 0)
    upload_ready = _safe_int(outbox_stats.get("upload_ready"), pending_outbox)
    pending_inbox = _safe_int(inbox_by_status.get("pending"), 0)
    failed_inbox = _safe_int(inbox_by_status.get("failed"), 0)
    applied_inbox = _safe_int(inbox_by_status.get("applied"), 0)
    cache_payload = object_cache if isinstance(object_cache, dict) else {}
    transfers_payload = object_transfers if isinstance(object_transfers, dict) else {}
    transfer_by_status = transfers_payload.get("by_status") if isinstance(transfers_payload.get("by_status"), dict) else {}
    backpressure_until = str(auto_sync.get("upload_backpressure_until") or "")
    last_error = str(auto_sync.get("last_error") or auto_sync.get("last_state_delta_error") or "")
    return {
        "logged_in": bool(
            session_payload.get("base_url")
            and session_payload.get("access_token")
            and session_payload.get("refresh_token")
        ),
        "auto_sync_running": bool(auto_sync.get("running")),
        "event_stream_connected": bool(auto_sync.get("event_stream_connected")),
        "upload_backpressure_active": bool(backpressure_until),
        "upload_backpressure_until": backpressure_until,
        "upload_backpressure_retry_after_seconds": _safe_float(
            auto_sync.get("upload_backpressure_retry_after_seconds"),
            0.0,
        ),
        "upload_backpressure_queue_depth_hint": _safe_int(
            auto_sync.get("upload_backpressure_queue_depth_hint"),
            0,
        ),
        "upload_backpressure_bucket": str(auto_sync.get("upload_backpressure_bucket") or ""),
        "outbox_pending": pending_outbox,
        "outbox_failed": failed_outbox,
        "outbox_dead_letter": dead_letter,
        "outbox_upload_ready": upload_ready,
        "next_retry_after_seconds": _safe_int(outbox_stats.get("next_retry_after_seconds"), 0),
        "inbox_pending": pending_inbox,
        "inbox_failed": failed_inbox,
        "inbox_applied": applied_inbox,
        "agent_status_total": _safe_int(agent_status.get("total"), 0),
        "answers_cached": _safe_int(content_state.get("answers_total"), 0),
        "assets_cached": _safe_int(content_state.get("assets_total"), 0),
        "object_cache_objects": _safe_int(cache_payload.get("objects"), 0),
        "object_cache_bytes": _safe_int(cache_payload.get("bytes"), 0),
        "object_cache_max_bytes": _safe_int(cache_payload.get("max_cache_bytes"), 0),
        "object_transfers_total": _safe_int(transfers_payload.get("total"), 0),
        "object_transfers_failed": _safe_int(transfer_by_status.get("failed"), 0),
        "object_transfers_running": _safe_int(transfer_by_status.get("running"), 0),
        "last_upload_at": str(auto_sync.get("last_upload_at") or ""),
        "last_pull_at": str(auto_sync.get("last_pull_at") or ""),
        "last_state_delta_at": str(auto_sync.get("last_state_delta_at") or ""),
        "last_error": last_error,
        "healthy": not any(
            [
                dead_letter > 0,
                failed_inbox > 0,
                bool(last_error),
            ]
        ),
    }
