from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from .app_paths import resolve_app_path
from .local_account_space import account_profile_dir_from_session, account_scoped_path
from .time_utils import local_now

DEFAULT_CLOUD_OUTBOX_PATH = resolve_app_path("user_data/cloud_outbox.json")


class CloudOutbox:
    _change_event = threading.Event()

    def __init__(self, path: str | Path | None = None) -> None:
        self._explicit_path = Path(path) if path is not None else None
        self._lock = threading.RLock()

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

    def bind_to_session(self, session: dict[str, Any] | None) -> "CloudOutbox":
        if self._explicit_path is not None:
            return self
        return CloudOutbox(self.path_for_session(session))

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

    def pending(self, *, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = min(max(int(limit or 100), 1), 500)
        with self._lock:
            items = self._load_locked()
        pending_items = [
            dict(item)
            for item in items
            if str(item.get("status") or "pending") in {"pending", "failed"}
        ]
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

    def mark_failed(self, idempotency_keys: list[str] | set[str] | tuple[str, ...], message: str) -> None:
        keys = {str(key or "").strip() for key in idempotency_keys if str(key or "").strip()}
        if not keys:
            return
        now = local_now().isoformat(timespec="seconds")
        with self._lock:
            items = self._load_locked()
            for item in items:
                if str(item.get("idempotency_key") or "") in keys:
                    item["status"] = "failed"
                    item["updated_at"] = now
                    item["last_error"] = str(message or "上传失败").strip()
                    item["attempts"] = int(item.get("attempts") or 0) + 1
            self._save_locked(items)

    def stats(self) -> dict[str, int]:
        with self._lock:
            items = self._load_locked()
        stats = {"total": len(items), "pending": 0, "failed": 0, "sent": 0}
        for item in items:
            status = str(item.get("status") or "pending")
            if status not in stats:
                continue
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

    def _save_locked(self, items: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(self.path.parent), prefix=".cloud_outbox_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(items, handle, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
