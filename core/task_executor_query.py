"""Single-query execution helpers for task groups."""

from __future__ import annotations

import time
import traceback
from typing import Callable

from core.task_executor_api import _run_api_task
from core.task_executor_browser import (
    _create_browser_platform,
    _should_use_platform_serial_for_query,
    _should_use_session_pool_for_query,
    _sync_reused_platform_runtime_state,
)
from core.task_executor_smart import _run_smart_browser_task
from core.task_results import (
    build_result as _build_result,
    classify_result_outcome as _classify_result_outcome,
)
from platforms import (
    DeepSeekPlatform,
    DoubaoPlatform,
    KimiPlatform,
    TongyiPlatform,
    WenxinPlatform,
    YuanbaoPlatform,
)
from platforms.base import SchedulerStopRequested


PLATFORM_MAP = {
    "doubao": DoubaoPlatform,
    "deepseek": DeepSeekPlatform,
    "kimi": KimiPlatform,
    "yuanbao": YuanbaoPlatform,
    "tongyi": TongyiPlatform,
    "wenxin": WenxinPlatform,
    # 中文平台名别名
    "豆包": DoubaoPlatform,
    "Kimi": KimiPlatform,
    "元宝": YuanbaoPlatform,
    "通义千问": TongyiPlatform,
    "文心一言": WenxinPlatform,
    "DeepSeek": DeepSeekPlatform,
}


def execute_task_query(
    *,
    task: dict,
    task_name: str,
    entry: dict,
    platform_name: str,
    config: dict | None,
    platform_session_manager,
    stop_checker,
    progress_callback,
    should_stop: Callable[[], bool],
    stop_message: str,
    record_diagnostic: Callable[..., str],
    complete_query_result: Callable[..., dict],
    acquire_serial_platform: Callable[[str, object], object],
    run_api_task: Callable[..., dict] = _run_api_task,
    run_smart_browser_task: Callable[..., dict] = _run_smart_browser_task,
) -> dict | None:
    keyword = entry["keyword"]
    brand = entry["brand"]
    kw_entry = entry["kw_entry"]
    mode = entry["mode"]
    if should_stop():
        raise SchedulerStopRequested(stop_message)

    if mode == "api":
        return _execute_api_query(
            task_name=task_name,
            keyword=keyword,
            brand=brand,
            platform_name=platform_name,
            config=config,
            kw_entry=kw_entry,
            progress_callback=progress_callback,
            record_diagnostic=record_diagnostic,
            complete_query_result=complete_query_result,
            run_api_task=run_api_task,
        )

    platform_class = PLATFORM_MAP.get(platform_name)
    if not platform_class:
        print(f"[Main] 不支持的平台: {platform_name}")
        diagnostic_id = record_diagnostic(
            task_name,
            keyword,
            platform_name,
            brand,
            "当前仅支持 API 模式或尚未接入浏览器适配",
            category="platform",
        )
        result = _build_result(
            keyword,
            platform_name,
            brand,
            99,
            None,
            error_message="当前仅支持 API 模式或尚未接入浏览器适配",
            mode=mode,
            diagnostic_id=diagnostic_id,
        )
        return complete_query_result(
            result,
            error_message="当前仅支持 API 模式或尚未接入浏览器适配",
            record_history=False,
        )

    if mode == "smart":
        return _execute_smart_query(
            task=task,
            task_name=task_name,
            keyword=keyword,
            brand=brand,
            kw_entry=kw_entry,
            platform_name=platform_name,
            platform_class=platform_class,
            config=config,
            platform_session_manager=platform_session_manager,
            stop_checker=stop_checker,
            progress_callback=progress_callback,
            record_diagnostic=record_diagnostic,
            complete_query_result=complete_query_result,
            acquire_serial_platform=acquire_serial_platform,
            run_smart_browser_task=run_smart_browser_task,
        )

    return _execute_browser_query(
        task=task,
        task_name=task_name,
        keyword=keyword,
        brand=brand,
        kw_entry=kw_entry,
        mode=mode,
        platform_name=platform_name,
        platform_class=platform_class,
        config=config,
        platform_session_manager=platform_session_manager,
        stop_checker=stop_checker,
        progress_callback=progress_callback,
        record_diagnostic=record_diagnostic,
        complete_query_result=complete_query_result,
        acquire_serial_platform=acquire_serial_platform,
    )


def _execute_api_query(
    *,
    task_name: str,
    keyword: str,
    brand: str,
    platform_name: str,
    config: dict | None,
    kw_entry: dict,
    progress_callback,
    record_diagnostic: Callable[..., str],
    complete_query_result: Callable[..., dict],
    run_api_task: Callable[..., dict],
) -> dict | None:
    try:
        api_result = run_api_task(
            platform_name,
            keyword,
            brand,
            config or {},
            kw_entry=kw_entry,
            progress_callback=progress_callback,
        )
        diagnostic_id = ""
        if api_result.get("rank", 99) == 99 and api_result.get("error_message"):
            diagnostic_id = record_diagnostic(
                task_name,
                keyword,
                platform_name,
                brand,
                api_result.get("error_message", ""),
                category="query",
                details={"mode": "api"},
            )
        result = _build_result(
            keyword,
            platform_name,
            brand,
            api_result.get("rank", 99),
            api_result.get("screenshot"),
            answer_text=api_result.get("answer_text", ""),
            evidence=api_result.get("evidence", ""),
            error_message=api_result.get("error_message", ""),
            highlight_count=api_result.get("highlight_count", 0),
            mode="api",
            diagnostic_id=diagnostic_id,
            recovered_manually=api_result.get("recovered_manually", False),
        )
        return complete_query_result(result)
    except SchedulerStopRequested:
        raise
    except Exception as e:
        print(f"[Main] API任务失败 ({platform_name}/{keyword}): {e}\n{traceback.format_exc()}")
        diagnostic_id = record_diagnostic(
            task_name,
            keyword,
            platform_name,
            brand,
            str(e),
            category="query",
            details={"mode": "api"},
        )
        result = _build_result(
            keyword,
            platform_name,
            brand,
            99,
            None,
            error_message=str(e),
            mode="api",
            diagnostic_id=diagnostic_id,
        )
        return complete_query_result(result, error_message=str(e))


def _execute_smart_query(
    *,
    task: dict,
    task_name: str,
    keyword: str,
    brand: str,
    kw_entry: dict,
    platform_name: str,
    platform_class,
    config: dict | None,
    platform_session_manager,
    stop_checker,
    progress_callback,
    record_diagnostic: Callable[..., str],
    complete_query_result: Callable[..., dict],
    acquire_serial_platform: Callable[[str, object], object],
    run_smart_browser_task: Callable[..., dict],
) -> dict | None:
    try:
        use_session_pool = _should_use_session_pool_for_query("smart", task, platform_session_manager)
        use_platform_serial = _should_use_platform_serial_for_query("smart", task, config, platform_session_manager)
        active_platform = None
        if use_session_pool:
            active_platform = platform_session_manager.get_or_create(
                platform_name,
                lambda: _create_browser_platform(
                    platform_name,
                    platform_class,
                    config=config or {},
                    inspect=bool(task.get("inspect", False)),
                    stop_checker=stop_checker,
                ),
            )
            _sync_reused_platform_runtime_state(
                platform_name,
                active_platform,
                task,
                kw_entry,
                config=config,
                stop_checker=stop_checker,
            )
            active_platform.progress_callback = progress_callback
            active_platform.stop_checker = stop_checker
            platform_session_manager.mark_query_started(platform_name)
        elif use_platform_serial:
            active_platform = acquire_serial_platform(platform_name, platform_class)
            _sync_reused_platform_runtime_state(
                platform_name,
                active_platform,
                task,
                kw_entry,
                config=config,
                stop_checker=stop_checker,
            )
            active_platform.progress_callback = progress_callback
            active_platform.stop_checker = stop_checker

        query_started_at = time.monotonic()
        smart_result = run_smart_browser_task(
            platform_name=platform_name,
            keyword=keyword,
            brand=brand,
            task=task,
            kw_entry=kw_entry,
            config=config or {},
            platform_class=platform_class,
            progress_callback=progress_callback,
            stop_checker=stop_checker,
            platform=active_platform,
        )
        diagnostic_id = ""
        if smart_result.get("rank", 99) == 99 and smart_result.get("error_message"):
            diagnostic_id = record_diagnostic(
                task_name,
                keyword,
                platform_name,
                brand,
                smart_result.get("error_message", ""),
                category="query",
                details={"mode": "smart"},
            )
        result = _build_result(
            keyword,
            platform_name,
            brand,
            smart_result.get("rank", 99),
            smart_result.get("screenshot"),
            answer_text=smart_result.get("answer_text", ""),
            evidence=smart_result.get("evidence", ""),
            error_message=smart_result.get("error_message", ""),
            highlight_count=smart_result.get("highlight_count", 0),
            mode="smart",
            diagnostic_id=diagnostic_id,
            recovered_manually=smart_result.get("recovered_manually", False),
        )
        if use_session_pool:
            try:
                platform_session_manager.record_query_result(
                    platform_name,
                    _classify_result_outcome(result),
                    duration_seconds=time.monotonic() - query_started_at,
                    recovered_manually=bool(result.get("recovered_manually", False)),
                )
            except Exception as session_error:
                print(f"[Main] 复用会话结果记录失败 ({platform_name}): {session_error}")
        return complete_query_result(result)
    except SchedulerStopRequested:
        raise
    except Exception as e:
        print(f"[Main] 智能模式任务失败 ({platform_name}/{keyword}): {e}\n{traceback.format_exc()}")
        diagnostic_id = record_diagnostic(
            task_name,
            keyword,
            platform_name,
            brand,
            str(e),
            category="query",
            details={"mode": "smart"},
        )
        result = _build_result(
            keyword,
            platform_name,
            brand,
            99,
            None,
            error_message=str(e),
            mode="smart",
            diagnostic_id=diagnostic_id,
        )
        if _should_use_session_pool_for_query("smart", task, platform_session_manager):
            try:
                platform_session_manager.record_query_result(
                    platform_name,
                    _classify_result_outcome(result),
                    duration_seconds=0.0,
                    recovered_manually=False,
                )
            except Exception as session_error:
                print(f"[Main] 复用会话异常记录失败 ({platform_name}): {session_error}")
        return complete_query_result(result, error_message=str(e))


def _execute_browser_query(
    *,
    task: dict,
    task_name: str,
    keyword: str,
    brand: str,
    kw_entry: dict,
    mode: str,
    platform_name: str,
    platform_class,
    config: dict | None,
    platform_session_manager,
    stop_checker,
    progress_callback,
    record_diagnostic: Callable[..., str],
    complete_query_result: Callable[..., dict],
    acquire_serial_platform: Callable[[str, object], object],
) -> dict | None:
    use_session_pool = _should_use_session_pool_for_query(mode, task, platform_session_manager)
    use_platform_serial = _should_use_platform_serial_for_query(mode, task, config, platform_session_manager)
    pooled_platform = None
    query_started_at = time.monotonic()
    if use_session_pool:
        pooled_platform = platform_session_manager.get_or_create(
            platform_name,
            lambda: _create_browser_platform(
                platform_name,
                platform_class,
                config=config or {},
                inspect=bool(task.get("inspect", False)),
                stop_checker=stop_checker,
            ),
        )
        _sync_reused_platform_runtime_state(
            platform_name,
            pooled_platform,
            task,
            kw_entry,
            config=config,
            stop_checker=stop_checker,
        )
        pooled_platform.progress_callback = progress_callback
        pooled_platform.stop_checker = stop_checker
        pooled_platform.screenshot_on_mention = task.get("screenshot_on_mention", False)
        pooled_platform.deep_think = kw_entry.get("deep_think", {}).get(platform_name, False)
        pooled_platform.extract_references_enabled = bool(task.get("extract_references_enabled", False))
        platform_session_manager.mark_query_started(platform_name)
    elif use_platform_serial:
        pooled_platform = acquire_serial_platform(platform_name, platform_class)
        _sync_reused_platform_runtime_state(
            platform_name,
            pooled_platform,
            task,
            kw_entry,
            config=config,
            stop_checker=stop_checker,
        )
        pooled_platform.progress_callback = progress_callback
        pooled_platform.stop_checker = stop_checker
        pooled_platform.screenshot_on_mention = task.get("screenshot_on_mention", False)
        pooled_platform.deep_think = kw_entry.get("deep_think", {}).get(platform_name, False)
        pooled_platform.extract_references_enabled = bool(task.get("extract_references_enabled", False))
    else:
        pooled_platform = _create_browser_platform(
            platform_name,
            platform_class,
            config=config or {},
            inspect=bool(task.get("inspect", False)),
            stop_checker=stop_checker,
        )
        pooled_platform.screenshot_on_mention = task.get("screenshot_on_mention", False)
        pooled_platform.deep_think = kw_entry.get("deep_think", {}).get(platform_name, False)
        pooled_platform.extract_references_enabled = bool(task.get("extract_references_enabled", False))
        pooled_platform.progress_callback = progress_callback

    try:
        def _run_browser_query(active_platform):
            rank, screenshot = active_platform.search(keyword, brand)
            diagnostic_id = ""
            if rank == 99 and active_platform.last_error:
                diagnostic_id = record_diagnostic(
                    task_name,
                    keyword,
                    platform_name,
                    brand,
                    active_platform.last_error,
                    category="query",
                    details={"mode": "browser"},
                )
            result = _build_result(
                keyword,
                platform_name,
                brand,
                rank,
                screenshot,
                answer_text=active_platform.last_answer_text,
                evidence=brand if rank != 99 else "",
                error_message=active_platform.last_error,
                highlight_count=active_platform.last_screenshot_meta.get("highlight_count", 0),
                mode="browser",
                diagnostic_id=diagnostic_id,
                recovered_manually=bool(getattr(active_platform, "last_run_recovered_manually", False)),
                references=getattr(active_platform, "last_references", []),
                body_references=getattr(active_platform, "last_body_references", []),
            )
            if use_session_pool:
                try:
                    platform_session_manager.record_query_result(
                        platform_name,
                        _classify_result_outcome(result),
                        duration_seconds=time.monotonic() - query_started_at,
                        recovered_manually=bool(result.get("recovered_manually", False)),
                    )
                except Exception as session_error:
                    print(f"[Main] 复用会话结果记录失败 ({platform_name}): {session_error}")
            return complete_query_result(result)

        if use_session_pool or use_platform_serial:
            return _run_browser_query(pooled_platform)
        with pooled_platform:
            return _run_browser_query(pooled_platform)
    except SchedulerStopRequested:
        raise
    except Exception as e:
        print(f"[Main] 任务执行失败 ({platform_name}/{keyword}): {e}\n{traceback.format_exc()}")
        diagnostic_id = record_diagnostic(
            task_name,
            keyword,
            platform_name,
            brand,
            str(e),
            category="query",
            details={"mode": "browser"},
        )
        result = _build_result(
            keyword,
            platform_name,
            brand,
            99,
            None,
            error_message=str(e),
            mode="browser",
            diagnostic_id=diagnostic_id,
        )
        if use_session_pool:
            try:
                platform_session_manager.record_query_result(
                    platform_name,
                    _classify_result_outcome(result),
                    duration_seconds=time.monotonic() - query_started_at,
                    recovered_manually=False,
                )
            except Exception as session_error:
                print(f"[Main] 复用会话异常记录失败 ({platform_name}): {session_error}")
        return complete_query_result(result, error_message=str(e))


__all__ = ["execute_task_query"]
