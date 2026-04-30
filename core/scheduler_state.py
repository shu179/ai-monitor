"""
调度状态持久层

只服务于自动调度排序：
- 记录每个任务单元的失败惩罚和平均耗时
- 记录当天该任务单元是否已自动执行
- 不参与主页展示，不与历史记录混用
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import date

from .app_paths import resolve_app_path
from .time_utils import local_now, local_today


STATE_PATH = resolve_app_path("user_data/scheduler_state.json")
_LOCK = threading.Lock()


def _now_text() -> str:
    return local_now().isoformat(timespec="seconds")


def _today_text(target_date: date | None = None) -> str:
    return (target_date or local_today()).isoformat()


def _default_entry(unit_id: str) -> dict:
    return {
        "unit_id": unit_id,
        "task_name": "",
        "mode": "",
        "last_scheduled_time": "",
        "scheduler_penalty": 0,
        "recent_auto_failures": 0,
        "consecutive_auto_failures": 0,
        "structural_failures": 0,
        "avg_duration_seconds": 0.0,
        "last_auto_success_at": "",
        "last_auto_fail_at": "",
        "last_failure_kind": "",
        "last_run_recovered_manually": False,
        "last_auto_run_date": "",
        "last_round_status": "pending",
        "updated_at": "",
    }


def _load_all() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[SchedulerState] 读取状态失败: {e}")
        return {}


def _save_all(data: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(STATE_PATH.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, STATE_PATH)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        print(f"[SchedulerState] 保存状态失败: {e}")


def _prune(data: dict, keep_days: int = 90) -> dict:
    cutoff = local_today().toordinal() - max(keep_days, 30)
    pruned = {}
    for unit_id, entry in data.items():
        if not isinstance(entry, dict):
            continue
        date_text = str(entry.get("last_auto_run_date") or "").strip()
        if not date_text:
            pruned[unit_id] = entry
            continue
        try:
            ordinal = date.fromisoformat(date_text).toordinal()
        except Exception:
            pruned[unit_id] = entry
            continue
        if ordinal >= cutoff:
            pruned[unit_id] = entry
    return pruned


def get_entry(unit_id: str) -> dict:
    with _LOCK:
        data = _load_all()
        entry = data.get(unit_id)
        if isinstance(entry, dict):
            result = _default_entry(unit_id)
            result.update(entry)
            return result
        return _default_entry(unit_id)


def get_last_auto_run_date(unit_id: str) -> str:
    return str(get_entry(unit_id).get("last_auto_run_date") or "").strip()


def mark_processed(
    unit_id: str,
    *,
    task_name: str,
    mode: str,
    round_status: str,
    scheduled_time: str = "",
    target_date: date | None = None,
) -> dict:
    with _LOCK:
        data = _prune(_load_all())
        entry = _default_entry(unit_id)
        entry.update(data.get(unit_id) or {})
        entry["unit_id"] = unit_id
        entry["task_name"] = str(task_name or "").strip()
        entry["mode"] = str(mode or "").strip()
        entry["last_scheduled_time"] = str(scheduled_time or "").strip()
        entry["last_auto_run_date"] = _today_text(target_date)
        entry["last_round_status"] = str(round_status or "pending").strip()
        entry["updated_at"] = _now_text()
        data[unit_id] = entry
        _save_all(data)
        return dict(entry)


def update_after_run(
    unit_id: str,
    *,
    task_name: str,
    mode: str,
    round_status: str,
    scheduled_time: str = "",
    failure_kind: str = "",
    duration_seconds: float = 0.0,
    recovered_manually: bool = False,
    target_date: date | None = None,
) -> dict:
    with _LOCK:
        data = _prune(_load_all())
        entry = _default_entry(unit_id)
        entry.update(data.get(unit_id) or {})

        now_text = _now_text()
        entry["unit_id"] = unit_id
        entry["task_name"] = str(task_name or "").strip()
        entry["mode"] = str(mode or "").strip()
        entry["last_scheduled_time"] = str(scheduled_time or "").strip()
        entry["last_auto_run_date"] = _today_text(target_date)
        entry["last_round_status"] = str(round_status or "pending").strip()
        entry["last_run_recovered_manually"] = bool(recovered_manually)
        entry["updated_at"] = now_text

        if duration_seconds and duration_seconds > 0:
            old_avg = float(entry.get("avg_duration_seconds") or 0.0)
            if old_avg <= 0:
                entry["avg_duration_seconds"] = round(float(duration_seconds), 2)
            else:
                entry["avg_duration_seconds"] = round(old_avg * 0.7 + float(duration_seconds) * 0.3, 2)

        if round_status == "success":
            entry["last_auto_success_at"] = now_text
            if not recovered_manually:
                entry["consecutive_auto_failures"] = 0
                penalty = int(entry.get("scheduler_penalty") or 0)
                entry["scheduler_penalty"] = max(0, penalty - 1)
                recent_failures = int(entry.get("recent_auto_failures") or 0)
                if recent_failures > 0:
                    entry["recent_auto_failures"] = recent_failures - 1
            data[unit_id] = entry
            _save_all(data)
            return dict(entry)

        if round_status == "partial":
            entry["last_auto_fail_at"] = now_text
            entry["recent_auto_failures"] = int(entry.get("recent_auto_failures") or 0) + 1
            entry["consecutive_auto_failures"] = int(entry.get("consecutive_auto_failures") or 0) + 1
            if failure_kind == "structural":
                entry["structural_failures"] = int(entry.get("structural_failures") or 0) + 1
                entry["scheduler_penalty"] = int(entry.get("scheduler_penalty") or 0) + 3
            else:
                entry["scheduler_penalty"] = int(entry.get("scheduler_penalty") or 0) + 1
            entry["last_failure_kind"] = str(failure_kind or "temporary").strip()
            data[unit_id] = entry
            _save_all(data)
            return dict(entry)

        if round_status == "failed":
            entry["last_auto_fail_at"] = now_text
            entry["recent_auto_failures"] = int(entry.get("recent_auto_failures") or 0) + 1
            entry["consecutive_auto_failures"] = int(entry.get("consecutive_auto_failures") or 0) + 1
            if failure_kind == "structural":
                entry["structural_failures"] = int(entry.get("structural_failures") or 0) + 1
                entry["scheduler_penalty"] = int(entry.get("scheduler_penalty") or 0) + 5
            else:
                entry["scheduler_penalty"] = int(entry.get("scheduler_penalty") or 0) + 2
            entry["last_failure_kind"] = str(failure_kind or "temporary").strip()
            data[unit_id] = entry
            _save_all(data)
            return dict(entry)

        data[unit_id] = entry
        _save_all(data)
        return dict(entry)
