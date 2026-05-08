"""Browser runtime helpers for task execution."""

from __future__ import annotations

from core.browser_platform_factory import (
    apply_browser_runtime_config,
    create_browser_platform,
    resolve_browser_answer_screenshot_mode,
)
from core.platform_sessions import PlatformSessionManager, build_query_execution_policy


def _apply_browser_runtime_config(platform, platform_name: str, config: dict | None) -> None:
    """把浏览器自动化的运行时配置注入到平台实例。"""
    if not config:
        return
    browser_cfg = (config.get("browser_automation", {}) or {}).get(platform_name, {}) or {}
    if hasattr(platform, "debug_poll_metrics") and "debug_poll_metrics" not in browser_cfg:
        setattr(platform, "debug_poll_metrics", "")
    for key, value in browser_cfg.items():
        if not hasattr(platform, key):
            continue
        if key == "debug_poll_metrics" and isinstance(value, (bool, int, float)):
            setattr(platform, key, value)
            continue
        if not isinstance(value, str):
            continue
        normalized = value.strip()
        if not normalized:
            continue
        setattr(platform, key, normalized)


def _create_browser_platform(
    platform_name: str,
    platform_class,
    *,
    config: dict | None,
    inspect: bool,
    stop_checker=None,
):
    del platform_class
    return create_browser_platform(
        platform_name,
        config=config,
        inspect=inspect,
        stop_checker=stop_checker,
    )


def _should_use_session_pool_for_query(
    mode: str,
    task: dict,
    platform_session_manager: PlatformSessionManager | None,
) -> bool:
    normalized_mode = str(mode or "").strip()
    if platform_session_manager is None:
        return False
    if normalized_mode not in {"browser", "smart"}:
        return False
    return bool(platform_session_manager.policy.use_session_pool)


def _should_use_platform_serial_for_query(
    mode: str,
    task: dict,
    config: dict | None,
    platform_session_manager: PlatformSessionManager | None,
) -> bool:
    normalized_mode = str(mode or "").strip()
    if normalized_mode not in {"browser", "smart"}:
        return False
    if _should_use_session_pool_for_query(normalized_mode, task, platform_session_manager):
        return False
    policy = build_query_execution_policy(config or {}, normalized_mode)
    return bool(policy.use_platform_serial)


def _sync_reused_platform_runtime_state(
    platform_name: str,
    platform,
    task: dict,
    kw_entry: dict,
    config: dict | None = None,
    stop_checker=None,
) -> None:
    target_inspect = bool(task.get("inspect", False))
    current_inspect = bool(getattr(platform, "inspect", False))

    if current_inspect != target_inspect:
        if target_inspect:
            # 先按当前无头状态切到前台，再同步 inspect 标记，避免 _show_browser 误判。
            platform._show_browser()
            platform.inspect = True
        else:
            # 先取消 inspect 标记，再收回后台，确保 _hide_browser 会切回无头/最小化。
            platform.inspect = False
            platform._hide_browser()
    else:
        platform.inspect = target_inspect

    platform.stop_checker = stop_checker
    apply_browser_runtime_config(platform, platform_name, config)
    platform.screenshot_on_mention = task.get("screenshot_on_mention", False)
    platform.deep_think = kw_entry.get("deep_think", {}).get(platform_name, False)
    platform.extract_references_enabled = bool(task.get("extract_references_enabled", False))
    platform.answer_screenshot_mode = resolve_browser_answer_screenshot_mode(config)


__all__ = [
    "_apply_browser_runtime_config",
    "_create_browser_platform",
    "_should_use_platform_serial_for_query",
    "_should_use_session_pool_for_query",
    "_sync_reused_platform_runtime_state",
]
