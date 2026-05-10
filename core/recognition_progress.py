"""Pure progress counting helpers for recognition mode."""

from __future__ import annotations


def batch_image_count(batch: dict | None) -> int:
    payload = batch if isinstance(batch, dict) else {}
    image_items = payload.get("image_items")
    if isinstance(image_items, list):
        return len(image_items)
    image_paths = payload.get("image_paths")
    if isinstance(image_paths, list):
        return len(image_paths)
    return 0


def format_batch_image_progress(batch: dict) -> str:
    current_count = int(batch.get("current_image_count") or len(batch.get("image_paths") or []))
    historical_count = int(batch.get("historical_screenshot_count") or 0)
    total_count = int(batch.get("total_image_count") or len(batch.get("image_paths") or []))
    return f"累计 {total_count} 张（本次 {current_count} 张，历史 {historical_count} 张）"


def format_threshold_progress_text(current_count: int, batch_size: int, historical_count: int) -> str:
    current_count = max(0, int(current_count or 0))
    batch_size = max(1, int(batch_size or 1))
    historical_count = max(0, int(historical_count or 0))
    total_count = current_count + historical_count
    return f"累计 {total_count}/{batch_size} 张（本次 {current_count} 张，历史 {historical_count} 张）"


__all__ = [
    "batch_image_count",
    "format_batch_image_progress",
    "format_threshold_progress_text",
]
