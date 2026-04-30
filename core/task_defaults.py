"""
任务默认值辅助函数
- 计算新增任务时最常用的 webhook
- 计算识别模式截图数默认值
"""

from __future__ import annotations

from collections import Counter
from typing import Any


def _normalize_webhook_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text or "YOUR_KEY_HERE" in text:
        return ""
    return text


def get_most_common_task_webhook(tasks: list[dict[str, Any]] | None) -> str:
    normalized_items: list[str] = []
    first_seen_order: list[str] = []
    for task in tasks or []:
        webhook = _normalize_webhook_url((task or {}).get("webhook_url", ""))
        if not webhook:
            continue
        normalized_items.append(webhook)
        if webhook not in first_seen_order:
            first_seen_order.append(webhook)
    if not normalized_items:
        return ""

    counts = Counter(normalized_items)
    best_count = max(counts.values())
    for webhook in first_seen_order:
        if counts.get(webhook, 0) == best_count:
            return webhook
    return ""


def compute_recognition_batch_size_from_keywords(
    keywords: list[dict[str, Any]] | None,
    *,
    fallback_platforms: list[str] | None = None,
) -> int:
    fallback_count = len([item for item in (fallback_platforms or []) if str(item or "").strip()])
    total = 0

    for keyword in keywords or []:
        if not isinstance(keyword, dict):
            continue
        platform_count = len([
            item for item in (keyword.get("platforms") or [])
            if str(item or "").strip()
        ])
        if platform_count <= 0:
            platform_count = fallback_count
        if platform_count > 0:
            total += platform_count

    return max(1, total or fallback_count or 1)
