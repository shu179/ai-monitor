"""Snapshot payload fragment builders for the local web backend."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import datetime
from typing import Any

from core.daily_task_state import derive_task_id, get_task_day_status, get_task_status_label
from core.scheduler import describe_task_schedule


def _build_snapshot_session(session_token: str, header_name: str) -> dict[str, str]:
    return {
        "token": session_token,
        "header": header_name,
    }


def _build_snapshot_branding(app_name: str) -> dict[str, str]:
    return {
        "appName": app_name,
        "brandName": app_name,
        "subtitle": "",
    }


def _build_snapshot_stats(
    *,
    enabled_task_count: int,
    total_task_count: int,
    today_record_count: int,
    hit_record_count: int,
    error_record_count: int,
) -> dict[str, int]:
    return {
        "enabledTasks": enabled_task_count,
        "totalTasks": total_task_count,
        "todayRecords": today_record_count,
        "hitRecords": hit_record_count,
        "errorRecords": error_record_count,
    }


def _build_snapshot_monitoring(
    *,
    enabled: bool,
    running: bool,
    status_message: str,
) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "running": running,
        "statusMessage": status_message,
    }


def _build_snapshot_profile_summary(
    config: dict[str, Any],
    *,
    avatar_url_builder: Callable[[str], str],
) -> dict[str, str]:
    profile = config.get("profile", {}) or {}
    profile_avatar = str(profile.get("avatar") or "").strip()
    return {
        "name": str(profile.get("name") or "").strip(),
        "role": str(profile.get("role") or "").strip(),
        "avatar": profile_avatar,
        "avatarUrl": avatar_url_builder(profile_avatar),
        "birthday": str(profile.get("birthday") or "").strip(),
        "hireDate": str(profile.get("hire_date") or "").strip(),
    }


def _build_snapshot_sidebar(profile_summary: dict[str, str]) -> dict[str, Any]:
    return {
        "userName": profile_summary.get("name", ""),
        "role": profile_summary.get("role", ""),
        "avatar": profile_summary.get("avatarUrl", ""),
        "online": True,
    }


def _build_snapshot_profile(profile_summary: dict[str, str]) -> dict[str, str]:
    return {
        "name": profile_summary.get("name", ""),
        "role": profile_summary.get("role", ""),
        "avatar": profile_summary.get("avatar", ""),
        "birthday": profile_summary.get("birthday", ""),
        "hireDate": profile_summary.get("hireDate", ""),
    }


def _build_snapshot_assistant(
    config: dict[str, Any],
    *,
    monitoring_enabled: bool,
    monitoring_running: bool,
) -> dict[str, str]:
    assistant_cfg = config.get("ai_assistant", {}) or {}
    return {
        "name": "搜搜",
        "level": "Lv. 42 / 状态良好",
        "status": "定时任务运行中" if monitoring_running else ("定时任务已开启" if monitoring_enabled else "定时任务已关闭"),
        "platform": str(assistant_cfg.get("platform") or "监控助手"),
        "model": str(assistant_cfg.get("model") or "running"),
    }


def _build_snapshot_dashboard(
    *,
    current_date: datetime,
    weekday_label: str,
    home_copy: dict[str, Any],
    profile_name: str,
    dashboard_today_summary: dict[str, Any],
    failed_task_details: list[dict[str, Any]],
    today_record_count: int,
    hit_record_count: int,
    dashboard_trend: dict[str, Any],
    source_breakdown: list[dict[str, Any]],
    task_cards: list[dict[str, Any]],
    media_stats: list[dict[str, Any]],
    month_overview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "dateLabel": current_date.strftime("%d %b %Y").upper(),
        "weekdayLabel": weekday_label,
        "greeting": home_copy.get("greeting", "下午好"),
        "userName": profile_name or "AI 运营",
        "headline": home_copy.get(
            "headline",
            "系统运行平稳，今日已为您自动拦截 {} 项异常请求。".format(
                max(today_record_count, hit_record_count) or 12
            ),
        ),
        "todayTaskCount": int(dashboard_today_summary.get("todayTaskCount") or 0),
        "completedCount": int(dashboard_today_summary.get("completedCount") or 0),
        "runningCount": int(dashboard_today_summary.get("runningCount") or 0),
        "failedTaskCount": len(failed_task_details),
        "failedTasks": failed_task_details,
        "todayIntercepted": today_record_count or 12,
        "trend": dashboard_trend,
        "sourceBreakdown": source_breakdown,
        "taskCards": task_cards,
        "mediaStats": media_stats,
        "monthOverview": month_overview or {},
    }


def _build_snapshot_source_breakdown(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the dashboard industry source breakdown from all configured tasks."""
    industry_counter: Counter[str] = Counter()
    for task in tasks:
        for tag in (task.get("industry_tags") or []):
            tag_str = str(tag).strip()
            if tag_str:
                industry_counter[tag_str] += 1

    source_breakdown = [
        {"name": name, "value": value}
        for name, value in industry_counter.most_common()
    ]
    if source_breakdown:
        return source_breakdown
    return [
        {"name": "科技互联网", "value": 45},
        {"name": "消费零售", "value": 25},
        {"name": "金融医疗", "value": 20},
        {"name": "汽车制造", "value": 10},
    ]


def _build_snapshot_platform_items(platforms_config: dict[str, Any]) -> list[dict[str, Any]]:
    platform_items: list[dict[str, Any]] = []
    for key, platform in platforms_config.items():
        platform_items.append(
            {
                "id": key,
                "name": key,
                "enabled": bool(platform.get("enabled", False)),
                "model": platform.get("api_model", "") or platform.get("api_key", "")[:8],
                "userDataDir": platform.get("user_data_dir", ""),
            }
        )
    return platform_items


def _build_snapshot_task_items(
    tasks: list[dict[str, Any]],
    *,
    active_mode: str,
    scheduler_config: dict[str, Any] | None,
    platform_display_name: Callable[[str], str],
    day_status_loader: Callable[[dict[str, Any]], dict[str, Any]] = get_task_day_status,
    schedule_describer: Callable[[dict[str, Any], dict[str, Any] | None], str] = describe_task_schedule,
    status_labeler: Callable[[str], str] = get_task_status_label,
) -> list[dict[str, Any]]:
    tasks_payload: list[dict[str, Any]] = []
    for task in tasks:
        task_id = task.get("task_id") or derive_task_id(task)
        task_name = str(task.get("name") or derive_task_id(task)).strip()
        task_status_payload = day_status_loader(task)
        task_status = str(task_status_payload.get("status", "pending") or "pending").strip()
        tasks_payload.append(
            {
                "id": task_id,
                "name": task_name,
                "brand": task.get("brand", ""),
                "mode": active_mode,
                "schedule": schedule_describer(task, scheduler_config or {}),
                "status": task_status,
                "statusLabel": status_labeler(task_status),
                "statusSource": str(task_status_payload.get("source") or "").strip(),
                "testStatus": str(task_status_payload.get("test_status") or "").strip(),
                "testStatusSource": str(task_status_payload.get("test_source") or "").strip(),
                "platforms": sorted(
                    {
                        platform_display_name(platform)
                        for kw in (task.get("keywords") or [])
                        for platform in (kw.get("platforms") or [])
                        if str(platform).strip()
                    }
                ),
                "optimization_start_date": task.get("optimization_start_date", ""),
                "optimization_end_date": task.get("optimization_end_date", ""),
            }
        )
    return tasks_payload


def _collect_snapshot_tag_options(tasks: list[dict[str, Any]]) -> dict[str, list[Any]]:
    return {
        "industryTags": sorted({
            tag for task in tasks
            for tag in (task.get("industry_tags") or [])
            if str(tag).strip()
        }),
        "regionTags": sorted({
            tag for task in tasks
            for tag in (task.get("region_tags") or [])
            if str(tag).strip()
        }),
    }
