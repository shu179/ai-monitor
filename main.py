"""
Surfaced - 主入口
系统托盘应用
"""

import argparse
import sys
import time
import logging
import subprocess
import traceback
from collections import defaultdict
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 添加项目目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from core import load_config, SmartScheduler, WeComNotifier, ensure_config_task_ids
from core.app_paths import resolve_app_dir, resolve_app_path
from core.cloud_sync import CloudSyncManager
from core.cycle_state import resolve_report_status
from core.logging_utils import SecretRedactingFilter
from core.daily_task_state import (
    SOURCE_MODE_FORMAL,
    SOURCE_MODE_TEST,
    build_task_state_extra as build_daily_task_state_extra,
    finish_formal_task_run,
    get_task_day_status,
    is_task_sent_today,
    mark_task_send_failure,
    mark_task_sent,
    start_formal_task_run,
    write_task_status,
)
from core.local_model_manager import get_local_model_manager
from core.local_runtime_prep import prepare_local_runtime, shutdown_owned_local_runtime
from core.shutdown import install_shutdown_handlers, register_shutdown_callback, run_shutdown_callbacks
from core.browser_platform_factory import (
    create_browser_platform,
    resolve_browser_answer_screenshot_mode,
)
from core.platform_sessions import (
    PlatformSessionManager,
    build_query_execution_policy,
    build_round_query_plan,
    build_session_pool_dispatch_pairs as _build_session_pool_dispatch_pairs,
)
from core.scheduler_notifications import (
    _PLATFORM_LABELS,
    SchedulerWebhookReporter,
    build_scheduler_notifier as _build_scheduler_notifier,
    dedupe_non_empty as _dedupe_non_empty,
    display_platform_name as _display_platform_name,
    get_scheduler_notification_webhook as _get_scheduler_notification_webhook,
    normalize_webhook_url as _normalize_webhook_url,
)
from core.sync_service import apply_sync_bundle, build_sync_bundle
from core.task_notifications import (
    build_notification_result as _build_notification_result,
    is_success_record as _is_success_record,
    record_diagnostic as _record_diagnostic,
    send_fixed_screenshot_task_notification as _send_fixed_screenshot_task_notification,
    send_multi_query_task_notification as _send_multi_query_task_notification,
    send_single_query_task_notification as _send_single_query_task_notification,
    send_task_notifications as _send_task_notifications,
    should_send_single_query_notification as _should_send_single_query_notification,
)
from core.task_results import (
    _STRUCTURAL_ERROR_PATTERNS,
    build_execution_report as _build_execution_report,
    build_finished_progress_payload as _build_finished_progress_payload,
    build_keyword_result_index as _build_keyword_result_index,
    build_result as _build_result,
    build_task_state_extra as _build_task_state_extra,
    classify_result_outcome as _classify_result_outcome,
    collect_failed_query_details as _collect_failed_query_details,
    collect_successful_brands as _collect_successful_brands,
    collect_unique_screenshot_paths as _collect_unique_screenshot_paths,
    count_task_keywords as _count_task_keywords,
    count_task_queries as _count_task_queries,
    finalize_daily_pool_keyword_results as _finalize_daily_pool_keyword_results,
    finalize_execution_report as _finalize_execution_report,
    is_structural_error as _is_structural_error,
    is_success_result as _is_success_result,
    keyword_result_key as _keyword_result_key,
    load_today_success_only_query_results as _load_today_success_only_query_results,
    make_query_result_key as _make_query_result_key,
    record_result_history as _record_result_history,
    render_api_screenshot_with_retries as _render_api_screenshot_with_retries,
    result_has_usable_screenshot as _result_has_usable_screenshot,
    screenshot_path_exists as _screenshot_path_exists,
    summarize_keyword_result as _summarize_keyword_result,
    task_for_daily_state as _task_for_daily_state,
)
from core.time_utils import local_now, local_today
from core.version import APP_NAME, get_version_label, get_version_title
from platforms import (
    DoubaoPlatform, DeepSeekPlatform, KimiPlatform,
    YuanbaoPlatform, TongyiPlatform, WenxinPlatform
)
from platforms.base import InterruptionDetected, SchedulerStopRequested


def parse_args(argv: list[str] | None = None):
    """解析启动参数。"""
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument(
        "--version",
        action="version",
        version=get_version_title(),
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="启动 Web UI 桌面模式",
    )
    parser.add_argument(
        "--prepare-local-runtime",
        action="store_true",
        help="准备本地模型运行环境，供安装器或首次启动预热使用",
    )
    parser.add_argument(
        "--prepare-local-runtime-model",
        default="",
        help="准备本地运行环境时强制使用的模型名，默认读取配置",
    )
    parser.add_argument(
        "--prepare-local-runtime-timeout",
        type=int,
        default=1800,
        help="准备本地运行环境的超时时间（秒），默认 1800",
    )
    parser.add_argument(
        "--prepare-local-runtime-no-model-pull",
        action="store_true",
        help="只准备运行时，不自动拉取缺失模型",
    )
    return parser.parse_args(argv)


def setup_logging():
    """设置日志（同时写文件和终端）"""
    logger = logging.getLogger()
    if logger.handlers:
        redaction_filter = SecretRedactingFilter()
        for handler in logger.handlers:
            if not any(isinstance(item, SecretRedactingFilter) for item in handler.filters):
                handler.addFilter(redaction_filter)
        return  # 避免重复初始化（多次调用时）

    logs_dir = resolve_app_dir("logs")
    log_path = logs_dir / "monitor.log"
    max_log_bytes = 10 * 1024 * 1024
    backup_count = 5

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(message)s',
        handlers=[
            RotatingFileHandler(
                log_path,
                maxBytes=max_log_bytes,
                backupCount=backup_count,
                encoding='utf-8',
            ),
            logging.StreamHandler()
        ]
    )
    redaction_filter = SecretRedactingFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redaction_filter)


def cleanup_screenshots(config: dict):
    """清理过期截图（按天数和总大小）"""
    cfg = config.get('screenshot', {})
    if not cfg.get('auto_cleanup', False):
        return

    screenshots_dir = resolve_app_path("screenshots")
    if not screenshots_dir.exists():
        return

    keep_days = cfg.get('keep_days', 7)
    max_size_mb = cfg.get('max_size_mb', 500)
    cutoff = local_now().replace(tzinfo=None) - timedelta(days=keep_days)

    # 删除超过保留天数的文件（递归包含 recognition/ 子目录）
    for f in screenshots_dir.rglob('*.jpg'):
        try:
            if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                f.unlink()
                print(f"[Cleanup] 已删除过期截图: {f.relative_to(screenshots_dir)}")
        except Exception as e:
            print(f"[Cleanup] 删除截图失败 {f.name}: {e}")

    # 如果总大小超限，删除最旧的文件（递归包含 recognition/ 子目录）
    try:
        files = sorted(screenshots_dir.rglob('*.jpg'), key=lambda f: f.stat().st_mtime)
        total_mb = sum(f.stat().st_size for f in files) / (1024 * 1024)
        while total_mb > max_size_mb and files:
            oldest = files.pop(0)
            try:
                total_mb -= oldest.stat().st_size / (1024 * 1024)
                oldest.unlink()
                print(f"[Cleanup] 截图超限，已删除: {oldest.relative_to(screenshots_dir)}")
            except Exception as e:
                print(f"[Cleanup] 删除截图失败 {oldest.name}: {e}")
    except Exception as e:
        print(f"[Cleanup] 截图大小检查失败: {e}")


def check_gui_runtime():
    """
    在独立子进程里预检 tkinter/Tk 是否可用。
    这样即使底层 GUI 库触发 abort，也不会把主进程直接带崩。
    """
    if sys.platform not in {"darwin", "win32", "linux"}:
        return True

    probe_code = (
        "import tkinter as tk\n"
        "root = tk.Tk()\n"
        "root.withdraw()\n"
        "root.update_idletasks()\n"
        "root.destroy()\n"
    )

    try:
        result = subprocess.run(
            [sys.executable, "-c", probe_code],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception as e:
        print(f"[Main] GUI 运行环境预检失败: {e}")
        return False

    if result.returncode == 0:
        return True

    print("[Main] 检测到当前 GUI 运行环境不可用，已阻止托盘启动，避免直接崩溃。")
    print(f"[Main] Python: {sys.executable}")
    print(f"[Main] Python 版本: {sys.version.split()[0]}")
    print(f"[Main] 平台: {sys.platform}")
    print(f"[Main] 预检退出码: {result.returncode}")

    stderr_text = (result.stderr or "").strip()
    stdout_text = (result.stdout or "").strip()
    if stderr_text:
        print(f"[Main] GUI 预检 stderr:\n{stderr_text}")
    elif stdout_text:
        print(f"[Main] GUI 预检 stdout:\n{stdout_text}")

    if sys.platform == "darwin":
        print("[Main] 当前问题出在 tkinter/Tk 原生窗口初始化，不是业务逻辑报错。")
        print("[Main] 建议改用 Python 3.12 或 3.13 运行本项目，再重新安装依赖。")
        print("[Main] 示例：python3.13 -m pip install -r requirements.txt")
        print("[Main] 示例：python3.13 main.py")
    else:
        print("[Main] 请检查当前系统是否具备可用的图形界面环境。")

    return False



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
    API 模式执行查询：调用官方 API → 检测品牌名 → 渲染 HTML 截图
    最多重试 max_retries 次，找到品牌即停止。
    返回结果字典
    """
    from platforms.api_client import (
        query_platform_api,
        resolve_platform_api_model,
        should_use_platform_deep_think_param,
    )
    from platforms.html_renderer import render_text_to_screenshot
    from platforms.base import BasePlatform

    plat_cfg = config.get('platforms', {}).get(platform_name, {})
    api_key = plat_cfg.get('api_key', '')
    deep_think_enabled = bool((kw_entry or {}).get('deep_think', {}).get(platform_name, False))
    api_model = resolve_platform_api_model(platform_name, plat_cfg, deep_think_enabled)
    runtime_deep_think = should_use_platform_deep_think_param(platform_name, plat_cfg, deep_think_enabled)

    if not api_key:
        print(f"[API] {platform_name} 未配置 api_key，跳过")
        return {
            'rank': 99,
            'screenshot': None,
            'answer_text': '',
            'evidence': '',
            'error_message': '未配置 api_key',
            'highlight_count': 0,
            'mode': 'api',
        }

    text = ''  # 保证循环外引用时变量已定义
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
                'rank': 99,
                'screenshot': None,
                'answer_text': '',
                'evidence': '',
                'error_message': 'API调用失败',
                'highlight_count': 0,
                'mode': 'api',
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
                    'rank': 1,
                    'screenshot': screenshot,
                    'answer_text': text,
                    'evidence': brand,
                    'error_message': '',
                    'highlight_count': 0,
                    'mode': 'api',
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
                'rank': 99,
                'screenshot': None,
                'answer_text': text,
                'evidence': brand,
                'error_message': screenshot_error,
                'highlight_count': 0,
                'mode': 'api',
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
            time.sleep(min(2 ** attempt, 30))  # 指数退避，避免频繁触发速率限制

    return {
        'rank': 99,
        'screenshot': None,
        'answer_text': text,
        'evidence': '',
        'error_message': f"未识别到品牌名 {brand}",
        'highlight_count': 0,
        'mode': 'api',
    }


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
            inspect=bool(task.get('inspect', False)),
            stop_checker=stop_checker,
        )

    active_platform.screenshot_on_mention = task.get('screenshot_on_mention', False)
    active_platform.deep_think = kw_entry.get('deep_think', {}).get(platform_name, False)
    active_platform.stop_checker = stop_checker
    if owned_platform:
        active_platform.inspect = task.get('inspect', False)

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

                state = {'text': ''}

                def on_complete(_, page_text):
                    state['text'] = page_text or ''

                current_platform._poll_until_complete(
                    brand,
                    on_complete,
                    get_text=current_platform._get_answer_text,
                    keyword=keyword,
                )
                final_text = state['text'] or current_platform.last_answer_text or current_platform._get_answer_text()

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
                        'rank': 99,
                        'screenshot': None,
                        'answer_text': final_text,
                        'evidence': '',
                        'error_message': invalid_answer_error,
                        'highlight_count': 0,
                        'mode': 'smart',
                        'recovered_manually': bool(getattr(current_platform, 'last_run_recovered_manually', False)),
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
                        'rank': 99,
                        'screenshot': None,
                        'answer_text': final_text,
                        'evidence': '',
                        'error_message': unchanged_answer_error,
                        'highlight_count': 0,
                        'mode': 'smart',
                        'recovered_manually': bool(getattr(current_platform, 'last_run_recovered_manually', False)),
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
                            'rank': 99,
                            'screenshot': None,
                            'answer_text': final_text,
                            'evidence': evidence,
                            'error_message': screenshot_error,
                            'highlight_count': int(current_platform.last_screenshot_meta.get('highlight_count', 0) or 0),
                            'mode': 'smart',
                            'recovered_manually': bool(getattr(current_platform, 'last_run_recovered_manually', False)),
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
                        'rank': 1,
                        'screenshot': screenshot,
                        'answer_text': final_text,
                        'evidence': evidence,
                        'error_message': '',
                        'highlight_count': int(current_platform.last_screenshot_meta.get('highlight_count', 0) or 0),
                        'mode': 'smart',
                        'recovered_manually': bool(getattr(current_platform, 'last_run_recovered_manually', False)),
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
            'rank': 99,
            'screenshot': None,
            'answer_text': current_platform.last_answer_text,
            'evidence': '',
            'error_message': current_platform.last_error or f"未识别到品牌名 {brand}",
            'highlight_count': 0,
            'mode': 'smart',
            'recovered_manually': bool(getattr(current_platform, 'last_run_recovered_manually', False)),
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
            'rank': 99,
            'screenshot': None,
            'answer_text': '',
            'evidence': '',
            'error_message': str(e),
            'highlight_count': 0,
            'mode': 'smart',
            'recovered_manually': False,
        }


def _apply_browser_runtime_config(platform, platform_name: str, config: dict | None) -> None:
    """把浏览器自动化的运行时配置注入到平台实例。"""
    if not config:
        return
    browser_cfg = (config.get("browser_automation", {}) or {}).get(platform_name, {}) or {}
    for key, value in browser_cfg.items():
        if not hasattr(platform, key):
            continue
        if not isinstance(value, str):
            continue
        normalized = value.strip()
        if not normalized:
            continue
        setattr(platform, key, normalized)


def _create_browser_platform(
    platform_name: str,
    platform_class,
    *,
    config: dict | None,
    inspect: bool,
    stop_checker=None,
):
    del platform_class
    return create_browser_platform(
        platform_name,
        config=config,
        inspect=inspect,
        stop_checker=stop_checker,
    )


def _should_use_session_pool_for_query(
    mode: str,
    task: dict,
    platform_session_manager: PlatformSessionManager | None,
) -> bool:
    normalized_mode = str(mode or "").strip()
    if platform_session_manager is None:
        return False
    if normalized_mode not in {"browser", "smart"}:
        return False
    return bool(platform_session_manager.policy.use_session_pool)


def _should_use_platform_serial_for_query(
    mode: str,
    task: dict,
    config: dict | None,
    platform_session_manager: PlatformSessionManager | None,
) -> bool:
    normalized_mode = str(mode or "").strip()
    if normalized_mode not in {"browser", "smart"}:
        return False
    if _should_use_session_pool_for_query(normalized_mode, task, platform_session_manager):
        return False
    policy = build_query_execution_policy(config or {}, normalized_mode)
    return bool(policy.use_platform_serial)


def _sync_reused_platform_runtime_state(
    platform_name: str,
    platform,
    task: dict,
    kw_entry: dict,
    config: dict | None = None,
    stop_checker=None,
) -> None:
    target_inspect = bool(task.get("inspect", False))
    current_inspect = bool(getattr(platform, "inspect", False))

    if current_inspect != target_inspect:
        if target_inspect:
            # 先按当前无头状态切到前台，再同步 inspect 标记，避免 _show_browser 误判。
            platform._show_browser()
            platform.inspect = True
        else:
            # 先取消 inspect 标记，再收回后台，确保 _hide_browser 会切回无头/最小化。
            platform.inspect = False
            platform._hide_browser()
    else:
        platform.inspect = target_inspect

    platform.stop_checker = stop_checker
    platform.screenshot_on_mention = task.get('screenshot_on_mention', False)
    platform.deep_think = kw_entry.get('deep_think', {}).get(platform_name, False)
    platform.extract_references_enabled = bool(task.get('extract_references_enabled', False))
    platform.answer_screenshot_mode = resolve_browser_answer_screenshot_mode(config)


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
    platform_serial_state: dict | None = None,
) -> list | tuple[list, dict]:
    """
    执行任务组（多关键词 × 多平台），返回所有结果列表
    results: [{'keyword', 'platform', 'brand', 'rank', 'screenshot'}, ...]
    """
    # 向后兼容：旧格式单 keyword+platform+brand → 转为 keywords 列表
    if 'keyword' in task and 'keywords' not in task:
        task = {**task, 'keywords': [{'keyword': task['keyword'], 'brand': task.get('brand', ''), 'platforms': [task['platform']]}]}

    started_at = time.time()
    run_started_at_text = datetime.fromtimestamp(started_at, tz=local_now().tzinfo).isoformat(timespec='seconds')

    keywords = task.get('keywords', [])
    if not keywords:
        report = _build_execution_report(task, [], time.time() - started_at)
        return ([], report) if return_report else []

    runtime_mode = str((config or {}).get('detection_mode') or '').strip()
    if runtime_mode not in {'browser', 'api', 'smart', 'recognition'}:
        runtime_mode = str(task.get('_scheduler_mode') or '').strip()
    if runtime_mode not in {'browser', 'api', 'smart', 'recognition'}:
        runtime_mode = ''

    # 默认品牌取第一个关键词的 brand
    default_brand = keywords[0].get('brand', '') if keywords else ''
    task_name = task.get('name', default_brand)
    history_task_id = str(task.get('_scheduler_original_task_id') or task.get('task_id') or '').strip()
    cloud_task_id = task.get('cloud_task_id') or task.get('cloudTaskId')
    daily_state_task = _task_for_daily_state(task)
    day_status = get_task_day_status(daily_state_task)
    historical_keyword_states = dict(day_status.get('keyword_states') or {})
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
        report = _build_execution_report(task, [], time.time() - started_at)
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

    platform_map = {
        'doubao': DoubaoPlatform, 'deepseek': DeepSeekPlatform, 'kimi': KimiPlatform,
        'yuanbao': YuanbaoPlatform, 'tongyi': TongyiPlatform, 'wenxin': WenxinPlatform,
        # 中文平台名别名
        '豆包': DoubaoPlatform, 'Kimi': KimiPlatform,
        '元宝': YuanbaoPlatform, '通义千问': TongyiPlatform, '文心一言': WenxinPlatform,
        'DeepSeek': DeepSeekPlatform,
    }

    all_results = []
    selected_results = []
    fixed_screenshot_enabled = bool(task.get('fixed_screenshot_enabled', False))
    executable_entries = []
    ordered_platforms = []
    entries_by_platform: dict[str, list[dict]] = defaultdict(list)
    for kw_index, kw_entry in enumerate(keywords):
        keyword = str(kw_entry.get('keyword', '') or '').strip()
        brand = str(kw_entry.get('brand', '') or '').strip() or default_brand
        mode = runtime_mode or str(kw_entry.get('mode', 'browser') or 'browser').strip()
        platforms = [str(p).strip() for p in (kw_entry.get('platforms', []) or []) if str(p).strip()]
        if not keyword or not platforms or mode == 'recognition':
            continue
        executable_entries.append({
            'index': kw_index,
            'keyword': keyword,
            'brand': brand,
            'platforms': platforms,
            'mode': mode,
            'kw_entry': kw_entry,
        })
        for platform_name in platforms:
            if platform_name not in ordered_platforms:
                ordered_platforms.append(platform_name)
            entries_by_platform[platform_name].append(executable_entries[-1])

    def _record_session_pool_skip(platform_name: str, mode: str) -> None:
        if not _should_use_session_pool_for_query(mode, task, platform_session_manager):
            return
        try:
            platform_session_manager.record_query_skipped(platform_name)
        except Exception as e:
            print(f"[Main] 复用会话跳过记录失败 ({platform_name}): {e}")

    def _historical_query_result(entry: dict, platform_name: str) -> dict | None:
        if manual_test_replay_completed_keywords:
            return None
        historical_result = historical_success_map.get(
            _make_query_result_key(entry['keyword'], platform_name, entry['brand'])
        )
        if (
            fixed_screenshot_enabled
            and historical_result
            and historical_result.get('rank', 99) != 99
            and not _result_has_usable_screenshot(historical_result)
        ):
            return None
        return historical_result

    total_queries = sum(
        1
        for entry in executable_entries
        for platform_name in (entry.get('platforms', []) or [])
        if not _historical_query_result(entry, platform_name)
    )
    expected_query_count = _count_task_queries(keywords, default_brand)

    fixed_screenshot_target = 0
    if fixed_screenshot_enabled and executable_entries:
        try:
            raw_target = max(
                1,
                int(
                    task.get(
                        'fixed_screenshot_count',
                        task.get('recognition_batch_size', 1),
                    ) or 1
                ),
            )
        except Exception:
            raw_target = 1
        fixed_screenshot_target = max(raw_target, len(ordered_platforms))

    historical_selected_results = (
        [item for item in historical_success_map.values() if _result_has_usable_screenshot(item)]
        if fixed_screenshot_enabled
        else []
    )
    historical_completed_keywords = _dedupe_non_empty(
        [str(item.get('keyword') or '').strip() for item in historical_success_map.values()]
    )
    historical_detected_platforms = _dedupe_non_empty(
        [str(item.get('platform') or '').strip() for item in historical_success_map.values()]
    )
    historical_brands = _dedupe_non_empty(
        [str(item.get('brand') or '').strip() for item in historical_success_map.values()]
    )
    serial_platform_state = platform_serial_state if isinstance(platform_serial_state, dict) else {
        'name': '',
        'platform': None,
    }
    owns_serial_platform_state = serial_platform_state is not platform_serial_state

    def _close_serial_platform(reason: str = '') -> None:
        active_platform = serial_platform_state.get('platform')
        active_name = str(serial_platform_state.get('name') or '').strip()
        if active_platform is None:
            serial_platform_state['name'] = ''
            return
        if reason:
            print(f"[Main] 按平台分组串行策略关闭平台: {active_name or '未知平台'} ({reason})")
        try:
            active_platform.close()
        except Exception as e:
            print(f"[Main] 关闭串行平台失败 ({active_name or '未知平台'}): {e}")
        finally:
            serial_platform_state['platform'] = None
            serial_platform_state['name'] = ''

    def _acquire_serial_platform(platform_name: str, platform_class):
        active_platform = serial_platform_state.get('platform')
        active_name = str(serial_platform_state.get('name') or '').strip()
        needs_new_platform = (
            active_platform is None
            or active_name != platform_name
            or getattr(active_platform, 'page', None) is None
        )
        if not needs_new_platform:
            return active_platform
        if active_platform is not None and active_name != platform_name:
            _close_serial_platform(reason="切换到下一个平台")
        elif active_platform is not None and getattr(active_platform, 'page', None) is None:
            _close_serial_platform(reason="当前平台实例失效，准备重建")
        next_platform = _create_browser_platform(
            platform_name,
            platform_class,
            config=config or {},
            inspect=bool(task.get('inspect', False)),
            stop_checker=stop_checker,
        ).start()
        serial_platform_state['platform'] = next_platform
        serial_platform_state['name'] = platform_name
        print(f"[Main] 按平台分组串行策略切换平台: {platform_name}")
        return next_platform

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

    serial_ordered_platforms = list(ordered_platforms)
    active_serial_platform = str(serial_platform_state.get('name') or '').strip()
    route1_enabled = any(
        _should_use_platform_serial_for_query(entry['mode'], task, config, platform_session_manager)
        for entry in executable_entries
    )
    if route1_enabled and active_serial_platform and active_serial_platform in serial_ordered_platforms:
        serial_ordered_platforms = [active_serial_platform] + [
            platform_name
            for platform_name in serial_ordered_platforms
            if platform_name != active_serial_platform
        ]

    if total_queries <= 0:
        notify_result = _build_notification_result(
            attempted=False,
            success=False,
            found_results=len(historical_success_map),
            error_message='',
        )
        if historical_success_map and not sent_before_run:
            historical_results = list(historical_success_map.values())
            notify_result = _send_task_notifications(
                task,
                default_brand,
                notifier,
                keywords,
                historical_results,
                selected_results=historical_selected_results,
                fixed_screenshot_target=fixed_screenshot_target,
                fixed_screenshot_ready=True,
                expected_query_count=_count_task_queries(keywords, default_brand),
                force_notify=force_notify,
            )
        report = _build_execution_report(task, [], time.time() - started_at, notify_result=notify_result)
        report = _finalize_execution_report(report, notify_result)
        if fixed_screenshot_enabled:
            report['selected_screenshot_results'] = len(historical_selected_results)
            report['fixed_screenshot_target'] = fixed_screenshot_target
            report['completed_by_quota'] = _fixed_target_reached()
            report['skipped_remaining_queries'] = 0
        if report.get('task_status') == 'success':
            mark_task_sent(
                daily_state_task,
                source_mode=(SOURCE_MODE_TEST if execution_source == 'manual_test' else SOURCE_MODE_FORMAL),
                message="企业微信发送成功",
            )
        elif execution_source != 'manual_test':
            finish_formal_task_run(
                daily_state_task,
                message=str(report.get('task_failure_message') or '今日已无待执行关键词').strip(),
            )
        _emit_progress({
            "stage": "finished",
            "task_name": task_name,
            "total_queries": total_queries,
            "completed_queries": completed_queries,
            "hit_queries": hit_queries,
            "round_status": report.get("round_status", "skipped"),
            "success": report.get('task_status') == 'success',
            "completed_by_quota": bool(report.get('completed_by_quota', False)),
        })
        print(f"[Main] 任务组跳过: task={task_name}, 今日已无待执行查询")
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

        if mode == 'api':
            try:
                if _should_stop_remaining_work():
                    raise SchedulerStopRequested(stop_message)
                api_result = _run_api_task(
                    platform_name,
                    keyword,
                    brand,
                    config or {},
                    kw_entry=kw_entry,
                    progress_callback=_emit_progress,
                )
                diagnostic_id = ''
                if api_result.get('rank', 99) == 99 and api_result.get('error_message'):
                    diagnostic_id = _record_diagnostic(
                        task_name,
                        keyword,
                        platform_name,
                        brand,
                        api_result.get('error_message', ''),
                        category='query',
                        details={'mode': 'api'},
                    )
                result = _build_result(
                    keyword,
                    platform_name,
                    brand,
                    api_result.get('rank', 99),
                    api_result.get('screenshot'),
                    answer_text=api_result.get('answer_text', ''),
                    evidence=api_result.get('evidence', ''),
                    error_message=api_result.get('error_message', ''),
                    highlight_count=api_result.get('highlight_count', 0),
                    mode='api',
                    diagnostic_id=diagnostic_id,
                    recovered_manually=api_result.get('recovered_manually', False),
                )
                all_results.append(result)
                completed_queries += 1
                if result.get('rank', 99) != 99:
                    hit_queries += 1
                _notify_issue_for_result(result)
                _emit_progress({
                    "stage": "query_done",
                    "task_name": task_name,
                    "keyword": keyword,
                    "brand": brand,
                    "platform": platform_name,
                    "current_query": completed_queries,
                    "total_queries": total_queries,
                    "completed_queries": completed_queries,
                    "hit_queries": hit_queries,
                    "success": result.get('rank', 99) != 99,
                    "error_message": result.get('error_message', ''),
                })
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
                return result
            except SchedulerStopRequested:
                raise
            except Exception as e:
                print(f"[Main] API任务失败 ({platform_name}/{keyword}): {e}\n{traceback.format_exc()}")
                diagnostic_id = _record_diagnostic(
                    task_name,
                    keyword,
                    platform_name,
                    brand,
                    str(e),
                    category='query',
                    details={'mode': 'api'},
                )
                result = _build_result(keyword, platform_name, brand, 99, None, error_message=str(e), mode='api', diagnostic_id=diagnostic_id)
                all_results.append(result)
                completed_queries += 1
                _notify_issue_for_result(result)
                _emit_progress({
                    "stage": "query_done",
                    "task_name": task_name,
                    "keyword": keyword,
                    "brand": brand,
                    "platform": platform_name,
                    "current_query": completed_queries,
                    "total_queries": total_queries,
                    "completed_queries": completed_queries,
                    "hit_queries": hit_queries,
                    "success": False,
                    "error_message": str(e),
                })
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
                return result

        platform_class = platform_map.get(platform_name)
        if not platform_class:
            print(f"[Main] 不支持的平台: {platform_name}")
            diagnostic_id = _record_diagnostic(
                task_name,
                keyword,
                platform_name,
                brand,
                "当前仅支持 API 模式或尚未接入浏览器适配",
                category='platform',
            )
            result = _build_result(
                keyword, platform_name, brand, 99, None,
                error_message='当前仅支持 API 模式或尚未接入浏览器适配',
                mode=mode,
                diagnostic_id=diagnostic_id,
            )
            all_results.append(result)
            completed_queries += 1
            _notify_issue_for_result(result)
            _emit_progress({
                "stage": "query_done",
                "task_name": task_name,
                "keyword": keyword,
                "brand": brand,
                "platform": platform_name,
                "current_query": completed_queries,
                "total_queries": total_queries,
                "completed_queries": completed_queries,
                "hit_queries": hit_queries,
                "success": False,
                "error_message": '当前仅支持 API 模式或尚未接入浏览器适配',
            })
            return result

        if mode == 'smart':
            try:
                use_session_pool = _should_use_session_pool_for_query(mode, task, platform_session_manager)
                use_platform_serial = _should_use_platform_serial_for_query(mode, task, config, platform_session_manager)
                active_platform = None
                if use_session_pool:
                    active_platform = platform_session_manager.get_or_create(
                        platform_name,
                        lambda: _create_browser_platform(
                            platform_name,
                            platform_class,
                            config=config or {},
                            inspect=bool(task.get('inspect', False)),
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
                    active_platform.progress_callback = _emit_progress
                    active_platform.stop_checker = stop_checker
                    platform_session_manager.mark_query_started(platform_name)
                elif use_platform_serial:
                    active_platform = _acquire_serial_platform(platform_name, platform_class)
                    _sync_reused_platform_runtime_state(
                        platform_name,
                        active_platform,
                        task,
                        kw_entry,
                        config=config,
                        stop_checker=stop_checker,
                    )
                    active_platform.progress_callback = _emit_progress
                    active_platform.stop_checker = stop_checker

                query_started_at = time.time()
                smart_result = _run_smart_browser_task(
                    platform_name=platform_name,
                    keyword=keyword,
                    brand=brand,
                    task=task,
                    kw_entry=kw_entry,
                    config=config or {},
                    platform_class=platform_class,
                    progress_callback=_emit_progress,
                    stop_checker=stop_checker,
                    platform=active_platform,
                )
                diagnostic_id = ''
                if smart_result.get('rank', 99) == 99 and smart_result.get('error_message'):
                    diagnostic_id = _record_diagnostic(
                        task_name,
                        keyword,
                        platform_name,
                        brand,
                        smart_result.get('error_message', ''),
                        category='query',
                        details={'mode': 'smart'},
                    )
                result = _build_result(
                    keyword,
                    platform_name,
                    brand,
                    smart_result.get('rank', 99),
                    smart_result.get('screenshot'),
                    answer_text=smart_result.get('answer_text', ''),
                    evidence=smart_result.get('evidence', ''),
                    error_message=smart_result.get('error_message', ''),
                    highlight_count=smart_result.get('highlight_count', 0),
                    mode='smart',
                    diagnostic_id=diagnostic_id,
                    recovered_manually=smart_result.get('recovered_manually', False),
                )
                if use_session_pool:
                    try:
                        platform_session_manager.record_query_result(
                            platform_name,
                            _classify_result_outcome(result),
                            duration_seconds=time.time() - query_started_at,
                            recovered_manually=bool(result.get('recovered_manually', False)),
                        )
                    except Exception as session_error:
                        print(f"[Main] 复用会话结果记录失败 ({platform_name}): {session_error}")
                all_results.append(result)
                completed_queries += 1
                if result.get('rank', 99) != 99:
                    hit_queries += 1
                _notify_issue_for_result(result)
                _emit_progress({
                    "stage": "query_done",
                    "task_name": task_name,
                    "keyword": keyword,
                    "brand": brand,
                    "platform": platform_name,
                    "current_query": completed_queries,
                    "total_queries": total_queries,
                    "completed_queries": completed_queries,
                    "hit_queries": hit_queries,
                    "success": result.get('rank', 99) != 99,
                    "error_message": result.get('error_message', ''),
                })
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
                return result
            except SchedulerStopRequested:
                raise
            except Exception as e:
                print(f"[Main] 智能模式任务失败 ({platform_name}/{keyword}): {e}\n{traceback.format_exc()}")
                diagnostic_id = _record_diagnostic(
                    task_name,
                    keyword,
                    platform_name,
                    brand,
                    str(e),
                    category='query',
                    details={'mode': 'smart'},
                )
                result = _build_result(keyword, platform_name, brand, 99, None, error_message=str(e), mode='smart', diagnostic_id=diagnostic_id)
                if _should_use_session_pool_for_query(mode, task, platform_session_manager):
                    try:
                        platform_session_manager.record_query_result(
                            platform_name,
                            _classify_result_outcome(result),
                            duration_seconds=0.0,
                            recovered_manually=False,
                        )
                    except Exception as session_error:
                        print(f"[Main] 复用会话异常记录失败 ({platform_name}): {session_error}")
                all_results.append(result)
                completed_queries += 1
                _notify_issue_for_result(result)
                _emit_progress({
                    "stage": "query_done",
                    "task_name": task_name,
                    "keyword": keyword,
                    "brand": brand,
                    "platform": platform_name,
                    "current_query": completed_queries,
                    "total_queries": total_queries,
                    "completed_queries": completed_queries,
                    "hit_queries": hit_queries,
                    "success": False,
                    "error_message": str(e),
                })
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
                return result

        use_session_pool = _should_use_session_pool_for_query(mode, task, platform_session_manager)
        use_platform_serial = _should_use_platform_serial_for_query(mode, task, config, platform_session_manager)
        pooled_platform = None
        query_started_at = time.time()
        if use_session_pool:
            pooled_platform = platform_session_manager.get_or_create(
                platform_name,
                lambda: _create_browser_platform(
                    platform_name,
                    platform_class,
                    config=config or {},
                    inspect=bool(task.get('inspect', False)),
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
            pooled_platform.progress_callback = _emit_progress
            pooled_platform.stop_checker = stop_checker
            pooled_platform.screenshot_on_mention = task.get('screenshot_on_mention', False)
            pooled_platform.deep_think = kw_entry.get('deep_think', {}).get(platform_name, False)
            pooled_platform.extract_references_enabled = bool(task.get('extract_references_enabled', False))
            platform_session_manager.mark_query_started(platform_name)
        elif use_platform_serial:
            pooled_platform = _acquire_serial_platform(platform_name, platform_class)
            _sync_reused_platform_runtime_state(
                platform_name,
                pooled_platform,
                task,
                kw_entry,
                config=config,
                stop_checker=stop_checker,
            )
            pooled_platform.progress_callback = _emit_progress
            pooled_platform.stop_checker = stop_checker
            pooled_platform.screenshot_on_mention = task.get('screenshot_on_mention', False)
            pooled_platform.deep_think = kw_entry.get('deep_think', {}).get(platform_name, False)
            pooled_platform.extract_references_enabled = bool(task.get('extract_references_enabled', False))
        else:
            pooled_platform = _create_browser_platform(
                platform_name,
                platform_class,
                config=config or {},
                inspect=bool(task.get('inspect', False)),
                stop_checker=stop_checker,
            )
            pooled_platform.screenshot_on_mention = task.get('screenshot_on_mention', False)
            pooled_platform.deep_think = kw_entry.get('deep_think', {}).get(platform_name, False)
            pooled_platform.extract_references_enabled = bool(task.get('extract_references_enabled', False))
            pooled_platform.progress_callback = _emit_progress

        try:
            def _run_browser_query(active_platform):
                nonlocal completed_queries, hit_queries
                rank, screenshot = active_platform.search(keyword, brand)
                diagnostic_id = ''
                if rank == 99 and active_platform.last_error:
                    diagnostic_id = _record_diagnostic(
                        task_name,
                        keyword,
                        platform_name,
                        brand,
                        active_platform.last_error,
                        category='query',
                        details={'mode': 'browser'},
                    )
                result = _build_result(
                    keyword,
                    platform_name,
                    brand,
                    rank,
                    screenshot,
                    answer_text=active_platform.last_answer_text,
                    evidence=brand if rank != 99 else '',
                    error_message=active_platform.last_error,
                    highlight_count=active_platform.last_screenshot_meta.get('highlight_count', 0),
                    mode='browser',
                    diagnostic_id=diagnostic_id,
                    recovered_manually=bool(getattr(active_platform, 'last_run_recovered_manually', False)),
                    references=getattr(active_platform, 'last_references', []),
                    body_references=getattr(active_platform, 'last_body_references', []),
                )
                if use_session_pool:
                    try:
                        platform_session_manager.record_query_result(
                            platform_name,
                            _classify_result_outcome(result),
                            duration_seconds=time.time() - query_started_at,
                            recovered_manually=bool(result.get('recovered_manually', False)),
                        )
                    except Exception as session_error:
                        print(f"[Main] 复用会话结果记录失败 ({platform_name}): {session_error}")
                all_results.append(result)
                completed_queries += 1
                if result.get('rank', 99) != 99:
                    hit_queries += 1
                _notify_issue_for_result(result)
                _emit_progress({
                    "stage": "query_done",
                    "task_name": task_name,
                    "keyword": keyword,
                    "brand": brand,
                    "platform": platform_name,
                    "current_query": completed_queries,
                    "total_queries": total_queries,
                    "completed_queries": completed_queries,
                    "hit_queries": hit_queries,
                    "success": result.get('rank', 99) != 99,
                    "error_message": result.get('error_message', ''),
                })
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
                return result
            if use_session_pool or use_platform_serial:
                return _run_browser_query(pooled_platform)
            with pooled_platform:
                return _run_browser_query(pooled_platform)
        except SchedulerStopRequested:
            raise
        except Exception as e:
            print(f"[Main] 任务执行失败 ({platform_name}/{keyword}): {e}\n{traceback.format_exc()}")
            diagnostic_id = _record_diagnostic(
                task_name,
                keyword,
                platform_name,
                brand,
                str(e),
                category='query',
                details={'mode': 'browser'},
            )
            result = _build_result(keyword, platform_name, brand, 99, None, error_message=str(e), mode='browser', diagnostic_id=diagnostic_id)
            if use_session_pool:
                try:
                    platform_session_manager.record_query_result(
                        platform_name,
                        _classify_result_outcome(result),
                        duration_seconds=time.time() - query_started_at,
                        recovered_manually=False,
                    )
                except Exception as session_error:
                    print(f"[Main] 复用会话异常记录失败 ({platform_name}): {session_error}")
            all_results.append(result)
            completed_queries += 1
            _notify_issue_for_result(result)
            _emit_progress({
                "stage": "query_done",
                "task_name": task_name,
                "keyword": keyword,
                "brand": brand,
                "platform": platform_name,
                "current_query": completed_queries,
                "total_queries": total_queries,
                "completed_queries": completed_queries,
                "hit_queries": hit_queries,
                "success": False,
                "error_message": str(e),
            })
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
            return result

    try:
        if fixed_screenshot_enabled and executable_entries:
            print(
                f"[Main] 固定截图策略启动: task={task.get('name', default_brand)}, "
                f"target={fixed_screenshot_target}, platform_floor={len(ordered_platforms)}"
            )
            covered_platforms: set[str] = {
                str(item.get('platform') or '').strip()
                for item in historical_selected_results
                if str(item.get('platform') or '').strip()
            }
            hit_keyword_indexes: set[int] = set()
            hit_platforms_by_keyword: dict[int, set[str]] = defaultdict(set)
            tried_pairs: set[tuple[int, str]] = set()

            for item in historical_selected_results:
                item_keyword = str(item.get('keyword') or '').strip()
                item_brand = str(item.get('brand') or '').strip()
                item_platform = str(item.get('platform') or '').strip()
                for entry in executable_entries:
                    if entry['keyword'] != item_keyword or entry['brand'] != item_brand:
                        continue
                    hit_keyword_indexes.add(entry['index'])
                    if item_platform:
                        hit_platforms_by_keyword[entry['index']].add(item_platform)

            # 第一轮：优先确保每个平台至少拿到一张。
            for platform_name in serial_ordered_platforms:
                if _should_stop_remaining_work():
                    break
                if _fixed_target_reached():
                    break
                if platform_name in covered_platforms:
                    continue
                for entry in executable_entries:
                    if _should_stop_remaining_work():
                        break
                    if platform_name not in entry['platforms']:
                        continue
                    pair = (entry['index'], platform_name)
                    if pair in tried_pairs:
                        continue
                    tried_pairs.add(pair)
                    result = _execute_query(entry, platform_name)
                    if result and result.get('rank', 99) != 99:
                        covered_platforms.add(platform_name)
                        hit_keyword_indexes.add(entry['index'])
                        hit_platforms_by_keyword[entry['index']].add(platform_name)
                        _append_selected_result(result)
                        break

            # 第二轮：平台已覆盖后，优先补足尚未命中的关键词。
            if not _fixed_target_reached() and not _should_stop_remaining_work():
                for entry in executable_entries:
                    if _should_stop_remaining_work():
                        break
                    if _fixed_target_reached():
                        break
                    if entry['index'] in hit_keyword_indexes:
                        continue
                    for platform_name in entry['platforms']:
                        if _should_stop_remaining_work():
                            break
                        pair = (entry['index'], platform_name)
                        if pair in tried_pairs:
                            continue
                        tried_pairs.add(pair)
                        result = _execute_query(entry, platform_name)
                        if result and result.get('rank', 99) != 99:
                            covered_platforms.add(platform_name)
                            hit_keyword_indexes.add(entry['index'])
                            hit_platforms_by_keyword[entry['index']].add(platform_name)
                            _append_selected_result(result)
                            break

            # 第三轮：如果只差 1-2 张，则允许已命中过的关键词换其它平台补差。
            remaining_slots = max(0, fixed_screenshot_target - len(historical_selected_results) - len(selected_results))
            if 0 < remaining_slots <= 2 and not _should_stop_remaining_work():
                for entry in executable_entries:
                    if _should_stop_remaining_work():
                        break
                    if _fixed_target_reached():
                        break
                    if entry['index'] not in hit_keyword_indexes:
                        continue
                    for platform_name in entry['platforms']:
                        if _should_stop_remaining_work():
                            break
                        pair = (entry['index'], platform_name)
                        if pair in tried_pairs:
                            continue
                        if platform_name in hit_platforms_by_keyword.get(entry['index'], set()):
                            continue
                        tried_pairs.add(pair)
                        result = _execute_query(entry, platform_name)
                        if result and result.get('rank', 99) != 99:
                            covered_platforms.add(platform_name)
                            hit_platforms_by_keyword[entry['index']].add(platform_name)
                            _append_selected_result(result)
                        if _fixed_target_reached():
                            break
        else:
            if route1_enabled:
                print(f"[Main] 任务组启用按平台分组串行策略: task={task.get('name', default_brand)}")
                for platform_name in serial_ordered_platforms:
                    if _should_stop_remaining_work():
                        break
                    platform_entries = entries_by_platform.get(platform_name, [])
                    if not platform_entries:
                        continue
                    for entry in platform_entries:
                        if _should_stop_remaining_work():
                            break
                        if entry['mode'] == 'recognition':
                            print(f"[Main] 识别模式任务由剪切板监听器处理，跳过主动查询: {task.get('name', default_brand)}")
                            continue
                        _execute_query(entry, platform_name)
                    next_platform_name = ''
                    current_platform_index = serial_ordered_platforms.index(platform_name)
                    for candidate in serial_ordered_platforms[current_platform_index + 1:]:
                        candidate_entries = entries_by_platform.get(candidate, [])
                        if candidate_entries:
                            next_platform_name = candidate
                            break
                    if next_platform_name:
                        _close_serial_platform(reason=f"{platform_name} 平台批量执行结束，准备切换到 {next_platform_name}")
            elif (
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
                    if _should_stop_remaining_work():
                        break
                    _execute_query(entry, platform_name)
            else:
                for kw_entry in keywords:
                    if _should_stop_remaining_work():
                        break
                    keyword = kw_entry.get('keyword', '').strip()
                    brand = kw_entry.get('brand', '').strip() or default_brand
                    platforms = kw_entry.get('platforms', [])
                    task_name = task.get('name', default_brand)
                    mode = runtime_mode or str(kw_entry.get('mode', 'browser') or 'browser').strip()

                    if not keyword or not platforms:
                        continue

                    print(
                        f"[Main] 任务调度: task={task_name}, keyword={keyword}, brand={brand}, "
                        f"mode={mode}, configured_platforms={platforms}"
                    )

                    for platform_name in platforms:
                        if _should_stop_remaining_work():
                            break
                        if mode == 'recognition':
                            print(f"[Main] 识别模式任务由剪切板监听器处理，跳过主动查询: {task.get('name', default_brand)}")
                            continue
                        _execute_query(
                            {
                                'index': -1,
                                'keyword': keyword,
                                'brand': brand,
                                'platforms': platforms,
                                'mode': mode,
                                'kw_entry': kw_entry,
                            },
                            platform_name,
                        )
    except SchedulerStopRequested:
        scheduler_cancelled = True
    finally:
        if owns_serial_platform_state:
            _close_serial_platform(reason="任务组执行结束")

    if platform_session_manager is not None:
        try:
            platform_session_manager.close_exhausted_sessions()
        except Exception as e:
            print(f"[Main] 复用会话清理失败: {e}")

    if _should_stop_remaining_work():
        notify_result = _build_notification_result(
            attempted=False,
            success=False,
            found_results=len([item for item in all_results if item.get('rank', 99) != 99]),
            error_message='',
        )
        report = _build_execution_report(
            task,
            all_results,
            time.time() - started_at,
            notify_result=notify_result,
        )
        report['round_status'] = 'pending'
        report['query_round_status'] = 'pending'
        report['task_status'] = 'pending'
        report['task_failure_kind'] = 'cancelled'
        report['task_failure_message'] = stop_message
        report['_scheduler_cancelled'] = True
        if fixed_screenshot_enabled:
            report['selected_screenshot_results'] = len(historical_selected_results) + len(selected_results)
            report['fixed_screenshot_target'] = fixed_screenshot_target
            report['completed_by_quota'] = _fixed_target_reached()
            report['skipped_remaining_queries'] = max(total_queries - completed_queries, 0)
        if execution_source != 'manual_test':
            write_task_status(
                daily_state_task,
                status='pending',
                source=execution_source,
                message=stop_message,
                extra=build_daily_task_state_extra(
                    task_status='pending',
                    query_round_status='pending',
                    failed_queries=report.get('failed_queries', 0),
                    task_failure_kind='cancelled',
                    notification_success=False,
                ),
            )
        _emit_progress(_build_finished_progress_payload(
            task.get('name', default_brand),
            report,
            total_queries=total_queries,
            completed_queries=completed_queries,
            hit_queries=hit_queries,
        ))
        print(
            f"[Main] 任务组已暂停: task={task.get('name', default_brand)}, "
            f"completed={completed_queries}/{total_queries}, hits={hit_queries}"
        )
        if return_report:
            return all_results, report
        return all_results

    _finalize_daily_pool_keyword_results(
        daily_state_task,
        all_results=all_results,
        execution_source=execution_source,
        historical_keyword_states=historical_keyword_states,
    )
    historical_keyword_states = dict(get_task_day_status(daily_state_task).get('keyword_states') or {})

    current_hit_results = [item for item in all_results if item.get('rank', 99) != 99]
    notify_all_results = all_results
    notify_selected_results = selected_results
    if historical_success_map:
        current_result_keys = {
            _make_query_result_key(
                item.get('keyword', ''),
                item.get('platform', ''),
                item.get('brand', ''),
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

    notify_result = _send_task_notifications(
        task,
        default_brand,
        notifier,
        keywords,
        notify_all_results,
        selected_results=notify_selected_results,
        fixed_screenshot_target=fixed_screenshot_target,
        fixed_screenshot_ready=_fixed_target_reached(),
        expected_query_count=expected_query_count,
        force_notify=force_notify,
    )
    if bool(notify_result.get('attempted')) and not bool(notify_result.get('success')):
        _emit_issue({
            'title': '企业微信发送异常',
            'task_name': task_name,
            'mode': str(task.get('_scheduler_mode') or '').strip(),
            'brand': default_brand,
            'message': str(notify_result.get('error_message') or '企业微信发送未成功').strip(),
        })

    report = _build_execution_report(
        task,
        all_results,
        time.time() - started_at,
        notify_result=notify_result,
    )
    report = _finalize_execution_report(report, notify_result)
    if fixed_screenshot_enabled:
        report['selected_screenshot_results'] = len(historical_selected_results) + len(selected_results)
        report['fixed_screenshot_target'] = fixed_screenshot_target
        report['completed_by_quota'] = _fixed_target_reached()
        report['skipped_remaining_queries'] = max(total_queries - completed_queries, 0)

    if report.get('task_status') == 'success':
        if execution_source == 'manual_test':
            write_task_status(
                daily_state_task,
                status='success',
                source=execution_source,
                scope='test',
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
                status='success',
                source=execution_source,
                message="企业微信发送成功",
                extra=_build_task_state_extra(report, notify_result),
            )
    else:
        failure_message = (
            report.get('task_failure_message')
            or report.get('failure_kind')
            or notify_result.get('error_message')
            or "存在失败查询，等待下次调度重试"
        )
        failure_status = str(report.get('task_status') or 'failed').strip() or 'failed'
        failure_extra = _build_task_state_extra(report, notify_result)
        if (
            execution_source != 'manual_test'
            and str(report.get('task_failure_kind') or '').strip() == 'notification'
        ):
            mark_task_send_failure(
                daily_state_task,
                source_mode=SOURCE_MODE_FORMAL,
                message=failure_message,
            )
        elif execution_source != 'manual_test':
            finish_formal_task_run(
                daily_state_task,
                message=failure_message,
            )
        if execution_source == 'manual_test':
            write_task_status(
                daily_state_task,
                status=failure_status,
                source=execution_source,
                scope='test',
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
    _emit_progress(_build_finished_progress_payload(
        task.get('name', default_brand),
        report,
        total_queries=total_queries,
        completed_queries=completed_queries,
        hit_queries=hit_queries,
    ))
    print(
        f"[Main] 任务组完成: task={task.get('name', default_brand)}, "
        f"query_round_status={report.get('query_round_status', report.get('round_status', 'success'))}, "
        f"task_status={resolve_report_status(report)}, "
        f"completed={completed_queries}/{total_queries}, hits={hit_queries}"
    )
    if return_report:
        return all_results, report
    return all_results


def main(argv: list[str] | None = None):
    """主函数"""
    args = parse_args(argv)
    setup_logging()
    install_shutdown_handlers()

    if args.prepare_local_runtime:
        config_path = resolve_app_path("config.yaml")
        try:
            result = prepare_local_runtime(
                config_path,
                model_override=args.prepare_local_runtime_model,
                timeout_seconds=args.prepare_local_runtime_timeout,
                allow_model_pull=not args.prepare_local_runtime_no_model_pull,
            )
            message = str(result.get("message") or "").strip() or "本地运行环境准备完成"
            print(f"[LocalRuntimePrep] {message}")
            return 0 if result.get("ok") else 1
        except Exception as exc:
            print(f"[LocalRuntimePrep] 本地运行环境准备失败: {exc}")
            logging.exception("本地运行环境准备失败")
            return 1
        finally:
            shutdown_owned_local_runtime()

    if args.web:
        try:
            from web_desktop import run_desktop_app

            run_desktop_app()
        except Exception as e:
            print(f"[Main] Web UI 启动失败: {e}")
        return

    print("=" * 50)
    print(f"{APP_NAME} 启动")
    print(f"[Main] 应用版本: {get_version_label()}")
    print("=" * 50)

    # 1. 加载配置
    config_path = resolve_app_path("config.yaml")
    config = load_config(config_path)
    if not config:
        print("[Main] 配置文件加载失败，使用默认配置")
        config = {
            'scheduler': {
                'weekly_times': {'0': '09:30', '1': '09:30', '2': '09:30', '3': '09:30', '4': '09:30', '5': None, '6': None},
                'notification_webhook_url': '',
            },
            'default_notification': {
                'cooldown_minutes': 30,
                'send_interval': 2,
                'failure_alert_threshold': 7,
                'failure_alert_cooldown_minutes': 5,
            },
            'tasks': []
        }
    ensure_config_task_ids(config)
    get_local_model_manager().sync_config(config)

    # 2. 清理过期截图
    cleanup_screenshots(config)

    # 3. 创建核心组件
    scheduler_config = dict(config.get('scheduler', {}) or {})
    scheduler_config['detection_mode'] = str(config.get('detection_mode', 'browser') or 'browser').strip()
    scheduler = SmartScheduler(scheduler_config)
    runtime_state = {"config": config}
    round_session_state = {"mode": "", "manager": None}
    round_serial_state = {"mode": "", "state": None}
    scheduler_reporter = SchedulerWebhookReporter(lambda: runtime_state.get("config") or {})

    def execute_task(task):
        # 向后兼容：旧格式补全 keywords
        if 'keyword' in task and 'keywords' not in task:
            task = {**task, 'keywords': [{'keyword': task['keyword'], 'brand': task.get('brand', ''), 'platforms': [task['platform']]}]}

        task_name = task.get('name', '')
        keywords = task.get('keywords', [])
        default_brand = keywords[0].get('brand', '') if keywords else ''
        if not task_name:
            task_name = default_brand

        current_config = runtime_state["config"] or {}
        results = run_task_group(
            task,
            current_config.get('default_notification', {}),
            current_config,
            execution_source='auto',
            return_report=True,
            issue_callback=scheduler_reporter.handle_issue_payload,
            stop_checker=scheduler.should_stop,
            platform_session_manager=round_session_state.get("manager"),
            platform_serial_state=round_serial_state.get("state"),
        )
        if isinstance(results, tuple):
            result_items, execution_report = results
        else:
            result_items, execution_report = results, {}
        return result_items, execution_report

    def begin_mode_round(mode, ordered_units, current_date, scheduler_runtime_config):
        del current_date, scheduler_runtime_config
        current_config = runtime_state["config"] or {}
        policy = build_query_execution_policy(current_config, mode)
        manager = None
        try:
            recognition_status = app.recognition_manager.get_runtime_status() if app.recognition_manager else {}
        except Exception:
            recognition_status = {}
        if app.running and app.recognition_manager and app.recognition_manager.has_recognition_tasks() and not recognition_status.get("running"):
            try:
                app._post(lambda root: app._start_recognition_mode())
            except Exception as exc:
                print(f"[Main] 定时轮次启动识别监听失败: {exc}")
        if policy.use_session_pool:
            manager = PlatformSessionManager(
                mode,
                policy,
                build_round_query_plan(ordered_units, mode),
                logger=print,
            )
            print(f"[Main] {mode} 模式启用平台会话池策略(实验中)")
        else:
            print(f"[Main] {mode} 模式沿用按平台分组串行策略")
        round_session_state["mode"] = mode
        round_session_state["manager"] = manager
        round_serial_state["mode"] = mode
        round_serial_state["state"] = {"name": "", "platform": None} if policy.use_platform_serial else None

    def _close_round_runtime(reason: str) -> None:
        manager = round_session_state.get("manager")
        if manager is not None:
            manager.close_all(reason=reason)
        serial_state = round_serial_state.get("state")
        if isinstance(serial_state, dict):
            active_platform = serial_state.get("platform")
            if active_platform is not None:
                try:
                    active_platform.close()
                except Exception:
                    pass
        round_session_state["mode"] = ""
        round_session_state["manager"] = None
        round_serial_state["mode"] = ""
        round_serial_state["state"] = None

    def end_mode_round(mode, ordered_units, current_date, mode_reports, cancelled):
        del mode, ordered_units, current_date, mode_reports, cancelled
        _close_round_runtime("当前模式轮次结束")

    # 4. 预检 GUI 运行环境，避免 tkinter/Tk 原生 abort 直接导致主进程崩溃
    if not check_gui_runtime():
        return

    # 5. 创建托盘应用
    from core.recognition import ClipboardRecognitionManager
    from ui.tray import TrayApp

    app = TrayApp(scheduler, None, config)
    app.recognition_manager = ClipboardRecognitionManager(
        config_getter=lambda: app.config,
        on_batch_ready=app._on_recognition_batch_ready,
        on_send_complete=app._on_recognition_send_complete,
        on_round_complete=app._on_recognition_round_complete,
        on_mode_change=app._on_recognition_mode_change,
        on_manual_switch_required=app._on_recognition_manual_switch_required,
        on_manual_state_change=app._on_recognition_manual_state_change,
    )
    config_path = resolve_app_path("config.yaml")

    def _save_and_apply_runtime_config(new_config):
        from core.config_watcher import save_config as save_yaml_config

        save_yaml_config(new_config, config_path)
        app.apply_config(new_config, reload_runtime=False)
        runtime_state["config"] = app.config
        get_local_model_manager().sync_config(app.config)

    def _apply_remote_sync_bundle(bundle: dict, mode: str = "merge"):
        current_config = load_config(config_path)
        result = apply_sync_bundle(
            config_path,
            current_config,
            {"bundle": bundle, "mode": mode},
            mode=mode,
        )
        app.apply_config(result.get("config", current_config), reload_runtime=app.running)
        runtime_state["config"] = app.config
        get_local_model_manager().sync_config(app.config)
        return result

    app.cloud_sync_manager = CloudSyncManager(
        config_getter=lambda: app.config or load_config(config_path),
        bundle_builder=lambda include_secrets=False: build_sync_bundle(
            app.config or load_config(config_path),
            include_secrets=include_secrets,
        ),
        bundle_applier=_apply_remote_sync_bundle,
        config_updater=_save_and_apply_runtime_config,
        logger=print,
    )

    def _shutdown_tray_runtime() -> None:
        try:
            app.shutdown()
        finally:
            _close_round_runtime("进程退出")

    register_shutdown_callback("main.tray-runtime", _shutdown_tray_runtime)

    def tray_execute_task(task):
        runtime_state["config"] = app.config or runtime_state["config"]
        result_items, execution_report = execute_task(task)
        task_name = task.get('name', '')
        keywords = task.get('keywords', [])
        default_brand = keywords[0].get('brand', '') if keywords else ''
        if not task_name:
            task_name = default_brand
        for r in result_items:
            app.update_result(task_name, r['platform'], r['brand'], r['rank'])
        return execution_report
    tray_execute_task.begin_mode_round = begin_mode_round
    tray_execute_task.end_mode_round = end_mode_round

    def on_task_timeout(task, elapsed_seconds):
        task_name = task.get('name', '')
        keywords = task.get('keywords', [])
        kw_info = ', '.join(
            f"{kw.get('keyword','')}({kw.get('brand','')})"
            for kw in keywords[:3]
        )
        msg = f"任务组「{task_name}」已运行 {int(elapsed_seconds//60)} 分钟仍未结束\n关键词: {kw_info}"
        print(f"[Main] 超时警告: {msg}")
        scheduler_reporter.send_timeout(task, elapsed_seconds)
        app._post(lambda root, m=msg, tn=task_name: app._show_timeout_warning(root, m, tn))

    app._execute_task = tray_execute_task
    app._on_scheduler_round_complete = scheduler_reporter.send_mode_summary
    app._on_scheduler_cycle_complete = scheduler_reporter.send_cycle_summary
    app._on_scheduler_status_event = scheduler_reporter.handle_status_event
    app._on_recognition_send_complete_hook = scheduler_reporter.record_recognition_result
    app._on_recognition_round_complete_hook = scheduler_reporter.send_recognition_round_summary

    # 7. 启动托盘
    try:
        app.run()
    except KeyboardInterrupt:
        print("\n[Main] 收到中断信号，正在退出...")
        run_shutdown_callbacks("keyboard interrupt")


if __name__ == "__main__":
    sys.exit(main())
