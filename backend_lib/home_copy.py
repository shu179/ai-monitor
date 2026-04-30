"""Home-screen copy helpers for the local web backend."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from core.time_utils import local_now


DEFAULT_MODE_KEY_TO_TITLE: dict[str, str] = {
    "browser": "抓取模式",
    "recognition": "识别模式",
    "api": "保险模式",
    "smart": "智能模式",
}

_HOME_COPY_SKIP_TOKENS = (
    "早上好",
    "中午好",
    "下午好",
    "晚上好",
    "夜深了",
    "当前默认停留在",
    "当前处于",
    "主页提醒会跟随模式状态自动变化",
    "启动监控后会自动刷新互动提醒",
    "抓取模式适合",
    "保险模式走平台 API",
    "识别模式会低负载监听",
    "智能模式由 AI 接管",
)


def _home_greeting(now: datetime) -> str:
    hour = now.hour
    if hour < 6:
        return "早点休息"
    if hour < 11:
        return "早上好"
    if hour < 14:
        return "中午好"
    if hour < 18:
        return "下午好"
    return "晚上好"


def _default_home_headline(
    *,
    monitoring_running: bool,
    enabled_task_count: int,
    total_task_count: int,
    failed_task_count: int,
    today_record_count: int,
    hit_record_count: int,
) -> str:
    if failed_task_count > 0:
        return f"今天有 {failed_task_count} 个任务需要补跑或补发，建议优先处理。"
    if monitoring_running and enabled_task_count > 0:
        return (
            f"今天已启用 {enabled_task_count} 个品牌任务，"
            f"当前已累计 {max(today_record_count, hit_record_count, 0)} 条运行记录。"
        )
    if enabled_task_count > 0:
        return f"当前已录入 {total_task_count or enabled_task_count} 个品牌任务，其中 {enabled_task_count} 个处于启用状态。"
    if total_task_count > 0:
        return f"当前已录入 {total_task_count} 个品牌任务，开启监控后会按计划继续运行。"
    return "当前还没有录入品牌任务，可以先新增一个需要监控的品牌。"


def _build_dashboard_home_copy(
    *,
    config: dict[str, Any],
    mode_key: str,
    monitoring_running: bool,
    recognition_status: dict[str, Any] | None = None,
    enabled_task_count: int = 0,
    total_task_count: int = 0,
    failed_task_count: int = 0,
    today_record_count: int = 0,
    hit_record_count: int = 0,
    mode_title_by_key: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    current_time = now or local_now()
    greeting = _home_greeting(current_time)
    default_headline = _default_home_headline(
        monitoring_running=monitoring_running,
        enabled_task_count=enabled_task_count,
        total_task_count=total_task_count,
        failed_task_count=failed_task_count,
        today_record_count=today_record_count,
        hit_record_count=hit_record_count,
    )

    try:
        from ui.home_copy import build_home_copy_context, get_home_messages

        titles = mode_title_by_key or DEFAULT_MODE_KEY_TO_TITLE
        context = build_home_copy_context(
            config=config,
            current_mode=titles.get(str(mode_key or "").strip(), "抓取模式"),
            mode_notice="",
            running=monitoring_running,
            recognition_status=recognition_status or {},
            now=current_time,
        )
        messages = [str(item or "").strip() for item in get_home_messages(context) if str(item or "").strip()]
        headline = next(
            (
                message
                for message in messages
                if not any(token in message for token in _HOME_COPY_SKIP_TOKENS)
            ),
            "",
        )
        return {
            "greeting": greeting,
            "headline": headline or default_headline,
        }
    except Exception:
        return {
            "greeting": greeting,
            "headline": default_headline,
        }
