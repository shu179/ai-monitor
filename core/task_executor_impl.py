"""Task execution implementation shared by desktop and Web UI entrypoints."""

from __future__ import annotations

import time

from core.daily_task_state import (
    build_task_state_extra as build_daily_task_state_extra,
    get_task_day_status,
    is_task_sent_today,
    write_task_status,
)
from core.notifier import WeComNotifier
from core.platform_sessions import (
    PlatformSessionManager,
    build_query_execution_policy,
    build_round_query_plan,
)
from core.task_executor_api import _run_api_task
from core.task_executor_browser import (
    _apply_browser_runtime_config,
    _create_browser_platform,
    _should_use_session_pool_for_query,
    _sync_reused_platform_runtime_state,
)
from core.task_executor_dispatch import dispatch_task_queries
from core.task_executor_finalization import (
    finalize_cancelled_run,
    finalize_completed_run,
    finalize_no_pending_queries,
)
from core.task_executor_plan import (
    build_task_execution_plan,
    historical_query_result as _planned_historical_query_result,
)
from core.task_executor_query import execute_task_query
from core.task_executor_smart import _run_smart_browser_task
from core.task_notifications import (
    record_diagnostic as _record_diagnostic,
    send_task_notifications as _send_task_notifications,
)
from core.task_results import (
    build_execution_report as _build_execution_report,
    classify_result_outcome as _classify_result_outcome,
    finalize_daily_pool_keyword_results as _finalize_daily_pool_keyword_results,
    is_success_result as _is_success_result,
    load_today_success_only_query_results as _load_today_success_only_query_results,
    make_query_result_key as _make_query_result_key,
    record_result_history as _record_result_history,
    result_has_usable_screenshot as _result_has_usable_screenshot,
    task_for_daily_state as _task_for_daily_state,
)
from core.time_utils import local_now
from platforms.base import SchedulerStopRequested


def run_task_group(
    task: dict,
    default_notify_config: dict,
    config: dict = None,
    force_notify: bool = False,
    execution_source: str = 'auto',
    return_report: bool = False,
    progress_callback=None,
    issue_callback=None,
    stop_checker=None,
    platform_session_manager: PlatformSessionManager | None = None,
) -> list | tuple[list, dict]:
    """
    执行任务组（多关键词 × 多平台），返回所有结果列表
    results: [{'keyword', 'platform', 'brand', 'rank', 'screenshot'}, ...]
    """
    # 向后兼容：旧格式单 keyword+platform+brand → 转为 keywords 列表
    if 'keyword' in task and 'keywords' not in task:
        task = {**task, 'keywords': [{'keyword': task['keyword'], 'brand': task.get('brand', ''), 'platforms': [task['platform']]}]}

    started_at = time.monotonic()
    run_started_at_text = local_now().isoformat(timespec='seconds')

    keywords = task.get('keywords', [])
    if not keywords:
        report = _build_execution_report(task, [], time.monotonic() - started_at)
        return ([], report) if return_report else []

    runtime_mode = str((config or {}).get('detection_mode') or '').strip()
    if runtime_mode not in {'browser', 'api', 'smart', 'recognition'}:
        runtime_mode = str(task.get('_scheduler_mode') or '').strip()
    if runtime_mode not in {'browser', 'api', 'smart', 'recognition'}:
        runtime_mode = ''
    owns_session_manager = False
    if platform_session_manager is None and runtime_mode in {"browser", "smart"}:
        policy = build_query_execution_policy(config or {}, runtime_mode)
        if policy.use_session_pool:
            platform_session_manager = PlatformSessionManager(
                runtime_mode,
                policy,
                build_round_query_plan([task], runtime_mode),
                logger=print,
            )
            owns_session_manager = True

    # 默认品牌取第一个关键词的 brand
    default_brand = keywords[0].get('brand', '') if keywords else ''
    task_name = task.get('name', default_brand)
    history_task_id = str(task.get('_scheduler_original_task_id') or task.get('task_id') or '').strip()
    cloud_task_id = task.get('cloud_task_id') or task.get('cloudTaskId')
    daily_state_task = _task_for_daily_state(task)
    day_status = get_task_day_status(daily_state_task)
    historical_keyword_states = dict(
        day_status.get('test_keyword_states')
        if execution_source == 'manual_test'
        else day_status.get('keyword_states')
        or {}
    )
    historical_success_map = _load_today_success_only_query_results(
        task_name,
        task_id=history_task_id,
    )
    sent_before_run = is_task_sent_today(daily_state_task)
    # 手动测试始终整组重跑，且不受“今日已发送”影响。
    manual_test_replay_completed_keywords = execution_source == 'manual_test'
    if manual_test_replay_completed_keywords:
        historical_success_map = {}
    if execution_source == 'auto' and sent_before_run:
        report = _build_execution_report(task, [], time.monotonic() - started_at)
        report['round_status'] = 'skipped'
        report['query_round_status'] = 'skipped'
        report['task_status'] = 'success'
        report['task_failure_kind'] = ''
        report['task_failure_message'] = ''
        if return_report:
            return [], report
        return []
    completed_queries = 0
    hit_queries = 0
    stop_message = "定时任务已关闭，当前任务已暂停，等待重新开启后继续执行"
    scheduler_cancelled = False
    stop_notice_emitted = False

    def _emit_progress(payload: dict) -> None:
        if not progress_callback:
            return
        try:
            progress_callback(payload)
        except Exception as e:
            print(f"[Main] 测试进度回调失败: {e}")

    def _emit_issue(payload: dict) -> None:
        if not issue_callback:
            return
        try:
            issue_callback(payload)
        except Exception as e:
            print(f"[Main] 问题通知回调失败: {e}")

    def _notify_issue_for_result(result: dict) -> None:
        outcome = _classify_result_outcome(result)
        if outcome not in {'structural_error', 'temporary_error'}:
            return
        _emit_issue({
            'title': '任务执行异常',
            'task_name': task_name,
            'mode': str(result.get('mode') or '').strip(),
            'keyword': str(result.get('keyword') or '').strip(),
            'platform': str(result.get('platform') or '').strip(),
            'brand': str(result.get('brand') or '').strip(),
            'message': str(result.get('error_message') or '任务执行失败').strip(),
        })

    def _record_query_history_safely(result: dict) -> None:
        try:
            _record_result_history(
                task_name,
                result,
                execution_source=execution_source,
                task_id=history_task_id,
                cloud_task_id=cloud_task_id,
                run_started_at=run_started_at_text,
            )
        except Exception as he:
            print(f"[Main] 历史记录失败: {he}")

    def _complete_query_result(
        result: dict,
        *,
        error_message: str | None = None,
        record_history: bool = True,
    ) -> dict:
        nonlocal completed_queries, hit_queries
        all_results.append(result)
        completed_queries += 1
        success = result.get('rank', 99) != 99
        if success:
            hit_queries += 1
        _notify_issue_for_result(result)
        _emit_progress({
            "stage": "query_done",
            "task_name": task_name,
            "keyword": str(result.get('keyword') or '').strip(),
            "brand": str(result.get('brand') or '').strip(),
            "platform": str(result.get('platform') or '').strip(),
            "current_query": completed_queries,
            "total_queries": total_queries,
            "completed_queries": completed_queries,
            "hit_queries": hit_queries,
            "success": success,
            "error_message": str(error_message if error_message is not None else result.get('error_message', '') or ''),
        })
        if record_history:
            _record_query_history_safely(result)
        return result

    def _should_stop_remaining_work() -> bool:
        nonlocal scheduler_cancelled, stop_notice_emitted
        if not callable(stop_checker):
            return False
        try:
            should_stop = bool(stop_checker())
        except Exception:
            should_stop = False
        if not should_stop:
            return False
        scheduler_cancelled = True
        if not stop_notice_emitted:
            print(f"[Main] {stop_message}: task={task_name}")
            _emit_progress({
                "stage": "paused",
                "task_name": task_name,
                "total_queries": total_queries,
                "completed_queries": completed_queries,
                "hit_queries": hit_queries,
                "message": stop_message,
            })
            stop_notice_emitted = True
        return True

    # 获取 webhook
    webhook_url = task.get('webhook_url', '')

    notifier = None
    if webhook_url and 'YOUR_KEY_HERE' not in webhook_url:
        notifier = WeComNotifier(
            webhook_url=webhook_url,
            cooldown_minutes=default_notify_config.get('cooldown_minutes', 30),
            send_interval=default_notify_config.get('send_interval', 2)
        )

    all_results = []
    selected_results = []
    plan = build_task_execution_plan(
        task=task,
        keywords=keywords,
        default_brand=default_brand,
        runtime_mode=runtime_mode,
        historical_success_map=historical_success_map,
        manual_test_replay_completed_keywords=manual_test_replay_completed_keywords,
    )
    fixed_screenshot_enabled = plan.fixed_screenshot_enabled
    executable_entries = plan.executable_entries
    ordered_platforms = plan.ordered_platforms
    entries_by_platform = plan.entries_by_platform
    total_queries = plan.total_queries
    expected_query_count = plan.expected_query_count
    fixed_screenshot_target = plan.fixed_screenshot_target
    historical_selected_results = plan.historical_selected_results
    historical_completed_keywords = plan.historical_completed_keywords
    historical_detected_platforms = plan.historical_detected_platforms
    historical_brands = plan.historical_brands

    def _record_session_pool_skip(platform_name: str, mode: str) -> None:
        if not _should_use_session_pool_for_query(mode, task, platform_session_manager):
            return
        try:
            platform_session_manager.record_query_skipped(platform_name)
        except Exception as e:
            print(f"[Main] 复用会话跳过记录失败 ({platform_name}): {e}")

    def _historical_query_result(entry: dict, platform_name: str) -> dict | None:
        return _planned_historical_query_result(
            entry,
            platform_name,
            historical_success_map=historical_success_map,
            fixed_screenshot_enabled=fixed_screenshot_enabled,
            manual_test_replay_completed_keywords=manual_test_replay_completed_keywords,
        )

    daily_state_task = _task_for_daily_state(task)
    if total_queries > 0 and execution_source != 'manual_test':
        current_day_status = str(get_task_day_status(daily_state_task).get('status') or '').strip()
        if current_day_status != 'success':
            write_task_status(
                daily_state_task,
                status='running',
                source=execution_source,
                message='任务执行中',
                extra=build_daily_task_state_extra(
                    brands=historical_brands,
                    completed_keywords=historical_completed_keywords,
                    detected_platforms=historical_detected_platforms,
                    found_results=len(historical_success_map),
                    image_count=len(historical_selected_results),
                    selected_screenshot_results=len(historical_selected_results),
                    fixed_screenshot_target=fixed_screenshot_target,
                    completed_by_quota=(
                        fixed_screenshot_enabled
                        and fixed_screenshot_target > 0
                        and (len(historical_selected_results) + len(selected_results)) >= fixed_screenshot_target
                    ),
                    task_status='running',
                    query_round_status='running',
                ),
            )

    _emit_progress({
        "stage": "started",
        "task_name": task_name,
        "total_queries": total_queries,
        "completed_queries": completed_queries,
        "hit_queries": hit_queries,
    })

    def _append_selected_result(result: dict) -> None:
        if not fixed_screenshot_enabled:
            return
        if result.get('rank', 99) == 99:
            return
        if not _result_has_usable_screenshot(result):
            print(
                f"[Main] 固定截图任务命中但截图不可用，保留查询结果但不计入截图 quota: "
                f"task={task_name}, keyword={result.get('keyword', '')}, platform={result.get('platform', '')}"
            )
            return
        if fixed_screenshot_target > 0 and (len(historical_selected_results) + len(selected_results)) >= fixed_screenshot_target:
            return
        selected_results.append(result)

    def _fixed_target_reached() -> bool:
        return (
            fixed_screenshot_enabled
            and fixed_screenshot_target > 0
            and (len(historical_selected_results) + len(selected_results)) >= fixed_screenshot_target
        )

    if total_queries <= 0:
        empty_results, report = finalize_no_pending_queries(
            task=task,
            task_name=task_name,
            default_brand=default_brand,
            notifier=notifier,
            keywords=keywords,
            historical_success_map=historical_success_map,
            historical_selected_results=historical_selected_results,
            fixed_screenshot_enabled=fixed_screenshot_enabled,
            fixed_screenshot_target=fixed_screenshot_target,
            expected_query_count=expected_query_count,
            sent_before_run=sent_before_run,
            daily_state_task=daily_state_task,
            execution_source=execution_source,
            force_notify=force_notify,
            elapsed_seconds=time.monotonic() - started_at,
            total_queries=total_queries,
            completed_queries=completed_queries,
            hit_queries=hit_queries,
            send_task_notifications=_send_task_notifications,
            emit_progress=_emit_progress,
        )
        return ([], report) if return_report else []

    def _execute_query(entry: dict, platform_name: str) -> dict | None:
        nonlocal completed_queries, hit_queries
        keyword = entry['keyword']
        brand = entry['brand']
        kw_entry = entry['kw_entry']
        mode = entry['mode']
        if _should_stop_remaining_work():
            raise SchedulerStopRequested(stop_message)
        current_query_key = _make_query_result_key(keyword, platform_name, brand)
        existing_query_results = [
            item
            for item in all_results
            if _make_query_result_key(
                item.get('keyword', ''),
                item.get('platform', ''),
                item.get('brand', ''),
            ) == current_query_key
        ]
        if any(_is_success_result(item) and _result_has_usable_screenshot(item) for item in existing_query_results):
            print(
                f"[Main] 跳过已在本轮完成的查询: task={task_name}, keyword={keyword}, "
                f"brand={brand}, platform={platform_name}"
            )
            return None
        skipped_history_result = _historical_query_result(entry, platform_name)
        if skipped_history_result is not None:
            print(
                f"[Main] 跳过已完成查询: task={task_name}, keyword={keyword}, "
                f"brand={brand}, mode={mode}, platform={platform_name}"
            )
            _record_session_pool_skip(platform_name, mode)
            return None

        current_query = completed_queries + 1
        _emit_progress({
            "stage": "query_start",
            "task_name": task_name,
            "keyword": keyword,
            "brand": brand,
            "platform": platform_name,
            "current_query": current_query,
            "total_queries": total_queries,
            "completed_queries": completed_queries,
            "hit_queries": hit_queries,
            "mode": mode,
        })
        print(
            f"[Main] 即将执行: task={task_name}, keyword={keyword}, brand={brand}, "
            f"mode={mode}, platform={platform_name}, all_platforms={entry['platforms']}"
        )

        return execute_task_query(
            task=task,
            task_name=task_name,
            entry=entry,
            platform_name=platform_name,
            config=config,
            platform_session_manager=platform_session_manager,
            stop_checker=stop_checker,
            progress_callback=_emit_progress,
            should_stop=_should_stop_remaining_work,
            stop_message=stop_message,
            record_diagnostic=_record_diagnostic,
            complete_query_result=_complete_query_result,
            run_api_task=_run_api_task,
            run_smart_browser_task=_run_smart_browser_task,
        )

    try:
        dispatch_task_queries(
            task=task,
            default_brand=default_brand,
            runtime_mode=runtime_mode,
            keywords=keywords,
            executable_entries=executable_entries,
            ordered_platforms=ordered_platforms,
            entries_by_platform=entries_by_platform,
            fixed_screenshot_enabled=fixed_screenshot_enabled,
            fixed_screenshot_target=fixed_screenshot_target,
            historical_selected_results=historical_selected_results,
            selected_results=selected_results,
            platform_session_manager=platform_session_manager,
            should_stop=_should_stop_remaining_work,
            fixed_target_reached=_fixed_target_reached,
            append_selected_result=_append_selected_result,
            execute_query=_execute_query,
        )
    except SchedulerStopRequested:
        scheduler_cancelled = True
    finally:
        if platform_session_manager is not None:
            try:
                if owns_session_manager:
                    platform_session_manager.close_all(reason="任务组执行结束")
                else:
                    platform_session_manager.close_exhausted_sessions()
            except Exception as e:
                print(f"[Main] 复用会话清理失败: {e}")

    if _should_stop_remaining_work():
        cancelled_results, report = finalize_cancelled_run(
            task=task,
            default_brand=default_brand,
            all_results=all_results,
            historical_selected_results=historical_selected_results,
            selected_results=selected_results,
            fixed_screenshot_enabled=fixed_screenshot_enabled,
            fixed_screenshot_target=fixed_screenshot_target,
            total_queries=total_queries,
            completed_queries=completed_queries,
            hit_queries=hit_queries,
            daily_state_task=daily_state_task,
            execution_source=execution_source,
            stop_message=stop_message,
            elapsed_seconds=time.monotonic() - started_at,
            emit_progress=_emit_progress,
        )
        if return_report:
            return cancelled_results, report
        return cancelled_results

    completed_results, report = finalize_completed_run(
        task=task,
        default_brand=default_brand,
        notifier=notifier,
        keywords=keywords,
        all_results=all_results,
        selected_results=selected_results,
        historical_success_map=historical_success_map,
        historical_keyword_states=historical_keyword_states,
        historical_selected_results=historical_selected_results,
        fixed_screenshot_enabled=fixed_screenshot_enabled,
        fixed_screenshot_target=fixed_screenshot_target,
        fixed_screenshot_ready=_fixed_target_reached(),
        expected_query_count=expected_query_count,
        total_queries=total_queries,
        completed_queries=completed_queries,
        hit_queries=hit_queries,
        daily_state_task=daily_state_task,
        execution_source=execution_source,
        elapsed_seconds=time.monotonic() - started_at,
        force_notify=force_notify,
        send_task_notifications=_send_task_notifications,
        finalize_daily_pool_keyword_results=_finalize_daily_pool_keyword_results,
        emit_issue=_emit_issue,
        emit_progress=_emit_progress,
    )
    if return_report:
        return completed_results, report
    return completed_results


__all__ = [
    "_apply_browser_runtime_config",
    "_create_browser_platform",
    "_run_api_task",
    "_run_smart_browser_task",
    "_should_use_session_pool_for_query",
    "_sync_reused_platform_runtime_state",
    "run_task_group",
]
