from __future__ import annotations

from core.task_results import (
    collect_unique_screenshot_paths,
    count_task_queries,
    is_success_result,
    make_query_result_key,
    result_has_usable_screenshot,
)


def record_diagnostic(
    task_name: str,
    keyword: str,
    platform_name: str,
    brand: str,
    message: str,
    *,
    category: str,
    details: dict | None = None,
) -> str:
    from core.diagnostics import record_event

    event = record_event(
        category=category,
        message=message,
        task_name=task_name,
        keyword=keyword,
        platform=platform_name,
        brand=brand,
        details=details or {},
    )
    return str(event.get('id', '')).strip()


def is_success_record(record: dict) -> bool:
    """判断一条历史记录是否表示本次查询命中品牌。"""
    from core.history import is_success_record as history_is_success_record

    return history_is_success_record(record)


def should_send_single_query_notification(task_name: str, *, task_id: str = '') -> bool:
    """
    单查询任务只在“当天首次识别到”时发通知。
    同一天内仅在未命中 -> 命中 时发送；跨天后的首次命中允许再次通知。
    """
    try:
        from core.history import get_records

        records = get_records(task_name, task_id=task_id)
        if not records:
            return False

        current = records[-1]
        if not is_success_record(current):
            return False

        if len(records) == 1:
            return True

        current_date = str(current.get('ts', ''))[:10]
        previous = records[-2]
        previous_date = str(previous.get('ts', ''))[:10]

        if previous_date != current_date:
            return True

        return not is_success_record(previous)
    except Exception as e:
        print(f"[Main] 单查询通知状态判断失败，回退为本次命中即通知: {e}")
        return True


def build_notification_result(
    *,
    attempted: bool,
    success: bool,
    found_results: int,
    error_message: str = '',
) -> dict:
    return {
        'attempted': bool(attempted),
        'success': bool(success),
        'found_results': max(0, int(found_results or 0)),
        'error_message': str(error_message or ''),
    }


def send_fixed_screenshot_task_notification(
    *,
    task_name: str,
    notifier,
    selected_results: list,
    fixed_screenshot_target: int,
    fixed_screenshot_ready: bool,
) -> dict:
    effective_results = [
        item for item in (selected_results or [])
        if item.get('rank', 99) != 99 and result_has_usable_screenshot(item)
    ]
    screenshot_paths = collect_unique_screenshot_paths(effective_results)
    if not effective_results:
        print(f"[Main] 固定截图任务组「{task_name}」暂无可发送截图，继续等待补齐")
        return build_notification_result(
            attempted=False,
            success=False,
            found_results=0,
            error_message='暂无可发送截图，未发送企业微信',
        )
    if fixed_screenshot_target > 0 and not fixed_screenshot_ready:
        print(
            f"[Main] 固定截图任务组「{task_name}」截图未补满，暂不发送 "
            f"({len(screenshot_paths)}/{fixed_screenshot_target})"
        )
        return build_notification_result(
            attempted=False,
            success=False,
            found_results=len(screenshot_paths),
            error_message=f'截图未补满（{len(screenshot_paths)}/{fixed_screenshot_target}），未发送企业微信',
        )
    if fixed_screenshot_target > 0 and len(screenshot_paths) < fixed_screenshot_target:
        print(
            f"[Main] 固定截图任务组「{task_name}」有效截图未补满，暂不发送 "
            f"({len(screenshot_paths)}/{fixed_screenshot_target})"
        )
        return build_notification_result(
            attempted=False,
            success=False,
            found_results=len(screenshot_paths),
            error_message=f'有效截图未补满（{len(screenshot_paths)}/{fixed_screenshot_target}），未发送企业微信',
        )
    print(
        f"[Main] 固定截图任务组「{task_name}」入选 {len(effective_results)} 条，"
        f"准备发送 {len(screenshot_paths)} 张截图"
    )
    ok = notifier.send_detected_images(
        task_name=task_name,
        brands=[item.get('brand', '') for item in effective_results],
        screenshot_paths=screenshot_paths,
        detected_platforms=[item.get('platform', '') for item in effective_results],
        source='固定截图策略',
        completed_keywords=[item.get('keyword', '') for item in effective_results],
        total_screenshot_count=len(screenshot_paths),
    )
    if not ok:
        first_hit = effective_results[0]
        record_diagnostic(
            task_name,
            first_hit['keyword'],
            first_hit['platform'],
            first_hit['brand'],
            notifier.last_error or "企业微信固定截图发送失败",
            category='notification',
            details={'selected_result_count': len(effective_results), 'screenshot_count': len(screenshot_paths)},
        )
    return build_notification_result(
        attempted=True,
        success=ok,
        found_results=len(screenshot_paths),
        error_message=notifier.last_error or "",
    )


def send_single_query_task_notification(
    *,
    task_name: str,
    task_id: str,
    notifier,
    found_results: list,
    force_notify: bool,
) -> dict:
    if force_notify:
        print(f"[Main] 单查询任务组「{task_name}」强制发送通知")
    elif not should_send_single_query_notification(task_name, task_id=task_id):
        print(f"[Main] 单查询任务组「{task_name}」持续命中，跳过重复通知")
        return build_notification_result(
            attempted=False,
            success=False,
            found_results=len(found_results),
        )

    first_hit = found_results[0]
    print(f"[Main] 单查询任务组「{task_name}」首次识别到品牌，发送截图通知")
    ok = notifier.send(
        platform=first_hit['platform'],
        keyword=first_hit['keyword'],
        brand=first_hit['brand'],
        rank=first_hit['rank'],
        screenshot_path=first_hit.get('screenshot'),
        references=first_hit.get('references', []),
        body_references=first_hit.get('body_references', []),
        bypass_cooldown=force_notify,
    )
    if not ok and notifier.last_skip_reason == 'cooldown':
        print(f"[Main] 单查询任务组「{task_name}」命中结果仍在通知冷却期，跳过发送")
        return build_notification_result(
            attempted=False,
            success=False,
            found_results=len(found_results),
        )
    if not ok:
        record_diagnostic(
            task_name,
            first_hit['keyword'],
            first_hit['platform'],
            first_hit['brand'],
            notifier.last_error or "企业微信发送失败",
            category='notification',
            details={'screenshot': first_hit.get('screenshot')},
        )
    return build_notification_result(
        attempted=True,
        success=ok,
        found_results=len(found_results),
        error_message=notifier.last_error or "",
    )


def send_multi_query_task_notification(
    *,
    task_name: str,
    notifier,
    all_results: list,
    found_results: list,
    query_count: int,
) -> dict:
    print(f"[Main] 多查询任务组「{task_name}」命中 {len(found_results)}/{query_count}，发送汇总通知")
    ok = notifier.send_summary(task_name=task_name, results=all_results)
    if not ok:
        first_hit = found_results[0]
        record_diagnostic(
            task_name,
            first_hit['keyword'],
            first_hit['platform'],
            first_hit['brand'],
            notifier.last_error or "企业微信汇总发送失败",
            category='notification',
            details={'result_count': len(found_results)},
        )
    return build_notification_result(
        attempted=True,
        success=ok,
        found_results=len(found_results),
        error_message=notifier.last_error or "",
    )


def send_task_notifications(
    task: dict,
    default_brand: str,
    notifier,
    keywords: list,
    all_results: list,
    selected_results: list | None = None,
    fixed_screenshot_target: int = 0,
    fixed_screenshot_ready: bool = False,
    expected_query_count: int = 0,
    force_notify: bool = False,
) -> dict:
    """按任务规模分流通知：多查询走汇总，单查询只发首次命中截图。"""
    if not all_results:
        return build_notification_result(attempted=False, success=False, found_results=0)

    found_results = [r for r in all_results if r['rank'] != 99]
    task_name = task.get('name', default_brand)
    fixed_screenshot_enabled = bool(task.get('fixed_screenshot_enabled', False))
    selected_results = [r for r in (selected_results or []) if r.get('rank', 99) != 99]
    query_count = expected_query_count or count_task_queries(keywords, default_brand)
    success_query_keys = {
        make_query_result_key(
            item.get('keyword', ''),
            item.get('platform', ''),
            item.get('brand', ''),
        )
        for item in all_results
        if is_success_result(item) and result_has_usable_screenshot(item)
    }

    if query_count > 1 and len(success_query_keys) < query_count:
        print(
            f"[Main] 任务组「{task_name}」仍有未命中/失败查询待补齐，暂不发送企业微信 "
            f"({len(success_query_keys)}/{query_count})"
        )
        return build_notification_result(
            attempted=False,
            success=False,
            found_results=len(found_results),
            error_message=f'任务组未补齐（{len(success_query_keys)}/{query_count}），未发送企业微信',
        )

    if not notifier:
        effective_results = selected_results if fixed_screenshot_enabled else found_results
        if effective_results:
            print(f"[Main] 任务组「{task_name}」命中 {len(effective_results)} 条，但未配置有效 webhook，跳过发送")
        return build_notification_result(
            attempted=bool(effective_results),
            success=False,
            found_results=len(effective_results),
            error_message='未配置有效 webhook',
        )

    if not found_results:
        return build_notification_result(attempted=False, success=False, found_results=0)

    if fixed_screenshot_enabled:
        return send_fixed_screenshot_task_notification(
            task_name=task_name,
            notifier=notifier,
            selected_results=selected_results,
            fixed_screenshot_target=fixed_screenshot_target,
            fixed_screenshot_ready=fixed_screenshot_ready,
        )

    if query_count <= 1:
        return send_single_query_task_notification(
            task_name=task_name,
            task_id=str(task.get('_scheduler_original_task_id') or task.get('task_id') or '').strip(),
            notifier=notifier,
            found_results=found_results,
            force_notify=force_notify,
        )

    return send_multi_query_task_notification(
        task_name=task_name,
        notifier=notifier,
        all_results=all_results,
        found_results=found_results,
        query_count=query_count,
    )


_record_diagnostic = record_diagnostic
_is_success_record = is_success_record
_should_send_single_query_notification = should_send_single_query_notification
_build_notification_result = build_notification_result
_send_fixed_screenshot_task_notification = send_fixed_screenshot_task_notification
_send_single_query_task_notification = send_single_query_task_notification
_send_multi_query_task_notification = send_multi_query_task_notification
_send_task_notifications = send_task_notifications


__all__ = [
    "build_notification_result",
    "is_success_record",
    "record_diagnostic",
    "send_fixed_screenshot_task_notification",
    "send_multi_query_task_notification",
    "send_single_query_task_notification",
    "send_task_notifications",
    "should_send_single_query_notification",
]
