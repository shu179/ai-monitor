"""Compatibility exports for dashboard helper modules."""

from __future__ import annotations

from backend_lib.dashboard_tasks import (
    DEFAULT_TASK_MODE_TITLES,
    DEFAULT_TASK_PLATFORM_LABELS,
    _build_dashboard_failed_tasks,
    _build_dashboard_today_task_summary,
    _build_task_failure_summary,
    _collect_failed_modes_today,
    _collect_today_successful_task_payload,
    _compute_fixed_screenshot_target,
    _default_normalize_string_list,
    _display_platform_name,
    _task_is_scheduled_for_day,
)
from backend_lib.dashboard_trends import (
    DEFAULT_MEDIA_STAT_PLATFORM_LABELS,
    _build_dashboard_media_stats,
    _build_dashboard_media_stats_monthly,
    _build_dashboard_trend,
    _build_media_stats,
    _build_task_trend,
    _build_trend_payload,
    _empty_trend_payload,
    _format_trend_points,
    _range_to_days,
    _summarize_trend_points,
    _weekday_name_from_iso,
)
from backend_lib.dashboard_month_overview import _build_dashboard_month_overview

__all__ = [
    "DEFAULT_MEDIA_STAT_PLATFORM_LABELS",
    "DEFAULT_TASK_MODE_TITLES",
    "DEFAULT_TASK_PLATFORM_LABELS",
    "_build_dashboard_failed_tasks",
    "_build_dashboard_media_stats",
    "_build_dashboard_media_stats_monthly",
    "_build_dashboard_month_overview",
    "_build_dashboard_today_task_summary",
    "_build_dashboard_trend",
    "_build_media_stats",
    "_build_task_failure_summary",
    "_build_task_trend",
    "_build_trend_payload",
    "_collect_failed_modes_today",
    "_collect_today_successful_task_payload",
    "_compute_fixed_screenshot_target",
    "_default_normalize_string_list",
    "_display_platform_name",
    "_empty_trend_payload",
    "_format_trend_points",
    "_range_to_days",
    "_summarize_trend_points",
    "_task_is_scheduled_for_day",
    "_weekday_name_from_iso",
]
