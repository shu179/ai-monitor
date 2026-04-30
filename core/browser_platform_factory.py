"""浏览器平台实例创建与配置注入。"""

from __future__ import annotations

from typing import Any

from core.browser_auth import (
    cleanup_browser_runtime_cache,
    get_active_browser_auth_dir,
    mark_browser_auth_profile_used,
)
from platforms import (
    DoubaoPlatform,
    DeepSeekPlatform,
    KimiPlatform,
    TongyiPlatform,
    WenxinPlatform,
    YuanbaoPlatform,
)

BROWSER_PLATFORM_CLASS_MAP: dict[str, Any] = {
    "doubao": DoubaoPlatform,
    "deepseek": DeepSeekPlatform,
    "kimi": KimiPlatform,
    "yuanbao": YuanbaoPlatform,
    "tongyi": TongyiPlatform,
    "wenxin": WenxinPlatform,
    "豆包": DoubaoPlatform,
    "DeepSeek": DeepSeekPlatform,
    "Kimi": KimiPlatform,
    "元宝": YuanbaoPlatform,
    "通义千问": TongyiPlatform,
    "文心一言": WenxinPlatform,
}

BROWSER_PLATFORM_IDS: tuple[str, ...] = (
    "doubao",
    "deepseek",
    "kimi",
    "yuanbao",
    "tongyi",
    "wenxin",
)

_RUNTIME_CONFIG_BLOCKED_FIELDS: frozenset[str] = frozenset(
    {
        "target_url",
        "target_url_aliases",
        "external_chrome_launch_target_url",
        "user_data_dir",
        "context",
        "page",
        "_playwright",
        "_browser_connection",
        "_external_browser_process",
        "_external_browser_port",
    }
)


def normalize_browser_platform_name(platform_name: str) -> str:
    text = str(platform_name or "").strip()
    if not text:
        return ""
    if text in BROWSER_PLATFORM_CLASS_MAP:
        for pid in BROWSER_PLATFORM_IDS:
            if BROWSER_PLATFORM_CLASS_MAP.get(pid) is BROWSER_PLATFORM_CLASS_MAP[text]:
                return pid
    lowered = text.lower()
    if lowered in BROWSER_PLATFORM_CLASS_MAP:
        return lowered
    return text


def get_browser_platform_class(platform_name: str):
    normalized = normalize_browser_platform_name(platform_name)
    return BROWSER_PLATFORM_CLASS_MAP.get(normalized)


def apply_browser_runtime_config(platform, platform_name: str, config: dict | None) -> None:
    if not config:
        return
    browser_cfg = (config.get("browser_automation", {}) or {}).get(platform_name, {}) or {}
    for key, value in browser_cfg.items():
        if key in _RUNTIME_CONFIG_BLOCKED_FIELDS:
            continue
        if not hasattr(platform, key):
            continue
        if not isinstance(value, str):
            continue
        normalized = value.strip()
        if not normalized:
            continue
        setattr(platform, key, normalized)


def resolve_browser_answer_screenshot_mode(config: dict | None) -> str:
    screenshot_cfg = (config or {}).get("screenshot", {}) or {}
    mode = str(screenshot_cfg.get("browser_answer_mode", "page") or "page").strip().lower()
    return mode if mode in {"page", "dom"} else "page"


def create_browser_platform(
    platform_name: str,
    *,
    config: dict | None,
    inspect: bool,
    stop_checker=None,
):
    normalized = normalize_browser_platform_name(platform_name)
    platform_class = get_browser_platform_class(normalized)
    if platform_class is None:
        raise ValueError(f"不支持的平台: {platform_name}")
    user_data_dir = get_active_browser_auth_dir(normalized)
    cleanup_browser_runtime_cache(user_data_dir)
    mark_browser_auth_profile_used(normalized)
    platform = platform_class(str(user_data_dir))
    apply_browser_runtime_config(platform, normalized, config)
    platform.answer_screenshot_mode = resolve_browser_answer_screenshot_mode(config)
    platform.inspect = bool(inspect)
    platform.stop_checker = stop_checker
    return platform
