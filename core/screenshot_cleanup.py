"""Screenshot cleanup: age-based and size-based deletion of stale screenshots.

Safe to import on any platform. Call ``cleanup_screenshots()`` once at startup.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .app_paths import resolve_app_path

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}

DEFAULT_SCREENSHOT_CLEANUP_CONFIG: dict[str, Any] = {
    "auto_cleanup": True,
    "retention_days": 14,
    "max_size_mb": 1024,
    "include_recognition": True,
}

_MIN_RETENTION_DAYS = 1
_MIN_MAX_SIZE_MB = 50
_SECONDS_PER_DAY = 86400


def cleanup_screenshots(
    config: dict[str, Any] | None = None,
    *,
    screenshots_dir: Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Clean up old screenshots based on age and total size.

    Parameters
    ----------
    config : dict, optional
        Application config. Reads ``config["screenshot"]`` for cleanup settings.
    screenshots_dir : Path, optional
        Override screenshots directory. Defaults to ``resolve_app_path("screenshots")``.
    now : float, optional
        Current Unix timestamp for testing. Defaults to ``time.time()``.

    Returns
    -------
    dict
        Summary with keys: enabled, directory, deleted, deleted_bytes, kept,
        scanned, reason_counts, errors.
    """
    cfg = _parse_config(config)
    result: dict[str, Any] = {
        "enabled": cfg["auto_cleanup"],
        "directory": "",
        "deleted": 0,
        "deleted_bytes": 0,
        "kept": 0,
        "scanned": 0,
        "reason_counts": {"age": 0, "size": 0},
        "errors": [],
    }

    if not cfg["auto_cleanup"]:
        return result

    base_dir = screenshots_dir or resolve_app_path("screenshots")
    result["directory"] = str(base_dir)

    if not base_dir.exists():
        return result

    current_time = now if now is not None else time.time()
    retention_seconds = max(_MIN_RETENTION_DAYS, cfg["retention_days"]) * _SECONDS_PER_DAY
    max_bytes = max(_MIN_MAX_SIZE_MB, cfg["max_size_mb"]) * 1024 * 1024

    candidates = _scan_candidates(base_dir, cfg["include_recognition"])
    result["scanned"] = len(candidates)

    # Phase 1: age-based cleanup
    age_cutoff = current_time - retention_seconds
    remaining: list[tuple[Path, int, float]] = []
    for path, size, mtime in candidates:
        if mtime < age_cutoff:
            if _try_delete(path, result, size, "age"):
                continue
        remaining.append((path, size, mtime))

    # Phase 2: size-based cleanup (oldest first)
    remaining.sort(key=lambda x: x[2])  # sort by mtime ascending
    total_bytes = sum(size for _, size, _ in remaining)
    while total_bytes > max_bytes and remaining:
        path, size, mtime = remaining.pop(0)
        if _try_delete(path, result, size, "size"):
            total_bytes -= size

    result["kept"] = result["scanned"] - result["deleted"]
    return result


def cleanup_screenshots_from_default_config() -> dict[str, Any]:
    """Convenience wrapper that loads config.yaml and runs cleanup.

    Falls back to default cleanup config if config loading fails.
    """
    try:
        from .config_watcher import load_config
        config = load_config("config.yaml")
    except Exception:
        logger.debug("Failed to load config.yaml for screenshot cleanup, using defaults", exc_info=True)
        config = {}
    return cleanup_screenshots(config)


def _parse_config(config: dict[str, Any] | None) -> dict[str, Any]:
    raw = (config or {}).get("screenshot", {})
    if not isinstance(raw, dict):
        raw = {}
    return {
        "auto_cleanup": bool(raw.get("auto_cleanup", DEFAULT_SCREENSHOT_CLEANUP_CONFIG["auto_cleanup"])),
        "retention_days": _coerce_int(raw.get("retention_days"), DEFAULT_SCREENSHOT_CLEANUP_CONFIG["retention_days"]),
        "max_size_mb": _coerce_int(raw.get("max_size_mb"), DEFAULT_SCREENSHOT_CLEANUP_CONFIG["max_size_mb"]),
        "include_recognition": bool(raw.get("include_recognition", DEFAULT_SCREENSHOT_CLEANUP_CONFIG["include_recognition"])),
    }


def _coerce_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _scan_candidates(base_dir: Path, include_recognition: bool) -> list[tuple[Path, int, float]]:
    """Scan for image files, returning (path, size_bytes, mtime) tuples."""
    candidates: list[tuple[Path, int, float]] = []
    try:
        resolved_base = base_dir.resolve()
    except Exception:
        resolved_base = base_dir

    for path in base_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.is_symlink():
            continue
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        # Skip files outside the base directory (safety check)
        try:
            if not path.resolve().is_relative_to(resolved_base):
                continue
        except (ValueError, OSError):
            continue
        # Skip recognition subdir if not included
        if not include_recognition:
            try:
                rel = path.relative_to(base_dir)
                if rel.parts and rel.parts[0] == "recognition":
                    continue
            except ValueError:
                continue
        try:
            stat = path.stat()
            candidates.append((path, stat.st_size, stat.st_mtime))
        except OSError:
            continue
    return candidates


def _try_delete(path: Path, result: dict[str, Any], size: int, reason: str) -> bool:
    """Try to delete a file. Returns True on success."""
    try:
        path.unlink()
        result["deleted"] += 1
        result["deleted_bytes"] += size
        result["reason_counts"][reason] = result["reason_counts"].get(reason, 0) + 1
        logger.debug("Deleted screenshot: %s (reason=%s, size=%d)", path.name, reason, size)
        return True
    except Exception as exc:
        result["errors"].append({"path": str(path), "error": str(exc)})
        logger.warning("Failed to delete screenshot %s: %s", path, exc)
        return False
