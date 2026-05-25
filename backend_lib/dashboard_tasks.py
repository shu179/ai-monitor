"""Dashboard task status helpers for the local web backend."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

from core.daily_task_state import derive_task_id, get_task_day_status
from core.scheduler import normalize_weekly_times
from core.scheduler_state import get_entry as get_scheduler_state_entry
from core.task_results import load_today_success_only_query_results, screenshot_path_exists
from core.time_utils import local_today


DEFAULT_TASK_PLATFORM_LABELS: dict[str, str] = {
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

DEFAULT_TASK_MODE_TITLES: dict[str, str] = {
    "browser": "抓取模式",
    "recognition": "识别模式",
}


def _display_platform_name(platform: str) -> str:
    text = str(platform or "").strip()
    return DEFAULT_TASK_PLATFORM_LABELS.get(text, text)


def _default_normalize_string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _collect_failed_modes_today(
    task: dict,
    *,
    today_text: str | None = None,
    scheduler_state_loader: Callable[[str], dict[str, Any]] | None = None,
    mode_title_by_key: Mapping[str, str] | None = None,
) -> tuple[list[str], str, str]:
    task_id = str(task.get("task_id") or derive_task_id(task)).strip()
    current_today_text = str(today_text or local_today().isoformat()).strip()
    load_scheduler_state = scheduler_state_loader or get_scheduler_state_entry
    mode_titles = mode_title_by_key or DEFAULT_TASK_MODE_TITLES
    failed_modes: list[str] = []
    latest_failed_at = ""
    latest_failure_kind = ""

    for mode in ("browser",):
        entry = load_scheduler_state(f"{task_id}::{mode}")
        if str(entry.get("last_auto_run_date") or "").strip() != current_today_text:
            continue
        round_status = str(entry.get("last_round_status") or "").strip()
        if round_status not in {"partial", "failed"}:
            continue
        failed_modes.append(mode_titles.get(mode, mode))
        failed_at = str(entry.get("last_auto_fail_at") or "").strip()
        if failed_at >= latest_failed_at:
            latest_failed_at = failed_at
            latest_failure_kind = str(entry.get("last_failure_kind") or "").strip()

    return failed_modes, latest_failed_at, latest_failure_kind


def _task_is_scheduled_for_day(
    task: dict,
    scheduler_config: dict | None,
    target_date: date | None = None,
) -> bool:
    if not task.get("enabled", True):
        return False

    current_date = target_date or local_today()
    start_str = task.get("optimization_start_date")
    end_str = task.get("optimization_end_date")
    if start_str:
        try:
            if current_date < date.fromisoformat(str(start_str)):
                return False
        except ValueError:
            pass
    if end_str:
        try:
            if current_date > date.fromisoformat(str(end_str)):
                return False
        except ValueError:
            pass

    weekdays_raw = task.get("weekdays", [0, 1, 2, 3, 4])
    weekdays: set[int] = set()
    for item in weekdays_raw:
        try:
            weekday = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= weekday <= 6:
            weekdays.add(weekday)
    if current_date.weekday() not in weekdays:
        return False

    weekly_times = normalize_weekly_times(scheduler_config)
    return bool(weekly_times.get(str(current_date.weekday())))


def _build_dashboard_today_task_summary(
    tasks: list[dict],
    scheduler_config: dict | None,
    target_date: date | None = None,
    *,
    day_status_loader: Callable[[dict, date | None], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    current_date = target_date or local_today()
    load_day_status = day_status_loader or get_task_day_status
    included_tasks: dict[str, dict] = {}
    total_task_ids: set[str] = set()
    completed_task_ids: set[str] = set()
    in_progress_task_ids: set[str] = set()

    for task in tasks:
        task_id = str(task.get("task_id") or derive_task_id(task)).strip()
        if not task_id:
            continue

        status = load_day_status(task, current_date)
        brand_status = str(status.get("brand_status") or "").strip()
        has_started_today = bool(status.get("formal_started")) or brand_status in {"success", "sent"}
        scheduled_today = _task_is_scheduled_for_day(task, scheduler_config, current_date)
        if not (has_started_today or scheduled_today):
            continue

        total_task_ids.add(task_id)
        included_tasks.setdefault(task_id, task)

        if brand_status in {"success", "sent"}:
            completed_task_ids.add(task_id)
            continue

        if brand_status == "running" or (brand_status == "pending" and scheduled_today):
            in_progress_task_ids.add(task_id)

    ordered_tasks = [included_tasks[task_id] for task_id in included_tasks]
    return {
        "tasks": ordered_tasks,
        "todayTaskCount": len(total_task_ids),
        "completedCount": len(completed_task_ids),
        "runningCount": len(in_progress_task_ids),
    }


def _build_dashboard_failed_tasks(
    tasks: list[dict],
    *,
    day_status_loader: Callable[[dict, date | None], dict[str, Any]] | None = None,
    scheduler_state_loader: Callable[[str], dict[str, Any]] | None = None,
    platform_display_name: Callable[[str], str] | None = None,
    mode_title_by_key: Mapping[str, str] | None = None,
    today_text: str | None = None,
) -> list[dict[str, Any]]:
    load_day_status = day_status_loader or get_task_day_status
    display_platform = platform_display_name or _display_platform_name
    failed_tasks: list[dict[str, Any]] = []
    for task in tasks:
        status = load_day_status(task, None)
        current_status = str(status.get("status") or "").strip()
        brand_status = str(status.get("brand_status") or "").strip()
        formal_started = bool(status.get("formal_started"))
        if brand_status in {"success", "sent"}:
            continue
        if brand_status == "running" or bool(status.get("formal_running")):
            continue
        # User-disabled or pending-deletion tasks should not surface their
        # earlier-today failures: the user has explicitly opted out of running
        # them, so the dashboard "failed tasks" panel must drop them.
        if not task.get("enabled", True) or bool(task.get("delete_pending")):
            continue

        task_id = str(task.get("task_id") or derive_task_id(task)).strip()
        task_name = str(task.get("name") or task_id).strip()
        status_extra = status.get("extra") if isinstance(status.get("extra"), dict) else {}
        official_extra = status.get("official_extra") if isinstance(status.get("official_extra"), dict) else {}
        failure_kind_today = str(official_extra.get("task_failure_kind") or "").strip()
        last_send_error = str(status_extra.get("last_send_error") or "").strip()
        is_notification_failure = bool(last_send_error) and not bool(status.get("has_gap"))
        is_no_screenshot_failure = (
            not is_notification_failure
            and failure_kind_today == "no_screenshot"
        )
        failed_modes_today, failed_updated_at, failure_kind_today = _collect_failed_modes_today(
            task,
            today_text=today_text,
            scheduler_state_loader=scheduler_state_loader,
            mode_title_by_key=mode_title_by_key,
        )
        failed_queries: list[dict[str, Any]] = []
        gap_details = list(status_extra.get("gap_details") or []) if formal_started else []
        sendable_screenshot_paths = _collect_successful_screenshot_paths(status.get("keyword_states") or {})
        for state in gap_details:
            keyword = str(state.get("keyword") or "").strip()
            platform = str(state.get("platform") or "").strip()
            reason = str(state.get("reason") or "").strip()
            failed_queries.append({
                "keyword": keyword,
                "platform": display_platform(platform) if platform else "",
                "mode": "正式任务",
                "error_message": {
                    "run_failed": "关键词执行失败",
                    "screenshot_save_failed": "截图保存失败",
                    "not_run": "关键词尚未执行",
                }.get(reason, "关键词未完成"),
                "ts": str(state.get("updated_at") or "").strip(),
            })
        failed_queries.sort(key=lambda item: str(item.get("ts") or ""), reverse=True)

        if not failed_queries and not failed_modes_today and not is_notification_failure and not is_no_screenshot_failure:
            continue

        issue_type = "notification" if is_notification_failure else "material" if is_no_screenshot_failure else "query"
        issue_title = (
            "企业微信发送失败，等待自动补发"
            if is_notification_failure
            else "查询已完成，但暂无可发送图片"
            if is_no_screenshot_failure
            else "查询失败，等待自动补跑"
        )
        issue_description = str(status.get("message") or "").strip()
        if not issue_description and issue_type == "query":
            issue_description = "当前仍有关键词缺口待补齐"
        elif not issue_description and issue_type == "notification":
            issue_description = last_send_error or "企业微信发送失败，等待自动补发"
        elif not issue_description and issue_type == "material":
            issue_description = "当前任务已完成，但暂无可发送图片，暂时无法补发"

        failed_tasks.append({
            "taskId": task_id,
            "taskName": task_name,
            "brand": str(task.get("brand") or task_name).strip(),
            "status": current_status or "pending",
            "statusMessage": str(status.get("message") or "").strip(),
            "failedModes": failed_modes_today,
            "failedUpdatedAt": (
                str(status.get("updated_at") or "")
                or failed_updated_at
            ),
            "failureKind": (
                "notification"
                if is_notification_failure
                else "no_screenshot"
                if is_no_screenshot_failure
                else failure_kind_today
                or "query"
            ),
            "issueType": issue_type,
            "issueTitle": issue_title,
            "issueDescription": issue_description,
            "failedQueries": failed_queries,
            "sendableSuccessCount": len(sendable_screenshot_paths),
            "canForceSendSuccess": bool(sendable_screenshot_paths),
        })

    failed_tasks.sort(
        key=lambda item: (
            item.get("failedUpdatedAt", ""),
            len(item.get("failedQueries", []) or []),
        ),
        reverse=True,
    )
    return failed_tasks


def _build_task_failure_summary(
    task: dict,
    *,
    day_status_loader: Callable[[dict, date | None], dict[str, Any]] | None = None,
    scheduler_state_loader: Callable[[str], dict[str, Any]] | None = None,
    platform_display_name: Callable[[str], str] | None = None,
    mode_title_by_key: Mapping[str, str] | None = None,
    today_text: str | None = None,
) -> dict[str, Any]:
    load_day_status = day_status_loader or get_task_day_status
    task_id = str(task.get("task_id") or derive_task_id(task)).strip()
    task_name = str(task.get("name") or task_id).strip()
    task_status = load_day_status(task, None)
    current_status = str(task_status.get("status") or "pending").strip()
    failed_tasks = _build_dashboard_failed_tasks(
        [task],
        day_status_loader=load_day_status,
        scheduler_state_loader=scheduler_state_loader,
        platform_display_name=platform_display_name,
        mode_title_by_key=mode_title_by_key,
        today_text=today_text,
    )
    if failed_tasks:
        summary = dict(failed_tasks[0])
    else:
        summary = {
            "taskId": task_id,
            "taskName": task_name,
            "brand": str(task.get("brand") or task_name).strip(),
            "status": current_status or "pending",
            "statusMessage": str(task_status.get("message") or "").strip(),
            "failedModes": [],
            "failedUpdatedAt": str(task_status.get("updated_at") or "").strip(),
            "failureKind": "",
            "issueType": "",
            "issueTitle": "",
            "issueDescription": "",
            "failedQueries": [],
        }
    summary["failedToday"] = bool(
        summary.get("status") not in {"success", "sent"}
        and (
            summary.get("failedModes")
            or summary.get("failedQueries")
            or summary.get("issueType") == "notification"
        )
    )
    return summary


def _collect_today_successful_task_payload(
    task: dict,
    *,
    day_status_loader: Callable[[dict, date | None], dict[str, Any]] | None = None,
    normalize_string_list: Callable[[Any], list[str]] | None = None,
) -> dict[str, Any]:
    load_day_status = day_status_loader or get_task_day_status
    normalize_values = normalize_string_list or _default_normalize_string_list
    task_status = load_day_status(task, None)
    task_id = str(task.get("task_id") or derive_task_id(task)).strip()
    task_name = str(task.get("name") or task_id).strip()
    fixed_screenshot_enabled = bool(task.get("fixed_screenshot_enabled", False))
    status_extra = dict(task_status.get("extra") or {})
    official_extra = dict(task_status.get("official_extra") or {}) if isinstance(task_status.get("official_extra"), dict) else {}
    keyword_states = dict(task_status.get("keyword_states") or {})
    brands = normalize_values([
        str(item.get("brand") or "").strip()
        for item in keyword_states.values()
        if str(item.get("brand") or "").strip()
    ])
    completed_keywords = []
    if not fixed_screenshot_enabled:
        completed_keywords = [
            str(item).strip()
            for item in (status_extra.get("completed_keywords") or [])
            if str(item).strip()
        ]
    if (
        not completed_keywords
        and str(task_status.get("brand_status") or "").strip() in {"success", "sent"}
        and not bool(status_extra.get("forced_ignore_failure") or official_extra.get("forced_ignore_failure"))
        and not bool(status_extra.get("completed_by_quota") or official_extra.get("completed_by_quota"))
        and not fixed_screenshot_enabled
    ):
        completed_keywords = [
            str(item).strip()
            for item in (task_status.get("required_keywords") or [])
            if str(item).strip()
        ]
    detected_platforms = []
    if not fixed_screenshot_enabled:
        detected_platforms = [
            str(item).strip()
            for item in (status_extra.get("detected_platforms") or [])
            if str(item).strip()
        ]
    status_screenshot_paths = [
        str(item).strip()
        for item in (
            status_extra.get("screenshot_paths")
            or official_extra.get("screenshot_paths")
            or []
        )
        if screenshot_path_exists(str(item).strip())
    ]
    screenshot_paths = normalize_values(status_screenshot_paths + _collect_successful_screenshot_paths(keyword_states))
    history_results = _load_today_successful_query_results(task_name, task_id)
    if history_results:
        progress_history_results = [
            item for item in history_results
            if (not fixed_screenshot_enabled) or screenshot_path_exists(str(item.get("screenshot") or "").strip())
        ]
        completed_keywords = normalize_values(
            completed_keywords
            + [str(item.get("keyword") or "").strip() for item in progress_history_results]
        )
        detected_platforms = normalize_values(
            detected_platforms
            + [str(item.get("platform") or "").strip() for item in progress_history_results]
        )
        screenshot_paths = normalize_values(
            screenshot_paths
            + [
                str(item.get("screenshot") or "").strip()
                for item in history_results
                if screenshot_path_exists(str(item.get("screenshot") or "").strip())
            ]
        )

    return {
        "taskId": task_id,
        "taskName": task_name,
        # 预留给成功详情/强制发送弹窗展示状态来源；当前发送逻辑主要消费成功明细本身。
        "status": str(task_status.get("status") or "pending").strip() or "pending",
        "statusSource": str(task_status.get("source") or "").strip(),
        "brands": brands or [str(task.get("brand") or task_name).strip() or task_name],
        "completedKeywords": completed_keywords,
        "detectedPlatforms": detected_platforms,
        "screenshotPaths": screenshot_paths,
        "actualScreenshotCount": max(int(status_extra.get("actual_screenshot_count") or 0), len(screenshot_paths)),
    }


def _compute_fixed_screenshot_target(task: dict) -> int:
    if not bool(task.get("fixed_screenshot_enabled", False)):
        return 0
    platforms = {
        str(platform).strip()
        for kw in (task.get("keywords") or [])
        for platform in (kw.get("platforms") or [])
        if str(platform).strip()
    }
    try:
        raw_target = max(
            1,
            int(task.get("fixed_screenshot_count", task.get("recognition_batch_size", 1)) or 1),
        )
    except Exception:
        raw_target = 1
    return max(raw_target, len(platforms))


def _load_today_successful_query_results(task_name: str, task_id: str) -> list[dict[str, Any]]:
    try:
        result_map = load_today_success_only_query_results(task_name, task_id=task_id)
    except Exception:
        return []
    return [dict(item) for item in (result_map or {}).values() if isinstance(item, dict)]


def _iter_successful_state_candidates(keyword_states: dict[str, Any]):
    for item in keyword_states.values():
        if not isinstance(item, dict):
            continue
        platform_states = item.get("platform_states")
        if isinstance(platform_states, dict):
            for platform_state in platform_states.values():
                if isinstance(platform_state, dict):
                    yield platform_state
        yield item


def _collect_successful_screenshot_paths(keyword_states: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for item in _iter_successful_state_candidates(keyword_states):
        path = str(item.get("image_path") or "").strip()
        if not path or path in seen:
            continue
        if not bool(item.get("run_success")) or not bool(item.get("screenshot_saved")):
            continue
        seen.add(path)
        paths.append(path)
    return paths
