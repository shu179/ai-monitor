"""API-mode task execution helpers."""

from __future__ import annotations

import time

from core.task_results import render_api_screenshot_with_retries as _render_api_screenshot_with_retries


def _run_api_task(
    platform_name: str,
    keyword: str,
    brand: str,
    config: dict,
    kw_entry: dict | None = None,
    max_retries: int = 5,
    progress_callback=None,
):
    """
    API 模式执行查询：调用官方 API → 检测品牌名 → 渲染 HTML 截图。
    最多重试 max_retries 次，找到品牌即停止。
    """
    from platforms.api_client import (
        query_platform_api,
        resolve_platform_api_model,
        should_use_platform_deep_think_param,
    )
    from platforms.base import BasePlatform
    from platforms.html_renderer import render_text_to_screenshot

    plat_cfg = config.get("platforms", {}).get(platform_name, {})
    api_key = plat_cfg.get("api_key", "")
    deep_think_enabled = bool((kw_entry or {}).get("deep_think", {}).get(platform_name, False))
    api_model = resolve_platform_api_model(platform_name, plat_cfg, deep_think_enabled)
    runtime_deep_think = should_use_platform_deep_think_param(platform_name, plat_cfg, deep_think_enabled)

    if not api_key:
        print(f"[API] {platform_name} 未配置 api_key，跳过")
        return {
            "rank": 99,
            "screenshot": None,
            "answer_text": "",
            "evidence": "",
            "error_message": "未配置 api_key",
            "highlight_count": 0,
            "mode": "api",
        }

    text = ""
    for attempt in range(1, max_retries + 1):
        if progress_callback:
            progress_callback({
                "stage": "attempt_start",
                "platform": platform_name,
                "keyword": keyword,
                "brand": brand,
                "attempt": attempt,
                "max_attempts": max_retries,
            })
        print(f"[API] {platform_name} 尝试 {attempt}/{max_retries}: '{keyword}' -> 查找 '{brand}'")
        text = query_platform_api(
            platform_name,
            keyword,
            api_key,
            api_model,
            enable_search=True,
            deep_think=runtime_deep_think,
        )
        if text is None:
            print(f"[API] {platform_name} API调用失败，跳过重试")
            if progress_callback:
                progress_callback({
                    "stage": "attempt_done",
                    "platform": platform_name,
                    "keyword": keyword,
                    "brand": brand,
                    "attempt": attempt,
                    "max_attempts": max_retries,
                    "success": False,
                    "error_message": "API调用失败",
                })
            return {
                "rank": 99,
                "screenshot": None,
                "answer_text": "",
                "evidence": "",
                "error_message": "API调用失败",
                "highlight_count": 0,
                "mode": "api",
            }

        if BasePlatform.contains_brand_mention(text, brand):
            print(f"[API] {platform_name} ✅ 找到品牌名，生成截图...")
            screenshot = _render_api_screenshot_with_retries(
                render_text_to_screenshot,
                text=text,
                platform_name=platform_name,
                brand=brand,
                keyword=keyword,
            )
            if screenshot:
                if progress_callback:
                    progress_callback({
                        "stage": "attempt_done",
                        "platform": platform_name,
                        "keyword": keyword,
                        "brand": brand,
                        "attempt": attempt,
                        "max_attempts": max_retries,
                        "success": True,
                    })
                return {
                    "rank": 1,
                    "screenshot": screenshot,
                    "answer_text": text,
                    "evidence": brand,
                    "error_message": "",
                    "highlight_count": 0,
                    "mode": "api",
                }

            screenshot_error = f"已识别到品牌名，但截图生成失败：{brand}"
            print(f"[API] {platform_name} {screenshot_error}")
            if progress_callback:
                progress_callback({
                    "stage": "attempt_done",
                    "platform": platform_name,
                    "keyword": keyword,
                    "brand": brand,
                    "attempt": attempt,
                    "max_attempts": max_retries,
                    "success": False,
                    "error_message": screenshot_error,
                })
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 30))
                continue
            return {
                "rank": 99,
                "screenshot": None,
                "answer_text": text,
                "evidence": brand,
                "error_message": screenshot_error,
                "highlight_count": 0,
                "mode": "api",
            }

        print(f"[API] {platform_name} 未找到品牌名 '{brand}'，{'准备重试...' if attempt < max_retries else '已达最大重试次数'}")
        if progress_callback:
            progress_callback({
                "stage": "attempt_done",
                "platform": platform_name,
                "keyword": keyword,
                "brand": brand,
                "attempt": attempt,
                "max_attempts": max_retries,
                "success": False,
                "error_message": "",
            })
        if attempt < max_retries:
            time.sleep(min(2 ** attempt, 30))

    return {
        "rank": 99,
        "screenshot": None,
        "answer_text": text,
        "evidence": "",
        "error_message": f"未识别到品牌名 {brand}",
        "highlight_count": 0,
        "mode": "api",
    }


__all__ = ["_run_api_task"]
