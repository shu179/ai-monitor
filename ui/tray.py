"""
系统托盘应用

架构说明（macOS + Windows 统一方案）：
  1. 主线程先创建隐藏的 tk.Tk() 根窗口
  2. 再调用 icon.run_detached()（pystray 在后台线程运行）
  3. 主线程进入 tk mainloop()
  4. pystray 菜单回调通过 _queue 把 build 函数投递给主线程
  5. 主线程 after() 轮询队列，在主线程里创建 Toplevel 弹窗

这样 tkinter 和 pystray 都能正常工作，不存在线程冲突。
"""

import sys
import queue
import tkinter as tk
from tkinter import messagebox, scrolledtext

import pystray
from PIL import Image, ImageDraw
from ui.status_palette import get_percentage_color
from ui.tk_compat import install_global_tk_behaviors
from ui.keyword_guide import KeywordGuideWindow
from ui.article_window import ArticleWindow
from ui.quick_todo_window import QuickTodoWindow
from core.config_watcher import load_config as load_yaml_config, save_config as save_yaml_config
from core.app_paths import resolve_app_path
from core.daily_task_state import ensure_config_task_ids
from core.local_model_manager import get_local_model_manager
from core.runtime_state import should_auto_resume_monitoring, set_auto_resume_monitoring


class TrayApp:
    """系统托盘应用，集成调度器和通知器"""

    def __init__(self, scheduler=None, notifier=None, config=None):
        self.scheduler = scheduler
        self.notifier = notifier
        self.config = config or {}
        self.config_path = resolve_app_path("config.yaml")
        self.recognition_manager = None
        self.icon = None
        self.running = False
        self.last_results = {}  # (task_name, platform, brand) -> rank
        self.status_message = "就绪"
        self.current_mode = "抓取模式"
        self.mode_notice = ""
        self.config_revision = 0
        self.cloud_sync_manager = None
        self._shutdown_started = False

        self._root = None       # 主线程 tk.Tk()，由 run() 初始化
        self._queue = queue.Queue()  # 弹窗任务队列
        self._keyword_guide = None  # 识别模式关键词悬浮窗

    def _apply_scheduler_config(self):
        if not self.scheduler:
            return
        scheduler_cfg = dict(self.config.get("scheduler", {}) or {})
        scheduler_cfg["detection_mode"] = str(self.config.get("detection_mode", "browser") or "browser").strip()
        self.scheduler.config = scheduler_cfg
        if hasattr(self.scheduler, "_refresh_config"):
            self.scheduler._refresh_config()

    def apply_config(self, new_config, reload_runtime=False):
        """应用新配置并更新修订号，供界面同步刷新。"""
        self.config = new_config or {}
        ensure_config_task_ids(self.config)
        get_local_model_manager().sync_config(self.config)
        self.config_revision += 1
        self._apply_scheduler_config()
        if reload_runtime and self.running:
            self.reload_runtime("配置已更新，重新加载监控任务")

    def reload_config(self):
        try:
            new_config = load_yaml_config(self.config_path)
            self.apply_config(new_config)
        except Exception as e:
            print(f"[Tray] 重新加载配置失败: {e}")
        return self.config

    def _post(self, fn):
        """从任意线程投递一个 fn(root) 到主线程执行"""
        self._queue.put(fn)

    def _poll(self):
        """主线程轮询队列，执行弹窗任务"""
        try:
            while True:
                fn = self._queue.get_nowait()
                try:
                    fn(self._root)
                except Exception as e:
                    import traceback
                    print(f"[Tray] 弹窗异常: {e}\n{traceback.format_exc()}")
        except queue.Empty:
            pass
        self._root.after(100, self._poll)

    def _refresh_menu(self):
        if not self.icon:
            return
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def _get_query_tasks(self, tasks):
        global_mode = str(self.config.get('detection_mode', 'browser') or 'browser').strip()
        if global_mode == 'recognition':
            return []
        query_tasks = []
        for task in tasks:
            keywords = task.get('keywords', [])
            if not keywords and task.get('keyword'):
                keywords = [{'keyword': task.get('keyword', ''), 'platforms': [task.get('platform', '')]}]
            if any(
                str((kw or {}).get('keyword') or '').strip()
                and bool((kw or {}).get('platforms'))
                for kw in keywords
            ):
                query_tasks.append(task)
        return query_tasks

    def _build_keyword_items(self):
        if str(self.config.get('detection_mode', 'browser') or 'browser').strip() != 'recognition':
            return []
        items = []
        for task in self.config.get('tasks', []):
            if not task.get('enabled', True):
                continue
            name = task.get('name', '')
            keywords = task.get('keywords', [])
            if not keywords and task.get('keyword'):
                items.append({"task_name": name, "keyword": task['keyword']})
            else:
                for kw in keywords:
                    kw_str = kw.get('keyword', '')
                    if kw_str:
                        items.append({"task_name": name, "keyword": kw_str})
        return items

    def _get_keyword_guide_state(self):
        if self.recognition_manager and hasattr(self.recognition_manager, "get_keyword_guide_state"):
            try:
                return self.recognition_manager.get_keyword_guide_state() or {}
            except Exception as e:
                print(f"[Tray] 读取悬浮窗状态失败: {e}")

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
        self._keyword_guide.set_display_options(
            mode_label="手动确认模式" if state.get("manual_mode") else "OCR 自动识别",
            controls_visible=state.get("controls_visible", state.get("manual_mode")),
        )
        self._keyword_guide.set_action_state(
            text=state.get("action_label"),
            enabled=state.get("action_enabled"),
            detail=state.get("detail_text", ""),
        )

    def _on_recognition_manual_state_change(self, payload=None):
        self._post(lambda root: self._refresh_keyword_guide_ui())

    def _handle_keyword_guide_action(self):
        state = self._get_keyword_guide_state()
        if state.get("manual_mode") and self.recognition_manager:
            try:
                self.recognition_manager.handle_keyword_guide_action()
            except Exception as e:
                print(f"[Tray] 悬浮窗人工确认失败: {e}")
        elif self._keyword_guide:
            self._keyword_guide.advance()
            return
        self._refresh_keyword_guide_ui()

    def _start_recognition_mode(self):
        if self.recognition_manager and self.recognition_manager.has_recognition_tasks():
            self.current_mode = "识别模式"
            self.mode_notice = "仅监听启动后的新剪切板截图"
            self.recognition_manager.start()
            items = self._get_keyword_guide_state().get("items") or []
            if items:
                def _show_guide(root, _items=items):
                    if self._keyword_guide:
                        self._keyword_guide.destroy()
                    self._keyword_guide = KeywordGuideWindow(
                        root,
                        _items,
                        on_action=self._handle_keyword_guide_action,
                        mode_label="手动确认模式" if self._get_keyword_guide_state().get("manual_mode") else "OCR 自动识别",
                        controls_visible=self._get_keyword_guide_state().get("controls_visible", self._get_keyword_guide_state().get("manual_mode")),
                    )
                    self._refresh_keyword_guide_ui()
                self._post(_show_guide)

    def _stop_recognition_mode(self):
        if self.recognition_manager:
            self.recognition_manager.stop()
        if self._keyword_guide:
            def _destroy_guide(root):
                if self._keyword_guide:
                    self._keyword_guide.destroy()
                    self._keyword_guide = None
            self._post(_destroy_guide)

    def _on_recognition_batch_ready(self, batch):
        self._post(lambda root: self._refresh_keyword_guide_ui())

    def _on_recognition_send_complete(self, batch, ok, reason):
        msg = f"{batch['task_name']} 已发送" if ok else f"{batch['task_name']} 发送失败: {reason or '未知错误'}"
        for brand in batch.get('brands', []):
            self.update_result(batch['task_name'], 'recognition', brand, 1 if ok else 99)
        print(f"[Recognition] {msg}")
        callback = getattr(self, "_on_recognition_send_complete_hook", None)
        if callable(callback):
            try:
                callback(batch, ok, reason)
            except Exception as e:
                print(f"[Tray] 识别发送完成回调失败: {e}")
        self._post(lambda root, m=msg: self._show_recognition_result(root, m, ok))

    def _on_recognition_round_complete(self, payload=None):
        callback = getattr(self, "_on_recognition_round_complete_hook", None)
        if callable(callback):
            try:
                callback(payload or {})
            except Exception as e:
                print(f"[Tray] 识别轮次完成回调失败: {e}")

    def _on_recognition_mode_change(self, payload):
        mode = payload.get("mode", "").strip()
        reason = payload.get("reason", "").strip()
        if mode == "recognition":
            self.current_mode = "识别模式"
        elif mode == "capture":
            self.current_mode = "抓取模式"
        if reason:
            self.mode_notice = reason
            self.status_message = self.current_mode
            print(f"[Recognition] 模式切换: {reason}")

    def _on_recognition_manual_switch_required(self, payload):
        message = payload.get("message", "").strip() or "识别模式长时间未收到新截图，请手动切换到抓取模式。"
        self.mode_notice = message
        self.status_message = "请手动切换"
        print(f"[Recognition] {message}")
        self._post(lambda root, m=message: self._show_manual_switch_reminder(root, m))

    def _show_recognition_confirm(self, root, batch):
        if not self.recognition_manager:
            return
        win = tk.Toplevel(root)
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

    def _show_recognition_result(self, root, message, ok):
        win = tk.Toplevel(root)
        win.title("识别模式结果")
        win.geometry("420x150")
        win.attributes('-topmost', True)
        win.lift()
        win.focus_force()
        fg = "#2e7d32" if ok else "#c62828"
        tk.Label(win, text=message, fg=fg, wraplength=360, justify="left").pack(padx=20, pady=28)
        tk.Button(win, text="关闭", width=10, command=win.destroy).pack()

    def _show_manual_switch_reminder(self, root, message):
        win = tk.Toplevel(root)
        win.title("模式切换提醒")
        win.geometry("460x180")
        win.attributes('-topmost', True)
        win.lift()
        win.focus_force()
        tk.Label(win, text="请手动切换模式", font=("Arial", 13, "bold"), fg="#f59e0b").pack(pady=(18, 8))
        tk.Label(win, text=message, wraplength=400, justify="left").pack(padx=20, pady=6)
        tk.Button(win, text="我知道了", width=12, command=win.destroy).pack(pady=14)

    # ------------------------------------------------------------------
    # 图标 & 菜单
    # ------------------------------------------------------------------

    def create_icon(self):
        image = Image.new('RGB', (64, 64), color='#1890ff')
        dc = ImageDraw.Draw(image)
        dc.text((18, 20), "AI", fill='white')
        return image

    def create_menu(self):
        return pystray.Menu(
            pystray.MenuItem(
                lambda text: f"状态: {self.status_message}",
                lambda: None,
                enabled=False
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("快速代办", self.open_quick_todo),
            pystray.MenuItem("打开主界面", self.open_main_window),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("查看监控状态", self.show_status),
            pystray.MenuItem(
                lambda text: "监控状态：已开启" if self.running else "监控状态：已关闭",
                self.toggle_monitoring
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("程序设置", self.open_settings),
            pystray.MenuItem("搜搜", self.open_ai_assistant),
            pystray.MenuItem("文章录入管理", self.open_article_manager),
            pystray.MenuItem("查看日志", self.view_logs),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self.quit)
        )

    # ------------------------------------------------------------------
    # 弹窗：监控状态
    # ------------------------------------------------------------------

    def show_status(self):
        last_results = dict(self.last_results)
        scheduler = self.scheduler
        notifier = self.notifier

        def build(root):
            win = tk.Toplevel(root)
            win.title("品牌监控状态")
            win.geometry("500x400")
            win.resizable(True, True)
            win.attributes('-topmost', True)
            win.lift()
            win.focus_force()

            tk.Label(win, text="各平台品牌提及状态", font=("Arial", 14, "bold")).pack(pady=10)

            text_area = scrolledtext.ScrolledText(win, wrap=tk.WORD, font=("Courier", 11))
            text_area.pack(padx=10, pady=5, fill=tk.BOTH, expand=True)

            if last_results:
                for (task_name, platform, brand), rank in sorted(last_results.items()):
                    if rank == 99:
                        status_str, result_str = "未提及", "—"
                    else:
                        status_str, result_str = "已提及", "✅"
                    line = f"{status_str}  {task_name:15s} | {platform:10s} | {brand:12s} | {result_str}\n"
                    text_area.insert(tk.END, line)
            else:
                text_area.insert(tk.END, "暂无数据，监控未运行或未产生结果\n")

            if scheduler:
                text_area.insert(tk.END, "\n" + "=" * 50 + "\n")
                status = scheduler.get_status()
                text_area.insert(tk.END, f"调度器状态: {'运行中' if status['running'] else '已停止'}\n")
                current_round = status.get('current_round_summary') or {}
                last_completed = status.get('last_completed_summary') or {}
                if current_round.get('mode_label') and current_round.get('total'):
                    text_area.insert(
                        tk.END,
                        f"当前轮次: {current_round.get('mode_label')} "
                        f"{current_round.get('completed', 0)}/{current_round.get('total', 0)} "
                        f"成功{current_round.get('success', 0)} "
                        f"失败{current_round.get('failed', 0)} "
                        f"跳过{current_round.get('skipped', 0)}\n"
                    )
                elif last_completed.get('mode_label'):
                    text_area.insert(
                        tk.END,
                        f"最近完成: {last_completed.get('mode_label')} "
                        f"成功{last_completed.get('success', 0)} "
                        f"失败{last_completed.get('failed', 0)} "
                        f"跳过{last_completed.get('skipped', 0)}\n"
                    )
                for weekday in range(7):
                    label = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][weekday]
                    run_time = status.get('weekly_times', {}).get(str(weekday))
                    text_area.insert(tk.END, f"{label}: {run_time or '不自动运行'}\n")

            if notifier:
                text_area.insert(tk.END, "\n" + "=" * 50 + "\n")
                notify_status = notifier.get_status()
                text_area.insert(tk.END, f"活跃冷却: {len(notify_status['active_cooldowns'])} 个\n")

            text_area.config(state=tk.DISABLED)
            tk.Button(win, text="关闭", command=win.destroy).pack(pady=10)

        self._post(build)

    # ------------------------------------------------------------------
    # 弹窗：趋势图
    # ------------------------------------------------------------------

    def show_trend(self):
        def build(root):
            from core.daily_task_state import derive_task_id
            from core.history import (
                build_trend_rate_items,
                get_current_task_names,
                get_records,
                get_task_brand_names,
                is_manual_test_failure_record,
            )
            from datetime import date, timedelta

            win = tk.Toplevel(root)
            win.title("查询趋势图")
            win.geometry("900x580")
            win.attributes('-topmost', True)
            win.lift()
            win.focus_force()

            task_names = get_current_task_names(self.config)

            if not task_names:
                tk.Label(win, text="当前没有可展示的品牌配置",
                         font=("Arial", 13), fg="gray").pack(pady=40)
                tk.Button(win, text="关闭", command=win.destroy).pack(pady=10)
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

            top = tk.Frame(win)
            top.pack(fill=tk.X, padx=10, pady=8)

            tk.Label(top, text="任务:").pack(side=tk.LEFT)
            selected_task = tk.StringVar(value=task_names[0])
            task_menu = tk.OptionMenu(top, selected_task, *task_names)
            task_menu.pack(side=tk.LEFT, padx=(4, 16))

            tk.Label(top, text="关键词:").pack(side=tk.LEFT)
            selected_kw = tk.StringVar(value="全部")
            kw_menu = tk.OptionMenu(top, selected_kw, "全部")
            kw_menu.pack(side=tk.LEFT, padx=(4, 8))

            tk.Label(top, text="品牌:").pack(side=tk.LEFT)
            selected_brand = tk.StringVar(value="全部")
            brand_menu = tk.OptionMenu(top, selected_brand, "全部")
            brand_menu.pack(side=tk.LEFT, padx=(4, 16))

            tk.Label(top, text="平台:").pack(side=tk.LEFT)
            selected_plat = tk.StringVar(value="全部")
            plat_menu = tk.OptionMenu(top, selected_plat, "全部")
            plat_menu.pack(side=tk.LEFT, padx=(4, 16))

            tk.Label(top, text="范围:").pack(side=tk.LEFT)
            range_var = tk.StringVar(value="月度")
            for label in ("月度", "季度", "年度"):
                tk.Radiobutton(top, text=label, variable=range_var, value=label,
                               command=lambda: win.after(50, redraw)).pack(side=tk.LEFT, padx=3)
            tk.Button(top, text="刷新", command=lambda: win.after(50, redraw)).pack(side=tk.LEFT, padx=8)

            canvas = tk.Canvas(win, bg="white")
            canvas.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

            def _task_history_records(task_name: str) -> list[dict]:
                task = task_map.get(task_name, {})
                task_id = str((task or {}).get("task_id") or derive_task_id(task or {})).strip() if task else ""
                records = get_records(task_name, task_id=task_id)
                current_brands = get_task_brand_names(task)
                if current_brands:
                    records = [r for r in records if str(r.get("brand", "")).strip() in current_brands]
                return [r for r in records if not is_manual_test_failure_record(r)]

            def update_filters(*_):
                task_name = selected_task.get()
                records = _task_history_records(task_name)
                current_brands = get_task_brand_names(task_map.get(task_name, {}))
                kws = ["全部"] + sorted({r.get("keyword", "") for r in records if r.get("keyword")})
                plats = ["全部"] + sorted({r.get("platform", "") for r in records if r.get("platform")})
                brands = ["全部"] + (current_brands or sorted({r.get("brand", "") for r in records if r.get("brand")}))

                kw_menu["menu"].delete(0, "end")
                for kw in kws:
                    kw_menu["menu"].add_command(label=kw,
                        command=lambda v=kw: (selected_kw.set(v), win.after(50, redraw)))
                selected_kw.set("全部")

                brand_menu["menu"].delete(0, "end")
                for b in brands:
                    brand_menu["menu"].add_command(label=b,
                        command=lambda v=b: (selected_brand.set(v), win.after(50, redraw)))
                selected_brand.set("全部")

                plat_menu["menu"].delete(0, "end")
                for p in plats:
                    plat_menu["menu"].add_command(label=p,
                        command=lambda v=p: (selected_plat.set(v), win.after(50, redraw)))
                selected_plat.set("全部")
                win.after(50, redraw)

            def compute_filtered_rates(task_name, kw_filter, brand_filter, plat_filter):
                records = _task_history_records(task_name)
                if kw_filter != "全部":
                    records = [r for r in records if r.get("keyword") == kw_filter]
                if brand_filter != "全部":
                    records = [r for r in records if r.get("brand") == brand_filter]
                if plat_filter != "全部":
                    records = [r for r in records if r.get("platform") == plat_filter]
                return build_trend_rate_items(task_name, records)

            def redraw():
                canvas.delete("all")
                task_name = selected_task.get()
                kw_filter = selected_kw.get()
                brand_filter = selected_brand.get()
                plat_filter = selected_plat.get()
                range_label = range_var.get()
                days = {"月度": 30, "季度": 90, "年度": 365}.get(range_label, 30)

                if kw_filter == "全部" and brand_filter == "全部" and plat_filter == "全部":
                    all_rates = build_trend_rate_items(task_name, _task_history_records(task_name))
                else:
                    all_rates = compute_filtered_rates(task_name, kw_filter, brand_filter, plat_filter)

                W = canvas.winfo_width() or 860
                H = canvas.winfo_height() or 480

                if not all_rates:
                    canvas.create_text(W // 2, H // 2, text="暂无数据，请先运行任务",
                                       font=("Arial", 13), fill="gray")
                    return

                cutoff = (date.today() - timedelta(days=days)).isoformat()
                rates = [r for r in all_rates if r["date"] >= cutoff]
                if not rates:
                    rates = all_rates[-days:]

                pad_l, pad_r, pad_t, pad_b = 52, 20, 20, 48
                n = len(rates)

                def cx(i):
                    return pad_l + (i / max(n - 1, 1)) * (W - pad_l - pad_r)

                def cy(rate):
                    return pad_t + (1 - rate / 100) * (H - pad_t - pad_b)

                for pct, lbl in ((100, "100%"), (80, "80%"), (60, "60%"), (0, "0%")):
                    yy = cy(pct)
                    dash = (4, 4) if pct in (60, 80) else ()
                    col = "#e0e0e0" if pct not in (60, 80) else get_percentage_color(float(pct))
                    canvas.create_line(pad_l, yy, W - pad_r, yy, fill=col, dash=dash)
                    canvas.create_text(pad_l - 4, yy, text=lbl, anchor="e", font=("Arial", 8), fill="#666")

                canvas.create_text(12, pad_t + (H - pad_t - pad_b) // 2,
                                   text="提及率", font=("Arial", 9), fill="#333")

                label_step = max(1, n // {"月度": 6, "季度": 6, "年度": 12}.get(range_label, 6))
                for i, r in enumerate(rates):
                    if i % label_step == 0 or i == n - 1:
                        canvas.create_text(cx(i), H - pad_b + 14,
                                           text=r["date"][5:], font=("Arial", 8), fill="#666")

                for i in range(n):
                    r = rates[i]
                    if r["missing"] or r["rate"] is None:
                        continue
                    if i < n - 1:
                        r2 = rates[i + 1]
                        if not r2["missing"] and r2["rate"] is not None:
                            canvas.create_line(cx(i), cy(r["rate"]), cx(i + 1), cy(r2["rate"]),
                                               fill=get_percentage_color(min(r["rate"], r2["rate"])), width=2)
                    x0, y0 = cx(i), cy(r["rate"])
                    canvas.create_oval(x0 - 4, y0 - 4, x0 + 4, y0 + 4,
                                       fill=get_percentage_color(r["rate"]), outline="white", width=1)

                legend_x = W - 180
                for color, lbl in (
                    (get_percentage_color(90), ">=80%"),
                    (get_percentage_color(70), "60~80%"),
                    (get_percentage_color(30), "<60%"),
                ):
                    canvas.create_oval(legend_x, 10, legend_x + 10, 20, fill=color, outline="")
                    canvas.create_text(legend_x + 14, 15, text=lbl, anchor="w", font=("Arial", 8), fill="#333")
                    legend_x += 58

                valid = [r for r in rates if not r["missing"] and r["rate"] is not None]
                if valid:
                    avg = round(sum(r["rate"] for r in valid) / len(valid), 1)
                    canvas.create_text(pad_l, H - 6,
                                       text=f"共 {n} 天  有效 {len(valid)} 天  平均提及率 {avg}%",
                                       anchor="w", font=("Arial", 9), fill="#888")

            selected_task.trace_add("write", update_filters)
            win.after(120, update_filters)

        self._post(build)

    # ------------------------------------------------------------------
    # 弹窗：日志
    # ------------------------------------------------------------------

    def view_logs(self):
        def build(root):
            log_path = resolve_app_path("logs/monitor.log")
            win = tk.Toplevel(root)
            win.title("运行日志")
            win.geometry("700x500")
            win.attributes('-topmost', True)
            win.lift()
            win.focus_force()

            text_area = scrolledtext.ScrolledText(win, wrap=tk.WORD)
            text_area.pack(padx=10, pady=10, fill=tk.BOTH, expand=True)

            if log_path.exists():
                try:
                    with open(log_path, 'r', encoding='utf-8') as f:
                        text_area.insert(tk.END, f.read())
                    text_area.see(tk.END)
                except Exception as e:
                    text_area.insert(tk.END, f"读取日志失败: {e}")
            else:
                text_area.insert(tk.END, "日志文件不存在\n")

            text_area.config(state=tk.DISABLED)
            tk.Button(win, text="关闭", command=win.destroy).pack(pady=10)

        self._post(build)

    def _on_task_timeout(self, task, elapsed_seconds):
        task_name = task.get('name', '')
        keywords = task.get('keywords', [])
        kw_info = ', '.join(
            f"{kw.get('keyword','')}/{kw.get('brand','')}"
            for kw in keywords[:3]
        )
        msg = f"任务组「{task_name}」已运行 {int(elapsed_seconds//60)} 分钟仍未结束\n关键词: {kw_info}"
        print(f"[Tray] ⚠️ 超时警告: {msg}")
        self._post(lambda root, m=msg, tn=task_name: self._show_timeout_warning(root, m, tn))

    def _show_timeout_warning(self, root, msg, task_name):
        import tkinter as tk
        win = tk.Toplevel(root)
        win.title("⚠️ 任务超时警告")
        win.geometry("420x180")
        win.attributes('-topmost', True)
        win.lift()
        win.focus_force()
        tk.Label(win, text="⚠️ 任务运行超时", font=("Arial", 13, "bold"), fg="#f5222d").pack(pady=(16, 6))
        tk.Label(win, text=msg, font=("Arial", 10), wraplength=380, justify="left").pack(padx=20)
        tk.Button(win, text="关闭", command=win.destroy, width=10).pack(pady=14)

    # ------------------------------------------------------------------
    # 弹窗：配置管理
    # ------------------------------------------------------------------

    def open_main_window(self):
        last_results = dict(self.last_results)
        scheduler = self.scheduler
        notifier = self.notifier
        config = self.config

        def build_toplevel(root):
            import importlib
            import tkinter as tk
            import ui.main_window as main_window_module
            import ui.tk_compat as tk_compat_module

            tk_compat_module = importlib.reload(tk_compat_module)
            main_window_module = importlib.reload(main_window_module)
            MainWindow = main_window_module.MainWindow

            # 直接在托盘的 root 下开一个 Toplevel 版主界面
            win = tk.Toplevel(root)
            win.title("Surfaced")
            win.geometry("1000x680")
            win.minsize(800, 560)
            win.lift()
            win.focus_force()

            # 复用 MainWindow 逻辑但挂在 Toplevel 上
            app = MainWindow.__new__(MainWindow)
            app.scheduler = scheduler
            app.notifier = notifier
            app.config = config
            app.config_path = resolve_app_path("config.yaml")
            app.last_results = last_results
            app.running = self.running
            app.current_mode = self.current_mode
            app.mode_notice = self.mode_notice
            app.config_revision = self.config_revision
            app._home_copy_messages = []
            app._home_copy_cursor = 0
            app._timeout_warnings = {}
            app._keyword_guide = None
            app._mode_cards = {}
            app._mode_card_labels = {}
            app._message_action_phase = "idle"
            app._region_distribution_redraw = None
            app._region_distribution_mode = "domestic"
            app.execute_task_callback = self._execute_task
            app.recognition_manager = self.recognition_manager
            app.on_config_change = self.apply_config
            app.root = win
            app._trend_built = False
            app._should_restore_monitoring_on_launch = False
            if hasattr(app, "_init_ui_resources"):
                app._init_ui_resources()
            if hasattr(root, "_ai_monitor_tk_fonts_installed"):
                delattr(root, "_ai_monitor_tk_fonts_installed")
            app._build_ui()
            app._refresh_tasks()
            app._refresh_status()

            def sync_window_state():
                if not win.winfo_exists():
                    return
                app.running = self.running
                if hasattr(app, "_apply_monitoring_button_state"):
                    app._apply_monitoring_button_state(self.running)
                if self.running:
                    app._status_pill.config(text="状态: 运行中", fg="#10B981")
                else:
                    app._status_pill.config(text="状态: 已停止", fg="#7A7A9A")
                app.current_mode = self.current_mode
                app.mode_notice = self.mode_notice
                if app.config_revision != self.config_revision:
                    app.config = self.config
                    app.config_revision = self.config_revision
                    app._refresh_tasks()
                    app._refresh_status()
                app._refresh_mode_notice()
                win.after(1000, sync_window_state)

            def toggle_from_window():
                self.toggle_monitoring()
                win.after(120, sync_window_state)

            app._toggle_btn.configure(command=toggle_from_window)
            sync_window_state()

        self._post(build_toplevel)

    def open_quick_todo(self):
        def build(root):
            QuickTodoWindow(
                root,
                get_config=lambda: self.config,
                save_config=self._save_quick_todo_config,
            )

        self._post(build)

    def _save_quick_todo_config(self, new_config: dict):
        save_yaml_config(new_config, self.config_path)
        self.apply_config(new_config, reload_runtime=False)

    def open_config(self):
        last_results = dict(self.last_results)

        def build(root):
            from ui.config_manager import ConfigManagerWindow
            app = ConfigManagerWindow(
                "config.yaml",
                results=last_results,
                parent=root,
                on_config_change=lambda new_config: self.apply_config(new_config, reload_runtime=True),
            )
            app.run()

        self._post(build)

    def open_settings(self):
        def build(root):
            from ui.settings_dialog import SettingsDialog
            SettingsDialog(
                root,
                config_path="config.yaml",
                on_config_change=lambda new_config: self.apply_config(new_config, reload_runtime=True),
            )

        self._post(build)

    def open_ai_assistant(self):
        def build(root):
            from ui.ai_dialog import AIAssistantDialog

            def on_config_change(new_config):
                self.apply_config(new_config, reload_runtime=True)

            def get_runtime_state():
                scheduler_info = None
                if self.scheduler:
                    scheduler_info = {
                        "weekly_times": dict(getattr(self.scheduler, "weekly_times", {}) or {}),
                    }
                return {
                    "running": self.running,
                    "status_message": self.status_message,
                    "scheduler": scheduler_info,
                    "last_results": dict(self.last_results),
                }

            def _save_config_and_apply(cfg):
                save_yaml_config(cfg, self.config_path)
                self.apply_config(cfg, reload_runtime=self.running)

            def on_program_action(action, params=None):
                params = params or {}

                # ── 监控控制 ──────────────────────────────────────
                if action == "start_monitoring":
                    if self.running:
                        return "监控已经在运行中。"
                    self.start_monitoring()
                    return "已启动监控。"

                elif action == "stop_monitoring":
                    if not self.running:
                        return "监控当前未运行。"
                    self.stop_monitoring()
                    return "已停止监控。"

                # ── 任务启用/禁用/删除 ──────────────────────────
                elif action in ("enable_task", "disable_task", "delete_task"):
                    task_name = params.get("task_name", "").strip()
                    if not task_name:
                        return "请提供要操作的任务名称。"
                    tasks = self.config.get("tasks", [])
                    idx = next((i for i, t in enumerate(tasks) if t.get("name") == task_name), None)
                    if idx is None:
                        names = "、".join(t.get("name", "") for t in tasks) or "无"
                        return f"未找到任务「{task_name}」，现有任务：{names}"
                    if action == "enable_task":
                        tasks[idx]["enabled"] = True
                        _save_config_and_apply(self.config)
                        return f"已启用任务「{task_name}」。"
                    elif action == "disable_task":
                        tasks[idx]["enabled"] = False
                        _save_config_and_apply(self.config)
                        return f"已禁用任务「{task_name}」。"
                    elif action == "delete_task":
                        confirmed = messagebox.askyesno(
                            "确认删除任务",
                            f"确定删除任务「{task_name}」吗？此操作不可撤销。",
                            parent=root,
                        )
                        if not confirmed:
                            return f"已取消删除任务「{task_name}」。"
                        tasks.pop(idx)
                        _save_config_and_apply(self.config)
                        return f"已删除任务「{task_name}」。"

                # ── 调度器设置 ──────────────────────────────────
                elif action == "set_scheduler":
                    changed = []
                    sched_cfg = self.config.setdefault("scheduler", {})
                    if "weekly_times" in params and isinstance(params["weekly_times"], dict):
                        sched_cfg["weekly_times"] = dict(params["weekly_times"])
                        changed.append("每周时间表已更新")
                    if not changed:
                        return "没有可修改的调度器参数。"
                    _save_config_and_apply(self.config)
                    return f"调度器已更新：{'，'.join(changed)}。"

                # ── 任务关键词/平台修改 ────────────────────────
                elif action == "update_task_keyword":
                    task_name = params.get("task_name", "").strip()
                    if not task_name:
                        return "请提供任务名称。"
                    tasks = self.config.get("tasks", [])
                    idx = next((i for i, t in enumerate(tasks) if t.get("name") == task_name), None)
                    if idx is None:
                        return f"未找到任务「{task_name}」。"
                    kw_index = params.get("keyword_index", 0)
                    keywords = tasks[idx].get("keywords", [])
                    if kw_index >= len(keywords):
                        return f"关键词索引 {kw_index} 超出范围，该任务有 {len(keywords)} 个关键词组。"
                    updates = params.get("fields", params)
                    for field in ("keyword", "brand", "platforms", "mode"):
                        if field in updates:
                            keywords[kw_index][field] = updates[field]
                    _save_config_and_apply(self.config)
                    return f"已更新任务「{task_name}」第 {kw_index+1} 个关键词组。"

                return f"未知操作：{action}"

            AIAssistantDialog(
                root,
                config=self.config,
                config_path="config.yaml",
                on_config_change=on_config_change,
                get_runtime_state=get_runtime_state,
                on_program_action=on_program_action,
            )

        self._post(build)

    # ------------------------------------------------------------------
    # 文章录入管理
    # ------------------------------------------------------------------

    def open_article_manager(self):
        def build(root):
            ArticleWindow(root, config=self.config)
        self._post(build)

    # ------------------------------------------------------------------
    # 监控控制
    # ------------------------------------------------------------------

    def toggle_monitoring(self):
        if self.running:
            self.stop_monitoring()
        else:
            self.start_monitoring()

    def start_monitoring(self):
        if not self.scheduler:
            print("[Tray] 调度器未初始化")
            return

        self.reload_config()
        self.running = True
        self.status_message = "运行中"

        tasks = self.config.get('tasks', [])
        enabled_tasks = [t for t in tasks if t.get('enabled', True)]
        query_tasks = self._get_query_tasks(enabled_tasks)
        recognition_configured = str(self.config.get('detection_mode', 'browser') or 'browser').strip() == 'recognition'
        has_recognition = bool(self.recognition_manager and self.recognition_manager.has_recognition_tasks())
        if not query_tasks and not has_recognition:
            if recognition_configured:
                print("[Tray] 当前处于识别模式，但没有可监听的关键词或任务")
                self.running = False
                self.status_message = "无识别任务"
                return
            print("[Tray] 当前没有可运行的监控任务")
            self.running = False
            self.status_message = "无可运行任务"
            return

        def on_status_change(status, message):
            self.status_message = message[:30]
            print(f"[Tray] {status}: {message}")
            callback = getattr(self, "_on_scheduler_status_event", None)
            if callable(callback):
                try:
                    callback(status, message)
                except Exception as e:
                    print(f"[Tray] 调度状态回调失败: {e}")

        if query_tasks:
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
            self.status_message = "等待定时轮次"
        else:
            self.current_mode = "抓取模式"
            self.mode_notice = "当前未启用识别监听，可手动执行抓取流程"
            self.status_message = self.current_mode
        set_auto_resume_monitoring(True)
        self._refresh_menu()
        print("[Tray] 监控已启动")

    def stop_monitoring(self, *, persist_preference=True):
        if self.scheduler:
            self.scheduler.stop()
        self._stop_recognition_mode()
        self.running = False
        self.status_message = "已停止"
        self.current_mode = "抓取模式"
        if persist_preference:
            set_auto_resume_monitoring(False)
        self._refresh_menu()
        print("[Tray] 监控已停止")

    def _execute_task(self, task: dict):
        print(f"[Tray] 执行任务: {task.get('name', task.get('platform'))}")
        return {}

    def reload_runtime(self, reason="配置已更新，重新加载监控任务"):
        """重载运行态，确保最新配置进入调度。"""
        was_running = self.running
        if was_running:
            print(f"[Tray] {reason}")
            if self.scheduler:
                self.scheduler.stop()
            self._stop_recognition_mode()
            self.running = False

        self.reload_config()

        if was_running:
            self.start_monitoring()

    def update_result(self, task_name: str, platform: str, brand: str, rank: int):
        self.last_results[(task_name, platform, brand)] = rank
        if rank == 99:
            self.status_message = f"{task_name} 未提及"

    # ------------------------------------------------------------------
    # 退出
    # ------------------------------------------------------------------

    def shutdown(self):
        if self._shutdown_started:
            return
        self._shutdown_started = True
        print("[Tray] 正在清理运行资源...")
        try:
            get_local_model_manager().shutdown_owned_process()
        except Exception:
            pass
        if self.cloud_sync_manager:
            try:
                self.cloud_sync_manager.stop()
            except Exception:
                pass
        if self.running:
            self.stop_monitoring(persist_preference=False)
        else:
            try:
                if self.scheduler:
                    self.scheduler.stop()
            except Exception:
                pass
            try:
                self._stop_recognition_mode()
            except Exception:
                pass
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
        if self._root:
            try:
                self._root.quit()
            except Exception:
                pass

    def quit(self):
        print("[Tray] 正在退出...")
        self.shutdown()
        sys.exit(0)

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------

    def run(self):
        # 1. 先初始化 tkinter（必须在 pystray 之前，避免 macOS NSApplication 冲突）
        self._root = tk.Tk()
        self._root.withdraw()
        install_global_tk_behaviors(self._root)

        # 2. 创建托盘图标并用 run_detached() 在后台线程启动
        self.icon = pystray.Icon(
            "ai_brand_monitor",
            self.create_icon(),
            "Surfaced",
            self.create_menu()
        )
        self.icon.run_detached()
        print("[Tray] 系统托盘已启动")

        # 3. 启动队列轮询
        self._root.after(100, self._poll)
        if self.cloud_sync_manager:
            try:
                self.cloud_sync_manager.start()
            except Exception as e:
                print(f"[Tray] 自动同步启动失败: {e}")
        if should_auto_resume_monitoring():
            self.status_message = "正在恢复监控"
            self._refresh_menu()
            self._root.after(250, self.start_monitoring)

        # 4. 主线程进入 tkinter mainloop
        self._root.mainloop()


if __name__ == "__main__":
    app = TrayApp()
    app.run()
