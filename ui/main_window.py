"""
Surfaced - 统一主窗口

当前版本优先使用更接近系统原生的浅色 Tk/ttk 外观，
保留原有功能结构，方便后续替换成正式 UI。
"""

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, scrolledtext, messagebox
from collections import Counter
import threading
import re
import yaml
from pathlib import Path

from core.app_paths import resolve_app_path
from core.article_store import get_articles, get_task_article_counts
from core.config_watcher import ConfigWatcher, load_config as load_yaml_config
from core.daily_task_state import ensure_config_task_ids
from core.notifier import send_scheduler_test_message
from core.quick_todos import normalize_quick_todos
from core.runtime_state import should_auto_resume_monitoring, set_auto_resume_monitoring
from core.scheduler import describe_task_schedule, normalize_weekly_times
from core.screenshot_tools import get_decoration_theme, get_default_decoration_theme
from ui.config_runtime import persist_config, persist_config_with_feedback
from ui.article_window import ArticleWindow
from ui.home_copy import build_home_copy_context, get_home_messages, pick_home_message
from ui.status_palette import get_percentage_color
from ui.tk_compat import (
    bind_mousewheel_recursive,
    install_global_tk_behaviors,
    scroll_canvas_on_mousewheel,
)
from ui.keyword_guide import KeywordGuideWindow
from ui.quick_todo_window import QuickTodoWindow

# ── 原生风格令牌 ──────────────────────────────────────────────────────────────
_BG         = "#F0F0F0"
_SURFACE    = "#FFFFFF"
_SURFACE2   = "#F7F7F7"
_BORDER     = "#D0D0D0"
_BORDER2    = "#C4C4C4"

_BLOB_A     = _BG
_BLOB_B     = _BG
_BLOB_C     = _BG
_BLOB_D     = _BG

_TEXT_PRI   = "#202020"
_TEXT_SEC   = "#4F4F4F"
_TEXT_TER   = "#6F6F6F"

_ACCENT     = "#0A64A4"
_SUCCESS    = "#1F7A1F"
_DANGER     = "#B42318"
_WARNING    = "#A15C00"
_TREND_ACTUAL = "#2563EB"
_TREND_PRED   = "#94A3B8"
_TREND_FILL   = "#DDE7FF"
_BAR_EMPTY    = "#CBD5E1"

_GLASS      = _BG
_GLASS_BORDER = _BORDER
_MONITOR_OFF_BG = "#0A64A4"
_MONITOR_OFF_BG_ACTIVE = "#084E80"
_MONITOR_ON_BG = "#E7F4EA"
_MONITOR_ON_BG_ACTIVE = "#D8EDDE"
_MONITOR_ON_FG = "#1D5E34"

_FONT       = "TkDefaultFont"
_FONT_MONO  = "TkFixedFont"

HERO_H      = 0

_BROWSER_ANSWER_MODE_OPTIONS = {
    "页面原始截图": "page",
    "DOM 复排截图": "dom",
}

FORMAL_MODES = [
    ("抓取模式", "固定脚本操作浏览器、抓页面内容、规则判断、截图发送"),
    ("保险模式", "直接走平台 API，默认联网搜索，适合稳定兜底"),
    ("识别模式", "自动读取剪切板截图，识别品牌并归类统计后确认发送"),
    ("智能模式", "AI 在后台接管浏览器操作，完成输入、判断、截图和发送"),
]


def _hex_blend(c1: str, c2: str, t: float) -> str:
    """线性插值两个 hex 颜色，t=0→c1, t=1→c2"""
    r1, g1, b1 = int(c1[1:3],16), int(c1[3:5],16), int(c1[5:7],16)
    r2, g2, b2 = int(c2[1:3],16), int(c2[3:5],16), int(c2[5:7],16)
    r = int(r1 + (r2-r1)*t)
    g = int(g1 + (g2-g1)*t)
    b = int(b1 + (b2-b1)*t)
    return f"#{r:02x}{g:02x}{b:02x}"


def _draw_diffuse_gradient(canvas, w, h):
    """在 canvas 上绘制弥散渐变背景（多色 blob 叠加）"""
    canvas.delete("all")
    # 底色
    canvas.create_rectangle(0, 0, w, h, fill=_BG, outline="")

    # blob A — 左侧蓝紫，大椭圆
    steps = 32
    for i in range(steps, 0, -1):
        t = i / steps
        alpha_color = _hex_blend(_BG, _BLOB_A, t * 0.55)
        r = int(w * 0.52 * t)
        cx, cy = int(w * 0.18), int(h * 0.55)
        canvas.create_oval(cx-r, cy-int(r*0.7), cx+r, cy+int(r*0.7),
                           fill=alpha_color, outline="")

    # blob B — 右侧紫，中椭圆
    for i in range(steps, 0, -1):
        t = i / steps
        alpha_color = _hex_blend(_BG, _BLOB_B, t * 0.45)
        r = int(w * 0.38 * t)
        cx, cy = int(w * 0.78), int(h * 0.45)
        canvas.create_oval(cx-r, cy-int(r*0.65), cx+r, cy+int(r*0.65),
                           fill=alpha_color, outline="")

    # blob C — 中央天蓝，小椭圆
    for i in range(steps, 0, -1):
        t = i / steps
        alpha_color = _hex_blend(_BG, _BLOB_C, t * 0.30)
        r = int(w * 0.28 * t)
        cx, cy = int(w * 0.50), int(h * 0.35)
        canvas.create_oval(cx-r, cy-int(r*0.5), cx+r, cy+int(r*0.5),
                           fill=alpha_color, outline="")

    # blob D — 右下翠绿点缀
    for i in range(steps, 0, -1):
        t = i / steps
        alpha_color = _hex_blend(_BG, _BLOB_D, t * 0.20)
        r = int(w * 0.22 * t)
        cx, cy = int(w * 0.88), int(h * 0.80)
        canvas.create_oval(cx-r, cy-int(r*0.5), cx+r, cy+int(r*0.5),
                           fill=alpha_color, outline="")

    # 毛玻璃遮罩：底部渐变收敛到 _BG
    fade_h = max(1, h // 3)
    for i in range(fade_h):
        t = i / fade_h
        col = _hex_blend(_BG, _GLASS, 1 - t * 0.6)
        canvas.create_rectangle(0, h - fade_h + i, w, h - fade_h + i + 1,
                                 fill=col, outline="")


def _draw_round_rect(canvas, x1, y1, x2, y2, radius, *, fill, outline="", width=1):
    radius = max(0, min(radius, int((x2 - x1) / 2), int((y2 - y1) / 2)))

    canvas.create_rectangle(x1 + radius, y1, x2 - radius, y2, fill=fill, outline="")
    canvas.create_rectangle(x1, y1 + radius, x2, y2 - radius, fill=fill, outline="")
    canvas.create_oval(x1, y1, x1 + radius * 2, y1 + radius * 2, fill=fill, outline="")
    canvas.create_oval(x2 - radius * 2, y1, x2, y1 + radius * 2, fill=fill, outline="")
    canvas.create_oval(x1, y2 - radius * 2, x1 + radius * 2, y2, fill=fill, outline="")
    canvas.create_oval(x2 - radius * 2, y2 - radius * 2, x2, y2, fill=fill, outline="")

    if not outline:
        return

    canvas.create_line(x1 + radius, y1, x2 - radius, y1, fill=outline, width=width)
    canvas.create_line(x1 + radius, y2, x2 - radius, y2, fill=outline, width=width)
    canvas.create_line(x1, y1 + radius, x1, y2 - radius, fill=outline, width=width)
    canvas.create_line(x2, y1 + radius, x2, y2 - radius, fill=outline, width=width)
    canvas.create_arc(x1, y1, x1 + radius * 2, y1 + radius * 2, start=90, extent=90,
                      style=tk.ARC, outline=outline, width=width)
    canvas.create_arc(x2 - radius * 2, y1, x2, y1 + radius * 2, start=0, extent=90,
                      style=tk.ARC, outline=outline, width=width)
    canvas.create_arc(x1, y2 - radius * 2, x1 + radius * 2, y2, start=180, extent=90,
                      style=tk.ARC, outline=outline, width=width)
    canvas.create_arc(x2 - radius * 2, y2 - radius * 2, x2, y2, start=270, extent=90,
                      style=tk.ARC, outline=outline, width=width)


def _split_tags(value) -> list[str]:
    if isinstance(value, str):
        raw_items = re.split(r"[,\n，、;/]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw_items = [str(item) for item in value]
    else:
        return []
    seen = set()
    tags = []
    for item in raw_items:
        tag = str(item or "").strip()
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


def _task_brand_page(task: dict) -> str:
    return str(task.get("brand_page") or task.get("brand_page_name") or task.get("name") or "").strip()


def _task_tag_summary(task: dict) -> str:
    industry = _split_tags(task.get("industry_tags") or task.get("industry"))
    region = _split_tags(task.get("region_tags") or task.get("region"))
    parts = []
    if industry:
        parts.append(f"行业：{' / '.join(industry[:3])}")
    if region:
        parts.append(f"地区：{' / '.join(region[:3])}")
    return "  ·  ".join(parts)


def _task_primary_industry(task: dict) -> str:
    industry = _split_tags(task.get("industry_tags") or task.get("industry"))
    return industry[0] if industry else "未分类"


def _region_key(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"[\s·•,，、;/]+", "", text)
    text = text.replace("中华人民共和国", "").replace("中国", "")
    text = text.replace("特别行政区", "")
    text = text.replace("壮族自治区", "").replace("回族自治区", "").replace("维吾尔自治区", "")
    text = text.replace("自治区", "")
    text = text.replace("省", "").replace("市", "").replace("州", "")
    ascii_chars = re.sub(r"[^A-Za-z0-9]+", "", text.lower())
    if ascii_chars:
        return ascii_chars
    return text


def _build_region_lookup(points: list[dict]) -> dict[str, dict]:
    lookup: dict[str, dict] = {}
    for point in points:
        for alias in [point["label"], *(point.get("aliases") or [])]:
            key = _region_key(alias)
            if key and key not in lookup:
                lookup[key] = point
    return lookup


_DOMESTIC_REGION_POINTS = [
    {"label": "北京", "aliases": ["北京市", "京"], "x": 0.74, "y": 0.22},
    {"label": "天津", "aliases": ["天津市"], "x": 0.77, "y": 0.26},
    {"label": "河北", "aliases": ["河北省"], "x": 0.69, "y": 0.30},
    {"label": "山西", "aliases": ["山西省"], "x": 0.63, "y": 0.30},
    {"label": "内蒙古", "aliases": ["内蒙古自治区"], "x": 0.58, "y": 0.16},
    {"label": "辽宁", "aliases": ["辽宁省"], "x": 0.84, "y": 0.18},
    {"label": "吉林", "aliases": ["吉林省"], "x": 0.90, "y": 0.12},
    {"label": "黑龙江", "aliases": ["黑龙江省"], "x": 0.90, "y": 0.06},
    {"label": "上海", "aliases": ["上海市"], "x": 0.86, "y": 0.46},
    {"label": "江苏", "aliases": ["江苏省"], "x": 0.80, "y": 0.42},
    {"label": "浙江", "aliases": ["浙江省"], "x": 0.85, "y": 0.52},
    {"label": "安徽", "aliases": ["安徽省"], "x": 0.76, "y": 0.50},
    {"label": "福建", "aliases": ["福建省"], "x": 0.83, "y": 0.61},
    {"label": "江西", "aliases": ["江西省"], "x": 0.72, "y": 0.60},
    {"label": "山东", "aliases": ["山东省"], "x": 0.76, "y": 0.34},
    {"label": "河南", "aliases": ["河南省"], "x": 0.67, "y": 0.47},
    {"label": "湖北", "aliases": ["湖北省"], "x": 0.64, "y": 0.56},
    {"label": "湖南", "aliases": ["湖南省"], "x": 0.66, "y": 0.66},
    {"label": "广东", "aliases": ["广东省"], "x": 0.72, "y": 0.80},
    {"label": "广西", "aliases": ["广西壮族自治区"], "x": 0.62, "y": 0.82},
    {"label": "海南", "aliases": ["海南省"], "x": 0.76, "y": 0.93},
    {"label": "重庆", "aliases": ["重庆市"], "x": 0.49, "y": 0.56},
    {"label": "四川", "aliases": ["四川省"], "x": 0.40, "y": 0.55},
    {"label": "贵州", "aliases": ["贵州省"], "x": 0.48, "y": 0.67},
    {"label": "云南", "aliases": ["云南省"], "x": 0.36, "y": 0.76},
    {"label": "西藏", "aliases": ["西藏自治区"], "x": 0.18, "y": 0.56},
    {"label": "陕西", "aliases": ["陕西省"], "x": 0.54, "y": 0.42},
    {"label": "甘肃", "aliases": ["甘肃省"], "x": 0.39, "y": 0.30},
    {"label": "青海", "aliases": ["青海省"], "x": 0.31, "y": 0.40},
    {"label": "宁夏", "aliases": ["宁夏回族自治区"], "x": 0.47, "y": 0.27},
    {"label": "新疆", "aliases": ["新疆维吾尔自治区"], "x": 0.12, "y": 0.38},
    {"label": "香港", "aliases": ["香港特别行政区"], "x": 0.80, "y": 0.88},
    {"label": "澳门", "aliases": ["澳门特别行政区"], "x": 0.77, "y": 0.90},
    {"label": "台湾", "aliases": ["台湾省"], "x": 0.92, "y": 0.72},
]

_INTERNATIONAL_REGION_POINTS = [
    {"label": "美国", "aliases": ["USA", "US", "U.S.", "United States", "United States of America"], "x": 0.18, "y": 0.42},
    {"label": "加拿大", "aliases": ["Canada"], "x": 0.14, "y": 0.24},
    {"label": "墨西哥", "aliases": ["Mexico"], "x": 0.18, "y": 0.58},
    {"label": "巴西", "aliases": ["Brazil"], "x": 0.29, "y": 0.74},
    {"label": "英国", "aliases": ["UK", "United Kingdom", "Britain", "England"], "x": 0.45, "y": 0.30},
    {"label": "法国", "aliases": ["France"], "x": 0.48, "y": 0.36},
    {"label": "德国", "aliases": ["Germany"], "x": 0.51, "y": 0.32},
    {"label": "俄罗斯", "aliases": ["Russia"], "x": 0.67, "y": 0.20},
    {"label": "印度", "aliases": ["India"], "x": 0.68, "y": 0.57},
    {"label": "日本", "aliases": ["Japan"], "x": 0.83, "y": 0.40},
    {"label": "韩国", "aliases": ["South Korea", "Korea"], "x": 0.79, "y": 0.36},
    {"label": "新加坡", "aliases": ["Singapore"], "x": 0.72, "y": 0.74},
    {"label": "澳大利亚", "aliases": ["Australia"], "x": 0.86, "y": 0.84},
    {"label": "阿联酋", "aliases": ["UAE", "United Arab Emirates"], "x": 0.59, "y": 0.57},
    {"label": "沙特阿拉伯", "aliases": ["Saudi Arabia"], "x": 0.55, "y": 0.49},
]

_DOMESTIC_REGION_LOOKUP = _build_region_lookup(_DOMESTIC_REGION_POINTS)
_INTERNATIONAL_REGION_LOOKUP = _build_region_lookup(_INTERNATIONAL_REGION_POINTS)


class _GlassCard(tk.Frame):
    """兼容旧结构的普通卡片。"""
    def __init__(self, parent, **kw):
        super().__init__(parent, bg=_GLASS,
                         highlightbackground=_GLASS_BORDER,
                         highlightthickness=1, **kw)


class MainWindow:
    """统一主窗口，包含5个标签页"""

    def __init__(self, scheduler=None, notifier=None, config=None, config_path="config.yaml",
                 recognition_manager=None, on_config_change=None, execute_task_callback=None):
        self.scheduler   = scheduler
        self.notifier    = notifier
        self.config      = config or {}
        self.config_path = resolve_app_path(config_path)
        self.recognition_manager = recognition_manager
        self.on_config_change = on_config_change
        self.execute_task_callback = execute_task_callback
        self.current_mode = "抓取模式"
        self.mode_notice = ""
        self._home_copy_messages = []
        self._home_copy_cursor = 0
        self.last_results = {}
        self.running     = False
        self._timeout_warnings = {}
        self.config_revision = 0
        self._review_items = []
        self._diagnostic_items = []
        self._report_data = None
        self._review_preview_photo = None
        self._keyword_guide = None
        self._mode_cards = {}
        self._mode_card_labels = {}
        self._overview_trend_redraw = None
        self._industry_source_redraw = None
        self._publication_media_redraw = None
        self._region_distribution_redraw = None
        self._publication_media_filter = "all"
        self._region_distribution_mode = "domestic"
        self._message_action_phase = "idle"
        self._config_poll_interval_ms = 1500
        self._article_poll_interval_ms = 4000
        self._config_watcher = None
        self._article_poll_started = False
        self._article_last_mtime = None
        self._should_restore_monitoring_on_launch = True

        self.root = tk.Tk()
        self.root.title("Surfaced")
        self.root.geometry("1080x720")
        self.root.minsize(860, 600)
        self.root.configure(bg=_BG)
        install_global_tk_behaviors(self.root)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._init_ui_resources()

        self._build_ui()
        self._start_config_watcher()
        self._schedule_config_poll()

    def _schedule_config_poll(self):
        try:
            self.root.after(self._config_poll_interval_ms, self._poll_config_file)
        except Exception:
            pass

    def _start_config_watcher(self):
        if self._config_watcher is not None:
            return

        def _on_change(new_config: dict):
            try:
                self.root.after(0, lambda cfg=new_config: self._apply_external_config(cfg))
            except Exception:
                pass

        try:
            self._config_watcher = ConfigWatcher(str(self.config_path), on_change=_on_change)
            self._config_watcher.start_polling(interval=max(1.0, self._config_poll_interval_ms / 1000.0))
        except Exception:
            self._config_watcher = None

    def _stop_config_watcher(self):
        watcher = self._config_watcher
        self._config_watcher = None
        if watcher is None:
            return
        try:
            watcher.stop()
        except Exception:
            pass

    def _apply_external_config(self, new_config: dict):
        if not new_config:
            return
        ensure_config_task_ids(new_config)
        if new_config == self.config:
            return
        self.config = new_config
        self._trend_built = False
        self._refresh_mode_cards()
        self._refresh_overview_trend()
        self._refresh_tasks()
        self._refresh_quick_todo_panel()
        self._refresh_region_distribution()
        self._refresh_status()
        self._refresh_mode_notice()

    def _poll_config_file(self):
        try:
            new_config = load_yaml_config(self.config_path)
            if new_config:
                ensure_config_task_ids(new_config)
                if new_config != self.config:
                    self.config = new_config
                    self._trend_built = False
                    self._refresh_mode_cards()
                    self._refresh_overview_trend()
                    self._refresh_tasks()
                    self._refresh_quick_todo_panel()
                    self._refresh_region_distribution()
                    self._refresh_status()
                    self._refresh_mode_notice()
        finally:
            self._schedule_config_poll()

    def _init_ui_resources(self):
        """初始化依赖 root 的 UI 资源，兼容托盘 Toplevel 打开路径。"""
        base_todo_font = tkfont.nametofont(_FONT)
        self._todo_font = tkfont.Font(self.root, font=base_todo_font)
        self._todo_done_font = tkfont.Font(self.root, font=base_todo_font)
        self._todo_done_font.configure(overstrike=1)

    # ──────────────────────────────────────────────────────────────────────────
    # 顶层布局
    # ──────────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.root.configure(bg=_BG)
        style = ttk.Style()
        style.theme_use("default")
        style.configure(
            "MonitorOff.TButton",
            padding=(16, 9),
            font=(_FONT, 9, "bold"),
            background=_MONITOR_OFF_BG,
            foreground="#FFFFFF",
            borderwidth=0,
            relief="flat",
        )
        style.map(
            "MonitorOff.TButton",
            background=[("active", _MONITOR_OFF_BG_ACTIVE), ("pressed", _MONITOR_OFF_BG_ACTIVE)],
            foreground=[("disabled", "#E7EEF5")],
        )
        style.configure(
            "MonitorOn.TButton",
            padding=(16, 9),
            font=(_FONT, 9, "bold"),
            background=_MONITOR_ON_BG,
            foreground=_MONITOR_ON_FG,
            borderwidth=1,
            relief="flat",
        )
        style.map(
            "MonitorOn.TButton",
            background=[("active", _MONITOR_ON_BG_ACTIVE), ("pressed", _MONITOR_ON_BG_ACTIVE)],
            foreground=[("disabled", "#6A8A74")],
        )
        header = ttk.Frame(self.root, padding=(16, 12))
        header.pack(fill=tk.X)

        header_left = ttk.Frame(header)
        header_left.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self._title_lbl = tk.Label(
            header_left, text="Surfaced",
            bg=_BG, fg=_TEXT_PRI,
            font=(_FONT, 16, "bold"), anchor="w")
        self._title_lbl.pack(anchor=tk.W)
        self._sub_lbl = tk.Label(
            header_left, text="Brand Intelligence Monitor",
            bg=_BG, fg=_TEXT_TER,
            font=(_FONT, 9), anchor="w")
        self._sub_lbl.pack(anchor=tk.W, pady=(2, 0))
        self._mode_lbl = tk.Label(
            header_left, text="模式: 抓取模式",
            bg=_BG, fg=_TEXT_SEC,
            font=(_FONT, 9), anchor="w")
        self._mode_lbl.pack(anchor=tk.W, pady=(6, 0))
        self._home_copy_lbl = tk.Label(
            header_left, text="欢迎回来，主页提醒会显示在这里。",
            bg=_BG, fg=_TEXT_SEC,
            font=(_FONT, 9), anchor="w", justify="left")
        self._home_copy_lbl.pack(anchor=tk.W, pady=(2, 0))

        self._message_bar = tk.Frame(header_left, bg="#FFF7E6",
                                     highlightbackground="#F5C46B", highlightthickness=1)
        self._message_bar.pack(fill=tk.X, pady=(8, 0))
        self._message_bar.pack_forget()
        self._message_label = tk.Label(
            self._message_bar,
            text="",
            bg="#FFF7E6",
            fg=_TEXT_PRI,
            font=(_FONT, 9),
            anchor="w",
            justify="left",
        )
        self._message_label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 8), pady=8)
        self._message_actions = tk.Frame(self._message_bar, bg="#FFF7E6")
        self._message_actions.pack(side=tk.RIGHT, padx=8, pady=6)

        header_right = ttk.Frame(header)
        header_right.pack(side=tk.RIGHT, anchor="n")

        self._status_pill = tk.Label(
            header_right, text="状态: 就绪",
            bg=_BG, fg=_TEXT_SEC,
            font=(_FONT, 9), anchor="e")
        self._status_pill.pack(anchor=tk.E, pady=(0, 8))

        self._recognition_btn = ttk.Button(
            header_right, text="仅启动识别", command=self._start_recognition_only)
        self._recognition_btn.pack(anchor=tk.E, pady=(0, 6))

        self._quick_todo_btn = ttk.Button(
            header_right, text="快速代办", command=self._open_quick_todo_window)
        self._quick_todo_btn.pack(anchor=tk.E, pady=(0, 6))

        self._toggle_btn = ttk.Button(
            header_right, text="监控状态：已关闭", command=self._toggle_monitoring,
            style="MonitorOff.TButton")
        self._toggle_btn.pack(anchor=tk.E)
        self._apply_monitoring_button_state(False)

        ttk.Separator(self.root, orient="horizontal").pack(fill=tk.X)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self._build_quick_todo_tab()
        self._build_dashboard_tab()
        self._build_tasks_tab()
        self._build_status_tab()
        self._build_settings_tab()
        self._build_api_tab()

        if getattr(self, "_should_restore_monitoring_on_launch", False) and should_auto_resume_monitoring():
            self._status_pill.config(text="状态: 正在恢复", fg=_TEXT_SEC, bg=_BG)
            self.root.after(250, self._start_monitoring)

    def _apply_monitoring_button_state(self, is_running: bool):
        if not hasattr(self, "_toggle_btn"):
            return
        if is_running:
            self._toggle_btn.config(
                text="监控状态：已开启",
                style="MonitorOn.TButton",
            )
        else:
            self._toggle_btn.config(
                text="监控状态：已关闭",
                style="MonitorOff.TButton",
            )
        self._build_log_tab()
        self._build_review_tab()
        self._build_diagnostics_tab()
        self._build_report_tab()
        self._bind_config_update_listener()
        self._start_article_watcher()
        self._refresh_mode_notice()
        self.root.after(200, self._rotate_home_copy)

    def _bind_config_update_listener(self):
        if getattr(self, "_config_update_listener_bound", False):
            return
        self._config_update_listener_bound = True
        try:
            self.root.bind_all("<<ConfigUpdated>>", self._on_config_updated, add="+")
        except Exception:
            pass

    def _on_config_updated(self, _event=None):
        try:
            new_config = load_yaml_config(self.config_path)
        except Exception:
            return
        if not new_config:
            return
        self._apply_external_config(new_config)

    # ──────────────────────────────────────────────────────────────────────────
    # Hero 绘制 & 布局
    # ──────────────────────────────────────────────────────────────────────────

    def _redraw_hero(self, event=None):
        return

    def _place_hero_widgets(self):
        return

    def _on_resize(self, event=None):
        return

    def _refresh_mode_notice(self):
        notice = self.mode_notice.strip()
        text = f"模式: {self.current_mode}"
        if notice:
            trimmed = notice if len(notice) <= 34 else notice[:34] + "..."
            text = f"{text} | {trimmed}"
        self._mode_lbl.config(text=text, fg=_WARNING if notice else _TEXT_SEC)
        self._refresh_home_copy_messages()

    def _build_keyword_items(self):
        items = []
        for task in self.config.get('tasks', []):
            if not task.get('enabled', True):
                continue
            name = task.get('name', '')
            keywords = task.get('keywords', [])
            if not keywords and task.get('keyword'):
                if task.get('mode', 'browser') == 'recognition':
                    items.append({"task_name": name, "keyword": task['keyword']})
            else:
                for kw in keywords:
                    if kw.get('mode', 'browser') == 'recognition':
                        kw_str = kw.get('keyword', '')
                        if kw_str:
                            items.append({"task_name": name, "keyword": kw_str})
        return items

    def _get_keyword_guide_state(self):
        if self.recognition_manager and hasattr(self.recognition_manager, "get_keyword_guide_state"):
            try:
                return self.recognition_manager.get_keyword_guide_state() or {}
            except Exception as e:
                print(f"[MainWindow] 读取悬浮窗状态失败: {e}")

        index = self._keyword_guide.get_index() if self._keyword_guide else 0
        return {
            "items": self._build_keyword_items(),
            "index": index,
            "manual_mode": False,
            "action_label": None,
            "action_enabled": None,
            "detail_text": "",
        }

    def _refresh_keyword_guide_ui(self):
        if not self._keyword_guide:
            return
        state = self._get_keyword_guide_state()
        self._keyword_guide.set_items(state.get("items") or [])
        self._keyword_guide.set_index(int(state.get("index", 0) or 0))

        # 使用 state 中的 mode_label，如果没有则回退到旧逻辑
        mode_label = state.get("mode_label")
        if not mode_label:
            mode_label = "手动确认模式" if state.get("manual_mode") else "OCR 自动识别"

        self._keyword_guide.set_display_options(
            mode_label=mode_label,
            controls_visible=state.get("controls_visible", state.get("manual_mode")),
        )
        self._keyword_guide.set_action_state(
            text=state.get("action_label"),
            enabled=state.get("action_enabled"),
            detail=state.get("detail_text", ""),
        )

    def _on_recognition_manual_state_change(self, payload=None):
        self.root.after(0, self._refresh_keyword_guide_ui)

    def _handle_keyword_guide_action(self):
        state = self._get_keyword_guide_state()
        if state.get("manual_mode") and self.recognition_manager:
            try:
                self.recognition_manager.handle_keyword_guide_action()
            except Exception as e:
                print(f"[MainWindow] 悬浮窗人工确认失败: {e}")
        elif self._keyword_guide:
            self._keyword_guide.advance()
            return
        self._refresh_keyword_guide_ui()

    def _start_recognition_mode(self):
        if not (self.recognition_manager and self.recognition_manager.has_recognition_tasks()):
            return
        self.current_mode = "识别模式"
        self.mode_notice = "仅监听启动后的新剪切板截图"
        self._refresh_mode_notice()
        self.recognition_manager.start()
        items = self._get_keyword_guide_state().get("items") or []
        if items:
            if self._keyword_guide:
                self._keyword_guide.destroy()

            state = self._get_keyword_guide_state()
            mode_label = state.get("mode_label")
            if not mode_label:
                mode_label = "手动确认模式" if state.get("manual_mode") else "OCR 自动识别"

            self._keyword_guide = KeywordGuideWindow(
                self.root,
                items,
                on_action=self._handle_keyword_guide_action,
                mode_label=mode_label,
                controls_visible=state.get("controls_visible", state.get("manual_mode")),
            )
            self._refresh_keyword_guide_ui()

    def _stop_recognition_mode(self):
        if self.recognition_manager:
            self.recognition_manager.stop()
        if self._keyword_guide:
            self._keyword_guide.destroy()
            self._keyword_guide = None

    def _on_recognition_batch_ready(self, batch):
        self.root.after(0, self._refresh_keyword_guide_ui)

    def _on_recognition_send_complete(self, batch, ok, reason):
        msg = f"{batch['task_name']} 已发送" if ok else f"{batch['task_name']} 发送失败: {reason or '未知错误'}"
        for brand in batch.get('brands', []):
            self.update_result(batch['task_name'], 'recognition', brand, 1 if ok else 99)
        print(f"[Recognition] {msg}")
        self.root.after(0, lambda m=msg, s=ok: self._show_recognition_result(m, s))

    def _on_recognition_mode_change(self, payload):
        mode = payload.get("mode", "").strip()
        reason = payload.get("reason", "").strip()

        def apply_change():
            if mode == "recognition":
                self.current_mode = "识别模式"
            elif mode == "capture":
                self.current_mode = "抓取模式"
            if reason:
                self.mode_notice = reason
                print(f"[Recognition] 模式切换: {reason}")
            self._refresh_mode_notice()

        self.root.after(0, apply_change)

    def _on_recognition_manual_switch_required(self, payload):
        message = payload.get("message", "").strip() or "识别模式长时间未收到新截图，请手动切换到抓取模式。"

        def show_reminder():
            self.mode_notice = message
            print(f"[Recognition] {message}")
            self._refresh_mode_notice()
            self._show_manual_switch_reminder(message)

        self.root.after(0, show_reminder)

    def _show_recognition_confirm(self, batch):
        if not self.recognition_manager:
            return
        win = tk.Toplevel(self.root)
        win.title("识别模式待确认")
        win.geometry("520x260")
        win.attributes('-topmost', True)
        win.lift()
        win.focus_force()

        tk.Label(win, text="识别模式已达到发送批次", font=("Arial", 13, "bold")).pack(pady=(16, 8))
        current_count = int(batch.get("current_image_count") or len(batch.get("image_paths") or []))
        historical_count = int(batch.get("historical_screenshot_count") or 0)
        total_count = int(batch.get("total_image_count") or len(batch.get("image_paths") or []))
        lines = [
            f"任务组: {batch['task_name']}",
            f"品牌: {', '.join(batch['brands'])}",
            f"截图数: 累计 {total_count} 张（本次 {current_count} 张，历史 {historical_count} 张）",
        ]
        if batch.get('summary'):
            lines.append(f"总结: {batch['summary'][:120]}")
        tk.Label(win, text="\n".join(lines), justify="left", wraplength=460).pack(padx=20, pady=6)

        btns = tk.Frame(win)
        btns.pack(pady=16)

        def confirm(send: bool):
            self.recognition_manager.confirm_batch(batch['id'], send=send)
            if self._keyword_guide:
                self._keyword_guide.advance()
                self._refresh_keyword_guide_ui()
            win.destroy()

        tk.Button(btns, text="确认发送", width=12, command=lambda: confirm(True)).pack(side=tk.LEFT, padx=8)
        tk.Button(btns, text="忽略本批", width=12, command=lambda: confirm(False)).pack(side=tk.LEFT, padx=8)

    def _show_recognition_result(self, message, ok):
        win = tk.Toplevel(self.root)
        win.title("识别模式结果")
        win.geometry("420x150")
        win.attributes('-topmost', True)
        win.lift()
        win.focus_force()
        fg = "#2e7d32" if ok else "#c62828"
        tk.Label(win, text=message, fg=fg, wraplength=360, justify="left").pack(padx=20, pady=28)
        tk.Button(win, text="关闭", width=10, command=win.destroy).pack()

    def _show_manual_switch_reminder(self, message):
        win = tk.Toplevel(self.root)
        win.title("模式切换提醒")
        win.geometry("460x180")
        win.attributes('-topmost', True)
        win.lift()
        win.focus_force()
        tk.Label(win, text="请手动切换模式", font=("Arial", 13, "bold"), fg="#f59e0b").pack(pady=(18, 8))
        tk.Label(win, text=message, wraplength=400, justify="left").pack(padx=20, pady=6)
        tk.Button(win, text="我知道了", width=12, command=win.destroy).pack(pady=14)

    def _build_home_copy_context(self):
        recognition_status = {}
        if self.recognition_manager and hasattr(self.recognition_manager, "get_runtime_status"):
            try:
                recognition_status = self.recognition_manager.get_runtime_status()
            except Exception:
                recognition_status = {}
        return build_home_copy_context(
            config=self.config,
            current_mode=self.current_mode,
            mode_notice=self.mode_notice,
            running=self.running,
            recognition_status=recognition_status,
        )

    def _refresh_home_copy_messages(self):
        context = self._build_home_copy_context()
        self._home_copy_messages = get_home_messages(context)
        self._home_copy_cursor = 0
        self._render_home_copy()

    def _render_home_copy(self):
        context = self._build_home_copy_context()
        message = pick_home_message(context, self._home_copy_cursor)
        trimmed = message if len(message) <= 54 else message[:54] + "..."
        color = _WARNING if "切换" in trimmed or "待确认" in trimmed else _TEXT_SEC
        self._home_copy_lbl.config(text=trimmed, fg=color)

    def _rotate_home_copy(self):
        if not hasattr(self, "_home_copy_lbl") or not self.root.winfo_exists():
            return
        if self._home_copy_messages:
            self._home_copy_cursor = (self._home_copy_cursor + 1) % len(self._home_copy_messages)
        self._render_home_copy()
        self.root.after(8000, self._rotate_home_copy)

    def _message_btn(self, parent, text, command):
        btn = tk.Button(
            parent,
            text=text,
            command=command,
            bg=_SURFACE,
            fg=_TEXT_PRI,
            relief=tk.FLAT,
            padx=10,
            pady=4,
            cursor="hand2",
            activebackground=_SURFACE2,
            activeforeground=_TEXT_PRI,
            font=(_FONT, 8),
        )
        btn.pack(side=tk.LEFT, padx=3)
        return btn

    def _hide_message_banner(self):
        self._message_action_phase = "idle"
        for widget in self._message_actions.winfo_children():
            widget.destroy()
        self._message_label.config(text="")
        self._message_bar.pack_forget()

    def _show_message_banner(self, message: str, actions: list[tuple[str, callable]] | None = None):
        for widget in self._message_actions.winfo_children():
            widget.destroy()
        self._message_label.config(text=message)
        for label, callback in (actions or []):
            self._message_btn(self._message_actions, label, callback)
        if not self._message_bar.winfo_ismapped():
            self._message_bar.pack(fill=tk.X, pady=(8, 0))

    def _confirm_followup_prompt(self):
        self._message_action_phase = "choose_followup"
        if not self.scheduler:
            return
        status = self.scheduler.get_status()
        pending_modes = status.get("pending_followup_modes") or []
        if not pending_modes:
            self._hide_message_banner()
            return
        code_to_label = {
            "browser": "抓取模式",
            "api": "保险模式",
            "recognition": "识别模式",
            "smart": "智能模式",
        }
        actions = []
        for mode in pending_modes:
            actions.append((code_to_label.get(mode, mode), lambda m=mode: self._continue_followup_modes([m])))
        if len(pending_modes) > 1:
            actions.append(("全部继续", lambda modes=list(pending_modes): self._continue_followup_modes(modes)))
        actions.append(("返回", self._restore_followup_prompt))
        self._show_message_banner("请选择要继续执行的后续模式。", actions)

    def _restore_followup_prompt(self):
        if not self.scheduler:
            return
        status = self.scheduler.get_status()
        message = status.get("pending_followup_message") or "默认模式执行后有失败，是否继续后续模式？"
        self._message_action_phase = "confirm_followup"
        self._show_message_banner(message, [
            ("确认", self._confirm_followup_prompt),
            ("跳过", self._skip_followup_modes),
        ])

    def _continue_followup_modes(self, modes: list[str]):
        if self.scheduler:
            self.scheduler.continue_with_modes(modes)
        self._show_message_banner("已确认继续后续模式，系统正在接续执行。", [("关闭", self._hide_message_banner)])

    def _skip_followup_modes(self):
        if self.scheduler:
            self.scheduler.skip_followup_modes()
        self._show_message_banner("已跳过本轮后续模式。", [("关闭", self._hide_message_banner)])

    def _get_default_mode(self) -> str:
        scheduler_cfg = self.config.get("scheduler", {}) or {}
        mode = str(scheduler_cfg.get("default_mode") or "browser").strip()
        return mode if mode in {"browser", "api", "recognition", "smart"} else "browser"

    def _set_default_mode(self, mode_label: str):
        label_to_code = {
            "抓取模式": "browser",
            "保险模式": "api",
            "识别模式": "recognition",
            "智能模式": "smart",
        }
        mode_code = label_to_code.get(mode_label)
        if not mode_code:
            return
        self.config.setdefault("scheduler", {})
        self.config["scheduler"]["default_mode"] = mode_code
        if self.scheduler:
            self.scheduler.config = self.config.get("scheduler", {})
            if hasattr(self.scheduler, "_refresh_config"):
                self.scheduler._refresh_config()
        self.mode_notice = f"默认模式已切换为{mode_label}"
        self._refresh_mode_notice()
        self._refresh_mode_cards()

    def _refresh_mode_cards(self):
        code_to_label = {
            "browser": "抓取模式",
            "api": "保险模式",
            "recognition": "识别模式",
            "smart": "智能模式",
        }
        selected_label = code_to_label.get(self._get_default_mode(), "抓取模式")
        for label, card in self._mode_cards.items():
            is_selected = label == selected_label
            card.configure(
                bg="#E8F2FB" if is_selected else _SURFACE,
                highlightbackground=_ACCENT if is_selected else _BORDER,
                highlightthickness=2 if is_selected else 1,
            )
            name_label, desc_label = self._mode_card_labels.get(label, (None, None))
            if name_label:
                name_label.configure(bg=card.cget("bg"), fg=_ACCENT if is_selected else _TEXT_PRI)
            if desc_label:
                desc_label.configure(bg=card.cget("bg"))

    def _collect_overall_trend_records(self) -> list[dict]:
        try:
            from core.daily_task_state import derive_task_id
            from core.history import get_current_task_names, get_records, get_task_brand_names, is_manual_test_failure_record
        except ImportError:
            return []

        task_names = get_current_task_names(self.config)
        if not task_names:
            return []

        task_lookup: dict[str, dict] = {}
        for task in (self.config or {}).get("tasks", []) or []:
            if not isinstance(task, dict):
                continue
            task_name = str(task.get("name") or "").strip()
            if not task_name:
                task_name = next(iter(get_task_brand_names(task)), "")
            if task_name and task_name not in task_lookup:
                task_lookup[task_name] = task

        records: list[dict] = []
        for task_name in task_names:
            task_cfg = task_lookup.get(task_name, {})
            allowed_brands = get_task_brand_names(task_cfg)
            task_id = str((task_cfg or {}).get("task_id") or derive_task_id(task_cfg or {})).strip() if task_cfg else ""
            task_records = get_records(task_name, task_id=task_id)
            if allowed_brands:
                allowed = set(allowed_brands)
                task_records = [
                    record for record in task_records
                    if str(record.get("brand", "")).strip() in allowed
                ]
            task_records = [record for record in task_records if not is_manual_test_failure_record(record)]
            records.extend(task_records)
        return records

    def _build_overall_trend_series(self, days: int) -> dict | None:
        try:
            from core.daily_task_state import derive_task_id
            from core.history import get_brand_trend_series, get_current_task_names, get_task_brand_names
        except ImportError:
            return None

        task_names = get_current_task_names(self.config)
        if not task_names:
            return None

        days = max(7, int(days or 30))
        task_lookup: dict[str, dict] = {}
        for task in (self.config or {}).get("tasks", []) or []:
            if not isinstance(task, dict):
                continue
            task_name = str(task.get("name") or "").strip()
            if not task_name:
                task_name = next(iter(get_task_brand_names(task)), "")
            if task_name and task_name not in task_lookup:
                task_lookup[task_name] = task

        actual_map: dict[str, list[float]] = {}
        predicted_map: dict[str, list[float]] = {}
        for task_name in task_names:
            task = task_lookup.get(task_name, {})
            try:
                series = get_brand_trend_series(
                    task_name,
                    get_task_brand_names(task),
                    days,
                    task_id=str((task or {}).get("task_id") or derive_task_id(task or {})).strip() if task else "",
                    task_created_at=str((task or {}).get("created_at") or "").strip() if task else "",
                )
            except Exception:
                series = None
            if not series:
                continue
            dates = list(series.get("dates") or [])
            actual = list(series.get("actual") or [])
            predicted = list(series.get("predicted") or [])
            for idx, current_date in enumerate(dates):
                ds = getattr(current_date, "isoformat", lambda: str(current_date))()
                if idx < len(actual) and actual[idx] is not None:
                    actual_map.setdefault(ds, []).append(float(actual[idx]))
                if idx < len(predicted) and predicted[idx] is not None:
                    predicted_map.setdefault(ds, []).append(float(predicted[idx]))

        all_dates = sorted(set(actual_map.keys()) | set(predicted_map.keys()))
        if not all_dates:
            return None

        from datetime import datetime

        date_list = [datetime.strptime(ds, "%Y-%m-%d").date() for ds in all_dates]
        actual_values = [
            round(sum(actual_map[ds]) / len(actual_map[ds]), 1) if actual_map.get(ds) else None
            for ds in all_dates
        ]
        predicted_values = [
            round(sum(predicted_map[ds]) / len(predicted_map[ds]), 1)
            if predicted_map.get(ds)
            else round(sum(actual_map[ds]) / len(actual_map[ds]), 1)
            for ds in all_dates
        ]

        valid_values = [value for value in actual_values if value is not None]
        if not valid_values:
            return None

        current_value = valid_values[-1]
        previous_value = valid_values[-2] if len(valid_values) >= 2 else None

        peak_value = round(max(valid_values), 1)
        average_value = round(sum(valid_values) / len(valid_values), 1)
        delta_value = round(current_value - previous_value, 1) if previous_value is not None else 0.0

        return {
            "dates": date_list,
            "actual": actual_values,
            "predicted": predicted_values,
            "summary": {
                "current": round(current_value, 1),
                "delta": delta_value,
                "peak": peak_value,
                "average": average_value,
            },
        }

    def _draw_overall_trend_chart(self, canvas, series: dict | None, range_label: str):
        from datetime import date

        canvas.delete("all")
        W = canvas.winfo_width() or 820
        H = canvas.winfo_height() or 240
        if not series:
            canvas.create_text(W // 2, H // 2, text="当前没有可展示的总体趋势", font=(_FONT, 12), fill=_TEXT_TER)
            return

        dates = series["dates"]
        actual = series["actual"]
        predicted = series["predicted"]
        summary = series["summary"]
        n = len(dates)
        if n <= 1:
            canvas.create_text(W // 2, H // 2, text="数据不足，先运行几次任务再看趋势", font=(_FONT, 12), fill=_TEXT_TER)
            return

        pad_l, pad_r, pad_t, pad_b = 50, 24, 24, 46
        chart_h = max(80, H - pad_t - pad_b)
        chart_w = max(100, W - pad_l - pad_r)

        def cx(i: int) -> float:
            return pad_l + (i / max(n - 1, 1)) * chart_w

        def cy(value: float) -> float:
            return pad_t + (1 - value / 100) * chart_h

        # 网格
        for pct, lbl in ((100, "100%"), (75, "75%"), (50, "50%"), (25, "25%"), (0, "0%")):
            y = cy(pct)
            canvas.create_line(pad_l, y, W - pad_r, y, fill="#E7EBF2", dash=(3, 4))
            if pct in (100, 50, 0):
                canvas.create_text(pad_l - 6, y, text=lbl, anchor="e", font=(_FONT, 8), fill=_TEXT_TER)

        # 预测区间填充，先填一层柔和背景
        predicted_points = []
        for i, value in enumerate(predicted):
            predicted_points.extend([cx(i), cy(value)])
        if len(predicted_points) >= 4:
            area = [predicted_points[0], H - pad_b] + predicted_points + [predicted_points[-2], H - pad_b]
            canvas.create_polygon(area, fill=_TREND_FILL, outline="")

        # 预测值：平滑虚线
        canvas.create_line(
            *predicted_points,
            smooth=True,
            splinesteps=36,
            fill=_TREND_PRED,
            width=2,
            dash=(5, 4),
        )

        # 实际值：分段平滑曲线，避免缺值直接拉线
        segments: list[list[float]] = []
        current_segment: list[float] = []
        for i, value in enumerate(actual):
            if value is None:
                if len(current_segment) >= 4:
                    segments.append(current_segment[:])
                current_segment = []
                continue
            current_segment.extend([cx(i), cy(value)])
        if len(current_segment) >= 4:
            segments.append(current_segment[:])

        for segment in segments:
            canvas.create_line(
                *segment,
                smooth=True,
                splinesteps=36,
                fill=_TREND_ACTUAL,
                width=3,
            )

        # 实际点
        for i, value in enumerate(actual):
            if value is None:
                continue
            x, y = cx(i), cy(value)
            canvas.create_oval(x - 3.5, y - 3.5, x + 3.5, y + 3.5, fill=_TREND_ACTUAL, outline="white", width=1)

        # 最后一个点强调
        last_idx = next((idx for idx in range(n - 1, -1, -1) if actual[idx] is not None), None)
        if last_idx is not None:
            x, y = cx(last_idx), cy(actual[last_idx])
            canvas.create_oval(x - 5, y - 5, x + 5, y + 5, outline=_TREND_ACTUAL, width=2)

        # X 轴标签
        if range_label == "周":
            label_step = 1
        elif range_label == "月":
            label_step = max(1, n // 6)
        else:
            label_step = max(1, n // 12)

        for i, d in enumerate(dates):
            if i % label_step != 0 and i != n - 1:
                continue
            label = d.strftime("%a") if range_label == "周" else d.strftime("%m-%d")
            if range_label == "周":
                label_map = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                label = label_map[d.weekday()]
            canvas.create_text(cx(i), H - pad_b + 14, text=label, font=(_FONT, 8), fill=_TEXT_TER)

        # 统计说明
        canvas.create_text(
            pad_l,
            H - 10,
            text=f"共 {n} 天  ·  峰值 {summary['peak']}%  ·  均值 {summary['average']}%",
            anchor="w",
            font=(_FONT, 8),
            fill=_TEXT_TER,
        )

    def _refresh_overview_trend(self):
        redraw = getattr(self, "_overview_trend_redraw", None)
        if callable(redraw):
            redraw()

    def _refresh_brand_industry_source(self):
        redraw = getattr(self, "_industry_source_redraw", None)
        if callable(redraw):
            redraw()

    def _refresh_publication_media(self):
        redraw = getattr(self, "_publication_media_redraw", None)
        if callable(redraw):
            redraw()

    def _refresh_region_distribution(self):
        redraw = getattr(self, "_region_distribution_redraw", None)
        if callable(redraw):
            redraw()

    def _start_article_watcher(self):
        if getattr(self, "_article_poll_started", False):
            return
        self._article_poll_started = True
        if not hasattr(self, "_article_last_mtime"):
            self._article_last_mtime = None
        self._schedule_article_poll()

    def _schedule_article_poll(self):
        try:
            self.root.after(self._article_poll_interval_ms, self._poll_article_file)
        except Exception:
            pass

    def _poll_article_file(self):
        try:
            article_path = resolve_app_path("logs/articles.json")
            try:
                mtime = article_path.stat().st_mtime if article_path.exists() else None
            except Exception:
                mtime = None
            if mtime != getattr(self, "_article_last_mtime", None):
                self._article_last_mtime = mtime
                self._refresh_publication_media()
                self._refresh_tasks()
        finally:
            self._schedule_article_poll()

    # ──────────────────────────────────────────────────────────────────────────
    # Tab 1: 快速代办
    # ──────────────────────────────────────────────────────────────────────────

    def _build_quick_todo_tab(self):
        frame = tk.Frame(self.notebook, bg=_BG)
        self.notebook.add(frame, text="  代办  ")

        top = tk.Frame(frame, bg=_BG)
        top.pack(fill=tk.X, padx=24, pady=(24, 0))
        tk.Label(
            top,
            text="快速代办",
            bg=_BG,
            fg=_TEXT_PRI,
            font=(_FONT, 15, "bold"),
            anchor="w",
        ).pack(anchor=tk.W)
        tk.Label(
            top,
            text="输入新任务后按回车添加，点击左侧方框即可标记完成。",
            bg=_BG,
            fg=_TEXT_TER,
            font=(_FONT, 9),
            anchor="w",
        ).pack(anchor=tk.W, pady=(4, 0))

        self._build_quick_todo_panel(frame)

    # ──────────────────────────────────────────────────────────────────────────
    # 共用工具
    # ──────────────────────────────────────────────────────────────────────────

    def _toolbar_btn(self, parent, text, cmd, accent=False):
        b = ttk.Button(parent, text=text, command=cmd)
        b.pack(side=tk.LEFT, padx=(0, 8), pady=2)
        return b

    def _section_label(self, parent, text):
        """简单区块标题。"""
        tk.Label(parent, text=text.upper(),
                 bg=_BG, fg=_TEXT_TER,
                 font=(_FONT, 9, "bold")).pack(anchor=tk.W, pady=(0, 6))

    def _get_quick_todos(self, *, persist_if_changed: bool = False) -> list[dict]:
        items = self.config.setdefault("quick_todos", [])
        normalized, changed = normalize_quick_todos(items)
        if changed or normalized != items:
            self.config["quick_todos"] = normalized
            if persist_if_changed:
                self._persist_quick_todos()
        return normalized

    def _persist_quick_todos(self):
        try:
            persist_config(
                self.config,
                self.config_path,
                ensure_task_ids=True,
                on_config_change=lambda cfg: self._apply_local_config_change(cfg, reload_runtime=False),
                event_root=self.root,
            )
        except Exception as e:
            messagebox.showerror("错误", f"保存代办失败: {e}")

    def _open_quick_todo_window(self):
        QuickTodoWindow(
            self.root,
            get_config=lambda: self.config,
            save_config=self._save_quick_todo_config,
        )

    def _save_quick_todo_config(self, new_config: dict):
        persist_config(
            new_config,
            self.config_path,
            ensure_task_ids=True,
            on_config_change=lambda cfg: self._apply_local_config_change(cfg, reload_runtime=False),
            event_root=self.root,
        )

    def _build_quick_todo_panel(self, parent):
        panel = tk.Frame(parent, bg=_SURFACE, highlightbackground=_BORDER, highlightthickness=1)
        panel.pack(fill=tk.X, padx=24, pady=(12, 0))

        pad = tk.Frame(panel, bg=_SURFACE)
        pad.pack(fill=tk.X, padx=18, pady=16)

        title_row = tk.Frame(pad, bg=_SURFACE)
        title_row.pack(fill=tk.X)
        tk.Label(
            title_row,
            text="快速代办",
            bg=_SURFACE,
            fg=_TEXT_PRI,
            font=(_FONT, 11, "bold"),
            anchor="w",
        ).pack(side=tk.LEFT)
        tk.Label(
            title_row,
            text="输入新任务后按回车添加，点左侧方框即可标记完成。",
            bg=_SURFACE,
            fg=_TEXT_TER,
            font=(_FONT, 8),
            anchor="e",
        ).pack(side=tk.RIGHT)

        input_row = tk.Frame(pad, bg=_SURFACE)
        input_row.pack(fill=tk.X, pady=(12, 10))
        self._quick_todo_var = tk.StringVar()
        entry = ttk.Entry(input_row, textvariable=self._quick_todo_var)
        entry.pack(fill=tk.X)
        entry.bind("<Return>", self._on_quick_todo_submit)
        self._quick_todo_entry = entry

        self._quick_todo_list = tk.Frame(pad, bg=_SURFACE)
        self._quick_todo_list.pack(fill=tk.X)
        self._refresh_quick_todo_panel()

    def _refresh_quick_todo_panel(self):
        if not hasattr(self, "_quick_todo_list"):
            return

        for widget in self._quick_todo_list.winfo_children():
            widget.destroy()

        todos = self._get_quick_todos(persist_if_changed=True)
        if not todos:
            tk.Label(
                self._quick_todo_list,
                text="还没有待办，先记下一件最重要的小事。",
                bg=_SURFACE,
                fg=_TEXT_TER,
                font=(_FONT, 9),
                anchor="w",
            ).pack(anchor=tk.W, pady=(4, 2))
            return

        for index, item in enumerate(todos):
            done = bool(item.get("done"))
            row = tk.Frame(self._quick_todo_list, bg=_SURFACE)
            row.pack(fill=tk.X, pady=3)

            toggle = tk.Canvas(
                row,
                width=18,
                height=18,
                bg=_SURFACE,
                highlightthickness=0,
                cursor="hand2",
            )
            toggle.pack(side=tk.LEFT, padx=(0, 10), pady=1)
            outline = "#9CA3AF" if done else _TEXT_SEC
            fill = "#DDE5DB" if done else _SURFACE
            toggle.create_rectangle(2, 2, 16, 16, outline=outline, fill=fill, width=1)
            if done:
                toggle.create_line(5, 9, 8, 12, 13, 6, fill=_SUCCESS, width=2)

            label = tk.Label(
                row,
                text=item["text"],
                bg=_SURFACE,
                fg="#8A8A8A" if done else _TEXT_PRI,
                font=self._todo_done_font if done else self._todo_font,
                anchor="w",
                justify="left",
                wraplength=760,
                cursor="hand2",
            )
            label.pack(side=tk.LEFT, fill=tk.X, expand=True)

            toggle.bind("<Button-1>", lambda e, idx=index: self._toggle_quick_todo(idx))
            label.bind("<Button-1>", lambda e, idx=index: self._toggle_quick_todo(idx))
            for widget in (row, toggle, label):
                widget.bind("<Button-3>", lambda e, idx=index: self._show_quick_todo_menu(e, idx))
                widget.bind("<Control-Button-1>", lambda e, idx=index: self._show_quick_todo_menu(e, idx))

    def _on_quick_todo_submit(self, event=None):
        self._add_quick_todo()
        return "break"

    def _add_quick_todo(self):
        if not hasattr(self, "_quick_todo_var"):
            return
        text = self._quick_todo_var.get().strip()
        if not text:
            return
        todos = self._get_quick_todos()
        todos.insert(0, {"text": text, "done": False})
        self.config["quick_todos"] = todos
        self._quick_todo_var.set("")
        self._persist_quick_todos()
        self._refresh_quick_todo_panel()
        if hasattr(self, "_quick_todo_entry"):
            self._quick_todo_entry.focus_set()

    def _toggle_quick_todo(self, index: int):
        todos = self._get_quick_todos()
        if not (0 <= index < len(todos)):
            return
        is_done = not bool(todos[index].get("done"))
        todos[index]["done"] = is_done
        if is_done:
            todos[index]["completed_at"] = ""
        else:
            todos[index].pop("completed_at", None)
        self.config["quick_todos"] = todos
        self._persist_quick_todos()
        self._refresh_quick_todo_panel()

    def _show_quick_todo_menu(self, event, index: int):
        todos = self._get_quick_todos()
        if not (0 <= index < len(todos)):
            return
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="清除", command=lambda idx=index: self._remove_quick_todo(idx))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _remove_quick_todo(self, index: int):
        todos = self._get_quick_todos()
        if not (0 <= index < len(todos)):
            return
        todos.pop(index)
        self.config["quick_todos"] = todos
        self._persist_quick_todos()
        self._refresh_quick_todo_panel()

    # ──────────────────────────────────────────────────────────────────────────
    # Tab 1: 看板
    # ──────────────────────────────────────────────────────────────────────────

    def _build_dashboard_tab(self):
        frame = tk.Frame(self.notebook, bg=_BG)
        self.notebook.add(frame, text="  看板  ")
        self._dashboard_tab_frame = frame
        self._dashboard_scroll_root = frame

        canvas = tk.Canvas(frame, bg=_BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=_BG, padx=0, pady=0)

        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        inner_win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(inner_win, width=e.width))

        def _dashboard_mousewheel(event):
            widget = getattr(event, "widget", None)
            root = getattr(self, "_dashboard_scroll_root", None)
            while widget is not None:
                if widget is root:
                    return scroll_canvas_on_mousewheel(canvas, event)
                widget = getattr(widget, "master", None)
            return None

        if not getattr(self, "_dashboard_mousewheel_bound", False):
            self._dashboard_mousewheel_bound = True
            self.root.bind_all("<MouseWheel>", _dashboard_mousewheel, add="+")
            self.root.bind_all("<Button-4>", _dashboard_mousewheel, add="+")
            self.root.bind_all("<Button-5>", _dashboard_mousewheel, add="+")

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        hero = tk.Frame(inner, bg=_BG)
        hero.pack(fill=tk.X, padx=24, pady=(20, 10))

        tk.Label(
            hero,
            text="品牌看板",
            bg=_BG,
            fg=_TEXT_PRI,
            font=(_FONT, 16, "bold"),
            anchor="w",
        ).pack(anchor=tk.W)
        tk.Label(
            hero,
            text="这里切换默认模式；品牌列表已移到「品牌」页。",
            bg=_BG,
            fg=_TEXT_SEC,
            font=(_FONT, 9),
            anchor="w",
            justify="left",
        ).pack(anchor=tk.W, pady=(2, 0))

        cards = tk.Frame(inner, bg=_BG)
        cards.pack(fill=tk.X, padx=24, pady=(0, 12))

        for name, desc in FORMAL_MODES:
            card = tk.Frame(cards, bg=_SURFACE, highlightbackground=_BORDER, highlightthickness=1)
            card.pack(fill=tk.X, pady=(0, 6))
            name_label = tk.Label(card, text=name, bg=_SURFACE, fg=_TEXT_PRI,
                                  font=(_FONT, 9, "bold"), anchor="w")
            name_label.pack(anchor=tk.W, padx=12, pady=(8, 2))
            desc_label = tk.Label(card, text=desc, bg=_SURFACE, fg=_TEXT_SEC,
                                  font=(_FONT, 8), anchor="w", justify="left", wraplength=900)
            desc_label.pack(anchor=tk.W, padx=12, pady=(0, 8))
            self._mode_cards[name] = card
            self._mode_card_labels[name] = (name_label, desc_label)
            for widget in (card, name_label, desc_label):
                widget.bind("<Button-1>", lambda e, mode_name=name: self._set_default_mode(mode_name))
                widget.configure(cursor="hand2")
        self._refresh_mode_cards()

        self._build_brand_industry_source_card(inner)
        self._build_publication_media_card(inner)
        self._build_region_distribution_card(inner)
        self._build_overview_trend_card(inner)

        hint = tk.Label(
            inner,
            text="点击任意模式卡片即可切换默认模式。",
            bg=_BG,
            fg=_TEXT_TER,
            font=(_FONT, 8),
            anchor="w",
            justify="left",
        )
        hint.pack(anchor=tk.W, padx=24, pady=(0, 18))

    def _build_tasks_tab(self):
        frame = tk.Frame(self.notebook, bg=_BG)
        self.notebook.add(frame, text="  品牌  ")
        self._tasks_tab_frame = frame

        # 工具栏
        toolbar = tk.Frame(frame, bg=_BG)
        toolbar.pack(fill=tk.X, padx=24, pady=(20, 0))
        self._toolbar_btn(toolbar, "+ 添加品牌", self._add_task, accent=True)
        self._toolbar_btn(toolbar, "保存配置", self._save_config)
        self._toolbar_btn(toolbar, "搜搜", self._open_ai_assistant)

        # 列表头（瑞士风格：小号全大写标签）
        hdr = tk.Frame(frame, bg=_BG)
        hdr.pack(fill=tk.X, padx=24, pady=(12, 6))
        for text, w in [("状态", 7), ("品牌", 20), ("词 / 次", 9),
                        ("文章总数", 8), ("运行时间", 16), ("Webhook", 22), ("", 10)]:
            tk.Label(hdr, text=text.upper(), bg=_BG, fg=_TEXT_TER,
                     font=(_FONT, 7, "bold"), width=w, anchor="w").pack(side=tk.LEFT, padx=2)

        tk.Frame(frame, bg=_BORDER, height=1).pack(fill=tk.X, padx=24)

        # 滚动列表
        list_wrap = tk.Frame(frame, bg=_BG)
        list_wrap.pack(fill=tk.BOTH, expand=True, padx=24, pady=(8, 20))

        sb = ttk.Scrollbar(list_wrap)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        self._tasks_canvas = tk.Canvas(list_wrap, yscrollcommand=sb.set,
                                       bg=_BG, highlightthickness=0)
        self._tasks_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.config(command=self._tasks_canvas.yview)

        self._tasks_inner = tk.Frame(self._tasks_canvas, bg=_BG)
        self._tasks_win = self._tasks_canvas.create_window(
            (0, 0), window=self._tasks_inner, anchor="nw")

        self._tasks_canvas.bind("<Configure>",
            lambda e: self._tasks_canvas.itemconfig(self._tasks_win, width=e.width))
        self._tasks_inner.bind("<Configure>",
            lambda e: self._tasks_canvas.configure(
                scrollregion=self._tasks_canvas.bbox("all")))
        self._tasks_canvas.bind("<MouseWheel>",
            lambda e: scroll_canvas_on_mousewheel(self._tasks_canvas, e))

        self._refresh_tasks()

    def _refresh_tasks(self):
        for w in self._tasks_inner.winfo_children():
            w.destroy()

        tasks = self.config.get('tasks', [])
        task_names = [str(task.get('name', '') or '').strip() for task in tasks if str(task.get('name', '') or '').strip()]
        try:
            article_counts = get_task_article_counts(task_names) if task_names else {}
        except Exception:
            article_counts = {}
        for i, task in enumerate(tasks):
            keywords = task.get('keywords', [])
            if not keywords and task.get('keyword'):
                keywords = [{'keyword': task['keyword'],
                             'brand': task.get('brand',''),
                             'platforms': [task.get('platform','')]}]
            default_brand = keywords[0].get('brand','') if keywords else ''
            task_name = str(task.get('name', '') or '').strip()
            brand_page = _task_brand_page(task) or task_name or default_brand or "未命名品牌页"
            tag_summary = _task_tag_summary(task)
            article_total = int(article_counts.get(task_name, 0) or 0) if task_name else 0

            # 状态
            if not task.get('enabled', True):
                dot_color, status_text = _TEXT_TER, "暂停"
            else:
                found = [self.last_results.get(
                             (task_name, p, kw.get('brand') or default_brand))
                         for kw in keywords for p in kw.get('platforms', [])]
                recognition_brands = list(dict.fromkeys(
                    (kw.get('brand') or default_brand)
                    for kw in keywords
                    if kw.get('mode', 'browser') == 'recognition' and (kw.get('brand') or default_brand)
                ))
                found.extend(
                    self.last_results.get((task_name, 'recognition', brand))
                    for brand in recognition_brands
                )
                found = [r for r in found if r is not None]
                if found:
                    best = min(found)
                    dot_color  = _SUCCESS if best != 99 else _DANGER
                    status_text = "已提及" if best != 99 else "未检测"
                else:
                    dot_color, status_text = _TEXT_TER, "待运行"

            # 卡片
            card = tk.Frame(self._tasks_inner, bg=_SURFACE,
                            highlightbackground=_BORDER,
                            highlightthickness=1)
            card.pack(fill=tk.X, pady=(0, 4))

            row = tk.Frame(card, bg=_SURFACE)
            row.pack(fill=tk.X, padx=14, pady=11)

            # 状态点 + 文字
            dot_frame = tk.Frame(row, bg=_SURFACE, width=56)
            dot_frame.pack(side=tk.LEFT, padx=(0, 4))
            dot_frame.pack_propagate(False)
            c_dot = tk.Canvas(dot_frame, width=8, height=8,
                              bg=_SURFACE, highlightthickness=0)
            c_dot.pack(side=tk.LEFT, padx=(0, 4), pady=2)
            c_dot.create_oval(1, 1, 7, 7, fill=dot_color, outline="")
            tk.Label(dot_frame, text=status_text, bg=_SURFACE, fg=dot_color,
                     font=(_FONT, 8)).pack(side=tk.LEFT)

            # 品牌页面信息
            title_frame = tk.Frame(row, bg=_SURFACE, width=160)
            title_frame.pack(side=tk.LEFT, padx=(0, 8))
            title_frame.pack_propagate(False)
            tk.Label(title_frame, text=brand_page, bg=_SURFACE, fg=_TEXT_PRI,
                     font=(_FONT, 11, "bold"), anchor="w", justify="left").pack(anchor="w")
            title_count_label = tk.Label(
                title_frame,
                text=f"文章总数：{article_total} 篇",
                bg=_SURFACE,
                fg=_ACCENT,
                font=(_FONT, 8, "bold"),
                anchor="w",
                justify="left",
                cursor="hand2",
            )
            title_count_label.pack(anchor="w", pady=(1, 0))
            if task_name and task_name != brand_page:
                tk.Label(title_frame, text=f"品牌组：{task_name}", bg=_SURFACE, fg=_TEXT_SEC,
                         font=(_FONT, 8), anchor="w", justify="left").pack(anchor="w", pady=(1, 0))
            if tag_summary:
                tk.Label(title_frame, text=tag_summary, bg=_SURFACE, fg=_TEXT_TER,
                         font=(_FONT, 8), anchor="w", justify="left", wraplength=240).pack(anchor="w", pady=(2, 0))

            def _open_articles(_event=None, task_name=task_name):
                if task_name:
                    ArticleWindow(self.root, self.config, task_name=task_name)
                return "break"

            # 词/次（次要）
            total = sum(len(kw.get('platforms',[])) for kw in keywords)
            tk.Label(row, text=f"{len(keywords)} / {total}",
                     bg=_SURFACE, fg=_TEXT_SEC,
                     font=(_FONT, 9), width=8, anchor="w").pack(side=tk.LEFT, padx=(0,8))

            # 总发表文章数
            article_label = tk.Label(
                row,
                text=f"{article_total} 篇",
                bg=_SURFACE,
                fg=_ACCENT,
                font=(_FONT, 9, "bold"),
                width=8,
                anchor="w",
                cursor="hand2",
            )
            article_label.pack(side=tk.LEFT, padx=(0,8))
            article_label.bind("<Button-1>", _open_articles)
            title_count_label.bind("<Button-1>", _open_articles)

            # 时间
            time_str = describe_task_schedule(task, self.config.get('scheduler', {}))
            tk.Label(row, text=time_str, bg=_SURFACE, fg=_TEXT_SEC,
                     font=(_FONT, 9), width=16, anchor="w").pack(side=tk.LEFT, padx=(0,8))

            # Webhook
            wh = task.get('webhook_url','')
            wh_short = wh[:24]+'…' if len(wh) > 24 else wh
            tk.Label(row, text=wh_short, bg=_SURFACE, fg=_TEXT_TER,
                     font=(_FONT, 8), width=24, anchor="w").pack(side=tk.LEFT, padx=(0,8))

            # 操作
            from functools import partial
            btn_f = tk.Frame(row, bg=_SURFACE)
            btn_f.pack(side=tk.RIGHT)

            def _sb(parent, text, cmd, fg=_TEXT_SEC):
                tk.Button(parent, text=text, command=cmd,
                          bg=_SURFACE, fg=fg, font=(_FONT, 8),
                          relief=tk.FLAT, padx=8, pady=3,
                          cursor="hand2",
                          activebackground=_SURFACE2,
                          activeforeground=_TEXT_PRI).pack(side=tk.LEFT, padx=2)

            _sb(btn_f, "编辑", partial(self._edit_task, i))
            _sb(btn_f, "删除", partial(self._delete_task, i), fg=_DANGER)

            card.bind("<Button-1>", lambda e, idx=i: self._edit_task(idx))
            row.bind("<Button-1>",  lambda e, idx=i: self._edit_task(idx))

            # 超时警告条
            tn_key = task.get('name','')
            if tn_key in self._timeout_warnings:
                wd = self._timeout_warnings[tn_key]
                warn = tk.Frame(card, bg="#1C1200")
                warn.pack(fill=tk.X)
                tk.Label(warn,
                         text=f"  ⚠  任务「{tn_key}」已运行 {wd['elapsed']} 分钟未结束   {wd['kw_info']}",
                         bg="#1C1200", fg=_WARNING, font=(_FONT, 8),
                         anchor="w").pack(side=tk.LEFT, pady=5)
                tk.Button(warn, text="×", bg="#1C1200", fg=_WARNING,
                          relief=tk.FLAT, font=(_FONT, 9), cursor="hand2",
                          command=lambda t=tn_key: (
                              self._timeout_warnings.pop(t, None),
                              self._refresh_tasks()
                          )).pack(side=tk.RIGHT, padx=10)

        if not tasks:
            tk.Label(self._tasks_inner,
                     text="暂无品牌 — 点击「+ 添加品牌」开始",
                     bg=_BG, fg=_TEXT_TER, font=(_FONT, 11)).pack(pady=60)

        self._tasks_inner.update_idletasks()
        self._tasks_canvas.configure(scrollregion=self._tasks_canvas.bbox("all"))
        bind_mousewheel_recursive(
            self._tasks_inner,
            lambda e: scroll_canvas_on_mousewheel(self._tasks_canvas, e),
        )

    def _build_overview_trend_card(self, parent):
        card = tk.Frame(parent, bg=_SURFACE, highlightbackground=_BORDER, highlightthickness=1)
        card.pack(fill=tk.X, padx=24, pady=(0, 18))

        top = tk.Frame(card, bg=_SURFACE)
        top.pack(fill=tk.X, padx=16, pady=(14, 8))

        title_box = tk.Frame(top, bg=_SURFACE)
        title_box.pack(side=tk.LEFT, anchor="w")
        tk.Label(
            title_box,
            text="AI 辅助优化趋势",
            bg=_SURFACE,
            fg=_TEXT_PRI,
            font=(_FONT, 13, "bold"),
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            title_box,
            text="汇总所有当前品牌的每日提及率，虚线为平滑预测值。",
            bg=_SURFACE,
            fg=_TEXT_TER,
            font=(_FONT, 8),
            anchor="w",
        ).pack(anchor="w", pady=(2, 0))

        control_box = tk.Frame(top, bg=_SURFACE)
        control_box.pack(side=tk.RIGHT, anchor="e")

        range_var = tk.StringVar(value="周")
        for label in ("周", "月", "年"):
            btn = tk.Radiobutton(
                control_box,
                text=label,
                variable=range_var,
                value=label,
                indicatoron=False,
                bg=_SURFACE2,
                fg=_TEXT_PRI,
                selectcolor=_SURFACE,
                activebackground=_BORDER2,
                activeforeground=_TEXT_PRI,
                relief=tk.FLAT,
                width=5,
                font=(_FONT, 9, "bold"),
                padx=8,
                pady=6,
                command=lambda: redraw(),
            )
            btn.pack(side=tk.LEFT, padx=(0, 8))

        legend = tk.Frame(control_box, bg=_SURFACE)
        legend.pack(side=tk.LEFT, padx=(6, 0))
        for color, text, dash in (
            (_TREND_PRED, "预测值", True),
            (_TREND_ACTUAL, "实际值", False),
        ):
            item = tk.Frame(legend, bg=_SURFACE)
            item.pack(side=tk.LEFT, padx=(0, 10))
            dot = tk.Canvas(item, width=14, height=14, bg=_SURFACE, highlightthickness=0)
            dot.pack(side=tk.LEFT)
            if dash:
                dot.create_line(1, 7, 13, 7, fill=color, width=3, dash=(4, 3))
            else:
                dot.create_oval(3, 3, 11, 11, fill=color, outline="")
            tk.Label(item, text=text, bg=_SURFACE, fg=_TEXT_SEC, font=(_FONT, 8, "bold")).pack(side=tk.LEFT, padx=(2, 0))

        summary = tk.Frame(card, bg=_SURFACE)
        summary.pack(fill=tk.X, padx=16, pady=(0, 10))

        left_summary = tk.Frame(summary, bg=_SURFACE)
        left_summary.pack(side=tk.LEFT, anchor="w")
        current_value_lbl = tk.Label(
            left_summary,
            text="--%",
            bg=_SURFACE,
            fg=_TEXT_PRI,
            font=(_FONT, 30, "bold"),
            anchor="w",
        )
        current_value_lbl.pack(anchor="w")
        delta_value_lbl = tk.Label(
            left_summary,
            text="",
            bg=_SURFACE,
            fg=_SUCCESS,
            font=(_FONT, 11, "bold"),
            anchor="w",
        )
        delta_value_lbl.pack(anchor="w", pady=(2, 0))

        divider = tk.Frame(summary, bg=_BORDER, width=1, height=54)
        divider.pack(side=tk.LEFT, padx=18, pady=2)

        stats_box = tk.Frame(summary, bg=_SURFACE)
        stats_box.pack(side=tk.LEFT, anchor="w")

        stat_peak = tk.Frame(stats_box, bg=_SURFACE)
        stat_peak.pack(side=tk.LEFT, padx=(0, 22))
        tk.Label(stat_peak, text="峰值", bg=_SURFACE, fg=_TEXT_TER, font=(_FONT, 8, "bold")).pack(anchor="w")
        peak_value_lbl = tk.Label(stat_peak, text="--%", bg=_SURFACE, fg=_TEXT_PRI, font=(_FONT, 18, "bold"))
        peak_value_lbl.pack(anchor="w")

        stat_avg = tk.Frame(stats_box, bg=_SURFACE)
        stat_avg.pack(side=tk.LEFT)
        tk.Label(stat_avg, text="均值", bg=_SURFACE, fg=_TEXT_TER, font=(_FONT, 8, "bold")).pack(anchor="w")
        avg_value_lbl = tk.Label(stat_avg, text="--%", bg=_SURFACE, fg=_TEXT_PRI, font=(_FONT, 18, "bold"))
        avg_value_lbl.pack(anchor="w")

        canvas = tk.Canvas(card, bg=_SURFACE, highlightthickness=0, height=240)
        canvas.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 12))

        def redraw():
            period_label = range_var.get()
            days = {"周": 7, "月": 30, "年": 365}.get(period_label, 30)
            series = self._build_overall_trend_series(days)
            if series:
                summary_data = series["summary"]
                current_value_lbl.config(text=f"{summary_data['current']:g}%")
                delta = summary_data["delta"]
                if delta > 0:
                    delta_value_lbl.config(text=f"↗ {abs(delta):g}%", fg=_SUCCESS)
                elif delta < 0:
                    delta_value_lbl.config(text=f"↘ {abs(delta):g}%", fg=_DANGER)
                else:
                    delta_value_lbl.config(text="→ 0%", fg=_TEXT_TER)
                peak_value_lbl.config(text=f"{summary_data['peak']:g}%")
                avg_value_lbl.config(text=f"{summary_data['average']:g}%")
            else:
                current_value_lbl.config(text="--%")
                delta_value_lbl.config(text="", fg=_TEXT_TER)
                peak_value_lbl.config(text="--%")
                avg_value_lbl.config(text="--%")
            self._draw_overall_trend_chart(canvas, series, period_label)

        self._overview_trend_redraw = redraw
        redraw()

    def _build_brand_industry_source_card(self, parent):
        card = tk.Frame(parent, bg=_SURFACE, highlightbackground=_BORDER, highlightthickness=1)
        card.pack(fill=tk.X, padx=24, pady=(0, 18))

        top = tk.Frame(card, bg=_SURFACE)
        top.pack(fill=tk.X, padx=16, pady=(14, 8))

        title_box = tk.Frame(top, bg=_SURFACE)
        title_box.pack(anchor="w")
        tk.Label(
            title_box,
            text="品牌行业来源",
            bg=_SURFACE,
            fg=_TEXT_PRI,
            font=(_FONT, 13, "bold"),
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            title_box,
            text="按当前全部品牌的行业标签统计，圆环按品牌数量占比分布。",
            bg=_SURFACE,
            fg=_TEXT_TER,
            font=(_FONT, 8),
            anchor="w",
        ).pack(anchor="w", pady=(2, 0))

        canvas = tk.Canvas(card, bg=_SURFACE, highlightthickness=0, height=190)
        canvas.pack(fill=tk.X, padx=8, pady=(0, 8))

        legend_frame = tk.Frame(card, bg=_SURFACE)
        legend_frame.pack(fill=tk.X, padx=16, pady=(0, 12))

        def _build_series():
            tasks = [task for task in (self.config.get("tasks", []) or []) if isinstance(task, dict)]
            if not tasks:
                return None

            counts = Counter()
            for task in tasks:
                counts[_task_primary_industry(task) or "未分类"] += 1

            if not counts:
                return None

            items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            if len(items) > 5:
                other_total = sum(count for _, count in items[4:])
                items = items[:4] + ([("其他", other_total)] if other_total else [])

            total = sum(count for _, count in items)
            if total <= 0:
                return None

            palette = ["#2563EB", "#60A5FA", "#93C5FD", "#BFDBFE", "#D6E4FF", "#1D4ED8"]
            display_items = []
            for idx, (label, count) in enumerate(items):
                display_items.append({
                    "label": label,
                    "count": count,
                    "ratio": count / total,
                    "color": palette[idx % len(palette)],
                })

            return {
                "brand_total": len(tasks),
                "total": total,
                "items": display_items,
            }

        def redraw():
            series = _build_series()
            canvas.delete("all")
            for widget in legend_frame.winfo_children():
                widget.destroy()

            W = canvas.winfo_width()
            if W <= 20:
                W = 820
            H = canvas.winfo_height()
            if H <= 20:
                H = 190
            if not series:
                canvas.create_text(
                    W // 2,
                    H // 2,
                    text="当前没有可展示的品牌行业来源",
                    font=(_FONT, 12),
                    fill=_TEXT_TER,
                )
                tk.Label(
                    legend_frame,
                    text="先在品牌页为品牌补充行业标签。",
                    bg=_SURFACE,
                    fg=_TEXT_TER,
                    font=(_FONT, 8),
                ).pack(anchor="center")
                return

            items = series["items"]
            cx = W // 2
            cy = H // 2 - 2
            outer = min(92, max(72, min(W, H) // 2 - 16))
            inner = int(outer * 0.67)
            x1, y1, x2, y2 = cx - outer, cy - outer, cx + outer, cy + outer

            start = 90.0
            for item in items:
                span = 360.0 * item["count"] / max(series["total"], 1)
                extent = -359.9 if len(items) == 1 else -span
                canvas.create_arc(
                    x1, y1, x2, y2,
                    start=start,
                    extent=extent,
                    style=tk.PIESLICE,
                    fill=item["color"],
                    outline=_SURFACE,
                    width=4,
                )
                start += extent

            canvas.create_oval(cx - inner, cy - inner, cx + inner, cy + inner,
                               fill=_SURFACE, outline="")
            canvas.create_text(
                cx,
                cy - 12,
                text="100%",
                font=(_FONT, 24, "bold"),
                fill=_TEXT_PRI,
            )
            canvas.create_text(
                cx,
                cy + 22,
                text="总来源",
                font=(_FONT, 10, "bold"),
                fill=_TEXT_TER,
            )

            rows = [items[i:i + 3] for i in range(0, len(items), 3)]
            for row_items in rows:
                row = tk.Frame(legend_frame, bg=_SURFACE)
                row.pack(anchor="center", pady=2)
                for item in row_items:
                    chip = tk.Frame(row, bg=_SURFACE)
                    chip.pack(side=tk.LEFT, padx=12)
                    dot = tk.Canvas(chip, width=12, height=12, bg=_SURFACE, highlightthickness=0)
                    dot.pack(side=tk.LEFT)
                    dot.create_oval(2, 2, 10, 10, fill=item["color"], outline="")
                    tk.Label(
                        chip,
                        text=item["label"],
                        bg=_SURFACE,
                        fg=_TEXT_SEC,
                        font=(_FONT, 10, "bold"),
                    ).pack(side=tk.LEFT, padx=(4, 0))

        self._industry_source_redraw = redraw
        canvas.after(120, redraw)

    def _build_publication_media_card(self, parent):
        card = tk.Frame(parent, bg=_SURFACE, highlightbackground=_BORDER, highlightthickness=1)
        card.pack(fill=tk.X, padx=24, pady=(0, 18))

        top = tk.Frame(card, bg=_SURFACE)
        top.pack(fill=tk.X, padx=16, pady=(14, 8))

        title_box = tk.Frame(top, bg=_SURFACE)
        title_box.pack(side=tk.LEFT, anchor="w")
        tk.Label(
            title_box,
            text="发布媒体统计",
            bg=_SURFACE,
            fg=_TEXT_PRI,
            font=(_FONT, 13, "bold"),
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            title_box,
            text="汇总所有品牌的发布文章，按月区分权威媒体与自媒体。",
            bg=_SURFACE,
            fg=_TEXT_TER,
            font=(_FONT, 8),
            anchor="w",
        ).pack(anchor="w", pady=(2, 0))

        legend_box = tk.Frame(top, bg=_SURFACE)
        legend_box.pack(side=tk.RIGHT, anchor="e")
        filter_state = {"value": getattr(self, "_publication_media_filter", "all")}
        filter_widgets: dict[str, dict[str, tk.Widget]] = {}

        def _set_filter(media_type: str) -> None:
            media_type = str(media_type or "all").strip()
            if media_type == filter_state["value"]:
                filter_state["value"] = "all"
            else:
                filter_state["value"] = media_type if media_type in ("authority", "selfmedia") else "all"
            self._publication_media_filter = filter_state["value"]
            redraw()

        def _style_filter_pill(kind: str, active: bool) -> None:
            item = filter_widgets.get(kind)
            if not item:
                return
            pill = item["pill"]
            dot = item["dot"]
            label = item["label"]
            bg = "#DCEBFF" if active else "#F4F6FA"
            pill.configure(bg=bg, highlightbackground=_ACCENT if active else _BORDER)
            dot.configure(bg=bg)
            label.configure(bg=bg, fg=_ACCENT if active else _TEXT_PRI)

        for text, key, color in (("权威媒体", "authority", "#2563EB"), ("自媒体", "selfmedia", "#93C5FD")):
            pill = tk.Frame(
                legend_box,
                bg="#F4F6FA",
                highlightbackground=_BORDER,
                highlightthickness=1,
                cursor="hand2",
            )
            pill.pack(side=tk.LEFT, padx=(0, 8))
            dot = tk.Canvas(pill, width=12, height=12, bg="#F4F6FA", highlightthickness=0, cursor="hand2")
            dot.pack(side=tk.LEFT, padx=(10, 4), pady=8)
            dot.create_oval(2, 2, 10, 10, fill=color, outline="")
            label = tk.Label(
                pill,
                text=text,
                bg="#F4F6FA",
                fg=_TEXT_PRI,
                font=(_FONT, 9, "bold"),
                cursor="hand2",
            )
            label.pack(side=tk.LEFT, padx=(0, 10), pady=6)
            for widget in (pill, dot, label):
                widget.bind("<Button-1>", lambda _e, k=key: _set_filter(k))
            filter_widgets[key] = {"pill": pill, "dot": dot, "label": label}

        meta_var = tk.StringVar(value="")
        tk.Label(
            card,
            textvariable=meta_var,
            bg=_SURFACE,
            fg=_TEXT_TER,
            font=(_FONT, 8),
            anchor="w",
        ).pack(fill=tk.X, padx=16, pady=(0, 8))

        canvas = tk.Canvas(card, bg=_SURFACE, highlightthickness=0, height=250)
        canvas.pack(fill=tk.X, padx=8, pady=(0, 10))

        detail_box = tk.Frame(card, bg=_SURFACE)
        detail_box.pack(fill=tk.X, padx=16, pady=(0, 14))

        detail_top = tk.Frame(detail_box, bg=_SURFACE)
        detail_top.pack(fill=tk.X)
        tk.Label(
            detail_top,
            text="文章展示",
            bg=_SURFACE,
            fg=_TEXT_PRI,
            font=(_FONT, 11, "bold"),
            anchor="w",
        ).pack(side=tk.LEFT, anchor="w")
        ttk.Button(detail_top, text="查看全部文章", command=lambda: ArticleWindow(self.root, self.config)).pack(side=tk.RIGHT)

        detail_meta_var = tk.StringVar(value="")
        tk.Label(
            detail_box,
            textvariable=detail_meta_var,
            bg=_SURFACE,
            fg=_TEXT_TER,
            font=(_FONT, 8),
            anchor="w",
        ).pack(fill=tk.X, pady=(2, 8))

        article_rows = tk.Frame(detail_box, bg=_SURFACE)
        article_rows.pack(fill=tk.X)

        def _parse_article_ts(value):
            text = str(value or "").strip()
            if not text:
                return None
            from datetime import datetime

            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(text[:len(datetime.now().strftime(fmt))], fmt)
                except Exception:
                    continue
            return None

        def _load_articles():
            try:
                return get_articles()
            except Exception:
                return []

        def _filter_articles(articles, media_type: str):
            media_type = str(media_type or "all").strip()
            if media_type in ("authority", "selfmedia"):
                return [a for a in articles if str(a.get("media_type", "selfmedia")).strip() == media_type]
            return articles

        def _build_series(articles):
            if not articles:
                return None

            from datetime import date

            today = date.today()
            month_starts = []
            cur = date(today.year, today.month, 1)
            for _ in range(6):
                month_starts.append(cur)
                if cur.month == 1:
                    cur = date(cur.year - 1, 12, 1)
                else:
                    cur = date(cur.year, cur.month - 1, 1)
            month_starts.reverse()
            month_keys = [item.strftime("%Y-%m") for item in month_starts]
            counts = {key: {"authority": 0, "selfmedia": 0} for key in month_keys}

            for article in articles:
                dt = _parse_article_ts(article.get("ts", ""))
                if not dt:
                    continue
                key = dt.strftime("%Y-%m")
                if key not in counts:
                    continue
                media_type = str(article.get("media_type", "selfmedia")).strip()
                if media_type not in ("authority", "selfmedia"):
                    media_type = "selfmedia"
                counts[key][media_type] += 1

            month_items = []
            total_authority = total_selfmedia = 0
            for month_start in month_starts:
                key = month_start.strftime("%Y-%m")
                authority = counts[key]["authority"]
                selfmedia = counts[key]["selfmedia"]
                total_authority += authority
                total_selfmedia += selfmedia
                month_items.append({
                    "label": f"{month_start.month}月",
                    "authority": authority,
                    "selfmedia": selfmedia,
                    "total": authority + selfmedia,
                })

            overall_total = total_authority + total_selfmedia
            if overall_total <= 0:
                return None

            return {
                "months": month_items,
                "authority_total": total_authority,
                "selfmedia_total": total_selfmedia,
                "total": overall_total,
            }

        def _render_articles(articles, media_type: str):
            for widget in article_rows.winfo_children():
                widget.destroy()

            media_type = str(media_type or "all").strip()
            filtered = _filter_articles(articles, media_type)
            label_map = {"authority": "权威媒体", "selfmedia": "自媒体", "all": "全部类别"}
            detail_meta_var.set(
                f"当前类别：{label_map.get(media_type, '全部类别')}  ·  显示 {len(filtered)} 篇  ·  最近 6 条"
            )

            if not filtered:
                tk.Label(
                    article_rows,
                    text="当前筛选类别没有文章。",
                    bg=_SURFACE,
                    fg=_TEXT_TER,
                    font=(_FONT, 8),
                    anchor="w",
                ).pack(anchor="w", pady=(2, 0))
                return

            for idx, article in enumerate(filtered[:6]):
                row_bg = "#FAFBFC" if idx % 2 == 0 else _SURFACE
                row = tk.Frame(article_rows, bg=row_bg, highlightbackground=_BORDER, highlightthickness=1)
                row.pack(fill=tk.X, pady=(0, 6))

                ts = str(article.get("ts", "") or "")[:16]
                title = str(article.get("title") or article.get("url", "") or "未命名文章").strip()
                if len(title) > 72:
                    title = title[:72] + "..."
                platform = str(article.get("platform", "") or "").strip() or "未知来源"
                media_label = "权威媒体" if str(article.get("media_type", "selfmedia")).strip() == "authority" else "自媒体"

                head = tk.Frame(row, bg=row_bg)
                head.pack(fill=tk.X, padx=10, pady=(8, 2))
                tk.Label(
                    head,
                    text=title,
                    bg=row_bg,
                    fg=_TEXT_PRI,
                    font=(_FONT, 9, "bold"),
                    anchor="w",
                    justify="left",
                ).pack(side=tk.LEFT, fill=tk.X, expand=True)
                tk.Label(
                    head,
                    text=media_label,
                    bg=row_bg,
                    fg=_ACCENT if media_label == "权威媒体" else "#C07A00",
                    font=(_FONT, 8, "bold"),
                ).pack(side=tk.RIGHT, padx=(8, 0))

                foot = tk.Frame(row, bg=row_bg)
                foot.pack(fill=tk.X, padx=10, pady=(0, 8))
                tk.Label(foot, text=ts, bg=row_bg, fg=_TEXT_TER, font=(_FONT, 8), anchor="w").pack(side=tk.LEFT)
                tk.Label(foot, text=platform, bg=row_bg, fg=_TEXT_SEC, font=(_FONT, 8), anchor="w").pack(side=tk.RIGHT)

            if len(filtered) > 6:
                tk.Label(
                    article_rows,
                    text=f"还有 {len(filtered) - 6} 篇未显示。",
                    bg=_SURFACE,
                    fg=_TEXT_TER,
                    font=(_FONT, 8),
                    anchor="w",
                ).pack(anchor="w", pady=(2, 0))

        def redraw():
            articles = _load_articles()
            series = _build_series(articles)
            canvas.delete("all")

            W = canvas.winfo_width()
            if W <= 20:
                W = 820
            H = canvas.winfo_height()
            if H <= 20:
                H = 250

            current_filter = filter_state["value"]
            for kind in ("authority", "selfmedia"):
                _style_filter_pill(kind, current_filter == kind)

            if not series:
                meta_var.set("暂无可展示的发布文章数据")
                detail_meta_var.set("当前没有可展示的文章。")
                canvas.create_text(
                    W // 2,
                    H // 2,
                    text="当前没有可展示的发布媒体统计",
                    font=(_FONT, 12),
                    fill=_TEXT_TER,
                )
                _render_articles([], current_filter)
                return

            filtered_articles = _filter_articles(articles, current_filter)
            visible_total = len(filtered_articles)
            meta_var.set(
                f"总文章 {series['total']} 篇  ·  当前显示 {visible_total} 篇  ·  权威媒体 {series['authority_total']} 篇  ·  自媒体 {series['selfmedia_total']} 篇"
            )

            months = series["months"]
            active_types = [current_filter] if current_filter in ("authority", "selfmedia") else ["authority", "selfmedia"]
            visible_max = max((month[mt] for month in months for mt in active_types), default=0)
            import math

            axis_step = max(1, int(math.ceil(max(1, visible_max) / 4.0)))
            axis_max = axis_step * 4
            left, right, top, bottom = 56, 18, 22, 38
            plot_w = max(1, W - left - right)
            plot_h = max(1, H - top - bottom)

            def y_for_value(value: float) -> float:
                return top + plot_h - (value / axis_max) * plot_h

            grid_values = [axis_step * i for i in range(5)]
            for idx, value in enumerate(grid_values):
                y = y_for_value(value)
                canvas.create_line(left, y, W - right, y, fill=_BORDER, dash=() if idx == 0 else (3, 4))
                canvas.create_text(
                    left - 8,
                    y,
                    text=str(int(value)),
                    anchor="e",
                    fill=_TEXT_TER,
                    font=(_FONT, 8),
                )

            slot_w = plot_w / max(len(months), 1)
            bar_width = max(10, slot_w * 0.28)
            bar_gap = max(4, slot_w * 0.10)
            authority_color = "#2563EB"
            selfmedia_color = "#93C5FD"
            x_axis_y = H - bottom
            canvas.create_line(left, top, left, x_axis_y, fill=_BORDER)
            canvas.create_line(left, x_axis_y, W - right, x_axis_y, fill=_BORDER)

            for idx, month in enumerate(months):
                x_center = left + slot_w * idx + slot_w / 2
                if current_filter in ("authority", "selfmedia"):
                    media_types = [current_filter]
                else:
                    media_types = ["authority", "selfmedia"]

                if len(media_types) == 2:
                    offsets = {"authority": -(bar_width / 2 + bar_gap / 2), "selfmedia": bar_width / 2 + bar_gap / 2}
                else:
                    offsets = {media_types[0]: 0}

                for media_type in media_types:
                    value = int(month.get(media_type, 0) or 0)
                    bh = (value / axis_max) * (plot_h - 6)
                    x0 = x_center + offsets[media_type] - bar_width / 2
                    x1 = x_center + offsets[media_type] + bar_width / 2
                    y1 = x_axis_y
                    y0 = y1 - bh
                    fill = authority_color if media_type == "authority" else selfmedia_color
                    fill = fill if value > 0 else _BAR_EMPTY
                    bar_tag = f"pub_{media_type}_{idx}"
                    canvas.create_rectangle(x0, y0, x1, y1, fill=fill, outline="", tags=(bar_tag,))
                    if value > 0:
                        canvas.create_text(
                            (x0 + x1) / 2,
                            y0 - 8,
                            text=str(value),
                            fill=_TEXT_PRI,
                            font=(_FONT, 8, "bold"),
                            tags=(bar_tag,),
                        )
                    canvas.tag_bind(bar_tag, "<Button-1>", lambda _e, mt=media_type: _set_filter(mt))

                canvas.create_text(
                    x_center,
                    H - 16,
                    text=month["label"],
                    fill=_TEXT_TER,
                    font=(_FONT, 8, "bold"),
                )

            _render_articles(articles, current_filter)

        redraw_job = {"id": None}

        def schedule_redraw(_event=None):
            job_id = redraw_job["id"]
            if job_id is not None:
                try:
                    canvas.after_cancel(job_id)
                except Exception:
                    pass
            redraw_job["id"] = canvas.after(90, redraw)

        canvas.bind("<Configure>", schedule_redraw)
        self._publication_media_redraw = redraw
        schedule_redraw()

    def _build_region_distribution_card(self, parent):
        card = tk.Frame(parent, bg=_SURFACE, highlightbackground=_BORDER, highlightthickness=1)
        card.pack(fill=tk.X, padx=24, pady=(0, 18))

        top = tk.Frame(card, bg=_SURFACE)
        top.pack(fill=tk.X, padx=16, pady=(14, 8))

        title_box = tk.Frame(top, bg=_SURFACE)
        title_box.pack(side=tk.LEFT, anchor="w")
        tk.Label(
            title_box,
            text="优化区域分布",
            bg=_SURFACE,
            fg=_TEXT_PRI,
            font=(_FONT, 13, "bold"),
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            title_box,
            text="读取品牌标签里的地区字段，国内模式点亮省份，国际模式点亮主要国家。",
            bg=_SURFACE,
            fg=_TEXT_TER,
            font=(_FONT, 8),
            anchor="w",
        ).pack(anchor="w", pady=(2, 0))

        control_box = tk.Frame(top, bg=_SURFACE)
        control_box.pack(side=tk.RIGHT, anchor="e")

        mode_var = tk.StringVar(
            value=getattr(self, "_region_distribution_mode", "domestic")
            if getattr(self, "_region_distribution_mode", "domestic") in ("domestic", "international")
            else "domestic"
        )
        mode_buttons: dict[str, tk.Widget] = {}

        def _set_mode(mode: str):
            mode = mode if mode in ("domestic", "international") else "domestic"
            self._region_distribution_mode = mode
            redraw()

        for mode, label in (("domestic", "国内"), ("international", "国际")):
            btn = tk.Radiobutton(
                control_box,
                text=label,
                variable=mode_var,
                value=mode,
                indicatoron=False,
                bg=_SURFACE2,
                fg=_TEXT_PRI,
                selectcolor=_SURFACE,
                activebackground=_BORDER2,
                activeforeground=_TEXT_PRI,
                relief=tk.FLAT,
                width=6,
                font=(_FONT, 9, "bold"),
                padx=8,
                pady=6,
                command=lambda m=mode: _set_mode(m),
            )
            btn.pack(side=tk.LEFT, padx=(0, 8))
            mode_buttons[mode] = btn

        summary_var = tk.StringVar(value="")
        tk.Label(
            card,
            textvariable=summary_var,
            bg=_SURFACE,
            fg=_TEXT_TER,
            font=(_FONT, 8),
            anchor="w",
        ).pack(fill=tk.X, padx=16, pady=(0, 6))

        canvas = tk.Canvas(card, bg=_SURFACE, highlightthickness=0, height=140)
        canvas.pack(fill=tk.X, padx=8, pady=(0, 12))

        redraw_job = {"id": None}

        domestic_nodes = [
            {"id": "xj", "label": "新疆", "x": 0.18, "y": 0.35},
            {"id": "xz", "label": "西藏", "x": 0.18, "y": 0.65},
            {"id": "qh", "label": "青海", "x": 0.35, "y": 0.48},
            {"id": "gs", "label": "甘肃", "x": 0.45, "y": 0.40},
            {"id": "nm", "label": "内蒙古", "x": 0.55, "y": 0.25},
            {"id": "hlj", "label": "黑龙江", "x": 0.82, "y": 0.15},
            {"id": "jl", "label": "吉林", "x": 0.85, "y": 0.25},
            {"id": "ln", "label": "辽宁", "x": 0.80, "y": 0.32},
            {"id": "bj", "label": "北京", "x": 0.70, "y": 0.35},
            {"id": "tj", "label": "天津", "x": 0.73, "y": 0.38},
            {"id": "he", "label": "河北", "x": 0.68, "y": 0.40},
            {"id": "sx", "label": "山西", "x": 0.60, "y": 0.45},
            {"id": "sn", "label": "陕西", "x": 0.55, "y": 0.52},
            {"id": "nx", "label": "宁夏", "x": 0.48, "y": 0.45},
            {"id": "sd", "label": "山东", "x": 0.75, "y": 0.46},
            {"id": "ha", "label": "河南", "x": 0.65, "y": 0.54},
            {"id": "js", "label": "江苏", "x": 0.80, "y": 0.56},
            {"id": "ah", "label": "安徽", "x": 0.75, "y": 0.60},
            {"id": "sh", "label": "上海", "x": 0.85, "y": 0.59},
            {"id": "zj", "label": "浙江", "x": 0.82, "y": 0.66},
            {"id": "jx", "label": "江西", "x": 0.75, "y": 0.70},
            {"id": "fj", "label": "福建", "x": 0.80, "y": 0.75},
            {"id": "tw", "label": "台湾", "x": 0.86, "y": 0.80},
            {"id": "hb", "label": "湖北", "x": 0.65, "y": 0.62},
            {"id": "hn", "label": "湖南", "x": 0.65, "y": 0.72},
            {"id": "gd", "label": "广东", "x": 0.70, "y": 0.85},
            {"id": "hk", "label": "香港", "x": 0.73, "y": 0.90},
            {"id": "mc", "label": "澳门", "x": 0.68, "y": 0.90},
            {"id": "hi", "label": "海南", "x": 0.65, "y": 0.96},
            {"id": "gx", "label": "广西", "x": 0.60, "y": 0.85},
            {"id": "gz", "label": "贵州", "x": 0.52, "y": 0.76},
            {"id": "sc", "label": "四川", "x": 0.45, "y": 0.66},
            {"id": "cq", "label": "重庆", "x": 0.53, "y": 0.66},
            {"id": "yn", "label": "云南", "x": 0.40, "y": 0.82},
        ]

        international_nodes = [
            {"id": "usa", "label": "美国", "x": 0.20, "y": 0.35},
            {"id": "japan", "label": "日本", "x": 0.85, "y": 0.30},
            {"id": "uk", "label": "英国", "x": 0.45, "y": 0.25},
            {"id": "singapore", "label": "新加坡", "x": 0.75, "y": 0.55},
            {"id": "germany", "label": "德国", "x": 0.52, "y": 0.30},
            {"id": "australia", "label": "澳大利亚", "x": 0.85, "y": 0.80},
            {"id": "france", "label": "法国", "x": 0.50, "y": 0.35},
            {"id": "canada", "label": "加拿大", "x": 0.20, "y": 0.20},
            {"id": "sk", "label": "韩国", "x": 0.80, "y": 0.35},
            {"id": "brazil", "label": "巴西", "x": 0.30, "y": 0.75},
            {"id": "india", "label": "印度", "x": 0.25, "y": 0.60},
            {"id": "russia", "label": "俄罗斯", "x": 0.70, "y": 0.20},
        ]

        def _collect_region_data(mode: str):
            tasks = [task for task in (self.config.get("tasks", []) or []) if isinstance(task, dict)]
            visible_nodes = domestic_nodes if mode == "domestic" else international_nodes
            lookup = _DOMESTIC_REGION_LOOKUP if mode == "domestic" else _INTERNATIONAL_REGION_LOOKUP

            counts = Counter()
            unmatched = Counter()
            matched_tasks = 0
            matched_tags = 0

            for task in tasks:
                tags = _split_tags(task.get("region_tags") or task.get("region"))
                task_matched = False
                for tag in tags:
                    point = lookup.get(_region_key(tag))
                    if point:
                        counts[point["label"]] += 1
                        matched_tags += 1
                        task_matched = True
                    else:
                        unmatched[tag] += 1
                if task_matched:
                    matched_tasks += 1

            return {
                "tasks": tasks,
                "nodes": [{**node, "count": int(counts.get(node["label"], 0) or 0)} for node in visible_nodes],
                "matched_tasks": matched_tasks,
                "matched_tags": matched_tags,
                "unmatched": unmatched,
                "total_active": sum(1 for node in visible_nodes if counts.get(node["label"], 0)),
                "total_unmatched": sum(unmatched.values()),
            }

        def redraw():
            fallback_mode = getattr(self, "_region_distribution_mode", "domestic")
            mode = mode_var.get() if mode_var.get() in ("domestic", "international") else fallback_mode
            mode_var.set(mode)
            self._region_distribution_mode = mode
            data = _collect_region_data(mode)
            canvas.delete("all")

            for mode_name, button in mode_buttons.items():
                try:
                    button.configure(
                        bg="#DCEBFF" if mode_name == mode else _SURFACE2,
                        highlightbackground=_ACCENT if mode_name == mode else _BORDER,
                    )
                except Exception:
                    pass

            W = canvas.winfo_width()
            if W <= 20:
                W = 820
            H = canvas.winfo_height()
            if H <= 20:
                H = 140

            canvas.create_rectangle(0, 0, W, H, fill="#FAFBFC", outline="")
            for x in range(0, W, 16):
                canvas.create_line(x, 0, x, H, fill="#000000", stipple="gray12")
            for y in range(0, H, 16):
                canvas.create_line(0, y, W, y, fill="#000000", stipple="gray12")

            nodes = data["nodes"]
            active_nodes = [node for node in nodes if node["count"] > 0]

            if len(active_nodes) > 1:
                for idx, node in enumerate(active_nodes[:-1]):
                    nxt = active_nodes[idx + 1]
                    canvas.create_line(
                        node["x"] * W,
                        node["y"] * H,
                        nxt["x"] * W,
                        nxt["y"] * H,
                        fill="#1864ff",
                        width=1,
                        dash=(3, 3),
                    )

            for node in nodes:
                x = node["x"] * W
                y = node["y"] * H
                count = int(node["count"] or 0)
                active = count > 0
                if active:
                    canvas.create_oval(x - 10, y - 10, x + 10, y + 10, fill="#DCEBFF", outline="")
                    canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill="#1864ff", outline="")
                else:
                    canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill="#CBD5E1", outline="")
                canvas.create_text(
                    x,
                    y + 14,
                    text=node["label"],
                    fill="#374151" if active else "#9CA3AF",
                    font=(_FONT, 8, "bold"),
                    anchor="n",
                )

            summary_var.set(
                f"{'国内' if mode == 'domestic' else '国际'}模式 · 品牌 {len(data['tasks'])} 个 · 已点亮 {data['total_active']} 个区域 · 匹配 {data['matched_tags']} 条标签 · 未匹配 {data['total_unmatched']} 条"
            )

        def schedule_redraw(_event=None):
            job_id = redraw_job["id"]
            if job_id is not None:
                try:
                    canvas.after_cancel(job_id)
                except Exception:
                    pass
            redraw_job["id"] = canvas.after(90, redraw)

        canvas.bind("<Configure>", schedule_redraw)
        self._region_distribution_redraw = redraw
        schedule_redraw()

    def _add_task(self):
        from ui.config_manager import TaskConfigDialog
        dlg = TaskConfigDialog(self.root, existing_tasks=self.config.get('tasks', []))
        self.root.wait_window(dlg.dialog)
        if dlg.result:
            self.config.setdefault('tasks', []).append(dlg.result)
            self._refresh_tasks()
            self._persist_runtime_config_silent()

    def _edit_task(self, index):
        tasks = self.config.get('tasks', [])
        if index >= len(tasks):
            return
        from ui.config_manager import TaskConfigDialog
        dlg = TaskConfigDialog(self.root, task=tasks[index], existing_tasks=self.config.get('tasks', []))
        self.root.wait_window(dlg.dialog)
        if dlg.result:
            tasks[index] = dlg.result
            self._refresh_tasks()
            self._persist_runtime_config_silent()

    def _delete_task(self, index):
        if messagebox.askyesno("确认", "确定要删除这个品牌吗？"):
            tasks = self.config.get('tasks', [])
            if index < len(tasks):
                tasks.pop(index)
                self._refresh_tasks()
                self._persist_runtime_config_silent()

    def _apply_local_config_change(self, new_config, reload_runtime=False):
        self.config = new_config or {}
        ensure_config_task_ids(self.config)
        self._trend_built = False
        self._refresh_mode_cards()
        self._refresh_overview_trend()
        self._refresh_brand_industry_source()
        self._refresh_region_distribution()
        self._refresh_tasks()
        self._refresh_quick_todo_panel()
        self._refresh_status()
        self._refresh_mode_notice()
        try:
            self.root.event_generate("<<ConfigUpdated>>", when="tail")
        except Exception:
            pass
        if self.on_config_change:
            self.on_config_change(self.config)
        elif reload_runtime and self.running:
            self._stop_monitoring()
            self._start_monitoring()

    def _apply_config_and_reload(self, new_config):
        """统一处理配置保存后的本地刷新与运行态重载。"""
        self._apply_local_config_change(new_config, reload_runtime=True)

    def _persist_runtime_config(self, success_message: str):
        """主窗口内统一的配置保存入口。"""
        persist_config_with_feedback(
            self.config,
            self.config_path,
            success_message=success_message,
            ensure_task_ids=True,
            on_config_change=self._apply_config_and_reload,
            event_root=self.root,
        )

    def _persist_runtime_config_silent(self):
        """主窗口内不弹窗的落盘入口，用于列表编辑这类高频操作。"""
        persist_config(
            self.config,
            self.config_path,
            ensure_task_ids=True,
            on_config_change=self._apply_config_and_reload,
            event_root=self.root,
        )

    def _save_config(self):
        self._persist_runtime_config("配置已保存")

    def _open_ai_assistant(self):
        from ui.ai_dialog import AIAssistantDialog

        AIAssistantDialog(
            self.root,
            config=self.config,
            config_path=str(self.config_path),
            on_config_change=self._apply_config_and_reload,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Tab 2: 监控状态
    # ──────────────────────────────────────────────────────────────────────────

    def _build_status_tab(self):
        frame = tk.Frame(self.notebook, bg=_BG)
        self.notebook.add(frame, text="  状态  ")

        toolbar = tk.Frame(frame, bg=_BG)
        toolbar.pack(fill=tk.X, padx=24, pady=(20, 0))
        self._toolbar_btn(toolbar, "刷新", self._refresh_status)

        # 统计卡片行
        self._stat_row = tk.Frame(frame, bg=_BG)
        self._stat_row.pack(fill=tk.X, padx=24, pady=(16, 0))

        tk.Frame(frame, bg=_BORDER, height=1).pack(fill=tk.X, padx=24, pady=(16, 0))

        self._status_text = scrolledtext.ScrolledText(
            frame, wrap=tk.WORD, font=(_FONT_MONO, 9),
            state=tk.DISABLED,
            bg=_SURFACE, fg=_TEXT_PRI,
            relief=tk.FLAT,
            highlightbackground=_BORDER, highlightthickness=1,
            padx=20, pady=16,
            insertbackground=_TEXT_PRI,
            selectbackground=_ACCENT)
        self._status_text.pack(fill=tk.BOTH, expand=True, padx=24, pady=(12, 20))
        self._refresh_status()

    def _refresh_status(self):
        # 清空统计卡片
        for w in self._stat_row.winfo_children():
            w.destroy()

        results = self.last_results
        total   = len(results)
        found   = sum(1 for r in results.values() if r != 99)
        missed  = total - found
        rate    = f"{round(found/total*100)}%" if total else "—"

        for label, value, color in [
            ("总检测", str(total), _TEXT_PRI),
            ("已提及", str(found), _SUCCESS),
            ("未提及", str(missed), _DANGER),
            ("提及率", rate, _ACCENT),
        ]:
            card = tk.Frame(self._stat_row, bg=_SURFACE,
                            highlightbackground=_BORDER, highlightthickness=1)
            card.pack(side=tk.LEFT, padx=(0, 8), ipadx=20, ipady=10)
            tk.Label(card, text=value, bg=_SURFACE, fg=color,
                     font=(_FONT, 22, "bold")).pack()
            tk.Label(card, text=label.upper(), bg=_SURFACE, fg=_TEXT_TER,
                     font=(_FONT, 7, "bold")).pack()

        # 文字区
        self._status_text.config(state=tk.NORMAL)
        self._status_text.delete(1.0, tk.END)

        if results:
            header = f"{'状态':<8}  {'任务':<16}  {'平台':<12}  {'品牌':<14}  结果\n"
            self._status_text.insert(tk.END, header)
            self._status_text.insert(tk.END, "─" * 58 + "\n")
            for (task_name, platform, brand), rank in sorted(results.items()):
                s = "已提及" if rank != 99 else "未提及"
                r = "✅" if rank != 99 else "—"
                self._status_text.insert(
                    tk.END, f"{s:<8}  {task_name:<16}  {platform:<12}  {brand:<14}  {r}\n")
        else:
            self._status_text.insert(tk.END, "暂无数据 — 监控未运行或未产生结果\n")

        if self.scheduler:
            st = self.scheduler.get_status()
            self._status_text.insert(tk.END, "\n" + "─" * 58 + "\n")
            self._status_text.insert(tk.END, f"调度器  {'运行中' if st['running'] else '已停止'}\n")
            current_mode_label = str(st.get('current_mode_label') or '').strip()
            current_round = st.get('current_round_summary') or {}
            last_completed = st.get('last_completed_summary') or {}
            if current_mode_label and current_round.get('total'):
                self._status_text.insert(
                    tk.END,
                    f"当前轮次  {current_mode_label}  "
                    f"{current_round.get('completed', 0)}/{current_round.get('total', 0)}  "
                    f"成功{current_round.get('success', 0)}  "
                    f"失败{current_round.get('failed', 0)}  "
                    f"跳过{current_round.get('skipped', 0)}\n",
                )
            elif last_completed.get('mode_label'):
                self._status_text.insert(
                    tk.END,
                    f"最近完成  {last_completed.get('mode_label')}  "
                    f"成功{last_completed.get('success', 0)}  "
                    f"失败{last_completed.get('failed', 0)}  "
                    f"跳过{last_completed.get('skipped', 0)}\n",
                )
            weekly_times = st.get('weekly_times', {})
            for weekday in range(7):
                run_time = weekly_times.get(str(weekday))
                label = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][weekday]
                self._status_text.insert(tk.END, f"{label}  {run_time or '不自动运行'}\n")

        self._status_text.config(state=tk.DISABLED)

    # ──────────────────────────────────────────────────────────────────────────
    # Tab 3: 趋势图
    # ──────────────────────────────────────────────────────────────────────────

    def _build_trend_tab(self):
        frame = tk.Frame(self.notebook, bg=_BG)
        self.notebook.add(frame, text="  趋势  ")
        self._trend_frame = frame
        self._trend_tab_frame = frame
        tk.Label(
            frame,
            text="品牌趋势已移到每个品牌的编辑页",
            fg=_TEXT_PRI,
            bg=_BG,
            font=(_FONT, 14, "bold"),
        ).pack(pady=(90, 8))
        tk.Label(
            frame,
            text="请打开任意品牌的编辑窗口查看该品牌自己的周月年趋势和平滑预测曲线。",
            fg=_TEXT_TER,
            bg=_BG,
            font=(_FONT, 10),
        ).pack()
        tk.Button(
            frame,
            text="前往品牌页",
            command=lambda: self.notebook.select(getattr(self, "_tasks_tab_frame", self._trend_tab_frame)),
        ).pack(pady=18)

    def _on_tab_change(self, event):
        current_tab = self.notebook.nametowidget(self.notebook.select())
        if current_tab == getattr(self, "_trend_tab_frame", None) and not self._trend_built:
            self._trend_built = True
            for w in self._trend_frame.winfo_children():
                w.destroy()
            self._init_trend(self._trend_frame)

    def _init_trend(self, frame):
        try:
            from core.daily_task_state import derive_task_id
            from core.history import (
                build_trend_rate_items,
                get_current_task_names,
                get_records,
                get_task_brand_names,
                is_manual_test_failure_record,
            )
            from datetime import date, timedelta
        except ImportError:
            tk.Label(frame, text="历史模块未找到", fg=_DANGER, bg=_BG).pack(pady=20)
            return

        task_names = get_current_task_names(self.config)
        if not task_names:
            tk.Label(
                frame,
                text="当前没有可展示的品牌配置",
                fg=_TEXT_TER,
                bg=_BG,
                font=(_FONT, 12),
            ).pack(pady=80)
            return
        task_map = {}
        for task in (self.config or {}).get("tasks", []) or []:
            if not isinstance(task, dict):
                continue
            task_name = str(task.get("name") or "").strip()
            if not task_name:
                task_name = next(iter(get_task_brand_names(task)), "")
            if task_name and task_name not in task_map:
                task_map[task_name] = task

        def _task_history_records(task_name: str) -> list[dict]:
            task_cfg = task_map.get(task_name, {})
            task_id = str((task_cfg or {}).get("task_id") or derive_task_id(task_cfg or {})).strip() if task_cfg else ""
            records = get_records(task_name, task_id=task_id)
            current_brands = get_task_brand_names(task_cfg)
            if current_brands:
                records = [r for r in records if str(r.get("brand", "")).strip() in current_brands]
            return [r for r in records if not is_manual_test_failure_record(r)]

        # 筛选栏
        top = tk.Frame(frame, bg=_BG)
        top.pack(fill=tk.X, padx=24, pady=(16, 0))

        def _lbl(text):
            tk.Label(top, text=text, bg=_BG, fg=_TEXT_TER,
                     font=(_FONT, 8)).pack(side=tk.LEFT, padx=(0, 4))

        def _menu(var, values):
            om = tk.OptionMenu(top, var, *values)
            om.config(bg=_SURFACE2, fg=_TEXT_PRI, font=(_FONT, 9),
                      relief=tk.FLAT, highlightthickness=0,
                      activebackground=_BORDER2, activeforeground=_TEXT_PRI)
            om["menu"].config(bg=_SURFACE2, fg=_TEXT_PRI, font=(_FONT, 9),
                              activebackground=_ACCENT, activeforeground=_TEXT_PRI)
            om.pack(side=tk.LEFT, padx=(0, 12))
            return om

        _lbl("品牌")
        selected_task = tk.StringVar(value=task_names[0])
        task_om = _menu(selected_task, task_names)

        _lbl("��键词")
        selected_kw = tk.StringVar(value="全部")
        kw_om = _menu(selected_kw, ["全部"])

        _lbl("品牌")
        selected_brand = tk.StringVar(value="全部")
        brand_om = _menu(selected_brand, ["全部"])

        _lbl("平台")
        selected_plat = tk.StringVar(value="全部")
        plat_om = _menu(selected_plat, ["全部"])

        _lbl("范围")
        range_var = tk.StringVar(value="月度")
        for lbl in ("月度", "季度", "年度"):
            tk.Radiobutton(top, text=lbl, variable=range_var, value=lbl,
                           bg=_BG, fg=_TEXT_SEC, selectcolor=_BG,
                           activebackground=_BG, activeforeground=_TEXT_PRI,
                           font=(_FONT, 9),
                           command=lambda: frame.after(50, redraw)).pack(side=tk.LEFT, padx=3)

        self._toolbar_btn(top, "刷新", lambda: frame.after(50, redraw))

        tk.Frame(frame, bg=_BORDER, height=1).pack(fill=tk.X, padx=24, pady=(12, 0))

        canvas = tk.Canvas(frame, bg=_BG, highlightthickness=0)
        canvas.pack(fill=tk.BOTH, expand=True, padx=24, pady=(8, 20))

        LINE_COLORS = ["#4A6CF7","#10B981","#F59E0B","#EF4444","#8B5CF6",
                       "#06B6D4","#EC4899","#F97316","#84CC16","#0EA5E9"]

        def update_filters(*_):
            task_name = selected_task.get()
            records = _task_history_records(task_name)
            task_cfg = task_map.get(task_name, {})
            current_brands = get_task_brand_names(task_cfg)
            kws    = ["全部"] + sorted({r.get("keyword","") for r in records if r.get("keyword")})
            brands = ["全部"] + (current_brands or sorted({r.get("brand","") for r in records if r.get("brand")}))
            plats  = ["全部"] + sorted({r.get("platform","") for r in records if r.get("platform")})
            for om, var, opts in [(kw_om, selected_kw, kws),
                                   (brand_om, selected_brand, brands),
                                   (plat_om, selected_plat, plats)]:
                om["menu"].delete(0, "end")
                for opt in opts:
                    om["menu"].add_command(label=opt,
                        command=lambda v=opt, sv=var: (sv.set(v), frame.after(50, redraw)))
                if var.get() not in opts:
                    var.set("全部")
            frame.after(50, redraw)

        def compute_series(records, kw_f, brand_f, plat_f, days):
            if kw_f    != "全部": records = [r for r in records if r.get("keyword")  == kw_f]
            if brand_f != "全部": records = [r for r in records if r.get("brand")    == brand_f]
            if plat_f  != "全部": records = [r for r in records if r.get("platform") == plat_f]
            if not records: return [], []
            cutoff = (date.today() - timedelta(days=days)).isoformat()
            items = [item for item in build_trend_rate_items(selected_task.get(), records) if item["date"] >= cutoff]
            dates = [item["date"] for item in items]
            rates = [None if item.get("missing") else item.get("rate") for item in items]
            return dates, rates

        def get_multi_series(records, days):
            cutoff = (date.today() - timedelta(days=days)).isoformat()
            if not records: return {}
            groups = {}
            for r in records:
                groups.setdefault((r.get("keyword",""), r.get("brand",""), r.get("platform","")), []).append(r)
            series = {}
            for key, recs in groups.items():
                items = [item for item in build_trend_rate_items(selected_task.get(), recs) if item["date"] >= cutoff]
                dates = [item["date"] for item in items]
                rates = [None if item.get("missing") else item.get("rate") for item in items]
                if dates:
                    series[f"{key[0]}/{key[1]}/{key[2]}"] = (dates, rates)
            return series

        def redraw():
            canvas.delete("all")
            W = canvas.winfo_width() or 860
            H = canvas.winfo_height() or 420
            pl, pr, pt, pb = 56, 24, 24, 56

            task_name = selected_task.get()
            kw_f = selected_kw.get(); brand_f = selected_brand.get(); plat_f = selected_plat.get()
            days = {"月度":30,"季度":90,"年度":365}.get(range_var.get(), 30)
            records = _task_history_records(task_name)

            all_default = kw_f == "全部" and brand_f == "全部" and plat_f == "全部"
            if all_default:
                series_map = get_multi_series(records, days)
            else:
                dates, rates = compute_series(records, kw_f, brand_f, plat_f, days)
                if not dates:
                    canvas.create_text(W//2, H//2, text="暂无数据", font=(_FONT,13), fill=_TEXT_TER)
                    return
                series_map = {f"{kw_f}/{brand_f}/{plat_f}": (dates, rates)}

            if not series_map:
                canvas.create_text(W//2, H//2, text="暂无数据", font=(_FONT,13), fill=_TEXT_TER)
                return

            all_dates = max((v[0] for v in series_map.values()), key=len)
            n = len(all_dates)
            def cx(i): return pl + (i / max(n-1,1)) * (W - pl - pr)
            def cy(v): return pt + (1 - v/100) * (H - pt - pb)

            # 网格
            for pct, lbl in ((100,"100%"),(80,"80%"),(60,"60%"),(0,"0%")):
                yy = cy(pct)
                dash = (3,5) if pct in (60,80) else ()
                col = _BORDER if pct not in (60,80) else get_percentage_color(float(pct))
                canvas.create_line(pl, yy, W-pr, yy, fill=col, dash=dash, width=1)
                canvas.create_text(pl-6, yy, text=lbl, anchor="e",
                                   font=(_FONT,8), fill=_TEXT_TER)

            # X轴
            step = max(1, n//8)
            for i, d in enumerate(all_dates):
                if i % step == 0 or i == n-1:
                    canvas.create_text(cx(i), H-pb+14, text=d[5:],
                                       font=(_FONT,8), fill=_TEXT_TER)

            # 折线
            for idx, (label, (dates, rates)) in enumerate(series_map.items()):
                color = LINE_COLORS[idx % len(LINE_COLORS)]
                date_to_rate = dict(zip(dates, rates))
                aligned = [date_to_rate.get(d) for d in all_dates]
                for i in range(n):
                    if aligned[i] is None: continue
                    if i < n-1 and aligned[i+1] is not None:
                        canvas.create_line(cx(i), cy(aligned[i]), cx(i+1), cy(aligned[i+1]),
                                           fill=color, width=2)
                    x0, y0 = cx(i), cy(aligned[i])
                    canvas.create_oval(x0-3, y0-3, x0+3, y0+3,
                                       fill=color, outline=_BG, width=1)

            # 图例
            lx = pl
            for idx, label in enumerate(series_map.keys()):
                color = LINE_COLORS[idx % len(LINE_COLORS)]
                canvas.create_rectangle(lx, H-pb+28, lx+10, H-pb+36, fill=color, outline="")
                canvas.create_text(lx+14, H-pb+32, text=label, anchor="w",
                                   font=(_FONT,8), fill=_TEXT_SEC)
                lx += len(label)*7 + 22
                if lx > W-100: break

        selected_task.trace_add("write", lambda *_: update_filters())
        frame.after(200, update_filters)

    # ──────────────────────────────────────────────────────────────────────────
    # Tab 4: 设置
    # ──────────────────────────────────────────────────────────────────────────

    def _build_settings_tab(self):
        frame = tk.Frame(self.notebook, bg=_BG)
        self.notebook.add(frame, text="  设置  ")

        canvas = tk.Canvas(frame, bg=_BG, highlightthickness=0)
        sb = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=_BG, padx=24, pady=20)

        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win, width=e.width))
        canvas.bind("<MouseWheel>", lambda e: scroll_canvas_on_mousewheel(canvas, e))
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        tk.Label(inner, text="程序设置", bg=_BG, fg=_TEXT_PRI,
                 font=(_FONT, 16, "bold")).pack(anchor=tk.W, pady=(0, 4))
        tk.Label(inner, text="这里统一管理每周自动查询时间、识别模式策略和搜索插件配置。",
                 bg=_BG, fg=_TEXT_TER, font=(_FONT, 9)).pack(anchor=tk.W, pady=(0, 20))

        self._recognition_api_vars = {}
        self._scheduler_setting_vars = {}
        self._scheduler_extra_vars = {}
        self._settings_search_vars = {}
        self._settings_browser_vars = {}
        self._settings_decoration_vars = {}
        recognition_cfg = self.config.get('recognition', {}) or {}
        scheduler_cfg = self.config.get('scheduler', {}) or {}
        search_cfg = self.config.get('search', {}) or {}
        decoration_theme = get_decoration_theme(self.config)
        weekly_times = normalize_weekly_times(scheduler_cfg)

        def add_entry_row(parent, row, column, label, variable):
            tk.Label(parent, text=label, bg=_SURFACE, fg=_TEXT_TER, font=(_FONT, 8)).grid(
                row=row, column=column, sticky="w", pady=(0, 4)
            )
            entry = tk.Entry(
                parent,
                textvariable=variable,
                font=(_FONT_MONO, 9),
                bg=_SURFACE2,
                fg=_TEXT_PRI,
                relief=tk.FLAT,
                highlightbackground=_BORDER2,
                highlightthickness=1,
                insertbackground=_TEXT_PRI,
            )
            entry.grid(row=row, column=column + 1, sticky="ew", padx=(6, 14), pady=(0, 4), ipady=5)
            entry.bind("<MouseWheel>", lambda e: scroll_canvas_on_mousewheel(canvas, e))
            parent.grid_columnconfigure(column + 1, weight=1)

        def add_spinbox_row(parent, row, column, label, variable, min_value, max_value):
            tk.Label(parent, text=label, bg=_SURFACE, fg=_TEXT_TER, font=(_FONT, 8)).grid(
                row=row, column=column, sticky="w", pady=(0, 4)
            )
            spin = tk.Spinbox(parent, from_=min_value, to=max_value, width=6, textvariable=variable)
            spin.grid(row=row, column=column + 1, sticky="w", padx=(6, 14), pady=(0, 4))
            spin.bind("<MouseWheel>", lambda e: scroll_canvas_on_mousewheel(canvas, e))

        scheduler_card = tk.Frame(inner, bg=_SURFACE,
                                  highlightbackground=_BORDER, highlightthickness=1)
        scheduler_card.pack(fill=tk.X, pady=(0, 8))
        scheduler_pad = tk.Frame(scheduler_card, bg=_SURFACE, padx=18, pady=14)
        scheduler_pad.pack(fill=tk.X)

        tk.Label(scheduler_pad, text="自动查询时间", bg=_SURFACE, fg=_TEXT_PRI,
                 font=(_FONT, 11, "bold")).pack(anchor=tk.W)
        tk.Label(
            scheduler_pad,
            text="查询任务只决定周几参与自动执行；具体每天几点开始，由这里统一控制。",
            bg=_SURFACE,
            fg=_TEXT_TER,
            font=(_FONT, 9),
            wraplength=560,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))

        tk.Label(
            scheduler_pad,
            text="调度通知 Webhook  ·  用于模式汇总、整轮完成汇总、实时异常提醒",
            bg=_SURFACE,
            fg=_TEXT_TER,
            font=(_FONT, 8),
        ).pack(anchor=tk.W, pady=(10, 3))
        notification_webhook_var = tk.StringVar(value=str(scheduler_cfg.get('notification_webhook_url', '')))
        notification_entry = tk.Entry(
            scheduler_pad,
            textvariable=notification_webhook_var,
            font=(_FONT_MONO, 10),
            bg=_SURFACE2,
            fg=_TEXT_PRI,
            relief=tk.FLAT,
            highlightbackground=_BORDER2,
            highlightthickness=1,
            insertbackground=_TEXT_PRI,
        )
        notification_entry.pack(fill=tk.X, ipady=7)
        notification_entry.bind("<MouseWheel>", lambda e: scroll_canvas_on_mousewheel(canvas, e))

        def _scheduler_webhook_ctx(event, entry=notification_entry):
            menu = tk.Menu(frame, tearoff=0, bg=_SURFACE2, fg=_TEXT_PRI,
                           activebackground=_ACCENT, activeforeground=_TEXT_PRI)
            menu.add_command(label="复制", command=lambda: entry.event_generate("<<CopyCompat>>"))
            menu.add_command(label="剪切", command=lambda: entry.event_generate("<<CutCompat>>"))
            menu.add_command(label="粘贴", command=lambda: entry.event_generate("<<PasteCompat>>"))
            menu.add_command(label="全选", command=lambda: entry.event_generate("<<SelectAllCompat>>"))
            menu.post(event.x_root, event.y_root)

        notification_entry.bind("<Button-3>", _scheduler_webhook_ctx)
        notification_entry.bind("<Button-2>", _scheduler_webhook_ctx)
        tk.Label(
            scheduler_pad,
            text="留空则不推送调度过程通知；这个 webhook 不会替代品牌任务自己的截图通知 webhook。",
            bg=_SURFACE,
            fg=_TEXT_TER,
            font=(_FONT, 8),
            wraplength=560,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(6, 0))
        self._scheduler_extra_vars['notification_webhook_url'] = notification_webhook_var

        scheduler_action_row = tk.Frame(scheduler_pad, bg=_SURFACE)
        scheduler_action_row.pack(fill=tk.X, pady=(8, 0))
        tk.Button(
            scheduler_action_row,
            text="发送测试消息",
            command=self._send_scheduler_test_message,
            bg=_SURFACE2,
            fg=_TEXT_PRI,
            relief=tk.FLAT,
            padx=12,
            pady=6,
            cursor="hand2",
        ).pack(side=tk.LEFT)
        tk.Label(
            scheduler_action_row,
            text="会向当前填写的调度 webhook 发送一条测试文本。",
            bg=_SURFACE,
            fg=_TEXT_TER,
            font=(_FONT, 8),
        ).pack(side=tk.LEFT, padx=(10, 0))

        auto_continue_var = tk.BooleanVar(value=bool(scheduler_cfg.get('auto_continue_after_default_failure', False)))
        tk.Checkbutton(
            scheduler_pad,
            text="默认模式失败后自动启动后续模式",
            variable=auto_continue_var,
            bg=_SURFACE,
            fg=_TEXT_PRI,
            activebackground=_SURFACE,
            activeforeground=_TEXT_PRI,
            selectcolor=_SURFACE,
        ).pack(anchor=tk.W, pady=(10, 0))
        tk.Label(
            scheduler_pad,
            text="关闭时，模式完成后只在主页消息区提醒；点击确认后才会显示后续模式选项。",
            bg=_SURFACE,
            fg=_TEXT_TER,
            font=(_FONT, 8),
            wraplength=560,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 0))
        self._scheduler_extra_vars['auto_continue_after_default_failure'] = auto_continue_var

        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        minute_values = ("00", "05", "10", "15", "20", "25", "30", "35", "40", "45", "50", "55")
        for weekday, label in enumerate(weekday_names):
            row = tk.Frame(scheduler_pad, bg=_SURFACE)
            row.pack(fill=tk.X, pady=(8 if weekday == 0 else 6, 0))

            time_text = weekly_times.get(str(weekday))
            enabled_var = tk.BooleanVar(value=bool(time_text))
            hour_text, minute_text = ("09", "30")
            if time_text:
                hour_text, minute_text = time_text.split(":")

            tk.Checkbutton(
                row,
                text=label,
                variable=enabled_var,
                bg=_SURFACE,
                fg=_TEXT_PRI,
                activebackground=_SURFACE,
                activeforeground=_TEXT_PRI,
                selectcolor=_SURFACE,
                width=8,
                anchor="w",
            ).pack(side=tk.LEFT)
            tk.Label(row, text="小时", bg=_SURFACE, fg=_TEXT_SEC, font=(_FONT, 9)).pack(side=tk.LEFT, padx=(6, 4))
            hour_var = tk.StringVar(value=hour_text)
            tk.Spinbox(
                row,
                from_=0,
                to=23,
                width=5,
                format="%02.0f",
                textvariable=hour_var,
            ).pack(side=tk.LEFT, padx=(0, 12))
            tk.Label(row, text="分钟", bg=_SURFACE, fg=_TEXT_SEC, font=(_FONT, 9)).pack(side=tk.LEFT, padx=(0, 4))
            minute_var = tk.StringVar(value=minute_text)
            tk.Spinbox(
                row,
                values=minute_values,
                width=5,
                textvariable=minute_var,
            ).pack(side=tk.LEFT)
            self._scheduler_setting_vars[str(weekday)] = {
                "enabled": enabled_var,
                "hour": hour_var,
                "minute": minute_var,
            }

        recognition_card = tk.Frame(inner, bg=_SURFACE,
                                    highlightbackground=_BORDER, highlightthickness=1)
        recognition_card.pack(fill=tk.X, pady=(0, 8))
        recognition_pad = tk.Frame(recognition_card, bg=_SURFACE, padx=18, pady=14)
        recognition_pad.pack(fill=tk.X)

        tk.Label(recognition_pad, text="识别模式", bg=_SURFACE, fg=_TEXT_PRI,
                 font=(_FONT, 11, "bold")).pack(anchor=tk.W)
        tk.Label(
            recognition_pad,
            text="这里控制识别模式是否优先使用本地 OCR，以及本地 OCR 未命中时是否启用 AI 辅助复核。",
            bg=_SURFACE,
            fg=_TEXT_TER,
            font=(_FONT, 9),
            wraplength=560,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))

        safe_mode_ocr_var = tk.BooleanVar(value=bool(recognition_cfg.get('safe_mode_ocr_enabled', True)))
        tk.Checkbutton(
            recognition_pad,
            text="启用本地 OCR 自动识别",
            variable=safe_mode_ocr_var,
            bg=_SURFACE,
            fg=_TEXT_PRI,
            activebackground=_SURFACE,
            activeforeground=_TEXT_PRI,
            selectcolor=_SURFACE,
        ).pack(anchor=tk.W, pady=(10, 0))
        self._recognition_api_vars['safe_mode_ocr_enabled'] = safe_mode_ocr_var

        ai_fallback_var = tk.BooleanVar(value=bool(recognition_cfg.get('ai_fallback_enabled', False)))
        tk.Checkbutton(
            recognition_pad,
            text="本地 OCR 未命中时启用 AI 识别辅助",
            variable=ai_fallback_var,
            bg=_SURFACE,
            fg=_TEXT_PRI,
            activebackground=_SURFACE,
            activeforeground=_TEXT_PRI,
            selectcolor=_SURFACE,
        ).pack(anchor=tk.W, pady=(8, 0))
        self._recognition_api_vars['ai_fallback_enabled'] = ai_fallback_var

        tk.Label(
            recognition_pad,
            text="关闭本地 OCR 后，识别模式会进入人工推进；开启 AI 辅助后，会在 OCR 未识别到目标品牌时再调用 AI 复核。",
            bg=_SURFACE,
            fg=_TEXT_TER,
            font=(_FONT, 8),
            wraplength=560,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))

        browser_card = tk.Frame(inner, bg=_SURFACE,
                                highlightbackground=_BORDER, highlightthickness=1)
        browser_card.pack(fill=tk.X, pady=(0, 8))
        browser_pad = tk.Frame(browser_card, bg=_SURFACE, padx=18, pady=14)
        browser_pad.pack(fill=tk.X)

        screenshot_cfg = self.config.get('screenshot', {}) or {}
        browser_answer_mode = str(screenshot_cfg.get('browser_answer_mode', 'page') or 'page').strip().lower()
        browser_answer_mode_label = next(
            (label for label, code in _BROWSER_ANSWER_MODE_OPTIONS.items() if code == browser_answer_mode),
            "页面原始截图",
        )
        self._settings_browser_vars = {
            'answer_mode': tk.StringVar(value=browser_answer_mode_label),
        }

        tk.Label(browser_pad, text="抓取模式", bg=_SURFACE, fg=_TEXT_PRI,
                 font=(_FONT, 11, "bold")).pack(anchor=tk.W)
        tk.Label(
            browser_pad,
            text="这里控制抓取模式命中后的出图方式。页面原始截图会继续走下方的页面截图装饰模板；DOM 文本生成会固定走 Surfaced 模版本地重排。",
            bg=_SURFACE,
            fg=_TEXT_TER,
            font=(_FONT, 9),
            wraplength=560,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))
        tk.Label(
            browser_pad,
            text="回答生成方式",
            bg=_SURFACE,
            fg=_TEXT_PRI,
            font=(_FONT, 10, "bold"),
        ).pack(anchor=tk.W, pady=(12, 0))
        tk.Label(
            browser_pad,
            text="“DOM 文本生成”会直接提取回答区 HTML，并固定使用 Surfaced 模版重排；下方装饰模板仅作用于“页面原始截图”。",
            bg=_SURFACE,
            fg=_TEXT_TER,
            font=(_FONT, 8),
            wraplength=560,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 6))
        browser_mode_picker = ttk.Combobox(
            browser_pad,
            textvariable=self._settings_browser_vars['answer_mode'],
            values=list(_BROWSER_ANSWER_MODE_OPTIONS.keys()),
            state="readonly",
            width=20,
        )
        browser_mode_picker.pack(anchor=tk.W)
        browser_mode_picker.bind("<MouseWheel>", lambda e: scroll_canvas_on_mousewheel(canvas, e))

        search_card = tk.Frame(inner, bg=_SURFACE,
                               highlightbackground=_BORDER, highlightthickness=1)
        search_card.pack(fill=tk.X, pady=(0, 8))
        search_pad = tk.Frame(search_card, bg=_SURFACE, padx=18, pady=14)
        search_pad.pack(fill=tk.X)

        tk.Label(search_pad, text="搜索插件配置", bg=_SURFACE, fg=_TEXT_PRI,
                 font=(_FONT, 11, "bold")).pack(anchor=tk.W)
        tk.Label(
            search_pad,
            text="联网搜索桥接优先走 Tavily；填写后，搜搜和保险模式的搜索兜底都会优先使用它。",
            bg=_SURFACE,
            fg=_TEXT_TER,
            font=(_FONT, 9),
            wraplength=560,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))

        tk.Label(search_pad, text="API KEY  ·  Tavily Bearer Token（以 tvly- 开头）",
                 bg=_SURFACE, fg=_TEXT_TER, font=(_FONT, 8)).pack(anchor=tk.W, pady=(8, 3))
        tavily_key_var = tk.StringVar(value=search_cfg.get('tavily_api_key', ''))
        tavily_entry = tk.Entry(search_pad, textvariable=tavily_key_var, show="•",
                                font=(_FONT_MONO, 10),
                                bg=_SURFACE2, fg=_TEXT_PRI,
                                relief=tk.FLAT,
                                highlightbackground=_BORDER2, highlightthickness=1,
                                insertbackground=_TEXT_PRI)
        tavily_entry.pack(fill=tk.X, ipady=7)
        tavily_entry.bind("<MouseWheel>", lambda e: scroll_canvas_on_mousewheel(canvas, e))

        def _search_ctx(event, e=tavily_entry):
            m = tk.Menu(frame, tearoff=0, bg=_SURFACE2, fg=_TEXT_PRI,
                        activebackground=_ACCENT, activeforeground=_TEXT_PRI)
            m.add_command(label="复制", command=lambda: e.event_generate("<<CopyCompat>>"))
            m.add_command(label="剪切", command=lambda: e.event_generate("<<CutCompat>>"))
            m.add_command(label="粘贴", command=lambda: e.event_generate("<<PasteCompat>>"))
            m.add_command(label="全选", command=lambda: e.event_generate("<<SelectAllCompat>>"))
            m.post(event.x_root, event.y_root)
        tavily_entry.bind("<Button-3>", _search_ctx)
        tavily_entry.bind("<Button-2>", _search_ctx)

        tk.Label(
            search_pad,
            text="未填写时，会自动回退到内置公开搜索桥接。",
            bg=_SURFACE,
            fg=_TEXT_TER,
            font=(_FONT, 8),
            wraplength=560,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))

        self._settings_search_vars = {
            'provider': tk.StringVar(value=search_cfg.get('provider', 'tavily') or 'tavily'),
            'tavily_api_key': tavily_key_var,
        }

        decoration_card = tk.Frame(inner, bg=_SURFACE,
                                   highlightbackground=_BORDER, highlightthickness=1)
        decoration_card.pack(fill=tk.X, pady=(0, 8))
        decoration_pad = tk.Frame(decoration_card, bg=_SURFACE, padx=18, pady=14)
        decoration_pad.pack(fill=tk.X)

        tk.Label(decoration_pad, text="页面原始截图装饰模板", bg=_SURFACE, fg=_TEXT_PRI,
                 font=(_FONT, 11, "bold")).pack(anchor=tk.W)
        tk.Label(
            decoration_pad,
            text="这里只作用于“页面原始截图”。DOM 文本生成会固定走 Surfaced 模版，不会套用这里的旧装饰样式。",
            bg=_SURFACE,
            fg=_TEXT_TER,
            font=(_FONT, 9),
            wraplength=560,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))

        self._settings_decoration_vars = {
            'enabled': tk.BooleanVar(value=bool(decoration_theme.get('enabled', True))),
            'title': tk.StringVar(value=str(decoration_theme.get('title', ''))),
            'subtitle': tk.StringVar(value=str(decoration_theme.get('subtitle', ''))),
            'footer': tk.StringVar(value=str(decoration_theme.get('footer', ''))),
            'show_timestamp': tk.BooleanVar(value=bool(decoration_theme.get('show_timestamp', True))),
            'show_footer': tk.BooleanVar(value=bool(decoration_theme.get('show_footer', True))),
            'draw_highlight_boxes': tk.BooleanVar(value=bool(decoration_theme.get('draw_highlight_boxes', True))),
            'accent_color': tk.StringVar(value=str(decoration_theme.get('accent_color', '#14C7F3'))),
            'background_start': tk.StringVar(value=str((decoration_theme.get('background', {}) or {}).get('start', '#FCFDFF'))),
            'background_end': tk.StringVar(value=str((decoration_theme.get('background', {}) or {}).get('end', '#F7FAFF'))),
            'header_start': tk.StringVar(value=str((decoration_theme.get('header', {}) or {}).get('start', '#173A43'))),
            'header_end': tk.StringVar(value=str((decoration_theme.get('header', {}) or {}).get('end', '#14C7F3'))),
            'outer_padding': tk.IntVar(value=int((decoration_theme.get('layout', {}) or {}).get('outer_padding', 28))),
            'header_height': tk.IntVar(value=int((decoration_theme.get('layout', {}) or {}).get('header_height', 136))),
            'radius': tk.IntVar(value=int((decoration_theme.get('layout', {}) or {}).get('radius', 28))),
            'image_radius': tk.IntVar(value=int((decoration_theme.get('layout', {}) or {}).get('image_radius', 22))),
        }

        tk.Checkbutton(
            decoration_pad,
            text="启用页面截图装饰模板",
            variable=self._settings_decoration_vars['enabled'],
            bg=_SURFACE,
            fg=_TEXT_PRI,
            activebackground=_SURFACE,
            activeforeground=_TEXT_PRI,
            selectcolor=_SURFACE,
        ).pack(anchor=tk.W, pady=(10, 0))

        tk.Label(decoration_pad, text="标题  ·  支持 {brand} / {platform} / {keyword} / {time}",
                 bg=_SURFACE, fg=_TEXT_TER, font=(_FONT, 8)).pack(anchor=tk.W, pady=(8, 3))
        title_entry = tk.Entry(
            decoration_pad,
            textvariable=self._settings_decoration_vars['title'],
            font=(_FONT, 10),
            bg=_SURFACE2, fg=_TEXT_PRI,
            relief=tk.FLAT, highlightbackground=_BORDER2, highlightthickness=1,
            insertbackground=_TEXT_PRI,
        )
        title_entry.pack(fill=tk.X, ipady=6)
        title_entry.bind("<MouseWheel>", lambda e: scroll_canvas_on_mousewheel(canvas, e))

        tk.Label(decoration_pad, text="副标题", bg=_SURFACE, fg=_TEXT_TER, font=(_FONT, 8)).pack(anchor=tk.W, pady=(8, 3))
        subtitle_entry = tk.Entry(
            decoration_pad,
            textvariable=self._settings_decoration_vars['subtitle'],
            font=(_FONT, 10),
            bg=_SURFACE2, fg=_TEXT_PRI,
            relief=tk.FLAT, highlightbackground=_BORDER2, highlightthickness=1,
            insertbackground=_TEXT_PRI,
        )
        subtitle_entry.pack(fill=tk.X, ipady=6)
        subtitle_entry.bind("<MouseWheel>", lambda e: scroll_canvas_on_mousewheel(canvas, e))

        tk.Label(decoration_pad, text="页脚文案", bg=_SURFACE, fg=_TEXT_TER, font=(_FONT, 8)).pack(anchor=tk.W, pady=(8, 3))
        footer_entry = tk.Entry(
            decoration_pad,
            textvariable=self._settings_decoration_vars['footer'],
            font=(_FONT, 10),
            bg=_SURFACE2, fg=_TEXT_PRI,
            relief=tk.FLAT, highlightbackground=_BORDER2, highlightthickness=1,
            insertbackground=_TEXT_PRI,
        )
        footer_entry.pack(fill=tk.X, ipady=6)
        footer_entry.bind("<MouseWheel>", lambda e: scroll_canvas_on_mousewheel(canvas, e))

        toggles = tk.Frame(decoration_pad, bg=_SURFACE)
        toggles.pack(fill=tk.X, pady=(10, 8))
        for label, key in (
            ("显示时间", 'show_timestamp'),
            ("显示页脚", 'show_footer'),
            ("绘制高亮框", 'draw_highlight_boxes'),
        ):
            tk.Checkbutton(
                toggles,
                text=label,
                variable=self._settings_decoration_vars[key],
                bg=_SURFACE,
                fg=_TEXT_PRI,
                activebackground=_SURFACE,
                activeforeground=_TEXT_PRI,
                selectcolor=_SURFACE,
            ).pack(side=tk.LEFT, padx=(0, 12))

        color_grid = tk.Frame(decoration_pad, bg=_SURFACE)
        color_grid.pack(fill=tk.X, pady=(0, 6))
        add_entry_row(color_grid, 0, 0, "强调色", self._settings_decoration_vars['accent_color'])
        add_entry_row(color_grid, 0, 2, "背景起始", self._settings_decoration_vars['background_start'])
        add_entry_row(color_grid, 1, 0, "背景结束", self._settings_decoration_vars['background_end'])
        add_entry_row(color_grid, 1, 2, "头部起始", self._settings_decoration_vars['header_start'])
        add_entry_row(color_grid, 2, 0, "头部结束", self._settings_decoration_vars['header_end'])

        layout_grid = tk.Frame(decoration_pad, bg=_SURFACE)
        layout_grid.pack(fill=tk.X, pady=(2, 0))
        add_spinbox_row(layout_grid, 0, 0, "外边距", self._settings_decoration_vars['outer_padding'], 12, 80)
        add_spinbox_row(layout_grid, 0, 2, "头部高度", self._settings_decoration_vars['header_height'], 88, 240)
        add_spinbox_row(layout_grid, 1, 0, "卡片圆角", self._settings_decoration_vars['radius'], 12, 48)
        add_spinbox_row(layout_grid, 1, 2, "图片圆角", self._settings_decoration_vars['image_radius'], 8, 36)

        def reset_decoration_defaults():
            theme = get_default_decoration_theme()
            layout = (theme.get('layout', {}) or {})
            background = (theme.get('background', {}) or {})
            header = (theme.get('header', {}) or {})
            values = {
                'enabled': bool(theme.get('enabled', True)),
                'title': str(theme.get('title', '')),
                'subtitle': str(theme.get('subtitle', '')),
                'footer': str(theme.get('footer', '')),
                'show_timestamp': bool(theme.get('show_timestamp', True)),
                'show_footer': bool(theme.get('show_footer', True)),
                'draw_highlight_boxes': bool(theme.get('draw_highlight_boxes', True)),
                'accent_color': str(theme.get('accent_color', '#14C7F3')),
                'background_start': str(background.get('start', '#FCFDFF')),
                'background_end': str(background.get('end', '#F7FAFF')),
                'header_start': str(header.get('start', '#173A43')),
                'header_end': str(header.get('end', '#14C7F3')),
                'outer_padding': int(layout.get('outer_padding', 28)),
                'header_height': int(layout.get('header_height', 136)),
                'radius': int(layout.get('radius', 28)),
                'image_radius': int(layout.get('image_radius', 22)),
            }
            for key, value in values.items():
                self._settings_decoration_vars[key].set(value)

        decoration_actions = tk.Frame(decoration_pad, bg=_SURFACE)
        decoration_actions.pack(fill=tk.X, pady=(10, 0))
        tk.Button(
            decoration_actions,
            text="恢复默认模板",
            command=reset_decoration_defaults,
            bg=_SURFACE2,
            fg=_TEXT_PRI,
            relief=tk.FLAT,
            padx=12,
            pady=6,
            cursor="hand2",
        ).pack(side=tk.LEFT)
        tk.Label(
            decoration_actions,
            text="保存后，后续“页面原始截图”会默认使用这里的样式。",
            bg=_SURFACE,
            fg=_TEXT_TER,
            font=(_FONT, 8),
        ).pack(side=tk.LEFT, padx=(10, 0))

        btn_frame = tk.Frame(inner, bg=_BG)
        btn_frame.pack(fill=tk.X, pady=(20, 0))
        tk.Button(btn_frame, text="保存设置",
                  command=self._save_settings,
                  bg=_ACCENT, fg=_TEXT_PRI,
                  font=(_FONT, 10, "bold"),
                  relief=tk.FLAT, padx=22, pady=9,
                  cursor="hand2",
                  activebackground="#3558d4",
                  activeforeground=_TEXT_PRI).pack(side=tk.RIGHT)
        bind_mousewheel_recursive(inner, lambda e: scroll_canvas_on_mousewheel(canvas, e))

    def _build_api_tab(self):
        frame = tk.Frame(self.notebook, bg=_BG)
        self.notebook.add(frame, text="  API  ")

        from ui.api_config import (
            iter_platform_groups,
            PLATFORM_LABELS,
            get_config_model_options,
            normalize_model_options,
            run_platform_api_test,
        )

        canvas = tk.Canvas(frame, bg=_BG, highlightthickness=0)
        sb = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=_BG, padx=24, pady=20)

        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0,0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win, width=e.width))
        canvas.bind("<MouseWheel>", lambda e: scroll_canvas_on_mousewheel(canvas, e))
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        tk.Label(inner, text="API KEY 配置", bg=_BG, fg=_TEXT_PRI,
                 font=(_FONT, 16, "bold")).pack(anchor=tk.W, pady=(0, 4))
        tk.Label(inner, text="这里只负责各平台 API Key 与模型。搜索插件、OCR 和调度时间请到“设置”里配置。",
                 bg=_BG, fg=_TEXT_TER, font=(_FONT, 9)).pack(anchor=tk.W, pady=(0, 20))

        self._api_vars = {}
        platforms_cfg = self.config.get('platforms', {})

        def on_scroll(e):
            return scroll_canvas_on_mousewheel(canvas, e)

        for section_title, section_items in iter_platform_groups():
            section = tk.Frame(inner, bg=_BG)
            section.pack(fill=tk.X, pady=(0, 10))

            tk.Label(
                section,
                text=section_title,
                bg=_BG,
                fg=_TEXT_PRI,
                font=(_FONT, 12, "bold"),
            ).pack(anchor=tk.W, pady=(4, 6))

            for code, name, hint, has_api in section_items:
                card = tk.Frame(section, bg=_SURFACE,
                                highlightbackground=_BORDER, highlightthickness=1)
                card.pack(fill=tk.X, pady=(0, 8))
                card.bind("<MouseWheel>", on_scroll)

                pad = tk.Frame(card, bg=_SURFACE, padx=18, pady=14)
                pad.pack(fill=tk.X)

                # 平台名（大字重）
                tk.Label(pad, text=name, bg=_SURFACE, fg=_TEXT_PRI,
                         font=(_FONT, 11, "bold")).pack(anchor=tk.W)

                if not has_api:
                    tk.Label(pad, text=hint, bg=_SURFACE, fg=_TEXT_TER,
                             font=(_FONT, 9)).pack(anchor=tk.W, pady=(6, 0))
                    self._api_vars[code] = {
                        'api_key': tk.StringVar(),
                        'api_model': tk.StringVar(),
                        'model_options': [],
                        'test_status': tk.StringVar(value=''),
                    }
                    continue

                tk.Label(pad, text=f"API KEY  ·  {hint}", bg=_SURFACE, fg=_TEXT_TER,
                         font=(_FONT, 8)).pack(anchor=tk.W, pady=(8, 3))
                key_var = tk.StringVar(value=platforms_cfg.get(code, {}).get('api_key', ''))
                key_entry = tk.Entry(pad, textvariable=key_var, show="•",
                                     font=(_FONT_MONO, 10),
                                     bg=_SURFACE2, fg=_TEXT_PRI,
                                     relief=tk.FLAT,
                                     highlightbackground=_BORDER2, highlightthickness=1,
                                     insertbackground=_TEXT_PRI)
                key_entry.pack(fill=tk.X, ipady=7)
                key_entry.bind("<MouseWheel>", on_scroll)

                def _ctx(event, e=key_entry):
                    m = tk.Menu(frame, tearoff=0, bg=_SURFACE2, fg=_TEXT_PRI,
                                activebackground=_ACCENT, activeforeground=_TEXT_PRI)
                    m.add_command(label="复制", command=lambda: e.event_generate("<<CopyCompat>>"))
                    m.add_command(label="剪切", command=lambda: e.event_generate("<<CutCompat>>"))
                    m.add_command(label="粘贴", command=lambda: e.event_generate("<<PasteCompat>>"))
                    m.add_command(label="全选", command=lambda: e.event_generate("<<SelectAllCompat>>"))
                    m.post(event.x_root, event.y_root)
                key_entry.bind("<Button-3>", _ctx)
                key_entry.bind("<Button-2>", _ctx)

                model_var = tk.StringVar(value=platforms_cfg.get(code, {}).get('api_model', ''))
                model_options = get_config_model_options(self.config, code, model_var.get().strip())
                model_row = tk.Frame(pad, bg=_SURFACE)
                model_row.pack(fill=tk.X, pady=(10, 0))
                if code == 'ark_deepseek':
                    model_text = "推理接入点 ID / 模型标识（建议填 Endpoint ID，如 ep-xxxx，不要填页面展示名）"
                else:
                    model_text = "模型  （手填或从自定义列表中选，建议显式填写）"
                tk.Label(model_row, text=model_text,
                         bg=_SURFACE, fg=_TEXT_TER, font=(_FONT, 8)).pack(side=tk.LEFT)
                model_combo = ttk.Combobox(
                    model_row,
                    textvariable=model_var,
                    values=model_options,
                    width=28,
                    state="normal",
                )
                model_combo.pack(side=tk.LEFT, padx=(10, 0), ipady=5)

                def _model_ctx(event, e=model_combo):
                    m = tk.Menu(frame, tearoff=0, bg=_SURFACE2, fg=_TEXT_PRI,
                                activebackground=_ACCENT, activeforeground=_TEXT_PRI)
                    m.add_command(label="复制", command=lambda: e.event_generate("<<CopyCompat>>"))
                    m.add_command(label="剪切", command=lambda: e.event_generate("<<CutCompat>>"))
                    m.add_command(label="粘贴", command=lambda: e.event_generate("<<PasteCompat>>"))
                    m.add_command(label="全选", command=lambda: e.event_generate("<<SelectAllCompat>>"))
                    m.post(event.x_root, event.y_root)
                model_combo.bind("<Button-3>", _model_ctx)
                model_combo.bind("<Button-2>", _model_ctx)

                model_btn_row = tk.Frame(pad, bg=_SURFACE)
                model_btn_row.pack(fill=tk.X, pady=(6, 0))
                ttk.Button(
                    model_btn_row,
                    text="新增模型名",
                    command=lambda c=code: self._add_api_model_option(c),
                ).pack(side=tk.LEFT)
                ttk.Button(
                    model_btn_row,
                    text="删除当前模型名",
                    command=lambda c=code: self._remove_api_model_option(c),
                ).pack(side=tk.LEFT, padx=(8, 0))
                test_btn = ttk.Button(
                    model_btn_row,
                    text="测试",
                    command=lambda c=code: self._test_api_platform(c),
                )
                test_btn.pack(side=tk.LEFT, padx=(8, 0))
                test_status = tk.StringVar(value="")
                tk.Label(
                    model_btn_row,
                    text="列表由你维护，程序不再内置固定候选。",
                    bg=_SURFACE,
                    fg=_TEXT_TER,
                    font=(_FONT, 8),
                ).pack(side=tk.LEFT, padx=(10, 0))
                tk.Label(
                    model_btn_row,
                    textvariable=test_status,
                    bg=_SURFACE,
                    fg=_TEXT_TER,
                    font=(_FONT, 8),
                ).pack(side=tk.RIGHT)

                tk.Label(
                    pad,
                    text="联网搜索默认启用：优先走原生搜索，失败时自动兜底。",
                    bg=_SURFACE,
                    fg=_TEXT_TER,
                    font=(_FONT, 9),
                ).pack(anchor=tk.W, pady=(10, 0))

                if code == 'wenxin':
                    tk.Label(
                        pad,
                        text="提示：文心使用 Bearer Token 时优先走原生搜索；旧 ACCESS_KEY|SECRET_KEY 会自动退回兼容模式。",
                        bg=_SURFACE,
                        fg=_TEXT_TER,
                        font=(_FONT, 8),
                        wraplength=560,
                        justify=tk.LEFT,
                    ).pack(anchor=tk.W, pady=(6, 0))
                elif code == 'ark_deepseek':
                    tk.Label(
                        pad,
                        text="提示：方舟 DeepSeek 可以直接复用豆包的 API Key，模型单独填 Endpoint ID（通常形如 ep-xxxx）即可；联网搜索会优先走火山方舟原生 web_search。",
                        bg=_SURFACE,
                        fg=_TEXT_TER,
                        font=(_FONT, 8),
                        wraplength=560,
                        justify=tk.LEFT,
                    ).pack(anchor=tk.W, pady=(6, 0))

                self._api_vars[code] = {
                    'api_key': key_var,
                    'api_model': model_var,
                    'model_options': model_options,
                    'model_combo': model_combo,
                    'test_button': test_btn,
                    'test_status': test_status,
                    'platform_label': PLATFORM_LABELS.get(code, code),
                }

        btn_frame = tk.Frame(inner, bg=_BG)
        btn_frame.pack(fill=tk.X, pady=(20, 0))
        tk.Button(btn_frame, text="保存 API 配置",
                  command=self._save_api,
                  bg=_ACCENT, fg=_TEXT_PRI,
                  font=(_FONT, 10, "bold"),
                  relief=tk.FLAT, padx=22, pady=9,
                  cursor="hand2",
                  activebackground="#3558d4",
                  activeforeground=_TEXT_PRI).pack(side=tk.RIGHT)
        bind_mousewheel_recursive(inner, on_scroll)

    def _save_api(self):
        from ui.api_config import normalize_model_options
        if 'platforms' not in self.config:
            self.config['platforms'] = {}
        for code, d in self._api_vars.items():
            self.config['platforms'].setdefault(code, {})
            self.config['platforms'][code]['api_key'] = d['api_key'].get().strip()
            model = d['api_model'].get().strip()
            if model:
                self.config['platforms'][code]['api_model'] = model
            elif 'api_model' in self.config['platforms'].get(code, {}):
                del self.config['platforms'][code]['api_model']
            model_options = normalize_model_options(d.get('model_options', []))
            if model_options:
                self.config['platforms'][code]['model_options'] = model_options
            elif 'model_options' in self.config['platforms'].get(code, {}):
                del self.config['platforms'][code]['model_options']
        self._persist_runtime_config("API Key 已保存")

    def _save_settings(self):
        self.config.setdefault('scheduler', {})
        weekly_times = {}
        for weekday in range(7):
            vars_for_day = self._scheduler_setting_vars.get(str(weekday), {})
            enabled_var = vars_for_day.get('enabled', tk.BooleanVar(value=False))
            if not enabled_var.get():
                weekly_times[str(weekday)] = None
                continue
            hour = str(vars_for_day.get('hour', tk.StringVar(value='09')).get()).zfill(2)
            minute = str(vars_for_day.get('minute', tk.StringVar(value='30')).get()).zfill(2)
            weekly_times[str(weekday)] = f"{hour}:{minute}"
        self.config['scheduler']['weekly_times'] = weekly_times
        self.config['scheduler']['auto_continue_after_default_failure'] = bool(
            self._scheduler_extra_vars.get(
                'auto_continue_after_default_failure',
                tk.BooleanVar(value=False),
            ).get()
        )
        self.config['scheduler']['notification_webhook_url'] = self._scheduler_extra_vars.get(
            'notification_webhook_url',
            tk.StringVar(value=''),
        ).get().strip()
        self.config.setdefault('recognition', {})
        self.config['recognition']['safe_mode_ocr_enabled'] = bool(
            self._recognition_api_vars.get('safe_mode_ocr_enabled', tk.BooleanVar(value=True)).get()
        )
        self.config['recognition']['ai_fallback_enabled'] = bool(
            self._recognition_api_vars.get('ai_fallback_enabled', tk.BooleanVar(value=False)).get()
        )
        self.config.setdefault('search', {})
        self.config['search']['provider'] = self._settings_search_vars.get(
            'provider', tk.StringVar(value='tavily')
        ).get().strip() or 'tavily'
        self.config['search']['tavily_api_key'] = self._settings_search_vars.get(
            'tavily_api_key', tk.StringVar(value='')
        ).get().strip()
        self.config.setdefault('screenshot', {})
        self.config['screenshot']['browser_answer_mode'] = _BROWSER_ANSWER_MODE_OPTIONS.get(
            str(self._settings_browser_vars.get('answer_mode', tk.StringVar(value='页面原始截图')).get() or '').strip(),
            'page',
        )
        self.config['screenshot']['decoration'] = {
            'enabled': bool(self._settings_decoration_vars.get('enabled', tk.BooleanVar(value=True)).get()),
            'title': self._settings_decoration_vars.get('title', tk.StringVar(value='')).get().strip(),
            'subtitle': self._settings_decoration_vars.get('subtitle', tk.StringVar(value='')).get().strip(),
            'footer': self._settings_decoration_vars.get('footer', tk.StringVar(value='')).get().strip(),
            'show_timestamp': bool(self._settings_decoration_vars.get('show_timestamp', tk.BooleanVar(value=True)).get()),
            'show_footer': bool(self._settings_decoration_vars.get('show_footer', tk.BooleanVar(value=True)).get()),
            'draw_highlight_boxes': bool(self._settings_decoration_vars.get('draw_highlight_boxes', tk.BooleanVar(value=True)).get()),
            'accent_color': self._settings_decoration_vars.get('accent_color', tk.StringVar(value='#14C7F3')).get().strip(),
            'background': {
                'start': self._settings_decoration_vars.get('background_start', tk.StringVar(value='#FCFDFF')).get().strip(),
                'end': self._settings_decoration_vars.get('background_end', tk.StringVar(value='#F7FAFF')).get().strip(),
            },
            'header': {
                'start': self._settings_decoration_vars.get('header_start', tk.StringVar(value='#173A43')).get().strip(),
                'end': self._settings_decoration_vars.get('header_end', tk.StringVar(value='#14C7F3')).get().strip(),
            },
            'layout': {
                'outer_padding': int(self._settings_decoration_vars.get('outer_padding', tk.IntVar(value=28)).get()),
                'header_height': int(self._settings_decoration_vars.get('header_height', tk.IntVar(value=136)).get()),
                'radius': int(self._settings_decoration_vars.get('radius', tk.IntVar(value=28)).get()),
                'image_radius': int(self._settings_decoration_vars.get('image_radius', tk.IntVar(value=22)).get()),
            },
        }
        self._persist_runtime_config("设置已保存")

    def _send_scheduler_test_message(self):
        webhook_url = self._scheduler_extra_vars.get(
            'notification_webhook_url',
            tk.StringVar(value=''),
        ).get().strip()
        send_interval = int((self.config.get('default_notification', {}) or {}).get('send_interval', 1) or 1)
        ok, error = send_scheduler_test_message(webhook_url, send_interval=send_interval)
        if ok:
            messagebox.showinfo("发送成功", "测试消息已发送，请到企业微信里确认是否收到。")
            return
        messagebox.showerror("发送失败", error or "测试消息发送失败")

    def _refresh_api_model_combo(self, code):
        from ui.api_config import normalize_model_options

        data = self._api_vars.get(code)
        if not data:
            return
        combo = data.get('model_combo')
        if combo is not None:
            combo['values'] = normalize_model_options(data.get('model_options', []), data['api_model'].get().strip())

    def _add_api_model_option(self, code):
        from ui.api_config import normalize_model_options

        data = self._api_vars.get(code)
        if not data:
            return
        model = data['api_model'].get().strip()
        if not model:
            messagebox.showwarning("提示", "请先输入模型名")
            return
        data['model_options'] = normalize_model_options(data.get('model_options', []), model)
        self._refresh_api_model_combo(code)

    def _remove_api_model_option(self, code):
        data = self._api_vars.get(code)
        if not data:
            return
        model = data['api_model'].get().strip()
        if not model:
            messagebox.showwarning("提示", "请先输入或选中要删除的模型名")
            return
        options = [item for item in data.get('model_options', []) if item != model]
        if len(options) == len(data.get('model_options', [])):
            messagebox.showinfo("提示", "当前模型名不在自定义列表里")
            return
        data['model_options'] = options
        self._refresh_api_model_combo(code)

    def _set_api_test_ui_state(self, code, status_text="", busy=False):
        data = self._api_vars.get(code)
        if not data:
            return
        status_var = data.get('test_status')
        if status_var is not None:
            status_var.set(status_text)
        button = data.get('test_button')
        if button is not None:
            button.config(state=(tk.DISABLED if busy else tk.NORMAL))

    def _test_api_platform(self, code):
        from ui.api_config import run_platform_api_test

        data = self._api_vars.get(code)
        if not data:
            return

        api_key = data['api_key'].get().strip()
        model = data['api_model'].get().strip()
        platform_label = data.get('platform_label', code)
        self._set_api_test_ui_state(code, "测试中...", busy=True)

        def worker():
            try:
                result = run_platform_api_test(code, api_key, model, config=self.config)
                preview = result if len(result) <= 120 else result[:120] + "..."
                self.root.after(0, lambda: (
                    self._set_api_test_ui_state(code, "测试成功", busy=False),
                    messagebox.showinfo(
                        "测试成功",
                        f"{platform_label} 接口可用。\n\n返回内容：\n{preview}",
                    ),
                ))
            except Exception as e:
                error_text = str(e).strip() or e.__class__.__name__
                self.root.after(0, lambda: (
                    self._set_api_test_ui_state(code, "测试失败", busy=False),
                    messagebox.showerror(
                        "测试失败",
                        f"{platform_label} 测试失败：{error_text}",
                    ),
                ))

        threading.Thread(target=worker, daemon=True).start()

    # ──────────────────────────────────────────────────────────────────────────
    # Tab 5: 日志
    # ──────────────────────────────────────────────────────────────────────────

    def _build_log_tab(self):
        frame = tk.Frame(self.notebook, bg=_BG)
        self.notebook.add(frame, text="  日志  ")

        toolbar = tk.Frame(frame, bg=_BG)
        toolbar.pack(fill=tk.X, padx=24, pady=(20, 0))
        self._toolbar_btn(toolbar, "刷新日志", self._refresh_log)
        self._toolbar_btn(toolbar, "清空日志", self._clear_log)

        self._log_text = scrolledtext.ScrolledText(
            frame, wrap=tk.WORD,
            font=(_FONT_MONO, 9),
            state=tk.DISABLED,
            bg=_SURFACE, fg=_TEXT_SEC,
            relief=tk.FLAT,
            highlightbackground=_BORDER, highlightthickness=1,
            padx=20, pady=16,
            insertbackground=_TEXT_PRI,
            selectbackground=_ACCENT)
        self._log_text.pack(fill=tk.BOTH, expand=True, padx=24, pady=(16, 20))
        self._refresh_log()

    def _refresh_log(self):
        log_path = resolve_app_path("logs/monitor.log")
        self._log_text.config(state=tk.NORMAL)
        self._log_text.delete(1.0, tk.END)
        if log_path.exists():
            try:
                with open(log_path, 'r', encoding='utf-8') as f:
                    self._log_text.insert(tk.END, f.read())
                self._log_text.see(tk.END)
            except Exception as e:
                self._log_text.insert(tk.END, f"读取日志失败: {e}")
        else:
            self._log_text.insert(tk.END, "日志文件不存在\n")
        self._log_text.config(state=tk.DISABLED)

    def _clear_log(self):
        if messagebox.askyesno("确认", "确定要清空日志文件吗？"):
            log_path = resolve_app_path("logs/monitor.log")
            try:
                log_path.write_text("", encoding='utf-8')
                self._refresh_log()
            except Exception as e:
                messagebox.showerror("错误", f"清空失败: {e}")

    # ──────────────────────────────────────────────────────────────────────────
    # Tab 6: 手动复核
    # ──────────────────────────────────────────────────────────────────────────

    def _build_review_tab(self):
        frame = tk.Frame(self.notebook, bg=_BG)
        self.notebook.add(frame, text="  复核  ")

        toolbar = tk.Frame(frame, bg=_BG)
        toolbar.pack(fill=tk.X, padx=24, pady=(20, 0))
        self._toolbar_btn(toolbar, "刷新待复核", self._refresh_review_tab)
        self._toolbar_btn(toolbar, "通过", lambda: self._apply_review_decision("approved"), accent=True)
        self._toolbar_btn(toolbar, "误报", lambda: self._apply_review_decision("rejected"))

        body = tk.Frame(frame, bg=_BG)
        body.pack(fill=tk.BOTH, expand=True, padx=24, pady=(16, 20))

        left = tk.Frame(body, bg=_BG)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 16))
        tk.Label(left, text="待复核提及", bg=_BG, fg=_TEXT_TER, font=(_FONT, 8)).pack(anchor=tk.W, pady=(0, 6))
        self._review_list = tk.Listbox(
            left,
            width=34,
            height=24,
            bg=_SURFACE,
            fg=_TEXT_PRI,
            selectbackground=_ACCENT,
            selectforeground=_TEXT_PRI,
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground=_BORDER,
            font=(_FONT, 9),
        )
        self._review_list.pack(fill=tk.Y, expand=False)
        self._review_list.bind("<<ListboxSelect>>", lambda e: self._render_review_detail())

        right = tk.Frame(body, bg=_BG)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._review_meta = scrolledtext.ScrolledText(
            right,
            wrap=tk.WORD,
            height=10,
            bg=_SURFACE,
            fg=_TEXT_PRI,
            relief=tk.FLAT,
            highlightbackground=_BORDER,
            highlightthickness=1,
            padx=14,
            pady=12,
            font=(_FONT_MONO, 9),
        )
        self._review_meta.pack(fill=tk.X, pady=(0, 12))
        self._review_meta.config(state=tk.DISABLED)

        tk.Label(right, text="复核备注", bg=_BG, fg=_TEXT_TER, font=(_FONT, 8)).pack(anchor=tk.W, pady=(0, 6))
        self._review_note = tk.Entry(
            right,
            bg=_SURFACE2,
            fg=_TEXT_PRI,
            relief=tk.FLAT,
            insertbackground=_TEXT_PRI,
            highlightbackground=_BORDER2,
            highlightthickness=1,
            font=(_FONT, 9),
        )
        self._review_note.pack(fill=tk.X, ipady=6, pady=(0, 12))

        self._review_image = tk.Label(
            right,
            text="选中左侧记录后，这里会显示截图预览",
            bg=_SURFACE,
            fg=_TEXT_TER,
            relief=tk.FLAT,
            highlightbackground=_BORDER,
            highlightthickness=1,
            anchor="center",
            justify="center",
        )
        self._review_image.pack(fill=tk.BOTH, expand=True)
        self._refresh_review_tab()

    def _refresh_review_tab(self):
        from core.history import get_pending_reviews

        self._review_items = get_pending_reviews(limit=300)
        self._review_list.delete(0, tk.END)
        for item in self._review_items:
            label = f"{item.get('ts', '')} | {item.get('task_name', '')} | {item.get('brand', '')}"
            self._review_list.insert(tk.END, label)
        if self._review_items:
            self._review_list.selection_clear(0, tk.END)
            self._review_list.selection_set(0)
            self._render_review_detail()
        else:
            self._render_review_detail()

    def _selected_review_item(self):
        sel = self._review_list.curselection()
        if not sel:
            return None
        idx = sel[0]
        if idx >= len(self._review_items):
            return None
        return self._review_items[idx]

    def _render_review_detail(self):
        item = self._selected_review_item()
        self._review_meta.config(state=tk.NORMAL)
        self._review_meta.delete(1.0, tk.END)
        self._review_note.delete(0, tk.END)
        self._review_preview_photo = None
        if not item:
            self._review_meta.insert(tk.END, "暂无待复核记录")
            self._review_meta.config(state=tk.DISABLED)
            self._review_image.config(image="", text="暂无截图预览")
            return

        lines = [
            f"时间: {item.get('ts', '')}",
            f"任务: {item.get('task_name', '')}",
            f"平台: {item.get('platform', '')}",
            f"关键词: {item.get('keyword', '')}",
            f"品牌: {item.get('brand', '')}",
            f"结果: {'已提及' if item.get('rank', 99) != 99 else '未提及'}",
            f"高亮数: {item.get('highlight_count', 0)}",
            f"证据: {item.get('evidence', '') or '无'}",
            "",
            (item.get('answer_text', '') or '')[:1600] or "无正文记录",
        ]
        self._review_meta.insert(tk.END, "\n".join(lines))
        self._review_meta.config(state=tk.DISABLED)

        screenshot = str(item.get('screenshot', '') or '').strip()
        if screenshot and Path(screenshot).exists():
            try:
                from PIL import Image, ImageTk
                with Image.open(screenshot) as img:
                    preview = img.copy()
                preview.thumbnail((680, 420))
                self._review_preview_photo = ImageTk.PhotoImage(preview)
                self._review_image.config(image=self._review_preview_photo, text="")
            except Exception as e:
                self._review_image.config(image="", text=f"截图预览失败: {e}")
        else:
            self._review_image.config(image="", text="该记录没有可用截图")

    def _apply_review_decision(self, status: str):
        from core.history import apply_review

        item = self._selected_review_item()
        if not item:
            messagebox.showwarning("提示", "请先选择一条待复核记录")
            return
        note = self._review_note.get().strip()
        ok = apply_review(
            item.get('task_name', ''),
            item.get('id', ''),
            status,
            note=note,
            task_id=item.get('task_id', ''),
        )
        if not ok:
            messagebox.showerror("错误", "写入复核结果失败")
            return
        self._refresh_review_tab()
        self._refresh_status()
        self._trend_built = False
        messagebox.showinfo("成功", "复核结果已保存")

    # ──────────────────────────────────────────────────────────────────────────
    # Tab 7: 失败诊断中心
    # ──────────────────────────────────────────────────────────────────────────

    def _build_diagnostics_tab(self):
        frame = tk.Frame(self.notebook, bg=_BG)
        self.notebook.add(frame, text="  诊断  ")

        toolbar = tk.Frame(frame, bg=_BG)
        toolbar.pack(fill=tk.X, padx=24, pady=(20, 0))
        self._toolbar_btn(toolbar, "刷新诊断", self._refresh_diagnostics_tab)
        self._toolbar_btn(toolbar, "标记已处理", self._resolve_selected_diagnostic)

        body = tk.Frame(frame, bg=_BG)
        body.pack(fill=tk.BOTH, expand=True, padx=24, pady=(16, 20))

        self._diagnostic_list = tk.Listbox(
            body,
            width=40,
            bg=_SURFACE,
            fg=_TEXT_PRI,
            selectbackground=_ACCENT,
            selectforeground=_TEXT_PRI,
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground=_BORDER,
            font=(_FONT, 9),
        )
        self._diagnostic_list.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 16))
        self._diagnostic_list.bind("<<ListboxSelect>>", lambda e: self._render_diagnostic_detail())

        self._diagnostic_text = scrolledtext.ScrolledText(
            body,
            wrap=tk.WORD,
            bg=_SURFACE,
            fg=_TEXT_PRI,
            relief=tk.FLAT,
            highlightbackground=_BORDER,
            highlightthickness=1,
            padx=16,
            pady=14,
            font=(_FONT_MONO, 9),
        )
        self._diagnostic_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._diagnostic_text.config(state=tk.DISABLED)
        self._refresh_diagnostics_tab()

    def _refresh_diagnostics_tab(self):
        from core.diagnostics import get_events

        self._diagnostic_items = get_events(limit=300, include_resolved=False)
        self._diagnostic_list.delete(0, tk.END)
        for item in self._diagnostic_items:
            label = f"{item.get('ts', '')} | {item.get('category', '')}"
            self._diagnostic_list.insert(tk.END, label)
        if self._diagnostic_items:
            self._diagnostic_list.selection_clear(0, tk.END)
            self._diagnostic_list.selection_set(0)
            self._render_diagnostic_detail()
        else:
            self._render_diagnostic_detail()

    def _selected_diagnostic_item(self):
        sel = self._diagnostic_list.curselection()
        if not sel:
            return None
        idx = sel[0]
        if idx >= len(self._diagnostic_items):
            return None
        return self._diagnostic_items[idx]

    def _render_diagnostic_detail(self):
        item = self._selected_diagnostic_item()
        self._diagnostic_text.config(state=tk.NORMAL)
        self._diagnostic_text.delete(1.0, tk.END)
        if not item:
            self._diagnostic_text.insert(tk.END, "暂无未处理诊断事件")
            self._diagnostic_text.config(state=tk.DISABLED)
            return

        lines = [
            f"时间: {item.get('ts', '')}",
            f"级别: {item.get('level', '')}",
            f"分类: {item.get('category', '')}",
            f"任务: {item.get('task_name', '')}",
            f"平台: {item.get('platform', '')}",
            f"关键词: {item.get('keyword', '')}",
            f"品牌: {item.get('brand', '')}",
            "",
            f"问题: {item.get('message', '')}",
            "",
            f"建议: {item.get('suggestion', '') or '无'}",
        ]
        details = item.get('details') or {}
        if details:
            lines.extend(["", "详情:", yaml.dump(details, allow_unicode=True, sort_keys=False)])
        self._diagnostic_text.insert(tk.END, "\n".join(lines))
        self._diagnostic_text.config(state=tk.DISABLED)

    def _resolve_selected_diagnostic(self):
        from core.diagnostics import resolve_event

        item = self._selected_diagnostic_item()
        if not item:
            messagebox.showwarning("提示", "请先选择一条诊断事件")
            return
        if not resolve_event(item.get('id', '')):
            messagebox.showerror("错误", "标记失败")
            return
        self._refresh_diagnostics_tab()

    # ──────────────────────────────────────────────────────────────────────────
    # Tab 8: 周报 / 月报
    # ──────────────────────────────────────────────────────────────────────────

    def _build_report_tab(self):
        frame = tk.Frame(self.notebook, bg=_BG)
        self.notebook.add(frame, text="  报表  ")

        toolbar = tk.Frame(frame, bg=_BG)
        toolbar.pack(fill=tk.X, padx=24, pady=(20, 0))
        self._toolbar_btn(toolbar, "生成周报", lambda: self._generate_report("weekly"), accent=True)
        self._toolbar_btn(toolbar, "生成月报", lambda: self._generate_report("monthly"))
        self._toolbar_btn(toolbar, "发送当前报表", self._send_current_report)

        self._report_hint = tk.Label(
            frame,
            text="生成后会在这里显示文本摘要，并把卡片图片写到 reports/ 目录。",
            bg=_BG,
            fg=_TEXT_TER,
            font=(_FONT, 9),
        )
        self._report_hint.pack(anchor=tk.W, padx=24, pady=(12, 0))

        self._report_text = scrolledtext.ScrolledText(
            frame,
            wrap=tk.WORD,
            bg=_SURFACE,
            fg=_TEXT_PRI,
            relief=tk.FLAT,
            highlightbackground=_BORDER,
            highlightthickness=1,
            padx=20,
            pady=16,
            font=(_FONT_MONO, 9),
        )
        self._report_text.pack(fill=tk.BOTH, expand=True, padx=24, pady=(16, 20))
        self._report_text.config(state=tk.DISABLED)

    def _generate_report(self, period: str):
        from core.reports import generate_period_report

        self._report_data = generate_period_report(self.config or {}, period=period)
        self._report_text.config(state=tk.NORMAL)
        self._report_text.delete(1.0, tk.END)
        self._report_text.insert(tk.END, self._report_data.get('text', ''))
        self._report_text.insert(tk.END, f"\n\n图片文件: {self._report_data.get('image_path', '')}")
        self._report_text.config(state=tk.DISABLED)

    def _send_current_report(self):
        if not self._report_data:
            messagebox.showwarning("提示", "请先生成周报或月报")
            return

        webhook_url = ""
        for task in (self.config.get('tasks', []) or []):
            if not isinstance(task, dict):
                continue
            webhook_url = str(task.get('webhook_url', '')).strip()
            if webhook_url:
                break
        if not webhook_url or "YOUR_KEY_HERE" in webhook_url:
            messagebox.showerror("错误", "没有找到可用的企业微信机器人 webhook")
            return

        from core.notifier import WeComNotifier
        notifier = WeComNotifier(webhook_url=webhook_url, cooldown_minutes=0, send_interval=2)
        ok_text = notifier.send_text_message(self._report_data.get('text', ''))
        ok_image = True
        image_path = str(self._report_data.get('image_path', '') or '').strip()
        if image_path and Path(image_path).exists():
            ok_image = notifier.send_image_message(image_path)
        if ok_text and ok_image:
            messagebox.showinfo("成功", "报表已发送到企业微信")
        else:
            messagebox.showerror("错误", notifier.last_error or "报表发送失败")

    # ──────────────────────────────────────────────────────────────────────────
    # 监控控制
    # ──────────────────────────────────────────────────────────────────────────

    def _toggle_monitoring(self):
        if self.running:
            self._stop_monitoring()
        else:
            self._start_monitoring()

    def _start_recognition_only(self):
        tasks = [t for t in self.config.get('tasks', []) if t.get('enabled', True)]
        recognition_configured = False
        for task in tasks:
            keywords = task.get('keywords', [])
            if not keywords and task.get('keyword'):
                keywords = [{'mode': task.get('mode', 'browser')}]
            if any(kw.get('mode', 'browser') == 'recognition' for kw in keywords):
                recognition_configured = True
                break

        has_recognition = bool(self.recognition_manager and self.recognition_manager.has_recognition_tasks())
        if not has_recognition:
            if recognition_configured:
                messagebox.showwarning("提示", "检测到识别模式关键词，但未启用识别模式监听。请编辑任务并勾选“启用识别模式监听”。")
                return
            messagebox.showwarning("提示", "当前没有可用的识别模式任务")
            return

        if self.running:
            runtime = {}
            if self.recognition_manager and hasattr(self.recognition_manager, "get_runtime_status"):
                try:
                    runtime = self.recognition_manager.get_runtime_status() or {}
                except Exception:
                    runtime = {}
            if runtime.get("running"):
                messagebox.showinfo("提示", "识别模式已经在监听中了，直接复制新截图到剪切板即可。")
                return
            self._start_recognition_mode()
            return

        self.running = True
        self._apply_monitoring_button_state(True)
        self._status_pill.config(text="状态: 识别监听中", fg=_SUCCESS, bg=_BG)
        self._start_recognition_mode()

    def _start_monitoring(self):
        if not self.scheduler and not self.recognition_manager:
            messagebox.showwarning("提示", "调度器未初始化")
            return
        tasks = [t for t in self.config.get('tasks', []) if t.get('enabled', True)]
        query_tasks = []
        recognition_configured = False
        for task in tasks:
            keywords = task.get('keywords', [])
            if not keywords and task.get('keyword'):
                keywords = [{'mode': task.get('mode', 'browser')}]
            if any(kw.get('mode', 'browser') == 'recognition' for kw in keywords):
                recognition_configured = True
            if any(kw.get('mode', 'browser') != 'recognition' for kw in keywords):
                query_tasks.append(task)
        has_recognition = bool(self.recognition_manager and self.recognition_manager.has_recognition_tasks())
        if not query_tasks and not has_recognition:
            if recognition_configured:
                messagebox.showwarning("提示", "检测到识别模式关键词，但未启用识别模式监听。请编辑任务并勾选“启用识别模式监听”。")
                return
            messagebox.showwarning("提示", "当前没有可运行的监控任务")
            return

        self.running = True
        self._apply_monitoring_button_state(True)
        self._status_pill.config(text="状态: 等待定时", fg=_SUCCESS, bg=_BG)
        set_auto_resume_monitoring(True)
        def on_status_change(status, message):
            def _apply(state=status, msg=message):
                self._status_pill.config(text=f"状态: {msg[:18]}", fg=_TEXT_SEC)
                self._refresh_status()
                if state == "followup_needed":
                    self._message_action_phase = "confirm_followup"
                    self._show_message_banner(msg, [
                        ("确认", self._confirm_followup_prompt),
                        ("跳过", self._skip_followup_modes),
                    ])
                elif state == "mode_complete" and not (self.scheduler and self.scheduler.get_status().get("awaiting_followup")):
                    self._show_message_banner(msg, [("关闭", self._hide_message_banner)])
                elif state == "stopped":
                    self._hide_message_banner()
            self.root.after(0, _apply)
            callback = getattr(self, "_on_scheduler_status_event", None)
            if callable(callback):
                try:
                    callback(status, message)
                except Exception as e:
                    print(f"[Main] 调度状态回调失败: {e}")
        if self.scheduler and query_tasks:
            self.scheduler.start(
                query_tasks,
                self._execute_task,
                on_status_change,
                on_task_timeout=self._on_task_timeout,
                on_round_complete=getattr(self, "_on_scheduler_round_complete", None),
                on_cycle_complete=getattr(self, "_on_scheduler_cycle_complete", None),
            )
        if has_recognition:
            self.current_mode = "抓取模式"
            self.mode_notice = "等待定时轮次；识别监听不会立刻启动，需要到点后自动拉起。若要立即监听，请点击“仅启动识别”。"
            self._refresh_mode_notice()
        else:
            self.current_mode = "抓取模式"
            self._refresh_mode_notice()

    def _stop_monitoring(self, *, persist_preference=True):
        if self.scheduler:
            self.scheduler.stop()
        self._stop_recognition_mode()
        self._hide_message_banner()
        self.running = False
        self.current_mode = "抓取模式"
        self.mode_notice = ""
        self._apply_monitoring_button_state(False)
        self._status_pill.config(text="状态: 已停止", fg=_TEXT_SEC, bg=_BG)
        if persist_preference:
            set_auto_resume_monitoring(False)
        self._refresh_mode_notice()

    def _on_task_timeout(self, task, elapsed_seconds):
        task_name = task.get('name', '')
        kw_info = ', '.join(
            f"{kw.get('keyword','')}/{kw.get('brand','')}"
            for kw in task.get('keywords', [])[:3])
        self._timeout_warnings[task_name] = {
            'task': task,
            'elapsed': int(elapsed_seconds // 60),
            'kw_info': kw_info,
        }
        self.root.after(0, self._refresh_tasks)

    def _execute_task(self, task: dict):
        if callable(self.execute_task_callback):
            return self.execute_task_callback(task)
        print(f"[Main] 执行任务: {task.get('name')}")
        return {}

    def update_result(self, task_name, platform, brand, rank):
        self.last_results[(task_name, platform, brand)] = rank
        self.root.after(0, self._refresh_tasks)
        self.root.after(0, self._refresh_status)
        self.root.after(0, self._refresh_review_tab)
        self.root.after(0, self._refresh_diagnostics_tab)

    # ──────────────────────────────────────────────────────────────────────────
    # 运行
    # ──────────────────────────────────────────────────────────────────────────

    def run(self):
        self.root.mainloop()

    def _on_close(self):
        try:
            if self.running:
                self._stop_monitoring(persist_preference=False)
            persist_config(
                self.config,
                self.config_path,
                ensure_task_ids=True,
            )
        except Exception as e:
            print(f"[Main] 退出时保存配置失败: {e}")
        self._stop_config_watcher()
        try:
            self.root.destroy()
        except Exception:
            pass


def open_main_window(scheduler=None, notifier=None, config=None, config_path="config.yaml",
                     recognition_manager=None, on_config_change=None, execute_task_callback=None):
    app = MainWindow(scheduler=scheduler, notifier=notifier,
                     config=config, config_path=config_path,
                     recognition_manager=recognition_manager,
                     on_config_change=on_config_change,
                     execute_task_callback=execute_task_callback)
    app.run()


if __name__ == "__main__":
    from core import SmartScheduler, ensure_config_task_ids
    from core.recognition import ClipboardRecognitionManager
    from main import run_task_group

    try:
        cfg = load_yaml_config(resolve_app_path("config.yaml"))
    except Exception:
        cfg = {}
    ensure_config_task_ids(cfg)

    scheduler = SmartScheduler(cfg.get("scheduler", {}))
    app = MainWindow(scheduler=scheduler, config=cfg)
    app.recognition_manager = ClipboardRecognitionManager(
        config_getter=lambda: app.config,
        on_batch_ready=app._on_recognition_batch_ready,
        on_send_complete=app._on_recognition_send_complete,
        on_mode_change=app._on_recognition_mode_change,
        on_manual_switch_required=app._on_recognition_manual_switch_required,
        on_manual_state_change=app._on_recognition_manual_state_change,
    )

    def execute_task(task):
        current_config = app.config or {}
        task_name = task.get('name', '')
        keywords = task.get('keywords', [])
        default_brand = keywords[0].get('brand', '') if keywords else task.get('brand', '')
        if not task_name:
            task_name = default_brand

        results = run_task_group(
            task,
            current_config.get('default_notification', {}),
            current_config,
            execution_source='auto',
            return_report=True,
            stop_checker=scheduler.should_stop,
        )
        if isinstance(results, tuple):
            result_items, execution_report = results
        else:
            result_items, execution_report = results, {}
        for result in result_items:
            app.update_result(task_name, result['platform'], result['brand'], result['rank'])
        return execution_report

    app.execute_task_callback = execute_task
    app.run()
