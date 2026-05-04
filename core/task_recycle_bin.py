from __future__ import annotations

import copy
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from .daily_task_state import assign_task_id, derive_task_id
from .time_utils import local_now


DELETED_TASKS_KEY = "deleted_tasks"
TASK_DELETE_RETENTION_DAYS = 3
TASK_DELETE_PENDING_FIELDS = {
    "delete_pending",
    "delete_pending_at",
    "delete_pending_expires_at",
    "delete_pending_reason",
    "delete_pending_error",
}


def retention_expires_at(now: datetime | None = None) -> str:
    current = now or local_now()
    return (current + timedelta(days=TASK_DELETE_RETENTION_DAYS)).isoformat(timespec="seconds")


def ensure_deleted_tasks(config: dict[str, Any]) -> list[dict[str, Any]]:
    items = config.get(DELETED_TASKS_KEY)
    if not isinstance(items, list):
        items = []
        config[DELETED_TASKS_KEY] = items
    normalized = [item for item in items if isinstance(item, dict)]
    if len(normalized) != len(items):
        config[DELETED_TASKS_KEY] = normalized
    return normalized


def purge_expired_deleted_tasks(config: dict[str, Any], *, now: datetime | None = None) -> bool:
    items = ensure_deleted_tasks(config)
    current = now or local_now()
    kept: list[dict[str, Any]] = []
    changed = False
    for item in items:
        expires_at = _parse_datetime(item.get("expires_at") or item.get("delete_expires_at"))
        if expires_at is not None and expires_at <= current:
            changed = True
            continue
        kept.append(item)
    if changed:
        config[DELETED_TASKS_KEY] = kept
    return changed


def mark_task_delete_pending(
    task: dict[str, Any],
    *,
    reason: str = "formal_running",
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or local_now()
    task["delete_pending"] = True
    task["delete_pending_at"] = current.isoformat(timespec="seconds")
    task["delete_pending_expires_at"] = retention_expires_at(current)
    task["delete_pending_reason"] = reason
    task.pop("delete_pending_error", None)
    return task


def clear_task_delete_pending(task: dict[str, Any]) -> dict[str, Any]:
    for key in TASK_DELETE_PENDING_FIELDS:
        task.pop(key, None)
    return task


def soft_delete_task(
    config: dict[str, Any],
    task_id: str,
    *,
    source: str = "local",
    reason: str = "",
    now: datetime | None = None,
    deleted_at: str | None = None,
    expires_at: str | None = None,
) -> dict[str, Any] | None:
    tasks = [item for item in (config.get("tasks") or []) if isinstance(item, dict)]
    target_index = None
    for index, task in enumerate(tasks):
        if str(task.get("task_id") or derive_task_id(task)).strip() == str(task_id or "").strip():
            target_index = index
            break
    if target_index is None:
        config["tasks"] = tasks
        return None

    task = tasks.pop(target_index)
    config["tasks"] = tasks
    return upsert_deleted_task_backup(
        config,
        task,
        source=source,
        reason=reason,
        now=now,
        deleted_at=deleted_at,
        expires_at=expires_at,
    )


def upsert_deleted_task_backup(
    config: dict[str, Any],
    task: dict[str, Any],
    *,
    source: str = "local",
    reason: str = "",
    now: datetime | None = None,
    deleted_at: str | None = None,
    expires_at: str | None = None,
    tombstone_id: str | None = None,
) -> dict[str, Any]:
    current = now or local_now()
    backup_task = _clean_task_for_backup(task)
    task_id = str(backup_task.get("task_id") or derive_task_id(backup_task)).strip()
    if task_id:
        backup_task["task_id"] = task_id
    cloud_task_id = _safe_int(backup_task.get("cloud_task_id") or backup_task.get("cloudTaskId"))
    item_deleted_at = str(deleted_at or current.isoformat(timespec="seconds")).strip()
    item_expires_at = str(expires_at or retention_expires_at(current)).strip()
    item_id = str(tombstone_id or _stable_tombstone_id(task_id, cloud_task_id)).strip()
    item = {
        "id": item_id,
        "task_id": task_id,
        "name": str(backup_task.get("name") or backup_task.get("brand") or task_id).strip(),
        "brand": str(backup_task.get("brand") or backup_task.get("name") or task_id).strip(),
        "cloud_task_id": cloud_task_id,
        "cloud_task_key": str(backup_task.get("cloud_task_key") or backup_task.get("cloudTaskKey") or "").strip(),
        "source": str(source or "local").strip(),
        "reason": str(reason or "").strip(),
        "deleted_at": item_deleted_at,
        "expires_at": item_expires_at,
        "task": backup_task,
    }

    items = ensure_deleted_tasks(config)
    deduped: list[dict[str, Any]] = []
    for existing in items:
        if _same_deleted_task(existing, item):
            continue
        deduped.append(existing)
    deduped.append(item)
    deduped.sort(key=lambda value: str(value.get("deleted_at") or ""), reverse=True)
    config[DELETED_TASKS_KEY] = deduped
    return item


def restore_deleted_task(
    config: dict[str, Any],
    *,
    deleted_task_id: str = "",
    brand_name: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    purge_expired_deleted_tasks(config, now=now)
    items = ensure_deleted_tasks(config)
    candidate_index = _find_deleted_task_index(items, deleted_task_id=deleted_task_id, brand_name=brand_name)
    if candidate_index is None:
        return {"ok": False, "message": "未找到可恢复的品牌配置"}

    candidate = items[candidate_index]
    task = copy.deepcopy(candidate.get("task") if isinstance(candidate.get("task"), dict) else {})
    if not task:
        return {"ok": False, "message": "备份中的任务配置不完整，无法恢复"}
    clear_task_delete_pending(task)
    assign_task_id(task)
    task_id = str(task.get("task_id") or "").strip()
    active_tasks = [item for item in (config.get("tasks") or []) if isinstance(item, dict)]
    if any(str(item.get("task_id") or derive_task_id(item)).strip() == task_id for item in active_tasks):
        return {"ok": False, "message": f"任务 {task_id} 已存在，无法重复恢复"}

    active_tasks.append(task)
    config["tasks"] = active_tasks
    config[DELETED_TASKS_KEY] = [item for index, item in enumerate(items) if index != candidate_index]
    return {"ok": True, "task": task, "deleted_task": candidate}


def deleted_task_snapshots(config: dict[str, Any], *, now: datetime | None = None) -> list[dict[str, Any]]:
    purge_expired_deleted_tasks(config, now=now)
    snapshots: list[dict[str, Any]] = []
    for item in ensure_deleted_tasks(config):
        task = item.get("task") if isinstance(item.get("task"), dict) else {}
        snapshots.append(
            {
                "id": str(item.get("id") or "").strip(),
                "task_id": str(item.get("task_id") or task.get("task_id") or "").strip(),
                "name": str(item.get("name") or task.get("name") or "").strip(),
                "brand": str(item.get("brand") or task.get("brand") or task.get("name") or "").strip(),
                "cloud_task_id": item.get("cloud_task_id"),
                "cloud_task_key": str(item.get("cloud_task_key") or "").strip(),
                "source": str(item.get("source") or "").strip(),
                "deleted_at": str(item.get("deleted_at") or "").strip(),
                "expires_at": str(item.get("expires_at") or "").strip(),
                "reason": str(item.get("reason") or "").strip(),
            }
        )
    snapshots.sort(key=lambda value: value.get("deleted_at") or "", reverse=True)
    return snapshots


def find_deleted_task_snapshot_by_brand(config: dict[str, Any], brand_name: str) -> dict[str, Any] | None:
    target = _normalize_name(brand_name)
    if not target:
        return None
    for item in deleted_task_snapshots(config):
        if _normalize_name(item.get("brand")) == target or _normalize_name(item.get("name")) == target:
            return item
    return None


def _clean_task_for_backup(task: dict[str, Any]) -> dict[str, Any]:
    backup = copy.deepcopy(task or {})
    clear_task_delete_pending(backup)
    return backup


def _same_deleted_task(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_id = str(left.get("id") or "").strip()
    right_id = str(right.get("id") or "").strip()
    if left_id and right_id and left_id == right_id:
        return True
    left_task_id = str(left.get("task_id") or "").strip()
    right_task_id = str(right.get("task_id") or "").strip()
    if left_task_id and right_task_id and left_task_id == right_task_id:
        return True
    left_cloud_id = _safe_int(left.get("cloud_task_id"))
    right_cloud_id = _safe_int(right.get("cloud_task_id"))
    return left_cloud_id is not None and right_cloud_id is not None and left_cloud_id == right_cloud_id


def _stable_tombstone_id(task_id: str, cloud_task_id: int | None) -> str:
    if cloud_task_id is not None and cloud_task_id > 0:
        return f"cloud_{cloud_task_id}"
    if task_id:
        return f"task_{task_id}"
    return f"deleted_{uuid4().hex}"


def _find_deleted_task_index(
    items: list[dict[str, Any]],
    *,
    deleted_task_id: str = "",
    brand_name: str = "",
) -> int | None:
    target_id = str(deleted_task_id or "").strip()
    target_brand = _normalize_name(brand_name)
    for index, item in enumerate(items):
        if target_id and str(item.get("id") or "").strip() == target_id:
            return index
        if target_id and str(item.get("task_id") or "").strip() == target_id:
            return index
        if target_brand and (
            _normalize_name(item.get("brand")) == target_brand
            or _normalize_name(item.get("name")) == target_brand
        ):
            return index
    return None


def _normalize_name(value: Any) -> str:
    return str(value or "").strip().casefold()


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _safe_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
