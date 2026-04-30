"""独立的快速代办窗口。"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox

from core.quick_todos import normalize_quick_todos

_BG = "#F0F0F0"
_SURFACE = "#FFFFFF"
_BORDER = "#D0D0D0"
_TEXT_PRI = "#202020"
_TEXT_SEC = "#4F4F4F"
_TEXT_TER = "#6F6F6F"
_SUCCESS = "#1F7A1F"
_FONT = "TkDefaultFont"


class QuickTodoWindow:
    """支持新增和勾选完成的独立代办窗口。"""

    def __init__(self, parent, *, get_config, save_config):
        self._get_config = get_config
        self._save_config = save_config

        self.window = tk.Toplevel(parent)
        self.window.title("快速代办")
        self.window.geometry("460x560")
        self.window.minsize(360, 420)
        self.window.configure(bg=_BG)
        self.window.lift()
        self.window.focus_force()

        base_font = tkfont.nametofont(_FONT)
        self._todo_font = tkfont.Font(self.window, font=base_font)
        self._todo_done_font = tkfont.Font(self.window, font=base_font)
        self._todo_done_font.configure(overstrike=1)

        self._todo_var = tk.StringVar()
        self._build_ui()
        self._refresh_list()

    def _build_ui(self):
        outer = tk.Frame(self.window, bg=_BG)
        outer.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        tk.Label(
            outer,
            text="快速代办",
            bg=_BG,
            fg=_TEXT_PRI,
            font=(_FONT, 15, "bold"),
            anchor="w",
        ).pack(anchor=tk.W)

        tk.Label(
            outer,
            text="输入任务后按回车添加，点击左侧方框即可标记完成。",
            bg=_BG,
            fg=_TEXT_TER,
            font=(_FONT, 9),
            anchor="w",
        ).pack(anchor=tk.W, pady=(4, 12))

        entry = ttk.Entry(outer, textvariable=self._todo_var)
        entry.pack(fill=tk.X)
        entry.bind("<Return>", self._on_submit)
        entry.focus_set()
        self._entry = entry

        card = tk.Frame(outer, bg=_SURFACE, highlightbackground=_BORDER, highlightthickness=1)
        card.pack(fill=tk.BOTH, expand=True, pady=(14, 0))

        self._list_wrap = tk.Frame(card, bg=_SURFACE)
        self._list_wrap.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)

    def _get_items(self, *, persist_if_changed: bool = False) -> list[dict]:
        config = dict(self._get_config() or {})
        normalized, changed = normalize_quick_todos(config.get("quick_todos", []))
        if changed or config.get("quick_todos") != normalized:
            config["quick_todos"] = normalized
            if persist_if_changed:
                self._save_items(normalized)
        return normalized

    def _save_items(self, items: list[dict]):
        config = dict(self._get_config() or {})
        normalized, _ = normalize_quick_todos(items)
        config["quick_todos"] = normalized
        try:
            self._save_config(config)
        except Exception as e:
            messagebox.showerror("错误", f"保存代办失败: {e}", parent=self.window)
            raise

    def _refresh_list(self):
        for widget in self._list_wrap.winfo_children():
            widget.destroy()

        items = self._get_items(persist_if_changed=True)
        if not items:
            tk.Label(
                self._list_wrap,
                text="还没有待办，先记一件最想完成的事。",
                bg=_SURFACE,
                fg=_TEXT_TER,
                font=(_FONT, 10),
                anchor="w",
            ).pack(anchor=tk.W, pady=(4, 0))
            return

        for index, item in enumerate(items):
            done = bool(item.get("done"))
            row = tk.Frame(self._list_wrap, bg=_SURFACE)
            row.pack(fill=tk.X, pady=4)

            box = tk.Canvas(
                row,
                width=18,
                height=18,
                bg=_SURFACE,
                highlightthickness=0,
                cursor="hand2",
            )
            box.pack(side=tk.LEFT, padx=(0, 10), pady=1)
            box.create_rectangle(
                2,
                2,
                16,
                16,
                outline="#9CA3AF" if done else _TEXT_SEC,
                fill="#DDE5DB" if done else _SURFACE,
                width=1,
            )
            if done:
                box.create_line(5, 9, 8, 12, 13, 6, fill=_SUCCESS, width=2)

            label = tk.Label(
                row,
                text=item["text"],
                bg=_SURFACE,
                fg="#8A8A8A" if done else _TEXT_PRI,
                font=self._todo_done_font if done else self._todo_font,
                anchor="w",
                justify="left",
                wraplength=360,
                cursor="hand2",
            )
            label.pack(side=tk.LEFT, fill=tk.X, expand=True)

            box.bind("<Button-1>", lambda e, idx=index: self._toggle_item(idx))
            label.bind("<Button-1>", lambda e, idx=index: self._toggle_item(idx))
            for widget in (row, box, label):
                widget.bind("<Button-3>", lambda e, idx=index: self._show_context_menu(e, idx))
                widget.bind("<Control-Button-1>", lambda e, idx=index: self._show_context_menu(e, idx))

    def _on_submit(self, event=None):
        text = self._todo_var.get().strip()
        if not text:
            return "break"
        items = self._get_items()
        items.insert(0, {"text": text, "done": False})
        self._save_items(items)
        self._todo_var.set("")
        self._refresh_list()
        self._entry.focus_set()
        return "break"

    def _toggle_item(self, index: int):
        items = self._get_items()
        if not (0 <= index < len(items)):
            return
        is_done = not bool(items[index].get("done"))
        items[index]["done"] = is_done
        if is_done:
            items[index]["completed_at"] = ""
        else:
            items[index].pop("completed_at", None)
        self._save_items(items)
        self._refresh_list()

    def _show_context_menu(self, event, index: int):
        items = self._get_items()
        if not (0 <= index < len(items)):
            return
        menu = tk.Menu(self.window, tearoff=0)
        menu.add_command(label="清除", command=lambda idx=index: self._remove_item(idx))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _remove_item(self, index: int):
        items = self._get_items()
        if not (0 <= index < len(items)):
            return
        items.pop(index)
        self._save_items(items)
        self._refresh_list()
