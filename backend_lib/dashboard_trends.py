"""Dashboard trend and media-stat helpers for the local web backend."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from core.daily_task_state import derive_task_id
from core.history import (
    get_brand_trend_series,
    get_brand_trend_series_from_records,
    get_records_many,
    get_task_brand_names,
)


DEFAULT_MEDIA_STAT_PLATFORM_LABELS: dict[str, str] = {
    "local_model": "本地模型",
    "doubao": "豆包",
    "deepseek": "DeepSeek",
    "kimi": "Kimi",
    "tongyi": "通义千问",
    "wenxin": "文心一言",
    "yuanbao": "元宝",
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "gemini": "Gemini",
    "ark_deepseek": "Ark DeepSeek",
    "perplexity": "Perplexity",
}


def _build_media_stats(
    platform_counter: Any,
    *,
    platform_display_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """从平台调用计数生成 mediaStats，按总量降序。"""
    if not platform_counter:
        return [
            {"label": "豆包", "auth": 0, "self": 0},
            {"label": "Kimi", "auth": 0, "self": 0},
            {"label": "文心一言", "auth": 0, "self": 0},
            {"label": "通义千问", "auth": 0, "self": 0},
            {"label": "元宝", "auth": 0, "self": 0},
            {"label": "DeepSeek", "auth": 0, "self": 0},
        ]

    labels = platform_display_names or DEFAULT_MEDIA_STAT_PLATFORM_LABELS
    stats = []
    for pid, total in platform_counter.most_common(6):
        label = labels.get(pid, pid)
        # 粗略按 7:3 分配权威/自媒体（后续可从 article_store 精确统计）
        auth = int(total * 0.7)
        self_ = total - auth
        stats.append({"label": label, "auth": auth, "self": self_})
    return stats


def _range_to_days(range_key: str) -> int:
    key = str(range_key or "week").strip().lower()
    if key == "year":
        return 365
    if key == "month":
        return 30
    return 7


def _weekday_name_from_iso(date_text: str) -> str:
    try:
        dt = datetime.strptime(str(date_text or ""), "%Y-%m-%d")
        return f"周{['一', '二', '三', '四', '五', '六', '日'][dt.weekday()]}"
    except Exception:
        return str(date_text or "")


def _task_created_at(task: dict[str, Any]) -> str:
    return str(
        task.get("created_at")
        or task.get("createdAt")
        or task.get("cloud_created_at")
        or task.get("cloud_synced_at")
        or ""
    ).strip()


def _summarize_trend_points(
    points: list[dict[str, Any]],
    recorded_dates: list[str] | None = None,
    range_key: str = "week",
) -> dict[str, int]:
    values = [int(round(float(item.get("value") or 0))) for item in points if item.get("value") is not None]
    if not values:
        return {"current": 0, "avg": 0, "peak": 0, "delta": 0}

    recorded_keys = set()
    for ds in recorded_dates or []:
        text = str(ds or "").strip()
        if not text:
            continue
        if range_key == "year":
            recorded_keys.add(text[:7])
        else:
            recorded_keys.add(text[:10])

    comparable_values = []
    if recorded_keys:
        for item in points:
            point_key = str(item.get("date") or "").strip()
            if not point_key or point_key not in recorded_keys or item.get("value") is None:
                continue
            comparable_values.append(int(round(float(item.get("value") or 0))))

    summary_pool = comparable_values if comparable_values else values
    current = summary_pool[-1]
    previous = summary_pool[-2] if len(summary_pool) >= 2 else current
    if previous > 0:
        delta = round(((current - previous) / previous) * 100)
    else:
        delta = 0
    delta = max(-9, min(9, delta))
    return {
        "current": current,
        "avg": round(sum(summary_pool) / len(summary_pool)),
        "peak": max(summary_pool),
        "delta": delta,
    }


def _format_trend_points(series: dict[str, Any] | None, range_key: str) -> list[dict[str, Any]]:
    if not series:
        return []

    dates = list(series.get("dates") or [])
    actual = list(series.get("actual") or [])
    predicted = list(series.get("predicted") or [])
    rows = []
    for idx, ds in enumerate(dates):
        if idx >= len(actual):
            continue
        actual_value = actual[idx]
        predicted_value = predicted[idx] if idx < len(predicted) else None
        if actual_value is None and predicted_value is None:
            continue
        rows.append({
            "date": getattr(ds, "isoformat", lambda: str(ds))(),
            "value": round(float(actual_value or 0), 1),
            "predict": round(float(predicted_value or actual_value or 0), 1),
        })

    if not rows:
        return []

    if range_key == "year":
        buckets: dict[str, dict[str, Any]] = {}
        for row in rows:
            month_key = str(row["date"])[:7]
            bucket = buckets.setdefault(month_key, {"value": [], "predict": []})
            bucket["value"].append(float(row["value"]))
            bucket["predict"].append(float(row["predict"]))
        points = []
        for month_key in sorted(buckets.keys())[-12:]:
            month_num = int(month_key.split("-")[1])
            bucket = buckets[month_key]
            points.append({
                "date": month_key,
                "name": f"{month_num}月",
                "value": round(sum(bucket["value"]) / len(bucket["value"]), 1),
                "predict": round(sum(bucket["predict"]) / len(bucket["predict"]), 1),
            })
        return points

    if range_key == "month":
        return [
            {
                "date": row["date"],
                "name": row["date"][5:],
                "value": row["value"],
                "predict": row["predict"],
            }
            for row in rows[-30:]
        ]

    return [
        {
            "date": row["date"],
            "name": _weekday_name_from_iso(row["date"]),
            "value": row["value"],
            "predict": row["predict"],
        }
        for row in rows[-7:]
    ]


def _build_dashboard_media_stats(articles: list[dict[str, Any]], days: int = 7) -> list[dict[str, Any]]:
    if not articles:
        return []

    today = datetime.now().date()
    day_list = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    stats_by_day = {
        day.isoformat(): {"name": day.strftime("%m-%d"), "auth": 0, "self": 0}
        for day in day_list
    }

    for article in articles:
        ds = str(article.get("ts", ""))[:10]
        if ds not in stats_by_day:
            continue
        if str(article.get("media_type", "")).strip() == "authority":
            stats_by_day[ds]["auth"] += 1
        else:
            stats_by_day[ds]["self"] += 1

    return [stats_by_day[day.isoformat()] for day in day_list]


def _empty_trend_payload(range_key: str) -> dict[str, Any]:
    return {
        "timeRange": str(range_key or "week"),
        "current": 0,
        "avg": 0,
        "peak": 0,
        "delta": 0,
        "data": [],
    }


def _build_trend_payload(series: dict[str, Any] | None, range_key: str) -> dict[str, Any]:
    points = _format_trend_points(series, range_key)
    summary = _summarize_trend_points(points, list(series.get("recorded_dates") or []), range_key)
    return {
        "timeRange": str(range_key or "week"),
        "current": int(summary.get("current") or 0),
        "avg": int(summary.get("avg") or 0),
        "peak": int(summary.get("peak") or 0),
        "delta": int(summary.get("delta") or 0),
        "data": points,
    }


def _build_task_trend(task: dict[str, Any], range_key: str) -> dict[str, Any]:
    task_name = str(task.get("name") or derive_task_id(task)).strip()
    if not task_name:
        return _empty_trend_payload(range_key)

    try:
        series = get_brand_trend_series(
            task_name,
            get_task_brand_names(task),
            _range_to_days(range_key),
            task_id=str(task.get("task_id") or derive_task_id(task)).strip(),
            task_created_at=_task_created_at(task),
        )
    except Exception:
        series = None

    if not series:
        return _empty_trend_payload(range_key)
    return _build_trend_payload(series, range_key)


def _build_dashboard_trend(tasks: list[dict[str, Any]], range_key: str) -> dict[str, Any]:
    days = _range_to_days(range_key)
    actual_map: dict[str, list[float]] = {}
    predicted_map: dict[str, list[float]] = {}
    recorded_dates: set[str] = set()
    eligible_tasks: list[tuple[dict[str, Any], str, str]] = []

    for task in tasks:
        if not task.get("enabled", True):
            continue
        task_name = str(task.get("name") or derive_task_id(task)).strip()
        if not task_name:
            continue
        task_id = str(task.get("task_id") or derive_task_id(task)).strip()
        eligible_tasks.append((task, task_name, task_id))

    try:
        history_batches = get_records_many([
            (task_name, task_id)
            for _task, task_name, task_id in eligible_tasks
        ])
    except Exception:
        history_batches = [[] for _task, _task_name, _task_id in eligible_tasks]

    for (task, task_name, task_id), records in zip(eligible_tasks, history_batches):
        try:
            series = get_brand_trend_series_from_records(
                task_name,
                get_task_brand_names(task),
                days,
                records,
                task_id=task_id,
                task_created_at=_task_created_at(task),
            )
        except Exception:
            series = None
        if not series:
            continue

        dates = list(series.get("dates") or [])
        actual = list(series.get("actual") or [])
        predicted = list(series.get("predicted") or [])
        for ds in (series.get("recorded_dates") or []):
            text = str(ds or "").strip()
            if text:
                recorded_dates.add(text)
        for idx, current_date in enumerate(dates):
            ds = getattr(current_date, "isoformat", lambda: str(current_date))()
            if idx < len(actual) and actual[idx] is not None:
                actual_map.setdefault(ds, []).append(float(actual[idx]))
            if idx < len(predicted) and predicted[idx] is not None:
                predicted_map.setdefault(ds, []).append(float(predicted[idx]))

    all_dates = sorted(set(actual_map.keys()) | set(predicted_map.keys()))
    if not all_dates:
        return _empty_trend_payload(range_key)

    aggregated_series = {
        "dates": [datetime.strptime(ds, "%Y-%m-%d").date() for ds in all_dates],
        "actual": [
            round(sum(actual_map[ds]) / len(actual_map[ds]), 1) if actual_map.get(ds) else None
            for ds in all_dates
        ],
        "predicted": [
            round(sum(predicted_map[ds]) / len(predicted_map[ds]), 1) if predicted_map.get(ds) else None
            for ds in all_dates
        ],
        "recorded_dates": sorted(recorded_dates),
    }
    return _build_trend_payload(aggregated_series, range_key)


def _build_dashboard_media_stats_monthly(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    today = date.today()
    window_days = 30
    first_day = today - timedelta(days=window_days - 1)
    day_list = [first_day + timedelta(days=offset) for offset in range(window_days)]
    stats_by_day = {
        current_day.isoformat(): {
            "date": current_day.isoformat(),
            "name": f"{current_day.month}/{current_day.day}",
            "auth": 0,
            "self": 0,
        }
        for current_day in day_list
    }

    for article in articles:
        ds = str(article.get("ts", ""))[:10]
        if ds not in stats_by_day:
            continue
        if str(article.get("media_type", "")).strip() == "authority":
            stats_by_day[ds]["auth"] += 1
        else:
            stats_by_day[ds]["self"] += 1

    return [stats_by_day[current_day.isoformat()] for current_day in day_list]
