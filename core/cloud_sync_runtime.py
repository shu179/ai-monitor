"""Small facade for local cloud sync runtime wiring.

This module is intentionally thin for now. It centralizes the manager/auto-sync
construction so the next step can move cloud sync into a daemon without further
spreading web_backend.py dependencies.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

from core.cloud_platform_auto_sync import CloudPlatformAutoSync
from core.cloud_sync import CloudSyncManager
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


def create_local_cloud_sync_runtime(
    *,
    config_getter: Callable[[], dict[str, Any]],
    bundle_applier: Callable[..., Any],
    config_updater: Callable[[dict[str, Any]], Any],
    pull_tasks: Callable[..., dict[str, Any]],
    recover_upload_candidates: Callable[[], dict[str, Any]],
    logger: Callable[[str], None],
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
