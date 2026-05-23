"""Monthly dashboard overview helpers."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any, Callable

from backend_lib.dashboard_tasks import _task_is_scheduled_for_day
from core.daily_task_state import derive_task_id, get_task_day_status
from core.history import (
    extract_success_record_detected_platforms,
    get_records_many,
    is_manual_test_failure_record,
    is_success_record,
    normalize_platform_id,
)
from core.time_utils import local_today


_SPEND_FIELD_NAMES = {
    "cost",
    "cost_amount",
    "expense",
    "fee",
    "price",
    "publish_cost",
    "publish_fee",
    "spend",
    "total_cost",
    "total_fee",
    "total_price",
    "total_spend",
}


def _parse_date_text(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _article_day(article: dict[str, Any]) -> date | None:
    return (
        _parse_date_text(article.get("published_at"))
        or _parse_date_text(article.get("published"))
        or _parse_date_text(article.get("published_ts"))
        or _parse_date_text(article.get("ts"))
        or _parse_date_text(article.get("imported_at"))
        or _parse_date_text(article.get("created_at"))
    )


def _safe_float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return float(str(value or "").replace(",", "").replace("¥", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(float(str(value or "").replace(",", "").strip()))
    except (TypeError, ValueError):
        return default


def _article_spend(article: dict[str, Any]) -> float:
    for key, value in article.items():
        normalized_key = str(key or "").strip()
        if not normalized_key:
            continue
        snake_key = normalized_key.replace("-", "_").replace(" ", "_")
        if snake_key in _SPEND_FIELD_NAMES:
            amount = _safe_float(value)
            if amount > 0:
                return amount
    return 0.0


def _article_platform(article: dict[str, Any]) -> str:
    for key in ("account_platform", "account_platform_label", "platform", "source", "media_name"):
        text = str(article.get(key) or "").strip()
        if text:
            return normalize_platform_id(text) or text
    return ""


def _record_day(record: dict[str, Any]) -> date | None:
    return _parse_date_text(record.get("ts") or record.get("created_at"))


def _reference_event_day(event: dict[str, Any]) -> date | None:
    return _parse_date_text(
        event.get("referenced_at")
        or event.get("last_referenced_at")
        or event.get("ts")
        or event.get("created_at")
    )


def _reference_event_platform(event: dict[str, Any]) -> str:
    for key in ("platform", "platform_id", "source_platform", "provider", "source"):
        text = str(event.get(key) or "").strip()
        if text:
            return normalize_platform_id(text) or text
    return ""


def _month_bounds(today: date) -> tuple[date, date]:
    month_start = today.replace(day=1)
    if month_start.month == 12:
        next_month = date(month_start.year + 1, 1, 1)
    else:
        next_month = date(month_start.year, month_start.month + 1, 1)
    return month_start, next_month - timedelta(days=1)


def _task_overlaps_month(task: dict[str, Any], month_start: date, month_end: date) -> bool:
    if not task.get("enabled", True):
        return False
    start = _parse_date_text(task.get("optimization_start_date")) or month_start
    end = _parse_date_text(task.get("optimization_end_date")) or month_end
    return start <= month_end and end >= month_start


def _empty_day(current_day: date, today: date) -> dict[str, Any]:
    return {
        "date": current_day.isoformat(),
        "day": current_day.day,
        "weekday": current_day.weekday(),
        "articleCount": 0,
        "spend": 0.0,
        "taskCount": 0,
        "displayCount": 0,
        "pendingOptimizationCount": 0,
        "referenceCount": 0,
        "platformCount": 0,
        "platforms": [],
        "workDay": False,
        "future": current_day > today,
        "intensity": 0,
    }


def _task_display_name(task: dict[str, Any]) -> str:
    return str(task.get("brand") or task.get("name") or task.get("task_id") or derive_task_id(task)).strip()


def _build_dashboard_month_overview(
    tasks: list[dict[str, Any]],
    scheduler_config: dict[str, Any] | None,
    articles: list[dict[str, Any]],
    *,
    today: date | None = None,
    history_records_loader: Callable[[list[tuple[str, str]]], list[list[dict[str, Any]]]] | None = None,
    day_status_loader: Callable[[dict[str, Any], date | None], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    current_today = today or local_today()
    month_start, month_end = _month_bounds(current_today)
    days = [month_start + timedelta(days=offset) for offset in range((month_end - month_start).days + 1)]
    by_date = {current_day.isoformat(): _empty_day(current_day, current_today) for current_day in days}

    active_tasks = [task for task in tasks if _task_overlaps_month(task, month_start, month_end)]
    task_specs: list[tuple[str, str]] = []
    for task in active_tasks:
        task_id = str(task.get("task_id") or derive_task_id(task)).strip()
        task_name = str(task.get("name") or task_id).strip()
        if task_name:
            task_specs.append((task_name, task_id))

    load_history_records = history_records_loader or get_records_many
    try:
        history_batches = load_history_records(task_specs) if task_specs else []
    except Exception:
        history_batches = [[] for _spec in task_specs]

    month_platforms: set[str] = set()
    article_total = 0
    total_spend = 0.0
    display_total = 0
    reference_total = 0
    day_task_names: dict[str, set[str]] = {key: set() for key in by_date.keys()}
    day_platforms: dict[str, set[str]] = {key: set() for key in by_date.keys()}

    for article in articles or []:
        if not isinstance(article, dict):
            continue
        article_day = _article_day(article)
        if article_day is None or not (month_start <= article_day <= month_end):
            continue
        key = article_day.isoformat()
        amount = _article_spend(article)
        by_date[key]["articleCount"] += 1
        by_date[key]["spend"] += amount
        article_total += 1
        total_spend += amount
        platform = _article_platform(article)
        if platform:
            month_platforms.add(platform)
            day_platforms[key].add(platform)

    for task, records in zip(active_tasks, history_batches):
        task_name = _task_display_name(task)
        for record in records or []:
            if not isinstance(record, dict) or is_manual_test_failure_record(record):
                continue
            record_day = _record_day(record)
            if record_day is None or not (month_start <= record_day <= month_end):
                continue
            key = record_day.isoformat()
            day_task_names[key].add(task_name)
            for platform in extract_success_record_detected_platforms(record):
                month_platforms.add(platform)
                day_platforms[key].add(platform)
            if is_success_record(record):
                by_date[key]["displayCount"] += 1
                display_total += 1

    load_day_status = day_status_loader or get_task_day_status
    current_pending_task_ids: set[str] = set()
    for task in active_tasks:
        task_id = str(task.get("task_id") or derive_task_id(task)).strip()
        task_name = _task_display_name(task)
        for current_day in days:
            if current_day > current_today:
                continue
            scheduled = _task_is_scheduled_for_day(task, scheduler_config, current_day)
            try:
                status = load_day_status(task, current_day)
            except Exception:
                status = {}
            brand_status = str(status.get("brand_status") or "").strip()
            formal_started = bool(status.get("formal_started"))
            if not scheduled and not formal_started:
                continue
            key = current_day.isoformat()
            day_task_names[key].add(task_name)
            if brand_status not in {"success", "sent"}:
                by_date[key]["pendingOptimizationCount"] += 1
                if current_day == current_today and task_id:
                    current_pending_task_ids.add(task_id)

    for article in articles or []:
        if not isinstance(article, dict):
            continue
        reference_hits = article.get("reference_hits") if isinstance(article.get("reference_hits"), dict) else {}
        for raw_hit in reference_hits.values():
            if not isinstance(raw_hit, dict):
                continue
            events = [item for item in (raw_hit.get("events") or []) if isinstance(item, dict)]
            counted_events = 0
            for event in events:
                event_day = _reference_event_day(event)
                if event_day is None or not (month_start <= event_day <= month_end):
                    continue
                key = event_day.isoformat()
                by_date[key]["referenceCount"] += 1
                reference_total += 1
                counted_events += 1
                platform = _reference_event_platform(event)
                if platform:
                    month_platforms.add(platform)
                    day_platforms[key].add(platform)

            if counted_events:
                continue
            hit_day = _parse_date_text(raw_hit.get("last_referenced_at"))
            if hit_day is None or not (month_start <= hit_day <= month_end):
                continue
            legacy_count = max(1, _safe_int(raw_hit.get("count"), 0))
            key = hit_day.isoformat()
            by_date[key]["referenceCount"] += legacy_count
            reference_total += legacy_count

    scores: list[int] = []
    for current_day in days:
        key = current_day.isoformat()
        row = by_date[key]
        row["taskCount"] = len(day_task_names[key])
        row["platforms"] = sorted(day_platforms[key])
        row["platformCount"] = len(day_platforms[key])
        row["spend"] = round(float(row["spend"] or 0), 2)
        row["workDay"] = bool(
            row["taskCount"]
            or row["articleCount"]
            or row["displayCount"]
            or row["referenceCount"]
        )
        score = (
            int(row["articleCount"] or 0)
            + int(row["displayCount"] or 0)
            + int(row["referenceCount"] or 0)
        )
        scores.append(score)

    max_score = max(scores or [0])
    for current_day in days:
        row = by_date[current_day.isoformat()]
        score = (
            int(row["articleCount"] or 0)
            + int(row["displayCount"] or 0)
            + int(row["referenceCount"] or 0)
        )
        if score <= 0:
            row["intensity"] = 0
        elif max_score <= 1:
            row["intensity"] = 2
        else:
            row["intensity"] = max(1, min(4, round((score / max_score) * 4)))

    day_rows = [by_date[current_day.isoformat()] for current_day in days]
    task_count = len(active_tasks)
    work_days = sum(1 for row in day_rows if row.get("workDay"))
    current_day_key = current_today.isoformat()
    if current_day_key not in by_date:
        current_day_key = day_rows[-1]["date"] if day_rows else ""

    return {
        "month": month_start.strftime("%Y-%m"),
        "monthLabel": f"{month_start.year}年{month_start.month}月",
        "startDate": month_start.isoformat(),
        "endDate": month_end.isoformat(),
        "selectedDate": current_day_key,
        "totals": {
            "articlePublishedTotal": article_total,
            "totalSpend": round(total_spend, 2),
            "workDays": work_days,
            "brandTaskCount": task_count,
            "brandDisplayTotal": display_total,
            "brandPendingOptimizationCount": len(current_pending_task_ids),
            "referenceTotal": reference_total,
            "platformCoverage": len(month_platforms),
        },
        "days": day_rows,
    }


__all__ = ["_build_dashboard_month_overview"]
