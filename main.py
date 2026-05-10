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
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 添加项目目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from core.windows_bootstrap import install_windows_bootstrap
install_windows_bootstrap()

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
    apply_browser_runtime_config,
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
# Compatibility re-exports for older imports from main.py. New code should
# import task execution helpers from core.task_executor* modules directly.
from core.task_executor import (
    _apply_browser_runtime_config,
    _create_browser_platform,
    _run_api_task,
    _run_smart_browser_task,
    _should_use_platform_serial_for_query,
    _should_use_session_pool_for_query,
    _sync_reused_platform_runtime_state,
    run_task_group,
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
    """清理过期截图（按天数和总大小）。委托给 core.screenshot_cleanup。"""
    from core.screenshot_cleanup import cleanup_screenshots as _cleanup
    _cleanup(config)


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
