"""
任务配置管理器
图形界面管理任务组：品牌页面 + 行业/地区标签 + 平台 + 关键词 + 品牌 + 企业微信
"""

import copy
import re
import tkinter as tk
from tkinter import ttk, messagebox

from core.app_paths import resolve_app_path
from core.article_store import get_articles_by_task, get_task_article_counts
from core.config_watcher import ConfigWatcher, load_config as load_yaml_config
from core.history import get_brand_trend_series, get_task_brand_names
from core.task_defaults import compute_recognition_batch_size_from_keywords, get_most_common_task_webhook
from ui.article_window import ArticleWindow, _CalendarPopup
from core.recognition import ClipboardRecognitionManager
from core.daily_task_state import assign_task_id, build_task_state_extra, write_task_status
from core.scheduler import describe_task_schedule, normalize_weekly_times
from core.time_utils import local_now
from ui.config_runtime import persist_config, persist_config_with_feedback
from ui.keyword_guide import KeywordGuideWindow
from ui.tk_compat import (
    bind_mousewheel_recursive,
    install_global_tk_behaviors,
    scroll_canvas_on_mousewheel,
)


PLATFORMS = [
    ('doubao', '豆包'),
    ('deepseek', 'DeepSeek'),
    ('ark_deepseek', '方舟 DeepSeek'),
    ('kimi', 'Kimi'),
    ('yuanbao', '腾讯元宝'),
    ('tongyi', '通义千问'),
    ('wenxin', '文心一言'),
]

MODE_LABELS = {
    'browser': '抓取模式',
    'api': '保险模式',
    'recognition': '识别模式',
    'smart': '智能模式',
}

_BG = "#F0F0F0"
_SURFACE = "#FFFFFF"
_SURFACE2 = "#F7F7F7"
_BORDER = "#D0D0D0"
_BORDER2 = "#C4C4C4"
_TEXT_PRI = "#202020"
_TEXT_SEC = "#4F4F4F"
_TEXT_TER = "#6F6F6F"
_ACCENT = "#0A64A4"
_SUCCESS = "#1F7A1F"
_DANGER = "#B42318"


def _split_tags(value) -> list[str]:
    if isinstance(value, str):
        items = re.split(r"[,\n，、;/]+", value)
    elif isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value]
    else:
        return []
    seen = set()
    tags = []
    for item in items:
        tag = str(item or "").strip()
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


def _task_tag_summary(task: dict) -> str:
    industry = _split_tags(task.get("industry_tags") or task.get("industry"))
    region = _split_tags(task.get("region_tags") or task.get("region"))
    parts = []
    if industry:
        parts.append(f"行业：{' / '.join(industry[:3])}")
    if region:
        parts.append(f"地区：{' / '.join(region[:3])}")
    return "  ·  ".join(parts)


def _task_brand_page(task: dict) -> str:
    keywords = task.get("keywords", []) or []
    first_brand = ""
    if keywords:
        first_brand = str((keywords[0] or {}).get("brand", "")).strip()
    return str(task.get("name") or first_brand).strip()


def _task_brand_names(task: dict | None) -> list[str]:
    names = get_task_brand_names(task or {})
    return names or ([str((task or {}).get("name") or "").strip()] if str((task or {}).get("name") or "").strip() else [])


def _stringify_tags(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item).strip() for item in value if str(item).strip())
    return ""


def _parse_tag_text(value: str) -> list[str]:
    if not value:
        return []
    parts = re.split(r"[,\n，、;/]+", str(value))
    seen = set()
    tags = []
    for part in parts:
        tag = part.strip()
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


class TaskConfigDialog:
    """添加/编辑任务对话框（支持多关键词×多平台）"""

    def __init__(self, parent, task=None, existing_tasks=None):
        self.result = None
        self._is_new_task = task is None
        self._existing_tasks = list(existing_tasks or [])
        self._original_task = task or {}
        # 向后兼容：旧格式转换
        if task and 'keyword' in task and 'keywords' not in task:
            task = {**task, 'keywords': [{'keyword': task['keyword'], 'brand': task.get('brand', ''), 'platforms': [task['platform']]}]}
        self._original_task = task or {}
        self._webhook_default_value = get_most_common_task_webhook(self._existing_tasks)
        self._recognition_batch_manually_edited = bool((task or {}).get('recognition_batch_size'))

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("添加品牌" if task is None else "编辑品牌")
        self.dialog.geometry("700x810")
        self.dialog.transient(parent)
        self.dialog.grab_set()
        install_global_tk_behaviors(self.dialog)

        self._kw_rows = []  # 每行: {'keyword': StringVar, 'brand': StringVar, 'platform_vars': {code: BooleanVar}, 'deep_think_vars': {code: BooleanVar}, 'frame': Frame}
        self._test_recognition_manager = None
        self._test_keyword_guide = None
        self._test_status_win = None
        self._test_status_label = None
        self._test_runtime_config = None
        self._brand_trend_redraw = None

        self.create_form(task)
        self.dialog.protocol("WM_DELETE_WINDOW", self._close_dialog)

    def create_form(self, task):
        """创建表单（带滚动条）"""
        existing_keywords = task.get('keywords', []) if task else []
        has_recognition_mode = any(
            kw.get('mode', 'browser') == 'recognition'
            for kw in existing_keywords
        )

        canvas = tk.Canvas(self.dialog)
        scrollbar = ttk.Scrollbar(self.dialog, orient="vertical", command=canvas.yview)
        self._form_frame = ttk.Frame(canvas, padding="20")

        self._form_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        _win = canvas.create_window((0, 0), window=self._form_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(_win, width=e.width))
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def on_mousewheel(event):
            return scroll_canvas_on_mousewheel(canvas, event)

        canvas.bind("<MouseWheel>", on_mousewheel)
        self._canvas = canvas
        self._on_mousewheel = on_mousewheel

        frame = self._form_frame

        # 任务名称默认跟随首个关键词品牌，不单独编辑
        ttk.Label(frame, text="品牌名称（自动取第一个品牌名）:").pack(anchor=tk.W, pady=(0, 5))
        preview_name = (task.get('name', '') if task else '') or (existing_keywords[0].get('brand', '') if existing_keywords else '')
        self._task_name_preview = tk.StringVar(value=preview_name)
        ttk.Label(
            frame,
            textvariable=self._task_name_preview,
            foreground="gray",
            font=("Arial", 10, "bold"),
        ).pack(anchor=tk.W, pady=(0, 12))

        ttk.Label(frame, text="所属行业标签（逗号分隔）:").pack(anchor=tk.W, pady=(0, 5))
        self.industry_tags_var = tk.StringVar(value=_stringify_tags((task or {}).get('industry_tags', (task or {}).get('industry', ''))))
        ttk.Entry(frame, textvariable=self.industry_tags_var, width=50).pack(fill=tk.X, pady=(0, 12))

        ttk.Label(frame, text="地区标签（逗号分隔）:").pack(anchor=tk.W, pady=(0, 5))
        self.region_tags_var = tk.StringVar(value=_stringify_tags((task or {}).get('region_tags', (task or {}).get('region', ''))))
        ttk.Entry(frame, textvariable=self.region_tags_var, width=50).pack(fill=tk.X, pady=(0, 15))

        # 优化时间周期
        ttk.Label(frame, text="优化时间周期:").pack(anchor=tk.W, pady=(0, 5))
        period_frame = ttk.Frame(frame)
        period_frame.pack(fill=tk.X, pady=(0, 15))

        ttk.Label(period_frame, text="开始日期:").pack(side=tk.LEFT)
        self.opt_start_var = tk.StringVar(value=(task or {}).get('optimization_start_date', ''))
        opt_start_entry = ttk.Entry(period_frame, textvariable=self.opt_start_var, width=14)
        opt_start_entry.pack(side=tk.LEFT, padx=(4, 12))

        ttk.Label(period_frame, text="结束日期:").pack(side=tk.LEFT)
        self.opt_end_var = tk.StringVar(value=(task or {}).get('optimization_end_date', ''))
        opt_end_entry = ttk.Entry(period_frame, textvariable=self.opt_end_var, width=14)
        opt_end_entry.pack(side=tk.LEFT, padx=(4, 12))

        ttk.Label(period_frame, text="总时长:").pack(side=tk.LEFT)
        self.opt_duration_var = tk.StringVar(value="")
        opt_duration_entry = ttk.Entry(period_frame, textvariable=self.opt_duration_var, width=6)
        opt_duration_entry.pack(side=tk.LEFT, padx=(4, 4))
        ttk.Label(period_frame, text="天").pack(side=tk.LEFT)

        def _calc_duration(*_args):
            start = self.opt_start_var.get().strip()
            end = self.opt_end_var.get().strip()
            if start and end:
                try:
                    from datetime import date as _date
                    d1 = _date.fromisoformat(start)
                    d2 = _date.fromisoformat(end)
                    days = (d2 - d1).days
                    self.opt_duration_var.set(str(max(0, days)))
                except ValueError:
                    pass

        def _on_duration_change(*_args):
            start = self.opt_start_var.get().strip()
            dur = self.opt_duration_var.get().strip()
            if start and dur:
                try:
                    from datetime import date as _date, timedelta as _td
                    d1 = _date.fromisoformat(start)
                    d2 = d1 + _td(days=max(0, int(dur)))
                    self.opt_end_var.set(d2.isoformat())
                except (ValueError, TypeError):
                    pass

        self.opt_start_var.trace_add('write', _calc_duration)
        self.opt_end_var.trace_add('write', _calc_duration)
        self.opt_duration_var.trace_add('write', _on_duration_change)
        _calc_duration()

        # 点击日期输入框弹出日历选择器
        def _open_date_picker(target_var):
            from datetime import date as _date
            try:
                initial = _date.fromisoformat(target_var.get().strip())
            except (ValueError, AttributeError):
                initial = _date.today()
            def _apply(selected):
                target_var.set(selected.isoformat())
            self._date_popup = _CalendarPopup(self.dialog, initial, _apply)

        opt_start_entry.bind("<Button-1>", lambda _e: _open_date_picker(self.opt_start_var) or "break")
        opt_end_entry.bind("<Button-1>", lambda _e: _open_date_picker(self.opt_end_var) or "break")

        ttk.Label(frame, text="格式：YYYY-MM-DD，留空表示不限制。到达结束日期后自动暂停查询。",
                  foreground="gray", font=("Arial", 9)).pack(anchor=tk.W, pady=(0, 15))

        # 关键词列表区域
        ttk.Label(frame, text="关键词列表:").pack(anchor=tk.W, pady=(0, 5))
        self._kw_container = ttk.Frame(frame)
        self._kw_container.pack(fill=tk.X, pady=(0, 5))

        # 加载已有关键词或默认一行
        if existing_keywords:
            for kw_entry in existing_keywords:
                self._add_keyword_row(
                    keyword=kw_entry.get('keyword', ''),
                    brand=kw_entry.get('brand', ''),
                    platforms=kw_entry.get('platforms', []),
                    deep_think=kw_entry.get('deep_think', {}),
                    mode=kw_entry.get('mode', 'browser'),
                )
        else:
            self._add_keyword_row()

        ttk.Button(frame, text="＋ 添加关键词", command=self._add_keyword_row).pack(anchor=tk.W, pady=(0, 15))

        # 企业微信Webhook
        ttk.Label(frame, text="企业微信机器人Webhook:").pack(anchor=tk.W, pady=(0, 5))
        default_webhook = task.get('webhook_url', '') if task else self._webhook_default_value
        self.webhook_var = tk.StringVar(value=default_webhook)
        self.webhook_entry = ttk.Entry(frame, textvariable=self.webhook_var, width=50)
        self.webhook_entry.pack(fill=tk.X, pady=(0, 15))

        def show_context_menu(event):
            menu = tk.Menu(self.dialog, tearoff=0)
            menu.add_command(label="粘贴", command=lambda: self.webhook_entry.event_generate("<<Paste>>"))
            menu.post(event.x_root, event.y_root)
        self.webhook_entry.bind("<Button-3>", show_context_menu)
        self.webhook_entry.bind("<Button-2>", show_context_menu)

        # 运行星期
        ttk.Label(frame, text="自动查询星期:").pack(anchor=tk.W, pady=(0, 5))
        weekdays_frame = ttk.Frame(frame)
        weekdays_frame.pack(fill=tk.X, pady=(0, 15))
        self.weekday_vars = []
        current_weekdays = task.get('weekdays', [0, 1, 2, 3, 4]) if task else [0, 1, 2, 3, 4]
        for i, name in enumerate(['周一', '周二', '周三', '周四', '周五', '周六', '周日']):
            var = tk.BooleanVar(value=i in current_weekdays)
            self.weekday_vars.append(var)
            ttk.Checkbutton(weekdays_frame, text=name, variable=var).pack(side=tk.LEFT, padx=5)

        scheduler_note = ttk.Label(
            frame,
            text="具体执行时刻由“调度设置”里的每周时间统一控制，这里只决定任务在哪几天参与自动查询。",
            foreground="gray",
            font=("Arial", 9),
        )
        scheduler_note.pack(anchor=tk.W, pady=(0, 15))

        # 启用状态
        self.enabled_var = tk.BooleanVar(value=task.get('enabled', True) if task else True)
        ttk.Checkbutton(frame, text="启用此任务", variable=self.enabled_var).pack(anchor=tk.W, pady=(10, 5))

        # 检查开关（前台模式）
        self.inspect_var = tk.BooleanVar(value=task.get('inspect', False) if task else False)
        inspect_frame = ttk.Frame(frame)
        inspect_frame.pack(anchor=tk.W, pady=(0, 20))
        ttk.Checkbutton(inspect_frame, text="检查模式（运行时显示浏览器）", variable=self.inspect_var).pack(side=tk.LEFT)
        ttk.Label(inspect_frame, text="  开启后任务运行时浏览器前台显示，关闭后自动后台运行",
                  foreground="gray", font=("Arial", 9)).pack(side=tk.LEFT)

        recognition_frame = ttk.Frame(frame)
        recognition_frame.pack(fill=tk.X, pady=(0, 15))
        ttk.Label(recognition_frame, text="识别模式每组截图数:").pack(side=tk.LEFT)
        default_batch_size = (
            max(1, int(task.get('recognition_batch_size', 1) or 1))
            if task else
            self._compute_recognition_batch_default()
        )
        self.recognition_batch_var = tk.IntVar(value=default_batch_size)
        self.recognition_batch_spinbox = tk.Spinbox(recognition_frame, from_=1, to=200, width=5, textvariable=self.recognition_batch_var)
        self.recognition_batch_spinbox.pack(side=tk.LEFT, padx=(6, 12))
        ttk.Label(recognition_frame, text="达到数量后会汇总确认并排队发送", foreground="gray",
                  font=("Arial", 9)).pack(side=tk.LEFT)
        self.recognition_batch_spinbox.bind("<KeyRelease>", self._mark_recognition_batch_edited)
        self.recognition_batch_spinbox.bind("<ButtonRelease-1>", self._mark_recognition_batch_edited)

        recognition_toggle_frame = ttk.Frame(frame)
        recognition_toggle_frame.pack(fill=tk.X, pady=(0, 10))
        self.recognition_enabled_var = tk.BooleanVar(
            value=task.get('recognition_enabled', has_recognition_mode) if task else has_recognition_mode
        )
        ttk.Checkbutton(
            recognition_toggle_frame,
            text="启用识别模式监听",
            variable=self.recognition_enabled_var
        ).pack(side=tk.LEFT)
        ttk.Label(
            recognition_toggle_frame,
            text="  仅对任务模式选为“识别模式”的关键词行生效",
            foreground="gray",
            font=("Arial", 9)
        ).pack(side=tk.LEFT)

        ttk.Label(frame, text="识别品牌别名（可选，逗号分隔）:").pack(anchor=tk.W, pady=(0, 5))
        alias_value = ""
        if task:
            raw_aliases = task.get('recognition_brands', '')
            if isinstance(raw_aliases, str):
                alias_value = raw_aliases
            elif isinstance(raw_aliases, (list, tuple, set)):
                alias_value = ", ".join(str(item) for item in raw_aliases if str(item).strip())
            elif isinstance(raw_aliases, dict):
                parts = []
                for brand, aliases in raw_aliases.items():
                    if isinstance(aliases, str):
                        alias_str = aliases
                    elif isinstance(aliases, (list, tuple, set)):
                        alias_str = ", ".join(str(item) for item in aliases if str(item).strip())
                    else:
                        alias_str = ""
                    if brand and alias_str:
                        parts.append(f"{brand}: {alias_str}")
                alias_value = "；".join(parts)
        self.recognition_brands_var = tk.StringVar(value=alias_value)
        ttk.Entry(frame, textvariable=self.recognition_brands_var, width=50).pack(fill=tk.X, pady=(0, 15))

        # 已录入文章列表
        task_name = task.get('name', '') if task else ''
        if task_name:
            articles = get_articles_by_task(task_name)
            ttk.Separator(frame, orient='horizontal').pack(fill=tk.X, pady=(10, 8))
            ttk.Label(frame, text=f"总发表文章（{len(articles)} 篇）:").pack(anchor=tk.W, pady=(0, 5))
            art_container = tk.Frame(frame)
            art_container.pack(fill=tk.X, pady=(0, 10))
            if articles:
                for art in articles:
                    art_row = tk.Frame(art_container, relief='flat', bd=0)
                    art_row.pack(fill=tk.X, pady=2)
                    media = art.get('media_type', 'selfmedia')
                    media_label = '权威媒体' if media == 'authority' else '自媒体'
                    media_color = '#2D7FF9' if media == 'authority' else '#F5A623'
                    ts = art.get('ts', '')
                    title = art.get('title', '') or art.get('url', '')
                    tk.Label(
                        art_row, text=f"[{media_label}]",
                        fg=media_color, font=('Arial', 9, 'bold'), width=8, anchor='w'
                    ).pack(side=tk.LEFT)
                    tk.Label(
                        art_row, text=ts,
                        fg='gray', font=('Arial', 9), width=14, anchor='w'
                    ).pack(side=tk.LEFT)
                    tk.Label(
                        art_row, text=title,
                        fg='black', font=('Arial', 9), anchor='w', justify='left',
                        wraplength=300
                    ).pack(side=tk.LEFT, fill=tk.X, expand=True)
            else:
                tk.Label(art_container, text='暂无录入文章', fg='gray', font=('Arial', 9)).pack(anchor=tk.W)

        self._build_brand_trend_section(frame, task)

        # 按钮
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(btn_frame, text="🚀 立即运行测试", command=self.run_now).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="保存", command=self.save).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="取消", command=self._close_dialog).pack(side=tk.RIGHT, padx=5)
        bind_mousewheel_recursive(frame, on_mousewheel)

    def _get_current_brand_trend_scope(self) -> tuple[str, list[str], str]:
        preview_var = getattr(self, "_task_name_preview", None)
        task_name = str(preview_var.get() if preview_var is not None else "").strip()
        if not task_name:
            task_name = str((self._original_task or {}).get("name") or "").strip()
        task_id = str((self._original_task or {}).get("task_id") or "").strip()

        brands: list[str] = []
        for row in self._kw_rows:
            brand = str(row["brand"].get() or "").strip()
            if brand and brand not in brands:
                brands.append(brand)

        if not brands:
            brands = _task_brand_names(self._original_task)

        if not task_name and brands:
            task_name = brands[0]
        if not brands and task_name:
            brands = [task_name]
        return task_name, brands, task_id

    def _build_brand_trend_series(self, days: int) -> dict | None:
        task_name, brands, task_id = self._get_current_brand_trend_scope()
        if not task_name:
            return None

        try:
            return get_brand_trend_series(
                task_name,
                brands,
                days,
                task_id=task_id,
                task_created_at=str((self._original_task or {}).get("created_at") or "").strip(),
            )
        except Exception:
            return None

    def _draw_brand_trend_chart(self, canvas: tk.Canvas, series: dict | None, period_label: str):
        canvas.delete("all")
        W = canvas.winfo_width() or 620
        H = canvas.winfo_height() or 230
        if not series:
            canvas.create_text(W // 2, H // 2, text="暂无该品牌历史趋势", font=("Arial", 11), fill="#888")
            return

        dates = series["dates"]
        actual = series["actual"]
        predicted = series["predicted"]
        summary = series["summary"]
        n = len(dates)
        if n <= 1:
            canvas.create_text(W // 2, H // 2, text="数据不足，保存后再查看趋势", font=("Arial", 11), fill="#888")
            return

        pad_l, pad_r, pad_t, pad_b = 40, 18, 18, 40
        chart_h = max(80, H - pad_t - pad_b)
        chart_w = max(100, W - pad_l - pad_r)

        def cx(i: int) -> float:
            return pad_l + (i / max(n - 1, 1)) * chart_w

        def cy(value: float) -> float:
            return pad_t + (1 - value / 100) * chart_h

        for pct, lbl in ((100, "100%"), (75, "75%"), (50, "50%"), (25, "25%"), (0, "0%")):
            yy = cy(pct)
            canvas.create_line(pad_l, yy, W - pad_r, yy, fill="#E7EBF2", dash=(3, 4))
            if pct in (100, 50, 0):
                canvas.create_text(pad_l - 4, yy, text=lbl, anchor="e", font=("Arial", 8), fill="#888")

        predicted_points = []
        for i, value in enumerate(predicted):
            predicted_points.extend([cx(i), cy(value)])
        if len(predicted_points) >= 4:
            canvas.create_line(*predicted_points, smooth=True, splinesteps=36, fill="#94A3B8", width=2, dash=(5, 4))

        actual_points = []
        for i, value in enumerate(actual):
            if value is None:
                if len(actual_points) >= 4:
                    canvas.create_line(*actual_points, smooth=True, splinesteps=36, fill="#2563EB", width=3)
                actual_points = []
                continue
            actual_points.extend([cx(i), cy(value)])
        if len(actual_points) >= 4:
            canvas.create_line(*actual_points, smooth=True, splinesteps=36, fill="#2563EB", width=3)

        for i, value in enumerate(actual):
            if value is None:
                continue
            x, y = cx(i), cy(value)
            canvas.create_oval(x - 3.5, y - 3.5, x + 3.5, y + 3.5, fill="#2563EB", outline="white", width=1)

        last_idx = next((idx for idx in range(n - 1, -1, -1) if actual[idx] is not None), None)
        if last_idx is not None:
            x, y = cx(last_idx), cy(actual[last_idx])
            canvas.create_oval(x - 5, y - 5, x + 5, y + 5, outline="#2563EB", width=2)

        if period_label == "周":
            label_step = 1
        elif period_label == "月":
            label_step = max(1, n // 6)
        else:
            label_step = max(1, n // 12)
        for i, current_date in enumerate(dates):
            if i % label_step != 0 and i != n - 1:
                continue
            label = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][current_date.weekday()] if period_label == "周" else current_date.strftime("%m-%d")
            canvas.create_text(cx(i), H - pad_b + 12, text=label, font=("Arial", 8), fill="#888")

        canvas.create_text(
            pad_l,
            H - 8,
            text=f"当前 {summary['current']:g}%  ·  峰值 {summary['peak']:g}%  ·  均值 {summary['average']:g}%",
            anchor="w",
            font=("Arial", 8),
            fill="#888",
        )

    def _refresh_brand_trend(self):
        redraw = getattr(self, "_brand_trend_redraw", None)
        if callable(redraw):
            redraw()

    def _build_brand_trend_section(self, frame, task):
        ttk.Separator(frame, orient='horizontal').pack(fill=tk.X, pady=(10, 10))

        card = tk.Frame(frame, bg=_SURFACE, relief='flat', bd=0, highlightbackground=_BORDER, highlightthickness=1)
        card.pack(fill=tk.X, pady=(0, 10))

        header = tk.Frame(card, bg=_SURFACE)
        header.pack(fill=tk.X, padx=14, pady=(12, 8))

        left = tk.Frame(header, bg=_SURFACE)
        left.pack(side=tk.LEFT, anchor='w')
        tk.Label(left, text="品牌趋势", bg=_SURFACE, fg=_TEXT_PRI, font=("Arial", 12, "bold")).pack(anchor='w')
        self._brand_trend_title = tk.Label(left, text="", bg=_SURFACE, fg=_TEXT_TER, font=("Arial", 8))
        self._brand_trend_title.pack(anchor='w', pady=(2, 0))

        right = tk.Frame(header, bg=_SURFACE)
        right.pack(side=tk.RIGHT, anchor='e')
        range_var = tk.StringVar(value="周")
        for label in ("周", "月", "年"):
            tk.Radiobutton(
                right,
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
                width=4,
                font=("Arial", 9, "bold"),
                padx=8,
                pady=4,
                command=lambda: redraw(),
            ).pack(side=tk.LEFT, padx=(0, 6))

        canvas = tk.Canvas(card, bg=_SURFACE, highlightthickness=0, height=220)
        canvas.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 10))

        metrics = tk.Frame(card, bg=_SURFACE)
        metrics.pack(fill=tk.X, padx=14, pady=(0, 12))

        current_lbl = tk.Label(metrics, text="--%", bg=_SURFACE, fg=_TEXT_PRI, font=("Arial", 24, "bold"))
        current_lbl.pack(side=tk.LEFT, padx=(0, 16))
        delta_lbl = tk.Label(metrics, text="", bg=_SURFACE, fg=_SUCCESS, font=("Arial", 10, "bold"))
        delta_lbl.pack(side=tk.LEFT, padx=(0, 20))
        peak_lbl = tk.Label(metrics, text="峰值 --%", bg=_SURFACE, fg=_TEXT_SEC, font=("Arial", 10, "bold"))
        peak_lbl.pack(side=tk.LEFT, padx=(0, 14))
        avg_lbl = tk.Label(metrics, text="均值 --%", bg=_SURFACE, fg=_TEXT_SEC, font=("Arial", 10, "bold"))
        avg_lbl.pack(side=tk.LEFT)

        def redraw():
            period_label = range_var.get()
            days = {"周": 7, "月": 30, "年": 365}.get(period_label, 30)
            task_name, brands, _task_id = self._get_current_brand_trend_scope()
            title_text = task_name or "未命名品牌"
            if brands:
                title_text = f"{title_text} · {' / '.join(brands[:3])}"
            self._brand_trend_title.config(text=f"当前统计：{title_text}")
            series = self._build_brand_trend_series(days)
            if series:
                summary = series["summary"]
                current_lbl.config(text=f"{summary['current']:g}%")
                delta = summary["delta"]
                if delta > 0:
                    delta_lbl.config(text=f"↗ {abs(delta):g}%", fg=_SUCCESS)
                elif delta < 0:
                    delta_lbl.config(text=f"↘ {abs(delta):g}%", fg=_DANGER)
                else:
                    delta_lbl.config(text="→ 0%", fg=_TEXT_TER)
                peak_lbl.config(text=f"峰值 {summary['peak']:g}%")
                avg_lbl.config(text=f"均值 {summary['average']:g}%")
            else:
                current_lbl.config(text="--%")
                delta_lbl.config(text="", fg=_TEXT_TER)
                peak_lbl.config(text="峰值 --%")
                avg_lbl.config(text="均值 --%")
            self._draw_brand_trend_chart(canvas, series, period_label)

        self._brand_trend_redraw = redraw
        redraw()

    def _close_dialog(self):
        self._stop_recognition_test()
        self.dialog.destroy()

    def _add_keyword_row(self, keyword='', brand='', platforms=None, deep_think=None, mode='browser'):
        """动态添加一行关键词配置"""
        platforms = platforms or []
        deep_think = deep_think or {}
        row_idx = len(self._kw_rows)
        is_first = row_idx == 0

        outer = ttk.LabelFrame(self._kw_container, padding="8")
        outer.pack(fill=tk.X, pady=(0, 8))

        # 第一行：关键词 + 品牌 + 删除
        top_row = ttk.Frame(outer)
        top_row.pack(fill=tk.X)

        ttk.Label(top_row, text="关键词:").pack(side=tk.LEFT)
        kw_var = tk.StringVar(value=keyword)
        ttk.Entry(top_row, textvariable=kw_var, width=20).pack(side=tk.LEFT, padx=(4, 12))

        brand_hint = "(默认品牌)" if is_first else "(留空同上)"
        ttk.Label(top_row, text=f"品牌{brand_hint}:").pack(side=tk.LEFT)
        brand_var = tk.StringVar(value=brand)
        ttk.Entry(top_row, textvariable=brand_var, width=16).pack(side=tk.LEFT, padx=(4, 12))

        del_btn = ttk.Button(top_row, text="删除", command=lambda f=outer: self._remove_keyword_row(f))
        del_btn.pack(side=tk.RIGHT)

        # 第二行：平台多选
        plat_row = ttk.Frame(outer)
        plat_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(plat_row, text="平台:").pack(side=tk.LEFT)
        plat_vars = {}
        no_deep_think = {'kimi'}
        for code, name in PLATFORMS:
            var = tk.BooleanVar(value=code in platforms)
            plat_vars[code] = var
            ttk.Checkbutton(plat_row, text=name, variable=var).pack(side=tk.LEFT, padx=4)

        # 第三行：深度思考（仅支持的平台）
        dt_row = ttk.Frame(outer)
        dt_row.pack(fill=tk.X, pady=(2, 0))
        ttk.Label(dt_row, text="深度思考:").pack(side=tk.LEFT)
        deep_think_vars = {}
        for code, name in PLATFORMS:
            if code not in no_deep_think:
                dt_val = deep_think.get(code, False) if isinstance(deep_think, dict) else False
                dt_var = tk.BooleanVar(value=dt_val)
                deep_think_vars[code] = dt_var
                ttk.Checkbutton(dt_row, text=name, variable=dt_var).pack(side=tk.LEFT, padx=4)

        row_data = {'keyword': kw_var, 'brand': brand_var, 'platform_vars': plat_vars, 'deep_think_vars': deep_think_vars, 'frame': outer}
        self._kw_rows.append(row_data)

        # 第一行品牌变化时自动同步任务名
        if is_first:
            def on_first_brand(*_):
                if hasattr(self, "_task_name_preview"):
                    self._task_name_preview.set(brand_var.get().strip())
                self._refresh_brand_trend()
            brand_var.trace_add('write', on_first_brand)
        else:
            brand_var.trace_add('write', lambda *_: self._refresh_brand_trend())

        kw_var.trace_add('write', lambda *_: self._refresh_default_recognition_batch())
        brand_var.trace_add('write', lambda *_: self._refresh_default_recognition_batch())
        for var in plat_vars.values():
            var.trace_add('write', lambda *_: self._refresh_default_recognition_batch())

        # 绑定滚轮
        bind_mousewheel_recursive(outer, self._on_mousewheel)

        self._form_frame.update_idletasks()
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        self._refresh_default_recognition_batch()

    def _remove_keyword_row(self, frame):
        """删除一行关键词"""
        if len(self._kw_rows) <= 1:
            messagebox.showwarning("提示", "至少保留一个关键词")
            return
        self._kw_rows = [r for r in self._kw_rows if r['frame'] is not frame]
        frame.destroy()
        self._form_frame.update_idletasks()
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        self._refresh_brand_trend()
        self._refresh_default_recognition_batch()

    def _mark_recognition_batch_edited(self, _event=None):
        self._recognition_batch_manually_edited = True

    def _collect_keyword_payloads(self):
        keywords = []
        for row in self._kw_rows:
            kw = row['keyword'].get().strip()
            brand = row['brand'].get().strip()
            platforms = [code for code, var in row['platform_vars'].items() if var.get()]
            deep_think = {code: var.get() for code, var in row.get('deep_think_vars', {}).items()}
            if kw:
                keywords.append({'keyword': kw, 'brand': brand, 'platforms': platforms, 'deep_think': deep_think})
        return keywords

    def _compute_recognition_batch_default(self) -> int:
        return compute_recognition_batch_size_from_keywords(self._collect_keyword_payloads())

    def _refresh_default_recognition_batch(self):
        if self._recognition_batch_manually_edited:
            return
        try:
            self.recognition_batch_var.set(self._compute_recognition_batch_default())
        except Exception:
            pass

    def save(self):
        """保存任务配置"""
        keywords = self._collect_keyword_payloads()

        if not keywords:
            messagebox.showerror('错误', '请至少添加一个关键词')
            return
        if not keywords[0].get('brand'):
            messagebox.showerror('错误', '第一个关键词的品牌名不能为空')
            return
        for i, kw_entry in enumerate(keywords):
            if not kw_entry['platforms']:
                messagebox.showerror('错误', f'第{i+1}个关键词请至少选择一个平台')
                return

        weekdays = [i for i, var in enumerate(self.weekday_vars) if var.get()]
        if not weekdays:
            messagebox.showerror('错误', '请至少选择一天运行')
            return

        webhook = self.webhook_var.get().strip()
        if webhook and not webhook.startswith('https://'):
            messagebox.showerror('错误', 'Webhook URL 格式不正确，应以 https:// 开头')
            return

        result = dict(self._original_task)
        for legacy_key in ('keyword', 'brand', 'platform'):
            result.pop(legacy_key, None)
        result.pop('schedule', None)
        result.pop('interval', None)

        result.update({
            'name': keywords[0]['brand'],
            'industry_tags': _parse_tag_text(self.industry_tags_var.get()),
            'region_tags': _parse_tag_text(self.region_tags_var.get()),
            'keywords': keywords,
            'webhook_url': webhook,
            'weekdays': weekdays,
            'enabled': self.enabled_var.get(),
            'inspect': self.inspect_var.get(),
            'recognition_enabled': self.recognition_enabled_var.get(),
            'recognition_brands': self.recognition_brands_var.get().strip(),
            'recognition_batch_size': max(1, int(self.recognition_batch_var.get() or 1)),
            'optimization_start_date': self.opt_start_var.get().strip(),
            'optimization_end_date': self.opt_end_var.get().strip(),
        })
        assign_task_id(result)
        if self._is_new_task and not str(result.get('created_at') or '').strip():
            result['created_at'] = local_now().isoformat(timespec='seconds')

        # 记录优化周期到历史 sidecar 文件
        opt_start = result.get('optimization_start_date', '')
        opt_end = result.get('optimization_end_date', '')
        if opt_start and opt_end:
            try:
                from core.history import save_optimization_period
                task_name = result.get('name') or ''
                save_optimization_period(task_name, opt_start, opt_end)
            except Exception:
                pass

        self.result = result
        self._close_dialog()

    def _refresh_test_keyword_guide_ui(self):
        if not self._test_keyword_guide or not self._test_recognition_manager:
            return
        try:
            state = self._test_recognition_manager.get_keyword_guide_state() or {}
        except Exception:
            state = {}
        self._test_keyword_guide.set_items(state.get("items") or [])
        self._test_keyword_guide.set_index(int(state.get("index", 0) or 0))
        self._test_keyword_guide.set_display_options(
            mode_label="测试模式 · 手动确认" if state.get("manual_mode") else "测试模式 · OCR 自动识别",
            controls_visible=state.get("controls_visible", state.get("manual_mode")),
        )
        self._test_keyword_guide.set_action_state(
            text=state.get("action_label"),
            enabled=state.get("action_enabled"),
            detail=state.get("detail_text", ""),
        )

    def _handle_test_keyword_guide_action(self):
        if not self._test_recognition_manager:
            return
        try:
            self._test_recognition_manager.handle_keyword_guide_action()
        except Exception as e:
            messagebox.showerror("识别测试失败", f"人工确认失败: {e}")
        self._refresh_test_keyword_guide_ui()

    def _on_test_recognition_batch_ready(self, batch):
        self.dialog.after(0, self._refresh_test_keyword_guide_ui)

    def _on_test_recognition_send_complete(self, batch, ok, reason):
        def _show():
            title = "识别测试成功" if ok else "识别测试失败"
            current_count = int(batch.get("current_image_count") or len(batch.get("image_paths") or []))
            historical_count = int(batch.get("historical_screenshot_count") or 0)
            total_count = int(batch.get("total_image_count") or len(batch.get("image_paths") or []))
            outcome_text = "发送成功" if ok else f"发送失败: {reason or '未知错误'}"
            log_message = (
                f"[RecognitionTest] {batch['task_name']} "
                f"{outcome_text}，"
                f"累计 {total_count} 张（本次 {current_count} 张，历史 {historical_count} 张）"
            )
            print(log_message)
            message = (
                f"品牌: {batch['task_name']}\n"
                f"品牌: {', '.join(batch.get('brands', [])) or '未识别'}\n"
                f"截图数: 累计 {total_count} 张（本次 {current_count} 张，历史 {historical_count} 张）\n\n"
            )
            if ok:
                message += "企业微信已发送。"
            else:
                message += f"企业微信发送失败：{reason or '未知错误'}"
            messagebox.showinfo(title, message) if ok else messagebox.showerror(title, message)
            self._stop_recognition_test()

        self.dialog.after(0, _show)

    def _on_test_recognition_mode_change(self, payload):
        self.dialog.after(0, self._refresh_test_keyword_guide_ui)

    def _on_test_recognition_manual_switch_required(self, payload):
        message = (payload or {}).get("message", "").strip() or "识别模式长时间未收到新截图。"
        self.dialog.after(0, lambda msg=message: messagebox.showwarning("识别测试提醒", msg))

    def _on_test_recognition_manual_state_change(self, payload=None):
        self.dialog.after(0, self._refresh_test_keyword_guide_ui)

    def _stop_recognition_test(self):
        if self._test_recognition_manager:
            try:
                self._test_recognition_manager.stop()
            except Exception:
                pass
            self._test_recognition_manager = None
        self._test_runtime_config = None
        if self._test_keyword_guide:
            try:
                self._test_keyword_guide.destroy()
            except Exception:
                pass
            self._test_keyword_guide = None
        self._test_status_win = None
        self._test_status_label = None

    def _load_runtime_config(self) -> dict:
        """读取运行时配置，保持与主程序一致的合并规则。"""
        try:
            return load_yaml_config(resolve_app_path("config.yaml"))
        except Exception:
            return {}

    def _prepare_manual_test_task(self, task: dict, *, recognition_batch_size: int | None = None) -> dict:
        """为手动测试补齐运行所需字段。"""
        prepared = dict(task or {})
        prepared['enabled'] = True
        prepared['_daily_state_source'] = 'manual_test'
        if recognition_batch_size is not None:
            prepared['recognition_enabled'] = True
            prepared['recognition_batch_size'] = recognition_batch_size
        assign_task_id(prepared)
        return prepared

    def _build_manual_test_runtime_config(
        self,
        task: dict,
        *,
        recognition_batch_size: int | None = None,
    ) -> tuple[dict, dict]:
        """统一组装手动测试运行配置，避免多处各自读取并覆写 tasks。"""
        runtime_config = copy.deepcopy(self._load_runtime_config())
        if recognition_batch_size is not None:
            runtime_config['detection_mode'] = 'recognition'
        test_task = self._prepare_manual_test_task(
            task,
            recognition_batch_size=recognition_batch_size,
        )
        runtime_config['tasks'] = [test_task]
        return runtime_config, test_task

    def _prime_manual_test_status(self, task: dict):
        task_payload = dict(task or {})
        if not task_payload:
            return
        try:
            write_task_status(
                task_payload,
                status="running",
                source="manual_test",
                scope="test",
                message="识别模式测试已启动",
                extra=build_task_state_extra(
                    brands=[str(task_payload.get('brand') or task_payload.get('name') or '').strip()],
                    image_count=0,
                    fixed_screenshot_target=max(1, int(task_payload.get('recognition_batch_size', 1) or 1)),
                    completed_by_quota=False,
                ),
            )
        except Exception as exc:
            print(f"[ConfigManager] 预写识别测试状态失败，已忽略: {exc}")

    def _start_recognition_test(self, task: dict):
        self._stop_recognition_test()

        batch_size = max(1, int(task.get('recognition_batch_size', 1) or 1))
        runtime_config, test_task = self._build_manual_test_runtime_config(
            task,
            recognition_batch_size=batch_size,
        )
        self._prime_manual_test_status(test_task)
        self._test_runtime_config = runtime_config
        self._test_recognition_manager = ClipboardRecognitionManager(
            config_getter=lambda: self._test_runtime_config,
            on_batch_ready=self._on_test_recognition_batch_ready,
            on_send_complete=self._on_test_recognition_send_complete,
            on_mode_change=self._on_test_recognition_mode_change,
            on_manual_switch_required=self._on_test_recognition_manual_switch_required,
            on_manual_state_change=self._on_test_recognition_manual_state_change,
        )
        self._test_recognition_manager.start()

        items = self._test_recognition_manager.get_keyword_guide_state().get("items") or []
        if items:
            self._test_keyword_guide = KeywordGuideWindow(
                self.dialog,
                items,
                on_action=self._handle_test_keyword_guide_action,
                mode_label="测试模式 · 手动确认" if self._test_recognition_manager.get_keyword_guide_state().get("manual_mode") else "测试模式 · OCR 自动识别",
                controls_visible=self._test_recognition_manager.get_keyword_guide_state().get("controls_visible", self._test_recognition_manager.get_keyword_guide_state().get("manual_mode")),
            )
            self._refresh_test_keyword_guide_ui()

    def run_now(self):
        """立即运行测试"""
        keywords = []
        for row in self._kw_rows:
            kw = row['keyword'].get().strip()
            brand = row['brand'].get().strip()
            platforms = [code for code, var in row['platform_vars'].items() if var.get()]
            deep_think = {code: var.get() for code, var in row.get('deep_think_vars', {}).items()}
            if kw:
                keywords.append({'keyword': kw, 'brand': brand, 'platforms': platforms, 'deep_think': deep_think})

        if not keywords or not keywords[0].get('brand'):
            messagebox.showerror('错误', '请填写关键词和第一个品牌名')
            return

        task = {
            'name': keywords[0]['brand'],
            'industry_tags': _parse_tag_text(self.industry_tags_var.get()),
            'region_tags': _parse_tag_text(self.region_tags_var.get()),
            'keywords': keywords,
            'webhook_url': self.webhook_var.get().strip(),
            'weekdays': [0, 1, 2, 3, 4, 5, 6],
            'enabled': True,
            'recognition_enabled': self.recognition_enabled_var.get(),
            'recognition_brands': self.recognition_brands_var.get().strip(),
            'recognition_batch_size': max(1, int(self.recognition_batch_var.get() or 1)),
            '_daily_state_source': 'manual_test',
        }
        if self._original_task.get('task_id'):
            task['task_id'] = self._original_task['task_id']
        runtime_config, task = self._build_manual_test_runtime_config(task)

        recognition_keywords = [
            kw for kw in keywords
            if kw.get('mode', 'browser') == 'recognition' and kw.get('keyword', '').strip()
        ]
        query_count = sum(
            len(kw.get('platforms', []))
            for kw in keywords
            if kw.get('mode', 'browser') != 'recognition'
            and kw.get('keyword', '').strip()
            and ((kw.get('brand', '').strip()) or keywords[0].get('brand', '').strip())
            and kw.get('platforms', [])
        )

        import threading
        def do_run():
            try:
                import sys
                from pathlib import Path as _Path
                _root = str(_Path(__file__).parent.parent)
                if _root not in sys.path:
                    sys.path.insert(0, _root)
                from core.task_executor import run_task_group
                print("\n" + "="*50)
                print(f"开始测试运行: {task['name']}")
                print("="*50)
                if recognition_keywords and query_count <= 0:
                    print("\n测试结果: 已直接启动识别模式测试，等待新的剪切板截图")
                    self.dialog.after(0, lambda t=task: self._start_recognition_test(t))
                    return

                run_output = run_task_group(
                    task,
                    {'cooldown_minutes': 1, 'send_interval': 2},
                    config=runtime_config,
                    force_notify=True,
                    execution_source='manual_test',
                    return_report=True,
                )
                if isinstance(run_output, tuple):
                    results, execution_report = run_output
                else:
                    results, execution_report = run_output, {}

                found = [r for r in results if r['rank'] != 99]
                notify_attempted = bool(execution_report.get('notification_attempted'))
                notify_success = bool(execution_report.get('notification_success'))
                notify_error = str(execution_report.get('notification_error') or '').strip()

                print(f"\n测试结果: {len(found)}/{len(results)} 个找到品牌")
                if found:
                    message = f"{len(found)}/{len(results)} 个平台检测到品牌提及"
                    if notify_success:
                        message += "\n\n企业微信截图已发送。"
                    elif notify_attempted:
                        message += f"\n\n企业微信未发送成功：{notify_error or '未知错误'}"
                    else:
                        message += f"\n\n本次未发送企业微信截图：{notify_error or '未触发通知'}"
                    if recognition_keywords:
                        message += "\n\n识别模式关键词仍需保存任务后，在主界面右上角点击“仅启动识别”或“开始监控”，并通过剪切板新截图验证。"
                    self.dialog.after(
                        0,
                        lambda msg=message: messagebox.showinfo('测试成功', msg)
                    )
                else:
                    message = '未检测到品牌提及'
                    if recognition_keywords:
                        message = (
                            '主动查询未检测到品牌提及。\n\n'
                            '识别模式关键词不会立即返回结果，仍需保存任务后，在主界面右上角点击“仅启动识别”或“开始监控”，并复制新的截图到剪切板。'
                        )
                    self.dialog.after(
                        0,
                        lambda msg=message: messagebox.showinfo('测试完成', msg)
                    )
            except Exception as e:
                import traceback
                self.dialog.after(
                    0,
                    lambda msg=f'错误: {e}\n{traceback.format_exc()}': messagebox.showerror('运行失败', msg)
                )

        threading.Thread(target=do_run, daemon=True).start()
        messagebox.showinfo('提示', '测试任务正在后台运行，请查看终端输出')

class SchedulerConfigDialog:
    """全局调度设置。"""

    def __init__(self, parent, scheduler_config=None):
        self.result = None
        scheduler_config = dict(scheduler_config or {})
        weekly_times = normalize_weekly_times(scheduler_config)

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("调度设置")
        self.dialog.geometry("520x420")
        self.dialog.transient(parent)
        self.dialog.grab_set()

        frame = ttk.Frame(self.dialog, padding="20")
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="每周自动查询时间", font=("Arial", 16, "bold")).pack(anchor=tk.W)
        ttk.Label(
            frame,
            text="同一天所有查询任务共用同一个执行时刻。未勾选的日期不会自动运行。",
            foreground="gray",
            font=("Arial", 10),
        ).pack(anchor=tk.W, pady=(6, 18))

        self._day_enabled_vars = {}
        self._hour_vars = {}
        self._minute_vars = {}

        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        minute_values = ("00", "05", "10", "15", "20", "25", "30", "35", "40", "45", "50", "55")

        for weekday, label in enumerate(weekday_names):
            row = ttk.Frame(frame)
            row.pack(fill=tk.X, pady=4)

            time_text = weekly_times.get(str(weekday))
            enabled_var = tk.BooleanVar(value=bool(time_text))
            hour_text, minute_text = ("09", "30")
            if time_text:
                hour_text, minute_text = time_text.split(":")

            self._day_enabled_vars[str(weekday)] = enabled_var
            self._hour_vars[str(weekday)] = tk.StringVar(value=hour_text)
            self._minute_vars[str(weekday)] = tk.StringVar(value=minute_text)

            ttk.Checkbutton(row, text=label, variable=enabled_var).pack(side=tk.LEFT, padx=(0, 10))
            ttk.Label(row, text="小时").pack(side=tk.LEFT)
            tk.Spinbox(
                row,
                from_=0,
                to=23,
                width=5,
                format="%02.0f",
                textvariable=self._hour_vars[str(weekday)],
            ).pack(side=tk.LEFT, padx=(4, 14))
            ttk.Label(row, text="分钟").pack(side=tk.LEFT)
            tk.Spinbox(
                row,
                values=minute_values,
                width=5,
                textvariable=self._minute_vars[str(weekday)],
            ).pack(side=tk.LEFT, padx=(4, 0))

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, pady=(20, 0))
        ttk.Button(btn_frame, text="取消", command=self.dialog.destroy).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="保存", command=self.save).pack(side=tk.RIGHT, padx=5)

    def save(self):
        weekly_times = {}
        for weekday in range(7):
            key = str(weekday)
            if not self._day_enabled_vars[key].get():
                weekly_times[key] = None
                continue

            hour = self._hour_vars[key].get().zfill(2)
            minute = self._minute_vars[key].get().zfill(2)
            weekly_times[key] = f"{hour}:{minute}"

        self.result = {
            "weekly_times": weekly_times,
        }
        self.dialog.destroy()


class ConfigManagerWindow:
    """
    配置管理窗口
    管理所有品牌：添加、编辑、删除
    """

    def __init__(self, config_path="config.yaml", results=None, parent=None, on_config_change=None):
        self.config_path = resolve_app_path(config_path)
        self.config = self.load_config()
        self.results = results or {}
        self.on_config_change = on_config_change
        self._config_poll_interval_ms = 1500
        self._config_watcher = None

        if parent is not None:
            self.root = tk.Toplevel(parent)
            self._owns_mainloop = False
        else:
            self.root = tk.Tk()
            self._owns_mainloop = True
        install_global_tk_behaviors(self.root)

        self.root.title("Surfaced - 品牌配置管理")
        self.root.geometry("900x600")
        self.root.configure(bg=_BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.create_ui()
        self._bind_config_update_listener()
        self._start_config_watcher()
        self.refresh_task_list()
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
        self.config = new_config
        self.refresh_task_list()

    def _poll_config_file(self):
        try:
            new_config = self.load_config()
            if new_config:
                self.config = new_config
                self.refresh_task_list()
        finally:
            self._schedule_config_poll()

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
            new_config = self.load_config()
        except Exception:
            return
        if not new_config:
            return
        self.config = new_config
        self.refresh_task_list()

    def load_config(self) -> dict:
        """加载配置"""
        try:
            config = load_yaml_config(self.config_path)
            ensure_config_task_ids(config)
            return config
        except:
            return {
                'scheduler': {
                    'weekly_times': {
                        '0': '09:30', '1': '09:30', '2': '09:30',
                        '3': '09:30', '4': '09:30', '5': None, '6': None,
                    }
                },
                'tasks': []
            }

    def save_config(self):
        """保存配置到文件"""
        return self._persist_config(show_feedback=True, success_message="配置已保存！")

    def _persist_config(self, *, show_feedback: bool = False, success_message: str = "配置已保存！") -> bool:
        try:
            if show_feedback:
                ok = persist_config_with_feedback(
                    self.config,
                    self.config_path,
                    success_message=success_message,
                    ensure_task_ids=True,
                    on_config_change=self.on_config_change,
                    event_root=self.root,
                )
                if ok:
                    self.config = self.load_config()
                    self.refresh_task_list()
                return ok
            saved_config = persist_config(
                self.config,
                self.config_path,
                ensure_task_ids=True,
                on_config_change=self.on_config_change,
                event_root=self.root,
            )
            if isinstance(saved_config, dict) and saved_config:
                self.config = saved_config
            else:
                self.config = self.load_config()
            self.refresh_task_list()
            return True
        except Exception as e:
            messagebox.showerror("错误", f"保存失败: {e}")
            return False

    def _reload_and_refresh(self, *, scroll_to_end: bool = False):
        try:
            self.config = self.load_config()
        except Exception:
            pass
        self.refresh_task_list()
        try:
            self.canvas.update_idletasks()
            self.canvas.config(scrollregion=self.canvas.bbox("all"))
            if scroll_to_end:
                self.canvas.yview_moveto(1.0)
                self.root.after(80, lambda: self.canvas.yview_moveto(1.0))
            self.root.update_idletasks()
            self.root.update()
        except Exception:
            pass

    def _on_close(self):
        self._stop_config_watcher()
        if self._persist_config(show_feedback=False):
            self.root.destroy()

    def create_ui(self):
        """创建界面"""
        # 标题
        title = ttk.Label(
            self.root,
            text="品牌配置管理",
            font=("Arial", 16, "bold")
        )
        title.pack(pady=10)

        # 说明
        desc = ttk.Label(
            self.root,
            text="品牌决定哪几天参与自动查询，具体每天几点查由统一调度设置控制",
            font=("Arial", 10)
        )
        desc.pack()

        # 任务列表
        list_frame = tk.Frame(self.root, bg=_BG, padx=20, pady=20)
        list_frame.pack(fill=tk.BOTH, expand=True)

        # 列表标题
        headers = tk.Frame(list_frame, bg=_BG)
        headers.pack(fill=tk.X, pady=(0, 5))

        for text, w in [("状态", 10), ("品牌", 20), ("关键词/平台", 16), ("文章总数", 10), ("Webhook", 18), ("运行时间", 15)]:
            tk.Label(headers, text=text, width=w, bg=_BG, fg=_TEXT_TER,
                     font=("Arial", 8, "bold"), anchor="w").pack(side=tk.LEFT)

        tk.Frame(list_frame, bg=_BORDER, height=1).pack(fill=tk.X)

        # 任务列表（带滚动条）
        canvas_frame = tk.Frame(list_frame, bg=_BG)
        canvas_frame.pack(fill=tk.BOTH, expand=True, pady=10)

        scrollbar = ttk.Scrollbar(canvas_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.canvas = tk.Canvas(canvas_frame, yscrollcommand=scrollbar.set, bg=_BG, highlightthickness=0)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar.config(command=self.canvas.yview)

        self.tasks_frame = tk.Frame(self.canvas, bg=_BG)
        self._tasks_window = self.canvas.create_window((0, 0), window=self.tasks_frame, anchor=tk.NW)

        # 关键修复：Canvas 宽度变化时同步更新内部 Frame 宽度，否则 Windows 上内容不显示
        def on_canvas_configure(event):
            self.canvas.itemconfig(self._tasks_window, width=event.width)

        self.canvas.bind("<Configure>", on_canvas_configure)

        def on_main_mousewheel(event):
            return scroll_canvas_on_mousewheel(self.canvas, event)

        self._on_main_mousewheel = on_main_mousewheel

        self.canvas.bind("<MouseWheel>", on_main_mousewheel)
        self.tasks_frame.bind("<MouseWheel>", on_main_mousewheel)

        # 按钮区域
        btn_frame = ttk.Frame(self.root, padding="20")
        btn_frame.pack(fill=tk.X)

        ttk.Button(
            btn_frame,
            text="➕ 添加品牌",
            command=self.add_task
        ).pack(side=tk.LEFT, padx=5)

        ttk.Button(
            btn_frame,
            text="程序设置",
            command=self.open_settings
        ).pack(side=tk.LEFT, padx=5)

        ttk.Button(
            btn_frame,
            text="💾 保存配置",
            command=self.save_config
        ).pack(side=tk.RIGHT, padx=5)

    def refresh_task_list(self):
        """刷新任务列表显示"""
        try:
            self.config = self.load_config()
        except Exception:
            pass
        # 清空现有列表
        for widget in self.tasks_frame.winfo_children():
            widget.destroy()

        tasks = self.config.get('tasks', [])
        task_names = [str(task.get('name', '') or '').strip() for task in tasks if str(task.get('name', '') or '').strip()]
        try:
            article_counts = get_task_article_counts(task_names) if task_names else {}
        except Exception:
            article_counts = {}

        for i, task in enumerate(tasks):
            card = tk.Frame(
                self.tasks_frame,
                bg=_SURFACE,
                highlightbackground=_BORDER,
                highlightthickness=1,
            )
            card.pack(fill=tk.X, pady=(0, 4))

            row = tk.Frame(card, bg=_SURFACE)
            row.pack(fill=tk.X, padx=14, pady=11)

            # 状态：检查所有关键词×平台结果
            task_name = str(task.get('name', '') or '').strip()
            keywords = task.get('keywords', [])
            # 向后兼容旧格式
            if not keywords and task.get('keyword'):
                keywords = [{'keyword': task['keyword'], 'brand': task.get('brand', ''), 'platforms': [task.get('platform', '')]}]
            default_brand = keywords[0].get('brand', '') if keywords else ''
            brand_page = _task_brand_page(task) or task_name or default_brand or "未命名品牌页"
            tag_summary = _task_tag_summary(task)
            article_total = int(article_counts.get(task_name, 0) or 0) if task_name else 0

            if not task.get('enabled', True):
                status = "⏸️"
                fg = "gray"
            else:
                # 找任意一个有结果的 (task_name, platform, brand)
                found_ranks = [
                    self.results[(task_name, p, kw.get('brand') or default_brand)]
                    for kw in keywords
                    for p in kw.get('platforms', [])
                    if (task_name, p, kw.get('brand') or default_brand) in self.results
                ]
                recognition_brands = list(dict.fromkeys(
                    (kw.get('brand') or default_brand)
                    for kw in keywords
                    if kw.get('mode', 'browser') == 'recognition' and (kw.get('brand') or default_brand)
                ))
                found_ranks.extend(
                    self.results[(task_name, 'recognition', brand)]
                    for brand in recognition_brands
                    if (task_name, 'recognition', brand) in self.results
                )
                if found_ranks:
                    best = min(found_ranks)
                    if best == 99:
                        status, fg = "❌ 未提及", "red"
                    else:
                        status, fg = "✅ 已提及", "green"
                else:
                    status, fg = "— 待运行", "gray"
            dot_frame = tk.Frame(row, bg=_SURFACE, width=56)
            dot_frame.pack(side=tk.LEFT, padx=(0, 4))
            dot_frame.pack_propagate(False)
            dot = tk.Canvas(dot_frame, width=8, height=8, bg=_SURFACE, highlightthickness=0)
            dot.pack(side=tk.LEFT, padx=(0, 4), pady=2)
            dot.create_oval(1, 1, 7, 7, fill=fg if status != "⏸️" else _TEXT_TER, outline="")
            tk.Label(
                dot_frame,
                text=status,
                bg=_SURFACE,
                fg=fg if status != "⏸️" else _TEXT_TER,
                font=("Arial", 8),
            ).pack(side=tk.LEFT)

            # 品牌页面
            brand_frame = tk.Frame(row, bg=_SURFACE, width=180)
            brand_frame.pack(side=tk.LEFT, padx=(0, 8))
            brand_frame.pack_propagate(False)
            tk.Label(
                brand_frame,
                text=brand_page,
                bg=_SURFACE,
                fg=_TEXT_PRI,
                font=("Arial", 11, "bold"),
                anchor="w",
                justify="left",
            ).pack(anchor=tk.W)
            def _open_articles(_event=None, task_name=task_name):
                if task_name:
                    ArticleWindow(self.root, self.config, task_name=task_name)
                return "break"
            brand_count_label = tk.Label(
                brand_frame,
                text=f"文章总数：{article_total} 篇",
                bg=_SURFACE,
                fg=_ACCENT,
                font=("Arial", 8, "bold"),
                anchor="w",
                cursor="hand2",
            )
            brand_count_label.pack(anchor=tk.W)
            if task_name and task_name != brand_page:
                tk.Label(
                    brand_frame,
                    text=f"品牌组：{task_name}",
                    bg=_SURFACE,
                    fg=_TEXT_SEC,
                    font=("Arial", 8),
                    anchor="w",
                ).pack(anchor=tk.W)
            if tag_summary:
                tk.Label(
                    brand_frame,
                    text=tag_summary,
                    bg=_SURFACE,
                    fg=_TEXT_TER,
                    font=("Arial", 8),
                    anchor="w",
                    justify="left",
                    wraplength=240,
                ).pack(anchor=tk.W, pady=(1, 0))
            brand_count_label.bind("<Button-1>", _open_articles)

            # 关键词数 / 平台总次数
            total_runs = sum(len(kw.get('platforms', [])) for kw in keywords)
            kw_info = f"{len(keywords)}关键词/{total_runs}平台次"
            tk.Label(row, text=kw_info, bg=_SURFACE, fg=_TEXT_SEC, font=("Arial", 9), width=14, anchor="w").pack(side=tk.LEFT, padx=(0, 8))

            article_row_label = tk.Label(row, text=f"{article_total}篇", bg=_SURFACE, fg=_ACCENT,
                                         cursor="hand2", width=8, anchor="w", font=("Arial", 9, "bold"))
            article_row_label.pack(side=tk.LEFT, padx=(0, 8))

            # Webhook
            webhook = task.get('webhook_url', '')
            webhook_short = webhook[:15] + '...' if len(webhook) > 15 else webhook
            tk.Label(row, text=webhook_short, bg=_SURFACE, fg=_TEXT_TER, font=("Arial", 8), width=16, anchor="w").pack(side=tk.LEFT, padx=(0, 8))

            # 运行时间
            weekdays = task.get('weekdays', [])
            if weekdays:
                run_info = describe_task_schedule(task, self.config.get('scheduler', {}))
            else:
                run_info = "未设置"
            tk.Label(row, text=run_info, bg=_SURFACE, fg=_TEXT_SEC, font=("Arial", 8), width=18, anchor="w").pack(side=tk.LEFT, padx=(0, 8))

            # 整行点击编辑
            from functools import partial
            for widget in row.winfo_children():
                widget.bind("<Button-1>", lambda e, idx=i: self.edit_task(idx))
            row.bind("<Button-1>", lambda e, idx=i: self.edit_task(idx))
            article_row_label.bind("<Button-1>", _open_articles)

            # 操作按钮
            btn_wrap = tk.Frame(row, bg=_SURFACE)
            btn_wrap.pack(side=tk.RIGHT)
            tk.Button(
                btn_wrap,
                text="编辑",
                command=partial(self.edit_task, i),
                bg=_SURFACE2,
                fg=_TEXT_PRI,
                relief=tk.FLAT,
                padx=8,
                pady=3,
                cursor="hand2",
                activebackground=_SURFACE2,
                activeforeground=_TEXT_PRI,
            ).pack(side=tk.LEFT, padx=2)
            tk.Button(
                btn_wrap,
                text="删除",
                command=partial(self.delete_task, i),
                bg=_SURFACE2,
                fg=_DANGER,
                relief=tk.FLAT,
                padx=8,
                pady=3,
                cursor="hand2",
                activebackground=_SURFACE2,
                activeforeground=_DANGER,
            ).pack(side=tk.LEFT, padx=2)

        self.tasks_frame.update_idletasks()
        self.canvas.config(scrollregion=self.canvas.bbox("all"))

        # 把滚轮事件绑定到所有子控件
        bind_mousewheel_recursive(self.tasks_frame, self._on_main_mousewheel)

    def add_task(self):
        """添加新任务"""
        dialog = TaskConfigDialog(self.root, existing_tasks=self.config.get('tasks', []))
        self.root.wait_window(dialog.dialog)

        if dialog.result:
            if 'tasks' not in self.config:
                self.config['tasks'] = []
            self.config['tasks'].append(dialog.result)
            self._persist_config(show_feedback=False)
            self._reload_and_refresh(scroll_to_end=True)

    def edit_task(self, index):
        """编辑任务"""
        tasks = self.config.get('tasks', [])
        if index >= len(tasks):
            return

        dialog = TaskConfigDialog(self.root, task=tasks[index], existing_tasks=self.config.get('tasks', []))
        self.root.wait_window(dialog.dialog)

        if dialog.result:
            tasks[index] = dialog.result
            self._persist_config(show_feedback=False)
            self._reload_and_refresh()

    def delete_task(self, index):
        """删除任务"""
        if messagebox.askyesno("确认", "确定要删除这个品牌吗？"):
            tasks = self.config.get('tasks', [])
            if index < len(tasks):
                tasks.pop(index)
                self._persist_config(show_feedback=False)
                self._reload_and_refresh()

    def edit_scheduler(self):
        """编辑全局调度设置。"""
        dialog = SchedulerConfigDialog(self.root, scheduler_config=self.config.get('scheduler', {}))
        self.root.wait_window(dialog.dialog)

        if dialog.result is not None:
            current = dict(self.config.get('scheduler', {}) or {})
            current.update(dialog.result)
            self.config['scheduler'] = current
            self._persist_config(show_feedback=False)
            self._reload_and_refresh()
            messagebox.showinfo("提示", "调度设置已更新，记得点击“保存配置”写入 config.yaml。")

    def open_settings(self):
        """打开统一程序设置。"""
        from ui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(self.root, config_path=str(self.config_path))
        self.root.wait_window(dialog.dialog)
        self._reload_and_refresh()

    def run(self):
        """运行窗口"""
        if self._owns_mainloop:
            self.root.mainloop()
        # Toplevel 模式下不需要 mainloop，由父窗口驱动


def open_config_manager():
    """打开配置管理器（供托盘调用）"""
    app = ConfigManagerWindow("config.yaml")
    app.run()


if __name__ == "__main__":
    open_config_manager()
