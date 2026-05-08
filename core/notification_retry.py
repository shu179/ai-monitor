"""Persistent retry queue for notification sends."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from .app_paths import resolve_app_path
from .file_lock import CrossProcessRLock
from .notification_idempotency import (
    build_payload_hash,
    record_notification_sent,
)
from .notifier import WeComNotifier, is_valid_wecom_webhook


DEFAULT_INITIAL_DELAY_SECONDS = 60
DEFAULT_BACKOFF_SECONDS = 60
MAX_BACKOFF_SECONDS = 30 * 60
DEFAULT_MAX_ATTEMPTS = 10
DEFAULT_WORKER_INTERVAL_SECONDS = 60
DEFAULT_WORKER_LIMIT_PER_TICK = 1

_LOCK = CrossProcessRLock(lambda: _retry_queue_lock_path())
_ACTIVE_RETRY_IDS: set[str] = set()
_WORKER_LOCK = threading.Lock()
_WORKER: "NotificationRetryWorker | None" = None


def get_retry_queue_path() -> Path:
    return resolve_app_path("user_data/notification_retry_queue.json")


def _retry_queue_lock_path() -> Path:
    path = get_retry_queue_path()
    return path.with_name(f"{path.name}.lock")


def load_retry_queue(*, include_inactive: bool = False) -> list[dict]:
    """Return queued retry entries in creation order."""
    with _LOCK:
        entries = _load_queue_unlocked()
    if include_inactive:
        return entries
    return [entry for entry in entries if str(entry.get("status") or "pending") == "pending"]


def clear_retry_queue() -> None:
    """Clear the retry queue. Intended for tests and explicit maintenance."""
    with _LOCK:
        _ACTIVE_RETRY_IDS.clear()
        _save_queue_unlocked([])


def enqueue_wecom_notification(
    payload: dict,
    *,
    initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS,
    now: float | None = None,
) -> dict:
    """Persist a WeCom notification payload for later retry."""
    entry = _normalize_payload(payload, now=now, initial_delay_seconds=initial_delay_seconds)
    if not entry:
        return {}

    with _LOCK:
        entries = _load_queue_unlocked()
        for index, existing in enumerate(entries):
            if str(existing.get("dedupe_key") or "") != entry["dedupe_key"]:
                continue
            preserved_attempts = int(existing.get("attempt_count") or 0)
            if str(existing.get("status") or "pending") == "pending":
                entry["id"] = str(existing.get("id") or entry["id"])
                entry["created_at"] = str(existing.get("created_at") or entry["created_at"])
                entry["created_ts"] = float(existing.get("created_ts") or entry["created_ts"])
                entry["attempt_count"] = preserved_attempts
                entry["last_error"] = str(existing.get("last_error") or "")
                old_next = float(existing.get("next_attempt_ts") or 0)
                if old_next > 0:
                    entry["next_attempt_ts"] = min(float(entry["next_attempt_ts"]), old_next)
                    entry["next_attempt_at"] = _format_ts(float(entry["next_attempt_ts"]))
            entries[index] = entry
            _save_queue_unlocked(entries)
            print(
                f"[NotificationRetry] 已更新补发队列: "
                f"id={entry['id']}, task={entry.get('task_name', '')}"
            )
            return copy.deepcopy(entry)

        entries.append(entry)
        _save_queue_unlocked(entries)
    print(
        f"[NotificationRetry] 已加入补发队列: "
        f"id={entry['id']}, task={entry.get('task_name', '')}, "
        f"next={entry.get('next_attempt_at', '')}"
    )
    return copy.deepcopy(entry)


def retry_due_notifications(
    *,
    limit: int = DEFAULT_WORKER_LIMIT_PER_TICK,
    notifier_factory: Callable[..., WeComNotifier] | None = None,
    now: float | None = None,
    pause_callback: Callable[[], bool] | None = None,
) -> dict:
    """Retry due notifications without rerunning capture/OCR work."""
    notifier_factory = notifier_factory or WeComNotifier
    now_ts = float(time.time() if now is None else now)
    selected = _select_due_entries(limit=max(1, int(limit or 1)), now=now_ts)
    stats = {"attempted": 0, "succeeded": 0, "failed": 0, "skipped": 0, "disabled": 0}

    for entry in selected:
        if pause_callback and pause_callback():
            stats["skipped"] += 1
            _release_active(entry)
            continue
        stats["attempted"] += 1
        try:
            result = _retry_one(entry, notifier_factory=notifier_factory, now=now_ts)
        finally:
            _release_active(entry)

        if result == "success":
            stats["succeeded"] += 1
        elif result == "disabled":
            stats["disabled"] += 1
        else:
            stats["failed"] += 1
    return stats


class NotificationRetryWorker:
    """Small daemon worker that drains the persisted retry queue opportunistically."""

    def __init__(
        self,
        *,
        interval_seconds: float = DEFAULT_WORKER_INTERVAL_SECONDS,
        limit_per_tick: int = DEFAULT_WORKER_LIMIT_PER_TICK,
        pause_callback: Callable[[], bool] | None = None,
        notifier_factory: Callable[..., WeComNotifier] | None = None,
    ) -> None:
        self.interval_seconds = max(5.0, float(interval_seconds or DEFAULT_WORKER_INTERVAL_SECONDS))
        self.limit_per_tick = max(1, int(limit_per_tick or DEFAULT_WORKER_LIMIT_PER_TICK))
        self.pause_callback = pause_callback
        self.notifier_factory = notifier_factory or WeComNotifier
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="NotificationRetryWorker",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)

    def update_pause_callback(self, pause_callback: Callable[[], bool] | None) -> None:
        self.pause_callback = pause_callback

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                if not (self.pause_callback and self.pause_callback()):
                    stats = retry_due_notifications(
                        limit=self.limit_per_tick,
                        notifier_factory=self.notifier_factory,
                        pause_callback=self.pause_callback,
                    )
                    if stats.get("attempted"):
                        print(f"[NotificationRetry] 补发轮询结果: {stats}")
            except Exception as exc:
                print(f"[NotificationRetry] 补发轮询异常: {exc}")
            self._stop_event.wait(self.interval_seconds)


def start_notification_retry_worker(
    *,
    pause_callback: Callable[[], bool] | None = None,
    interval_seconds: float = DEFAULT_WORKER_INTERVAL_SECONDS,
    limit_per_tick: int = DEFAULT_WORKER_LIMIT_PER_TICK,
    notifier_factory: Callable[..., WeComNotifier] | None = None,
) -> NotificationRetryWorker:
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None:
            _WORKER = NotificationRetryWorker(
                interval_seconds=interval_seconds,
                limit_per_tick=limit_per_tick,
                pause_callback=pause_callback,
                notifier_factory=notifier_factory,
            )
        else:
            _WORKER.update_pause_callback(pause_callback)
        _WORKER.start()
        return _WORKER


def stop_notification_retry_worker() -> None:
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is not None:
            _WORKER.stop()
            _WORKER = None


def _normalize_payload(
    payload: dict,
    *,
    now: float | None,
    initial_delay_seconds: float,
) -> dict:
    if not isinstance(payload, dict):
        return {}
    notifier = payload.get("notifier") if isinstance(payload.get("notifier"), dict) else {}
    send_args = payload.get("send_args") if isinstance(payload.get("send_args"), dict) else {}
    webhook_url = str(notifier.get("webhook_url") or payload.get("webhook_url") or "").strip()
    if not is_valid_wecom_webhook(webhook_url):
        return {}

    now_ts = float(time.time() if now is None else now)
    delay = max(0.0, float(initial_delay_seconds or 0))
    normalized_send_args = _normalize_send_args(send_args)
    task_name = str(
        payload.get("task_name")
        or normalized_send_args.get("task_name")
        or (payload.get("task") or {}).get("name")
        or ""
    ).strip()
    normalized_send_args["task_name"] = task_name

    entry = {
        "id": uuid.uuid4().hex,
        "type": "wecom_detected_images",
        "status": "pending",
        "dedupe_key": "",
        "created_at": _format_ts(now_ts),
        "created_ts": now_ts,
        "updated_at": _format_ts(now_ts),
        "updated_ts": now_ts,
        "next_attempt_at": _format_ts(now_ts + delay),
        "next_attempt_ts": now_ts + delay,
        "attempt_count": 0,
        "max_attempts": max(1, int(payload.get("max_attempts") or DEFAULT_MAX_ATTEMPTS)),
        "last_error": str(payload.get("last_error") or "").strip(),
        "task_name": task_name,
        "task_id": str((payload.get("task") or {}).get("task_id") or payload.get("task_id") or "").strip(),
        "daily_state_source": str(payload.get("daily_state_source") or "recognition").strip() or "recognition",
        "daily_state_scope": str(payload.get("daily_state_scope") or "official").strip() or "official",
        "notifier": {
            "webhook_url": webhook_url,
            "cooldown_minutes": _safe_number(notifier.get("cooldown_minutes"), default=30),
            "send_interval": _safe_number(notifier.get("send_interval"), default=2),
        },
        "send_args": normalized_send_args,
        "task": _json_safe(payload.get("task") or {}),
        "idempotency": _normalize_idempotency(payload.get("idempotency")),
    }
    if not entry["idempotency"]:
        entry["idempotency"] = _build_retry_idempotency(entry)
    entry["dedupe_key"] = _build_dedupe_key(entry)
    return entry


def _normalize_send_args(send_args: dict) -> dict:
    screenshot_paths = [
        str(path).strip()
        for path in (send_args.get("screenshot_paths") or [])
        if str(path or "").strip()
    ]
    return {
        "task_name": str(send_args.get("task_name") or "").strip(),
        "brands": _dedupe_text_list(send_args.get("brands") or []),
        "screenshot_paths": list(dict.fromkeys(screenshot_paths)),
        "detected_platforms": _dedupe_text_list(send_args.get("detected_platforms") or []),
        "source": str(send_args.get("source") or "识别模式").strip() or "识别模式",
        "greeting": str(send_args.get("greeting") or "🎯 品牌监控报告").strip() or "🎯 品牌监控报告",
        "completed_keywords": _dedupe_text_list(send_args.get("completed_keywords") or []),
        "supplemented_keywords": _dedupe_text_list(send_args.get("supplemented_keywords") or []),
        "total_screenshot_count": _safe_int(send_args.get("total_screenshot_count"), default=None),
        "references": _json_safe(send_args.get("references") or []),
        "body_references": _json_safe(send_args.get("body_references") or []),
    }


def _retry_one(
    entry: dict,
    *,
    notifier_factory: Callable[..., WeComNotifier],
    now: float,
) -> str:
    send_args = dict(entry.get("send_args") or {})
    screenshot_paths = [
        str(path).strip()
        for path in (send_args.get("screenshot_paths") or [])
        if str(path or "").strip()
    ]
    missing_paths = [path for path in screenshot_paths if not Path(path).exists()]
    if missing_paths:
        error = f"补发截图文件不存在: {', '.join(missing_paths[:3])}"
        if len(missing_paths) > 3:
            error += f" 等 {len(missing_paths)} 个"
        _disable_entry(entry, error=error, now=now)
        _record_retry_event(entry, error, final=True)
        print(f"[NotificationRetry] {error}")
        return "disabled"

    notifier_cfg = dict(entry.get("notifier") or {})
    notifier = notifier_factory(
        webhook_url=str(notifier_cfg.get("webhook_url") or "").strip(),
        cooldown_minutes=_safe_number(notifier_cfg.get("cooldown_minutes"), default=30),
        send_interval=_safe_number(notifier_cfg.get("send_interval"), default=2),
    )
    ok = notifier.send_detected_images(
        task_name=str(send_args.get("task_name") or entry.get("task_name") or ""),
        brands=list(send_args.get("brands") or []),
        screenshot_paths=screenshot_paths,
        detected_platforms=list(send_args.get("detected_platforms") or []),
        source=str(send_args.get("source") or "识别模式"),
        greeting=str(send_args.get("greeting") or "🎯 品牌监控报告"),
        completed_keywords=list(send_args.get("completed_keywords") or []),
        supplemented_keywords=list(send_args.get("supplemented_keywords") or []),
        total_screenshot_count=send_args.get("total_screenshot_count"),
        references=list(send_args.get("references") or []),
        body_references=list(send_args.get("body_references") or []),
    )
    if ok:
        _record_notification_idempotency(entry)
        _remove_entry(entry)
        _mark_daily_state_success(entry)
        print(
            f"[NotificationRetry] 补发成功: "
            f"id={entry.get('id', '')}, task={entry.get('task_name', '')}"
        )
        return "success"

    error = str(getattr(notifier, "last_error", "") or "企业微信补发失败").strip()
    _reschedule_entry(entry, error=error, now=now)
    return "failed"


def _select_due_entries(*, limit: int, now: float) -> list[dict]:
    selected: list[dict] = []
    with _LOCK:
        entries = _load_queue_unlocked()
        for entry in entries:
            if len(selected) >= limit:
                break
            entry_id = str(entry.get("id") or "").strip()
            if not entry_id or entry_id in _ACTIVE_RETRY_IDS:
                continue
            if str(entry.get("type") or "") != "wecom_detected_images":
                continue
            if str(entry.get("status") or "pending") != "pending":
                continue
            if int(entry.get("attempt_count") or 0) >= int(entry.get("max_attempts") or DEFAULT_MAX_ATTEMPTS):
                continue
            if float(entry.get("next_attempt_ts") or 0) > now:
                continue
            _ACTIVE_RETRY_IDS.add(entry_id)
            selected.append(copy.deepcopy(entry))
    return selected


def _release_active(entry: dict) -> None:
    entry_id = str((entry or {}).get("id") or "").strip()
    if not entry_id:
        return
    with _LOCK:
        _ACTIVE_RETRY_IDS.discard(entry_id)


def _reschedule_entry(entry: dict, *, error: str, now: float) -> None:
    entry_id = str(entry.get("id") or "").strip()
    with _LOCK:
        entries = _load_queue_unlocked()
        for existing in entries:
            if str(existing.get("id") or "") != entry_id:
                continue
            attempt_count = int(existing.get("attempt_count") or 0) + 1
            existing["attempt_count"] = attempt_count
            existing["updated_at"] = _format_ts(now)
            existing["updated_ts"] = now
            existing["last_error"] = error
            if attempt_count >= int(existing.get("max_attempts") or DEFAULT_MAX_ATTEMPTS):
                existing["status"] = "disabled"
                existing["disabled_at"] = _format_ts(now)
                _save_queue_unlocked(entries)
                _record_retry_event(existing, error, final=True)
                print(
                    f"[NotificationRetry] 补发达到最大次数，停止重试: "
                    f"id={entry_id}, task={existing.get('task_name', '')}, error={error}"
                )
                return

            delay = min(MAX_BACKOFF_SECONDS, DEFAULT_BACKOFF_SECONDS * (2 ** max(0, attempt_count - 1)))
            existing["next_attempt_ts"] = now + delay
            existing["next_attempt_at"] = _format_ts(now + delay)
            _save_queue_unlocked(entries)
            print(
                f"[NotificationRetry] 补发失败，已安排下次重试: "
                f"id={entry_id}, attempt={attempt_count}, next={existing['next_attempt_at']}, error={error}"
            )
            return


def _disable_entry(entry: dict, *, error: str, now: float) -> None:
    entry_id = str(entry.get("id") or "").strip()
    with _LOCK:
        entries = _load_queue_unlocked()
        for existing in entries:
            if str(existing.get("id") or "") != entry_id:
                continue
            existing["status"] = "disabled"
            existing["updated_at"] = _format_ts(now)
            existing["updated_ts"] = now
            existing["disabled_at"] = _format_ts(now)
            existing["last_error"] = error
            _save_queue_unlocked(entries)
            return


def _remove_entry(entry: dict) -> None:
    entry_id = str(entry.get("id") or "").strip()
    with _LOCK:
        entries = [
            existing for existing in _load_queue_unlocked()
            if str(existing.get("id") or "") != entry_id
        ]
        _save_queue_unlocked(entries)


def _mark_daily_state_success(entry: dict) -> None:
    try:
        from .daily_task_state import build_task_state_extra, write_task_status
        from .cycle_state import update_cycle_report_with_recognition

        send_args = dict(entry.get("send_args") or {})
        task = dict(entry.get("task") or {})
        if not task:
            task = {
                "name": str(entry.get("task_name") or send_args.get("task_name") or ""),
                "task_id": str(entry.get("task_id") or ""),
            }
        source = str(entry.get("daily_state_source") or "recognition").strip() or "recognition"
        scope = str(entry.get("daily_state_scope") or "official").strip() or "official"
        extra = build_task_state_extra(
            brands=list(send_args.get("brands") or []),
            image_count=len(send_args.get("screenshot_paths") or []),
            completed_keywords=list(send_args.get("completed_keywords") or []),
            supplemented_keywords=list(send_args.get("supplemented_keywords") or []),
            detected_platforms=list(send_args.get("detected_platforms") or []),
            task_failure_kind="",
            notification_success=True,
            extra={
                "retry_queue_id": str(entry.get("id") or ""),
                "retry_attempt_count": int(entry.get("attempt_count") or 0) + 1,
            },
        )
        write_task_status(
            task,
            status="success",
            source=source,
            scope=scope,
            message="企业微信通知补发成功",
            extra=extra,
        )
        task_id = str(task.get("task_id") or entry.get("task_id") or "").strip()
        if task_id and source != "manual_test":
            update_cycle_report_with_recognition(
                task_id=task_id,
                task_name=str(task.get("name") or entry.get("task_name") or send_args.get("task_name") or ""),
                brands=list(send_args.get("brands") or []),
                ok=True,
                reason="",
                supplemented_keywords=list(send_args.get("supplemented_keywords") or []),
                completed_keywords=list(send_args.get("completed_keywords") or []),
                detected_platforms=list(send_args.get("detected_platforms") or []),
                image_count=len(send_args.get("screenshot_paths") or []),
            )
    except Exception as exc:
        print(f"[NotificationRetry] 补发成功后更新任务状态失败: {exc}")


def _record_retry_event(entry: dict, error: str, *, final: bool) -> None:
    try:
        from .diagnostics import record_event

        send_args = dict(entry.get("send_args") or {})
        record_event(
            category="notification",
            message=error,
            task_name=str(entry.get("task_name") or send_args.get("task_name") or ""),
            platform="recognition",
            keyword="clipboard",
            brand=",".join(send_args.get("brands") or []),
            details={
                "retry_queue_id": str(entry.get("id") or ""),
                "attempt_count": int(entry.get("attempt_count") or 0),
                "final": bool(final),
                "image_paths": list(send_args.get("screenshot_paths") or []),
            },
            suggestion="检查网络恢复情况、企业微信 webhook 是否可用，以及截图文件是否仍然存在。",
        )
    except Exception:
        pass


def _record_notification_idempotency(entry: dict) -> None:
    if str((entry or {}).get("daily_state_source") or "").strip() == "manual_test":
        return
    identity = entry.get("idempotency") if isinstance(entry.get("idempotency"), dict) else {}
    if not identity:
        identity = _build_retry_idempotency(entry)
    if not identity:
        return
    try:
        record_notification_sent(**identity)
    except Exception as exc:
        print(f"[NotificationRetry] 补发成功后写入通知幂等失败: {exc}")


def _normalize_idempotency(value) -> dict:
    if not isinstance(value, dict):
        return {}
    identity = {
        "webhook_url": str(value.get("webhook_url") or "").strip(),
        "task_id": str(value.get("task_id") or "").strip(),
        "task_name": str(value.get("task_name") or "").strip(),
        "channel": str(value.get("channel") or "").strip(),
        "run_date": str(value.get("run_date") or "").strip(),
        "round_id": str(value.get("round_id") or "").strip(),
        "payload_hash": str(value.get("payload_hash") or "").strip(),
    }
    if not identity["webhook_url"] or not identity["channel"]:
        return {}
    return identity


def _build_retry_idempotency(entry: dict) -> dict:
    send_args = dict(entry.get("send_args") or {})
    notifier = dict(entry.get("notifier") or {})
    task = entry.get("task") if isinstance(entry.get("task"), dict) else {}
    webhook_url = str(notifier.get("webhook_url") or "").strip()
    task_id = str(entry.get("task_id") or task.get("task_id") or "").strip()
    task_name = str(entry.get("task_name") or send_args.get("task_name") or "").strip()
    channel = "recognition_detected_images"
    payload_hash = build_payload_hash({
        "brands": list(send_args.get("brands") or []),
        "completed_keywords": list(send_args.get("completed_keywords") or []),
        "supplemented_keywords": list(send_args.get("supplemented_keywords") or []),
        "detected_platforms": list(send_args.get("detected_platforms") or []),
        "image_count": len(send_args.get("screenshot_paths") or []),
    })
    run_date = str(entry.get("run_date") or "").strip() or str(entry.get("created_at") or "")[:10]
    return _normalize_idempotency({
        "webhook_url": webhook_url,
        "task_id": task_id,
        "task_name": task_name,
        "channel": channel,
        "run_date": run_date,
        "round_id": f"{channel}:{task_id or task_name}:{run_date or 'today'}",
        "payload_hash": payload_hash,
    })


def _build_dedupe_key(entry: dict) -> str:
    send_args = dict(entry.get("send_args") or {})
    identity = {
        "type": entry.get("type"),
        "webhook_url": (entry.get("notifier") or {}).get("webhook_url"),
        "task_id": entry.get("task_id"),
        "task_name": entry.get("task_name"),
        "daily_state_source": entry.get("daily_state_source"),
        "brands": send_args.get("brands") or [],
        "screenshot_paths": send_args.get("screenshot_paths") or [],
        "completed_keywords": send_args.get("completed_keywords") or [],
        "detected_platforms": send_args.get("detected_platforms") or [],
    }
    text = json.dumps(identity, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_queue_unlocked() -> list[dict]:
    path = get_retry_queue_path()
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]
    except Exception:
        return []


def _save_queue_unlocked(entries: list[dict]) -> None:
    path = get_retry_queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(entries, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _dedupe_text_list(values) -> list[str]:
    items: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in items:
            items.append(text)
    return items


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _safe_number(value, *, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value, *, default: int | None) -> int | None:
    if value is None and default is None:
        return None
    try:
        return int(value)
    except Exception:
        return default


def _format_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
