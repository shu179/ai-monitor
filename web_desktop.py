"""Desktop shell for the Web UI."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from core.windows_bootstrap import install_windows_bootstrap
install_windows_bootstrap()

from PySide6.QtCore import QEvent, QObject, QPoint, QSettings, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtWebEngineWidgets import QWebEngineView

from core.app_paths import resolve_app_path
from core.shutdown import install_shutdown_handlers, register_shutdown_callback, run_shutdown_callbacks
from core.version import APP_NAME
from web_backend import create_server


class DirectoryPickerBridge(QObject):
    request_pick = Signal(str, str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._pending: dict[str, dict[str, object]] = {}
        self._lock = threading.Lock()
        self.request_pick.connect(self._on_request_pick)

    def pick_directory(self, title: str) -> str:
        request_id = f"pick-{id(self)}-{threading.get_ident()}-{len(self._pending)}"
        done = threading.Event()
        with self._lock:
            self._pending[request_id] = {
                "event": done,
                "path": "",
            }
        self.request_pick.emit(request_id, title)
        done.wait(timeout=120)
        with self._lock:
            result = self._pending.pop(request_id, None) or {}
        return str(result.get("path") or "")

    @Slot(str, str)
    def _on_request_pick(self, request_id: str, title: str) -> None:
        path = QFileDialog.getExistingDirectory(
            None,
            title or "选择目录",
            str(Path.home()),
            QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontResolveSymlinks,
        )
        with self._lock:
            pending = self._pending.get(request_id)
            if not pending:
                return
            pending["path"] = str(path or "")
            event = pending.get("event")
        if isinstance(event, threading.Event):
            event.set()


class DesktopWebWindow(QMainWindow):
    def __init__(self, url: str):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1440, 980)
        self._view = QWebEngineView(self)
        self._view.load(QUrl(url))
        self.setCentralWidget(self._view)


class RecognitionOverlayWindow(QWidget):
    PLATFORM_OPTIONS = (
        ("doubao", "豆包"),
        ("deepseek", "DeepSeek"),
        ("kimi", "Kimi"),
        ("yuanbao", "元宝"),
        ("tongyi", "通义"),
        ("wenxin", "文心"),
    )

    def __init__(self, base_url: str):
        super().__init__(None)
        self._base_url = base_url.rstrip("/")
        self._drag_offset = QPoint()
        self._expanded = False
        self._current_keyword = ""
        self._current_platforms: set[str] = set()
        self._forward_action = "next"
        self._busy_action = ""
        self._keyword_display_text = ""
        self._keyword_copy_text = "复制"
        self._last_running = False
        self._dismissed_for_current_run = False
        self._ball_click_count = 0
        self._platform_browser_minimized = False
        self._settings = QSettings("OpenAI", APP_NAME)
        self.setWindowTitle("识别模式悬浮窗")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.resize(56, 56)
        self.move(self._restore_position())

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        self._platform_panel = QFrame(self)
        self._platform_panel.setFixedWidth(188)
        self._platform_buttons: dict[str, QPushButton] = {}
        platform_layout = QVBoxLayout(self._platform_panel)
        platform_layout.setContentsMargins(8, 8, 8, 8)
        platform_layout.setSpacing(4)
        for platform_id, label in self.PLATFORM_OPTIONS:
            button = QPushButton(label, self._platform_panel)
            button.setObjectName("platformButton")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, pid=platform_id: self._open_platform(pid))
            self._platform_buttons[platform_id] = button
            platform_layout.addWidget(button)
        root.addWidget(self._platform_panel)

        self._card = QFrame(self)
        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(10, 10, 10, 10)
        card_layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(8)
        self._brand_label = QLabel("暂无品牌", self._card)
        self._brand_label.setObjectName("brandPill")
        header.addWidget(self._brand_label, 1)
        close_button = QPushButton("×", self._card)
        close_button.setObjectName("closeButton")
        close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        close_button.clicked.connect(self._dismiss_current_run)
        header.addWidget(close_button)
        card_layout.addLayout(header)

        self._keyword_label = QLabel("", self)
        self._keyword_label.setObjectName("keywordButton")
        self._keyword_label.setWordWrap(True)
        self._keyword_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._keyword_label.installEventFilter(self)
        self._keyword_label.mousePressEvent = lambda event: self._copy_keyword()  # type: ignore[method-assign]
        card_layout.addWidget(self._keyword_label)

        nav = QHBoxLayout()
        nav.setSpacing(8)
        self._prev_button = QPushButton("上一个", self._card)
        self._prev_button.setObjectName("navButton")
        self._prev_button.clicked.connect(lambda: self._run_action("prev"))
        self._next_button = QPushButton("下一个", self._card)
        self._next_button.setObjectName("navButton")
        self._next_button.clicked.connect(lambda: self._run_action(self._forward_action))
        nav.addWidget(self._prev_button)
        nav.addWidget(self._next_button)
        card_layout.addLayout(nav)

        self._open_button = QPushButton("打开平台", self._card)
        self._open_button.setObjectName("menuButton")
        self._open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._open_button.enterEvent = lambda event: self._show_platform_panel()  # type: ignore[method-assign]
        card_layout.addWidget(self._open_button)

        self._screenshot_button = QPushButton("截图", self._card)
        self._screenshot_button.setObjectName("menuButton")
        self._screenshot_button.clicked.connect(lambda: self._run_action("screenshot"))
        card_layout.addWidget(self._screenshot_button)

        self._detail_label = QLabel("", self)
        self._detail_label.setWordWrap(True)
        self._detail_label.setObjectName("detailText")
        card_layout.addWidget(self._detail_label)

        root.addWidget(self._card)

        self._ball = QPushButton("◎", self)
        self._ball.setObjectName("floatBall")
        self._ball.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ball.clicked.connect(self._handle_ball_click)
        self._ball.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._ball.customContextMenuRequested.connect(self._show_ball_menu)
        root.addWidget(self._ball)

        self.setStyleSheet(
            """
            RecognitionOverlayWindow {
                background: transparent;
            }
            QFrame {
                background: rgba(255, 255, 255, 0.95);
                border: 1px solid rgba(203, 213, 225, 0.95);
                border-radius: 18px;
            }
            QLabel#brandPill {
                background: #f8fafc;
                border: 1px solid #e2e8f0;
                border-radius: 14px;
                color: #0f172a;
                font-size: 12px;
                font-weight: 700;
                padding: 7px 9px;
            }
            QLabel#keywordButton {
                background: #ffffff;
                border: 1px solid #e2e8f0;
                border-radius: 14px;
                color: #0f172a;
                font-size: 14px;
                font-weight: 700;
                padding: 10px 10px;
            }
            QLabel#detailText {
                color: #64748b;
                font-size: 10px;
                padding: 2px 1px;
            }
            QPushButton {
                background: #ffffff;
                border: 1px solid #e2e8f0;
                border-radius: 14px;
                color: #0f172a;
                font-size: 12px;
                font-weight: 700;
                min-height: 34px;
                padding: 6px 8px;
            }
            QPushButton:hover {
                background: #f1f5f9;
                border-color: #cbd5e1;
            }
            QPushButton#menuButton:hover, QPushButton#platformButton:hover, QPushButton#navButton:hover {
                background: #f1f5f9;
                border-color: #e2e8f0;
                color: #0f172a;
            }
            QPushButton#platformButton[active="true"] {
                background: #f1f5f9;
                border-color: #e2e8f0;
                color: #0f172a;
            }
            QPushButton#closeButton {
                min-width: 30px;
                max-width: 30px;
                min-height: 30px;
                border-radius: 15px;
                color: #64748b;
            }
            QPushButton#floatBall {
                background: #020617;
                border: 5px solid #ffffff;
                border-radius: 24px;
                color: #38bdf8;
                font-size: 22px;
                font-weight: 900;
                min-width: 48px;
                max-width: 48px;
                min-height: 48px;
                max-height: 48px;
                padding: 0;
            }
            """
        )

        self._collapse_timer = QTimer(self)
        self._collapse_timer.setSingleShot(True)
        self._collapse_timer.setInterval(500)
        self._collapse_timer.timeout.connect(self._collapse)

        self._ball_click_timer = QTimer(self)
        self._ball_click_timer.setSingleShot(True)
        self._ball_click_timer.setInterval(320)
        self._ball_click_timer.timeout.connect(self._flush_ball_clicks)

        self._timer = QTimer(self)
        self._timer.setInterval(1500)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self._platform_panel.enterEvent = lambda event: self._platform_panel.show()  # type: ignore[method-assign]
        self._platform_panel.leaveEvent = lambda event: self._platform_panel.hide()  # type: ignore[method-assign]
        self._collapse()
        self.hide()

    def _restore_position(self) -> QPoint:
        fallback = QPoint(1080, 100)
        raw = self._settings.value("recognitionOverlay/position")
        if isinstance(raw, QPoint):
            return self._clamp_to_screen(raw)
        if isinstance(raw, str) and "," in raw:
            try:
                x_text, y_text = raw.split(",", 1)
                return self._clamp_to_screen(QPoint(int(float(x_text)), int(float(y_text))))
            except Exception:
                pass
        return self._clamp_to_screen(fallback)

    def _clamp_to_screen(self, point: QPoint) -> QPoint:
        screen = QApplication.screenAt(point) or QApplication.primaryScreen()
        if screen is None:
            return point
        area = screen.availableGeometry()
        width = max(self.width(), 56)
        height = max(self.height(), 56)
        x = min(max(point.x(), area.left()), max(area.left(), area.right() - width))
        y = min(max(point.y(), area.top()), max(area.top(), area.bottom() - height))
        return QPoint(x, y)

    def _persist_position(self) -> None:
        point = self.pos()
        self._settings.setValue("recognitionOverlay/position", f"{point.x()},{point.y()}")

    def _dismiss_current_run(self) -> None:
        self._dismissed_for_current_run = True
        self._platform_browser_minimized = False
        self._run_action("close_platform_browser")
        self.hide()

    def _show_ball_menu(self, point: QPoint) -> None:
        menu = QMenu(self)
        disable_today_action = menu.addAction("今天停用")
        disable_action = menu.addAction("停用")
        disable_today_action.triggered.connect(self._disable_current_task_today)
        disable_action.triggered.connect(self._disable_recognition)
        menu.popup(self._ball.mapToGlobal(point))

    def _disable_current_task_today(self) -> None:
        self._dismissed_for_current_run = True
        self._platform_browser_minimized = False
        self._run_action("suppress_current_task_today")
        self.hide()

    def _disable_recognition(self) -> None:
        self._dismissed_for_current_run = True
        self._platform_browser_minimized = False
        self._run_action("disable_recognition")
        self.hide()

    def _post_action(self, action: str, payload: dict | None = None) -> dict:
        body = json.dumps({"action": action, **(payload or {})}).encode("utf-8")
        request = Request(
            f"{self._base_url}/api/recognition/action",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=1.5) as response:
            return json.loads(response.read().decode("utf-8"))

    def _open_platform(self, platform_id: str) -> None:
        self._platform_browser_minimized = False
        self._run_action("open_platform", {"platform": platform_id})

    def _run_action(self, action: str, payload: dict | None = None) -> None:
        if self._busy_action and action not in {"close_platform_browser", "minimize_platform_browser", "restore_platform_browser", "toggle_platform_browser"}:
            return
        self._busy_action = action
        self._set_controls_enabled(False)

        def worker() -> None:
            try:
                self._post_action(action, payload)
            except Exception as exc:
                print(f"[RecognitionOverlay] 动作失败: {action}: {exc}")
            finally:
                QTimer.singleShot(0, self._finish_action)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_action(self) -> None:
        self._busy_action = ""
        self._set_controls_enabled(True)
        self.refresh()

    def _set_controls_enabled(self, enabled: bool) -> None:
        for button in list(self._platform_buttons.values()) + [
            self._prev_button,
            self._next_button,
            self._open_button,
            self._screenshot_button,
        ]:
            button.setEnabled(enabled)

    def _copy_keyword(self) -> None:
        if not self._current_keyword:
            return
        QApplication.clipboard().setText(self._current_keyword)
        self._keyword_label.setText("已复制")
        self._detail_label.setText("已复制关键词")

    def _handle_ball_click(self) -> None:
        self._ball_click_count += 1
        self._ball_click_timer.start()

    def _flush_ball_clicks(self) -> None:
        click_count = int(self._ball_click_count or 0)
        self._ball_click_count = 0
        if click_count >= 2:
            self._run_action("toggle_platform_browser")
            self._collapse()
        elif self._expanded:
            self._collapse()
        else:
            self._expand()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self._keyword_label:
            if event.type() == QEvent.Type.Enter and self._current_keyword:
                self._keyword_label.setText(self._keyword_copy_text)
            elif event.type() == QEvent.Type.Leave:
                self._keyword_label.setText(self._keyword_display_text or self._current_keyword or "暂无关键词")
        return super().eventFilter(watched, event)

    def _expand(self) -> None:
        self._expanded = True
        self._collapse_timer.stop()
        self._card.show()
        self._ball.hide()
        self.resize(280, 248)

    def _show_platform_panel(self) -> None:
        if not self._expanded:
            self._expand()
        self._platform_panel.show()
        self.resize(476, 248)

    def _collapse(self) -> None:
        self._expanded = False
        self._platform_panel.hide()
        self._card.hide()
        self._ball.show()
        self.resize(56, 56)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._expand()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._collapse_timer.start()
        super().leaveEvent(event)

    def refresh(self) -> None:
        try:
            with urlopen(f"{self._base_url}/api/recognition/status", timeout=1.2) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (URLError, TimeoutError, ValueError, OSError):
            self.hide()
            return

        status = payload.get("status") or {}
        guide = status.get("keyword_guide") or {}
        items = guide.get("items") or []
        index = int(guide.get("index", 0) or 0)
        running = bool(status.get("running"))
        if running and not self._last_running:
            self._dismissed_for_current_run = False
        self._last_running = running
        if not running or not items:
            self.hide()
            return
        if self._dismissed_for_current_run:
            return

        index = max(0, min(index, len(items) - 1))
        current = items[index]
        brand = ((current.get("brands") or [])[:1] or [current.get("task_name", "")])[0]
        keyword = str(current.get("keyword", "") or "").strip()
        detail = str(guide.get("detail_text", "") or "").strip()
        manual_mode = bool(guide.get("manual_mode"))
        controls_visible = guide.get("controls_visible", True) is not False
        action_enabled = guide.get("action_enabled", True) not in {"disabled", False}
        platforms = {
            str(platform or "").strip()
            for platform in (current.get("platforms") or [])
            if str(platform or "").strip()
        }

        screenshot_count = max(0, int(current.get("screenshot_count", 0) or 0))
        screenshot_total = max(1, int(current.get("screenshot_total", 1) or 1))
        done_keywords = max(0, int(current.get("completed_keyword_count", 0) or 0))
        total_keyword_units = max(0, int(current.get("total_keyword_count", len(items)) or len(items)))

        self._current_keyword = keyword
        self._current_platforms = platforms
        self._forward_action = "complete" if (manual_mode or index >= len(items) - 1) else "next"
        self._brand_label.setText(str(brand or current.get("task_name", "")).strip())
        self._keyword_display_text = f"{keyword or '暂无关键词'}      {screenshot_count:>2}/{screenshot_total:<2}"
        self._keyword_copy_text = "已复制" if self._keyword_label.text() == "已复制" else "复制"
        self._keyword_label.setText(self._keyword_display_text)
        self._detail_label.setText(detail or ("手动推进模式" if manual_mode else "OCR 自动识别"))
        progress_text = f"{done_keywords:>2}/{(total_keyword_units or len(items)):<2}"
        self._prev_button.setText(f"上一个      {progress_text}")
        self._next_button.setText(f"下一个      {progress_text}")
        self._prev_button.setEnabled(bool(controls_visible and index > 0))
        self._next_button.setEnabled(bool(controls_visible and (self._forward_action != "complete" or action_enabled)))
        self._open_button.setEnabled(True)
        self._screenshot_button.setEnabled(True)
        for platform_id, button in self._platform_buttons.items():
            button.setEnabled(True)
            highlighted = platform_id in self._current_platforms
            button.setProperty("active", "true" if highlighted else "false")
            button.style().unpolish(button)
            button.style().polish(button)
        self.show()
        self.raise_()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.move(self._clamp_to_screen(event.globalPosition().toPoint() - self._drag_offset))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._persist_position()
            event.accept()
            return
        super().mouseReleaseEvent(event)


def run_desktop_app() -> None:
    try:
        from core.screenshot_cleanup import cleanup_screenshots_from_default_config
        cleanup_screenshots_from_default_config()
    except Exception:
        import logging
        logging.getLogger(__name__).warning("Screenshot cleanup failed", exc_info=True)
    server = create_server()
    server_stop_lock = threading.Lock()
    server_stopped = False
    server_started = False

    def _stop_server_once() -> None:
        nonlocal server_stopped
        with server_stop_lock:
            if server_stopped:
                return
            server_stopped = True
        server.stop()

    register_shutdown_callback("web-desktop.server", _stop_server_once)
    install_shutdown_handlers()

    try:
        url = server.start()
        server_started = True
    except Exception as exc:
        print(f"[WebDesktop] 本地服务启动失败，改为直接打开静态页面: {exc}")
        dist_index = resolve_app_path("web-ui/dist/index.html")
        url = Path(dist_index).as_uri()

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("OpenAI")
    server.runtime._exit_callback = app.quit
    directory_picker = DirectoryPickerBridge(app)
    server.runtime._directory_picker_callback = directory_picker.pick_directory

    window = DesktopWebWindow(url)
    overlay = RecognitionOverlayWindow(url) if server_started else None
    if server_started:
        app.aboutToQuit.connect(lambda: run_shutdown_callbacks("qt aboutToQuit"))
    window.show()
    if overlay is not None:
        overlay.refresh()
    sys.exit(app.exec())
