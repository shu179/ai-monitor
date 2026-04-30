"""UI 层配置保存与刷新通知工具。"""

from __future__ import annotations

from tkinter import messagebox

from core.config_watcher import load_config as load_yaml_config
from core.config_watcher import save_config as save_yaml_config
from core.daily_task_state import ensure_config_task_ids


def emit_config_updated(event_root) -> None:
    """向 Tk 根窗口广播配置更新事件。"""
    if event_root is None:
        return
    try:
        event_root.event_generate("<<ConfigUpdated>>", when="tail")
    except Exception:
        pass


def persist_config(
    config: dict,
    config_path,
    *,
    ensure_task_ids: bool = False,
    on_config_change=None,
    event_root=None,
) -> dict:
    """保存配置，并按需触发回调和 UI 刷新事件。"""
    if ensure_task_ids:
        ensure_config_task_ids(config)
    save_yaml_config(config, config_path)
    try:
        config = load_yaml_config(config_path)
    except Exception:
        pass
    if on_config_change:
        on_config_change(config)
    emit_config_updated(event_root)
    return config


def persist_config_with_feedback(
    config: dict,
    config_path,
    *,
    success_message: str,
    ensure_task_ids: bool = False,
    on_config_change=None,
    event_root=None,
    on_success=None,
) -> bool:
    """保存配置并展示统一成功/失败反馈。"""
    try:
        persist_config(
            config,
            config_path,
            ensure_task_ids=ensure_task_ids,
            on_config_change=on_config_change,
            event_root=event_root,
        )
        messagebox.showinfo("成功", success_message)
        if on_success:
            on_success()
        return True
    except Exception as e:
        messagebox.showerror("错误", f"保存失败: {e}")
        return False
