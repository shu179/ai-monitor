"""Export/import helpers for monitoring runtime data."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .app_paths import resolve_app_path
from .daily_task_state import STATE_PATH as DAILY_TASK_STATUS_PATH
from .diagnostics import DIAGNOSTICS_PATH
from .history import HISTORY_DIR
from .scheduler_state import STATE_PATH as SCHEDULER_STATE_PATH


def _read_json_file(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except FileNotFoundError:
        return copy.deepcopy(default)
    except Exception:
        return copy.deepcopy(default)


def _write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _merge_dicts(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base or {})
    for key, value in (incoming or {}).items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _merge_dicts(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _list_history_files() -> list[Path]:
    if not HISTORY_DIR.exists():
        return []
    return sorted(path for path in HISTORY_DIR.glob("*.json") if path.is_file())


def _record_dedupe_key(item: Any) -> str:
    if isinstance(item, dict):
        record_id = str(item.get("id", "")).strip()
        if record_id:
            return f"id:{record_id}"
        payload = json.dumps(item, ensure_ascii=False, sort_keys=True)
        return f"raw:{payload}"
    return f"raw:{json.dumps(item, ensure_ascii=False, sort_keys=True)}"


def _merge_lists(existing: list[Any], incoming: list[Any]) -> list[Any]:
    result = [copy.deepcopy(item) for item in existing]
    seen = {_record_dedupe_key(item) for item in result}
    for item in incoming:
        key = _record_dedupe_key(item)
        if key in seen:
            continue
        seen.add(key)
        result.append(copy.deepcopy(item))
    return result


def export_monitoring_sync_bundle() -> dict[str, Any]:
    history_files: dict[str, Any] = {}
    for path in _list_history_files():
        history_files[path.name] = _read_json_file(path, [])

    return {
        "history_files": history_files,
        "daily_task_status": _read_json_file(DAILY_TASK_STATUS_PATH, {}),
        "scheduler_state": _read_json_file(SCHEDULER_STATE_PATH, {}),
        "diagnostics": _read_json_file(DIAGNOSTICS_PATH, []),
    }


def import_monitoring_sync_bundle(bundle: dict[str, Any] | None, *, mode: str = "merge") -> dict[str, Any]:
    payload = bundle if isinstance(bundle, dict) else {}
    normalized_mode = str(mode or "merge").strip().lower()
    if normalized_mode not in {"merge", "replace"}:
        normalized_mode = "merge"

    history_files = payload.get("history_files", {})
    if not isinstance(history_files, dict):
        history_files = {}

    imported_history_files = 0
    if normalized_mode == "replace" and HISTORY_DIR.exists():
        for path in _list_history_files():
            try:
                path.unlink()
            except Exception:
                pass

    for raw_name, raw_data in history_files.items():
        file_name = Path(str(raw_name or "").strip()).name
        if not file_name.endswith(".json"):
            continue
        target_path = HISTORY_DIR / file_name
        incoming_data = copy.deepcopy(raw_data)
        if normalized_mode == "replace":
            final_data = incoming_data
        else:
            existing_data = _read_json_file(target_path, [] if isinstance(incoming_data, list) else {})
            if isinstance(existing_data, list) and isinstance(incoming_data, list):
                final_data = _merge_lists(existing_data, incoming_data)
            elif isinstance(existing_data, dict) and isinstance(incoming_data, dict):
                final_data = _merge_dicts(existing_data, incoming_data)
            else:
                final_data = incoming_data
        _write_json_atomic(target_path, final_data)
        imported_history_files += 1

    daily_payload = payload.get("daily_task_status", {})
    if isinstance(daily_payload, dict):
        if normalized_mode == "replace":
            final_daily = copy.deepcopy(daily_payload)
        else:
            final_daily = _merge_dicts(_read_json_file(DAILY_TASK_STATUS_PATH, {}), daily_payload)
        _write_json_atomic(DAILY_TASK_STATUS_PATH, final_daily)
    else:
        final_daily = _read_json_file(DAILY_TASK_STATUS_PATH, {})

    scheduler_payload = payload.get("scheduler_state", {})
    if isinstance(scheduler_payload, dict):
        if normalized_mode == "replace":
            final_scheduler = copy.deepcopy(scheduler_payload)
        else:
            final_scheduler = _merge_dicts(_read_json_file(SCHEDULER_STATE_PATH, {}), scheduler_payload)
        _write_json_atomic(SCHEDULER_STATE_PATH, final_scheduler)
    else:
        final_scheduler = _read_json_file(SCHEDULER_STATE_PATH, {})

    diagnostics_payload = payload.get("diagnostics", [])
    if isinstance(diagnostics_payload, list):
        if normalized_mode == "replace":
            final_diagnostics = copy.deepcopy(diagnostics_payload)
        else:
            final_diagnostics = _merge_lists(_read_json_file(DIAGNOSTICS_PATH, []), diagnostics_payload)
        _write_json_atomic(DIAGNOSTICS_PATH, final_diagnostics)
    else:
        final_diagnostics = _read_json_file(DIAGNOSTICS_PATH, [])

    return {
        "mode": normalized_mode,
        "history_file_count": imported_history_files,
        "history_total_files": len(_list_history_files()),
        "daily_task_status_entries": len(final_daily),
        "scheduler_state_entries": len(final_scheduler),
        "diagnostics_entries": len(final_diagnostics),
    }
