import sys
import tkinter as tk


def install_global_tk_behaviors(widget):
    root = widget._root()
    if getattr(root, "_ai_monitor_tk_compat_installed", False):
        return

    root._ai_monitor_tk_compat_installed = True

    bindings = {
        "<<CopyCompat>>": _copy_selection,
        "<<CutCompat>>": _cut_selection,
        "<<PasteCompat>>": _paste_clipboard,
        "<<SelectAllCompat>>": _select_all,
    }
    sequences = {
        "<<CopyCompat>>": (
            "<Command-c>", "<Command-C>", "<Control-c>", "<Control-C>",
            "<Command-KeyPress-c>", "<Control-KeyPress-c>", "<Control-Insert>",
        ),
        "<<CutCompat>>": (
            "<Command-x>", "<Command-X>", "<Control-x>", "<Control-X>",
            "<Command-KeyPress-x>", "<Control-KeyPress-x>", "<Shift-Delete>",
        ),
        "<<PasteCompat>>": (
            "<Command-v>", "<Command-V>", "<Control-v>", "<Control-V>",
            "<Command-KeyPress-v>", "<Control-KeyPress-v>", "<Shift-Insert>",
        ),
        "<<SelectAllCompat>>": (
            "<Command-a>", "<Command-A>", "<Control-a>", "<Control-A>",
            "<Command-KeyPress-a>", "<Control-KeyPress-a>",
        ),
    }

    for virtual_name, handler in bindings.items():
        root.bind_all(virtual_name, handler, add="+")
        for seq in sequences[virtual_name]:
            root.bind_all(seq, lambda e, v=virtual_name: e.widget.event_generate(v), add="+")


def _select_all(event):
    widget = _get_editable_widget(event.widget)
    if widget is None:
        return None
    try:
        if isinstance(widget, tk.Text):
            widget.tag_add("sel", "1.0", "end-1c")
            widget.mark_set("insert", "1.0")
            widget.see("insert")
            return "break"
    except tk.TclError:
        return None

    try:
        widget.selection_range(0, tk.END)
        widget.icursor(tk.END)
        return "break"
    except Exception:
        return None


def _copy_selection(event):
    widget = _get_editable_widget(event.widget)
    if widget is None:
        return None
    text = _get_selected_text(widget)
    if not text:
        return None
    root = widget._root()
    try:
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update_idletasks()
        return "break"
    except Exception:
        return None


def _cut_selection(event):
    widget = _get_editable_widget(event.widget)
    if widget is None:
        return None
    copied = _copy_selection(event)
    if copied != "break":
        return None
    try:
        if isinstance(widget, tk.Text):
            widget.delete("sel.first", "sel.last")
        else:
            start, end = _get_entry_selection_range(widget)
            if start is None or end is None:
                return None
            widget.delete(start, end)
        return "break"
    except Exception:
        return None


def _paste_clipboard(event):
    widget = _get_editable_widget(event.widget)
    if widget is None:
        return None
    try:
        text = widget.clipboard_get()
    except Exception:
        return None
    if text is None:
        return None
    try:
        if isinstance(widget, tk.Text):
            if widget.tag_ranges("sel"):
                widget.delete("sel.first", "sel.last")
            widget.insert("insert", text)
            widget.see("insert")
        else:
            start, end = _get_entry_selection_range(widget)
            if start is not None and end is not None:
                widget.delete(start, end)
                insert_at = start
            else:
                insert_at = widget.index(tk.INSERT)
            widget.insert(insert_at, text)
            widget.icursor(insert_at + len(text))
        return "break"
    except Exception:
        return None


def _get_editable_widget(widget):
    root = widget._root()
    target = widget
    if not _is_supported_widget(target):
        try:
            target = root.focus_get()
        except Exception:
            target = None
    if _is_supported_widget(target):
        return target
    return None


def _is_supported_widget(widget):
    if widget is None:
        return False
    try:
        widget_class = widget.winfo_class()
    except Exception:
        return False
    return widget_class in {"Entry", "TEntry", "Text", "Spinbox", "TCombobox"}


def _get_selected_text(widget):
    try:
        if isinstance(widget, tk.Text):
            if widget.tag_ranges("sel"):
                return widget.get("sel.first", "sel.last")
            return ""
        start, end = _get_entry_selection_range(widget)
        if start is None or end is None:
            return ""
        return widget.get()[start:end]
    except Exception:
        return ""


def _get_entry_selection_range(widget):
    try:
        start = int(widget.index("sel.first"))
        end = int(widget.index("sel.last"))
        return start, end
    except Exception:
        return None, None


def mousewheel_units(event):
    button_num = getattr(event, "num", None)
    if button_num == 4:
        return -1
    if button_num == 5:
        return 1

    delta = int(getattr(event, "delta", 0) or 0)
    if delta == 0:
        return 0

    if sys.platform == "darwin":
        units = -delta
        return units if units != 0 else (-1 if delta > 0 else 1)

    units = int(-delta / 120)
    return units if units != 0 else (-1 if delta > 0 else 1)


def scroll_canvas_on_mousewheel(canvas, event):
    units = mousewheel_units(event)
    if not units:
        return None
    canvas.yview_scroll(units, "units")
    return "break"


def bind_mousewheel_recursive(widget, handler):
    try:
        widget.bind("<MouseWheel>", handler, add="+")
        widget.bind("<Button-4>", handler, add="+")
        widget.bind("<Button-5>", handler, add="+")
    except Exception:
        return

    for child in widget.winfo_children():
        bind_mousewheel_recursive(child, handler)
