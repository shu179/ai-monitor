"""识别模式关键词悬浮窗"""
from pathlib import Path
import subprocess
import sys
import tkinter as tk

_PBCOPY = "/usr/bin/pbcopy"
_WINDOWS_CLIP = "clip"
_WINDOWS_POWERSHELL = "powershell"

_PLATFORM_DISPLAY = {
    "doubao": "豆包",
    "deepseek": "DeepSeek",
    "ark_deepseek": "方舟 DeepSeek",
    "kimi": "Kimi",
    "yuanbao": "元宝",
    "tongyi": "通义千问",
    "wenxin": "文心一言",
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "gemini": "Gemini",
    "perplexity": "Perplexity",
}


class KeywordGuideWindow:
    """始终置顶的关键词导航悬浮窗。

    items: [(task_name, keyword_str), ...]
    """

    def __init__(self, root, items: list, on_action=None, mode_label: str = "", controls_visible: bool = True):
        self._items = list(items or [])
        self._idx = 0
        self._on_action = on_action
        self._action_label_override = None
        self._action_enabled_override = None
        self._detail_text = ""
        self._mode_label = mode_label or ""
        self._controls_visible = bool(controls_visible)
        self._keyword_var = tk.StringVar(value="")
        # 上一次因切换关键词而自动复制的关键词，防止重复自动复制
        self._last_auto_copied_kw = ""
        # 复制反馈定时器 id
        self._feedback_after_id = None

        self._win = tk.Toplevel(root)
        self._win.title("关键词导航")
        self._win.geometry("320x210")
        self._win.resizable(False, False)
        self._win.attributes("-topmost", True)

        self._build_ui()
        try:
            self._win.lift()
            self._win.focus_force()
        except Exception:
            pass

        self._refresh()
        self._schedule_auto_copy()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        top = tk.Frame(self._win, padx=12, pady=8)
        top.pack(fill=tk.X)

        self._mode_lbl = tk.Label(
            top, text="", fg="#0A64A4", font=("Arial", 9, "bold"), anchor="w"
        )
        self._mode_lbl.pack(fill=tk.X)

        self._task_lbl = tk.Label(
            top, text="", fg="#888", font=("Arial", 10), anchor="w"
        )
        self._task_lbl.pack(fill=tk.X)

        self._platform_lbl = tk.Label(
            top, text="", fg="#0F766E", font=("Arial", 9), anchor="w"
        )
        self._platform_lbl.pack(fill=tk.X, pady=(2, 0))

        kw_frame = tk.Frame(self._win, padx=12)
        kw_frame.pack(fill=tk.X)

        self._kw_entry = tk.Entry(
            kw_frame,
            textvariable=self._keyword_var,
            font=("Arial", 16, "bold"),
            relief=tk.FLAT,
            readonlybackground="#FFFFFF",
            bd=0,
            state="readonly",
            justify="left",
        )
        self._kw_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        # 单击关键词文字区域直接复制
        self._kw_entry.bind("<Button-1>", self._handle_copy_click, add="+")
        self._kw_entry.bind("<Double-Button-1>", self._handle_copy_click, add="+")

        # 复制按钮：用 tk.Canvas 绘制，macOS 上事件最可靠
        self._copy_btn = tk.Canvas(
            kw_frame,
            width=52,
            height=28,
            bg="#F3F4F6",
            highlightthickness=1,
            highlightbackground="#D1D5DB",
            cursor="hand2",
        )
        self._copy_btn.pack(side=tk.RIGHT)
        self._copy_btn_text_id = self._copy_btn.create_text(
            26, 14, text="复制", font=("Arial", 10), fill="#111827"
        )
        self._copy_btn.bind("<Button-1>", self._handle_copy_click, add="+")
        self._copy_btn.bind("<ButtonRelease-1>", self._handle_copy_click, add="+")

        self._progress_lbl = tk.Label(
            self._win, text="", fg="#999", font=("Arial", 9)
        )
        self._progress_lbl.pack(pady=(4, 0))

        self._btn_frame = tk.Frame(self._win, pady=8)
        if self._controls_visible:
            self._btn_frame.pack()

        self._prev_btn = tk.Button(
            self._btn_frame, text="← 上一个", width=10, command=self.go_back
        )
        self._prev_btn.pack(side=tk.LEFT, padx=6)

        self._next_btn = tk.Button(
            self._btn_frame, text="下一个 →", width=12, command=self._handle_action
        )
        self._next_btn.pack(side=tk.LEFT, padx=6)

        self._detail_lbl = tk.Label(
            self._win, text="", fg="#666", font=("Arial", 9),
            wraplength=280, justify="left"
        )
        self._detail_lbl.pack(pady=(0, 8))

        # 键盘快捷键
        self._win.bind("<Command-c>", self._handle_copy_click, add="+")
        self._win.bind("<Control-c>", self._handle_copy_click, add="+")

    def _item_values(self, item):
        if isinstance(item, dict):
            platforms = item.get("platforms") or []
            platform_text = "、".join(
                _PLATFORM_DISPLAY.get(str(platform).strip(), str(platform).strip())
                for platform in platforms
                if str(platform).strip()
            )
            return item.get("task_name", ""), item.get("keyword", ""), platform_text
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            platform_text = item[2] if len(item) >= 3 else ""
            return item[0], item[1], platform_text
        return "", "", ""

    def _refresh(self):
        """只刷新 UI 显示，不触发任何复制副作用。"""
        if not self._items:
            self._mode_lbl.config(text=self._mode_label)
            self._task_lbl.config(text="（无关键词）")
            self._platform_lbl.config(text="")
            self._keyword_var.set("")
            self._progress_lbl.config(text="")
            self._detail_lbl.config(text="")
            self._prev_btn.config(state=tk.DISABLED)
            self._next_btn.config(state=tk.DISABLED)
            self._set_copy_btn_text("复制")
            return

        task_name, kw, platform_text = self._item_values(self._items[self._idx])
        total = len(self._items)

        self._mode_lbl.config(text=self._mode_label)
        self._task_lbl.config(text=task_name)
        self._platform_lbl.config(text=f"待补平台：{platform_text}" if platform_text else "")
        self._keyword_var.set(kw)
        self._progress_lbl.config(text=f"第 {self._idx + 1} / {total} 个关键词")
        self._detail_lbl.config(text=self._detail_text)

        if self._controls_visible:
            if not self._btn_frame.winfo_manager():
                self._btn_frame.pack()
            self._prev_btn.config(state=tk.NORMAL if self._idx > 0 else tk.DISABLED)
            btn_text = self._action_label_override
            btn_state = self._action_enabled_override
            if btn_text is None:
                btn_text = "下一个 →" if self._idx < total - 1 else "（已完成）"
            if btn_state is None:
                btn_state = tk.NORMAL if self._idx < total - 1 else tk.DISABLED
            self._next_btn.config(state=btn_state, text=btn_text)
        else:
            if self._btn_frame.winfo_manager():
                self._btn_frame.pack_forget()

    # ------------------------------------------------------------------
    # 自动复制（仅在关键词切换时触发一次）
    # ------------------------------------------------------------------

    def _schedule_auto_copy(self):
        """关键词切换后调度一次自动复制。"""
        if not self._items:
            return
        _, kw, _ = self._item_values(self._items[self._idx])
        if not kw or kw == self._last_auto_copied_kw:
            return
        self._win.after(80, lambda k=kw: self._do_auto_copy(k))

    def _do_auto_copy(self, kw: str):
        """执行自动复制，校验当前关键词仍匹配。"""
        if not self._items:
            return
        _, current_kw, _ = self._item_values(self._items[self._idx])
        if current_kw != kw or kw == self._last_auto_copied_kw:
            return
        print(f"[KeywordGuide] 自动复制关键词: {kw}")
        if self._copy_to_system_clipboard(kw):
            self._last_auto_copied_kw = kw
            self._show_copy_feedback("已复制")

    # ------------------------------------------------------------------
    # 复制核心
    # ------------------------------------------------------------------

    def _copy_to_system_clipboard(self, text: str) -> bool:
        try:
            if sys.platform == "darwin":
                if Path(_PBCOPY).exists():
                    subprocess.run([_PBCOPY], input=text, text=True, check=True)
                    print("[KeywordGuide] 已写入 macOS 系统剪贴板")
                    return True
                return False

            if sys.platform.startswith("win"):
                try:
                    subprocess.run(
                        _WINDOWS_CLIP,
                        input=text,
                        text=True,
                        check=True,
                        shell=True,
                    )
                    print("[KeywordGuide] 已写入 Windows clip 剪贴板")
                    return True
                except Exception:
                    subprocess.run(
                        [_WINDOWS_POWERSHELL, "-NoProfile", "-Command", "Set-Clipboard"],
                        input=text,
                        text=True,
                        check=True,
                    )
                    print("[KeywordGuide] 已写入 Windows PowerShell 剪贴板")
                    return True
        except Exception:
            print("[KeywordGuide] 写入系统剪贴板失败")
            return False
        return False

    def _copy_keyword(self):
        if not self._items:
            return
        _, kw = self._item_values(self._items[self._idx])
        print(f"[KeywordGuide] 手动复制关键词: {kw}")
        ok = self._copy_to_system_clipboard(kw)
        if ok:
            self._last_auto_copied_kw = kw
            self._show_copy_feedback("已复制 ✓")
        else:
            self._show_copy_feedback("复制失败")

    def _set_copy_btn_text(self, text: str):
        try:
            self._copy_btn.itemconfig(self._copy_btn_text_id, text=text)
        except Exception:
            pass

    def _show_copy_feedback(self, label: str):
        if self._feedback_after_id is not None:
            try:
                self._win.after_cancel(self._feedback_after_id)
            except Exception:
                pass
        self._set_copy_btn_text(label)
        self._feedback_after_id = self._win.after(1200, self._reset_copy_button)

    def _reset_copy_button(self):
        self._feedback_after_id = None
        self._set_copy_btn_text("复制")

    def _handle_copy_click(self, _event=None):
        print("[KeywordGuide] 收到复制点击事件")
        self._copy_keyword()
        return "break"

    def _handle_action(self):
        if callable(self._on_action):
            self._on_action()
            return
        self.advance()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_index(self):
        return self._idx

    def set_items(self, items):
        self._items = list(items or [])
        if self._items:
            self._idx = min(self._idx, len(self._items) - 1)
        else:
            self._idx = 0
        self._refresh()

    def set_index(self, index: int):
        if not self._items:
            self._idx = 0
        else:
            self._idx = max(0, min(int(index), len(self._items) - 1))
        self._refresh()

    def set_action_state(self, text=None, enabled=None, detail=None):
        self._action_label_override = text
        self._action_enabled_override = enabled
        self._detail_text = detail or ""
        self._refresh()

    def set_display_options(self, mode_label=None, controls_visible=None):
        if mode_label is not None:
            self._mode_label = mode_label
        if controls_visible is not None:
            self._controls_visible = bool(controls_visible)
        self._refresh()

    def advance(self):
        """切换到下一个关键词。"""
        if not self._items:
            return
        if self._idx < len(self._items) - 1:
            self._idx += 1
        self._refresh()
        self._schedule_auto_copy()

    def go_back(self):
        """切换到上一个关键词。"""
        if self._idx > 0:
            self._idx -= 1
        self._refresh()
        self._schedule_auto_copy()

    def destroy(self):
        try:
            self._win.destroy()
        except Exception:
            pass
