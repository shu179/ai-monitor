"""Pure notification payload helpers for recognition mode."""

from __future__ import annotations

from typing import Any

from core.daily_task_state import derive_task_id
from core.notification_idempotency import build_payload_hash
from core.time_utils import local_today


def build_recognition_notification_idempotency(
    *,
    webhook_url: str,
    task: dict[str, Any],
    batch: dict[str, Any],
    completed_keywords: list[str],
    supplemented_keywords: list[str],
    detected_platforms: list[str],
    image_count: int,
) -> dict[str, str]:
    task_id = str((task or {}).get("task_id") or derive_task_id(task or {}) or "").strip()
    task_name = str((batch or {}).get("task_name") or (task or {}).get("name") or task_id).strip()
    channel = "recognition_detected_images"
    run_date = local_today().isoformat()
    payload_hash = build_payload_hash({
        "brands": list((batch or {}).get("brands") or []),
        "completed_keywords": list(completed_keywords or []),
        "supplemented_keywords": list(supplemented_keywords or []),
        "detected_platforms": list(detected_platforms or []),
        "image_count": max(0, int(image_count or 0)),
    })
    return {
        "webhook_url": str(webhook_url or "").strip(),
        "task_id": task_id,
        "task_name": task_name,
        "channel": channel,
        "run_date": run_date,
        "round_id": f"{channel}:{task_id or task_name}:{run_date}",
        "payload_hash": payload_hash,
    }


__all__ = ["build_recognition_notification_idempotency"]
