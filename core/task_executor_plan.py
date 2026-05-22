"""Task execution planning helpers.

This module only derives query-plan data from task config and historical
successes. It intentionally does not execute queries or mutate runtime state.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from core.scheduler_notifications import dedupe_non_empty as _dedupe_non_empty
from core.task_results import (
    count_task_queries as _count_task_queries,
    make_query_result_key as _make_query_result_key,
    result_has_usable_screenshot as _result_has_usable_screenshot,
)


@dataclass
class TaskExecutionPlan:
    fixed_screenshot_enabled: bool
    executable_entries: list[dict]
    ordered_platforms: list[str]
    entries_by_platform: dict[str, list[dict]]
    total_queries: int
    expected_query_count: int
    fixed_screenshot_target: int
    historical_selected_results: list[dict]
    historical_completed_keywords: list[str]
    historical_detected_platforms: list[str]
    historical_brands: list[str]


def build_task_execution_plan(
    *,
    task: dict,
    keywords: list[dict],
    default_brand: str,
    runtime_mode: str,
    historical_success_map: dict[tuple[str, str, str], dict],
    manual_test_replay_completed_keywords: bool,
) -> TaskExecutionPlan:
    fixed_screenshot_enabled = bool(task.get("fixed_screenshot_enabled", False))
    executable_entries: list[dict] = []
    ordered_platforms: list[str] = []
    entries_by_platform: dict[str, list[dict]] = defaultdict(list)

    for kw_index, kw_entry in enumerate(keywords):
        keyword = str(kw_entry.get("keyword", "") or "").strip()
        brand = str(kw_entry.get("brand", "") or "").strip() or default_brand
        mode = runtime_mode or str(kw_entry.get("mode", "browser") or "browser").strip()
        if mode not in {"browser", "recognition"}:
            mode = "browser"
        platforms = [str(p).strip() for p in (kw_entry.get("platforms", []) or []) if str(p).strip()]
        if not keyword or not platforms or mode == "recognition":
            continue
        executable_entries.append({
            "index": kw_index,
            "keyword": keyword,
            "brand": brand,
            "platforms": platforms,
            "mode": mode,
            "kw_entry": kw_entry,
        })
        for platform_name in platforms:
            if platform_name not in ordered_platforms:
                ordered_platforms.append(platform_name)
            entries_by_platform[platform_name].append(executable_entries[-1])

    total_queries = sum(
        1
        for entry in executable_entries
        for platform_name in (entry.get("platforms", []) or [])
        if historical_query_result(
            entry,
            platform_name,
            historical_success_map=historical_success_map,
            fixed_screenshot_enabled=fixed_screenshot_enabled,
            manual_test_replay_completed_keywords=manual_test_replay_completed_keywords,
        )
        is None
    )
    expected_query_count = _count_task_queries(keywords, default_brand)
    fixed_screenshot_target = calculate_fixed_screenshot_target(
        task=task,
        fixed_screenshot_enabled=fixed_screenshot_enabled,
        executable_entries=executable_entries,
        ordered_platforms=ordered_platforms,
    )
    historical_selected_results = (
        [item for item in historical_success_map.values() if _result_has_usable_screenshot(item)]
        if fixed_screenshot_enabled
        else []
    )

    return TaskExecutionPlan(
        fixed_screenshot_enabled=fixed_screenshot_enabled,
        executable_entries=executable_entries,
        ordered_platforms=ordered_platforms,
        entries_by_platform=dict(entries_by_platform),
        total_queries=total_queries,
        expected_query_count=expected_query_count,
        fixed_screenshot_target=fixed_screenshot_target,
        historical_selected_results=historical_selected_results,
        historical_completed_keywords=_dedupe_non_empty(
            [str(item.get("keyword") or "").strip() for item in historical_success_map.values()]
        ),
        historical_detected_platforms=_dedupe_non_empty(
            [str(item.get("platform") or "").strip() for item in historical_success_map.values()]
        ),
        historical_brands=_dedupe_non_empty(
            [str(item.get("brand") or "").strip() for item in historical_success_map.values()]
        ),
    )


def historical_query_result(
    entry: dict,
    platform_name: str,
    *,
    historical_success_map: dict[tuple[str, str, str], dict],
    fixed_screenshot_enabled: bool,
    manual_test_replay_completed_keywords: bool,
) -> dict | None:
    if manual_test_replay_completed_keywords:
        return None
    historical_result = historical_success_map.get(
        _make_query_result_key(entry["keyword"], platform_name, entry["brand"])
    )
    if (
        fixed_screenshot_enabled
        and historical_result
        and historical_result.get("rank", 99) != 99
        and not _result_has_usable_screenshot(historical_result)
    ):
        return None
    return historical_result


def calculate_fixed_screenshot_target(
    *,
    task: dict,
    fixed_screenshot_enabled: bool,
    executable_entries: list[dict],
    ordered_platforms: list[str],
) -> int:
    if not fixed_screenshot_enabled or not executable_entries:
        return 0
    try:
        raw_target = max(
            1,
            int(
                task.get(
                    "fixed_screenshot_count",
                    task.get("recognition_batch_size", 1),
                )
                or 1
            ),
        )
    except Exception:
        raw_target = 1
    return max(raw_target, len(ordered_platforms))


__all__ = [
    "TaskExecutionPlan",
    "build_task_execution_plan",
    "calculate_fixed_screenshot_target",
    "historical_query_result",
]
