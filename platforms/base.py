"""
平台基类 - 提供通用的浏览器操作和排名解析逻辑
基于 doubao.json 的模式抽象
"""

import os
import re
import shlex
import signal
import socket
import sys
import time
import random
import html
import subprocess
import threading
import unicodedata
from dataclasses import dataclass, field
from abc import ABC
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

from core.app_paths import resolve_app_dir
from core.browser_processes import (
    browser_profile_owner_pids,
    browser_profile_process_tree_is_orphaned,
    is_browser_profile_in_use,
    terminate_browser_profile_processes,
)
from core.browser_runtime import resolve_system_browser_executable
from core.time_utils import local_now


_PROFILE_LAUNCH_LOCKS_GUARD = threading.RLock()
_PROFILE_LAUNCH_LOCKS: dict[str, threading.RLock] = {}
_BROWSER_EXTRA_ARG_DENY_PREFIXES = (
    "--user-data-dir",
    "--remote-debugging-port",
    "--remote-debugging-address",
    "--profile-directory",
    "--load-extension",
    "--disable-extensions-except",
)


def _is_safe_external_chrome_target_url(value: str) -> bool:
    text = str(value or "").strip()
    if not text or text.startswith("-") or any(char.isspace() or char == "\0" for char in text):
        return False
    parsed = urlparse(text)
    return parsed.scheme.lower() == "https" and bool(parsed.netloc)


def _is_safe_browser_extra_arg(value: str) -> bool:
    text = str(value or "").strip()
    if not text or not text.startswith("--") or any(char in text for char in "\r\n\0"):
        return False
    lowered = text.lower()
    return not any(lowered == prefix or lowered.startswith(f"{prefix}=") for prefix in _BROWSER_EXTRA_ARG_DENY_PREFIXES)


def _browser_profile_launch_lock(user_data_dir: str) -> threading.RLock:
    try:
        key = str(Path(user_data_dir).expanduser().resolve())
    except Exception:
        key = str(user_data_dir or "").strip()
    with _PROFILE_LAUNCH_LOCKS_GUARD:
        lock = _PROFILE_LAUNCH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROFILE_LAUNCH_LOCKS[key] = lock
        return lock


class InterruptionDetected(Exception):
    """人工干预完成后需要重新开始本次搜索尝试"""
    pass


class SchedulerStopRequested(Exception):
    """定时任务被关闭，当前浏览器查询需要立即终止"""
    pass


@dataclass
class AnswerBlock:
    key: str
    order: int
    text: str
    html: str
    updated_at: float


@dataclass
class AnswerCaptureSession:
    started_at: float
    root_key: str = ""
    blocks: dict[str, AnswerBlock] = field(default_factory=dict)
    last_materialized_text: str = ""
    last_materialized_html: str = ""
    last_update_at: float = 0.0


class BasePlatform(ABC):
    """AI平台监控基类"""

    _POST_COMPLETE_EMPTY_ANSWER_ERROR = "DOM信号已完成，但回答区正文未稳定，未获取到有效回答内容"

    # 子类必须定义
    target_url: str = ""
    target_url_aliases: list = []  # 合法的备用域名前缀，URL跳离检测时一并放行
    input_selector: str = "textarea"
    result_selector: str = "body"
    new_chat_selector: str = ""        # 新建对话按钮选择器
    chat_container_selector: str = ""  # 对话内容容器选择器（用于长截图）
    think_content_selector: str = ""   # 思考过程容器选择器（截图时排除）
    prefer_last_result_block: bool = False  # True=只读取最后一个回答块，避免命中旧内容或提问区
    always_headed: bool = False        # True=始终有头运行（百度等反爬强的平台）
    screenshot_brand_top_padding_px: int = 96
    screenshot_brand_top_ratio: float = 0.28
    use_automation_control_flag: bool = True
    use_automation_user_agent: bool = True
    use_automation_extra_headers: bool = True
    use_automation_ignore_default_args: bool = True
    use_automation_stealth_scripts: bool = True
    use_automation_storage_warmup: bool = True
    use_external_chrome_cdp: bool = False
    prefer_headed_runtime: bool = False
    external_chrome_launch_target_url: bool = False
    skip_runtime_startup_goto: bool = False
    external_chrome_light_control: bool = True
    force_reclaim_profile_processes_on_start: bool = False

    def __init__(self, user_data_dir: str):
        self.name = self.__class__.__name__.replace("Platform", "").lower()
        self.user_data_dir = user_data_dir
        self.context = None
        self.page = None
        self._playwright = None
        self._browser_connection = None
        self._external_browser_process = None
        self._external_browser_port = 0
        self.screenshot_on_mention = False
        self.answer_screenshot_mode = "page"
        self.extract_references_enabled = False
        self.deep_think = False
        self.inspect = False  # True=前台显示浏览器，False=后台无头
        self.last_answer_text = ""
        self.last_references = []       # 最近一次查询的信息源（点击展开的引用列表）
        self.last_body_references = []  # 最近一次查询的正文引用源（正文中的上标引用链接）
        self.last_screenshot_meta = {}
        self.last_error = ""
        self.last_interruption_state = {}
        self.last_run_recovered_manually = False
        self.progress_callback = None
        self._active_baseline_answer_text = ""
        self._answer_capture_session: AnswerCaptureSession | None = None
        self._last_prompt_text = ""
        self.stop_checker = None
        self._stop_requested = False
        self._stop_watcher_thread = None
        self._stop_watcher_event = threading.Event()
        self._stop_interrupt_lock = threading.Lock()
        self._stop_interrupt_started = False
        self._browser_app_label = "Google Chrome"
        self._current_context_headless = False
        self._browser_version_hint = ""
        self.browser_locale = "zh-CN"
        self.browser_accept_language = "zh-CN,zh;q=0.9,en;q=0.8"
        self.browser_timezone_id = ""
        self.browser_user_agent = ""
        self.browser_proxy_server = ""
        self.browser_proxy_username = ""
        self.browser_proxy_password = ""
        self.browser_extra_args = ""
        self.force_reclaim_profile_processes_on_start = bool(
            getattr(self, "force_reclaim_profile_processes_on_start", False)
        )
        self.page_stabilize_wait_min_ms = 1200
        self.page_stabilize_wait_max_ms = 2600
        self.failure_backoff_base_seconds = 3.0
        self.failure_backoff_max_seconds = 20.0

        # 增强：加载稳定的浏览器指纹配置
        from core.browser_fingerprint import BrowserFingerprint
        self._fingerprint = BrowserFingerprint(user_data_dir)

    def _scheduler_stop_requested(self) -> bool:
        if self._stop_requested:
            return True
        checker = getattr(self, "stop_checker", None)
        if not callable(checker):
            return False
        try:
            requested = bool(checker())
        except Exception:
            requested = False
        if requested:
            self._stop_requested = True
        return requested

    def _raise_if_stop_requested(self) -> None:
        if self._scheduler_stop_requested():
            self.last_error = "定时任务已关闭，当前浏览器查询已终止"
            raise SchedulerStopRequested(self.last_error)

    @staticmethod
    def _is_target_closed_error(exc: Exception) -> bool:
        text = str(exc or "")
        name = type(exc).__name__
        return (
            "Target page, context or browser has been closed" in text
            or "TargetClosedError" in name
            or "BrowserContext.close" in text
            or "Page.close" in text
        )

    def _reraise_stop_requested(self, exc: Exception) -> None:
        stop_requested = self._scheduler_stop_requested()
        if stop_requested or (self._stop_requested and self._is_target_closed_error(exc)):
            self.last_error = "定时任务已关闭，当前浏览器查询已终止"
            raise SchedulerStopRequested(self.last_error) from exc

    def _cooperative_sleep(self, seconds: float, interval: float = 0.1) -> None:
        deadline = time.time() + max(0.0, float(seconds or 0.0))
        while True:
            self._raise_if_stop_requested()
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            time.sleep(min(interval, remaining))

    def _wait_for_page_selector(self, selector: str, timeout_ms: int) -> None:
        deadline = time.time() + max(0.1, timeout_ms / 1000.0)
        last_error = None
        while time.time() < deadline:
            self._raise_if_stop_requested()
            try:
                remaining_ms = max(100, min(500, int((deadline - time.time()) * 1000)))
                self.page.wait_for_selector(selector, timeout=remaining_ms)
                self._raise_if_stop_requested()
                return
            except Exception as exc:
                last_error = exc
                self._reraise_stop_requested(exc)
        if last_error:
            raise last_error
        raise TimeoutError(f"等待元素超时: {selector}")

    def _wait_for_locator(self, locator, timeout_ms: int) -> None:
        deadline = time.time() + max(0.1, timeout_ms / 1000.0)
        last_error = None
        while time.time() < deadline:
            self._raise_if_stop_requested()
            try:
                remaining_ms = max(100, min(500, int((deadline - time.time()) * 1000)))
                locator.wait_for(timeout=remaining_ms)
                self._raise_if_stop_requested()
                return
            except Exception as exc:
                last_error = exc
                self._reraise_stop_requested(exc)
        if last_error:
            raise last_error
        raise TimeoutError("等待定位元素超时")

    def _click_locator(self, locator, timeout_ms: int = 5000, **kwargs) -> None:
        deadline = time.time() + max(0.1, timeout_ms / 1000.0)
        last_error = None
        while time.time() < deadline:
            self._raise_if_stop_requested()
            try:
                remaining_ms = max(200, min(800, int((deadline - time.time()) * 1000)))
                # 增强：点击前模拟鼠标悬停
                try:
                    locator.hover(timeout=min(1000, remaining_ms // 2))
                    self._cooperative_sleep(random.uniform(0.1, 0.3))
                except Exception:
                    pass
                locator.click(timeout=remaining_ms, **kwargs)
                self._raise_if_stop_requested()
                return
            except Exception as exc:
                last_error = exc
                self._reraise_stop_requested(exc)
                if "intercepts pointer events" in str(exc or ""):
                    try:
                        self.check_for_interruption(check_input_visible=False)
                    except InterruptionDetected:
                        raise
                self._cooperative_sleep(0.15)
        if last_error:
            raise last_error
        raise TimeoutError("点击元素超时")

    def _start_stop_watcher(self) -> None:
        self._stop_requested = False
        self._stop_interrupt_started = False
        self._stop_watcher_event.clear()
        if not callable(getattr(self, "stop_checker", None)):
            self._stop_watcher_thread = None
            return
        self._stop_watcher_thread = threading.Thread(
            target=self._stop_watcher_loop,
            name=f"{self.name}-stop-watcher",
            daemon=True,
        )
        self._stop_watcher_thread.start()

    def _stop_watcher_loop(self) -> None:
        while not self._stop_watcher_event.wait(0.15):
            if not self._scheduler_stop_requested():
                continue
            print(f"[{self.name}] 检测到定时任务关闭，正在终止当前浏览器查询")
            self._interrupt_for_scheduler_stop()
            return

    def _interrupt_for_scheduler_stop(self) -> None:
        """
        仅标记停止，不在 watcher 后台线程里直接关闭 Playwright 对象。

        sync_playwright 的 Page/Context 不是线程安全的；若在停止监听线程里直接
        close()，业务线程虽然能感知到 SchedulerStopRequested，但 Playwright
        内部仍可能留下未消费的 asyncio future，最终在终端打印
        "Future exception was never retrieved"。
        """
        with self._stop_interrupt_lock:
            if self._stop_interrupt_started:
                return
            self._stop_interrupt_started = True
        self._stop_requested = True

    def _live_context_pages(self) -> list:
        context = getattr(self, "context", None)
        if context is None:
            return []
        try:
            pages = list(context.pages)
        except Exception:
            return []
        live_pages = []
        for page in pages:
            try:
                if not page.is_closed():
                    live_pages.append(page)
            except Exception:
                continue
        return live_pages

    @staticmethod
    def _is_blank_page_url(url: str) -> bool:
        normalized = str(url or "").strip()
        if not normalized:
            return True
        return (
            normalized == "about:blank"
            or normalized.startswith("about:blank")
            or normalized in {"chrome://newtab/", "chrome://new-tab-page/"}
        )

    def _target_url_prefixes(self) -> list[str]:
        prefixes = []
        for value in [self.target_url, *(self.target_url_aliases or [])]:
            text = str(value or "").strip()
            if not text:
                continue
            prefixes.append(text.rstrip("/"))
        return prefixes

    def _is_target_page_url(self, url: str) -> bool:
        normalized = str(url or "").strip().rstrip("/")
        if not normalized:
            return False
        return any(normalized.startswith(prefix) for prefix in self._target_url_prefixes())

    @staticmethod
    def _page_url(page) -> str:
        try:
            return str(page.url or "")
        except Exception:
            return ""

    def _page_selection_score(self, page) -> int:
        url = self._page_url(page)
        if self._is_target_page_url(url):
            return 30
        if self._is_blank_page_url(url):
            return 0
        if url.startswith("chrome://") or url.startswith("devtools://"):
            return 1
        return 10

    def _ensure_primary_page(
        self,
        *,
        retries: int = 2,
        wait_seconds: float = 0.2,
        close_extra_pages: bool = True,
    ):
        last_error: Exception | None = None
        attempts = max(0, int(retries)) + 1
        for attempt in range(attempts):
            live_pages = self._live_context_pages()
            if live_pages:
                scored_pages = [
                    (self._page_selection_score(page), index, page)
                    for index, page in enumerate(live_pages)
                ]
                best_score, _, selected_page = max(scored_pages, key=lambda item: (item[0], item[1]))
                if best_score <= 0 and attempt < attempts - 1:
                    self._cooperative_sleep(wait_seconds)
                    continue

                self.page = selected_page
                selected_url = self._page_url(selected_page)
                selected_is_target = self._is_target_page_url(selected_url)
                if close_extra_pages:
                    for extra in live_pages:
                        if extra is selected_page:
                            continue
                        extra_url = self._page_url(extra)
                        if best_score <= 0:
                            continue
                        if not selected_is_target and not self._is_blank_page_url(extra_url):
                            continue
                        try:
                            extra.close()
                        except Exception:
                            pass
                return self.page
            try:
                self.page = self.context.new_page()
                return self.page
            except Exception as exc:
                last_error = exc
                print(f"[{self.name}] 获取可用页签失败，准备重试: {exc}")
                if attempt >= attempts - 1:
                    break
                self._cooperative_sleep(wait_seconds)
        if last_error is not None:
            raise last_error
        raise RuntimeError("浏览器启动后未获取到可用页面")

    @staticmethod
    def _allocate_local_debug_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            return int(sock.getsockname()[1])

    def _terminate_external_browser_process(self, *, graceful_timeout: float = 8.0, force: bool = True) -> None:
        process = getattr(self, "_external_browser_process", None)
        self._external_browser_process = None
        self._external_browser_port = 0
        if process is None:
            return
        try:
            if process.poll() is not None:
                return
        except Exception:
            return
        pgid = None
        if hasattr(os, "getpgid"):
            try:
                pgid = os.getpgid(process.pid)
            except Exception:
                pgid = None
        try:
            if pgid and hasattr(os, "killpg"):
                os.killpg(pgid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=max(1.0, float(graceful_timeout or 0.0)))
            return
        except Exception:
            pass
        if not force:
            return
        try:
            if pgid and hasattr(os, "killpg"):
                os.killpg(pgid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=2)
        except Exception:
            pass

    def _terminate_residual_profile_processes(self, *, graceful_timeout: float = 3.0) -> None:
        """Best-effort cleanup for Chrome children that outlive Playwright/CDP close."""
        profile_path = str(getattr(self, "user_data_dir", "") or "").strip()
        if not profile_path:
            return
        try:
            pids = browser_profile_owner_pids(profile_path)
        except Exception as exc:
            print(f"[{self.name}] 扫描残留浏览器进程失败: {exc}")
            return
        if not pids:
            return
        try:
            print(f"[{self.name}] 检测到关闭后残留浏览器进程，准备兜底回收: pids={pids}")
            terminated = terminate_browser_profile_processes(
                profile_path,
                graceful_timeout=graceful_timeout,
                force=True,
            )
            if terminated:
                print(f"[{self.name}] 已回收关闭后残留浏览器进程")
        except Exception as exc:
            print(f"[{self.name}] 回收关闭后残留浏览器进程失败: {exc}")

    def abort_startup(self) -> None:
        """Best-effort cleanup when a session worker times out during startup."""
        self._stop_requested = True
        self._stop_watcher_event.set()
        self._terminate_external_browser_process(graceful_timeout=1.0, force=True)
        self._terminate_residual_profile_processes(graceful_timeout=1.0)

    def _release_browser_handles(self, *, stop_playwright: bool) -> None:
        try:
            if self.context:
                self._close_all_pages()
        except Exception:
            pass
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        try:
            if self._browser_connection:
                self._browser_connection.close()
        except Exception:
            pass
        self._browser_connection = None
        self._terminate_external_browser_process()
        self._terminate_residual_profile_processes()
        if stop_playwright:
            try:
                if self._playwright:
                    self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        self.context = None
        self.page = None
        self._current_context_headless = False

    def _launch_external_chrome_context(
        self,
        playwright,
        *,
        executable_path: str,
        headless: bool,
        window_size: str,
        browser_label: str,
    ):
        if self._profile_directory_in_use():
            force_reclaim = bool(getattr(self, "force_reclaim_profile_processes_on_start", False))
            if (bool(headless) or force_reclaim) and self._reclaim_orphaned_profile_processes(force=force_reclaim):
                time.sleep(0.5)
        if self._profile_directory_in_use():
            raise RuntimeError(
                f"{self.name} 浏览器资料夹仍被已有 Chrome 进程占用，"
                "请先关闭残留窗口后再启动，避免同一资料夹重复打开导致崩溃"
            )
        port = self._allocate_local_debug_port()
        launch_args = [
            executable_path,
            f"--user-data-dir={self.user_data_dir}",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            f"--window-size={window_size}",
        ]
        if self._automation_feature_enabled("use_automation_control_flag", True):
            launch_args.insert(1, "--disable-blink-features=AutomationControlled")
        locale = self._browser_locale()
        if locale:
            launch_args.append(f"--lang={locale}")
        if headless:
            launch_args.append("--headless=new")
        else:
            launch_args.append("--new-window")

        proxy = self._browser_proxy_settings()
        if proxy and proxy.get("server"):
            launch_args.append(f"--proxy-server={proxy['server']}")

        for item in self._browser_extra_args_list():
            if item not in launch_args:
                launch_args.append(item)
        startup_url = "about:blank"
        if bool(getattr(self, "external_chrome_launch_target_url", False)):
            candidate_url = str(getattr(self, "target_url", "") or "").strip()
            startup_url = candidate_url if _is_safe_external_chrome_target_url(candidate_url) else startup_url
        launch_args.append(startup_url)

        process = subprocess.Popen(
            launch_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._external_browser_process = process
        self._external_browser_port = port

        endpoint = f"http://127.0.0.1:{port}"
        deadline = time.time() + 15.0
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                if process.poll() is not None:
                    raise RuntimeError("Chrome 启动后立即退出")
            except Exception as exc:
                last_error = exc
                break
            browser = None
            try:
                browser = playwright.chromium.connect_over_cdp(endpoint)
                contexts = list(browser.contexts)
                if not contexts:
                    raise RuntimeError("CDP 已连接，但尚未拿到默认浏览器上下文")
                self._browser_connection = browser
                self._browser_app_label = browser_label
                self._current_context_headless = bool(headless)
                return contexts[0]
            except Exception as exc:
                last_error = exc
                try:
                    if browser is not None:
                        browser.close()
                except Exception:
                    pass
                time.sleep(0.25)

        self._terminate_external_browser_process()
        self._browser_connection = None
        if last_error is not None:
            raise last_error
        raise RuntimeError("连接外部 Chrome CDP 超时")

    def start(self) -> "BasePlatform":
        """启动浏览器（复用 doubao.json 的 persistent context 模式）"""
        from patchright.sync_api import sync_playwright

        self._raise_if_stop_requested()

        # 确保用户数据目录存在
        if not os.path.exists(self.user_data_dir):
            os.makedirs(self.user_data_dir)

        pw = sync_playwright().start()
        try:
            # 增强：从指纹配置读取窗口尺寸，确保与 screen 一致
            screen_width, screen_height = self._fingerprint.get_screen_resolution()
            # 窗口高度减去任务栏/菜单栏高度（macOS ~28px, Windows ~40px）
            window_height = screen_height - (28 if sys.platform == "darwin" else 40)

            prefer_headed_runtime = bool(self.always_headed or getattr(self, "prefer_headed_runtime", False))
            self.context = self._launch_browser_context(
                pw,
                headless=(not self.inspect) and (not prefer_headed_runtime),
                no_viewport=(self.inspect or prefer_headed_runtime),
                viewport=None if (self.inspect or prefer_headed_runtime) else {"width": screen_width, "height": window_height},
            )
        except Exception:
            pw.stop()
            raise
        self._playwright = pw

        # 选择一个可用页签；外部 CDP 启动期避免同步关闭页签导致卡住。
        close_startup_extra_pages = not self._external_chrome_light_control_enabled()
        self._ensure_primary_page(
            retries=3,
            wait_seconds=0.25,
            close_extra_pages=close_startup_extra_pages,
        )
        try:
            if not self._current_context_headless:
                try:
                    self.page.bring_to_front()
                except Exception:
                    self._ensure_primary_page(
                        retries=1,
                        wait_seconds=0.2,
                        close_extra_pages=close_startup_extra_pages,
                    )
                    self.page.bring_to_front()
                self._activate_browser_app()
            self._start_stop_watcher()

            # 无头/有头统一应用 stealth，避免调试模式和切换窗口时暴露明显差异。
            if self._automation_feature_enabled("use_automation_stealth_scripts", True):
                self._apply_stealth_to_page()
            self._install_shared_js_helpers()

            # 访问目标页面。部分平台对“about:blank -> 受控跳转”更敏感，
            # 允许外部 Chrome 在启动时直接打开目标页，避免二次导航暴露控制链路。
            should_goto_target = not bool(getattr(self, "skip_runtime_startup_goto", False))
            current_url = ""
            try:
                current_url = str(self.page.url or "")
            except Exception:
                current_url = ""
            if should_goto_target:
                self.page.goto(self.target_url, timeout=30000, wait_until="domcontentloaded")
                self._wait_for_page_stability("浏览器初次启动")
                self._ensure_primary_page(
                    retries=1,
                    wait_seconds=0.2,
                    close_extra_pages=close_startup_extra_pages,
                )
            else:
                if current_url and current_url not in {"about:blank", "chrome://newtab/", "chrome://new-tab-page/"}:
                    self._wait_for_page_stability("浏览器初次启动(保留原始启动页)")
                    self._ensure_primary_page(
                        retries=1,
                        wait_seconds=0.2,
                        close_extra_pages=close_startup_extra_pages,
                    )
                else:
                    self.page.goto(self.target_url, timeout=30000, wait_until="domcontentloaded")
                    self._wait_for_page_stability("浏览器初次启动(回退导航)")
                    self._ensure_primary_page(
                        retries=1,
                        wait_seconds=0.2,
                        close_extra_pages=close_startup_extra_pages,
                    )

            # 增强：预热浏览器存储（模拟老用户）
            if self._automation_feature_enabled("use_automation_storage_warmup", True):
                self._warmup_browser_storage()

            # 移除：不在启动时做行为模拟（此时页面可能还是空白/登录页）
            # 真实用户不会在空白页面上滚动，应该在实际使用时才模拟行为

            print(f"[{self.name}] 浏览器已启动，访问: {self.target_url}")

            # always_headed 平台启动后最小化（有头但不占前台）
            if self.always_headed and not self.inspect:
                self._cooperative_sleep(1)  # 等窗口完全创建
                self._minimize_browser_app()

            return self
        except Exception as exc:
            try:
                self.close()
            except Exception:
                pass
            self._reraise_stop_requested(exc)
            raise

    def start_new_chat(self) -> None:
        """点击新建对话按钮，等待输入框就绪"""
        if not self.new_chat_selector:
            return
        try:
            self._raise_if_stop_requested()
            btn = self.page.locator(self.new_chat_selector).first
            self._wait_for_locator(btn, timeout_ms=5000)
            self._click_locator(btn, timeout_ms=5000)
            self._cooperative_sleep(random.uniform(1.0, 2.0))
            self._wait_for_page_selector(self.input_selector, timeout_ms=10000)
            print(f"[{self.name}] 已开启新对话")
        except Exception as e:
            self._reraise_stop_requested(e)
            print(f"[{self.name}] 开启新对话失败，继续: {e}")

    def _browser_locale(self) -> str:
        return str(getattr(self, "browser_locale", "") or "").strip() or "zh-CN"

    def _browser_accept_language(self) -> str:
        return (
            str(getattr(self, "browser_accept_language", "") or "").strip()
            or "zh-CN,zh;q=0.9,en;q=0.8"
        )

    def _browser_timezone_id(self) -> str:
        configured_value = str(getattr(self, "browser_timezone_id", "") or "").strip()
        if configured_value:
            return configured_value
        configured = os.environ.get("AIBRANDMONITOR_TIMEZONE", "").strip()
        if configured:
            return configured
        tzinfo = getattr(local_now(), "tzinfo", None)
        return str(getattr(tzinfo, "key", "") or "").strip()

    def _read_browser_version_from_executable(self, executable_path: str) -> str:
        path = str(executable_path or "").strip()
        if not path or not os.path.exists(path):
            return ""
        try:
            completed = subprocess.run(
                [path, "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3,
                check=False,
            )
        except Exception:
            return ""
        output = f"{completed.stdout or ''}\n{completed.stderr or ''}".strip()
        match = re.search(r"(\d+\.\d+\.\d+\.\d+)", output)
        return match.group(1) if match else ""

    def _guess_browser_version(self, *, headless: bool) -> str:
        if self._browser_version_hint:
            return self._browser_version_hint
        del headless
        candidates = [self._resolve_system_chrome_executable()]
        for candidate in candidates:
            version = self._read_browser_version_from_executable(candidate)
            if version:
                self._browser_version_hint = version
                return version
        # 未探测到正式版 Chrome 时，保留一个稳定回退版本字符串。
        self._browser_version_hint = "147.0.0.0"
        return self._browser_version_hint

    def _get_ua(self, *, headless: bool) -> str:
        """获取稳定的 User-Agent（从指纹配置读取，长期一致）"""
        configured = str(getattr(self, "browser_user_agent", "") or "").strip()
        if configured:
            return configured
        if not self._automation_feature_enabled("use_automation_user_agent", True):
            return ""

        # 使用稳定的指纹配置
        return self._fingerprint.get_user_agent()

    def _external_chrome_light_control_enabled(self) -> bool:
        return bool(getattr(self, "use_external_chrome_cdp", False)) and bool(
            getattr(self, "external_chrome_light_control", True)
        )

    def _automation_feature_enabled(self, attr_name: str, default: bool = True) -> bool:
        enabled = bool(getattr(self, attr_name, default))
        if not enabled:
            return False
        if not self._external_chrome_light_control_enabled():
            return True
        if attr_name in {
            "use_automation_user_agent",
            "use_automation_extra_headers",
            "use_automation_ignore_default_args",
            "use_automation_stealth_scripts",
            "use_automation_storage_warmup",
        }:
            return False
        return enabled

    def _should_simulate_human_behavior(self) -> bool:
        # 外部正式版 Chrome + CDP 时优先减少人为伪装痕迹。
        return not self._external_chrome_light_control_enabled()

    def _browser_proxy_settings(self) -> dict | None:
        server = str(getattr(self, "browser_proxy_server", "") or "").strip()
        if not server:
            return None
        proxy = {"server": server}
        username = str(getattr(self, "browser_proxy_username", "") or "").strip()
        password = str(getattr(self, "browser_proxy_password", "") or "").strip()
        if username:
            proxy["username"] = username
        if password:
            proxy["password"] = password
        return proxy

    def _browser_extra_args_list(self) -> list[str]:
        raw = str(getattr(self, "browser_extra_args", "") or "").strip()
        if not raw:
            return []
        try:
            parts = [part for part in shlex.split(raw) if part]
        except Exception:
            parts = [part.strip() for part in raw.splitlines() if part.strip()]
        return [part for part in parts if _is_safe_browser_extra_arg(part)]

    def _runtime_int_setting(
        self,
        attr_name: str,
        default: int,
        *,
        minimum: int = 0,
        maximum: int | None = None,
    ) -> int:
        raw_value = getattr(self, attr_name, default)
        try:
            value = int(float(raw_value))
        except Exception:
            value = int(default)
        if maximum is not None:
            value = min(value, int(maximum))
        return max(int(minimum), value)

    def _runtime_float_setting(
        self,
        attr_name: str,
        default: float,
        *,
        minimum: float = 0.0,
        maximum: float | None = None,
    ) -> float:
        raw_value = getattr(self, attr_name, default)
        try:
            value = float(raw_value)
        except Exception:
            value = float(default)
        if maximum is not None:
            value = min(value, float(maximum))
        return max(float(minimum), value)

    def _page_stabilize_wait_range_seconds(self) -> tuple[float, float]:
        minimum_ms = self._runtime_int_setting(
            "page_stabilize_wait_min_ms",
            1200,
            minimum=0,
            maximum=30000,
        )
        maximum_ms = self._runtime_int_setting(
            "page_stabilize_wait_max_ms",
            2600,
            minimum=minimum_ms,
            maximum=60000,
        )
        if maximum_ms < minimum_ms:
            maximum_ms = minimum_ms
        return minimum_ms / 1000.0, maximum_ms / 1000.0

    def _wait_for_page_stability(self, reason: str = "") -> None:
        self._raise_if_stop_requested()
        if not getattr(self, "page", None):
            return
        for state, timeout_ms in (
            ("domcontentloaded", 1200),
            ("load", 2000),
            ("networkidle", 2200),
        ):
            try:
                self.page.wait_for_load_state(state, timeout=timeout_ms)
            except Exception:
                pass
        min_seconds, max_seconds = self._page_stabilize_wait_range_seconds()
        wait_seconds = min_seconds if max_seconds <= min_seconds else random.uniform(min_seconds, max_seconds)
        if wait_seconds > 0:
            self._cooperative_sleep(wait_seconds)
        if reason:
            print(f"[{self.name}] 页面稳定等待完成: {reason} ({wait_seconds:.2f}s)")

    def _failure_backoff_seconds(self, attempt: int) -> float:
        base_seconds = self._runtime_float_setting(
            "failure_backoff_base_seconds",
            3.0,
            minimum=0.0,
            maximum=120.0,
        )
        max_seconds = self._runtime_float_setting(
            "failure_backoff_max_seconds",
            20.0,
            minimum=base_seconds,
            maximum=300.0,
        )
        if base_seconds <= 0:
            return 0.0
        exponent = max(0, int(attempt) - 1)
        candidate = base_seconds * (1.75 ** exponent)
        jitter = random.uniform(0.2, min(1.8, max_seconds))
        return min(max_seconds, candidate + jitter)

    def _apply_retry_backoff(self, attempt: int, max_retries: int, reason: str = "") -> None:
        if attempt >= max_retries:
            return
        wait_seconds = self._failure_backoff_seconds(attempt)
        if wait_seconds <= 0:
            return
        reason_text = f"，原因: {reason}" if reason else ""
        print(
            f"[{self.name}] 本轮失败后退避 {wait_seconds:.2f}s，"
            f"准备进入下一次重试 ({attempt}/{max_retries}){reason_text}"
        )
        self._cooperative_sleep(wait_seconds)

    def _apply_stealth_to_page(self) -> None:
        """
        极简反检测：只修复 Patchright 未覆盖的关键点

        策略：信任 Patchright 的 CDP 层反检测，只补充最少的 JS 注入
        原则：能不改就不改，避免过度伪装被检测为机器人
        """
        if not getattr(self, "page", None):
            return
        try:
            # 1. 修复 navigator.webdriver（Patchright 已处理，但双保险）
            self.page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => false,
                    configurable: true
                });
            """)

            # 2. 修复 window.chrome.runtime（自动化浏览器缺失此对象）
            self.page.add_init_script("""
                if (!window.chrome) {
                    window.chrome = { runtime: {} };
                } else if (!window.chrome.runtime) {
                    window.chrome.runtime = {};
                }
            """)

            # 3. WebRTC IP 泄露防护（拦截构造函数，强制 relay 模式）
            self.page.add_init_script("""
                // 保留 RTCPeerConnection 对象，但在构造时强制 relay 模式
                if (window.RTCPeerConnection) {
                    const OriginalRTCPeerConnection = window.RTCPeerConnection;
                    window.RTCPeerConnection = function(config) {
                        // 强制修改配置：只使用 relay 模式（隐藏真实 IP）
                        const modifiedConfig = {
                            ...(config || {}),
                            iceTransportPolicy: 'relay',  // 强制使用 TURN 服务器
                            iceServers: []  // 清空 ICE 服务器列表（防止 STUN 泄露）
                        };
                        return new OriginalRTCPeerConnection(modifiedConfig);
                    };
                    window.RTCPeerConnection.prototype = OriginalRTCPeerConnection.prototype;
                }
            """)

            # 4. 删除 Selenium/WebDriver 特征变量
            self.page.add_init_script("""
                delete window.__webdriver_script_fn;
                delete window.__driver_evaluate;
                delete window.__webdriver_evaluate;
                delete window.__selenium_evaluate;
                delete window.__fxdriver_evaluate;
                delete window.__driver_unwrapped;
                delete window.__webdriver_unwrapped;
                delete window.__selenium_unwrapped;
                delete window.__fxdriver_unwrapped;
            """)

            # 5. 修复 window.outerWidth/outerHeight（Headless 模式下为 0）
            self.page.add_init_script("""
                if (window.outerWidth === 0 || window.outerHeight === 0) {
                    Object.defineProperty(window, 'outerWidth', {
                        get: () => window.innerWidth
                    });
                    Object.defineProperty(window, 'outerHeight', {
                        get: () => window.innerHeight + 85
                    });
                }
            """)

            # 注意：以下内容已移除，信任浏览器真实值
            # - hardwareConcurrency/deviceMemory（Patchright 已通过启动参数设置）
            # - languages（浏览器会根据 --lang 参数自动设置）
            # - screen 分辨率（浏览器会根据 --window-size 自动设置）
            # - maxTouchPoints（桌面浏览器默认为 0）
            # - platform（浏览器会根据 User-Agent 自动设置）
            # - Canvas/Audio 噪声（会被检测为机器人特征，完全移除）
            # - permissions.query 覆盖（真实浏览器行为更安全）
            # - mediaDevices 拦截（返回真实设备列表更自然）

        except Exception as e:
            print(f"[{self.name}] 应用页面级反检测脚本失败: {e}")

    def _install_shared_js_helpers(self) -> None:
        """注入 page.evaluate 可复用的轻量 JS 辅助函数。"""
        if not getattr(self, "page", None):
            return
        script = r"""
            (() => {
                if (window.__aiMonitorHelpers__ && window.__aiMonitorHelpers__.__version === 1) {
                    return;
                }

                const normalize = (value) => String(value || '')
                    .replace(/\u00a0/g, ' ')
                    .replace(/\s+/g, ' ')
                    .trim();

                const trim = (value) => String(value || '').trim();

                const normalizeMultiline = (value) => String(value || '')
                    .replace(/\u00a0/g, ' ')
                    .replace(/[ \t]+/g, ' ')
                    .replace(/\n{3,}/g, '\n\n')
                    .trim();

                const textOf = (el) => normalizeMultiline(
                    el ? (el.innerText || el.textContent || el.value || '') : ''
                );

                const isVisible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (!style || style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) {
                        return false;
                    }
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                };

                const closestText = (el) => {
                    let current = el;
                    while (current && current !== document.body) {
                        const text = textOf(current);
                        if (text) return text;
                        current = current.parentElement;
                    }
                    return '';
                };

                const helpers = Object.freeze({
                    __version: 1,
                    trim,
                    normalize,
                    normalizeMultiline,
                    textOf,
                    isVisible,
                    closestText,
                });

                Object.defineProperty(window, '__aiMonitorHelpers__', {
                    value: helpers,
                    configurable: true,
                    enumerable: false,
                    writable: false,
                });
            })();
        """
        try:
            self.page.add_init_script(script)
        except Exception as e:
            self._reraise_stop_requested(e)
            print(f"[{self.name}] 注册共享 JS helper 失败: {e}")
        try:
            self.page.evaluate(script)
        except Exception as e:
            self._reraise_stop_requested(e)
            print(f"[{self.name}] 注入共享 JS helper 失败: {e}")

    def _simulate_human_behavior(self) -> None:
        """
        模拟真人行为：随机鼠标移动、滚动、停顿、点击空白区域
        增强：行为模式随机化，步骤随机跳过，避免固定套路
        """
        if not getattr(self, "page", None):
            return
        try:
            # 增强：随机选择行为模式（模拟不同用户习惯）
            behavior_pattern = random.choice([
                "quick_scan",      # 快速浏览（30%）
                "careful_read",    # 仔细阅读（40%）
                "direct_action",   # 直接操作（20%）
                "idle_browse",     # 闲逛浏览（10%）
            ])

            if behavior_pattern == "direct_action":
                # 直接操作：最小化行为，快速进入任务
                initial_pause = random.uniform(0.3, 0.8)
                self._cooperative_sleep(initial_pause)
                # 只做少量鼠标移动
                for _ in range(random.randint(1, 2)):
                    x = random.randint(200, 600)
                    y = random.randint(200, 500)
                    self.page.mouse.move(x, y)
                    self._cooperative_sleep(random.uniform(0.05, 0.15))
                return

            # 其他模式：正常行为模拟
            # 增强：随机初始停顿（模拟真人打开页面后的观察时间）
            if behavior_pattern == "careful_read":
                initial_pause = random.uniform(1.5, 3.0)  # 仔细阅读：停顿更久
            elif behavior_pattern == "idle_browse":
                initial_pause = random.uniform(2.0, 4.0)  # 闲逛：停顿最久
            else:
                initial_pause = random.uniform(0.5, 1.5)  # 快速浏览：停顿较短

            self._cooperative_sleep(initial_pause)

            # 1. 随机鼠标移动（模拟真人浏览页面，使用贝塞尔曲线轨迹）
            # 增强：30% 概率跳过鼠标移动（有些用户不移动鼠标）
            if random.random() > 0.3:
                mouse_moves = random.randint(2, 5) if behavior_pattern == "quick_scan" else random.randint(4, 7)
                for _ in range(mouse_moves):
                    start_x = random.randint(100, 400)
                    start_y = random.randint(100, 400)
                    end_x = random.randint(400, 1000)
                    end_y = random.randint(200, 700)

                    # 模拟贝塞尔曲线移动（分段移动）
                    steps = random.randint(8, 15)
                    for i in range(steps):
                        t = i / steps
                        # 添加随机抖动，模拟真人手抖
                        jitter_x = random.randint(-5, 5)
                        jitter_y = random.randint(-5, 5)
                        x = int(start_x + (end_x - start_x) * t + jitter_x)
                        y = int(start_y + (end_y - start_y) * t + jitter_y)
                        self.page.mouse.move(x, y)
                        # 增强：随机化每步延迟（避免机器人般的精确时间）
                        step_delay = random.uniform(0.008, 0.035)
                        self._cooperative_sleep(step_delay)

                    # 随机停顿（模拟真人思考）- 时间随机化
                    pause_duration = random.uniform(0.15, 0.6)
                    self._cooperative_sleep(pause_duration)

            # 2. 随机滚动（模拟真人查看页面内容，分段滚动）
            # 增强：20% 概率跳过滚动（有些用户不滚动）
            if random.random() > 0.2:
                # 增强：根据行为模式调整滚动距离
                if behavior_pattern == "careful_read":
                    total_scroll = random.randint(300, 1000)  # 仔细阅读：滚动更多
                    scroll_steps = random.randint(4, 8)
                elif behavior_pattern == "idle_browse":
                    total_scroll = random.randint(400, 1200)  # 闲逛：滚动最多
                    scroll_steps = random.randint(5, 10)
                else:
                    total_scroll = random.randint(150, 600)  # 快速浏览：滚动较少
                    scroll_steps = random.randint(2, 5)

                for _ in range(scroll_steps):
                    scroll_distance = total_scroll // scroll_steps + random.randint(-50, 50)
                    self.page.evaluate("(distance) => window.scrollBy(0, distance)", scroll_distance)
                    # 增强：滚动后随机停顿（模拟阅读）
                    read_pause = random.uniform(0.18, 0.45) if behavior_pattern != "quick_scan" else random.uniform(0.1, 0.25)
                    self._cooperative_sleep(read_pause)

                # 3. 随机停顿（模拟真人阅读）- 更长的随机时间
                if behavior_pattern == "careful_read":
                    reading_pause = random.uniform(1.0, 2.5)
                elif behavior_pattern == "idle_browse":
                    reading_pause = random.uniform(1.5, 3.0)
                else:
                    reading_pause = random.uniform(0.3, 1.0)
                self._cooperative_sleep(reading_pause)

                # 4. 再滚回顶部（分段滚动）- 增强：不是每次都滚回顶部
                if random.random() < 0.7:  # 70% 概率滚回顶部
                    for _ in range(scroll_steps):
                        scroll_back = -(total_scroll // scroll_steps + random.randint(-50, 50))
                        self.page.evaluate("(distance) => window.scrollBy(0, distance)", scroll_back)
                        # 增强：滚动速度随机化
                        scroll_pause = random.uniform(0.12, 0.35)
                        self._cooperative_sleep(scroll_pause)

            # 5. 随机点击空白区域（模拟真人误触）- 降低概率
            if random.random() < 0.2:  # 20% 概率（从 30% 降低）
                blank_x = random.randint(50, 300)
                blank_y = random.randint(50, 300)
                try:
                    self.page.mouse.click(blank_x, blank_y)
                    click_pause = random.uniform(0.08, 0.22)
                    self._cooperative_sleep(click_pause)
                except Exception:
                    pass

            # 6. 随机键盘事件（模拟真人按键，如 Tab、Shift）- 降低概率
            if random.random() < 0.15:  # 15% 概率（从 20% 降低）
                random_key = random.choice(["Tab", "Shift", "Control"])
                try:
                    self.page.keyboard.press(random_key)
                    key_pause = random.uniform(0.04, 0.18)
                    self._cooperative_sleep(key_pause)
                except Exception:
                    pass

            # 7. 随机窗口焦点切换（模拟真人切换标签页）- 降低概率
            if random.random() < 0.1:  # 10% 概率（从 15% 降低）
                try:
                    self.page.evaluate("window.blur()")
                    blur_pause = random.uniform(0.25, 0.7)
                    self._cooperative_sleep(blur_pause)
                    self.page.evaluate("window.focus()")
                except Exception:
                    pass

            # 增强：最后随机停顿（模拟真人完成浏览后的停留）
            # 增强：50% 概率跳过最后停顿（有些用户直接操作）
            if random.random() > 0.5:
                final_pause = random.uniform(0.3, 0.9)
                self._cooperative_sleep(final_pause)

        except Exception as e:
            print(f"[{self.name}] 模拟真人行为失败: {e}")

    def _warmup_browser_storage(self) -> None:
        """
        预热浏览器存储（Cookie/LocalStorage/SessionStorage）
        使用稳定的指纹配置，模拟老用户，长期一致
        """
        if not getattr(self, "page", None):
            return
        try:
            # 获取稳定的用户偏好
            preferences = self._fingerprint.get_user_preferences()

            # 增强：增加访问次数（模拟真实用户行为）
            session_count = self._fingerprint.increment_session_count()

            # 增强：获取上次访问时间（如果是第一次，则为空）
            last_visit = preferences.get("lastVisit", 0)
            if last_visit == 0:
                # 第一次访问，设置为"几天前"
                import time
                last_visit = int(time.time() * 1000) - random.randint(86400000 * 3, 86400000 * 30)  # 3-30天前

            # 1. 添加稳定的 LocalStorage 数据（模拟用户偏好）
            self.page.evaluate(
                """
                (payload) => {
                    try {
                        // 使用稳定的用户偏好设置
                        localStorage.setItem('theme', payload.theme);
                        localStorage.setItem('language', payload.language);
                        localStorage.setItem('visited', String(payload.visited));
                        localStorage.setItem('lastVisit', String(payload.lastVisit));  // 使用真实的上次访问时间
                        localStorage.setItem('sessionCount', String(payload.sessionCount));  // 使用递增的访问次数

                        // 模拟一些应用数据（用户ID基于指纹保持稳定）
                        const stableUserId = 'user_' + String(payload.userSeed || '').substring(0, 16);
                        const randomData = {
                            userId: stableUserId,  // 稳定的用户ID
                            preferences: {
                                notifications: payload.theme === 'dark',  // 基于主题偏好
                                autoplay: payload.theme === 'light'  // 基于主题偏好
                            },
                            timestamp: payload.lastVisit  // 使用真实的上次访问时间
                        };
                        localStorage.setItem('appData', JSON.stringify(randomData));
                    } catch (e) {
                        // 忽略错误
                    }
                }
                """,
                {
                    "theme": str(preferences.get("theme", "light")),
                    "language": str(preferences.get("language", "zh-CN")),
                    "visited": bool(preferences.get("visited")),
                    "lastVisit": int(last_visit),
                    "sessionCount": int(session_count),
                    "userSeed": f"{preferences.get('theme', 'light')}_{session_count % 1000}",
                },
            )

            # 2. 添加一些 SessionStorage 数据
            self.page.evaluate("""
                () => {
                    try {
                        sessionStorage.setItem('sessionId', 'sess_' + Math.random().toString(36).substr(2, 16));
                        sessionStorage.setItem('sessionStart', Date.now());
                        sessionStorage.setItem('pageViews', '1');
                    } catch (e) {
                        // 忽略错误
                    }
                }
            """)

            # 3. 模拟一些浏览历史（通过 pushState）
            if random.random() < 0.3:  # 30% 概率
                try:
                    self.page.evaluate("""
                        () => {
                            try {
                                // 模拟浏览历史
                                history.pushState({}, '', window.location.href);
                            } catch (e) {
                                // 忽略错误
                            }
                        }
                    """)
                except Exception:
                    pass

            # 增强：更新最后访问时间（为下次访问做准备）
            self._fingerprint.update_last_visit()

        except Exception as e:
            print(f"[{self.name}] 预热浏览器存储失败: {e}")

    @staticmethod
    def _browser_app_name() -> str:
        return "Google Chrome"

    @staticmethod
    def _resolve_system_chrome_executable() -> str:
        return resolve_system_browser_executable()

    def _browser_launch_args(self, *, window_size: str) -> list[str]:
        """
        浏览器启动参数 - 针对中国AI平台的平衡策略

        原则：
        1. Patchright 已在 CDP 层做了大量反检测,只保留最核心参数
        2. 不禁用平台可能依赖的功能（WebGL），但必须禁用 WebRTC（IP 泄露）
        3. 让浏览器尽可能接近真实用户的默认配置
        """
        args = [
            f"--window-size={window_size}",
            # 基础 UI 配置（真实用户常见）
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-infobars",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",

            # 性能优化（低内存环境常见）
            "--disable-dev-shm-usage",
            "--disable-background-timer-throttling",

            # 减少干扰（真实用户也会设置）
            "--disable-save-password-bubble",
            "--disable-prompt-on-repost",

            # 日志静默
            "--disable-logging",
            "--log-level=3",

            # 注意：移除 --disable-webrtc 等参数，改用 JS 层拦截
            # 完全禁用会被检测为机器人特征，改为保留 API 但隐藏 IP
        ]

        if self._automation_feature_enabled("use_automation_control_flag", True):
            args.insert(1, "--disable-blink-features=AutomationControlled")

        for item in self._browser_extra_args_list():
            if item not in args:
                args.append(item)
        return args

    def _launch_browser_context(self, playwright, *, headless: bool, no_viewport: bool, viewport) -> object:
        # 增强：从指纹配置读取窗口尺寸，确保与 screen 一致
        screen_width, screen_height = self._fingerprint.get_screen_resolution()
        window_height = screen_height - (28 if sys.platform == "darwin" else 40)
        window_size = f"{screen_width},{window_height}"

        launch_kwargs = {
            "user_data_dir": self.user_data_dir,
            "headless": bool(headless),
            "no_viewport": bool(no_viewport),
            "viewport": viewport,
            "locale": self._browser_locale(),
            "args": self._browser_launch_args(window_size=window_size),
            # 增强：添加真实浏览器权限配置
            "permissions": ["notifications"],
        }

        user_agent = self._get_ua(headless=headless)
        if user_agent:
            launch_kwargs["user_agent"] = user_agent

        if self._automation_feature_enabled("use_automation_extra_headers", True):
            accept_header = self._fingerprint.get_accept_format()
            accept_language = self._fingerprint.get_accept_language()
            dnt = self._fingerprint.get_dnt()
            client_hints = self._fingerprint.get_client_hints_headers()
            launch_kwargs["extra_http_headers"] = {
                "Accept-Language": accept_language,
                "Accept": accept_header,
                "Accept-Encoding": "gzip, deflate, br",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
                "DNT": dnt,
                **client_hints,
            }
            launch_kwargs["extra_http_headers"] = {
                k: v for k, v in launch_kwargs["extra_http_headers"].items() if v is not None
            }

        if self._automation_feature_enabled("use_automation_ignore_default_args", True):
            launch_kwargs["ignore_default_args"] = ["--enable-automation"]

        # 时区配置（从指纹配置获取，确保稳定）
        timezone_id = self._fingerprint.get_timezone_id()
        launch_kwargs["timezone_id"] = timezone_id

        proxy = self._browser_proxy_settings()
        if proxy:
            launch_kwargs["proxy"] = proxy

        def _launch_with_retry(*, executable_path=None, browser_label: str = ""):
            retry_cleanup_attempted = False
            while True:
                try:
                    if bool(getattr(self, "use_external_chrome_cdp", False)):
                        if not executable_path:
                            raise RuntimeError("外部 Chrome CDP 模式需要可执行文件路径")
                        return self._launch_external_chrome_context(
                            playwright,
                            executable_path=executable_path,
                            headless=bool(headless),
                            window_size=window_size,
                            browser_label=browser_label,
                        )
                    kwargs = dict(launch_kwargs)
                    if executable_path:
                        kwargs["executable_path"] = executable_path
                    context = playwright.chromium.launch_persistent_context(**kwargs)
                    if browser_label:
                        self._browser_app_label = browser_label
                    self._current_context_headless = bool(headless)
                    return context
                except Exception as exc:
                    if retry_cleanup_attempted or not self._is_process_singleton_error(exc):
                        raise
                    cleaned = self._cleanup_stale_profile_singletons()
                    retry_cleanup_attempted = True
                    if not cleaned:
                        raise

        chrome_executable = self._resolve_system_chrome_executable()
        if not chrome_executable:
            raise RuntimeError(
                "未找到系统正式版 Chrome。请安装 Google Chrome，或设置环境变量 "
                "CHROME_EXECUTABLE_PATH 指向正式版 Chrome 可执行文件。"
            )
        profile_launch_lock = _browser_profile_launch_lock(self.user_data_dir)
        with profile_launch_lock:
            force_reclaim = bool(getattr(self, "force_reclaim_profile_processes_on_start", False))
            if bool(headless) or force_reclaim:
                self._reclaim_orphaned_profile_processes(force=force_reclaim)
            self._cleanup_stale_profile_singletons()
            context = _launch_with_retry(
                executable_path=chrome_executable,
                browser_label="Google Chrome",
            )
        mode = "无头" if headless else "有头"
        print(f"[{self.name}] 已使用系统正式版 Chrome 启动{mode}浏览器")
        return context

    def _singleton_artifact_paths(self) -> list[Path]:
        base_path = Path(self.user_data_dir)
        names = ["SingletonLock", "SingletonCookie", "SingletonSocket"]
        paths = [base_path / name for name in names]
        paths.extend((base_path / "Default" / name) for name in ["LOCK"])
        return paths

    @staticmethod
    def _is_process_singleton_error(exc: Exception) -> bool:
        text = str(exc or "")
        return "ProcessSingleton" in text or "SingletonLock" in text

    def _profile_directory_in_use(self) -> bool:
        return is_browser_profile_in_use(self.user_data_dir)

    def _reclaim_orphaned_profile_processes(self, *, force: bool = False) -> bool:
        if self.inspect and not force:
            return False
        try:
            if not force and not browser_profile_process_tree_is_orphaned(self.user_data_dir):
                return False
            pids = browser_profile_owner_pids(self.user_data_dir)
            if not pids:
                return False
            reason = "已有进程" if force else "残留无头进程"
            print(f"[{self.name}] 检测到浏览器资料夹被{reason}占用，准备回收: pids={pids}")
            terminated = terminate_browser_profile_processes(
                self.user_data_dir,
                graceful_timeout=5.0,
                force=True,
            )
            if terminated:
                print(f"[{self.name}] 已回收残留浏览器进程")
            return bool(terminated)
        except Exception as exc:
            print(f"[{self.name}] 回收残留浏览器进程失败: {exc}")
            return False

    def _cleanup_stale_profile_singletons(self) -> bool:
        artifact_paths = [path for path in self._singleton_artifact_paths() if path.exists()]
        if not artifact_paths:
            return False
        if self._profile_directory_in_use():
            print(f"[{self.name}] 检测到资料夹仍被浏览器占用，跳过 Singleton 清理")
            return False
        cleaned = False
        for path in artifact_paths:
            try:
                path.unlink(missing_ok=True)
                cleaned = True
            except Exception:
                pass
        if cleaned:
            print(f"[{self.name}] 已清理陈旧的浏览器 Singleton 锁文件，准备重试启动")
        return cleaned

    def _run_macos_browser_script(self, script: str, *, wait: bool = True) -> None:
        if sys.platform != "darwin":
            return
        if not self._browser_app_label or "Chrome" not in self._browser_app_label:
            return
        try:
            if wait:
                subprocess.run(
                    ["osascript", "-e", script],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                subprocess.Popen(
                    ["osascript", "-e", script],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass

    def _activate_windows_browser_app(self) -> None:
        if sys.platform != "win32":
            return
        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "", "chrome"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
            )
        except Exception:
            pass

    def _activate_browser_app(self) -> None:
        app_name = self._browser_app_label or self._browser_app_name()
        if sys.platform == "win32":
            self._activate_windows_browser_app()
            return
        self._run_macos_browser_script(f'tell application "{app_name}" to activate')

    def _minimize_browser_app(self) -> None:
        app_name = self._browser_app_label or self._browser_app_name()
        self._run_macos_browser_script(
            f'tell application "{app_name}" to set miniaturized of every window to true',
            wait=False,
        )

    def _close_all_pages(self) -> None:
        context = getattr(self, "context", None)
        if context is None:
            return
        try:
            pages = list(context.pages)
        except Exception:
            pages = []
        for current_page in pages:
            try:
                current_page.close()
            except Exception:
                pass

    def _relaunch_context(self, headless: bool = False) -> None:
        """关闭当前 context 并重新启动（用于浏览器异常关闭后的恢复），始终有头模式。"""
        self._raise_if_stop_requested()
        current_url = self.page.url
        self._release_browser_handles(stop_playwright=False)

        self.context = self._launch_browser_context(
            self._playwright,
            headless=False,
            no_viewport=True,
            viewport=None,
        )
        self._ensure_primary_page(retries=2, wait_seconds=0.2)
        if self._automation_feature_enabled("use_automation_stealth_scripts", True):
            self._apply_stealth_to_page()
        self._install_shared_js_helpers()
        self.page.goto(current_url, wait_until="domcontentloaded")
        self._wait_for_page_stability("浏览器重启恢复")
        # 重启后立即最小化
        if not self.inspect:
            self._hide_browser()

    def _show_browser(self):
        """把浏览器显示到前台。always_headed 平台直接激活窗口；其他平台重启为有头模式。"""
        if self.always_headed or self.inspect or getattr(self, "prefer_headed_runtime", False):
            # 已经是有头模式，直接激活窗口到前台
            try:
                self.page.bring_to_front()
            except Exception:
                pass
            self._activate_browser_app()
            print(f"[{self.name}] 浏览器已显示到前台")
            return
        # 无头模式：关闭后重启为有头
        current_url = self.page.url
        self._release_browser_handles(stop_playwright=False)
        self.context = self._launch_browser_context(
            self._playwright,
            headless=False,
            no_viewport=True,
            viewport=None,
        )
        self._ensure_primary_page(retries=2, wait_seconds=0.2)
        if self._automation_feature_enabled("use_automation_stealth_scripts", True):
            self._apply_stealth_to_page()
        self._install_shared_js_helpers()
        self.page.goto(current_url, wait_until="domcontentloaded")
        self._wait_for_page_stability("切换到有头模式")
        self.page.bring_to_front()
        self._activate_browser_app()
        print(f"[{self.name}] 已切换为有头模式，浏览器已显示")

    def _hide_browser(self):
        """人工干预完成后，重启回无头模式继续运行（cookie 已保存在 user_data_dir）。"""
        if self.inspect or self.always_headed or getattr(self, "prefer_headed_runtime", False):
            # inspect / always_headed / 偏好有头的平台：不切回无头。
            if not self._current_context_headless:
                if self.always_headed and not self.inspect:
                    self._minimize_browser_app()
                    print(f"[{self.name}] 浏览器已最小化，继续后台运行")
                else:
                    print(f"[{self.name}] 保持有头浏览器运行，继续后续流程")
            return
        current_url = self.page.url
        # 等待浏览器把 cookie/session 数据 flush 到磁盘再关闭
        print(f"[{self.name}] 等待 cookie 写盘...")
        self._cooperative_sleep(3)
        if not self._current_context_headless:
            self._minimize_browser_app()
        self._release_browser_handles(stop_playwright=False)
        # 等 Chromium 进程完全退出，确保 user_data_dir 解锁
        self._cooperative_sleep(2)
        self.context = self._launch_browser_context(
            self._playwright,
            headless=True,
            no_viewport=False,
            viewport={"width": 1280, "height": 1600},
        )
        self._ensure_primary_page(retries=2, wait_seconds=0.2)
        if self._automation_feature_enabled("use_automation_stealth_scripts", True):
            self._apply_stealth_to_page()
        self._install_shared_js_helpers()
        self.page.goto(current_url, wait_until="domcontentloaded")
        self._wait_for_page_stability("切换回无头模式")
        # 确保输入框可见后再返回，避免后续 ensure_logged_in 超时
        try:
            self._wait_for_page_selector(self.input_selector, timeout_ms=5000)
        except Exception as e:
            print(f"[{self.name}] 切换回无头后输入框仍不可见: {e}")
        print(f"[{self.name}] 已切换回无头模式，继续后台运行")

    def _prompt_manual_action(self, message: str):
        """弹出提示窗口，用户点击「帮忙处理」后返回，程序进入自动轮询等待"""
        import sys
        import subprocess
        script = (
            "import tkinter as tk\n"
            "from tkinter import messagebox\n"
            "root = tk.Tk()\n"
            "root.withdraw()\n"
            "root.attributes('-topmost', True)\n"
            f"messagebox.showinfo('需要人工干预', {repr(message + chr(10) + chr(10) + '点击「确定」后程序将自动等待页面恢复，无需再次操作。')})\n"
            "root.destroy()\n"
        )
        subprocess.run(
            [sys.executable, "-c", script],
            stdin=subprocess.DEVNULL,
            close_fds=(sys.platform != "win32"),
        )

    # 人机识别关键词（只匹配明确的验证场景，避免误判正常回答内容）
    _captcha_keywords = ["captcha", "robot", "人机验证", "安全验证", "滑动验证", "点击验证", "请完成验证", "verify you are human"]
    _verification_text_fragments = [
        "captcha",
        "verifyyouarehuman",
        "humanverification",
        "securitycheck",
        "safetycheck",
        "notarobot",
        "iamnotarobot",
        "人机验证",
        "安全验证",
        "请完成验证",
        "完成验证后继续",
        "滑动验证",
        "点击验证",
        "拖动滑块",
        "验证后继续",
    ]

    # 子类可覆盖：豁免不应触发弹窗检测的 CSS 选择器列表
    _overlay_whitelist_selectors: list = []
    # 子类可覆盖：补充平台特有的拦截层/弹窗选择器
    _overlay_extra_selectors: list = []
    # 子类可关闭：某些平台存在常驻高 z-index 容器，通用遮罩扫描容易误判
    _overlay_detection_enabled: bool = True

    def _get_overlay_candidate_selectors(self) -> list[str]:
        selectors = [
            '[class*="captcha"],[class*="Captcha"]',
            '[class*="verify"],[class*="Verify"]',
            '[id*="captcha"],[id*="Captcha"]',
            '#captcha_container',
            '[role="alertdialog"]',
            '[role="dialog"]',
            '[aria-modal="true"]',
            '[class*="modal"],[class*="Modal"]',
            '[class*="dialog"],[class*="Dialog"]',
            '[class*="overlay"],[class*="Overlay"]',
            '[class*="mask"],[class*="Mask"]',
            '[class*="backdrop"],[class*="Backdrop"]',
        ]
        for selector in self._overlay_extra_selectors:
            text = str(selector or "").strip()
            if text and text not in selectors:
                selectors.append(text)
        return selectors

    def _scan_blocking_overlay(self) -> dict:
        try:
            return self.page.evaluate("""({selectors, whitelist, verificationFragments}) => {
                const seen = new Set();

                function norm(value) {
                    return String(value || '').replace(/\\s+/g, '').trim().toLowerCase();
                }

                function styleNumber(value) {
                    const num = Number.parseFloat(String(value || '').trim());
                    return Number.isFinite(num) ? num : 0;
                }

                function isWhitelisted(el) {
                    for (const sel of whitelist || []) {
                        try {
                            if (sel && (el.matches(sel) || el.closest(sel))) return true;
                        } catch (_) {}
                    }
                    return false;
                }

                function isReallyVisible(el) {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                    if (style.pointerEvents === 'none') return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width >= 100 && rect.height >= 50;
                }

                function hasVisibleContent(node) {
                    if (!node) return false;
                    if (node.nodeType === 3) return Boolean(String(node.textContent || '').trim());
                    if (node.nodeType !== 1) return false;
                    const tag = String(node.tagName || '').toUpperCase();
                    if (['IMG', 'CANVAS', 'SVG', 'VIDEO', 'IFRAME', 'BUTTON', 'INPUT', 'TEXTAREA'].includes(tag)) {
                        return true;
                    }
                    for (const child of node.childNodes || []) {
                        if (child.nodeType === 1) {
                            const cs = window.getComputedStyle(child);
                            if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') continue;
                        }
                        if (hasVisibleContent(child)) return true;
                    }
                    return false;
                }

                function looksLikeBlockingOverlay(el) {
                    if (!isReallyVisible(el) || isWhitelisted(el)) return false;

                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    const viewportW = Math.max(window.innerWidth || 0, document.documentElement.clientWidth || 0, 1);
                    const viewportH = Math.max(window.innerHeight || 0, document.documentElement.clientHeight || 0, 1);
                    const text = String(el.innerText || el.textContent || '').trim();
                    const normalizedText = norm(text);
                    const className = String(el.className || '');
                    const id = String(el.id || '');
                    const role = String(el.getAttribute('role') || '').toLowerCase();
                    const ariaModal = String(el.getAttribute('aria-modal') || '').toLowerCase();
                    const descriptor = `${className} ${id} ${role}`.toLowerCase();
                    const verificationHit = (verificationFragments || []).some((fragment) => {
                        const token = norm(fragment);
                        return Boolean(token) && normalizedText.includes(token);
                    });
                    const looksCaptcha = descriptor.includes('captcha') || descriptor.includes('verify');
                    const largeEnough = rect.width >= Math.min(320, viewportW * 0.28) || rect.height >= Math.min(180, viewportH * 0.22);
                    const coversViewport = rect.width >= viewportW * 0.55 || rect.height >= viewportH * 0.35;
                    const centered = Math.abs((rect.left + rect.width / 2) - viewportW / 2) <= viewportW * 0.28;
                    const fixedLike = ['fixed', 'sticky'].includes(style.position) || styleNumber(style.zIndex) >= 1000;

                    if (el.id === 'captcha_container' && rect.width > 800 && rect.height > 400) {
                        return hasVisibleContent(el);
                    }
                    if (!hasVisibleContent(el)) return false;
                    if (role === 'menu' || role === 'listbox' || role === 'tooltip') return false;
                    if (el.closest('[role="menu"], [role="listbox"], [role="tooltip"]')) return false;

                    if (verificationHit || looksCaptcha) return true;
                    if (ariaModal === 'true' || role === 'dialog' || role === 'alertdialog') {
                        return largeEnough && (fixedLike || centered || coversViewport);
                    }
                    if (
                        descriptor.includes('modal') ||
                        descriptor.includes('dialog') ||
                        descriptor.includes('overlay') ||
                        descriptor.includes('mask') ||
                        descriptor.includes('backdrop')
                    ) {
                        return largeEnough && (fixedLike || centered || coversViewport);
                    }
                    return false;
                }

                for (const selector of selectors || []) {
                    let elements = [];
                    try {
                        elements = Array.from(document.querySelectorAll(selector));
                    } catch (_) {
                        continue;
                    }
                    for (const el of elements) {
                        if (!el || seen.has(el)) continue;
                        seen.add(el);
                        if (!looksLikeBlockingOverlay(el)) continue;
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        const text = String(el.innerText || el.textContent || '').trim();
                        const normalizedText = norm(text);
                        return {
                            matched: true,
                            selector,
                            text,
                            verificationHit: (verificationFragments || []).some((fragment) => {
                                const token = norm(fragment);
                                return Boolean(token) && normalizedText.includes(token);
                            }),
                            role: String(el.getAttribute('role') || ''),
                            ariaModal: String(el.getAttribute('aria-modal') || ''),
                            className: String(el.className || ''),
                            id: String(el.id || ''),
                            position: String(style.position || ''),
                            zIndex: String(style.zIndex || ''),
                            rect: {
                                width: Math.round(rect.width),
                                height: Math.round(rect.height),
                                left: Math.round(rect.left),
                                top: Math.round(rect.top),
                            },
                        };
                    }
                }

                const captchaDomains = ['recaptcha', 'hcaptcha', 'geetest', 'tcaptcha', 'captcha.bytedance', 'verify.bytedance'];
                for (const frame of Array.from(document.querySelectorAll('iframe'))) {
                    const src = String(frame.src || '').toLowerCase();
                    if (captchaDomains.some((domain) => src.includes(domain))) {
                        return {
                            matched: true,
                            selector: 'iframe captcha domain',
                            text: src.slice(0, 200),
                            verificationHit: true,
                            role: '',
                            ariaModal: '',
                            className: '',
                            id: '',
                            position: '',
                            zIndex: '',
                            rect: null,
                        };
                    }
                }
                return { matched: false };
            }""", {
                "selectors": self._get_overlay_candidate_selectors(),
                "whitelist": list(self._overlay_whitelist_selectors or []),
                "verificationFragments": list(self._verification_text_fragments or []),
            }) or {"matched": False}
        except Exception:
            return {"matched": False}

    def _probe_input_visibility(self, attempts: int = 2, timeout_ms: int = 1200) -> bool | None:
        selector = str(self.input_selector or "").strip()
        if not selector:
            return None
        last_error = None
        for index in range(max(1, attempts)):
            self._raise_if_stop_requested()
            try:
                visible = self.page.locator(selector).first.is_visible(timeout=timeout_ms)
                if visible:
                    return True
            except Exception as exc:
                last_error = exc
                self._reraise_stop_requested(exc)
            if index < max(1, attempts) - 1:
                self._cooperative_sleep(0.25)
        if last_error:
            return False
        return False

    def _collect_interruption_state(self, *, check_input_visible: bool) -> dict:
        try:
            current_url = self.page.url
        except Exception:
            current_url = ""

        overlay_state = {"matched": False}
        if self._overlay_detection_enabled:
            overlay_state = self._scan_blocking_overlay()
        overlay_text = str(overlay_state.get("text") or "")
        matched_keywords = [
            kw for kw in self._captcha_keywords
            if kw.lower() in overlay_text.lower()
        ]
        verification_detected = bool(matched_keywords or overlay_state.get("verificationHit"))

        url_ok = True
        if self.target_url:
            allowed_prefixes = [self.target_url.rstrip("/")] + [
                alias.rstrip("/") for alias in (self.target_url_aliases or [])
            ]
            url_ok = any(current_url.startswith(prefix) for prefix in allowed_prefixes)

        input_visible = None
        if check_input_visible:
            input_visible = self._probe_input_visibility()

        state = {
            "current_url": current_url,
            "url_ok": url_ok,
            "overlay_detected": bool(overlay_state.get("matched")),
            "overlay_state": overlay_state,
            "overlay_text": overlay_text,
            "matched_keywords": matched_keywords,
            "verification_detected": verification_detected,
            "input_visible": input_visible,
            "check_input_visible": bool(check_input_visible),
        }
        self.last_interruption_state = dict(state)
        return state

    def _is_interruption_cleared(self, state: dict | None) -> bool:
        payload = state if isinstance(state, dict) else {}
        if payload.get("verification_detected"):
            return False
        if payload.get("overlay_detected"):
            return False
        if not payload.get("url_ok", True):
            return False
        if payload.get("check_input_visible") and payload.get("input_visible") is False:
            return False
        return True

    def _format_interruption_state(self, state: dict | None) -> str:
        payload = state if isinstance(state, dict) else {}
        reasons: list[str] = []
        if payload.get("verification_detected"):
            matched = list(payload.get("matched_keywords") or [])
            if matched:
                reasons.append(f"验证关键词命中: {', '.join(matched)}")
            else:
                reasons.append("检测到验证态遮罩")
        elif payload.get("overlay_detected"):
            overlay = payload.get("overlay_state") or {}
            selector = str(overlay.get("selector") or "").strip()
            text = str(overlay.get("text") or "").strip()
            desc = "检测到遮罩/弹层"
            if selector:
                desc += f" ({selector})"
            if text:
                desc += f" 文案预览: {text[:40]}"
            reasons.append(desc)
        if not payload.get("url_ok", True):
            current_url = str(payload.get("current_url") or "").strip()
            reasons.append(f"页面跳转: {current_url or '未知地址'}")
        if payload.get("check_input_visible") and payload.get("input_visible") is False:
            reasons.append("输入框不可见")
        if not reasons:
            reasons.append("页面状态未恢复")
        return "；".join(reasons)

    def _wait_for_interruption_clearance(
        self,
        *,
        timeout_seconds: float = 12.0,
        check_input_visible: bool = True,
    ) -> dict:
        deadline = time.time() + max(1.0, float(timeout_seconds or 0.0))
        last_state = self._collect_interruption_state(check_input_visible=check_input_visible)
        while time.time() < deadline:
            self._raise_if_stop_requested()
            last_state = self._collect_interruption_state(check_input_visible=check_input_visible)
            if self._is_interruption_cleared(last_state):
                return last_state
            self._cooperative_sleep(0.5)
        return last_state

    def _wait_for_human_resolution(self, msg: str, *, trigger_state: dict | None = None) -> None:
        """
        弹出 GUI 提示窗，用户点击「确定」后把浏览器带到前台（不刷新页面），
        自动轮询等待用户处理完成（输入框可用且无遮罩），完成后最小化继续。
        """
        self._raise_if_stop_requested()
        print(f"\n[{self.name}] ⚠️  需要人工干预：{msg}")
        self.last_run_recovered_manually = True
        if trigger_state:
            print(f"[{self.name}] 人工干预触发原因: {self._format_interruption_state(trigger_state)}")
        # 先把浏览器带到前台，再弹提示
        self._show_browser()
        prompt_message = str(msg or "").strip()
        if trigger_state:
            prompt_message = (
                f"{prompt_message}\n\n"
                f"当前检测原因：{self._format_interruption_state(trigger_state)}"
            )
        prompt_attempt = 0
        while True:
            prompt_attempt += 1
            # GUI 弹窗（阻塞当前线程；用户点击后进入自动复查）
            self._prompt_manual_action(prompt_message)
            state = self._wait_for_interruption_clearance(
                timeout_seconds=12.0,
                check_input_visible=True,
            )
            if self._is_interruption_cleared(state):
                print(f"[{self.name}] 用户已确认处理完毕，页面复查通过，继续运行...")
                break
            summary = self._format_interruption_state(state)
            print(f"[{self.name}] 用户确认后页面仍未恢复: {summary}")
            prompt_message = (
                f"{msg}\n\n"
                f"程序复查后仍未恢复：{summary}\n"
                "请确认页面已经回到可输入状态后，再点击「确定」。"
            )
            if prompt_attempt >= 3:
                print(f"[{self.name}] 连续 {prompt_attempt} 次人工确认后仍未恢复，继续等待进一步处理")
        if not self.inspect:
            self._cooperative_sleep(1)  # 给浏览器时间写入 cookie
            self._hide_browser()
        raise InterruptionDetected("人工干预完成，重新开始本次尝试")

    def _detect_overlay(self) -> bool:
        """只检测明确的验证/人机识别弹窗，避免误判正常 UI 元素。"""
        result = self._scan_blocking_overlay()
        if result.get("matched"):
            print(
                f"[{self.name}] _detect_overlay 命中: "
                f"{result.get('selector')} | role={result.get('role') or '-'} | "
                f"class={result.get('className') or result.get('id') or '-'} | "
                f"text={(result.get('text') or '')[:80]!r}"
            )
            return True
        return False

    def check_for_interruption(self, check_input_visible: bool = False) -> None:
        """
        检测页面是否被人机识别/弹窗打断。
        - check_input_visible=False（默认）：只检测 DOM遮罩 + URL跳离，用于轮询过程中
        - check_input_visible=True：额外检测输入框是否可见，用于重试前
        关键词检测只扫描弹窗/对话框区域，避免 AI 回答内容误触发。
        """
        self._raise_if_stop_requested()
        state = self._collect_interruption_state(check_input_visible=check_input_visible)
        current_url = str(state.get("current_url") or "")
        overlay_state = state.get("overlay_state") or {}
        overlay_text = str(state.get("overlay_text") or "")
        matched = list(state.get("matched_keywords") or [])
        if state.get("verification_detected"):
            print(
                f"[{self.name}] check_for_interruption 弹窗关键词命中: "
                f"{matched or ['verification-fragment']}"
            )
            self._wait_for_human_resolution(
                f"平台 {self.name} 检测到人机识别/安全验证，请手动完成后程序将自动恢复。",
                trigger_state=state,
            )
            return

        if state.get("overlay_detected"):
            print(
                f"[{self.name}] check_for_interruption DOM overlay 命中，触发人工干预: "
                f"{overlay_state.get('selector')} | text={(overlay_text or '')[:80]!r}"
            )
            self._wait_for_human_resolution(
                f"平台 {self.name} 检测到弹窗或遮罩层，请手动处理后程序将自动恢复。",
                trigger_state=state,
            )
            return

        # URL跳离检测
        if not state.get("url_ok", True):
            print(f"[{self.name}] check_for_interruption URL跳离: {current_url}")
            self._wait_for_human_resolution(
                f"平台 {self.name} 页面跳转至 {current_url}，请手动处理后点击「确定」继续。",
                trigger_state=state,
            )
            return

        # 重试前额外检测输入框可见性（多试几次，避免页面渲染中误判）
        if check_input_visible:
            try:
                visible = self._probe_input_visibility(attempts=3, timeout_ms=2000)
                if not visible:
                    state = self._collect_interruption_state(check_input_visible=True)
                    print(f"[{self.name}] check_for_interruption 输入框不可见，触发人工干预")
                    self._wait_for_human_resolution(
                        f"平台 {self.name} 输入框不可见，可能出现弹窗或验证，请手动处理后程序将自动恢复。",
                        trigger_state=state,
                    )
            except Exception:
                pass

    def ensure_logged_in(self, timeout: int = 15) -> bool:
        """
        检查是否已登录（通过检测输入框是否存在）
        如果遇到人机识别或未登录，显示浏览器并等待用户手动处理，完成后隐藏
        """
        try:
            self._wait_for_page_selector(self.input_selector, timeout_ms=timeout * 1000)
            print(f"[{self.name}] 已登录，输入框可用")
            return True
        except Exception as e:
            self._reraise_stop_requested(e)
            print(f"[{self.name}] 等待输入框超时: {e}")
            try:
                page_text = self.page.evaluate("() => document.body.innerText") or ""
            except Exception:
                page_text = ""
            captcha_keywords = ["验证", "captcha", "robot", "人机", "verify", "challenge", "滑动", "点击"]
            if any(kw in page_text.lower() for kw in captcha_keywords):
                msg = f"平台 {self.name} 检测到人机识别，请手动完成验证后点击「确定」继续。"
            else:
                msg = f"平台 {self.name} 未检测到输入框，请手动登录后点击「确定」继续。"
            self._wait_for_human_resolution(msg)
            return True

    def type_like_human(self, text: str) -> None:
        """
        模拟人类输入
        - 点击前随机停顿 + 鼠标移动
        - 清空现有内容
        - 逐字符输入，带随机延迟 + 偶尔的"思考停顿"
        - 按Enter前随机停顿
        """
        import sys
        select_all = "Meta+A" if sys.platform == "darwin" else "Control+A"
        self._raise_if_stop_requested()

        # 点击前随机停顿，模拟人类移动鼠标（增强：更长的随机延迟）
        self._cooperative_sleep(random.uniform(0.8, 2.0))

        # 随机鼠标移动，模拟人类自然行为（增强：多次移动轨迹）
        try:
            # 先移动到随机位置
            self.page.mouse.move(
                random.randint(100, 400),
                random.randint(100, 400)
            )
            self._cooperative_sleep(random.uniform(0.1, 0.3))
            # 再移动到目标区域附近
            self.page.mouse.move(
                random.randint(400, 900),
                random.randint(300, 700)
            )
            self._cooperative_sleep(random.uniform(0.2, 0.5))
        except Exception:
            pass

        chat_input = self.page.locator(self.input_selector).first
        # 点击前检测验证码/弹窗，避免 click 超时
        self.check_for_interruption(check_input_visible=False)
        try:
            self._click_locator(chat_input, timeout_ms=10000)
        except Exception as ce:
            self._reraise_stop_requested(ce)
            ce_str = str(ce)
            if 'captcha_container' in ce_str and 'intercepts pointer' in ce_str:
                print(f"[{self.name}] type_like_human click 异常触发人工干预: {ce}")
                self._wait_for_human_resolution(
                    f"平台 {self.name} 输入框被遮挡（可能是验证码），请手动处理后程序将自动恢复。"
                )
                self._click_locator(chat_input, timeout_ms=10000)
            else:
                raise
        self._cooperative_sleep(random.uniform(0.4, 1.0))
        try:
            chat_input.focus(timeout=3000)
        except Exception as e:
            self._reraise_stop_requested(e)
            pass

        # 优先直接清空当前输入框，避免把快捷键发到错误焦点对象。
        cleared = False
        try:
            tag_name = (chat_input.evaluate("(el) => el.tagName") or "").lower()
        except Exception:
            tag_name = ""
        try:
            if tag_name in {"textarea", "input"}:
                chat_input.fill("")
                cleared = True
        except Exception:
            cleared = False
        if not cleared:
            self.page.keyboard.press(select_all)
            self._cooperative_sleep(random.uniform(0.1, 0.3))
            self.page.keyboard.press("Backspace")
            self._cooperative_sleep(random.uniform(0.3, 0.6))

        # 增强：更真实的输入延迟，模拟人类打字节奏
        try:
            # 使用更大的随机延迟范围（100-250ms，更接近真人打字速度）
            chat_input.press_sequentially(text, delay=random.randint(100, 250))
        except Exception:
            for i, char in enumerate(text):
                self._raise_if_stop_requested()
                # 随机延迟，偶尔有较长停顿
                base_delay = random.randint(100, 250)
                chat_input.type(char, delay=base_delay)
                # 增强：更频繁的思考停顿，模拟人类打字习惯
                if random.random() < 0.15:  # 15% 概率停顿
                    self._cooperative_sleep(random.uniform(0.4, 1.2))
                # 在标点符号后偶尔停顿
                elif char in '，。、；：' and random.random() < 0.3:
                    self._cooperative_sleep(random.uniform(0.2, 0.6))

        # 按Enter前随机停顿，模拟人类确认后提交（增强：更长延迟）
        self._cooperative_sleep(random.uniform(0.8, 2.0))
        self._last_prompt_text = text
        if not self._wait_for_input_value(text):
            raise RuntimeError(f"{self.name} 输入框未确认写入关键词，已取消本次提交")

    def _read_input_value(self) -> str:
        try:
            self._raise_if_stop_requested()
            return self.page.evaluate(
                """(selector) => {
                    const input = document.querySelector(selector);
                    if (!input) return '';
                    return String(input.value || input.innerText || input.textContent || '').trim();
                }""",
                self.input_selector,
            ) or ""
        except Exception as e:
            self._reraise_stop_requested(e)
            return ""

    def _wait_for_input_value(self, expected_text: str, timeout: float = 4.0) -> bool:
        expected = self._normalize_compact_text(expected_text)
        if not expected:
            return True
        deadline = time.time() + max(1.0, float(timeout or 0.0))
        while time.time() < deadline:
            self._raise_if_stop_requested()
            current = self._normalize_compact_text(self._read_input_value())
            if current == expected or expected in current:
                return True
            self._cooperative_sleep(0.2)
        return False

    def _wait_for_submit_started(self, before_input: str, timeout: float = 8.0) -> bool:
        before_compact = self._normalize_compact_text(before_input)
        deadline = time.time() + max(1.0, float(timeout or 0.0))
        while time.time() < deadline:
            self._raise_if_stop_requested()
            signal = self._has_submit_started_signal()
            if signal:
                return True
            current_compact = self._normalize_compact_text(self._read_input_value())
            if before_compact and current_compact != before_compact and not current_compact:
                return True
            self._cooperative_sleep(0.2)
        return False

    def submit_prompt(self) -> None:
        """优先对当前输入框提交，避免把 Enter 发到错误焦点。"""
        self._raise_if_stop_requested()
        keyword = str(getattr(self, "_last_prompt_text", "") or "")
        before_input = self._read_input_value()
        before_compact = self._normalize_compact_text(before_input)
        keyword_compact = self._normalize_compact_text(keyword)
        if keyword_compact and before_compact != keyword_compact and keyword_compact not in before_compact:
            raise RuntimeError(f"{self.name} 输入框内容与本轮关键词不一致，已取消提交")
        chat_input = self.page.locator(self.input_selector).first
        try:
            chat_input.focus(timeout=3000)
        except Exception as e:
            self._reraise_stop_requested(e)
            pass
        try:
            chat_input.press("Enter", timeout=3000)
            if self._wait_for_submit_started(before_input, timeout=8.0):
                print(f"[{self.name}] 已确认问题已发送（策略: input_enter）")
                return
            if self._wait_for_submit_start_signal(timeout=4.0):
                print(f"[{self.name}] 已确认问题已发送（策略: input_enter; submit-signal）")
                return
        except Exception as e:
            self._reraise_stop_requested(e)
            pass
        self.page.keyboard.press("Enter")
        if self._wait_for_submit_started(before_input, timeout=8.0):
            print(f"[{self.name}] 已确认问题已发送（策略: keyboard_enter）")
            return
        if self._wait_for_submit_start_signal(timeout=4.0):
            print(f"[{self.name}] 已确认问题已发送（策略: keyboard_enter; submit-signal）")
            return
        raise RuntimeError(f"{self.name} 未确认问题已发送")

    def _has_submit_started_signal(self) -> bool | None:
        """返回当前平台是否已经出现“开始生成/提交成功”的信号。"""
        return None

    def _wait_for_submit_start_signal(self, timeout: float = 4.0) -> bool:
        deadline = time.time() + max(0.5, float(timeout or 0.0))
        supported = False
        while time.time() < deadline:
            self._raise_if_stop_requested()
            try:
                signal = self._has_submit_started_signal()
            except Exception as exc:
                self._reraise_stop_requested(exc)
                signal = None
            if signal is None:
                return False
            supported = True
            if signal:
                return True
            self._cooperative_sleep(0.2)
        return False if supported else False

    def _get_generation_debug_state(self) -> dict:
        """返回平台特定的生成态调试信息，用于排查“长时间未结束”问题。"""
        return {}

    def _allow_text_stable_completion(self, debug_state: dict | None = None) -> bool:
        """
        文本稳定兜底前的最后一道平台校验。

        默认允许；子类可基于自身“停止/发送/语音”按钮状态进一步约束，
        避免明明仍处于生成态却被文本稳定误判为完成。
        """
        return True

    # 各平台深度思考按钮的文本关键词（子类可覆盖）
    deep_think_selector = "button:has-text('深度思考'), button:has-text('深思'), button:has-text('DeepThink'), button:has-text('R1')"

    def enable_deep_think(self) -> bool:
        """点击深度思考按钮（如果未激活）。返回是否成功。"""
        if not self.deep_think:
            return False
        try:
            btn = self.page.locator(self.deep_think_selector).first
            self._wait_for_locator(btn, timeout_ms=3000)
            # 检查是否已激活（aria-pressed 或 class 含 active/selected）
            pressed = btn.get_attribute("aria-pressed") or ""
            cls = btn.get_attribute("class") or ""
            if pressed == "true" or "active" in cls or "selected" in cls or "on" in cls:
                print(f"[{self.name}] 深度思考已激活")
                return True
            self._click_locator(btn, timeout_ms=3000)
            self._cooperative_sleep(0.5)
            print(f"[{self.name}] 已开启深度思考")
            return True
        except Exception as e:
            self._reraise_stop_requested(e)
            print(f"[{self.name}] 深度思考按钮未找到: {e}")
            return False

    @staticmethod
    def _normalize_brand_match_text(value: str) -> str:
        text = unicodedata.normalize("NFKC", str(value or ""))
        text = "".join(char for char in text if unicodedata.category(char) != "Cf")
        return text.lower()

    @staticmethod
    def contains_brand_mention(text_fragment: str, brand: str) -> bool:
        """精确匹配品牌词，允许常见空白/标点分隔，避免 ASCII 单词子串误匹配。"""
        if not text_fragment or not brand:
            return False

        text = BasePlatform._normalize_brand_match_text(text_fragment)
        brand_text = BasePlatform._normalize_brand_match_text(brand).strip()
        if not brand_text:
            return False

        separator_chars_pattern = (
            r"[\s\u00a0\u3000"
            r"\-_/|｜"
            r"()（）\[\]【】"
            r",，:：;；.。·"
            r"'\"“”‘’`"
            r"&＋+"
            r"!！?？~～"
            r"—–]+"
        )
        brand_tokens = [
            token for token in re.split(separator_chars_pattern, brand_text)
            if token
        ]
        if not brand_tokens:
            return False

        separator_pattern = separator_chars_pattern[:-1] + "*"
        pattern = separator_pattern.join(re.escape(token) for token in brand_tokens)

        def is_ascii_alnum(c):
            return c.isascii() and c.isalnum()

        first_ascii = next((char for char in brand_text if char.isascii() and char.isalnum()), "")
        last_ascii = next((char for char in reversed(brand_text) if char.isascii() and char.isalnum()), "")

        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            before = text[match.start() - 1] if match.start() > 0 else ''
            after = text[match.end()] if match.end() < len(text) else ''

            if first_ascii and is_ascii_alnum(before):
                continue
            if last_ascii and is_ascii_alnum(after):
                continue
            return True

        compact_text = re.sub(separator_chars_pattern, "", text)
        compact_brand = re.sub(separator_chars_pattern, "", brand_text)
        if not compact_text or not compact_brand:
            return False

        idx = compact_text.find(compact_brand)
        while idx != -1:
            before = compact_text[idx - 1] if idx > 0 else ''
            after = compact_text[idx + len(compact_brand)] if idx + len(compact_brand) < len(compact_text) else ''
            if (not first_ascii or not is_ascii_alnum(before)) and (not last_ascii or not is_ascii_alnum(after)):
                return True
            idx = compact_text.find(compact_brand, idx + 1)
        return False

    @staticmethod
    def explain_brand_mention(text_fragment: str, brand: str, *, context_chars: int = 60) -> dict:
        """返回品牌命中的调试信息，便于定位误命中来源。"""
        text = str(text_fragment or "")
        brand_text = str(brand or "")
        if not text or not brand_text:
            return {"matched": False, "reason": "empty"}

        normalized_text = BasePlatform._normalize_brand_match_text(text)
        normalized_brand = BasePlatform._normalize_brand_match_text(brand_text).strip()
        if not normalized_brand:
            return {"matched": False, "reason": "empty_brand"}

        separator_chars_pattern = (
            r"[\s\u00a0\u3000"
            r"\-_/|｜"
            r"()（）\[\]【】"
            r",，:：;；.。·"
            r"'\"“”‘’`"
            r"&＋+"
            r"!！?？~～"
            r"—–]+"
        )
        brand_tokens = [
            token for token in re.split(separator_chars_pattern, normalized_brand)
            if token
        ]
        if not brand_tokens:
            return {"matched": False, "reason": "empty_tokens"}

        separator_pattern = separator_chars_pattern[:-1] + "*"
        pattern = separator_pattern.join(re.escape(token) for token in brand_tokens)

        def is_ascii_alnum(c):
            return c.isascii() and c.isalnum()

        first_ascii = next((char for char in normalized_brand if char.isascii() and char.isalnum()), "")
        last_ascii = next((char for char in reversed(normalized_brand) if char.isascii() and char.isalnum()), "")

        for match in re.finditer(pattern, normalized_text, flags=re.IGNORECASE):
            before = normalized_text[match.start() - 1] if match.start() > 0 else ""
            after = normalized_text[match.end()] if match.end() < len(normalized_text) else ""
            if first_ascii and is_ascii_alnum(before):
                continue
            if last_ascii and is_ascii_alnum(after):
                continue
            start = max(0, match.start() - context_chars)
            end = min(len(text), match.end() + context_chars)
            return {
                "matched": True,
                "mode": "regex",
                "match_start": match.start(),
                "match_end": match.end(),
                "excerpt": text[start:end],
                "normalized_excerpt": normalized_text[max(0, match.start() - context_chars):min(len(normalized_text), match.end() + context_chars)],
            }

        compact_text = re.sub(separator_chars_pattern, "", normalized_text)
        compact_brand = re.sub(separator_chars_pattern, "", normalized_brand)
        if not compact_text or not compact_brand:
            return {"matched": False, "reason": "empty_compact"}

        idx = compact_text.find(compact_brand)
        while idx != -1:
            before = compact_text[idx - 1] if idx > 0 else ""
            after = compact_text[idx + len(compact_brand)] if idx + len(compact_brand) < len(compact_text) else ""
            if (not first_ascii or not is_ascii_alnum(before)) and (not last_ascii or not is_ascii_alnum(after)):
                return {
                    "matched": True,
                    "mode": "compact",
                    "match_start": idx,
                    "match_end": idx + len(compact_brand),
                    "excerpt": text[: min(len(text), context_chars * 4)],
                    "normalized_excerpt": compact_text[max(0, idx - context_chars):min(len(compact_text), idx + len(compact_brand) + context_chars)],
                }
            idx = compact_text.find(compact_brand, idx + 1)

        return {"matched": False, "reason": "not_found"}

    @staticmethod
    def detect_brand_mention(
        text_fragment: str,
        brand: str,
        *,
        keyword: str = "",
        context_chars: int = 60,
    ) -> tuple[bool, str, dict]:
        """
        判断正文里是否明确命中品牌名，并返回短证据。
        供抓取模式优先走本地规则，降低 AI 二次判断漏判概率。
        """
        match_info = BasePlatform.explain_brand_mention(
            text_fragment,
            brand,
            context_chars=context_chars,
        )
        if not match_info.get("matched"):
            return False, "", match_info
        if BasePlatform._is_negated_brand_echo(
            text_fragment,
            brand,
            keyword=keyword,
            match_info=match_info,
        ):
            return False, "", {
                **match_info,
                "matched": False,
                "reason": "negated-echo",
            }
        evidence = str(match_info.get("excerpt") or brand or "").strip()
        return True, evidence, match_info

    @staticmethod
    def _is_negated_brand_echo(
        text_fragment: str,
        brand: str,
        *,
        keyword: str = "",
        match_info: dict | None = None,
    ) -> bool:
        """识别“否定型提问回显”，避免把“没有所谓某网站/品牌”误当成命中。"""
        text = str(text_fragment or "")
        brand_text = str(brand or "").strip()
        keyword_text = str(keyword or "").strip()
        if not text or not brand_text or not keyword_text:
            return False

        normalized_brand = BasePlatform._normalize_brand_match_text(brand_text).strip()
        normalized_keyword = BasePlatform._normalize_brand_match_text(keyword_text).strip()
        if not normalized_brand or not normalized_keyword:
            return False

        compact_brand = re.sub(r"[\W_]+", "", normalized_brand, flags=re.UNICODE)
        compact_keyword = re.sub(r"[\W_]+", "", normalized_keyword, flags=re.UNICODE)
        if not compact_brand or compact_brand != compact_keyword:
            return False

        normalized_text = BasePlatform._normalize_brand_match_text(text)
        if match_info and match_info.get("matched") and match_info.get("mode") == "regex":
            start = max(0, int(match_info.get("match_start", 0)) - 24)
            end = min(len(normalized_text), int(match_info.get("match_end", 0)) + 40)
            window = normalized_text[start:end]
        else:
            window = normalized_text[:160]

        quote_brand = r"[\"“”'‘’「」『』]?" + re.escape(normalized_brand) + r"[\"“”'‘’「」『』]?"
        negation_patterns = [
            rf"(没有|并无|并没有|不存在|并不存在|不是|并非|无所谓的|没有所谓的|没有一个单一的|没有单一的).{{0,12}}{quote_brand}",
            rf"{quote_brand}.{{0,12}}(并不存在|不存在|不是|并非|并不是)",
        ]
        return any(re.search(pattern, window) for pattern in negation_patterns)

    def parse_ranking(self, text: str, brand: str) -> int:
        """
        解析品牌在搜索结果中的排名（复用 doubao.json 的核心逻辑）

        支持的序号格式：
        - 1. / 2. / 3.
        - 1、 / 2、 / 3、
        - (1) / (2) / (3)
        - ① / ② / ③
        - 一、 / 二、 / 三、

        返回: 排名数字（1-based），99表示未找到
        """
        if not text or not brand:
            return 99

        # 分割关键词后的内容（通常推荐在关键词之后）
        # 但为了通用性，直接分析全文
        clean_text = re.sub(r'[*_#`\-\[\]]', '', text).replace(" ", "")
        lines = re.split(r'\n+', clean_text)

        chinese_num = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
                       '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
        circle_base = ord('①') - 1  # ① = 1

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # 尝试提取行首序号数字
            rank_num = None

            m = re.match(r'^(\d+)[\.、\s]', line)
            if m:
                rank_num = int(m.group(1))
            else:
                m = re.match(r'^\((\d+)\)', line)
                if m:
                    rank_num = int(m.group(1))
                else:
                    m = re.match(r'^([①-⑩])', line)
                    if m:
                        rank_num = ord(m.group(1)) - circle_base
                    else:
                        m = re.match(r'^([一二三四五六七八九十])[、\s]', line)
                        if m:
                            rank_num = chinese_num.get(m.group(1))

            if rank_num is not None and self.contains_brand_mention(line, brand):
                return rank_num

        # 表格格式解析：品牌在表格数据行中的位置
        raw_lines = re.split(r'\n+', text)
        data_row_num = 0
        for raw_line in raw_lines:
            if not raw_line.strip().startswith('|'):
                continue
            # 跳过分隔行 |---|
            if re.match(r'^\s*\|[\s\-|]+\|\s*$', raw_line):
                continue
            # 跳过表头（不含品牌的第一行）
            cells = [c.strip() for c in raw_line.strip().strip('|').split('|')]
            if any(self.contains_brand_mention(c, brand) for c in cells):
                return max(1, data_row_num)
            data_row_num += 1

        # 全文包含品牌但无明确序号，返回99让调用方重试
        if self.contains_brand_mention(clean_text, brand):
            return 99

        return 99

    @staticmethod
    def _normalize_compact_text(value: str) -> str:
        return re.sub(r"\s+", "", str(value or "").strip().lower())

    def _looks_like_verification_text(self, text: str) -> bool:
        compact = self._normalize_compact_text(text)
        if not compact:
            return False
        return any(fragment in compact for fragment in self._verification_text_fragments)

    def explain_unusable_answer_text(self, answer_text: str, *, keyword: str = "", brand: str = "") -> str:
        compact = self._normalize_compact_text(answer_text)
        if not compact:
            return "empty-text"
        if self._looks_like_verification_text(answer_text):
            return "matched-verification-text"

        remainder = compact
        for token in {self._normalize_compact_text(keyword), self._normalize_compact_text(brand)}:
            if token:
                remainder = remainder.replace(token, "")

        remainder = re.sub(r"[\W_]+", "", remainder, flags=re.UNICODE)
        minimum_length = max(12, min(40, len(self._normalize_compact_text(brand or keyword)) + 8))
        if len(remainder) < minimum_length:
            return f"content-too-short:{len(remainder)}<{minimum_length}"
        return ""

    def _has_new_answer_content(
        self,
        answer_text: str,
        *,
        baseline_text: str = "",
        keyword: str = "",
        brand: str = "",
    ) -> bool:
        compact = self._normalize_compact_text(answer_text)
        baseline_compact = self._normalize_compact_text(baseline_text)
        if not compact:
            return False
        if baseline_compact and compact == baseline_compact:
            return False

        if baseline_compact and compact.startswith(baseline_compact):
            appended = compact[len(baseline_compact):]
            for token in {self._normalize_compact_text(keyword), self._normalize_compact_text(brand)}:
                if token:
                    appended = appended.replace(token, "")
            appended = re.sub(r"[\W_]+", "", appended, flags=re.UNICODE)
            if len(appended) < max(8, min(24, len(self._normalize_compact_text(brand or keyword)) + 4)):
                return False

        return True

    def has_usable_answer_text(self, answer_text: str, *, keyword: str = "", brand: str = "") -> bool:
        return not self.explain_unusable_answer_text(answer_text, keyword=keyword, brand=brand)

    def _choose_better_answer_text(
        self,
        current_text: str,
        candidate_text: str,
        *,
        baseline_text: str = "",
        keyword: str = "",
        brand: str = "",
    ) -> str:
        current = str(current_text or "")
        candidate = str(candidate_text or "")
        if not candidate:
            return current
        if not current:
            return candidate

        current_usable = self.has_usable_answer_text(current, keyword=keyword, brand=brand)
        candidate_usable = self.has_usable_answer_text(candidate, keyword=keyword, brand=brand)
        if candidate_usable != current_usable:
            return candidate if candidate_usable else current

        current_has_new = self._has_new_answer_content(
            current,
            baseline_text=baseline_text,
            keyword=keyword,
            brand=brand,
        )
        candidate_has_new = self._has_new_answer_content(
            candidate,
            baseline_text=baseline_text,
            keyword=keyword,
            brand=brand,
        )
        if candidate_has_new != current_has_new:
            return candidate if candidate_has_new else current

        current_len = len(self._normalize_compact_text(current))
        candidate_len = len(self._normalize_compact_text(candidate))
        if candidate_len > current_len:
            return candidate
        return current

    def _begin_answer_capture(self, *, keyword: str = "", brand: str = "") -> None:
        self._answer_capture_session = AnswerCaptureSession(started_at=time.time())

    def _capture_answer_snapshot(self) -> dict:
        text = str(self._get_answer_text() or "").strip()
        html_body = str(self._get_answer_html() or "").strip()
        blocks = []
        if text or html_body:
            blocks.append({
                "key": "answer:0",
                "order": 0,
                "text": text,
                "html": html_body,
            })
        return {
            "root_key": "",
            "blocks": blocks,
            "raw_text": text,
            "raw_html": html_body,
            "captured_at": time.time(),
        }

    def _choose_better_answer_html(
        self,
        current_html: str,
        candidate_html: str,
        *,
        baseline_text: str = "",
        keyword: str = "",
        brand: str = "",
    ) -> str:
        current = str(current_html or "").strip()
        candidate = str(candidate_html or "").strip()
        if not candidate:
            return current
        if not current:
            return candidate

        current_text = self._extract_text_from_html_fragment(current)
        candidate_text = self._extract_text_from_html_fragment(candidate)
        better_text = self._choose_better_answer_text(
            current_text,
            candidate_text,
            baseline_text=baseline_text,
            keyword=keyword,
            brand=brand,
        )
        if better_text == candidate_text and better_text != current_text:
            return candidate
        if better_text == current_text and better_text != candidate_text:
            return current

        if len(candidate) > len(current):
            return candidate
        return current

    def _merge_answer_snapshot(
        self,
        snapshot: dict | None,
        *,
        keyword: str = "",
        brand: str = "",
    ) -> str:
        payload = snapshot if isinstance(snapshot, dict) else {}
        session = self._answer_capture_session
        if session is None:
            self._begin_answer_capture(keyword=keyword, brand=brand)
            session = self._answer_capture_session
        if session is None:
            return ""

        now = time.time()
        root_key = str(payload.get("root_key") or "").strip()
        if root_key:
            session.root_key = root_key

        for index, item in enumerate(payload.get("blocks") or []):
            if not isinstance(item, dict):
                continue
            html_fragment = str(item.get("html") or "").strip()
            text = str(item.get("text") or "").strip()
            if not text and html_fragment:
                text = self._extract_text_from_html_fragment(html_fragment)
            if not text:
                continue
            try:
                order = int(item.get("order", index) or index)
            except Exception:
                order = index
            key = str(item.get("key") or "").strip() or f"{session.root_key or 'answer'}:{order}"
            existing = session.blocks.get(key)
            if existing is None:
                session.blocks[key] = AnswerBlock(
                    key=key,
                    order=order,
                    text=text,
                    html=html_fragment,
                    updated_at=now,
                )
                continue
            existing.order = min(existing.order, order)
            existing.text = self._choose_better_answer_text(
                existing.text,
                text,
                baseline_text=self._active_baseline_answer_text,
                keyword=keyword,
                brand=brand,
            )
            existing.html = self._choose_better_answer_html(
                existing.html,
                html_fragment,
                baseline_text=self._active_baseline_answer_text,
                keyword=keyword,
                brand=brand,
            )
            existing.updated_at = now

        materialized = self._materialize_captured_answer(keyword=keyword, brand=brand)
        materialized_html = self._materialize_captured_answer_html(keyword=keyword, brand=brand)
        raw_text = str(payload.get("raw_text") or "").strip()
        if raw_text:
            materialized = self._choose_better_answer_text(
                materialized,
                raw_text,
                baseline_text=self._active_baseline_answer_text,
                keyword=keyword,
                brand=brand,
            )
        raw_html = str(payload.get("raw_html") or "").strip()
        if raw_html:
            materialized_html = self._choose_better_answer_html(
                materialized_html,
                raw_html,
                baseline_text=self._active_baseline_answer_text,
                keyword=keyword,
                brand=brand,
            )
        session.last_materialized_text = materialized
        session.last_materialized_html = materialized_html
        session.last_update_at = now
        return materialized

    def _materialize_captured_answer(self, *, keyword: str = "", brand: str = "") -> str:
        session = self._answer_capture_session
        if session is None:
            return ""
        blocks = sorted(
            session.blocks.values(),
            key=lambda item: (int(item.order), item.updated_at, item.key),
        )
        parts: list[str] = []
        seen_compact: list[str] = []
        for block in blocks:
            text = str(block.text or "").strip()
            if not text:
                continue
            compact = self._normalize_compact_text(text)
            if seen_compact and compact == seen_compact[-1]:
                continue
            parts.append(text)
            seen_compact.append(compact)
        joined = "\n".join(part for part in parts if part).strip()
        session.last_materialized_text = self._choose_better_answer_text(
            session.last_materialized_text,
            joined,
            baseline_text=self._active_baseline_answer_text,
            keyword=keyword,
            brand=brand,
        )
        return session.last_materialized_text

    def _materialize_captured_answer_html(self, *, keyword: str = "", brand: str = "") -> str:
        session = self._answer_capture_session
        if session is None:
            return ""
        blocks = sorted(
            session.blocks.values(),
            key=lambda item: (int(item.order), item.updated_at, item.key),
        )
        parts: list[str] = []
        seen_compact: list[str] = []
        for block in blocks:
            html_fragment = str(block.html or "").strip()
            text = self._extract_text_from_html_fragment(html_fragment) if html_fragment else str(block.text or "").strip()
            if not text:
                continue
            compact = self._normalize_compact_text(text)
            if seen_compact and compact == seen_compact[-1]:
                continue
            if html_fragment:
                parts.append(html_fragment)
            else:
                parts.append(f"<section class=\"answer-block\"><p>{html.escape(text)}</p></section>")
            seen_compact.append(compact)
        joined = "\n".join(part for part in parts if part).strip()
        session.last_materialized_html = self._choose_better_answer_html(
            session.last_materialized_html,
            joined,
            baseline_text=self._active_baseline_answer_text,
            keyword=keyword,
            brand=brand,
        )
        return session.last_materialized_html

    def _end_answer_capture(
        self,
        get_text,
        *,
        keyword: str = "",
        brand: str = "",
    ) -> str:
        captured_text = self._materialize_captured_answer(keyword=keyword, brand=brand)
        if callable(get_text):
            try:
                dom_text = str(get_text() or "").strip()
            except Exception as exc:
                self._reraise_stop_requested(exc)
                dom_text = ""
            captured_text = self._choose_better_answer_text(
                captured_text,
                dom_text,
                baseline_text=self._active_baseline_answer_text,
                keyword=keyword,
                brand=brand,
            )
        return captured_text

    def _is_post_completion_empty_answer_error(self, message: str) -> bool:
        return str(message or "").startswith(self._POST_COMPLETE_EMPTY_ANSWER_ERROR)

    def _get_stable_answer_text(
        self,
        get_text,
        *,
        keyword: str = "",
        brand: str = "",
        attempts: int = 8,
        interval: float = 1.0,
    ) -> tuple[str, bool]:
        """
        DOM 已提示生成完成后，再给回答区一个短暂稳定窗口。
        某些平台（尤其是虚拟列表）会先撤掉“停止生成”按钮，再晚一点才把最终文本挂到 DOM。
        """
        best_text = ""
        best_score = -1

        for index in range(attempts):
            self._raise_if_stop_requested()
            try:
                self.check_for_interruption()
            except InterruptionDetected:
                raise

            if self.chat_container_selector:
                self.page.evaluate(
                    """(selector) => {
                        const el = document.querySelector(selector);
                        if (el) el.scrollTop = el.scrollHeight;
                        else window.scrollTo(0, document.body.scrollHeight);
                    }""",
                    self.chat_container_selector,
                )
            else:
                self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

            wait_seconds = 0.5 if index == 0 else interval
            self._cooperative_sleep(wait_seconds)

            try:
                answer_text = get_text() or ""
            except Exception as exc:
                self._reraise_stop_requested(exc)
                raise
            try:
                snapshot = self._capture_answer_snapshot()
            except Exception as exc:
                self._reraise_stop_requested(exc)
                snapshot = {}
            captured_text = self._merge_answer_snapshot(
                snapshot,
                keyword=keyword,
                brand=brand,
            )
            answer_text = self._choose_better_answer_text(
                answer_text,
                captured_text,
                baseline_text=self._active_baseline_answer_text,
                keyword=keyword,
                brand=brand,
            )
            compact = self._normalize_compact_text(answer_text)
            if len(compact) > best_score:
                best_text = answer_text
                best_score = len(compact)

            if self.has_usable_answer_text(answer_text, keyword=keyword, brand=brand):
                if index > 0:
                    print(f"[{self.name}] 回答区延迟稳定，在第 {index + 1} 次补抓后提取成功")
                return answer_text, True

        if best_text:
            reason = self.explain_unusable_answer_text(best_text, keyword=keyword, brand=brand) or "unknown"
            preview = best_text[:300].replace("\n", "\\n")
            print(f"[{self.name}] 回答可用性校验未通过: {reason}; 预览: {preview}")
        return best_text, False

    def is_generation_complete(self, page_text: str, start_time: float) -> bool:
        """DOM-based completion check. Subclasses override with platform-specific JS."""
        return False

    def _poll_until_complete(self, brand: str, on_rank, get_text=None, timeout: int = 180, min_wait: int = 8, keyword: str = "") -> None:
        """Centralized polling loop. Waits for generation to complete, then calls on_rank once."""
        if get_text is None:
            def get_text():
                if self.chat_container_selector:
                    self.page.evaluate("""(selector) => {
                        const el = document.querySelector(selector);
                        if (el) el.scrollTop = el.scrollHeight;
                        else window.scrollTo(0, document.body.scrollHeight);
                    }""", self.chat_container_selector)
                else:
                    self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                return self.page.evaluate("() => document.body.innerText") or ""
        self._begin_answer_capture(keyword=keyword, brand=brand)
        start_time = time.time()
        last_text = ""
        stable_count = 0
        dom_fail_count = 0  # 连续DOM检测失败次数，用于降级到文本稳定判断
        dom_pending_count = 0  # DOM 持续返回“未完成”的次数，用于触发文本稳定兜底
        last_wait_log_bucket = -1
        baseline_text = str(getattr(self, "_active_baseline_answer_text", "") or "")
        while time.time() - start_time < timeout:
            self._raise_if_stop_requested()
            # 每次轮询前检测人机识别/弹窗
            try:
                self.check_for_interruption()
            except InterruptionDetected:
                print(f"[{self.name}] 人工干预完成，继续等待当前回答生成...")
                start_time = time.time()
                stable_count = 0
                dom_fail_count = 0
                dom_pending_count = 0
                last_text = ""
                self._cooperative_sleep(2)
                continue

            try:
                page_text = get_text()
            except Exception as exc:
                self._reraise_stop_requested(exc)
                raise
            try:
                snapshot = self._capture_answer_snapshot()
            except Exception as exc:
                self._reraise_stop_requested(exc)
                snapshot = {}
            captured_text = self._merge_answer_snapshot(
                snapshot,
                keyword=keyword,
                brand=brand,
            )
            page_text = self._choose_better_answer_text(
                page_text,
                captured_text,
                baseline_text=baseline_text,
                keyword=keyword,
                brand=brand,
            )
            elapsed = time.time() - start_time
            has_new_content = self._has_new_answer_content(
                page_text,
                baseline_text=baseline_text,
                keyword=keyword,
                brand=brand,
            )
            wait_bucket = int(elapsed // 20)
            if elapsed >= min_wait and wait_bucket > last_wait_log_bucket:
                last_wait_log_bucket = wait_bucket
                compact_length = len(self._normalize_compact_text(page_text))
                print(
                    f"[{self.name}] 等待回答中: {int(elapsed)}s, "
                    f"正文长度={compact_length}, 新内容={'是' if has_new_content else '否'}, "
                    f"DOM_pending={dom_pending_count}, DOM_fail={dom_fail_count}"
                )

            if elapsed < min_wait:
                last_text = page_text
                # 增强：随机轮询间隔（0.8-1.5秒），避免固定1秒的机器人特征
                poll_interval = random.uniform(0.8, 1.5)
                self._cooperative_sleep(poll_interval)
                continue

            # DOM信号检测
            generation_done = False
            try:
                dom_done = self.is_generation_complete(page_text, start_time)
                if dom_done:
                    dom_fail_count = 0
                    dom_pending_count = 0
                    # 再等待随机时间二次确认，避免流式输出刚开始时误判
                    confirm_wait = random.uniform(0.8, 1.5)
                    self._cooperative_sleep(confirm_wait)
                    try:
                        page_text = get_text()
                    except Exception as exc:
                        self._reraise_stop_requested(exc)
                        raise
                    has_new_content = self._has_new_answer_content(
                        page_text,
                        baseline_text=baseline_text,
                        keyword=keyword,
                        brand=brand,
                    )
                    if self.is_generation_complete(page_text, start_time) and has_new_content:
                        print(f"[{self.name}] DOM信号：生成完成")
                        generation_done = True
                    elif self.is_generation_complete(page_text, start_time):
                        print(f"[{self.name}] DOM已提示完成，但尚未检测到本轮新回答内容，继续等待")
                else:
                    dom_pending_count += 1
                    # DOM明确返回"未完成"，重置稳定计数，继续等待
                    if page_text != last_text:
                        stable_count = 0
                        last_text = page_text
            except Exception:
                dom_fail_count += 1

            # 文本稳定兜底：
            # 1. DOM 检测持续异常
            # 2. DOM 一直返回“未完成”，但正文已稳定很久（平台完成信号失灵）
            should_use_text_fallback = (
                dom_fail_count >= 3
                or (dom_pending_count >= 8 and elapsed >= max(min_wait + 8, 20))
            )
            if not generation_done and should_use_text_fallback:
                if (
                    page_text == last_text
                    and has_new_content
                    and self.has_usable_answer_text(page_text, keyword=keyword, brand=brand)
                ):
                    stable_count += 1
                    if stable_count >= 4:
                        debug_state = {}
                        try:
                            debug_state = self._get_generation_debug_state() or {}
                        except Exception as exc:
                            self._reraise_stop_requested(exc)
                            debug_state = {}
                        if not self._allow_text_stable_completion(debug_state):
                            stable_count = 0
                            last_text = page_text
                            # 增强：随机等待间隔
                            wait_interval = random.uniform(0.8, 1.5)
                            self._cooperative_sleep(wait_interval)
                            continue
                        if dom_pending_count >= 8 and dom_fail_count < 3:
                            print(f"[{self.name}] DOM长时间未结束，但文本连续稳定，判定生成完成")
                        else:
                            print(f"[{self.name}] 文本连续稳定，判定生成完成")
                        if debug_state:
                            print(f"[{self.name}] 生成态调试(关键): {debug_state}")
                        generation_done = True
                else:
                    stable_count = 0
                    last_text = page_text

            if generation_done:
                final_text, usable = self._get_stable_answer_text(
                    get_text,
                    keyword=keyword,
                    brand=brand,
                )
                capture_final_text = self._end_answer_capture(
                    get_text,
                    keyword=keyword,
                    brand=brand,
                )
                final_text = self._choose_better_answer_text(
                    final_text,
                    capture_final_text,
                    baseline_text=baseline_text,
                    keyword=keyword,
                    brand=brand,
                )
                usable = self.has_usable_answer_text(final_text, keyword=keyword, brand=brand)
                self.last_answer_text = final_text or ""
                if not usable:
                    self.last_error = self._POST_COMPLETE_EMPTY_ANSWER_ERROR
                    print(f"[{self.name}] {self.last_error}")
                    return
                on_rank(self.parse_ranking(final_text, brand), final_text)
                break

            self._cooperative_sleep(1)

        else:
            # 超时兜底：用当前页面文本尝试解析排名
            print(f"[{self.name}] 等待生成超时（{timeout}s），尝试用当前内容解析排名")
            try:
                final_text = get_text()
                capture_final_text = self._end_answer_capture(
                    get_text,
                    keyword=keyword,
                    brand=brand,
                )
                final_text = self._choose_better_answer_text(
                    final_text,
                    capture_final_text,
                    baseline_text=baseline_text,
                    keyword=keyword,
                    brand=brand,
                )
                self.last_answer_text = final_text or ""
                if not self._has_new_answer_content(
                    final_text,
                    baseline_text=baseline_text,
                    keyword=keyword,
                    brand=brand,
                ):
                    self.last_error = "未检测到本轮新的回答内容，可能仍停留在旧对话或问题未真正发送"
                    print(f"[{self.name}] {self.last_error}")
                    return
                if not self.has_usable_answer_text(final_text, keyword=keyword, brand=brand):
                    reason = self.explain_unusable_answer_text(final_text, keyword=keyword, brand=brand) or "unknown"
                    self.last_error = "未获取到有效回答内容，可能触发验证码或回答尚未生成"
                    preview = (final_text or "")[:300].replace("\n", "\\n")
                    print(f"[{self.name}] {self.last_error} ({reason}); 预览: {preview}")
                    return
                on_rank(self.parse_ranking(final_text, brand), final_text)
            except Exception as e:
                self._reraise_stop_requested(e)
                print(f"[{self.name}] 超时兜底解析失败: {e}")

    def _scroll_brand_into_view(self, brand: str) -> None:
        """滚动到品牌词第一次出现的位置，尽量让命中区域进入可视区。"""
        if not brand or not self.chat_container_selector:
            return
        try:
            root_sel = (
                self.result_selector
                if self.result_selector and self.result_selector != "body"
                else self.chat_container_selector
            )
            root_sel = root_sel or "body"
            think_sel = self.think_content_selector or ""
            self.page.evaluate("""({ containerSel, rootSel, thinkSel, brand, lastOnly }) => {
                const container = document.querySelector(containerSel);
                let roots = Array.from(document.querySelectorAll(rootSel));
                if (lastOnly && roots.length > 1) {
                    roots = [roots[roots.length - 1]];
                }
                if (!container || roots.length <= 0) return;
                for (const root of roots) {
                    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                    let node;
                    while (node = walker.nextNode()) {
                        if (!(node.textContent || '').includes(brand)) continue;
                        if (thinkSel && node.parentElement.closest(thinkSel)) continue;
                        const el = node.parentElement;
                        const style = window.getComputedStyle(container);
                        const isScrollable = style.overflowY === 'auto' || style.overflowY === 'scroll';
                        if (isScrollable) {
                            const elRect = el.getBoundingClientRect();
                            const containerRect = container.getBoundingClientRect();
                            const relTop = elRect.top - containerRect.top + container.scrollTop;
                            const target = relTop - container.clientHeight / 2 + elRect.height / 2;
                            container.scrollTop = Math.max(0, target);
                        } else {
                            el.scrollIntoView({block: 'center'});
                        }
                        return;
                    }
                }
            }""", {
                "containerSel": self.chat_container_selector,
                "rootSel": root_sel,
                "thinkSel": think_sel,
                "brand": brand,
                "lastOnly": bool(self.prefer_last_result_block),
            })
            self._cooperative_sleep(0.5)
        except Exception:
            pass

    def _capture_brand_preview(self, brand: str) -> tuple[bytes | None, list[dict], tuple[float, float]]:
        """截图品牌词附近区域，并尝试提取命中词矩形。"""
        try:
            self._scroll_brand_into_view(brand)
            input_sel = self.input_selector
            container_sel = self.chat_container_selector if self.chat_container_selector else ""
            root_sel = (
                self.result_selector
                if self.result_selector and self.result_selector != "body"
                else self.chat_container_selector
            )
            root_sel = root_sel or "body"
            think_sel = self.think_content_selector if self.think_content_selector else ""
            # 找到品牌词在当前视口中的实际位置，以品牌词为中心截取一定高度的区域
            clip_info = self.page.evaluate("""({
                inputSel,
                containerSel,
                rootSel,
                thinkSel,
                brand,
                brandTopPadding,
                brandTopRatio,
                lastOnly
            }) => {
                let brandRect = null;
                let roots = rootSel && rootSel !== "body"
                    ? Array.from(document.querySelectorAll(rootSel))
                    : [document.querySelector(rootSel) || document.body];
                if (lastOnly && roots.length > 1) {
                    roots = [roots[roots.length - 1]];
                }
                for (const root of roots) {
                    if (!root) continue;
                    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                    let node;
                    while (node = walker.nextNode()) {
                        if (!(node.textContent || '').includes(brand)) continue;
                        if (thinkSel && node.parentElement && node.parentElement.closest(thinkSel)) continue;
                        try {
                            const range = document.createRange();
                            const idx = node.textContent.indexOf(brand);
                            range.setStart(node, idx);
                            range.setEnd(node, idx + brand.length);
                            const rects = range.getClientRects();
                            if (rects.length > 0) {
                                brandRect = rects[0];
                                break;
                            }
                        } catch(e) {}
                    }
                    if (brandRect) {
                        break;
                    }
                }

                const container = containerSel ? document.querySelector(containerSel) : null;
                const visualRoot = roots.length > 0 ? roots[roots.length - 1] : null;

                // 如果找到了品牌词位置，以其为中心截取区域
                const W = window.innerWidth;
                const H = window.innerHeight;
                const input = document.querySelector(inputSel);
                const inputTop = input ? input.getBoundingClientRect().top : H;
                if (brandRect && brandRect.top >= 0 && brandRect.top < inputTop) {
                    const visualTop = container
                        ? Math.max(0, container.getBoundingClientRect().top)
                        : (visualRoot ? Math.max(0, visualRoot.getBoundingClientRect().top) : 0);
                    const availH = inputTop - visualTop;
                    const CROP_H = Math.min(availH, Math.max(400, Math.round(availH * 0.8)));
                    const desiredTopPadding = Math.max(
                        brandTopPadding,
                        Math.round(CROP_H * brandTopRatio)
                    );
                    let top = Math.round(brandRect.top - desiredTopPadding);
                    let bottom = top + CROP_H;
                    if (top < visualTop) {
                        bottom += visualTop - top;
                        top = visualTop;
                    }
                    if (bottom > inputTop) {
                        top = Math.max(visualTop, top - (bottom - inputTop));
                        bottom = inputTop;
                    }
                    let left = 0, width = W;
                    const cropBase = container || visualRoot;
                    if (cropBase) {
                        const r = cropBase.getBoundingClientRect();
                        left = Math.round(Math.max(r.left, 0));
                        width = Math.round(Math.min(r.right, W) - left);
                    }
                    if (width <= 0 || bottom <= top) return null;
                    return { x: left, y: top, width: width, height: bottom - top, brand_y: Math.round(brandRect.top - top) };
                }

                // 回退：截取回答区或容器可视区域底部（最新回答通常在底部）
                const fallbackRoot = visualRoot || container;
                if (fallbackRoot) {
                    const r = fallbackRoot.getBoundingClientRect();
                    const left = Math.round(Math.max(r.left, 0));
                    const width = Math.round(Math.min(r.right, W) - left);
                    const CROP_H = Math.round(inputTop - Math.max(0, r.top));
                    const bottom = Math.round(Math.min(inputTop, r.bottom, H));
                    const top = Math.max(Math.round(r.top, 0), bottom - CROP_H);
                    if (width > 0 && bottom > top) return { x: left, y: top, width: width, height: bottom - top };
                }

                if (container) {
                    const r = container.getBoundingClientRect();
                    const left = Math.round(Math.max(r.left, 0));
                    const width = Math.round(Math.min(r.right, W) - left);
                    const CROP_H = Math.round(inputTop - Math.max(0, r.top));
                    const bottom = Math.round(Math.min(inputTop, r.bottom, H));
                    const top = Math.max(Math.round(r.top, 0), bottom - CROP_H);
                    if (width > 0 && bottom > top) return { x: left, y: top, width: width, height: bottom - top };
                }

                return null;
            }""", {
                "inputSel": input_sel,
                "containerSel": container_sel,
                "rootSel": root_sel,
                "thinkSel": think_sel,
                "brand": brand,
                "brandTopPadding": max(0, int(self.screenshot_brand_top_padding_px or 0)),
                "brandTopRatio": max(0.0, min(0.45, float(self.screenshot_brand_top_ratio or 0.0))),
                "lastOnly": bool(self.prefer_last_result_block),
            })
            if clip_info:
                png_bytes = self.page.screenshot(type="png", clip={
                    "x": clip_info["x"], "y": clip_info["y"],
                    "width": clip_info["width"], "height": clip_info["height"],
                })
            else:
                png_bytes = self.page.screenshot(type="png")
            boxes = self._find_brand_rects(brand, clip_info=clip_info)
            scale = (1.0, 1.0)
            if clip_info:
                from PIL import Image
                import io
                with Image.open(io.BytesIO(png_bytes)) as img:
                    scale = (
                        img.width / max(1, int(clip_info.get("width", img.width))),
                        img.height / max(1, int(clip_info.get("height", img.height))),
                    )
            return png_bytes, boxes, scale
        except Exception:
            return None, [], (1.0, 1.0)

    def _find_brand_rects(self, brand: str, clip_info: dict | None = None) -> list[dict]:
        """定位当前可视区域内品牌词的矩形坐标。"""
        if not brand:
            return []
        try:
            root_sel = (
                self.result_selector
                if self.result_selector and self.result_selector != "body"
                else self.chat_container_selector
            ) or "body"
            root_sel = root_sel.replace('"', '\\"')
            think_sel = self.think_content_selector.replace('"', '\\"') if self.think_content_selector else ""
            clip_x = float((clip_info or {}).get("x", 0))
            clip_y = float((clip_info or {}).get("y", 0))
            clip_w = float((clip_info or {}).get("width", 0))
            clip_h = float((clip_info or {}).get("height", 0))
            boxes = self.page.evaluate(
                r"""({ rootSel, brand, thinkSel, clipX, clipY, clipW, clipH, lastOnly }) => {
                    if (!brand) return [];
                    let roots = rootSel && rootSel !== "body"
                        ? Array.from(document.querySelectorAll(rootSel))
                        : [document.querySelector(rootSel) || document.body];
                    if (lastOnly && roots.length > 1) {
                        roots = [roots[roots.length - 1]];
                    }
                    if (roots.length <= 0) return [];
                    const escaped = brand.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
                    const regex = new RegExp(escaped, "ig");
                    const results = [];
                    for (const root of roots) {
                        if (!root) continue;
                        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                        let node;
                        while ((node = walker.nextNode())) {
                            const text = node.textContent || "";
                            if (!text.trim()) continue;
                            if (thinkSel && node.parentElement && node.parentElement.closest(thinkSel)) continue;
                            regex.lastIndex = 0;
                            let match;
                            while ((match = regex.exec(text))) {
                                const range = document.createRange();
                                range.setStart(node, match.index);
                                range.setEnd(node, match.index + match[0].length);
                                for (const rect of range.getClientRects()) {
                                    const relX = rect.left - clipX;
                                    const relY = rect.top - clipY;
                                    if (clipW && (relX + rect.width < 0 || relX > clipW)) continue;
                                    if (clipH && (relY + rect.height < 0 || relY > clipH)) continue;
                                    results.push({
                                        x: Math.max(0, relX),
                                        y: Math.max(0, relY),
                                        width: rect.width,
                                        height: rect.height,
                                    });
                                    if (results.length >= 8) return results;
                                }
                            }
                        }
                    }
                    return results;
                }""",
                {
                    "rootSel": root_sel,
                    "brand": brand,
                    "thinkSel": think_sel,
                        "clipX": clip_x,
                        "clipY": clip_y,
                        "clipW": clip_w,
                        "clipH": clip_h,
                        "lastOnly": bool(self.prefer_last_result_block),
                    },
                )
            return boxes if isinstance(boxes, list) else []
        except Exception:
            return []

    def take_long_screenshot(self, rank: int = 0, quality: int = 85, brand: str = "") -> str:
        """
        滚动到品牌名第一次出现的位置，截视口截图，裁掉左侧侧边栏。
        """
        from PIL import Image
        import io
        from core.screenshot_tools import decorate_screenshot

        ts = datetime.now().strftime("%m%d_%H%M%S")
        screenshots_dir = resolve_app_dir("screenshots")
        filename = screenshots_dir / f"{self.name}_{ts}.jpg"

        try:
            preview_png, preview_boxes, preview_scale = self._capture_brand_preview(brand)
            png_bytes = preview_png or self.page.screenshot(type="png")
        except Exception as e:
            print(f"[{self.name}] 截图失败: {e}")
            return ""

        img = Image.open(io.BytesIO(png_bytes))
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')

        img.save(filename, format='JPEG', quality=quality, optimize=True)
        self.last_screenshot_meta = {
            "screenshot": str(filename),
            "highlight_count": len(preview_boxes),
            "preview_available": bool(preview_png),
        }
        meta = decorate_screenshot(
            str(filename),
            platform_name=self.name,
            brand=brand,
            keyword="",
            preview_png=preview_png,
            preview_boxes=preview_boxes,
            preview_scale=preview_scale,
            draw_boxes_on_main=True,
        )
        self.last_screenshot_meta.update(meta)
        print(f"[{self.name}] 长截图已保存: {filename}")
        return str(filename)

    def _take_long_screenshot_with_retries(self, brand: str = "", max_retries: int = 3) -> str:
        """多次尝试截图，只有文件真实生成后才视为成功。"""
        last_error = ""
        for attempt in range(1, max_retries + 1):
            screenshot = ""
            try:
                screenshot = self.take_long_screenshot(brand=brand)
            except Exception as e:
                last_error = str(e)
                print(f"[{self.name}] 第 {attempt}/{max_retries} 次截图异常: {e}")

            if screenshot and os.path.exists(screenshot):
                return screenshot

            if screenshot:
                print(f"[{self.name}] 第 {attempt}/{max_retries} 次截图文件不存在: {screenshot}")
            else:
                print(f"[{self.name}] 第 {attempt}/{max_retries} 次截图未生成有效文件")

            if attempt < max_retries:
                self._cooperative_sleep(min(1.5 * attempt, 3.0))

        self.last_error = last_error or f"已识别到品牌名，但截图生成失败：{brand}"
        return ""

    def _take_dom_render_screenshot(self, brand: str = "", keyword: str = "") -> str:
        """提取回答 DOM，本地复排后直接截图。"""
        from platforms.html_renderer import render_html_to_screenshot, render_text_to_screenshot

        ts = datetime.now().strftime("%m%d_%H%M%S")
        screenshots_dir = resolve_app_dir("screenshots")
        filename = screenshots_dir / f"{self.name}_{ts}_dom.jpg"

        captured_html = str(self._materialize_captured_answer_html(keyword=keyword, brand=brand) or "").strip()
        html_body = captured_html or str(self._get_answer_html() or "").strip()
        answer_text = str(self.last_answer_text or "").strip()
        if not answer_text:
            answer_text = str(self._get_answer_text() or "").strip()

        html_text = self._extract_text_from_html_fragment(html_body)
        html_len = len(self._normalize_compact_text(html_text))
        answer_len = len(self._normalize_compact_text(answer_text))
        prefer_text_render = False
        if answer_len > 0 and (
            not html_body
            or html_len <= 0
            or answer_len > html_len + max(80, int(html_len * 0.35))
            or html_len > answer_len + max(120, int(answer_len * 0.5))
        ):
            prefer_text_render = True
            if html_body and answer_len > 0:
                print(
                    f"[{self.name}] DOM HTML与全文不匹配，回退全文文本复排: "
                    f"html_len={html_len}, answer_len={answer_len}"
                )

        screenshot = ""
        if html_body and not prefer_text_render:
            screenshot = render_html_to_screenshot(
                html_body,
                self.name,
                keyword=keyword,
                brand=brand,
                output_path=str(filename),
                include_badges=True,
            )
        elif answer_text:
            screenshot = render_text_to_screenshot(
                answer_text,
                self.name,
                keyword=keyword,
                brand=brand,
                output_path=str(filename),
                include_badges=True,
            )

        if not screenshot or not os.path.exists(screenshot):
            self.last_error = f"已识别到品牌名，但 DOM 复排截图生成失败：{brand or keyword or self.name}"
            return ""

        self.last_screenshot_meta = {
            "screenshot": str(screenshot),
            "highlight_count": 0,
            "preview_available": False,
            "render_mode": "dom",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        print(f"[{self.name}] DOM 复排截图已保存: {screenshot}")
        return str(screenshot)

    @staticmethod
    def _extract_text_from_html_fragment(html_body: str) -> str:
        fragment = str(html_body or "").strip()
        if not fragment:
            return ""
        text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", fragment, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</(p|div|section|article|li|ul|ol|table|tr|blockquote|pre|h[1-6])>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text.strip()

    def _take_answer_screenshot_with_retries(
        self,
        *,
        brand: str = "",
        keyword: str = "",
        max_retries: int = 3,
    ) -> str:
        """根据当前截图模式选择原始页面截图或 DOM 复排截图。"""
        mode = str(getattr(self, "answer_screenshot_mode", "page") or "page").strip().lower()
        if mode != "dom":
            return self._take_long_screenshot_with_retries(brand=brand, max_retries=max_retries)

        last_error = ""
        for attempt in range(1, max_retries + 1):
            screenshot = ""
            try:
                screenshot = self._take_dom_render_screenshot(brand=brand, keyword=keyword)
            except Exception as exc:
                last_error = str(exc)
                print(f"[{self.name}] 第 {attempt}/{max_retries} 次 DOM 复排截图异常: {exc}")

            if screenshot and os.path.exists(screenshot):
                return screenshot

            print(f"[{self.name}] 第 {attempt}/{max_retries} 次 DOM 复排截图未生成有效文件")
            if attempt < max_retries:
                self._cooperative_sleep(min(0.8 * attempt, 2.0))

        self.last_error = last_error or f"已识别到品牌名，但 DOM 复排截图失败：{brand or keyword or self.name}"
        return ""

    def _stitch_screenshot(self) -> bytes:
        """
        通过 clip 参数只截容器区域，滚动后逐帧拼接，避免侧边栏干扰和重叠错位。
        """
        from PIL import Image
        import io

        if not self.chat_container_selector:
            return self.page.screenshot(type="png")

        container_sel = self.chat_container_selector
        input_sel = self.input_selector

        scroll_info = self.page.evaluate("""({ containerSel, inputSel }) => {
            const el = document.querySelector(containerSel);
            if (!el) return {found: false};
            const style = window.getComputedStyle(el);
            const overflowY = style.overflowY;
            const isScrollable = (overflowY === 'auto' || overflowY === 'scroll') && el.scrollHeight > el.clientHeight;
            const rect = el.getBoundingClientRect();
            const visibleTop = Math.max(rect.top, 0);
            const input = document.querySelector(inputSel);
            const inputTop = input ? input.getBoundingClientRect().top : window.innerHeight;
            const clipHeight = Math.round(Math.min(inputTop, window.innerHeight) - visibleTop);
            return {
                found: true,
                scrollHeight: isScrollable ? el.scrollHeight : Math.max(document.body.scrollHeight, document.documentElement.scrollHeight),
                clientHeight: clipHeight,
                maxScroll: isScrollable ? el.scrollHeight - el.clientHeight : 0,
                useElement: isScrollable,
                left: Math.round(rect.left),
                top: Math.round(visibleTop),
                width: Math.round(rect.width),
            };
        }""", {"containerSel": container_sel, "inputSel": input_sel})

        if not scroll_info['found']:
            raise Exception("容器未找到")

        total_height = scroll_info['scrollHeight']
        client_height = scroll_info['clientHeight']
        max_scroll = scroll_info['maxScroll']
        clip_left = scroll_info['left']
        clip_top = scroll_info['top']
        clip_width = scroll_info['width']

        if scroll_info['useElement']:
            scroll_js = "(payload) => { const el = document.querySelector(payload.selector); if (el) el.scrollTop = payload.pos; }"
            get_scroll_js = "(selector) => { const el = document.querySelector(selector); return el ? el.scrollTop : null; }"
        else:
            scroll_js = "(pos) => window.scrollTo(0, pos)"
            get_scroll_js = "() => window.scrollY"

        if total_height <= client_height * 1.2:
            return self.page.screenshot(type="png", clip={
                "x": clip_left, "y": clip_top,
                "width": clip_width, "height": client_height
            })

        def capture_at(scroll_pos: float) -> tuple[float, Image.Image]:
            if scroll_info['useElement']:
                self.page.evaluate(scroll_js, {"selector": self.chat_container_selector, "pos": scroll_pos})
            else:
                self.page.evaluate(scroll_js, scroll_pos)
            self._cooperative_sleep(0.5)
            actual_scroll = (
                self.page.evaluate(get_scroll_js, self.chat_container_selector)
                if scroll_info['useElement']
                else self.page.evaluate(get_scroll_js)
            )
            if actual_scroll is None:
                raise Exception("滚动位置读取失败")
            frame_bytes = self.page.screenshot(type="png", clip={
                "x": clip_left, "y": clip_top,
                "width": clip_width, "height": client_height
            })
            frame = Image.open(io.BytesIO(frame_bytes))
            if frame.mode != 'RGB':
                frame = frame.convert('RGB')
            return float(actual_scroll), frame

        first_scroll, first = capture_at(0)
        frame_px_h = first.height
        dpr = frame_px_h / client_height
        step = int(client_height * 0.85)
        min_capture_scroll = first_scroll
        total_h = max(
            frame_px_h,
            int(round(total_height * dpr)),
            int(round((max_scroll - min_capture_scroll + client_height) * dpr)),
        )

        captures = [(first_scroll, first)]
        current_scroll = first_scroll
        stagnant_rounds = 0

        while current_scroll + 0.5 < max_scroll:
            target_scroll = min(current_scroll + step, max_scroll)
            actual_scroll, frame = capture_at(target_scroll)

            if abs(actual_scroll - captures[-1][0]) > 0.5:
                captures.append((actual_scroll, frame))
                stagnant_rounds = 0
            else:
                stagnant_rounds += 1

            current_scroll = actual_scroll
            if target_scroll >= max_scroll and actual_scroll + 0.5 >= max_scroll:
                break
            if stagnant_rounds >= 2:
                break

        if not captures:
            raise Exception("无截图帧")

        captures.sort(key=lambda item: item[0])
        total_w = captures[0][1].width
        stitched = Image.new('RGB', (total_w, total_h))
        for scroll_top, frame in captures:
            start_px = int(round((scroll_top - min_capture_scroll) * dpr))
            if start_px >= total_h:
                continue
            paste_h = min(frame.height, total_h - start_px)
            if paste_h <= 0:
                continue
            stitched.paste(frame.crop((0, 0, frame.width, paste_h)), (0, start_px))

        out = io.BytesIO()
        stitched.save(out, format='PNG')
        return out.getvalue()

    def _get_answer_text(self) -> str:
        """获取回答文本，优先提取回答区，避免把提问内容或验证码文案混入判定。"""
        try:
            return self.page.evaluate(
                """({containerSel, resultSel, thinkSel, lastOnly}) => {
                    const root = containerSel ? document.querySelector(containerSel) : document.body;
                    if (root) {
                        const style = window.getComputedStyle(root);
                        if (style.overflowY === 'auto' || style.overflowY === 'scroll') {
                            root.scrollTop = root.scrollHeight;
                        } else {
                            window.scrollTo(0, document.body.scrollHeight);
                        }
                    } else {
                        window.scrollTo(0, document.body.scrollHeight);
                    }

                    const source = (root || document.body).cloneNode(true);
                    if (thinkSel) {
                        source.querySelectorAll(thinkSel).forEach((el) => el.remove());
                    }

                    if (resultSel && resultSel !== 'body') {
                        const blocks = Array.from(source.querySelectorAll(resultSel))
                            .filter((el) => !thinkSel || !el.closest(thinkSel))
                            .map((el) => (el.innerText || '').trim())
                            .filter(Boolean);
                        if (blocks.length > 0) {
                            return (lastOnly ? blocks.slice(-1) : blocks).join('\\n');
                        }
                    }

                    return (source.innerText || '').trim();
                }""",
                {
                    "containerSel": self.chat_container_selector or "",
                    "resultSel": self.result_selector or "",
                    "thinkSel": self.think_content_selector or "",
                    "lastOnly": bool(self.prefer_last_result_block),
                },
            ) or ""
        except Exception as exc:
            self._reraise_stop_requested(exc)
            return ""

    def _get_answer_html(self) -> str:
        """提取回答区 HTML，优先保留 markdown 语义结构，用于本地复排截图。"""
        try:
            return self.page.evaluate(
                """({containerSel, resultSel, thinkSel, lastOnly}) => {
                    const normalizeText = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    const isVisible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return (
                            style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            style.opacity !== '0' &&
                            rect.width > 0 &&
                            rect.height > 0
                        );
                    };
                    const roots = resultSel
                        ? Array.from(document.querySelectorAll(resultSel))
                            .filter((el) => isVisible(el) && (!thinkSel || !el.closest(thinkSel)))
                        : [];
                    const resolveGroupRoot = (node) => {
                        if (!node) return null;
                        return (
                            node.closest(
                                '[class*="speech_show"], [class*="speechShow"], [class*="answerItem"], '
                                + '[data-testid*="message"], [class*="message"], article, li, [role="listitem"]'
                            )
                            || node.parentElement
                            || node
                        );
                    };

                    let selected = roots;
                    if (lastOnly && roots.length > 0) {
                        const lastRoot = roots[roots.length - 1];
                        const groupRoot = resolveGroupRoot(lastRoot);
                        selected = roots.filter((el) => resolveGroupRoot(el) === groupRoot);
                        if (selected.length <= 0) {
                            selected = [lastRoot];
                        }
                    }

                    const removableSelectors = [
                        'script',
                        'style',
                        'noscript',
                        'button',
                        'textarea',
                        'input',
                        'select',
                        'canvas',
                        'svg',
                        'iframe',
                        'video',
                        'audio',
                        'img',
                        'picture',
                        'figure',
                        'figcaption',
                        '[contenteditable="false"]',
                        '[aria-hidden="true"]',
                        '[class*="reference"]',
                        '[class*="Reference"]',
                        '[class*="citation"]',
                        '[class*="Citation"]',
                        '[class*="footnote"]',
                        '[class*="Footnote"]',
                        '[class*="quote-extra"]',
                        '[class*="source"]',
                        '[class*="Source"]',
                    ];
                    const allowedAttrs = new Set(['href', 'src', 'alt', 'title', 'colspan', 'rowspan']);
                    const sanitized = [];
                    const referenceHeadingPattern = /^(参考资料|参考来源|资料来源|来源|引用|引用来源|网页来源|参考网页|相关阅读|延伸阅读|参考链接)$/;
                    const citationOnlyPattern = /^(?:\\[?\\d+\\]?|[①②③④⑤⑥⑦⑧⑨⑩]+|\\(\\d+\\)|\\d+[、.]?)(?:\\s*[、,，;；]\\s*(?:\\[?\\d+\\]?|[①②③④⑤⑥⑦⑧⑨⑩]+|\\(\\d+\\)|\\d+[、.]?))*$/;
                    const contentContainers = 'section, article, div, aside, footer, details, summary, ul, ol, li';
                    const cleanupEmptyNodes = (root) => {
                        const structuralSelector = 'table, thead, tbody, tr, th, td, ul, ol, li, pre, code, blockquote, hr, br';
                        for (let round = 0; round < 4; round += 1) {
                            let removed = false;
                            Array.from(root.querySelectorAll('*')).reverse().forEach((el) => {
                                if (!el || !el.parentElement) return;
                                if (el.matches(structuralSelector)) return;
                                const text = normalizeText(el.textContent || '');
                                if (text) return;
                                if (el.querySelector(structuralSelector)) return;
                                el.remove();
                                removed = true;
                            });
                            if (!removed) break;
                        }
                    };

                    const sanitizeElement = (el) => {
                        if (!el) return;
                        const tag = String(el.tagName || '').toLowerCase();
                        const text = normalizeText(el.textContent || '');
                        if (tag === 'sup') {
                            el.remove();
                            return;
                        }
                        if (tag === 'a' && (el.closest('sup') || citationOnlyPattern.test(text))) {
                            el.remove();
                            return;
                        }
                        if ((tag === 'p' || tag === 'div' || tag === 'span' || tag === 'strong' || tag === 'li')
                            && referenceHeadingPattern.test(text) && el.children.length <= 1) {
                            const container = el.closest(contentContainers);
                            if (container) {
                                container.remove();
                            } else {
                                el.remove();
                            }
                            return;
                        }
                        for (const attr of Array.from(el.attributes || [])) {
                            const name = String(attr.name || '').toLowerCase();
                            if (!allowedAttrs.has(name)) {
                                el.removeAttribute(attr.name);
                            }
                        }
                        if (tag === 'a') {
                            const href = String(el.getAttribute('href') || '').trim();
                            if (!href) {
                                el.replaceWith(...Array.from(el.childNodes));
                            }
                        }
                    };

                    for (const node of selected) {
                        const clone = node.cloneNode(true);
                        if (thinkSel) {
                            clone.querySelectorAll(thinkSel).forEach((el) => el.remove());
                        }
                        clone.querySelectorAll(removableSelectors.join(',')).forEach((el) => el.remove());
                        sanitizeElement(clone);
                        clone.querySelectorAll('*').forEach((el) => sanitizeElement(el));
                        cleanupEmptyNodes(clone);
                        const tag = String(clone.tagName || '').toLowerCase();
                        const unwrapTags = new Set(['div', 'section', 'article', 'aside', 'footer', 'details', 'summary']);
                        const attrs = Array.from(clone.attributes || []);
                        const flattenedHtml = (
                            unwrapTags.has(tag) && attrs.length === 0
                                ? String(clone.innerHTML || '')
                                : String(clone.outerHTML || '')
                        ).trim();
                        if (flattenedHtml) {
                            sanitized.push('<section class="answer-block">' + flattenedHtml + '</section>');
                        }
                    }

                    if (sanitized.length > 0) {
                        return sanitized.join('\\n');
                    }

                    const container = containerSel ? document.querySelector(containerSel) : document.body;
                    return container ? String(container.innerHTML || '').trim() : '';
                }""",
                {
                    "containerSel": self.chat_container_selector or "",
                    "resultSel": self.result_selector or "",
                    "thinkSel": self.think_content_selector or "",
                    "lastOnly": bool(self.prefer_last_result_block),
                },
            ) or ""
        except Exception as exc:
            self._reraise_stop_requested(exc)
            return ""

    def _scroll_answer_view_to_top(self) -> None:
        try:
            if self.chat_container_selector:
                self.page.evaluate(
                    """(selector) => {
                        const el = document.querySelector(selector);
                        if (el) el.scrollTop = 0;
                    }""",
                    self.chat_container_selector,
                )
            else:
                self.page.evaluate("window.scrollTo(0, 0)")
            self._cooperative_sleep(0.8)
        except Exception:
            pass

    def _extract_answer_reference_metadata(self) -> None:
        # 提取引用信息源
        try:
            references = self.extract_answer_references()
            self.last_references = references
            if references:
                print(f"[{self.name}] 提取到 {len(references)} 条平台抓取源")
                for ref in references:
                    print(f"[{self.name}]   [{ref.get('index')}] {ref.get('source')} - {ref.get('url')[:50]}...")
            else:
                print(f"[{self.name}] 未提取到引用信息源（回答中可能没有引用）")
        except Exception as e:
            print(f"[{self.name}] 引用信息提取失败: {e}")
            self.last_references = []

        # 提取正文引用源
        try:
            body_references = self.extract_body_references()
            self.last_body_references = body_references
            if body_references:
                print(f"[{self.name}] 提取到 {len(body_references)} 条正文引用源")
            else:
                print(f"[{self.name}] 未提取到正文引用源")
        except Exception as e:
            print(f"[{self.name}] 正文引用提取失败: {e}")
            self.last_body_references = []

    def _maybe_extract_answer_reference_metadata(self) -> None:
        if not bool(getattr(self, "extract_references_enabled", False)):
            self.last_references = []
            self.last_body_references = []
            print(f"[{self.name}] 已关闭引用抓取，跳过提取")
            return
        self._extract_answer_reference_metadata()

    def extract_answer_references(self) -> list[dict]:
        """
        提取 AI 回答中的引用信息源
        返回格式: [{"index": 1, "title": "...", "url": "...", "source": "..."}, ...]

        默认返回空列表，子类可以根据平台特性覆盖此方法。
        """
        return []

    def extract_body_references(self) -> list[dict]:
        """
        提取 AI 回答正文中的引用链接（上标引用、脚注引用等）
        返回格式: [{"index": 1, "title": "...", "url": "...", "source": "..."}, ...]

        默认返回空列表，子类可以根据平台特性覆盖此方法。
        """
        return []

    def _should_abort_query_retries_for_error(self, error_message: str) -> bool:
        """
        子类可覆盖：当异常说明当前浏览器会话已进入坏状态时，直接结束本次查询，
        把 session 轮换交给上层管理器处理，而不是继续在同一实例里重试。
        """
        return False

    def search(self, keyword: str, brand: str, max_retries: int = 5, deep_think: bool = True) -> Tuple[int, Optional[str]]:
        """通用搜索流程：新对话 -> 输入 -> 等待生成完成 -> 截图"""
        self._raise_if_stop_requested()

        # 确保浏览器已启动
        if self.page is None or self.context is None:
            print(f"[{self.name}] 浏览器未启动，正在启动...")
            self._start_stop_watcher()
            self.start()
            self.page.goto(self.target_url, timeout=30000, wait_until="domcontentloaded")
            print(f"[{self.name}] 浏览器已启动，已导航到 {self.target_url}")

        self.ensure_logged_in(timeout=15)
        self.last_answer_text = ""
        self.last_screenshot_meta = {}
        self.last_error = ""
        self.last_references = []
        self.last_body_references = []
        self.last_run_recovered_manually = False
        no_hit_message = f"未识别到品牌名 {brand}"

        for attempt in range(1, max_retries + 1):
            self._raise_if_stop_requested()
            self.last_error = ""
            self._active_baseline_answer_text = ""
            self._answer_capture_session = None
            if self.progress_callback:
                try:
                    self.progress_callback({
                        "stage": "attempt_start",
                        "platform": self.name,
                        "keyword": keyword,
                        "brand": brand,
                        "attempt": attempt,
                        "max_attempts": max_retries,
                    })
                except Exception:
                    pass
            print(f"\n[{self.name}] 尝试 {attempt}/{max_retries}: '{keyword}' -> 查找 '{brand}'")

            try:
                # 重试前检查页面状态（输入框可见性 + 弹窗）
                if attempt > 1:
                    self.check_for_interruption(check_input_visible=True)

                self.start_new_chat()
                if deep_think:
                    self.enable_deep_think()
                if hasattr(self, 'enable_web_search'):
                    self.enable_web_search()
                baseline_answer_text = self._get_answer_text()
                self._active_baseline_answer_text = baseline_answer_text or ""

                # 增强：30% 概率在输入前模拟浏览行为（更像真实用户）
                if self._should_simulate_human_behavior() and random.random() < 0.3:
                    try:
                        self._simulate_human_behavior()
                    except Exception as e:
                        print(f"[{self.name}] 输入前行为模拟失败: {e}")

                self.type_like_human(keyword)
                self.submit_prompt()

                found_rank = [None]

                def on_rank(rank, page_text):
                    self.last_answer_text = page_text or ""
                    match_info = self.explain_brand_mention(page_text, brand)
                    if match_info.get("matched"):
                        ignored_negated_echo = self._is_negated_brand_echo(
                            page_text,
                            brand,
                            keyword=keyword,
                            match_info=match_info,
                        )
                        if ignored_negated_echo:
                            print(f"[{self.name}] 品牌命中已忽略：检测到否定型提问回显")
                            if self.name == "tongyi":
                                excerpt = str(match_info.get("excerpt") or "").replace("\n", "\\n")
                                answer_preview = str((page_text or "")[:500]).replace("\n", "\\n")
                                print(f"[{self.name}] 忽略命中调试: excerpt={excerpt}")
                                print(f"[{self.name}] 回答预览(前500字): {answer_preview}")
                            return
                        print(f"[{self.name}] ✅ 找到品牌名")
                        if self.name == "tongyi":
                            excerpt = str(match_info.get("excerpt") or "").replace("\n", "\\n")
                            normalized_excerpt = str(match_info.get("normalized_excerpt") or "").replace("\n", "\\n")
                            answer_preview = str((page_text or "")[:500]).replace("\n", "\\n")
                            print(f"[{self.name}] 品牌命中调试: mode={match_info.get('mode')}, excerpt={excerpt}")
                            print(f"[{self.name}] 品牌命中调试(归一化): {normalized_excerpt}")
                            print(f"[{self.name}] 回答预览(前500字): {answer_preview}")
                        # rank==99 表示找到品牌词但无序号，视为排名=1（有提及）
                        found_rank[0] = rank if (rank is not None and rank != 99) else 1

                self._poll_until_complete(brand, on_rank, get_text=self._get_answer_text, keyword=keyword)

                if self.last_error:
                    print(
                        f"[{self.name}] {self.last_error}，"
                        f"{'准备重试...' if attempt < max_retries else '已达最大重试次数'}"
                    )
                    if self.progress_callback:
                        try:
                            self.progress_callback({
                                "stage": "attempt_done",
                                "platform": self.name,
                                "keyword": keyword,
                                "brand": brand,
                                "attempt": attempt,
                                "max_attempts": max_retries,
                                "success": False,
                                "error_message": self.last_error,
                            })
                        except Exception:
                            pass
                    self._apply_retry_backoff(attempt, max_retries, self.last_error)
                    continue

                if not self._has_new_answer_content(
                    self.last_answer_text,
                    baseline_text=baseline_answer_text,
                    keyword=keyword,
                    brand=brand,
                ):
                    self.last_error = "未检测到新的有效回答内容，可能触发验证码、停留在旧对话或回答尚未生成"
                    print(
                        f"[{self.name}] {self.last_error}，"
                        f"{'准备重试...' if attempt < max_retries else '已达最大重试次数'}"
                    )
                    if self.progress_callback:
                        try:
                            self.progress_callback({
                                "stage": "attempt_done",
                                "platform": self.name,
                                "keyword": keyword,
                                "brand": brand,
                                "attempt": attempt,
                                "max_attempts": max_retries,
                                "success": False,
                                "error_message": self.last_error,
                            })
                        except Exception:
                            pass
                    self._apply_retry_backoff(attempt, max_retries, self.last_error)
                    continue

                if found_rank[0] is not None:
                    render_mode = str(getattr(self, "answer_screenshot_mode", "page") or "page").strip().lower()
                    screenshot = ""
                    if render_mode == "dom":
                        screenshot = self._take_answer_screenshot_with_retries(
                            brand=brand,
                            keyword=keyword,
                        )
                        self._maybe_extract_answer_reference_metadata()
                        self._scroll_answer_view_to_top()
                    else:
                        self._scroll_answer_view_to_top()
                        self._maybe_extract_answer_reference_metadata()
                        screenshot = self._take_answer_screenshot_with_retries(
                            brand=brand,
                            keyword=keyword,
                        )
                    if screenshot:
                        if self.progress_callback:
                            try:
                                self.progress_callback({
                                    "stage": "attempt_done",
                                    "platform": self.name,
                                    "keyword": keyword,
                                    "brand": brand,
                                    "attempt": attempt,
                                    "max_attempts": max_retries,
                                    "success": True,
                                })
                            except Exception:
                                pass
                        return found_rank[0], screenshot

                    screenshot_error = self.last_error or f"已识别到品牌名，但截图生成失败：{brand}"
                    print(
                        f"[{self.name}] {screenshot_error}，"
                        f"{'准备重试...' if attempt < max_retries else '已达最大重试次数'}"
                    )
                    if self.progress_callback:
                        try:
                            self.progress_callback({
                                "stage": "attempt_done",
                                "platform": self.name,
                                "keyword": keyword,
                                "brand": brand,
                                "attempt": attempt,
                                "max_attempts": max_retries,
                                "success": False,
                                "error_message": screenshot_error,
                            })
                        except Exception:
                            pass
                    self._apply_retry_backoff(attempt, max_retries, screenshot_error)
                    continue

                print(
                    f"[{self.name}] 未找到品牌名 '{brand}'，"
                    f"{'准备重试...' if attempt < max_retries else '已达最大重试次数'}"
                )
                self._maybe_extract_answer_reference_metadata()
                if self.progress_callback:
                    try:
                        self.progress_callback({
                            "stage": "attempt_done",
                            "platform": self.name,
                            "keyword": keyword,
                            "brand": brand,
                            "attempt": attempt,
                            "max_attempts": max_retries,
                            "success": False,
                            "error_message": "" if attempt < max_retries else no_hit_message,
                        })
                    except Exception:
                        pass
                self._apply_retry_backoff(attempt, max_retries, no_hit_message)

            except SchedulerStopRequested:
                raise
            except InterruptionDetected:
                print(f"[{self.name}] 人工干预完成，重新开始...")
                self._apply_retry_backoff(attempt, max_retries, "人工干预后重新尝试")
                continue
            except Exception as e:
                self._reraise_stop_requested(e)
                import traceback
                self.last_error = str(e)
                print(f"[{self.name}] 搜索出错: {e}\n{traceback.format_exc()}")
                if self.progress_callback:
                    try:
                        self.progress_callback({
                            "stage": "attempt_done",
                            "platform": self.name,
                            "keyword": keyword,
                            "brand": brand,
                            "attempt": attempt,
                            "max_attempts": max_retries,
                            "success": False,
                            "error_message": str(e),
                        })
                    except Exception:
                        pass
                if self._should_abort_query_retries_for_error(str(e)):
                    print(f"[{self.name}] 当前异常命中会话重启条件，结束本次查询并交由上层轮换 session")
                    return 99, None
                # context 被关闭时尝试重启，否则后续重试都会失败
                if "Target page, context or browser has been closed" in str(e) or "TargetClosedError" in type(e).__name__:
                    print(f"[{self.name}] 浏览器 context 已关闭，尝试重启...")
                    try:
                        self._relaunch_context(headless=True)
                        self.page.goto(self.target_url, wait_until="domcontentloaded")
                        self._wait_for_page_stability("浏览器重启后重新进入目标页")
                        print(f"[{self.name}] 浏览器已重启")
                    except Exception as re_e:
                        print(f"[{self.name}] 重启失败，放弃: {re_e}")
                        return 99, None
                self._apply_retry_backoff(attempt, max_retries, self.last_error)
            finally:
                self._active_baseline_answer_text = ""
                self._answer_capture_session = None

        if not self.last_error:
            self.last_error = no_hit_message
        return 99, None

    def close(self):
        """关闭浏览器上下文"""
        self._stop_watcher_event.set()
        self._release_browser_handles(stop_playwright=True)
        watcher = self._stop_watcher_thread
        if watcher and watcher.is_alive() and watcher is not threading.current_thread():
            watcher.join(timeout=1)
        self._stop_watcher_thread = None
        print(f"[{self.name}] 浏览器已关闭")

    def __enter__(self):
        """上下文管理器支持"""
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器支持"""
        self.close()
        return False
