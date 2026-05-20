"""Query dispatch strategies for task execution."""

from __future__ import annotations

from collections import defaultdict
from typing import Callable

from core.platform_sessions import build_session_pool_dispatch_pairs as _build_session_pool_dispatch_pairs


def dispatch_task_queries(
    *,
    task: dict,
    default_brand: str,
    runtime_mode: str,
    keywords: list[dict],
    executable_entries: list[dict],
    ordered_platforms: list[str],
    entries_by_platform: dict[str, list[dict]],
    fixed_screenshot_enabled: bool,
    fixed_screenshot_target: int,
    historical_selected_results: list[dict],
    selected_results: list[dict],
    platform_session_manager,
    should_stop: Callable[[], bool],
    fixed_target_reached: Callable[[], bool],
    append_selected_result: Callable[[dict], None],
    execute_query: Callable[[dict, str], dict | None],
) -> None:
    if fixed_screenshot_enabled and executable_entries:
        _dispatch_fixed_screenshot_queries(
            task=task,
            default_brand=default_brand,
            executable_entries=executable_entries,
            ordered_platforms=ordered_platforms,
            fixed_screenshot_target=fixed_screenshot_target,
            historical_selected_results=historical_selected_results,
            selected_results=selected_results,
            should_stop=should_stop,
            fixed_target_reached=fixed_target_reached,
            append_selected_result=append_selected_result,
            execute_query=execute_query,
        )
        return

    if (
        platform_session_manager is not None
        and getattr(platform_session_manager.policy, "use_session_pool", False)
        and getattr(platform_session_manager.policy, "use_platform_batch_dispatch", False)
    ):
        batch_size = max(
            1,
            int(getattr(platform_session_manager.policy, "session_pool_platform_batch_size", 1) or 1),
        )
        print(
            f"[Main] 任务组启用平台会话池批次调度: "
            f"task={task.get('name', default_brand)}, batch_size={batch_size}"
        )
        for entry, platform_name in _build_session_pool_dispatch_pairs(
            executable_entries,
            ordered_platforms,
            entries_by_platform,
            platform_session_manager,
            logger=print,
        ):
            if should_stop():
                break
            execute_query(entry, platform_name)
        return

    _dispatch_default_keyword_queries(
        task=task,
        default_brand=default_brand,
        runtime_mode=runtime_mode,
        keywords=keywords,
        should_stop=should_stop,
        execute_query=execute_query,
    )


def _dispatch_fixed_screenshot_queries(
    *,
    task: dict,
    default_brand: str,
    executable_entries: list[dict],
    ordered_platforms: list[str],
    fixed_screenshot_target: int,
    historical_selected_results: list[dict],
    selected_results: list[dict],
    should_stop: Callable[[], bool],
    fixed_target_reached: Callable[[], bool],
    append_selected_result: Callable[[dict], None],
    execute_query: Callable[[dict, str], dict | None],
) -> None:
    print(
        f"[Main] 固定截图策略启动: task={task.get('name', default_brand)}, "
        f"target={fixed_screenshot_target}, platform_floor={len(ordered_platforms)}"
    )
    covered_platforms: set[str] = {
        str(item.get("platform") or "").strip()
        for item in historical_selected_results
        if str(item.get("platform") or "").strip()
    }
    hit_keyword_indexes: set[int] = set()
    hit_platforms_by_keyword: dict[int, set[str]] = defaultdict(set)
    tried_pairs: set[tuple[int, str]] = set()

    for item in historical_selected_results:
        item_keyword = str(item.get("keyword") or "").strip()
        item_brand = str(item.get("brand") or "").strip()
        item_platform = str(item.get("platform") or "").strip()
        for entry in executable_entries:
            if entry["keyword"] != item_keyword or entry["brand"] != item_brand:
                continue
            hit_keyword_indexes.add(entry["index"])
            if item_platform:
                hit_platforms_by_keyword[entry["index"]].add(item_platform)

    # 第一轮：优先确保每个平台至少拿到一张。
    for platform_name in ordered_platforms:
        if should_stop() or fixed_target_reached():
            break
        if platform_name in covered_platforms:
            continue
        for entry in executable_entries:
            if should_stop():
                break
            if platform_name not in entry["platforms"]:
                continue
            pair = (entry["index"], platform_name)
            if pair in tried_pairs:
                continue
            tried_pairs.add(pair)
            result = execute_query(entry, platform_name)
            if result and result.get("rank", 99) != 99:
                covered_platforms.add(platform_name)
                hit_keyword_indexes.add(entry["index"])
                hit_platforms_by_keyword[entry["index"]].add(platform_name)
                append_selected_result(result)
                break

    # 第二轮：平台已覆盖后，优先补足尚未命中的关键词。
    if not fixed_target_reached() and not should_stop():
        for entry in executable_entries:
            if should_stop() or fixed_target_reached():
                break
            if entry["index"] in hit_keyword_indexes:
                continue
            for platform_name in entry["platforms"]:
                if should_stop():
                    break
                pair = (entry["index"], platform_name)
                if pair in tried_pairs:
                    continue
                tried_pairs.add(pair)
                result = execute_query(entry, platform_name)
                if result and result.get("rank", 99) != 99:
                    covered_platforms.add(platform_name)
                    hit_keyword_indexes.add(entry["index"])
                    hit_platforms_by_keyword[entry["index"]].add(platform_name)
                    append_selected_result(result)
                    break

    # 第三轮：如果只差 1-2 张，则允许已命中过的关键词换其它平台补差。
    remaining_slots = max(0, fixed_screenshot_target - len(historical_selected_results) - len(selected_results))
    if 0 < remaining_slots <= 2 and not should_stop():
        for entry in executable_entries:
            if should_stop() or fixed_target_reached():
                break
            if entry["index"] not in hit_keyword_indexes:
                continue
            for platform_name in entry["platforms"]:
                if should_stop():
                    break
                pair = (entry["index"], platform_name)
                if pair in tried_pairs:
                    continue
                if platform_name in hit_platforms_by_keyword.get(entry["index"], set()):
                    continue
                tried_pairs.add(pair)
                result = execute_query(entry, platform_name)
                if result and result.get("rank", 99) != 99:
                    covered_platforms.add(platform_name)
                    hit_platforms_by_keyword[entry["index"]].add(platform_name)
                    append_selected_result(result)
                if fixed_target_reached():
                    break


def _dispatch_default_keyword_queries(
    *,
    task: dict,
    default_brand: str,
    runtime_mode: str,
    keywords: list[dict],
    should_stop: Callable[[], bool],
    execute_query: Callable[[dict, str], dict | None],
) -> None:
    for kw_entry in keywords:
        if should_stop():
            break
        keyword = kw_entry.get("keyword", "").strip()
        brand = kw_entry.get("brand", "").strip() or default_brand
        platforms = kw_entry.get("platforms", [])
        task_name = task.get("name", default_brand)
        mode = runtime_mode or str(kw_entry.get("mode", "browser") or "browser").strip()

        if not keyword or not platforms:
            continue

        print(
            f"[Main] 任务调度: task={task_name}, keyword={keyword}, brand={brand}, "
            f"mode={mode}, configured_platforms={platforms}"
        )

        for platform_name in platforms:
            if should_stop():
                break
            if mode == "recognition":
                print(f"[Main] 识别模式任务由剪切板监听器处理，跳过主动查询: {task.get('name', default_brand)}")
                continue
            execute_query(
                {
                    "index": -1,
                    "keyword": keyword,
                    "brand": brand,
                    "platforms": platforms,
                    "mode": mode,
                    "kw_entry": kw_entry,
                },
                platform_name,
            )


__all__ = ["dispatch_task_queries"]
