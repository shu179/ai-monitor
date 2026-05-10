"""In-memory runtime state containers used by the Web backend.

These classes deliberately avoid HTTP handlers, browser APIs, and task
execution logic. They keep short-lived process state behind a small Python
boundary so callers do not reach into raw dictionaries for TTL cleanup and
active-run queries.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

from core.time_utils import local_now


TERMINAL_TEST_RUN_STATUSES = {"success", "failed", "cancelled"}
ACTIVE_TEST_RUN_STATUSES = {"queued", "running"}


def local_iso_seconds() -> str:
    return local_now().isoformat(timespec="seconds")


def runtime_timestamp_age_seconds(value: Any, *, now: datetime | None = None) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        timestamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    current = now or local_now()
    try:
        if timestamp.tzinfo is None:
            current = current.replace(tzinfo=None)
        elif current.tzinfo is None:
            current = local_now()
        else:
            timestamp = timestamp.astimezone(current.tzinfo)
        return max(0.0, (current - timestamp).total_seconds())
    except Exception:
        return None


class TestRunStateStore:
    """Owns async test-run state and terminal TTL pruning."""

    __test__ = False

    def __init__(
        self,
        *,
        terminal_ttl_seconds: float,
        now_func: Callable[[], datetime] = local_now,
        iso_now_func: Callable[[], str] = local_iso_seconds,
    ) -> None:
        self._terminal_ttl_seconds = max(0.0, float(terminal_ttl_seconds or 0.0))
        self._now_func = now_func
        self._iso_now_func = iso_now_func
        self._lock = threading.RLock()
        self._runs: dict[str, dict[str, Any]] = {}
        self._cancel_events: dict[str, Any] = {}

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def clear(self) -> None:
        with self._lock:
            self._runs.clear()
            self._cancel_events.clear()

    def create(self, run_id: str, state: dict[str, Any], *, cancel_event: Any = None) -> None:
        normalized = str(run_id or "").strip()
        if not normalized:
            return
        with self._lock:
            self._runs[normalized] = dict(state or {})
            if cancel_event is not None:
                self._cancel_events[normalized] = cancel_event

    def get(self, run_id: str) -> dict[str, Any]:
        normalized = str(run_id or "").strip()
        if not normalized:
            return {}
        with self._lock:
            return dict(self._runs.get(normalized) or {})

    def update(self, run_id: str, patch: dict[str, Any], *, touch: bool = True) -> bool:
        normalized = str(run_id or "").strip()
        if not normalized:
            return False
        with self._lock:
            state = self._runs.get(normalized)
            if not state:
                return False
            state.update(dict(patch or {}))
            if touch:
                state["updatedAt"] = self._iso_now_func()
            return True

    def append_poll_diagnostic(self, run_id: str, item: dict[str, Any], *, limit: int = 50) -> bool:
        normalized = str(run_id or "").strip()
        if not normalized:
            return False
        with self._lock:
            state = self._runs.get(normalized)
            if not state:
                return False
            diagnostics = list(state.get("pollDiagnostics") or [])
            diagnostics.append(dict(item or {}))
            state["pollDiagnostics"] = diagnostics[-max(1, int(limit or 1)):]
            state["updatedAt"] = self._iso_now_func()
            return True

    def get_cancel_event(self, run_id: str) -> Any:
        normalized = str(run_id or "").strip()
        if not normalized:
            return None
        with self._lock:
            return self._cancel_events.get(normalized)

    def pop_cancel_event(self, run_id: str) -> Any:
        normalized = str(run_id or "").strip()
        if not normalized:
            return None
        with self._lock:
            return self._cancel_events.pop(normalized, None)

    def prune_terminal(self, *, now: datetime | None = None) -> list[str]:
        current = now or self._now_func()
        expired: list[str] = []
        with self._lock:
            for run_id, state in list(self._runs.items()):
                status = str((state or {}).get("status") or "").strip()
                if status not in TERMINAL_TEST_RUN_STATUSES:
                    continue
                timestamp = (
                    (state or {}).get("finishedAt")
                    or (state or {}).get("updatedAt")
                    or (state or {}).get("startedAt")
                )
                age = runtime_timestamp_age_seconds(timestamp, now=current)
                if age is not None and age > self._terminal_ttl_seconds:
                    expired.append(run_id)
            for run_id in expired:
                self._runs.pop(run_id, None)
                self._cancel_events.pop(run_id, None)
        return expired

    def active_run_ids(self) -> list[str]:
        with self._lock:
            return [
                run_id
                for run_id, state in self._runs.items()
                if str((state or {}).get("status") or "").strip() in ACTIVE_TEST_RUN_STATUSES
            ]

    def has_cancelling_run(self) -> bool:
        with self._lock:
            for state in self._runs.values():
                status = str((state or {}).get("status") or "").strip()
                if bool((state or {}).get("cancelRequested")) and status not in TERMINAL_TEST_RUN_STATUSES:
                    return True
        return False

    def snapshots(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(state or {}) for state in self._runs.values()]

    def update_where(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        patch: dict[str, Any],
        *,
        touch: bool = True,
    ) -> int:
        updated = 0
        with self._lock:
            for state in self._runs.values():
                if not predicate(state):
                    continue
                state.update(dict(patch or {}))
                if touch:
                    state["updatedAt"] = self._iso_now_func()
                updated += 1
        return updated


class BrowserAuthSessionStore:
    """Owns browser-auth session state and TTL/key normalization."""

    def __init__(
        self,
        *,
        ttl_seconds: float,
        normalize_platform: Callable[[Any], str] | None = None,
        now_func: Callable[[], datetime] = local_now,
    ) -> None:
        self._ttl_seconds = max(0.0, float(ttl_seconds or 0.0))
        self._normalize_platform = normalize_platform or (lambda value: str(value or "").strip())
        self._now_func = now_func
        self._lock = threading.RLock()
        self._sessions: dict[str, dict[str, Any]] = {}

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def _key(self, platform_name: Any) -> str:
        return str(self._normalize_platform(platform_name) or "").strip()

    def set(self, platform_name: Any, session: dict[str, Any]) -> bool:
        key = self._key(platform_name)
        if not key:
            return False
        with self._lock:
            self._sessions[key] = dict(session or {})
        return True

    def get(self, platform_name: Any) -> dict[str, Any]:
        key = self._key(platform_name)
        if not key:
            return {}
        with self._lock:
            return dict(self._sessions.get(key) or {})

    def pop(self, platform_name: Any) -> dict[str, Any]:
        key = self._key(platform_name)
        if not key:
            return {}
        with self._lock:
            return self._sessions.pop(key, {})

    def list_platforms(self) -> list[str]:
        with self._lock:
            return list(self._sessions.keys())

    def sessions(self) -> list[tuple[str, dict[str, Any]]]:
        with self._lock:
            return [(platform, dict(session or {})) for platform, session in self._sessions.items()]

    def expired_platforms(self, *, now: datetime | None = None) -> list[str]:
        current = now or self._now_func()
        expired: list[str] = []
        with self._lock:
            for platform_name, session in list(self._sessions.items()):
                age = runtime_timestamp_age_seconds((session or {}).get("opened_at"), now=current)
                if age is not None and age > self._ttl_seconds:
                    expired.append(platform_name)
        return expired

    def pop_where(self, predicate: Callable[[str, dict[str, Any]], bool]) -> list[tuple[str, dict[str, Any]]]:
        removed: list[tuple[str, dict[str, Any]]] = []
        with self._lock:
            for platform_name, session in list(self._sessions.items()):
                if not predicate(platform_name, session):
                    continue
                removed.append((platform_name, self._sessions.pop(platform_name, {})))
        return removed


__all__ = [
    "ACTIVE_TEST_RUN_STATUSES",
    "BrowserAuthSessionStore",
    "TERMINAL_TEST_RUN_STATUSES",
    "TestRunStateStore",
    "local_iso_seconds",
    "runtime_timestamp_age_seconds",
]
