"""Single-query execution helpers for task groups."""

from __future__ import annotations

import time
import traceback
from typing import Callable

from core.task_executor_browser import (
    _create_browser_platform,
    _should_use_session_pool_for_query,
    _sync_reused_platform_runtime_state,
)
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
) -> dict | None:
    keyword = entry["keyword"]
    brand = entry["brand"]
    kw_entry = entry["kw_entry"]
    mode = str(entry["mode"] or "browser").strip()
    if mode not in {"browser", "recognition"}:
        mode = "browser"
    if should_stop():
        raise SchedulerStopRequested(stop_message)

    platform_class = PLATFORM_MAP.get(platform_name)
    if not platform_class:
        print(f"[Main] 不支持的平台: {platform_name}")
        diagnostic_id = record_diagnostic(
            task_name,
            keyword,
            platform_name,
            brand,
            "当前平台尚未接入浏览器适配",
            category="platform",
        )
        result = _build_result(
            keyword,
            platform_name,
            brand,
            99,
            None,
            error_message="当前平台尚未接入浏览器适配",
            mode=mode,
            diagnostic_id=diagnostic_id,
        )
        return complete_query_result(
            result,
            error_message="当前平台尚未接入浏览器适配",
            record_history=False,
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
    )


def _mark_browser_extraction_references(
    *,
    task_name: str,
    platform_name: str,
    references: list,
    body_references: list,
) -> None:
    """抓取模式抓到的引用 URL → 对比已录入文章，命中则标记为"已引用"。

    复用识别模式的 mark_articles_referenced_by_urls 主流程；只做单次去重
    + 单次 SQLite UPDATE，失败不致命。性能 < 10ms，无持久缓存。
    """
    if not task_name:
        return
    try:
        seen: set[str] = set()
        urls: list[str] = []
        for ref in list(references or []) + list(body_references or []):
            url = str((ref or {}).get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            urls.append(url)
        if not urls:
            return
        from core.article_store import mark_articles_referenced_by_urls

        result = mark_articles_referenced_by_urls(
            [task_name],
            urls,
            source="browser_extraction",
            platform=platform_name or "",
        )
        matched = int((result or {}).get("matched_count", 0) or 0)
        if matched > 0:
            print(
                f"[Main] 抓取引用已标记 {matched} 条已录入文章为已引用: "
                f"task={task_name}, platform={platform_name}, candidates={len(urls)}"
            )
    except Exception as exc:
        print(
            f"[Main] 抓取引用→文章标记失败 ({platform_name}/{task_name}): {exc}"
        )


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
) -> dict | None:
    use_session_pool = _should_use_session_pool_for_query(mode, task, platform_session_manager)
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
            # 仅在开启引用抓取时做匹配，避免没抓引用却跑这一步
            if bool(getattr(active_platform, "extract_references_enabled", False)):
                _mark_browser_extraction_references(
                    task_name=task_name,
                    platform_name=platform_name,
                    references=result.get("references") or [],
                    body_references=result.get("body_references") or [],
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

        if use_session_pool:
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
