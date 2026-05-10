"""Throttled, safe wrappers for diagnostics recording.

All functions here never raise — they catch exceptions internally so
diagnostics failures cannot affect the main business flow.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from .diagnostics import record_event

# ---------------------------------------------------------------------------
# Throttled record_event
# ---------------------------------------------------------------------------

_throttle_lock = threading.Lock()
_last_fired: dict[str, float] = {}
_inflight: set[str] = set()


def record_event_safe(
    category: str,
    message: str,
    *,
    level: str = "warning",
    event_key: str = "",
    throttle_seconds: int = 300,
    details: dict[str, Any] | None = None,
    suggestion: str = "",
    task_name: str = "",
    platform: str = "",
    keyword: str = "",
    brand: str = "",
) -> bool:
    """Record a diagnostics event with optional per-key throttling.

    Returns True if the event was actually written, False if throttled or failed.
    Never raises.
    """
    try:
        if event_key and throttle_seconds > 0:
            now = time.monotonic()
            with _throttle_lock:
                last = _last_fired.get(event_key, 0.0)
                if now - last < throttle_seconds:
                    return False
                if event_key in _inflight:
                    return False
                _inflight.add(event_key)

        record_event(
            category,
            message,
            level=level,
            task_name=task_name,
            platform=platform,
            keyword=keyword,
            brand=brand,
            details=details,
            suggestion=suggestion,
        )

        # Success: commit timestamp and release inflight under the same lock
        if event_key and throttle_seconds > 0:
            with _throttle_lock:
                _last_fired[event_key] = time.monotonic()
                _inflight.discard(event_key)
        return True
    except Exception:
        # Failure: release inflight only (do not commit timestamp)
        if event_key and throttle_seconds > 0:
            with _throttle_lock:
                _inflight.discard(event_key)
        return False


def reset_throttle_state() -> None:
    """Clear all throttle state. For testing only."""
    with _throttle_lock:
        _last_fired.clear()
        _inflight.clear()


# ---------------------------------------------------------------------------
# Consecutive failure counter
# ---------------------------------------------------------------------------

_failure_counts: dict[str, int] = {}


def record_consecutive_failure(
    key: str,
    *,
    threshold: int = 3,
    category: str = "cloud_sync",
    message_template: str = "连续失败 {count} 次: {operation}",
    operation: str = "",
    error: str = "",
    details: dict[str, Any] | None = None,
    throttle_seconds: int = 600,
) -> int:
    """Increment failure counter for *key* and emit diagnostics at threshold.

    Returns the current consecutive failure count.
    """
    with _throttle_lock:
        _failure_counts[key] = _failure_counts.get(key, 0) + 1
        count = _failure_counts[key]

    if count >= threshold:
        merged = {"consecutive_failures": count, "operation": operation, "error": error}
        if details:
            merged.update(details)
        record_event_safe(
            category,
            message_template.format(count=count, operation=operation),
            level="error",
            event_key=f"{key}:consecutive_failures",
            throttle_seconds=throttle_seconds,
            details=merged,
        )
    return count


def clear_consecutive_failure(key: str) -> None:
    """Reset failure counter for *key* (call on success)."""
    with _throttle_lock:
        _failure_counts.pop(key, None)


def get_consecutive_failure_count(key: str) -> int:
    """Return current failure count for *key*. For testing."""
    with _throttle_lock:
        return _failure_counts.get(key, 0)


def reset_failure_counts() -> None:
    """Clear all failure counters. For testing only."""
    with _throttle_lock:
        _failure_counts.clear()
