"""
程序设置对话框
统一管理调度时间、识别模式和搜索桥接配置。
"""

import tkinter as tk
from tkinter import ttk, messagebox

from core.app_paths import resolve_app_path
from core.config_watcher import load_config as load_yaml_config
from core.notifier import send_scheduler_test_message
from core.scheduler import normalize_weekly_times
from core.screenshot_tools import get_decoration_theme, get_default_decoration_theme
from ui.config_runtime import persist_config_with_feedback
from ui.tk_compat import (
    bind_mousewheel_recursive,
    install_global_tk_behaviors,
    scroll_canvas_on_mousewheel,
)

_BROWSER_ANSWER_MODE_OPTIONS = {
    "页面原始截图": "page",
    "DOM 复排截图": "dom",
}


class SettingsDialog:
    """程序设置窗口。"""

    def __init__(self, parent, config_path="config.yaml", on_config_change=None):
        self.config_path = resolve_app_path(config_path)
        self.config = self._load()
        self.on_config_change = on_config_change

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("程序设置")
        self.dialog.geometry("620x620")
        self.dialog.grab_set()
        self.dialog.resizable(False, True)
        self.dialog.lift()
        self.dialog.focus_force()
        install_global_tk_behaviors(self.dialog)

        self._scheduler_vars = {}
        self._scheduler_extra_vars = {}
        self._browser_vars = {}
        self._recognition_vars = {}
        self._search_vars = {}
        self._decoration_vars = {}
        self._build_ui()

    def _load(self):
        try:
            return load_yaml_config(self.config_path)
        except Exception:
            return {}

    def _build_ui(self):
        canvas = tk.Canvas(self.dialog, borderwidth=0)
        scrollbar = ttk.Scrollbar(self.dialog, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, padding=20)

        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win, width=e.width))

        def on_scroll(event):
            return scroll_canvas_on_mousewheel(canvas, event)

        canvas.bind("<MouseWheel>", on_scroll)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        ttk.Label(inner, text="程序设置", font=("Arial", 13, "bold")).pack(anchor=tk.W, pady=(0, 4))
        ttk.Label(
            inner,
            text="这里统一管理每周自动查询时间、抓取模式、识别模式策略和联网搜索桥接配置。",
            foreground="gray",
        ).pack(anchor=tk.W, pady=(0, 14))

        self._build_scheduler_section(inner)
        self._build_browser_section(inner)
        self._build_recognition_section(inner)
        self._build_search_section(inner)
        self._build_screenshot_decoration_section(inner)

        btn_frame = ttk.Frame(inner)
        btn_frame.pack(fill=tk.X, pady=(16, 0))
        ttk.Button(btn_frame, text="保存", command=self._save).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="取消", command=self.dialog.destroy).pack(side=tk.RIGHT, padx=5)
        bind_mousewheel_recursive(inner, on_scroll)

    def _build_scheduler_section(self, parent):
        scheduler_cfg = self.config.get("scheduler", {}) or {}
        weekly_times = normalize_weekly_times(scheduler_cfg)
        minute_values = ("00", "05", "10", "15", "20", "25", "30", "35", "40", "45", "50", "55")
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

        frame = ttk.LabelFrame(parent, text="自动查询时间", padding=10)
        frame.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(
            frame,
            text="任务只决定在哪几天参与自动查询；每天具体几点开始，由这里统一控制。",
            foreground="gray",
            wraplength=520,
        ).pack(anchor=tk.W)

        ttk.Label(
            frame,
            text="调度通知 Webhook（用于模式汇总、整轮完成汇总、实时异常提醒）:",
        ).pack(anchor=tk.W, pady=(10, 0))
        notification_webhook_var = tk.StringVar(value=str(scheduler_cfg.get("notification_webhook_url", "")))
        notification_entry = ttk.Entry(frame, textvariable=notification_webhook_var, width=60)
        notification_entry.pack(fill=tk.X, pady=(2, 4))

        def _scheduler_ctx(event, entry=notification_entry):
            menu = tk.Menu(self.dialog, tearoff=0)
            menu.add_command(label="复制", command=lambda: entry.event_generate("<<CopyCompat>>"))
            menu.add_command(label="剪切", command=lambda: entry.event_generate("<<CutCompat>>"))
            menu.add_command(label="粘贴", command=lambda: entry.event_generate("<<PasteCompat>>"))
            menu.add_command(label="全选", command=lambda: entry.event_generate("<<SelectAllCompat>>"))
            menu.post(event.x_root, event.y_root)

        notification_entry.bind("<Button-3>", _scheduler_ctx)
        notification_entry.bind("<Button-2>", _scheduler_ctx)
        ttk.Label(
            frame,
            text="留空则不发送调度过程通知；这个 webhook 独立于每个任务自己的命中截图 webhook。",
            foreground="gray",
            wraplength=520,
        ).pack(anchor=tk.W)
        self._scheduler_extra_vars["notification_webhook_url"] = notification_webhook_var

        action_row = ttk.Frame(frame)
        action_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(
            action_row,
            text="发送测试消息",
            command=self._send_scheduler_test_message,
        ).pack(side=tk.LEFT)
        ttk.Label(
            action_row,
            text="会向当前填写的调度 webhook 发送一条测试文本。",
            foreground="gray",
        ).pack(side=tk.LEFT, padx=(10, 0))

        auto_continue_var = tk.BooleanVar(value=bool(scheduler_cfg.get("auto_continue_after_default_failure", False)))
        ttk.Checkbutton(
            frame,
            text="默认模式失败后自动启动后续模式",
            variable=auto_continue_var,
        ).pack(anchor=tk.W, pady=(10, 0))
        ttk.Label(
            frame,
            text="关闭时，系统会先发主页消息提醒；你确认后才显示可继续的后续模式选项。",
            foreground="gray",
            wraplength=520,
        ).pack(anchor=tk.W, pady=(4, 0))
        self._scheduler_extra_vars["auto_continue_after_default_failure"] = auto_continue_var

        for weekday, label in enumerate(weekday_names):
            row = ttk.Frame(frame)
            row.pack(fill=tk.X, pady=(8 if weekday == 0 else 6, 0))

            time_text = weekly_times.get(str(weekday))
            enabled_var = tk.BooleanVar(value=bool(time_text))
            hour_text, minute_text = ("09", "30")
            if time_text:
                hour_text, minute_text = time_text.split(":")

            ttk.Checkbutton(row, text=label, variable=enabled_var).pack(side=tk.LEFT, padx=(0, 10))
            ttk.Label(row, text="小时").pack(side=tk.LEFT)
            hour_var = tk.StringVar(value=hour_text)
            tk.Spinbox(
                row,
                from_=0,
                to=23,
                width=5,
                format="%02.0f",
                textvariable=hour_var,
            ).pack(side=tk.LEFT, padx=(4, 14))
            ttk.Label(row, text="分钟").pack(side=tk.LEFT)
            minute_var = tk.StringVar(value=minute_text)
            tk.Spinbox(
                row,
                values=minute_values,
                width=5,
                textvariable=minute_var,
            ).pack(side=tk.LEFT, padx=(4, 0))

            self._scheduler_vars[str(weekday)] = {
                "enabled": enabled_var,
                "hour": hour_var,
                "minute": minute_var,
            }

    def _build_recognition_section(self, parent):
        recognition_cfg = self.config.get("recognition", {}) or {}

        frame = ttk.LabelFrame(parent, text="识别模式", padding=10)
        frame.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(
            frame,
            text="这里控制识别模式是否优先使用本地 OCR，以及本地 OCR 未命中时是否启用 AI 辅助复核。",
            foreground="gray",
            wraplength=520,
        ).pack(anchor=tk.W)

        safe_mode_ocr_var = tk.BooleanVar(value=bool(recognition_cfg.get("safe_mode_ocr_enabled", True)))
        ttk.Checkbutton(
            frame,
            text="启用本地 OCR 自动识别",
            variable=safe_mode_ocr_var,
        ).pack(anchor=tk.W, pady=(10, 0))
        self._recognition_vars["safe_mode_ocr_enabled"] = safe_mode_ocr_var

        ai_fallback_var = tk.BooleanVar(value=bool(recognition_cfg.get("ai_fallback_enabled", False)))
        ttk.Checkbutton(
            frame,
            text="本地 OCR 未命中时启用 AI 识别辅助",
            variable=ai_fallback_var,
        ).pack(anchor=tk.W, pady=(8, 0))
        self._recognition_vars["ai_fallback_enabled"] = ai_fallback_var

        ttk.Label(
            frame,
            text="关闭本地 OCR 后，识别模式会进入人工推进；开启 AI 辅助后，会在 OCR 未识别到目标品牌时再调用 AI 复核。",
            foreground="gray",
            wraplength=520,
        ).pack(anchor=tk.W, pady=(8, 0))

    def _build_browser_section(self, parent):
        screenshot_cfg = self.config.get("screenshot", {}) or {}
        browser_answer_mode = str(screenshot_cfg.get("browser_answer_mode", "page") or "page").strip().lower()
        browser_answer_mode_label = next(
            (label for label, code in _BROWSER_ANSWER_MODE_OPTIONS.items() if code == browser_answer_mode),
            "页面原始截图",
        )

        frame = ttk.LabelFrame(parent, text="抓取模式", padding=10)
        frame.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(
            frame,
            text="这里控制抓取模式命中后的出图方式。页面原始截图会继续走下方的页面截图装饰模板；DOM 文本生成会固定走 Surfaced 模版本地重排。",
            foreground="gray",
            wraplength=520,
        ).pack(anchor=tk.W)

        self._browser_vars["answer_mode"] = tk.StringVar(value=browser_answer_mode_label)
        ttk.Label(frame, text="回答生成方式:").pack(anchor=tk.W, pady=(10, 0))
        ttk.Combobox(
            frame,
            textvariable=self._browser_vars["answer_mode"],
            values=list(_BROWSER_ANSWER_MODE_OPTIONS.keys()),
            state="readonly",
            width=20,
        ).pack(anchor=tk.W, pady=(2, 6))
        ttk.Label(
            frame,
            text="选择“DOM 文本生成”后，会直接提取回答区 HTML，并固定使用 Surfaced 模版重排；下方装饰模板仅作用于“页面原始截图”。",
            foreground="gray",
            wraplength=520,
        ).pack(anchor=tk.W, pady=(0, 4))

    def _build_search_section(self, parent):
        search_cfg = self.config.get("search", {}) or {}

        frame = ttk.LabelFrame(parent, text="搜索插件配置", padding=10)
        frame.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(
            frame,
            text="联网搜索桥接优先走 Tavily；填写后，搜搜和保险模式的搜索兜底都会优先使用它。",
            foreground="gray",
            wraplength=520,
        ).pack(anchor=tk.W)

        ttk.Label(frame, text="Tavily API Key（Bearer Token，以 tvly- 开头）:").pack(anchor=tk.W, pady=(8, 0))
        tavily_key_var = tk.StringVar(value=search_cfg.get("tavily_api_key", ""))
        tavily_entry = ttk.Entry(frame, textvariable=tavily_key_var, width=60, show="*")
        tavily_entry.pack(fill=tk.X, pady=(2, 6))

        def _search_ctx(event, entry=tavily_entry):
            menu = tk.Menu(self.dialog, tearoff=0)
            menu.add_command(label="复制", command=lambda: entry.event_generate("<<CopyCompat>>"))
            menu.add_command(label="剪切", command=lambda: entry.event_generate("<<CutCompat>>"))
            menu.add_command(label="粘贴", command=lambda: entry.event_generate("<<PasteCompat>>"))
            menu.add_command(label="全选", command=lambda: entry.event_generate("<<SelectAllCompat>>"))
            menu.post(event.x_root, event.y_root)

        tavily_entry.bind("<Button-3>", _search_ctx)
        tavily_entry.bind("<Button-2>", _search_ctx)

        ttk.Label(
            frame,
            text="未填写时，会自动回退到内置公开搜索桥接。",
            foreground="gray",
            wraplength=520,
        ).pack(anchor=tk.W, pady=(2, 0))

        self._search_vars = {
            "provider": tk.StringVar(value=search_cfg.get("provider", "tavily") or "tavily"),
            "tavily_api_key": tavily_key_var,
        }

    def _build_screenshot_decoration_section(self, parent):
        theme = get_decoration_theme(self.config)

        frame = ttk.LabelFrame(parent, text="页面原始截图装饰模板", padding=10)
        frame.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(
            frame,
            text="这里只作用于“页面原始截图”。DOM 文本生成会固定走 Surfaced 模版，不会套用这里的旧装饰样式。",
            foreground="gray",
            wraplength=520,
        ).pack(anchor=tk.W)

        self._decoration_vars = {
            "enabled": tk.BooleanVar(value=bool(theme.get("enabled", True))),
            "title": tk.StringVar(value=str(theme.get("title", ""))),
            "subtitle": tk.StringVar(value=str(theme.get("subtitle", ""))),
            "footer": tk.StringVar(value=str(theme.get("footer", ""))),
            "show_timestamp": tk.BooleanVar(value=bool(theme.get("show_timestamp", True))),
            "show_footer": tk.BooleanVar(value=bool(theme.get("show_footer", True))),
            "draw_highlight_boxes": tk.BooleanVar(value=bool(theme.get("draw_highlight_boxes", True))),
            "accent_color": tk.StringVar(value=str(theme.get("accent_color", "#14C7F3"))),
            "background_start": tk.StringVar(value=str((theme.get("background", {}) or {}).get("start", "#FCFDFF"))),
            "background_end": tk.StringVar(value=str((theme.get("background", {}) or {}).get("end", "#F7FAFF"))),
            "header_start": tk.StringVar(value=str((theme.get("header", {}) or {}).get("start", "#173A43"))),
            "header_end": tk.StringVar(value=str((theme.get("header", {}) or {}).get("end", "#14C7F3"))),
            "outer_padding": tk.IntVar(value=int((theme.get("layout", {}) or {}).get("outer_padding", 28))),
            "header_height": tk.IntVar(value=int((theme.get("layout", {}) or {}).get("header_height", 136))),
            "radius": tk.IntVar(value=int((theme.get("layout", {}) or {}).get("radius", 28))),
            "image_radius": tk.IntVar(value=int((theme.get("layout", {}) or {}).get("image_radius", 22))),
        }

        ttk.Checkbutton(
            frame,
            text="启用页面截图装饰模板",
            variable=self._decoration_vars["enabled"],
        ).pack(anchor=tk.W, pady=(10, 0))

        ttk.Label(frame, text="标题（支持 {brand} / {platform} / {keyword} / {time}）:").pack(anchor=tk.W, pady=(10, 0))
        ttk.Entry(frame, textvariable=self._decoration_vars["title"], width=60).pack(fill=tk.X, pady=(2, 6))

        ttk.Label(frame, text="副标题:").pack(anchor=tk.W)
        ttk.Entry(frame, textvariable=self._decoration_vars["subtitle"], width=60).pack(fill=tk.X, pady=(2, 6))

        ttk.Label(frame, text="页脚文案:").pack(anchor=tk.W)
        ttk.Entry(frame, textvariable=self._decoration_vars["footer"], width=60).pack(fill=tk.X, pady=(2, 10))

        toggles = ttk.Frame(frame)
        toggles.pack(fill=tk.X, pady=(0, 10))
        ttk.Checkbutton(toggles, text="显示时间", variable=self._decoration_vars["show_timestamp"]).pack(side=tk.LEFT)
        ttk.Checkbutton(toggles, text="显示页脚", variable=self._decoration_vars["show_footer"]).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Checkbutton(toggles, text="绘制高亮框", variable=self._decoration_vars["draw_highlight_boxes"]).pack(side=tk.LEFT, padx=(12, 0))

        colors = ttk.Frame(frame)
        colors.pack(fill=tk.X, pady=(0, 8))
        self._add_labeled_entry(colors, "强调色", self._decoration_vars["accent_color"], 0, 0)
        self._add_labeled_entry(colors, "背景起始", self._decoration_vars["background_start"], 0, 2)
        self._add_labeled_entry(colors, "背景结束", self._decoration_vars["background_end"], 1, 0)
        self._add_labeled_entry(colors, "头部起始", self._decoration_vars["header_start"], 1, 2)
        self._add_labeled_entry(colors, "头部结束", self._decoration_vars["header_end"], 2, 0)

        layout = ttk.Frame(frame)
        layout.pack(fill=tk.X, pady=(2, 8))
        self._add_labeled_spinbox(layout, "外边距", self._decoration_vars["outer_padding"], 0, 0, 12, 80)
        self._add_labeled_spinbox(layout, "头部高度", self._decoration_vars["header_height"], 0, 2, 88, 240)
        self._add_labeled_spinbox(layout, "卡片圆角", self._decoration_vars["radius"], 1, 0, 12, 48)
        self._add_labeled_spinbox(layout, "图片圆角", self._decoration_vars["image_radius"], 1, 2, 8, 36)

        actions = ttk.Frame(frame)
        actions.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(actions, text="恢复默认模板", command=self._reset_decoration_defaults).pack(side=tk.LEFT)
        ttk.Label(
            actions,
            text="改完保存后，后续“页面原始截图”会默认使用这里的样式。",
            foreground="gray",
        ).pack(side=tk.LEFT, padx=(10, 0))

    def _add_labeled_entry(self, parent, label, variable, row, column):
        ttk.Label(parent, text=f"{label}:").grid(row=row, column=column, sticky="w", pady=(0, 4))
        ttk.Entry(parent, textvariable=variable, width=18).grid(row=row, column=column + 1, sticky="ew", padx=(6, 14), pady=(0, 4))
        parent.grid_columnconfigure(column + 1, weight=1)

    def _add_labeled_spinbox(self, parent, label, variable, row, column, min_value, max_value):
        ttk.Label(parent, text=f"{label}:").grid(row=row, column=column, sticky="w", pady=(0, 4))
        tk.Spinbox(parent, from_=min_value, to=max_value, width=6, textvariable=variable).grid(
            row=row, column=column + 1, sticky="w", padx=(6, 14), pady=(0, 4)
        )

    def _reset_decoration_defaults(self):
        self._apply_decoration_theme(get_default_decoration_theme())

    def _apply_decoration_theme(self, theme):
        layout = (theme.get("layout", {}) or {})
        background = (theme.get("background", {}) or {})
        header = (theme.get("header", {}) or {})
        mapping = {
            "enabled": bool(theme.get("enabled", True)),
            "title": str(theme.get("title", "")),
            "subtitle": str(theme.get("subtitle", "")),
            "footer": str(theme.get("footer", "")),
            "show_timestamp": bool(theme.get("show_timestamp", True)),
            "show_footer": bool(theme.get("show_footer", True)),
            "draw_highlight_boxes": bool(theme.get("draw_highlight_boxes", True)),
            "accent_color": str(theme.get("accent_color", "#14C7F3")),
            "background_start": str(background.get("start", "#FCFDFF")),
            "background_end": str(background.get("end", "#F7FAFF")),
            "header_start": str(header.get("start", "#173A43")),
            "header_end": str(header.get("end", "#14C7F3")),
            "outer_padding": int(layout.get("outer_padding", 28)),
            "header_height": int(layout.get("header_height", 136)),
            "radius": int(layout.get("radius", 28)),
            "image_radius": int(layout.get("image_radius", 22)),
        }
        for key, value in mapping.items():
            var = self._decoration_vars.get(key)
            if var is not None:
                var.set(value)

    def _send_scheduler_test_message(self):
        webhook_url = self._scheduler_extra_vars.get(
            "notification_webhook_url",
            tk.StringVar(value=""),
        ).get().strip()
        send_interval = int((self.config.get("default_notification", {}) or {}).get("send_interval", 1) or 1)
        ok, error = send_scheduler_test_message(webhook_url, send_interval=send_interval)
        if ok:
            messagebox.showinfo("发送成功", "测试消息已发送，请到企业微信里确认是否收到。")
            return
        messagebox.showerror("发送失败", error or "测试消息发送失败")

    def _save(self):
        self.config.setdefault("scheduler", {})
        weekly_times = {}
        for weekday in range(7):
            vars_for_day = self._scheduler_vars.get(str(weekday), {})
            enabled_var = vars_for_day.get("enabled", tk.BooleanVar(value=False))
            if not enabled_var.get():
                weekly_times[str(weekday)] = None
                continue
            hour = str(vars_for_day.get("hour", tk.StringVar(value="09")).get()).zfill(2)
            minute = str(vars_for_day.get("minute", tk.StringVar(value="30")).get()).zfill(2)
            weekly_times[str(weekday)] = f"{hour}:{minute}"
        self.config["scheduler"]["weekly_times"] = weekly_times
        self.config["scheduler"]["auto_continue_after_default_failure"] = bool(
            self._scheduler_extra_vars.get(
                "auto_continue_after_default_failure",
                tk.BooleanVar(value=False),
            ).get()
        )
        self.config["scheduler"]["notification_webhook_url"] = self._scheduler_extra_vars.get(
            "notification_webhook_url",
            tk.StringVar(value=""),
        ).get().strip()

        self.config.setdefault("recognition", {})
        self.config["recognition"]["safe_mode_ocr_enabled"] = bool(
            self._recognition_vars.get("safe_mode_ocr_enabled", tk.BooleanVar(value=True)).get()
        )
        self.config["recognition"]["ai_fallback_enabled"] = bool(
            self._recognition_vars.get("ai_fallback_enabled", tk.BooleanVar(value=False)).get()
        )

        self.config.setdefault("search", {})
        self.config["search"]["provider"] = self._search_vars["provider"].get().strip() or "tavily"
        self.config["search"]["tavily_api_key"] = self._search_vars["tavily_api_key"].get().strip()

        self.config.setdefault("screenshot", {})
        self.config["screenshot"]["browser_answer_mode"] = _BROWSER_ANSWER_MODE_OPTIONS.get(
            str(self._browser_vars.get("answer_mode", tk.StringVar(value="页面原始截图")).get() or "").strip(),
            "page",
        )
        self.config["screenshot"]["decoration"] = {
            "enabled": bool(self._decoration_vars["enabled"].get()),
            "title": self._decoration_vars["title"].get().strip(),
            "subtitle": self._decoration_vars["subtitle"].get().strip(),
            "footer": self._decoration_vars["footer"].get().strip(),
            "show_timestamp": bool(self._decoration_vars["show_timestamp"].get()),
            "show_footer": bool(self._decoration_vars["show_footer"].get()),
            "draw_highlight_boxes": bool(self._decoration_vars["draw_highlight_boxes"].get()),
            "accent_color": self._decoration_vars["accent_color"].get().strip(),
            "background": {
                "start": self._decoration_vars["background_start"].get().strip(),
                "end": self._decoration_vars["background_end"].get().strip(),
            },
            "header": {
                "start": self._decoration_vars["header_start"].get().strip(),
                "end": self._decoration_vars["header_end"].get().strip(),
            },
            "layout": {
                "outer_padding": int(self._decoration_vars["outer_padding"].get()),
                "header_height": int(self._decoration_vars["header_height"].get()),
                "radius": int(self._decoration_vars["radius"].get()),
                "image_radius": int(self._decoration_vars["image_radius"].get()),
            },
        }

        persist_config_with_feedback(
            self.config,
            self.config_path,
            success_message="设置已保存",
            ensure_task_ids=True,
            on_config_change=self.on_config_change,
            event_root=self.dialog._root(),
            on_success=self.dialog.destroy,
        )
