"""Smart browser-mode task execution."""

from __future__ import annotations

import traceback

from core.task_executor_browser import _create_browser_platform
from core.task_results import screenshot_path_exists as _screenshot_path_exists
from platforms.base import InterruptionDetected, SchedulerStopRequested


def _run_smart_browser_task(
    platform_name: str,
    keyword: str,
    brand: str,
    task: dict,
    kw_entry: dict,
    config: dict,
    platform_class,
    max_retries: int = 3,
    progress_callback=None,
    stop_checker=None,
    platform=None,
):
    """
    智能模式：
    - 后台浏览器执行查询
    - 由 AI 对回答正文做品牌命中判断
    - 命中后沿用现有截图/通知链路
    """
    from core.ai_runtime import judge_brand_mention

    owned_platform = platform is None
    active_platform = platform
    if active_platform is None:
        active_platform = _create_browser_platform(
            platform_name,
            platform_class,
            config=config,
            inspect=bool(task.get("inspect", False)),
            stop_checker=stop_checker,
        )

    active_platform.screenshot_on_mention = task.get("screenshot_on_mention", False)
    active_platform.deep_think = kw_entry.get("deep_think", {}).get(platform_name, False)
    active_platform.stop_checker = stop_checker
    if owned_platform:
        active_platform.inspect = task.get("inspect", False)

    def _run_on_platform(current_platform):
        current_platform.ensure_logged_in(timeout=15)

        for attempt in range(1, max_retries + 1):
            current_platform.last_error = ""
            if progress_callback:
                progress_callback({
                    "stage": "attempt_start",
                    "platform": platform_name,
                    "keyword": keyword,
                    "brand": brand,
                    "attempt": attempt,
                    "max_attempts": max_retries,
                })
            print(f"[Smart] {platform_name} 尝试 {attempt}/{max_retries}: '{keyword}' -> 查找 '{brand}'")
            try:
                if attempt > 1:
                    current_platform.check_for_interruption(check_input_visible=True)

                current_platform.start_new_chat()
                current_platform.enable_deep_think()
                baseline_answer_text = current_platform._get_answer_text()
                current_platform._active_baseline_answer_text = baseline_answer_text or ""
                current_platform.type_like_human(keyword)
                current_platform.submit_prompt()

                state = {"text": ""}

                def on_complete(_, page_text):
                    state["text"] = page_text or ""

                current_platform._poll_until_complete(
                    brand,
                    on_complete,
                    get_text=current_platform._get_answer_text,
                    keyword=keyword,
                )
                final_text = state["text"] or current_platform.last_answer_text or current_platform._get_answer_text()

                if not current_platform.has_usable_answer_text(final_text, keyword=keyword, brand=brand):
                    invalid_answer_error = current_platform.last_error or "未获取到有效回答内容，可能触发验证码或回答尚未生成"
                    print(
                        f"[Smart] {platform_name} {invalid_answer_error}，"
                        f"{'准备重试...' if attempt < max_retries else '已达最大重试次数'}"
                    )
                    if progress_callback:
                        progress_callback({
                            "stage": "attempt_done",
                            "platform": platform_name,
                            "keyword": keyword,
                            "brand": brand,
                            "attempt": attempt,
                            "max_attempts": max_retries,
                            "success": False,
                            "error_message": invalid_answer_error,
                        })
                    if attempt < max_retries:
                        continue
                    return {
                        "rank": 99,
                        "screenshot": None,
                        "answer_text": final_text,
                        "evidence": "",
                        "error_message": invalid_answer_error,
                        "highlight_count": 0,
                        "mode": "smart",
                        "recovered_manually": bool(getattr(current_platform, "last_run_recovered_manually", False)),
                    }

                if not current_platform._has_new_answer_content(
                    final_text,
                    baseline_text=baseline_answer_text,
                    keyword=keyword,
                    brand=brand,
                ):
                    unchanged_answer_error = "未检测到新的有效回答内容，可能触发验证码、停留在旧对话或问题未真正发送"
                    print(
                        f"[Smart] {platform_name} {unchanged_answer_error}，"
                        f"{'准备重试...' if attempt < max_retries else '已达最大重试次数'}"
                    )
                    if progress_callback:
                        progress_callback({
                            "stage": "attempt_done",
                            "platform": platform_name,
                            "keyword": keyword,
                            "brand": brand,
                            "attempt": attempt,
                            "max_attempts": max_retries,
                            "success": False,
                            "error_message": unchanged_answer_error,
                        })
                    if attempt < max_retries:
                        continue
                    return {
                        "rank": 99,
                        "screenshot": None,
                        "answer_text": final_text,
                        "evidence": "",
                        "error_message": unchanged_answer_error,
                        "highlight_count": 0,
                        "mode": "smart",
                        "recovered_manually": bool(getattr(current_platform, "last_run_recovered_manually", False)),
                    }

                mentioned, evidence, match_info = current_platform.detect_brand_mention(
                    final_text,
                    brand,
                    keyword=keyword,
                )
                if mentioned:
                    debug_excerpt = str(match_info.get("normalized_excerpt") or evidence or "").replace("\n", "\\n")
                    print(f"[Smart] {platform_name} 规则判断: 命中; 证据: {debug_excerpt or '无'}")
                else:
                    mentioned, evidence = judge_brand_mention(
                        config=config or {},
                        platform_name=platform_name,
                        keyword=keyword,
                        brand=brand,
                        answer_text=final_text,
                    )
                    print(f"[Smart] {platform_name} AI判断: {'命中' if mentioned else '未命中'}; 证据: {evidence or '无'}")

                if mentioned:
                    screenshot = current_platform._take_long_screenshot_with_retries(brand=brand)
                    if not _screenshot_path_exists(screenshot):
                        screenshot_error = current_platform.last_error or f"已识别到品牌名，但截图生成失败：{brand}"
                        print(
                            f"[Smart] {platform_name} {screenshot_error}，"
                            f"{'准备重试...' if attempt < max_retries else '已达最大重试次数'}"
                        )
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
                            continue
                        return {
                            "rank": 99,
                            "screenshot": None,
                            "answer_text": final_text,
                            "evidence": evidence,
                            "error_message": screenshot_error,
                            "highlight_count": int(current_platform.last_screenshot_meta.get("highlight_count", 0) or 0),
                            "mode": "smart",
                            "recovered_manually": bool(getattr(current_platform, "last_run_recovered_manually", False)),
                        }
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
                        "answer_text": final_text,
                        "evidence": evidence,
                        "error_message": "",
                        "highlight_count": int(current_platform.last_screenshot_meta.get("highlight_count", 0) or 0),
                        "mode": "smart",
                        "recovered_manually": bool(getattr(current_platform, "last_run_recovered_manually", False)),
                    }

                print(f"[Smart] {platform_name} 未识别到品牌名 '{brand}'，{'准备重试...' if attempt < max_retries else '已达最大重试次数'}")
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

            except SchedulerStopRequested:
                raise
            except InterruptionDetected:
                print(f"[Smart] {platform_name} 人工干预完成，重新开始...")
                continue
            except Exception as e:
                current_platform.last_error = str(e)
                print(f"[Smart] 智能模式执行失败 ({platform_name}/{keyword}): {e}\n{traceback.format_exc()}")
                if progress_callback:
                    progress_callback({
                        "stage": "attempt_done",
                        "platform": platform_name,
                        "keyword": keyword,
                        "brand": brand,
                        "attempt": attempt,
                        "max_attempts": max_retries,
                        "success": False,
                        "error_message": str(e),
                    })

        return {
            "rank": 99,
            "screenshot": None,
            "answer_text": current_platform.last_answer_text,
            "evidence": "",
            "error_message": current_platform.last_error or f"未识别到品牌名 {brand}",
            "highlight_count": 0,
            "mode": "smart",
            "recovered_manually": bool(getattr(current_platform, "last_run_recovered_manually", False)),
        }

    try:
        if owned_platform:
            with active_platform:
                return _run_on_platform(active_platform)
        return _run_on_platform(active_platform)
    except SchedulerStopRequested:
        raise
    except Exception as e:
        print(f"[Smart] 智能模式启动失败 ({platform_name}/{keyword}): {e}\n{traceback.format_exc()}")
        return {
            "rank": 99,
            "screenshot": None,
            "answer_text": "",
            "evidence": "",
            "error_message": str(e),
            "highlight_count": 0,
            "mode": "smart",
            "recovered_manually": False,
        }


__all__ = ["_run_smart_browser_task"]
