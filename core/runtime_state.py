"""
运行偏好持久层

用于记录跨重启的轻量运行偏好，例如是否需要在下次启动时自动恢复监控。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading

from .app_paths import resolve_app_path
from .time_utils import local_now


STATE_PATH = resolve_app_path("user_data/runtime_state.json")
_LOCK = threading.Lock()


def _default_state() -> dict:
    return {
        "auto_resume_monitoring": False,
        "updated_at": "",
    }


def _load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            state = _default_state()
            state.update(data)
            return state
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[RuntimeState] 读取状态失败: {e}")
    return _default_state()


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(STATE_PATH.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, STATE_PATH)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        print(f"[RuntimeState] 保存状态失败: {e}")


def get_runtime_state() -> dict:
    with _LOCK:
        return dict(_load_state())


def should_auto_resume_monitoring() -> bool:
    return bool(get_runtime_state().get("auto_resume_monitoring"))


def set_auto_resume_monitoring(enabled: bool) -> dict:
    with _LOCK:
        state = _load_state()
        state["auto_resume_monitoring"] = bool(enabled)
        state["updated_at"] = local_now().isoformat(timespec="seconds")
        _save_state(state)
        return dict(state)
