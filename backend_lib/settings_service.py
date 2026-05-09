"""Settings projection service for the local web backend."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from core.account_crawler import normalize_account_crawling_settings
from core.local_model_manager import get_local_model_manager
from core.platform_sessions import (
    normalize_query_execution_strategy,
    normalize_session_pool_dispatch,
)
from core.screenshot_tools import get_decoration_theme
from core.update_manager import build_update_status, get_app_update_settings
from core.version import get_version_payload


def _screenshot_theme_to_api(theme: dict[str, Any]) -> dict[str, Any]:
    layout = theme.get("layout", {}) or {}
    background = theme.get("background", {}) or {}
    header = theme.get("header", {}) or {}
    return {
        "enabled": bool(theme.get("enabled", True)),
        "title": str(theme.get("title", "{platform}") or "{platform}"),
        "subtitle": str(theme.get("subtitle", "{brand}") or "{brand}"),
        "footer": str(theme.get("footer", "MONITOR SNAPSHOT") or "MONITOR SNAPSHOT"),
        "show_time": bool(theme.get("show_timestamp", True)),
        "show_footer": bool(theme.get("show_footer", True)),
        "show_highlight": bool(theme.get("draw_highlight_boxes", True)),
        "accent_color": str(theme.get("accent_color", "#14C7F3") or "#14C7F3"),
        "background_start": str(background.get("start", "#FCFDFF") or "#FCFDFF"),
        "background_end": str(background.get("end", "#F7FAFF") or "#F7FAFF"),
        "header_start": str(header.get("start", "#173A43") or "#173A43"),
        "header_end": str(header.get("end", "#14C7F3") or "#14C7F3"),
        "outer_padding": int(layout.get("outer_padding", 28) or 28),
        "header_height": int(layout.get("header_height", 152) or 152),
        "radius": int(layout.get("radius", 28) or 28),
        "image_radius": int(layout.get("image_radius", 22) or 22),
    }


def _normalize_storage_settings(config: dict[str, Any]) -> dict[str, Any]:
    storage_cfg = config.get("storage", {}) if isinstance(config, dict) else {}
    if not isinstance(storage_cfg, dict):
        storage_cfg = {}
    return {
        "history_read_backend": str(storage_cfg.get("history_read_backend") or "").strip().lower(),
        "history_shadow_writes_enabled": bool(storage_cfg.get("history_shadow_writes_enabled", False)),
    }


class SettingsService:
    """Read-only settings projection for the frontend API."""

    def __init__(
        self,
        *,
        context_snapshot_loader: Callable[[], tuple[dict, dict[str, Any]]],
        browser_auth_loader: Callable[[], dict[str, Any]],
        public_profile_builder: Callable[[dict[str, Any]], dict[str, Any]],
        cloud_sync_status_getter: Callable[[], dict[str, Any]],
        secret_masker: Callable[[Any], str],
    ) -> None:
        self._context_snapshot_loader = context_snapshot_loader
        self._browser_auth_loader = browser_auth_loader
        self._public_profile_builder = public_profile_builder
        self._cloud_sync_status_getter = cloud_sync_status_getter
        self._mask_secret = secret_masker

    def get_settings(self) -> dict:
        """Return current settings with sensitive values masked."""
        config, _ = self._context_snapshot_loader()
        search_cfg = dict(config.get("search", {}) or {})
        cloud_sync_cfg = dict(config.get("cloud_sync", {}) or {})
        query_execution_cfg = copy.deepcopy(config.get("query_execution", {}) or {})
        for mode in ("browser", "smart"):
            mode_cfg = query_execution_cfg.get(mode)
            if not isinstance(mode_cfg, dict):
                continue
            mode_cfg["strategy"] = normalize_query_execution_strategy(
                mode_cfg.get("strategy"),
                default="platform_serial",
            )
            mode_cfg["session_pool_dispatch"] = normalize_session_pool_dispatch(
                mode_cfg.get("session_pool_dispatch"),
                default="platform_batch",
            )
        screenshot_theme = get_decoration_theme(config)
        scheduler_cfg = dict(config.get("scheduler", {}) or {})
        scheduler_cfg["notification_webhook_url"] = self._mask_secret(
            scheduler_cfg.get("notification_webhook_url", "")
        )
        search_cfg["tavily_api_key"] = self._mask_secret(search_cfg.get("tavily_api_key", ""))
        cloud_sync_cfg["api_token"] = self._mask_secret(cloud_sync_cfg.get("api_token", ""))
        ai_assistant_cfg = dict(config.get("ai_assistant", {}) or {})
        selector_agent_cfg = dict(config.get("selector_agent", {}) or {})
        selector_agent_cfg["enabled"] = bool(selector_agent_cfg.get("enabled", False))
        selector_agent_platform = str(selector_agent_cfg.get("platform", "") or "").strip()
        if not selector_agent_platform:
            selector_agent_platform = str(ai_assistant_cfg.get("platform", "") or "").strip()
        selector_agent_model = str(selector_agent_cfg.get("model", "") or "").strip()
        if not selector_agent_model:
            selector_agent_model = str(ai_assistant_cfg.get("model", "") or "").strip()
        selector_agent_cfg["platform"] = selector_agent_platform
        selector_agent_cfg["model"] = selector_agent_model
        version_info = get_version_payload()
        return {
            "version": version_info,
            "scheduler": scheduler_cfg,
            "ai_assistant": ai_assistant_cfg,
            "local_model": config.get("local_model", {}),
            "local_model_status": get_local_model_manager().get_status(),
            "recognition": config.get("recognition", {}),
            "smart_vision": config.get("smart_vision", {}),
            "selector_agent": selector_agent_cfg,
            "cloud_sync": cloud_sync_cfg,
            "query_execution": query_execution_cfg,
            "browser_automation": config.get("browser_automation", {}),
            "browser_auth": self._browser_auth_loader().get("platforms", {}),
            "context_snapshots": config.get("context_snapshots", {}),
            "weather_snapshot": config.get("weather_snapshot", {}),
            "calendar_snapshot": config.get("calendar_snapshot", {}),
            "search": search_cfg,
            "article_export": {
                "show_keyword_category": bool(
                    (config.get("article_export", {}) or {}).get("show_keyword_category", False)
                ),
                "show_selfmedia_account": bool(
                    (config.get("article_export", {}) or {}).get("show_selfmedia_account", True)
                ),
            },
            "storage": _normalize_storage_settings(config),
            "tavily": {
                "api_key": self._mask_secret(search_cfg.get("tavily_api_key", "")),
            },
            "screenshot_template": _screenshot_theme_to_api(screenshot_theme),
            "screenshot": {
                "browser_answer_mode": str((config.get("screenshot", {}) or {}).get("browser_answer_mode", "page") or "page").strip().lower(),
                "decoration": screenshot_theme,
            },
            "profile": self._public_profile_builder(config),
            "account_crawling": normalize_account_crawling_settings(config, include_state=True),
            "default_notification": config.get("default_notification", {}),
            "detection_mode": config.get("detection_mode", "browser"),
            "app_update": get_app_update_settings(config),
            "update_status": build_update_status(config, include_check=False),
            "cloud_sync_status": self._cloud_sync_status_getter(),
        }
