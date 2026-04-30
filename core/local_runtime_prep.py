"""安装后本地模型运行时准备。"""

from __future__ import annotations

import copy
import time
from pathlib import Path
from typing import Any

from core.config_watcher import load_config
from core.local_model_manager import get_local_model_manager


DEFAULT_MODEL = "gemma4:e2b"


def _get_local_platform_config(config: dict[str, Any] | None) -> dict[str, Any]:
    platforms_cfg = (config or {}).get("platforms", {}) or {}
    entry = platforms_cfg.get("local_model")
    if isinstance(entry, dict):
        return entry
    legacy_entry = platforms_cfg.get("local_qwen")
    return legacy_entry if isinstance(legacy_entry, dict) else {}


def _resolve_target_model(config: dict[str, Any], model_override: str = "") -> str:
    candidate = str(model_override or "").strip()
    if candidate:
        return candidate

    local_cfg = (config or {}).get("local_model", {}) or {}
    return str(
        local_cfg.get("default_model")
        or _get_local_platform_config(config).get("api_model", "")
        or DEFAULT_MODEL
    ).strip() or DEFAULT_MODEL


def _with_prepare_overrides(config: dict[str, Any], allow_model_pull: bool) -> dict[str, Any]:
    next_config = copy.deepcopy(config or {})
    local_cfg = dict(next_config.get("local_model", {}) or {})
    local_cfg["auto_start"] = True
    local_cfg["auto_prepare_on_launch"] = False
    if not allow_model_pull:
        local_cfg["auto_pull"] = False
    next_config["local_model"] = local_cfg
    return next_config


def prepare_local_runtime(
    config_path: str | Path,
    *,
    model_override: str = "",
    timeout_seconds: int = 1800,
    allow_model_pull: bool = True,
    poll_interval_seconds: float = 2.0,
) -> dict[str, Any]:
    config = load_config(str(config_path))
    effective_config = _with_prepare_overrides(config, allow_model_pull=allow_model_pull)
    target_model = _resolve_target_model(effective_config, model_override=model_override)

    manager = get_local_model_manager()
    manager.sync_config(effective_config)

    ok, message = manager.ensure_ready(target_model, reason="installer_prepare")
    status = manager.get_status()
    if ok:
        return {
            "ok": True,
            "message": message or f"本地模型已准备完成：{target_model}",
            "model": target_model,
            "local_model": status,
        }

    deadline = time.time() + max(5, int(timeout_seconds or 0))
    last_message = str(message or status.get("status_message") or "").strip()

    while time.time() < deadline:
        status = manager.get_status()
        status_name = str(status.get("status") or "").strip()
        if bool(status.get("healthy")) and target_model in list(status.get("models") or []):
            return {
                "ok": True,
                "message": f"本地模型已准备完成：{target_model}",
                "model": target_model,
                "local_model": status,
            }

        current_message = str(status.get("status_message") or status.get("last_error") or "").strip()
        if current_message:
            last_message = current_message

        if status_name == "error" and not bool(status.get("pull_process_running")):
            return {
                "ok": False,
                "message": last_message or "本地模型准备失败",
                "model": target_model,
                "local_model": status,
            }

        time.sleep(max(0.5, float(poll_interval_seconds or 2.0)))

    status = manager.get_status()
    return {
        "ok": False,
        "message": last_message or f"本地模型准备超时：{target_model}",
        "model": target_model,
        "local_model": status,
    }


def shutdown_owned_local_runtime() -> None:
    get_local_model_manager().shutdown_owned_process()
