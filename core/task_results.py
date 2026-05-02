from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path

from core.cycle_state import resolve_report_status
from core.daily_task_state import (
    SOURCE_MODE_FORMAL,
    SOURCE_MODE_TEST,
    apply_task_keyword_updates,
    build_task_state_extra as build_daily_task_state_extra,
    normalize_task_screenshot_path,
)
from core.scheduler_notifications import dedupe_non_empty as _dedupe_non_empty


def count_task_queries(keywords: list, default_brand: str) -> int:
    """统计任务组实际会执行的查询数（关键词 × 平台）。"""
    total = 0
    for kw_entry in keywords:
        keyword = kw_entry.get('keyword', '').strip()
        brand = kw_entry.get('brand', '').strip() or default_brand
        platforms = kw_entry.get('platforms', [])
        if keyword and brand and platforms:
            total += len(platforms)
    return total


def count_task_keywords(keywords: list, default_brand: str) -> int:
    total = 0
    seen: set[tuple[str, str]] = set()
    for kw_entry in keywords:
        keyword = kw_entry.get('keyword', '').strip()
        brand = kw_entry.get('brand', '').strip() or default_brand
        platforms = kw_entry.get('platforms', [])
        key = (keyword, brand)
        if keyword and brand and platforms and key not in seen:
            seen.add(key)
            total += 1
    return total


def build_result(keyword: str, platform_name: str, brand: str, rank: int, screenshot: str | None, **extra) -> dict:
    result = {
        'keyword': keyword,
        'platform': platform_name,
        'brand': brand,
        'rank': rank,
        'screenshot': screenshot,
        'answer_text': extra.get('answer_text', ''),
        'evidence': extra.get('evidence', ''),
        'error_message': extra.get('error_message', ''),
        'highlight_count': int(extra.get('highlight_count', 0) or 0),
        'mode': extra.get('mode', ''),
        'diagnostic_id': extra.get('diagnostic_id', ''),
        'recovered_manually': bool(extra.get('recovered_manually', False)),
        'references': extra.get('references', []),
        'body_references': extra.get('body_references', []),
    }
    return result


def make_query_result_key(keyword: str, platform_name: str, brand: str) -> tuple[str, str, str]:
    from core.history import normalize_platform_id

    return (
        str(keyword or '').strip(),
        normalize_platform_id(platform_name),
        str(brand or '').strip(),
    )


def result_has_usable_screenshot(result: dict | None) -> bool:
    """固定截图任务只统计实际存在的截图文件。"""
    if not isinstance(result, dict):
        return False
    screenshot = str(result.get('screenshot') or '').strip()
    return bool(screenshot) and Path(screenshot).exists()


def screenshot_path_exists(path: str | None) -> bool:
    screenshot = str(path or '').strip()
    return bool(screenshot) and Path(screenshot).exists()


def collect_unique_screenshot_paths(results: list[dict] | None) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for item in results or []:
        path = str((item or {}).get('screenshot') or '').strip()
        if not path or path in seen or not Path(path).exists():
            continue
        seen.add(path)
        paths.append(path)
    return paths


def render_api_screenshot_with_retries(
    render_func,
    *,
    text: str,
    platform_name: str,
    brand: str,
    keyword: str,
    max_retries: int = 3,
) -> str | None:
    """API 模式命中后，固定走 HTML 模版渲染，截图真实生成才算成功。"""
    for attempt in range(1, max_retries + 1):
        screenshot = render_func(text, platform_name, keyword=keyword, brand=brand)
        if screenshot_path_exists(screenshot):
            return str(screenshot)

        print(
            f"[API] 第 {attempt}/{max_retries} 次截图生成失败: "
            f"{str(screenshot or '').strip() or '未返回有效文件路径'}"
        )
        if attempt < max_retries:
            time.sleep(min(1.5 * attempt, 3.0))
    return None


def load_today_success_only_query_results(
    task_name: str,
    *,
    excluded_execution_sources: set[str] | None = None,
    task_id: str = '',
) -> dict[tuple[str, str, str], dict]:
    """
    读取当天已经成功命中的查询结果。
    只有 rank != 99 的命中结果才算“已完成”，未命中仍视为失败项，等待后续补跑。
    """
    from core.history import get_task_daily_success_query_results

    return get_task_daily_success_query_results(
        task_name,
        excluded_execution_sources=excluded_execution_sources,
        task_id=task_id,
    )


def keyword_result_key(keyword: str, brand: str) -> tuple[str, str]:
    return (str(keyword or '').strip(), str(brand or '').strip())


def build_keyword_result_index(results: list[dict] | None) -> dict[tuple[str, str], list[dict]]:
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for item in results or []:
        key = keyword_result_key(item.get('keyword', ''), item.get('brand', ''))
        if not key[0]:
            continue
        grouped[key].append(item)
    return grouped


def summarize_keyword_result(results: list[dict]) -> dict:
    success_with_screenshot = next(
        (item for item in results if is_success_result(item) and result_has_usable_screenshot(item)),
        None,
    )
    if success_with_screenshot is not None:
        return {
            'run_success': True,
            'screenshot_saved': True,
            'failure_reason': '',
            'result': success_with_screenshot,
        }
    success_without_screenshot = next(
        (item for item in results if is_success_result(item)),
        None,
    )
    if success_without_screenshot is not None:
        return {
            'run_success': True,
            'screenshot_saved': False,
            'failure_reason': 'screenshot_save_failed',
            'result': success_without_screenshot,
        }
    failed_result = results[0] if results else None
    return {
        'run_success': False,
        'screenshot_saved': False,
        'failure_reason': 'run_failed',
        'result': failed_result,
    }


def finalize_daily_pool_keyword_results(
    task: dict,
    *,
    all_results: list[dict],
    execution_source: str,
    historical_keyword_states: dict | None,
) -> dict:
    grouped = build_keyword_result_index(all_results)
    source_mode = SOURCE_MODE_TEST if execution_source == 'manual_test' else SOURCE_MODE_FORMAL
    updates: list[dict] = []
    historical_states = dict(historical_keyword_states or {})

    for key, grouped_results in grouped.items():
        keyword, brand = key
        historical_state = dict(historical_states.get(keyword) or {})
        already_complete = bool(historical_state.get('run_success')) and bool(historical_state.get('screenshot_saved'))
        summary = summarize_keyword_result(grouped_results)
        chosen_result = dict(summary.get('result') or {})

        if execution_source == 'manual_test' and not summary.get('run_success'):
            continue
        if execution_source == 'manual_test' and already_complete:
            continue

        image_path = str(chosen_result.get('screenshot') or '').strip()
        platform_name = str(chosen_result.get('platform') or '').strip()
        if summary.get('run_success') and summary.get('screenshot_saved') and image_path:
            normalized_source_mode = SOURCE_MODE_FORMAL if execution_source == 'manual_test' else source_mode
            normalized_path = normalize_task_screenshot_path(
                task,
                keyword=keyword,
                brand=brand,
                platform=platform_name,
                source_mode=normalized_source_mode,
                original_path=image_path,
            )
            for result in grouped_results:
                if str(result.get('screenshot') or '').strip() == image_path:
                    result['screenshot'] = normalized_path
            image_path = normalized_path

        updates.append({
            'keyword': keyword,
            'brand': brand,
            'run_success': bool(summary.get('run_success')),
            'screenshot_saved': bool(summary.get('screenshot_saved')),
            'failure_reason': str(summary.get('failure_reason') or '').strip(),
            'image_path': image_path,
            'platform': platform_name,
        })

    if updates:
        apply_task_keyword_updates(
            task,
            updates,
            source_mode=source_mode,
        )

    return {
        'updates': updates,
        'source_mode': source_mode,
    }


def record_result_history(task_name: str, result: dict, *, execution_source: str = '', task_id: str = '') -> None:
    from core.history import record as history_record

    if execution_source == 'manual_test' and result.get('rank', 99) == 99:
        return

    references = result.get('references', [])
    if not isinstance(references, list):
        references = []
    body_references = result.get('body_references', [])
    if not isinstance(body_references, list):
        body_references = []

    previous_index_signature = None
    build_reference_index_source_signature = None
    update_reference_index_with_records = None
    try:
        from core.article_reference_index import (
            build_reference_index_source_signature as _build_reference_index_source_signature,
            update_reference_index_with_records as _update_reference_index_with_records,
        )

        previous_index_signature = _build_reference_index_source_signature(task_name, task_id)
        build_reference_index_source_signature = _build_reference_index_source_signature
        update_reference_index_with_records = _update_reference_index_with_records
    except Exception:
        previous_index_signature = None
        build_reference_index_source_signature = None
        update_reference_index_with_records = None

    written_entry = history_record(
        task_name,
        result.get('platform', ''),
        result.get('keyword', ''),
        result.get('brand', ''),
        result.get('rank', 99),
        result.get('rank', 99) != 99,
        task_id=task_id,
        details={
            'screenshot': result.get('screenshot'),
            'answer_text': result.get('answer_text'),
            'evidence': result.get('evidence'),
            'error_message': result.get('error_message'),
            'highlight_count': result.get('highlight_count', 0),
            'diagnostic_id': result.get('diagnostic_id', ''),
            'mode': result.get('mode', ''),
            'recovered_manually': result.get('recovered_manually', False),
            'execution_source': execution_source,
            'extra': {
                'references': references,
                'body_references': body_references,
                'reference_count': len(references),
                'body_reference_count': len(body_references),
                'total_reference_count': len(references) + len(body_references),
            },
        },
    )
    if isinstance(written_entry, dict) and build_reference_index_source_signature and update_reference_index_with_records:
        try:
            current_index_signature = build_reference_index_source_signature(task_name, task_id)
            update_reference_index_with_records(
                task_name,
                task_id,
                [written_entry],
                previous_source_signature=previous_index_signature,
                current_source_signature=current_index_signature,
            )
        except Exception:
            pass


STRUCTURAL_ERROR_PATTERNS = (
    '未配置 api_key',
    '未配置 api_model',
    '未配置有效 webhook',
    '当前仅支持 api 模式或尚未接入浏览器适配',
    '不支持的平台',
    '未登录',
    '登录失效',
    'cookie',
    'token',
    'access_key',
    'secret_key',
    '鉴权',
    '账号',
)


def is_structural_error(message: str) -> bool:
    text = str(message or '').strip().lower()
    if not text:
        return False
    return any(pattern in text for pattern in STRUCTURAL_ERROR_PATTERNS)


def classify_result_outcome(result: dict) -> str:
    error_message = str(result.get('error_message') or '').strip()
    if result.get('rank', 99) != 99:
        return 'hit'
    if not error_message:
        return 'no_hit'
    if error_message.startswith('未识别到品牌名'):
        return 'no_hit'
    if is_structural_error(error_message):
        return 'structural_error'
    return 'temporary_error'


def is_success_result(result: dict) -> bool:
    return classify_result_outcome(result) == 'hit'


def collect_successful_brands(results: list[dict]) -> list[str]:
    brands = []
    for result in results:
        if not is_success_result(result):
            continue
        brands.append(str(result.get('brand') or '').strip())
    return _dedupe_non_empty(brands)


def collect_failed_query_details(results: list[dict]) -> list[dict]:
    failed_details = []
    for result in results:
        outcome = classify_result_outcome(result)
        if outcome == 'hit':
            continue
        brand = str(result.get('brand') or '').strip()
        error_message = str(result.get('error_message') or '').strip()
        if not error_message:
            error_message = f"未识别到品牌名 {brand}" if brand else "未识别到品牌"
        failed_details.append({
            'keyword': str(result.get('keyword') or '').strip(),
            'platform': str(result.get('platform') or '').strip(),
            'brand': brand,
            'mode': str(result.get('mode') or '').strip(),
            'error_message': error_message,
            'failure_type': outcome,
        })
    return failed_details


def build_execution_report(
    task: dict,
    results: list,
    duration_seconds: float,
    notify_result: dict | None = None,
) -> dict:
    task_name = task.get('name', '') or task.get('brand', '') or task.get('task_id', '')
    notify_result = notify_result or {}
    mode_values = {
        str((item or {}).get('mode') or '').strip()
        for item in results
        if str((item or {}).get('mode') or '').strip()
    }
    mode = ''
    if len(mode_values) == 1:
        mode = next(iter(mode_values))
    elif len(mode_values) > 1:
        mode = 'mixed'

    if not results:
        return {
            'task_id': task.get('task_id', ''),
            'task_name': task_name,
            'mode': mode,
            'round_status': 'skipped',
            'failure_kind': '',
            'duration_seconds': round(duration_seconds, 2),
            'recovered_manually': False,
            'attempted_queries': 0,
            'success_queries': 0,
            'failed_queries': 0,
            'hit_queries': 0,
            'no_hit_queries': 0,
            'notification_attempted': bool(notify_result.get('attempted')),
            'notification_success': bool(notify_result.get('success')),
            'notification_error': str(notify_result.get('error_message') or ''),
            'successful_brands': [],
            'failed_query_details': [],
        }

    success_queries = 0
    failed_queries = 0
    hit_queries = 0
    no_hit_queries = 0
    failure_kind = ''
    recovered_manually = False

    for result in results:
        outcome = classify_result_outcome(result)
        if result.get('recovered_manually'):
            recovered_manually = True
        if outcome == 'hit':
            success_queries += 1
            hit_queries += 1
            continue
        if outcome == 'no_hit':
            no_hit_queries += 1
            failed_queries += 1
            continue
        failed_queries += 1
        if outcome == 'structural_error':
            failure_kind = 'structural'
        elif failure_kind != 'structural':
            failure_kind = 'temporary'

    round_status = 'success'
    if failed_queries and success_queries:
        round_status = 'partial'
    elif failed_queries and not success_queries:
        round_status = 'failed'

    return {
        'task_id': task.get('task_id', ''),
        'task_name': task_name,
        'mode': mode,
        'round_status': round_status,
        'failure_kind': failure_kind,
        'duration_seconds': round(duration_seconds, 2),
        'recovered_manually': recovered_manually,
        'attempted_queries': len(results),
        'success_queries': success_queries,
        'failed_queries': failed_queries,
        'hit_queries': hit_queries,
        'no_hit_queries': no_hit_queries,
        'notification_attempted': bool(notify_result.get('attempted')),
        'notification_success': bool(notify_result.get('success')),
        'notification_error': str(notify_result.get('error_message') or ''),
        'successful_brands': collect_successful_brands(results),
        'failed_query_details': collect_failed_query_details(results),
    }


def finalize_execution_report(report: dict, notify_result: dict | None = None) -> dict:
    """补充任务最终状态，避免“查询成功但未发送”被当成整组成功。"""
    finalized = dict(report or {})
    notify_result = notify_result or {}

    query_round_status = str(finalized.get('round_status') or 'skipped').strip()
    task_status = query_round_status
    task_failure_kind = str(finalized.get('failure_kind') or '').strip()
    task_failure_message = ''

    if query_round_status == 'success' and not bool(notify_result.get('success')):
        task_status = 'failed'
        task_failure_kind = 'notification'
        task_failure_message = str(notify_result.get('error_message') or '企业微信发送未成功').strip()
    elif query_round_status != 'success':
        task_failure_message = str(notify_result.get('error_message') or '').strip()
        if not task_failure_message:
            failed_details = list(finalized.get('failed_query_details') or [])
            if failed_details:
                task_failure_message = str((failed_details[0] or {}).get('error_message') or '').strip()

    finalized['query_round_status'] = query_round_status
    finalized['task_status'] = task_status
    finalized['task_failure_kind'] = task_failure_kind
    finalized['task_failure_message'] = task_failure_message
    return finalized


def build_task_state_extra(report: dict, notify_result: dict | None = None) -> dict:
    notify_result = notify_result or {}
    return build_daily_task_state_extra(
        found_results=notify_result.get('found_results', 0),
        selected_screenshot_results=(report or {}).get('selected_screenshot_results'),
        fixed_screenshot_target=(report or {}).get('fixed_screenshot_target'),
        completed_by_quota=(report or {}).get('completed_by_quota'),
        query_round_status=(report or {}).get('query_round_status', ''),
        task_status=resolve_report_status(report, fallback=''),
        failed_queries=(report or {}).get('failed_queries', 0),
        task_failure_kind=(report or {}).get('task_failure_kind', ''),
        notification_success=bool(notify_result.get('success')),
    )


def task_for_daily_state(task: dict) -> dict:
    daily_state_task = dict(task or {})
    original_task_id = str(daily_state_task.get('_scheduler_original_task_id') or '').strip()
    if original_task_id:
        daily_state_task['task_id'] = original_task_id
    return daily_state_task


def build_finished_progress_payload(
    task_name: str,
    report: dict,
    *,
    total_queries: int,
    completed_queries: int,
    hit_queries: int,
) -> dict:
    round_status = str((report or {}).get("round_status") or "success").strip()
    task_status = resolve_report_status(report)
    return {
        "stage": "finished",
        "task_name": task_name,
        "total_queries": total_queries,
        "completed_queries": completed_queries,
        "hit_queries": hit_queries,
        "round_status": round_status,
        "query_round_status": str((report or {}).get("query_round_status") or round_status).strip(),
        "task_status": task_status,
        "success": task_status == "success",
        "completed_by_quota": bool((report or {}).get('completed_by_quota', False)),
    }


_count_task_queries = count_task_queries
_count_task_keywords = count_task_keywords
_build_result = build_result
_make_query_result_key = make_query_result_key
_result_has_usable_screenshot = result_has_usable_screenshot
_screenshot_path_exists = screenshot_path_exists
_collect_unique_screenshot_paths = collect_unique_screenshot_paths
_render_api_screenshot_with_retries = render_api_screenshot_with_retries
_load_today_success_only_query_results = load_today_success_only_query_results
_keyword_result_key = keyword_result_key
_build_keyword_result_index = build_keyword_result_index
_summarize_keyword_result = summarize_keyword_result
_finalize_daily_pool_keyword_results = finalize_daily_pool_keyword_results
_record_result_history = record_result_history
_STRUCTURAL_ERROR_PATTERNS = STRUCTURAL_ERROR_PATTERNS
_is_structural_error = is_structural_error
_classify_result_outcome = classify_result_outcome
_is_success_result = is_success_result
_collect_successful_brands = collect_successful_brands
_collect_failed_query_details = collect_failed_query_details
_build_execution_report = build_execution_report
_finalize_execution_report = finalize_execution_report
_build_task_state_extra = build_task_state_extra
_task_for_daily_state = task_for_daily_state
_build_finished_progress_payload = build_finished_progress_payload


__all__ = [
    "STRUCTURAL_ERROR_PATTERNS",
    "build_execution_report",
    "build_finished_progress_payload",
    "build_keyword_result_index",
    "build_result",
    "build_task_state_extra",
    "classify_result_outcome",
    "collect_failed_query_details",
    "collect_successful_brands",
    "collect_unique_screenshot_paths",
    "count_task_keywords",
    "count_task_queries",
    "finalize_daily_pool_keyword_results",
    "finalize_execution_report",
    "is_structural_error",
    "is_success_result",
    "keyword_result_key",
    "load_today_success_only_query_results",
    "make_query_result_key",
    "record_result_history",
    "render_api_screenshot_with_retries",
    "result_has_usable_screenshot",
    "screenshot_path_exists",
    "summarize_keyword_result",
    "task_for_daily_state",
]
