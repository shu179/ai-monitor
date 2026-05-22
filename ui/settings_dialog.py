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
from core.screenshot_tools import get_default_decoration_theme
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
            text="识别模式会固定启用本地 OCR，这里只保留识别方式相关设置。",
            foreground="gray",
            wraplength=520,
        ).pack(anchor=tk.W)

        ttk.Label(
            frame,
            text="命中品牌后会直接进入后续发送流程。",
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
            text="联网搜索桥接优先走 Tavily；填写后，搜搜会优先使用它。",
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
        self.config["recognition"]["safe_mode_ocr_enabled"] = True

        self.config.setdefault("search", {})
        self.config["search"]["provider"] = self._search_vars["provider"].get().strip() or "tavily"
        self.config["search"]["tavily_api_key"] = self._search_vars["tavily_api_key"].get().strip()

        self.config.setdefault("screenshot", {})
        self.config["screenshot"]["browser_answer_mode"] = _BROWSER_ANSWER_MODE_OPTIONS.get(
            str(self._browser_vars.get("answer_mode", tk.StringVar(value="页面原始截图")).get() or "").strip(),
            "page",
        )
        self.config["screenshot"]["decoration"] = get_default_decoration_theme()

        persist_config_with_feedback(
            self.config,
            self.config_path,
            success_message="设置已保存",
            ensure_task_ids=True,
            on_config_change=self.on_config_change,
            event_root=self.dialog._root(),
            on_success=self.dialog.destroy,
        )
