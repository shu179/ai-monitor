"""Stable fingerprints for browser automation selector config."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _normalized_config_hash(value: Any) -> str:
    try:
        payload = json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except TypeError:
        payload = json.dumps(str(value or ""), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def browser_automation_hash(config: dict[str, Any] | None) -> str:
    """Return a stable hash for the full browser_automation config tree."""
    return _normalized_config_hash((config or {}).get("browser_automation", {}) or {})


def platform_browser_automation_hash(config: dict[str, Any] | None, platform_name: str) -> str:
    """Return a stable hash for one platform's browser automation config."""
    platform = str(platform_name or "").strip()
    browser_cfg = (config or {}).get("browser_automation", {}) or {}
    platform_cfg = browser_cfg.get(platform, {}) if isinstance(browser_cfg, dict) else {}
    return _normalized_config_hash(platform_cfg or {})
