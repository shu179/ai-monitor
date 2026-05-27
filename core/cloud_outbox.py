from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .app_paths import resolve_app_path
from .file_lock import CrossProcessRLock
from .local_account_space import account_profile_dir_from_session, account_scoped_path
from .time_utils import local_now

DEFAULT_CLOUD_OUTBOX_PATH = resolve_app_path("user_data/cloud_outbox.json")
DEFAULT_MAX_ITEMS = 10_000
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_SENT_ITEMS = 1_000

DEFAULT_MAX_ATTEMPTS = 5
_RETRY_BACKOFF_SECONDS = (60, 300, 1800, 7200)


def _backoff_delay(attempts: int) -> float:
    """Return backoff delay in seconds for the given attempt count (1-indexed)."""
    if attempts <= 0:
        return 0.0
    idx = min(attempts - 1, len(_RETRY_BACKOFF_SECONDS) - 1)
    return float(_RETRY_BACKOFF_SECONDS[idx])


class CloudOutbox:
    _change_event = threading.Event()

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        max_items: int | None = None,
        max_bytes: int | None = None,
        max_sent_items: int | None = None,
    ) -> None:
        self._explicit_path = Path(path) if path is not None else None
        self._lock = CrossProcessRLock(lambda: self._lock_path())
        self._max_items = max(1, int(max_items or DEFAULT_MAX_ITEMS))
        self._max_bytes = max(1024, int(max_bytes or DEFAULT_MAX_BYTES))
        self._max_sent_items = max(0, int(DEFAULT_MAX_SENT_ITEMS if max_sent_items is None else max_sent_items))

    @property
    def path(self) -> Path:
        if self._explicit_path is not None:
            return self._explicit_path
        return account_scoped_path("user_data/cloud_outbox.json", fallback=DEFAULT_CLOUD_OUTBOX_PATH)

    @staticmethod
    def path_for_session(session: dict[str, Any] | None) -> Path:
        profile_dir = account_profile_dir_from_session(session)
        if profile_dir is None:
            return DEFAULT_CLOUD_OUTBOX_PATH
        return profile_dir / "user_data/cloud_outbox.json"

    def _lock_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.lock")

    def bind_to_session(self, session: dict[str, Any] | None) -> "CloudOutbox":
        if self._explicit_path is not None:
            return self
        return CloudOutbox(
            self.path_for_session(session),
            max_items=self._max_items,
            max_bytes=self._max_bytes,
            max_sent_items=self._max_sent_items,
        )

    @classmethod
    def notify_changed(cls) -> None:
        cls._change_event.set()

    @classmethod
    def wait_for_change(cls, timeout: float) -> bool:
        changed = cls._change_event.wait(max(0.0, float(timeout or 0.0)))
        if changed:
            cls._change_event.clear()
        return changed

    def enqueue(self, *, event_type: str, idempotency_key: str, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        event_type = str(event_type or "").strip()
        idempotency_key = str(idempotency_key or "").strip()
        if not event_type or not idempotency_key:
            raise ValueError("event_type and idempotency_key are required")
        with self._lock:
            items = self._load_locked()
            for item in items:
                if str(item.get("idempotency_key") or "") == idempotency_key:
                    return dict(item), False
            now = local_now().isoformat(timespec="seconds")
            item = {
                "event_type": event_type,
                "idempotency_key": idempotency_key,
                "payload": dict(payload or {}),
                "status": "pending",
                "attempts": 0,
                "created_at": now,
                "updated_at": now,
                "last_error": "",
            }
            items.append(item)
            self._save_locked(items)
            self.notify_changed()
            return dict(item), True

    def enqueue_many(self, events: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> dict[str, Any]:
        """Enqueue multiple events with one read-modify-write of the outbox file."""
        normalized_events: list[dict[str, Any]] = []
        for event in events or []:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("event_type") or "").strip()
            idempotency_key = str(event.get("idempotency_key") or "").strip()
            if not event_type or not idempotency_key:
                raise ValueError("event_type and idempotency_key are required")
            normalized_events.append({
                "event_type": event_type,
                "idempotency_key": idempotency_key,
                "payload": dict(event.get("payload") or {}),
            })
        if not normalized_events:
            return {"items": [], "created": 0, "requested": 0, "dropped": _empty_dropped()}

        with self._lock:
            items = self._load_locked()
            by_key = {
                str(item.get("idempotency_key") or ""): item
                for item in items
                if str(item.get("idempotency_key") or "").strip()
            }
            queued_items: list[dict[str, Any]] = []
            created = 0
            now = local_now().isoformat(timespec="seconds")
            for event in normalized_events:
                existing = by_key.get(event["idempotency_key"])
                if existing is not None:
                    queued_items.append(dict(existing))
                    continue
                item = {
                    "event_type": event["event_type"],
                    "idempotency_key": event["idempotency_key"],
                    "payload": dict(event.get("payload") or {}),
                    "status": "pending",
                    "attempts": 0,
                    "created_at": now,
                    "updated_at": now,
                    "last_error": "",
                }
                items.append(item)
                by_key[event["idempotency_key"]] = item
                queued_items.append(dict(item))
                created += 1
            dropped = self._save_locked(items) if created else _empty_dropped()
            if created:
                self.notify_changed()
            return {
                "items": queued_items,
                "created": created,
                "requested": len(normalized_events),
                "dropped": dropped,
            }

    def pending(self, *, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = min(max(int(limit or 100), 1), 500)
        now = time.time()
        with self._lock:
            items = self._load_locked()
        pending_items = []
        for item in items:
            status = str(item.get("status") or "pending")
            if status == "pending":
                pending_items.append(dict(item))
            elif status == "failed":
                if _is_retry_ready(item, now):
                    pending_items.append(dict(item))
        return pending_items[:safe_limit]

    def mark_sent(self, idempotency_keys: list[str] | set[str] | tuple[str, ...]) -> None:
        keys = {str(key or "").strip() for key in idempotency_keys if str(key or "").strip()}
        if not keys:
            return
        now = local_now().isoformat(timespec="seconds")
        with self._lock:
            items = self._load_locked()
            for item in items:
                if str(item.get("idempotency_key") or "") in keys:
                    item["status"] = "sent"
                    item["updated_at"] = now
                    item["last_error"] = ""
            self._save_locked(items)

    def mark_failed(
        self,
        idempotency_keys: list[str] | set[str] | tuple[str, ...],
        message: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        keys = {str(key or "").strip() for key in idempotency_keys if str(key or "").strip()}
        if not keys:
            return
        now_iso = local_now().isoformat(timespec="seconds")
        now_ts = time.time()
        dead_letter_events: list[dict[str, Any]] = []
        with self._lock:
            items = self._load_locked()
            for item in items:
                if str(item.get("idempotency_key") or "") not in keys:
                    continue
                if _outbox_status(item) == "sent":
                    continue
                attempts = int(item.get("attempts") or 0) + 1
                item["attempts"] = attempts
                item["updated_at"] = now_iso
                item["last_error"] = str(message or "上传失败").strip()
                if attempts >= DEFAULT_MAX_ATTEMPTS:
                    item["status"] = "dead_letter"
                    item["dead_lettered_at"] = now_iso
                    item["dead_letter_reason"] = item["last_error"]
                    dead_letter_events.append(dict(item))
                else:
                    item["status"] = "failed"
                    next_ts = now_ts + _failure_retry_delay(attempts, retry_after_seconds=retry_after_seconds)
                    item["next_attempt_ts"] = next_ts
                    item["next_attempt_at"] = datetime.fromtimestamp(next_ts, tz=timezone.utc).isoformat(timespec="seconds")
            self._save_locked(items)
        for evt in dead_letter_events:
            try:
                from .diagnostics import record_event
                record_event(
                    "cloud_sync",
                    f"事件达到最大重试次数，进入 dead-letter: {evt.get('idempotency_key', '')}",
                    level="warning",
                    details={
                        "idempotency_key": evt.get("idempotency_key", ""),
                        "event_type": evt.get("event_type", ""),
                        "attempts": evt.get("attempts", 0),
                        "last_error": evt.get("last_error", ""),
                    },
                )
            except Exception:
                pass

    def stats(self) -> dict[str, int]:
        with self._lock:
            items = self._load_locked()
        stats: dict[str, int] = {"total": len(items), "pending": 0, "failed": 0, "sent": 0, "dead_letter": 0}
        for item in items:
            status = str(item.get("status") or "pending")
            if status in stats:
                stats[status] += 1
        return stats

    def _load_locked(self) -> list[dict[str, Any]]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return []
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def _save_locked(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        compacted_items, dropped = self._compact_items(items)
        serialized = json.dumps(compacted_items, ensure_ascii=False, indent=2, sort_keys=True)
        encoded_size = len(serialized.encode("utf-8"))
        protected_count = sum(1 for item in compacted_items if _outbox_status(item) != "sent")
        overflow = protected_count > 0 and (len(compacted_items) > self._max_items or encoded_size > self._max_bytes)
        dropped["overflow"] = overflow
        dropped["overflow_items"] = max(0, len(compacted_items) - self._max_items) if overflow else 0
        dropped["overflow_bytes"] = max(0, encoded_size - self._max_bytes) if overflow else 0
        dropped["active_retained"] = protected_count if overflow else 0
        if overflow:
            try:
                from .diagnostics import record_event
                record_event(
                    "cloud_sync",
                    "本地 Outbox 队列超过容量上限，已保留所有未发送事件",
                    level="warning",
                    details={
                        "path": str(self.path),
                        "total_items": len(compacted_items),
                        "active_retained": protected_count,
                        "overflow_items": dropped["overflow_items"],
                        "overflow_bytes": dropped["overflow_bytes"],
                    },
                )
            except Exception:
                pass
        fd, tmp_path = tempfile.mkstemp(dir=str(self.path.parent), prefix=".cloud_outbox_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(serialized)
            os.replace(tmp_path, self.path)
            return dropped
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _compact_items(self, items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
        compacted = [dict(item) for item in items if isinstance(item, dict)]
        dropped: dict[str, int] = {"total": 0, "active": 0, "sent": 0, "dead_letter": 0}

        def _add_dropped(total: int, active: int) -> None:
            dropped["total"] += int(total or 0)
            dropped["active"] += int(active or 0)
            dropped["sent"] = max(0, dropped["total"] - dropped["active"])

        sent_count = sum(1 for item in compacted if _outbox_status(item) == "sent")
        if sent_count > self._max_sent_items:
            total, active = self._drop_oldest_by_count(
                compacted,
                sent_count - self._max_sent_items,
                lambda item: _outbox_status(item) == "sent",
            )
            _add_dropped(total, active)

        if len(compacted) > self._max_items:
            total, active = self._drop_oldest_by_count(
                compacted,
                len(compacted) - self._max_items,
                lambda item: _outbox_status(item) == "sent",
            )
            _add_dropped(total, active)

        # Only drop sent items for bytes limit — never active/dead_letter
        if self._encoded_size(compacted) > self._max_bytes:
            total, active = self._drop_oldest_until_size(
                compacted,
                lambda item: _outbox_status(item) == "sent",
            )
            _add_dropped(total, active)

        # If still over limits, all remaining are active — keep them all (overflow)
        return compacted, dropped

    @staticmethod
    def _encoded_size(items: list[dict[str, Any]]) -> int:
        return len(json.dumps(items, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))

    @staticmethod
    def _drop_oldest_by_count(items: list[dict[str, Any]], count: int, predicate) -> tuple[int, int]:
        if count <= 0 or not items:
            return 0, 0
        indexes = [
            index
            for _, index in sorted(
                (_outbox_sort_key(item), index)
                for index, item in enumerate(items)
                if predicate(item)
            )
        ][:count]
        active = sum(1 for index in indexes if _outbox_status(items[index]) != "sent")
        for index in sorted(indexes, reverse=True):
            items.pop(index)
        return len(indexes), active

    def _drop_oldest_until_size(self, items: list[dict[str, Any]], predicate) -> tuple[int, int]:
        dropped = 0
        active = 0
        while len(items) > 1 and self._encoded_size(items) > self._max_bytes:
            index = self._oldest_index(items, predicate)
            if index is None:
                break
            item = items.pop(index)
            dropped += 1
            if _outbox_status(item) != "sent":
                active += 1
        return dropped, active

    @staticmethod
    def _oldest_index(items: list[dict[str, Any]], predicate) -> int | None:
        candidates: list[tuple[str, int]] = []
        for index, item in enumerate(items):
            try:
                if predicate(item):
                    candidates.append((_outbox_sort_key(item), index))
            except Exception:
                continue
        if not candidates:
            return None
        return min(candidates)[1]


def _outbox_status(item: dict[str, Any]) -> str:
    return str(item.get("status") or "pending").strip() or "pending"


def _is_retry_ready(item: dict[str, Any], now: float) -> bool:
    """Check if a failed item is ready for retry. Treats bad/missing next_attempt_ts as ready."""
    next_ts = item.get("next_attempt_ts")
    if next_ts is None:
        return True
    try:
        return float(next_ts) <= now
    except (TypeError, ValueError):
        return True


def _empty_dropped() -> dict[str, Any]:
    return {"total": 0, "active": 0, "sent": 0, "dead_letter": 0, "overflow": False, "overflow_items": 0, "overflow_bytes": 0, "active_retained": 0}


def _failure_retry_delay(attempts: int, *, retry_after_seconds: float | None = None) -> float:
    try:
        if retry_after_seconds is not None:
            return max(0.0, float(retry_after_seconds))
    except Exception:
        pass
    return _backoff_delay(attempts)


def _outbox_sort_key(item: dict[str, Any]) -> str:
    return str(item.get("created_at") or item.get("updated_at") or "").strip()
