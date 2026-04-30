"""Background cloud sync manager."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from typing import Any, Callable

import requests

from .app_paths import resolve_app_path
from .time_utils import local_now

BundleBuilder = Callable[[bool], dict[str, Any]]
BundleApplier = Callable[[dict[str, Any], str], dict[str, Any]]
ConfigGetter = Callable[[], dict[str, Any]]
ConfigUpdater = Callable[[dict[str, Any]], None]

_DEFAULT_INTERVAL_SECONDS = 300
_DEFAULT_TIMEOUT_SECONDS = 15
_LOCAL_SYNC_FILES = (
    "config.yaml",
    "config.local.yaml",
    "logs/articles.json",
    "logs/domain_overrides.json",
    "logs/domain_media_names.json",
    "logs/diagnostics.json",
    "logs/history",
    "user_data/daily_task_status.json",
    "user_data/scheduler_state.json",
)


def _normalize_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on"}


def _safe_int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except Exception:
        return default


def _json_fingerprint(payload: dict[str, Any] | None) -> str:
    data = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


class CloudSyncManager:
    """Polls a remote endpoint and syncs local data in the background."""

    def __init__(
        self,
        *,
        config_getter: ConfigGetter,
        bundle_builder: BundleBuilder,
        bundle_applier: BundleApplier,
        config_updater: ConfigUpdater | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self._config_getter = config_getter
        self._bundle_builder = bundle_builder
        self._bundle_applier = bundle_applier
        self._config_updater = config_updater
        self._logger = logger or print

        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._session = requests.Session()

        self._last_local_file_state: tuple[Any, ...] | None = None
        self._last_local_fingerprint = ""
        self._last_pushed_fingerprint = ""
        self._last_remote_fingerprint = ""
        self._last_applied_remote_fingerprint = ""
        self._last_pull_started_at = 0.0
        self._last_push_started_at = 0.0
        self._initial_pull_done = False

        self._status: dict[str, Any] = {
            "enabled": False,
            "running": False,
            "device_id": "",
            "endpoint_url": "",
            "last_pull_at": "",
            "last_push_at": "",
            "last_error": "",
            "last_error_at": "",
            "last_remote_fingerprint": "",
            "last_local_fingerprint": "",
            "last_remote_apply_at": "",
            "pending_local_change": False,
            "push_on_local_change": True,
            "pull_on_startup": True,
            "sync_interval_seconds": _DEFAULT_INTERVAL_SECONDS,
        }

    def _log(self, message: str) -> None:
        try:
            self._logger(f"[CloudSync] {message}")
        except Exception:
            pass

    def _local_paths(self) -> list:
        return [resolve_app_path(path) for path in _LOCAL_SYNC_FILES]

    def _capture_local_file_state(self) -> tuple[Any, ...]:
        state = []
        for path in self._local_paths():
            try:
                if path.is_dir():
                    state.append((str(path), True, "dir"))
                    for child in sorted(item for item in path.rglob("*") if item.is_file()):
                        child_stat = child.stat()
                        state.append((str(child), True, child_stat.st_mtime_ns, child_stat.st_size))
                    continue
                stat = path.stat()
                state.append((str(path), True, stat.st_mtime_ns, stat.st_size))
            except FileNotFoundError:
                state.append((str(path), False, 0, 0))
            except Exception:
                state.append((str(path), False, -1, -1))
        return tuple(state)

    def _read_cloud_sync_config(self, config: dict[str, Any] | None) -> dict[str, Any]:
        current = (config or {}).get("cloud_sync", {}) or {}
        endpoint_url = str(current.get("endpoint_url", "") or "").strip()
        api_token = str(current.get("api_token", "") or "").strip()
        device_id = str(current.get("device_id", "") or "").strip()
        interval_seconds = _safe_int(current.get("sync_interval_seconds", _DEFAULT_INTERVAL_SECONDS), _DEFAULT_INTERVAL_SECONDS)
        timeout_seconds = _safe_int(current.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS), _DEFAULT_TIMEOUT_SECONDS)
        import_mode = str(current.get("import_mode", "merge") or "merge").strip().lower()
        if import_mode not in {"merge", "replace"}:
            import_mode = "merge"
        return {
            "enabled": bool(_normalize_bool(current.get("enabled", False), False) and endpoint_url),
            "endpoint_url": endpoint_url,
            "api_token": api_token,
            "device_id": device_id,
            "sync_interval_seconds": interval_seconds,
            "timeout_seconds": timeout_seconds,
            "push_on_local_change": _normalize_bool(current.get("push_on_local_change", True), True),
            "pull_on_startup": _normalize_bool(current.get("pull_on_startup", True), True),
            "import_mode": import_mode,
        }

    def _update_status(self, **patch: Any) -> None:
        with self._lock:
            self._status.update(patch)

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def _current_headers(self, sync_cfg: dict[str, Any]) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-AI-Monitor-Device-Id": str(sync_cfg.get("device_id", "") or "").strip(),
        }
        token = str(sync_cfg.get("api_token", "") or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _ensure_device_id(self, config: dict[str, Any], sync_cfg: dict[str, Any]) -> dict[str, Any]:
        device_id = str(sync_cfg.get("device_id", "") or "").strip()
        if device_id:
            return sync_cfg
        next_config = dict(config or {})
        cloud_sync_cfg = dict(next_config.get("cloud_sync", {}) or {})
        cloud_sync_cfg["device_id"] = f"device-{uuid.uuid4().hex[:16]}"
        next_config["cloud_sync"] = cloud_sync_cfg
        if callable(self._config_updater):
            try:
                self._config_updater(next_config)
            except Exception as exc:
                self._record_error(f"生成 device_id 失败: {exc}")
                return sync_cfg
        sync_cfg = dict(sync_cfg)
        sync_cfg["device_id"] = cloud_sync_cfg["device_id"]
        return sync_cfg

    def _build_bundle(self) -> tuple[dict[str, Any], str]:
        bundle = self._bundle_builder(False)
        data = bundle.get("data") if isinstance(bundle, dict) and isinstance(bundle.get("data"), dict) else bundle
        fingerprint = _json_fingerprint(data if isinstance(data, dict) else {})
        self._update_status(last_local_fingerprint=fingerprint)
        return bundle, fingerprint

    def _record_error(self, message: str) -> None:
        now = local_now().isoformat(timespec="seconds")
        self._update_status(last_error=str(message or "同步失败").strip(), last_error_at=now)
        self._log(str(message or "同步失败").strip())

    def _clear_error(self) -> None:
        self._update_status(last_error="", last_error_at="")

    def _pull_once(self, sync_cfg: dict[str, Any], *, reason: str) -> None:
        endpoint_url = str(sync_cfg.get("endpoint_url", "") or "").strip()
        if not endpoint_url:
            return
        self._last_pull_started_at = time.time()
        params = {"device_id": str(sync_cfg.get("device_id", "") or "").strip()}
        try:
            response = self._session.get(
                endpoint_url,
                params=params,
                headers=self._current_headers(sync_cfg),
                timeout=float(sync_cfg.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)),
            )
            if response.status_code in {204, 404}:
                self._update_status(last_pull_at=local_now().isoformat(timespec="seconds"))
                return
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            self._record_error(f"拉取同步数据失败: {exc}")
            return

        bundle = payload.get("bundle") if isinstance(payload, dict) and isinstance(payload.get("bundle"), dict) else payload
        if not isinstance(bundle, dict):
            self._update_status(last_pull_at=local_now().isoformat(timespec="seconds"))
            return

        bundle_data = bundle.get("data") if isinstance(bundle.get("data"), dict) else bundle
        remote_fingerprint = _json_fingerprint(bundle_data if isinstance(bundle_data, dict) else {})
        self._update_status(
            last_pull_at=local_now().isoformat(timespec="seconds"),
            last_remote_fingerprint=remote_fingerprint,
        )
        self._last_remote_fingerprint = remote_fingerprint

        local_bundle, local_fingerprint = self._build_bundle()
        del local_bundle
        if not remote_fingerprint or remote_fingerprint == local_fingerprint:
            self._clear_error()
            self._last_local_fingerprint = local_fingerprint
            self._last_applied_remote_fingerprint = remote_fingerprint or self._last_applied_remote_fingerprint
            return

        apply_result = self._bundle_applier(bundle, str(sync_cfg.get("import_mode", "merge") or "merge"))
        if not bool((apply_result or {}).get("ok", False)):
            self._record_error(str((apply_result or {}).get("message", "应用同步数据失败") or "应用同步数据失败"))
            return

        now = local_now().isoformat(timespec="seconds")
        self._last_applied_remote_fingerprint = remote_fingerprint
        self._last_local_file_state = self._capture_local_file_state()
        self._last_local_fingerprint = remote_fingerprint
        self._update_status(last_remote_apply_at=now, pending_local_change=False)
        self._clear_error()
        self._log(f"已自动拉取并应用云端同步包 ({reason})")

    def _push_once(self, sync_cfg: dict[str, Any], *, reason: str) -> None:
        endpoint_url = str(sync_cfg.get("endpoint_url", "") or "").strip()
        if not endpoint_url:
            return
        bundle, local_fingerprint = self._build_bundle()
        self._last_local_fingerprint = local_fingerprint
        if not local_fingerprint:
            return
        if local_fingerprint == self._last_pushed_fingerprint:
            self._update_status(pending_local_change=False)
            return
        if local_fingerprint == self._last_applied_remote_fingerprint:
            self._update_status(pending_local_change=False)
            return

        self._last_push_started_at = time.time()
        request_payload = {
            "device_id": str(sync_cfg.get("device_id", "") or "").strip(),
            "bundle": bundle,
            "client": {"name": "ai-monitor"},
        }
        try:
            response = self._session.post(
                endpoint_url,
                headers=self._current_headers(sync_cfg),
                json=request_payload,
                timeout=float(sync_cfg.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)),
            )
            response.raise_for_status()
            payload = response.json() if response.content else {}
        except Exception as exc:
            self._record_error(f"推送同步数据失败: {exc}")
            self._update_status(pending_local_change=True)
            return

        remote_bundle = payload.get("bundle") if isinstance(payload, dict) and isinstance(payload.get("bundle"), dict) else None
        if isinstance(remote_bundle, dict):
            remote_data = remote_bundle.get("data") if isinstance(remote_bundle.get("data"), dict) else remote_bundle
            self._last_remote_fingerprint = _json_fingerprint(remote_data if isinstance(remote_data, dict) else {})

        now = local_now().isoformat(timespec="seconds")
        self._last_pushed_fingerprint = local_fingerprint
        self._update_status(last_push_at=now, pending_local_change=False)
        self._clear_error()
        self._log(f"已自动推送本地变更 ({reason})")

    def _refresh_pending_local_change(self) -> bool:
        file_state = self._capture_local_file_state()
        changed = file_state != self._last_local_file_state
        self._last_local_file_state = file_state
        if not changed:
            current_pending = bool(self.get_status().get("pending_local_change", False))
            return current_pending

        _, local_fingerprint = self._build_bundle()
        self._last_local_fingerprint = local_fingerprint
        pending = bool(local_fingerprint and local_fingerprint not in {self._last_pushed_fingerprint, self._last_applied_remote_fingerprint})
        self._update_status(pending_local_change=pending)
        return pending

    def _loop(self) -> None:
        self._update_status(running=True)
        self._last_local_file_state = self._capture_local_file_state()
        while not self._stop_event.is_set():
            config = self._config_getter() or {}
            sync_cfg = self._read_cloud_sync_config(config)
            self._update_status(
                enabled=bool(sync_cfg.get("enabled", False)),
                endpoint_url=str(sync_cfg.get("endpoint_url", "") or "").strip(),
                device_id=str(sync_cfg.get("device_id", "") or "").strip(),
                push_on_local_change=bool(sync_cfg.get("push_on_local_change", True)),
                pull_on_startup=bool(sync_cfg.get("pull_on_startup", True)),
                sync_interval_seconds=int(sync_cfg.get("sync_interval_seconds", _DEFAULT_INTERVAL_SECONDS)),
            )

            if bool(sync_cfg.get("enabled", False)):
                sync_cfg = self._ensure_device_id(config, sync_cfg)
                self._update_status(device_id=str(sync_cfg.get("device_id", "") or "").strip())

                if not self._initial_pull_done and bool(sync_cfg.get("pull_on_startup", True)):
                    self._pull_once(sync_cfg, reason="startup")
                self._initial_pull_done = True

                pending_local_change = self._refresh_pending_local_change()
                retry_window = max(15, min(int(sync_cfg.get("sync_interval_seconds", _DEFAULT_INTERVAL_SECONDS)), 60))
                if (
                    pending_local_change
                    and bool(sync_cfg.get("push_on_local_change", True))
                    and (time.time() - self._last_push_started_at >= retry_window)
                ):
                    self._push_once(sync_cfg, reason="local_change")

                interval_seconds = int(sync_cfg.get("sync_interval_seconds", _DEFAULT_INTERVAL_SECONDS))
                now_ts = time.time()
                if now_ts - self._last_pull_started_at >= interval_seconds:
                    self._pull_once(sync_cfg, reason="interval")
            else:
                self._initial_pull_done = False
                self._update_status(pending_local_change=False)

            self._stop_event.wait(5.0)

        self._update_status(running=False)

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="cloud-sync")
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop_event.set()
            thread = self._thread
            self._thread = None
        if thread:
            thread.join(timeout=2.0)
