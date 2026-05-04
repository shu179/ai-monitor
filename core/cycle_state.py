"""
自动调度整轮结果持久层

用于把当天自动调度的整轮结果保存下来，并允许识别模式在后续补齐成功后
继续更新同一份整轮口径，确保“整轮汇总”和“识别补齐”使用同一份结果视图。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from copy import deepcopy
from datetime import date

from .app_paths import resolve_app_path
from .local_account_space import account_scoped_path
from .time_utils import local_now, local_today


STATE_PATH = resolve_app_path("user_data/scheduler_cycle_state.json")
_LOCK = threading.Lock()


def _today_text(target_date: date | None = None) -> str:
    return (target_date or local_today()).isoformat()


def _now_text() -> str:
    return local_now().isoformat(timespec="seconds")


def _dedupe_text_list(values: list[str] | tuple[str, ...] | set[str] | None) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
    return items


def build_task_outcome(task_id: str, task_name: str) -> dict:
    normalized_task_id = str(task_id or "").strip()
    normalized_task_name = str(task_name or normalized_task_id).strip() or normalized_task_id
    return {
        "task_id": normalized_task_id,
        "task_name": normalized_task_name,
        "final_status": "skipped",
        "successful_brands": [],
        "failed_query_details": [],
        "task_failure_message": "",
        "executed_modes": [],
    }


def resolve_report_status(report: dict | None, fallback: str = "skipped") -> str:
    return str(
        (report or {}).get("task_status")
        or (report or {}).get("round_status")
        or fallback
    ).strip() or fallback


def resolve_report_failure_message(report: dict | None, fallback: str = "") -> str:
    return str(
        (report or {}).get("task_failure_message")
        or (report or {}).get("notification_error")
        or (report or {}).get("failure_kind")
        or fallback
    ).strip()


def is_report_success(report: dict | None) -> bool:
    return resolve_report_status(report, fallback="") == "success"


def resolve_report_display_message(
    report: dict | None,
    *,
    success_message: str = "测试成功",
    failure_message: str = "测试失败",
    send_failure_message: str = "测试失败：查询已完成，但发送未成功",
) -> str:
    if is_report_success(report):
        return success_message
    query_round_status = str(
        (report or {}).get("query_round_status")
        or (report or {}).get("round_status")
        or ""
    ).strip()
    if query_round_status == "success":
        return send_failure_message
    resolved = resolve_report_failure_message(report)
    return resolved or failure_message


def merge_task_outcome_from_report(
    task_outcomes: list[dict],
    report: dict,
    *,
    task_id: str | None = None,
    task_name: str | None = None,
    mode: str | None = None,
) -> dict:
    normalized_task_id = str(
        task_id
        or report.get("original_task_id")
        or report.get("task_id")
        or ""
    ).strip()
    normalized_task_name = str(
        task_name
        or report.get("task_name")
        or normalized_task_id
    ).strip() or normalized_task_id

    entry = None
    for item in task_outcomes:
        if str(item.get("task_id") or "").strip() == normalized_task_id:
            entry = item
            break
    if entry is None:
        entry = build_task_outcome(normalized_task_id, normalized_task_name)
        task_outcomes.append(entry)

    entry["task_name"] = normalized_task_name
    normalized_mode = str(mode or report.get("mode") or "").strip()
    if normalized_mode and normalized_mode not in entry["executed_modes"]:
        entry["executed_modes"].append(normalized_mode)

    for brand in _dedupe_text_list(report.get("successful_brands") or []):
        if brand not in entry["successful_brands"]:
            entry["successful_brands"].append(brand)

    status = resolve_report_status(report)
    if status == "success":
        entry["final_status"] = "success"
        entry["failed_query_details"] = []
        entry["task_failure_message"] = ""
        return entry

    if str(entry.get("final_status") or "").strip() == "success":
        return entry

    if status not in {"", "skipped"}:
        entry["final_status"] = "failed"
        entry["failed_query_details"] = list(report.get("failed_query_details") or [])
        entry["task_failure_message"] = resolve_report_failure_message(report)

    return entry


def _load_all() -> dict:
    state_path = _state_path()
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[CycleState] 读取状态失败: {e}")
        return {}


def _save_all(data: dict) -> None:
    state_path = _state_path()
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(state_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, state_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        print(f"[CycleState] 保存状态失败: {e}")


def _state_path():
    resolved_default = resolve_app_path("user_data/scheduler_cycle_state.json")
    if STATE_PATH != resolved_default:
        return STATE_PATH
    return account_scoped_path("user_data/scheduler_cycle_state.json", fallback=resolved_default)


def _prune(data: dict, keep_days: int = 45) -> dict:
    cutoff = local_today().toordinal() - max(keep_days, 7)
    pruned = {}
    for key, value in data.items():
        try:
            ordinal = date.fromisoformat(str(key)).toordinal()
        except Exception:
            pruned[key] = value
            continue
        if ordinal >= cutoff:
            pruned[key] = value
    return pruned


def _normalize_cycle_payload(payload: dict | None, *, cycle_date: str) -> dict:
    normalized = deepcopy(payload or {})
    normalized["date"] = str(normalized.get("date") or cycle_date).strip() or cycle_date
    normalized["scheduled_time"] = str(normalized.get("scheduled_time") or "").strip()
    normalized["default_mode"] = str(normalized.get("default_mode") or "").strip()
    normalized["executed_rounds"] = list(normalized.get("executed_rounds") or [])
    normalized["task_outcomes"] = list(normalized.get("task_outcomes") or [])
    normalized["summary"] = dict(normalized.get("summary") or {})
    normalized["all_success_notified_at"] = str(normalized.get("all_success_notified_at") or "").strip()
    normalized["updated_at"] = _now_text()
    return normalized


def save_cycle_report(cycle_payload: dict) -> dict:
    cycle_date = str((cycle_payload or {}).get("date") or _today_text()).strip() or _today_text()
    normalized = _normalize_cycle_payload(cycle_payload, cycle_date=cycle_date)
    with _LOCK:
        data = _prune(_load_all())
        data[cycle_date] = normalized
        _save_all(data)
    return deepcopy(normalized)


def get_cycle_report(target_date: date | None = None) -> dict | None:
    cycle_date = _today_text(target_date)
    with _LOCK:
        data = _load_all()
        payload = data.get(cycle_date)
    if not isinstance(payload, dict):
        return None
    return _normalize_cycle_payload(payload, cycle_date=cycle_date)


def mark_cycle_success_notified(target_date: date | None = None) -> dict | None:
    cycle_date = _today_text(target_date)
    with _LOCK:
        data = _prune(_load_all())
        existing = data.get(cycle_date)
        if not isinstance(existing, dict):
            return None
        payload = _normalize_cycle_payload(existing, cycle_date=cycle_date)
        payload["all_success_notified_at"] = _now_text()
        payload["updated_at"] = _now_text()
        data[cycle_date] = payload
        _save_all(data)
        return deepcopy(payload)


def _rebuild_summary(task_outcomes: list[dict]) -> dict:
    success = 0
    failed = 0
    skipped = 0
    for item in task_outcomes:
        status = str(item.get("final_status") or "skipped").strip()
        if status == "success":
            success += 1
        elif status == "failed":
            failed += 1
        else:
            skipped += 1
    return {
        "total_tasks": len(task_outcomes),
        "success": success,
        "failed": failed,
        "skipped": skipped,
    }


def _rebuild_round_summary(reports: list[dict], *, mode: str, mode_label: str) -> dict:
    success = 0
    failed = 0
    skipped = 0
    for item in reports:
        status = resolve_report_status(item)
        if status == "success":
            success += 1
        elif status == "failed":
            failed += 1
        else:
            skipped += 1
    return {
        "mode": str(mode or "").strip(),
        "mode_label": str(mode_label or mode or "").strip(),
        "total": len(reports),
        "completed": len(reports),
        "success": success,
        "failed": failed,
        "skipped": skipped,
    }


def _upsert_recognition_round(cycle_payload: dict, report: dict) -> None:
    rounds = list(cycle_payload.get("executed_rounds") or [])
    recognition_round = None
    for item in rounds:
        if str(item.get("mode") or "").strip() == "recognition":
            recognition_round = item
            break
    if recognition_round is None:
        recognition_round = {
            "mode": "recognition",
            "mode_label": "识别模式",
            "summary": {
                "mode": "recognition",
                "mode_label": "识别模式",
                "total": 0,
                "completed": 0,
                "success": 0,
                "failed": 0,
                "skipped": 0,
            },
            "reports": [],
            "scheduled_time": str(cycle_payload.get("scheduled_time") or "").strip(),
            "date": str(cycle_payload.get("date") or _today_text()).strip(),
        }
        rounds.append(recognition_round)

    reports = list(recognition_round.get("reports") or [])
    replaced = False
    task_id = str(report.get("original_task_id") or report.get("task_id") or "").strip()
    for idx, item in enumerate(reports):
        existing_task_id = str(item.get("original_task_id") or item.get("task_id") or "").strip()
        if existing_task_id == task_id and task_id:
            reports[idx] = deepcopy(report)
            replaced = True
            break
    if not replaced:
        reports.append(deepcopy(report))

    success = 0
    failed = 0
    skipped = 0
    for item in reports:
        status = resolve_report_status(item)
        if status == "success":
            success += 1
        elif status == "failed":
            failed += 1
        else:
            skipped += 1

    recognition_round["reports"] = reports
    recognition_round["summary"] = {
        "mode": "recognition",
        "mode_label": "识别模式",
        "total": len(reports),
        "completed": len(reports),
        "success": success,
        "failed": failed,
        "skipped": skipped,
    }
    cycle_payload["executed_rounds"] = rounds


def update_cycle_report_with_recognition(
    *,
    task_id: str,
    task_name: str,
    brands: list[str] | None = None,
    ok: bool,
    reason: str = "",
    supplemented_keywords: list[str] | None = None,
    completed_keywords: list[str] | None = None,
    detected_platforms: list[str] | None = None,
    image_count: int = 0,
    target_date: date | None = None,
) -> dict | None:
    cycle_date = _today_text(target_date)
    with _LOCK:
        data = _prune(_load_all())
        existing = data.get(cycle_date)
        if not isinstance(existing, dict):
            return None

        payload = _normalize_cycle_payload(existing, cycle_date=cycle_date)
        task_outcomes = list(payload.get("task_outcomes") or [])
        brands = [str(item).strip() for item in (brands or []) if str(item).strip()]
        supplemented_keywords = [
            str(item).strip() for item in (supplemented_keywords or []) if str(item).strip()
        ]
        completed_keywords = [
            str(item).strip() for item in (completed_keywords or []) if str(item).strip()
        ]
        detected_platforms = [
            str(item).strip() for item in (detected_platforms or []) if str(item).strip()
        ]

        recognition_report = {
            "task_id": task_id,
            "original_task_id": task_id,
            "task_name": task_name,
            "mode": "recognition",
            "mode_label": "识别模式",
            "round_status": "success" if ok else "failed",
            "task_status": "success" if ok else "failed",
            "task_failure_kind": "" if ok else "notification",
            "task_failure_message": "" if ok else str(reason or "识别模式补齐后发送失败").strip(),
            "duration_seconds": 0.0,
            "successful_brands": list(brands),
            "failed_query_details": [] if ok else [{
                "keyword": ",".join(supplemented_keywords),
                "platform": "recognition",
                "brand": ",".join(brands),
                "mode": "recognition",
                "error_message": str(reason or "识别模式补齐后发送失败").strip(),
                "failure_type": "notification",
            }],
            "supplemented_keywords": list(supplemented_keywords),
            "completed_keywords": list(completed_keywords),
            "detected_platforms": list(detected_platforms),
            "image_count": max(0, int(image_count or 0)),
            "recovered_by": "recognition",
        }

        merge_task_outcome_from_report(
            task_outcomes,
            recognition_report,
            task_id=task_id,
            task_name=task_name,
            mode="recognition",
        )
        payload["task_outcomes"] = task_outcomes
        payload["summary"] = _rebuild_summary(task_outcomes)
        _upsert_recognition_round(payload, recognition_report)
        payload["updated_at"] = _now_text()
        data[cycle_date] = payload
        _save_all(data)
        return deepcopy(payload)


def update_cycle_report_with_forced_success(
    *,
    task_id: str,
    task_name: str = "",
    brands: list[str] | None = None,
    completed_keywords: list[str] | None = None,
    detected_platforms: list[str] | None = None,
    image_count: int = 0,
    target_date: date | None = None,
) -> dict | None:
    cycle_date = _today_text(target_date)
    normalized_task_id = str(task_id or "").strip()
    if not normalized_task_id:
        return None

    with _LOCK:
        data = _prune(_load_all())
        existing = data.get(cycle_date)
        if not isinstance(existing, dict):
            return None

        payload = _normalize_cycle_payload(existing, cycle_date=cycle_date)
        task_outcomes = list(payload.get("task_outcomes") or [])
        brands = _dedupe_text_list(brands)
        completed_keywords = _dedupe_text_list(completed_keywords)
        detected_platforms = _dedupe_text_list(detected_platforms)

        matched_outcome = None
        for item in task_outcomes:
            if str(item.get("task_id") or "").strip() == normalized_task_id:
                matched_outcome = item
                break
        if matched_outcome is None:
            matched_outcome = build_task_outcome(normalized_task_id, task_name or normalized_task_id)
            task_outcomes.append(matched_outcome)

        if task_name:
            matched_outcome["task_name"] = str(task_name).strip() or normalized_task_id
        for brand in brands:
            if brand not in matched_outcome["successful_brands"]:
                matched_outcome["successful_brands"].append(brand)
        matched_outcome["final_status"] = "success"
        matched_outcome["failed_query_details"] = []
        matched_outcome["task_failure_message"] = ""

        rounds = list(payload.get("executed_rounds") or [])
        for round_item in rounds:
            reports = list(round_item.get("reports") or [])
            touched = False
            for report in reports:
                report_task_id = str(report.get("original_task_id") or report.get("task_id") or "").strip()
                if report_task_id != normalized_task_id:
                    continue
                existing_brands = _dedupe_text_list(report.get("successful_brands") or [])
                report["successful_brands"] = _dedupe_text_list(existing_brands + brands)
                report["round_status"] = "success"
                report["task_status"] = "success"
                report["task_failure_kind"] = ""
                report["task_failure_message"] = ""
                report["failed_query_details"] = []
                report["notification_error"] = ""
                report["recovered_manually"] = True
                report["forced_ignore_failure"] = True
                if completed_keywords:
                    report["completed_keywords"] = list(completed_keywords)
                if detected_platforms:
                    report["detected_platforms"] = list(detected_platforms)
                if image_count > 0:
                    report["image_count"] = max(0, int(image_count or 0))
                touched = True
            if touched:
                round_item["reports"] = reports
                round_item["summary"] = _rebuild_round_summary(
                    reports,
                    mode=str(round_item.get("mode") or "").strip(),
                    mode_label=str(round_item.get("mode_label") or "").strip(),
                )

        payload["executed_rounds"] = rounds
        payload["task_outcomes"] = task_outcomes
        payload["summary"] = _rebuild_summary(task_outcomes)
        payload["updated_at"] = _now_text()
        data[cycle_date] = payload
        _save_all(data)
        return deepcopy(payload)
