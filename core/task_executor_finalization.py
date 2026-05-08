"""Finalization helpers for task execution."""

from __future__ import annotations

from core.cycle_state import resolve_report_status
from core.daily_task_state import (
    SOURCE_MODE_FORMAL,
    SOURCE_MODE_TEST,
    build_task_state_extra as build_daily_task_state_extra,
    finish_formal_task_run,
    mark_task_send_failure,
    mark_task_sent,
    write_task_status,
)
from core.task_notifications import build_notification_result as _build_notification_result
from core.task_results import (
    build_execution_report as _build_execution_report,
    build_finished_progress_payload as _build_finished_progress_payload,
    build_task_state_extra as _build_task_state_extra,
    finalize_execution_report as _finalize_execution_report,
    make_query_result_key as _make_query_result_key,
    result_has_usable_screenshot as _result_has_usable_screenshot,
)


def merge_historical_notification_results(
    *,
    all_results: list[dict],
    selected_results: list[dict],
    historical_success_map: dict,
    fixed_screenshot_enabled: bool,
) -> tuple[list[dict], list[dict]]:
    notify_all_results = all_results
    notify_selected_results = selected_results
    if not historical_success_map:
        return notify_all_results, notify_selected_results

    current_result_keys = {
        _make_query_result_key(
            item.get("keyword", ""),
            item.get("platform", ""),
            item.get("brand", ""),
        )
        for item in all_results
    }
    historical_results = [
        result
        for key, result in historical_success_map.items()
        if key not in current_result_keys
    ]
    if fixed_screenshot_enabled:
        historical_selected_only = [
            result for result in historical_results
            if _result_has_usable_screenshot(result)
        ]
    else:
        historical_selected_only = []

    if historical_results:
        notify_all_results = historical_results + all_results
        if fixed_screenshot_enabled:
            notify_selected_results = historical_selected_only + selected_results
    return notify_all_results, notify_selected_results


def apply_final_task_status(
    *,
    daily_state_task: dict,
    report: dict,
    notify_result: dict,
    execution_source: str,
) -> None:
    if report.get("task_status") == "success":
        if execution_source == "manual_test":
            write_task_status(
                daily_state_task,
                status="success",
                source=execution_source,
                scope="test",
                message="测试企业微信发送成功",
                extra=_build_task_state_extra(report, notify_result),
            )
        else:
            mark_task_sent(
                daily_state_task,
                source_mode=SOURCE_MODE_FORMAL,
                message="企业微信发送成功",
            )
            write_task_status(
                daily_state_task,
                status="success",
                source=execution_source,
                message="企业微信发送成功",
                extra=_build_task_state_extra(report, notify_result),
            )
        return

    failure_message = (
        report.get("task_failure_message")
        or report.get("failure_kind")
        or notify_result.get("error_message")
        or "存在失败查询，等待下次调度重试"
    )
    failure_status = str(report.get("task_status") or "failed").strip() or "failed"
    failure_extra = _build_task_state_extra(report, notify_result)
    if (
        execution_source != "manual_test"
        and str(report.get("task_failure_kind") or "").strip() == "notification"
    ):
        mark_task_send_failure(
            daily_state_task,
            source_mode=SOURCE_MODE_FORMAL,
            message=failure_message,
        )
    elif execution_source != "manual_test":
        finish_formal_task_run(
            daily_state_task,
            message=failure_message,
        )

    if execution_source == "manual_test":
        write_task_status(
            daily_state_task,
            status=failure_status,
            source=execution_source,
            scope="test",
            message=failure_message,
            extra=failure_extra,
        )
    else:
        write_task_status(
            daily_state_task,
            status=failure_status,
            source=execution_source,
            message=failure_message,
            extra=failure_extra,
        )


def finalize_no_pending_queries(
    *,
    task: dict,
    task_name: str,
    default_brand: str,
    notifier,
    keywords: list[dict],
    historical_success_map: dict,
    historical_selected_results: list[dict],
    fixed_screenshot_enabled: bool,
    fixed_screenshot_target: int,
    expected_query_count: int,
    sent_before_run: bool,
    daily_state_task: dict,
    execution_source: str,
    force_notify: bool,
    elapsed_seconds: float,
    total_queries: int,
    completed_queries: int,
    hit_queries: int,
    send_task_notifications,
    emit_progress,
    logger=print,
) -> tuple[list, dict]:
    notify_result = _build_notification_result(
        attempted=False,
        success=False,
        found_results=len(historical_success_map),
        error_message="",
    )
    if historical_success_map and not sent_before_run:
        notify_result = send_task_notifications(
            task,
            default_brand,
            notifier,
            keywords,
            list(historical_success_map.values()),
            selected_results=historical_selected_results,
            fixed_screenshot_target=fixed_screenshot_target,
            fixed_screenshot_ready=True,
            expected_query_count=expected_query_count,
            force_notify=force_notify,
        )

    report = _build_execution_report(task, [], elapsed_seconds, notify_result=notify_result)
    report = _finalize_execution_report(report, notify_result)
    if fixed_screenshot_enabled:
        report["selected_screenshot_results"] = len(historical_selected_results)
        report["fixed_screenshot_target"] = fixed_screenshot_target
        report["completed_by_quota"] = (
            fixed_screenshot_target > 0
            and len(historical_selected_results) >= fixed_screenshot_target
        )
        report["skipped_remaining_queries"] = 0

    if report.get("task_status") == "success":
        mark_task_sent(
            daily_state_task,
            source_mode=(SOURCE_MODE_TEST if execution_source == "manual_test" else SOURCE_MODE_FORMAL),
            message="企业微信发送成功",
        )
    elif execution_source != "manual_test":
        finish_formal_task_run(
            daily_state_task,
            message=str(report.get("task_failure_message") or "今日已无待执行关键词").strip(),
        )

    emit_progress({
        "stage": "finished",
        "task_name": task_name,
        "total_queries": total_queries,
        "completed_queries": completed_queries,
        "hit_queries": hit_queries,
        "round_status": report.get("round_status", "skipped"),
        "success": report.get("task_status") == "success",
        "completed_by_quota": bool(report.get("completed_by_quota", False)),
    })
    logger(f"[Main] 任务组跳过: task={task_name}, 今日已无待执行查询")
    return [], report


def finalize_cancelled_run(
    *,
    task: dict,
    default_brand: str,
    all_results: list[dict],
    historical_selected_results: list[dict],
    selected_results: list[dict],
    fixed_screenshot_enabled: bool,
    fixed_screenshot_target: int,
    total_queries: int,
    completed_queries: int,
    hit_queries: int,
    daily_state_task: dict,
    execution_source: str,
    stop_message: str,
    elapsed_seconds: float,
    emit_progress,
    logger=print,
) -> tuple[list[dict], dict]:
    notify_result = _build_notification_result(
        attempted=False,
        success=False,
        found_results=len([item for item in all_results if item.get("rank", 99) != 99]),
        error_message="",
    )
    report = _build_execution_report(
        task,
        all_results,
        elapsed_seconds,
        notify_result=notify_result,
    )
    report["round_status"] = "pending"
    report["query_round_status"] = "pending"
    report["task_status"] = "pending"
    report["task_failure_kind"] = "cancelled"
    report["task_failure_message"] = stop_message
    report["_scheduler_cancelled"] = True
    if fixed_screenshot_enabled:
        report["selected_screenshot_results"] = len(historical_selected_results) + len(selected_results)
        report["fixed_screenshot_target"] = fixed_screenshot_target
        report["completed_by_quota"] = (
            fixed_screenshot_target > 0
            and (len(historical_selected_results) + len(selected_results)) >= fixed_screenshot_target
        )
        report["skipped_remaining_queries"] = max(total_queries - completed_queries, 0)
    if execution_source != "manual_test":
        write_task_status(
            daily_state_task,
            status="pending",
            source=execution_source,
            message=stop_message,
            extra=build_daily_task_state_extra(
                task_status="pending",
                query_round_status="pending",
                failed_queries=report.get("failed_queries", 0),
                task_failure_kind="cancelled",
                notification_success=False,
            ),
        )
    emit_progress(_build_finished_progress_payload(
        task.get("name", default_brand),
        report,
        total_queries=total_queries,
        completed_queries=completed_queries,
        hit_queries=hit_queries,
    ))
    logger(
        f"[Main] 任务组已暂停: task={task.get('name', default_brand)}, "
        f"completed={completed_queries}/{total_queries}, hits={hit_queries}"
    )
    return all_results, report


def finalize_completed_run(
    *,
    task: dict,
    default_brand: str,
    notifier,
    keywords: list[dict],
    all_results: list[dict],
    selected_results: list[dict],
    historical_success_map: dict,
    historical_keyword_states: dict | None,
    historical_selected_results: list[dict],
    fixed_screenshot_enabled: bool,
    fixed_screenshot_target: int,
    fixed_screenshot_ready: bool,
    expected_query_count: int,
    total_queries: int,
    completed_queries: int,
    hit_queries: int,
    daily_state_task: dict,
    execution_source: str,
    elapsed_seconds: float,
    force_notify: bool,
    send_task_notifications,
    finalize_daily_pool_keyword_results,
    emit_issue,
    emit_progress,
    logger=print,
) -> tuple[list[dict], dict]:
    finalize_daily_pool_keyword_results(
        daily_state_task,
        all_results=all_results,
        execution_source=execution_source,
        historical_keyword_states=historical_keyword_states,
    )

    notify_all_results, notify_selected_results = merge_historical_notification_results(
        all_results=all_results,
        selected_results=selected_results,
        historical_success_map=historical_success_map,
        fixed_screenshot_enabled=fixed_screenshot_enabled,
    )

    notify_result = send_task_notifications(
        task,
        default_brand,
        notifier,
        keywords,
        notify_all_results,
        selected_results=notify_selected_results,
        fixed_screenshot_target=fixed_screenshot_target,
        fixed_screenshot_ready=fixed_screenshot_ready,
        expected_query_count=expected_query_count,
        force_notify=force_notify,
    )
    if bool(notify_result.get("attempted")) and not bool(notify_result.get("success")):
        emit_issue({
            "title": "企业微信发送异常",
            "task_name": task.get("name", default_brand),
            "mode": str(task.get("_scheduler_mode") or "").strip(),
            "brand": default_brand,
            "message": str(notify_result.get("error_message") or "企业微信发送未成功").strip(),
        })

    report = _build_execution_report(
        task,
        all_results,
        elapsed_seconds,
        notify_result=notify_result,
    )
    report = _finalize_execution_report(report, notify_result)
    if fixed_screenshot_enabled:
        report["selected_screenshot_results"] = len(historical_selected_results) + len(selected_results)
        report["fixed_screenshot_target"] = fixed_screenshot_target
        report["completed_by_quota"] = fixed_screenshot_ready
        report["skipped_remaining_queries"] = max(total_queries - completed_queries, 0)

    apply_final_task_status(
        daily_state_task=daily_state_task,
        report=report,
        notify_result=notify_result,
        execution_source=execution_source,
    )
    emit_progress(_build_finished_progress_payload(
        task.get("name", default_brand),
        report,
        total_queries=total_queries,
        completed_queries=completed_queries,
        hit_queries=hit_queries,
    ))
    logger(
        f"[Main] 任务组完成: task={task.get('name', default_brand)}, "
        f"query_round_status={report.get('query_round_status', report.get('round_status', 'success'))}, "
        f"task_status={resolve_report_status(report)}, "
        f"completed={completed_queries}/{total_queries}, hits={hit_queries}"
    )
    return all_results, report


__all__ = [
    "apply_final_task_status",
    "finalize_cancelled_run",
    "finalize_completed_run",
    "finalize_no_pending_queries",
    "merge_historical_notification_results",
]
