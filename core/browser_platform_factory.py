"""浏览器平台实例创建与配置注入。"""

from __future__ import annotations

from typing import Any

from core.browser_auth import (
    build_browser_runtime_diagnostics,
    cleanup_browser_runtime_cache,
    get_active_browser_auth_dir,
    mark_browser_auth_profile_runtime_binding,
    mark_browser_auth_profile_used,
)
from core.local_account_space import current_account_profile_dir
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
    if hasattr(platform, "debug_poll_metrics") and "debug_poll_metrics" not in browser_cfg:
        setattr(platform, "debug_poll_metrics", "")
    for key, value in browser_cfg.items():
        if key in _RUNTIME_CONFIG_BLOCKED_FIELDS:
            continue
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


def _redact_proxy_server(value: str) -> str:
    text = str(value or "").strip()
    if not text or "@" not in text:
        return text
    prefix, suffix = text.rsplit("@", 1)
    scheme = ""
    if "://" in prefix:
        scheme = prefix.split("://", 1)[0] + "://"
    return f"{scheme}<credentials>@{suffix}"


def _runtime_binding_for_platform(platform) -> dict[str, Any]:
    account_dir = current_account_profile_dir()
    return {
        "account_profile_dir": str(account_dir or ""),
        "user_data_dir": str(getattr(platform, "user_data_dir", "") or ""),
        "proxy_server": _redact_proxy_server(getattr(platform, "browser_proxy_server", "")),
        "has_proxy_username": bool(str(getattr(platform, "browser_proxy_username", "") or "").strip()),
        "use_external_chrome_cdp": bool(getattr(platform, "use_external_chrome_cdp", False)),
        "external_chrome_light_control": bool(getattr(platform, "external_chrome_light_control", True)),
        "browser_locale": str(getattr(platform, "browser_locale", "") or ""),
        "browser_timezone_id": str(getattr(platform, "browser_timezone_id", "") or ""),
    }


def _log_runtime_identity_diagnostics(platform_name: str, binding: dict[str, Any]) -> None:
    try:
        proxy = str(binding.get("proxy_server") or "").strip() or "直连"
        account_dir = str(binding.get("account_profile_dir") or "").strip() or "未绑定"
        user_data_dir = str(binding.get("user_data_dir") or "").strip() or "未知"
        cdp_mode = "external_cdp_port" if bool(binding.get("use_external_chrome_cdp")) else "persistent_context"
        light_control = "light" if bool(binding.get("external_chrome_light_control")) else "full"
        print(
            f"[{platform_name}] 账号/Profile/IP诊断: "
            f"account_dir={account_dir}, user_data_dir={user_data_dir}, "
            f"proxy={proxy}, browser_mode={cdp_mode}/{light_control}"
        )
        diagnostics = build_browser_runtime_diagnostics()
        issues = [
            issue for issue in diagnostics.get("issues", [])
            if platform_name in (issue.get("platforms") or [])
        ]
        for issue in issues[:3]:
            print(
                f"[{platform_name}] 账号/Profile/IP风险: "
                f"{issue.get('severity')} {issue.get('title')} - {issue.get('message')}"
            )
    except Exception:
        pass


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
    try:
        binding = _runtime_binding_for_platform(platform)
        mark_browser_auth_profile_runtime_binding(normalized, binding)
        _log_runtime_identity_diagnostics(normalized, binding)
    except Exception:
        pass
    platform.answer_screenshot_mode = resolve_browser_answer_screenshot_mode(config)
    platform.inspect = bool(inspect)
    platform.stop_checker = stop_checker
    return platform
