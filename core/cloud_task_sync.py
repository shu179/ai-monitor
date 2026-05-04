from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from .cloud_client import CloudClientError, SurfacedCloudClient
from .cloud_session_store import (
    CloudSessionChangedError,
    CloudSessionStore,
    cloud_session_identity,
    cloud_session_identity_key,
)
from .daily_task_state import assign_task_id, get_task_day_status
from .history import import_records, normalize_platform_id
from .task_recycle_bin import (
    ensure_deleted_tasks,
    mark_task_delete_pending,
    purge_expired_deleted_tasks,
    retention_expires_at,
    upsert_deleted_task_backup,
)
from .time_utils import local_now


CLOUD_ACCESS_LEVELS = {"admin", "operate", "view"}
SAFE_LOCAL_TASK_CONFIG_KEYS = {
    "industry_tags",
    "region_tags",
    "weekdays",
    "inspect",
    "recognition_enabled",
    "recognition_brands",
    "recognition_batch_size",
    "extract_references_enabled",
    "fixed_screenshot_enabled",
    "fixed_screenshot_count",
    "optimization_start_date",
    "optimization_end_date",
    "notes",
}


def pull_cloud_tasks_into_config(
    config: dict[str, Any],
    *,
    client: SurfacedCloudClient | None = None,
    session_store: CloudSessionStore | None = None,
) -> dict[str, Any]:
    store = session_store or CloudSessionStore()
    session = store.load()
    initial_identity_key = cloud_session_identity_key(session)
    base_url = str(session.get("base_url") or "").strip()
    access_token = str(session.get("access_token") or "").strip()
    refresh_token = str(session.get("refresh_token") or "").strip()
    if not base_url or not access_token:
        return {"ok": False, "message": "未登录云端", "summary": _empty_summary()}

    target_client = client or SurfacedCloudClient(base_url)
    try:
        cloud_tasks, session = _list_tasks_with_refresh(
            target_client,
            store,
            session,
            base_url=base_url,
            access_token=access_token,
            refresh_token=refresh_token,
        )
        active_access_token = str(session.get("access_token") or access_token).strip()
        deleted_cloud_tasks, session = _list_deleted_tasks_best_effort(
            target_client,
            store,
            session,
            base_url=base_url,
            access_token=active_access_token,
            refresh_token=str(session.get("refresh_token") or refresh_token).strip(),
        )
        active_access_token = str(session.get("access_token") or active_access_token).strip()
        if cloud_session_identity_key(store.load()) != initial_identity_key:
            raise CloudSessionChangedError("云端账号已切换，本次拉取已中止")
    except (CloudClientError, CloudSessionChangedError) as exc:
        return {"ok": False, "message": str(exc), "summary": _empty_summary()}

    summary = merge_cloud_tasks_into_config(
        config,
        cloud_tasks,
        deleted_cloud_tasks=deleted_cloud_tasks,
        cloud_user=session.get("user") if isinstance(session.get("user"), dict) else {},
        base_url=base_url,
    )
    run_summary = pull_cloud_run_records_into_history(
        target_client,
        active_access_token,
        config,
        cloud_tasks,
        deleted_cloud_tasks=deleted_cloud_tasks,
        session_store=store,
        session=session,
        base_url=base_url,
        refresh_token=str(session.get("refresh_token") or refresh_token).strip(),
    )
    if cloud_session_identity_key(store.load()) != initial_identity_key:
        return {"ok": False, "message": "云端账号已切换，本次拉取已中止", "summary": _empty_summary()}
    summary["run_records"] = run_summary
    changed = (
        int(summary.get("added") or 0)
        + int(summary.get("updated") or 0)
        + int(summary.get("revoked") or 0)
        + int(summary.get("deleted") or 0)
        + int(summary.get("deleted_backups") or 0)
        + int(summary.get("deleted_pending") or 0)
    )
    run_changed = int(run_summary.get("imported") or 0) + int(run_summary.get("cursor_updates") or 0)
    return {
        "ok": True,
        "message": "云端任务已拉取" if changed or run_changed else "云端任务无变化",
        "summary": summary,
    }


def pull_cloud_run_records_into_history(
    client: SurfacedCloudClient,
    access_token: str,
    config: dict[str, Any],
    cloud_tasks: list[dict[str, Any]],
    *,
    deleted_cloud_tasks: list[dict[str, Any]] | None = None,
    session_store: CloudSessionStore | None = None,
    session: dict[str, Any] | None = None,
    base_url: str = "",
    refresh_token: str = "",
    limit_per_task: int = 6000,
) -> dict[str, Any]:
    if not access_token or not hasattr(client, "task_run_records"):
        return {"tasks": 0, "fetched": 0, "imported": 0, "failed": 0}
    store = session_store
    active_session = session if isinstance(session, dict) else {}
    active_access_token = str(access_token or "").strip()
    active_refresh_token = str(refresh_token or active_session.get("refresh_token") or "").strip()
    active_base_url = str(base_url or active_session.get("base_url") or "").strip()

    local_by_cloud_id: dict[int, dict[str, Any]] = {}
    for task in _iter_local_tasks_and_deleted_backups(config):
        cloud_task_id = _normalize_int(task.get("cloud_task_id") or task.get("cloudTaskId"))
        if cloud_task_id is not None and cloud_task_id not in local_by_cloud_id:
            local_by_cloud_id[cloud_task_id] = task

    summary = {"tasks": 0, "fetched": 0, "imported": 0, "failed": 0, "cursor_updates": 0}
    seen_cloud_task_ids: set[int] = set()
    for cloud_task in [*cloud_tasks, *(deleted_cloud_tasks or [])]:
        if not isinstance(cloud_task, dict):
            continue
        cloud_task_id = _normalize_int(cloud_task.get("id"))
        if cloud_task_id is None or cloud_task_id not in local_by_cloud_id:
            continue
        if cloud_task_id in seen_cloud_task_ids:
            continue
        seen_cloud_task_ids.add(cloud_task_id)
        local_task = local_by_cloud_id[cloud_task_id]
        task_name = str(local_task.get("name") or cloud_task.get("name") or cloud_task.get("brand") or "").strip()
        task_id = str(local_task.get("task_id") or "").strip()
        if not task_name:
            continue
        previous_cursor = _normalize_int(
            local_task.get("cloud_last_run_record_synced_id")
            or local_task.get("cloudLastRunRecordSyncedId")
        )
        try:
            run_records, active_session = _cloud_request_with_refresh(
                client,
                store,
                active_session,
                base_url=active_base_url,
                access_token=active_access_token,
                refresh_token=active_refresh_token,
                operation=lambda token, task_id=cloud_task_id, cursor=previous_cursor: client.task_run_records(
                    token,
                    task_id,
                    limit=limit_per_task,
                    since_id=cursor,
                ),
            )
            active_access_token = str(active_session.get("access_token") or active_access_token).strip()
            active_refresh_token = str(active_session.get("refresh_token") or active_refresh_token).strip()
        except (CloudClientError, CloudSessionChangedError):
            summary["failed"] += 1
            continue
        summary["tasks"] += 1
        summary["fetched"] += len(run_records)
        max_record_id = previous_cursor or 0
        for record in run_records:
            if isinstance(record, dict):
                record_id = _normalize_int(record.get("id"))
                if record_id is not None:
                    max_record_id = max(max_record_id, record_id)
        entries = [
            _cloud_run_record_to_history_entry(record, local_task=local_task, cloud_task_id=cloud_task_id)
            for record in run_records
            if isinstance(record, dict)
        ]
        entries = [entry for entry in entries if entry]
        summary["imported"] += import_records(task_name, entries, task_id=task_id)
        if max_record_id and max_record_id != (previous_cursor or 0):
            local_task["cloud_last_run_record_synced_id"] = max_record_id
            local_task["cloud_run_records_synced_at"] = local_now().isoformat(timespec="seconds")
            summary["cursor_updates"] += 1
    return summary


def _iter_local_tasks_and_deleted_backups(config: dict[str, Any]) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for task in config.get("tasks", []) or []:
        if isinstance(task, dict):
            tasks.append(task)
    for item in ensure_deleted_tasks(config):
        task = item.get("task") if isinstance(item.get("task"), dict) else None
        if isinstance(task, dict):
            tasks.append(task)
    return tasks


def merge_cloud_tasks_into_config(
    config: dict[str, Any],
    cloud_tasks: list[dict[str, Any]],
    *,
    deleted_cloud_tasks: list[dict[str, Any]] | None = None,
    cloud_user: dict[str, Any] | None = None,
    base_url: str = "",
    match_by_name: bool = True,
    disable_missing: bool = True,
) -> dict[str, Any]:
    purge_expired_deleted_tasks(config)
    if not isinstance(config.get("tasks"), list):
        config["tasks"] = []
    local_tasks: list[dict[str, Any]] = [item for item in config.get("tasks", []) if isinstance(item, dict)]
    config["tasks"] = local_tasks

    summary = _empty_summary()
    summary["received"] = len([item for item in cloud_tasks if isinstance(item, dict)])
    deleted_items = [item for item in (deleted_cloud_tasks or []) if isinstance(item, dict)]
    summary["deleted_received"] = len(deleted_items)
    now = local_now().isoformat(timespec="seconds")
    workspace_id = _normalize_int((cloud_user or {}).get("workspace_id"))
    deleted_by_cloud_id = {
        cloud_task_id: item
        for item in deleted_items
        if (cloud_task_id := _normalize_int(item.get("id"))) is not None
    }
    deleted_by_task_key = {
        str(item.get("task_key") or "").strip(): item
        for item in deleted_items
        if str(item.get("task_key") or "").strip()
    }

    matched_indexes: set[int] = set()
    for cloud_task in cloud_tasks:
        if not isinstance(cloud_task, dict):
            summary["skipped"] += 1
            continue
        cloud_task_id = _normalize_int(cloud_task.get("id"))
        if cloud_task_id is None:
            summary["skipped"] += 1
            continue

        index, match_reason = _find_local_task_index(local_tasks, cloud_task, match_by_name=match_by_name)
        before = copy.deepcopy(local_tasks[index]) if index is not None else None
        merged = _merge_one_cloud_task(
            cloud_task,
            existing=local_tasks[index] if index is not None else None,
            cloud_user=cloud_user or {},
            base_url=base_url,
            now=now,
        )
        if index is None:
            local_tasks.append(merged)
            matched_indexes.add(len(local_tasks) - 1)
            summary["added"] += 1
        else:
            local_tasks[index] = merged
            matched_indexes.add(index)
            if merged == before:
                summary["unchanged"] += 1
            else:
                summary["updated"] += 1
        summary["matched_by"][match_reason] = int(summary["matched_by"].get(match_reason) or 0) + 1
        summary["task_ids"].append(cloud_task_id)

    if disable_missing:
        visible_cloud_ids = set(summary["task_ids"])
        next_local_tasks: list[dict[str, Any]] = []
        tombstone_cloud_ids = _local_deleted_cloud_ids(config)
        for index, task in enumerate(local_tasks):
            keep_task = True
            if index in matched_indexes:
                next_local_tasks.append(task)
                continue
            existing_cloud_id = _normalize_int(task.get("cloud_task_id") or task.get("cloudTaskId"))
            deleted_cloud_task = _find_deleted_cloud_match(
                task,
                deleted_by_cloud_id=deleted_by_cloud_id,
                deleted_by_task_key=deleted_by_task_key,
            )
            if deleted_cloud_task is not None:
                if _is_local_task_formal_running(task):
                    mark_task_delete_pending(task, reason="cloud_deleted")
                    task["cloud_deleted_at"] = _cloud_deleted_at(deleted_cloud_task, now)
                    task["cloud_delete_expires_at"] = _cloud_delete_expires_at(deleted_cloud_task)
                    task["delete_pending_expires_at"] = task["cloud_delete_expires_at"]
                    task["cloud_synced_at"] = now
                    next_local_tasks.append(task)
                    summary["deleted_pending"] += 1
                    continue
                upsert_deleted_task_backup(
                    config,
                    task,
                    source="cloud",
                    reason="cloud_deleted",
                    deleted_at=_cloud_deleted_at(deleted_cloud_task, now),
                    expires_at=_cloud_delete_expires_at(deleted_cloud_task),
                )
                if existing_cloud_id is not None:
                    tombstone_cloud_ids.add(existing_cloud_id)
                summary["deleted"] += 1
                keep_task = False
            if not keep_task:
                continue
            if existing_cloud_id is None or existing_cloud_id in visible_cloud_ids:
                next_local_tasks.append(task)
                continue
            if not _same_cloud_scope(task, base_url=base_url, workspace_id=workspace_id):
                next_local_tasks.append(task)
                continue
            before = copy.deepcopy(task)
            task["enabled"] = False
            task["cloud_access_level"] = "revoked"
            task["cloud_revoked_at"] = now
            task["cloud_synced_at"] = now
            if task != before:
                summary["revoked"] += 1
            next_local_tasks.append(task)
        local_tasks = next_local_tasks
        config["tasks"] = local_tasks

        active_cloud_ids = {
            _normalize_int(task.get("cloud_task_id") or task.get("cloudTaskId"))
            for task in local_tasks
            if isinstance(task, dict)
        }
        for cloud_task in deleted_items:
            cloud_task_id = _normalize_int(cloud_task.get("id"))
            if cloud_task_id is None or cloud_task_id in active_cloud_ids or cloud_task_id in tombstone_cloud_ids:
                continue
            backup_task = _merge_one_cloud_task(
                cloud_task,
                existing=None,
                cloud_user=cloud_user or {},
                base_url=base_url,
                now=now,
            )
            upsert_deleted_task_backup(
                config,
                backup_task,
                source="cloud",
                reason="cloud_deleted",
                deleted_at=_cloud_deleted_at(cloud_task, now),
                expires_at=_cloud_delete_expires_at(cloud_task),
                tombstone_id=f"cloud_{cloud_task_id}",
            )
            tombstone_cloud_ids.add(cloud_task_id)
            summary["deleted_backups"] += 1

    return summary


def _list_tasks_with_refresh(
    client: SurfacedCloudClient,
    store: CloudSessionStore,
    session: dict[str, Any],
    *,
    base_url: str,
    access_token: str,
    refresh_token: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    return _cloud_request_with_refresh(
        client,
        store,
        session,
        base_url=base_url,
        access_token=access_token,
        refresh_token=refresh_token,
        operation=client.list_tasks,
    )


def _cloud_request_with_refresh(
    client: SurfacedCloudClient,
    store: CloudSessionStore | None,
    session: dict[str, Any],
    *,
    base_url: str,
    access_token: str,
    refresh_token: str,
    operation,
) -> tuple[Any, dict[str, Any]]:
    try:
        return operation(access_token), session
    except CloudClientError as exc:
        if exc.status_code != 401 or not refresh_token or store is None:
            raise

    identity = cloud_session_identity(session)
    try:
        refreshed_session = store.refresh_login_if_current(
            base_url=base_url,
            access_token=access_token,
            refresh_token=refresh_token,
            refresh=client.refresh,
            workspace_id=identity["workspace_id"],
            user_id=identity["user_id"],
        )
    except CloudClientError as refresh_exc:
        if refresh_exc.status_code == 401:
            store.clear_if_current(
                base_url=base_url,
                access_token=access_token,
                refresh_token=refresh_token,
                workspace_id=identity["workspace_id"],
                user_id=identity["user_id"],
            )
        raise refresh_exc
    refreshed_access_token = str(refreshed_session.get("access_token") or "").strip()
    if not refreshed_access_token:
        raise CloudClientError("未登录云端", status_code=401)
    try:
        return operation(refreshed_access_token), refreshed_session
    except CloudClientError as retry_exc:
        refreshed_identity = cloud_session_identity(refreshed_session)
        if retry_exc.status_code == 401:
            store.clear_if_current(
                base_url=base_url,
                access_token=refreshed_access_token,
                refresh_token=str(refreshed_session.get("refresh_token") or "").strip(),
                workspace_id=refreshed_identity["workspace_id"],
                user_id=refreshed_identity["user_id"],
            )
        raise retry_exc


def _merge_one_cloud_task(
    cloud_task: dict[str, Any],
    *,
    existing: dict[str, Any] | None,
    cloud_user: dict[str, Any],
    base_url: str,
    now: str,
) -> dict[str, Any]:
    config_json = cloud_task.get("config_json") if isinstance(cloud_task.get("config_json"), dict) else {}
    local_task_config = _extract_local_task_config(config_json)
    cloud_task_id = _normalize_int(cloud_task.get("id"))
    cloud_workspace_id = _normalize_int(cloud_task.get("workspace_id")) or _normalize_int(cloud_user.get("workspace_id"))
    access_level = _normalize_access_level(cloud_task.get("access_level"), user_role=cloud_user.get("role"))
    cloud_enabled = bool(cloud_task.get("enabled", True))

    task = copy.deepcopy(existing) if isinstance(existing, dict) else _default_local_task()
    task["name"] = str(cloud_task.get("name") or task.get("name") or cloud_task.get("brand") or "").strip()
    task["brand"] = str(cloud_task.get("brand") or task.get("brand") or task.get("name") or "").strip()
    task["enabled"] = cloud_enabled and access_level in {"admin", "operate"}

    for key in SAFE_LOCAL_TASK_CONFIG_KEYS:
        if key in local_task_config:
            task[key] = copy.deepcopy(local_task_config[key])

    platforms = _normalize_platform_list(
        config_json.get("platforms")
        or local_task_config.get("platforms")
        or task.get("platforms")
        or []
    )
    if platforms:
        task["platforms"] = platforms

    keywords = _normalize_keywords(
        config_json.get("keywords") if "keywords" in config_json else local_task_config.get("keywords"),
        brand=task.get("brand"),
        fallback_platforms=platforms,
        fallback_mode=str(config_json.get("mode") or local_task_config.get("mode") or "browser"),
        fallback_deep_think_platforms=_normalize_platform_list(
            config_json.get("deep_think_platforms") or local_task_config.get("deep_think_platforms") or []
        ),
    )
    if keywords:
        task["keywords"] = keywords

    if cloud_task_id is not None:
        task["cloud_task_id"] = cloud_task_id
        if not str(task.get("task_id") or "").strip():
            task["task_id"] = f"cloud_{cloud_task_id}"
    if cloud_workspace_id is not None:
        task["cloud_workspace_id"] = cloud_workspace_id
    task["cloud_task_key"] = str(cloud_task.get("task_key") or "").strip()
    task["cloud_access_level"] = access_level
    task["cloud_enabled"] = cloud_enabled
    task["cloud_config_version"] = _normalize_int(cloud_task.get("config_version")) or 1
    task["cloud_base_url"] = str(base_url or "").strip().rstrip("/")
    task["cloud_synced_at"] = now
    if existing is None and not str(task.get("created_at") or "").strip():
        task["created_at"] = str(cloud_task.get("created_at") or "").strip() or now
    if access_level != "revoked":
        task.pop("cloud_revoked_at", None)

    if not str(task.get("task_id") or "").strip():
        assign_task_id(task)
    return task


def _find_local_task_index(
    local_tasks: list[dict[str, Any]],
    cloud_task: dict[str, Any],
    *,
    match_by_name: bool,
) -> tuple[int | None, str]:
    cloud_task_id = _normalize_int(cloud_task.get("id"))
    cloud_task_key = str(cloud_task.get("task_key") or "").strip()
    for index, task in enumerate(local_tasks):
        if _normalize_int(task.get("cloud_task_id") or task.get("cloudTaskId")) == cloud_task_id:
            return index, "cloud_task_id"
    if cloud_task_key:
        for index, task in enumerate(local_tasks):
            if str(task.get("cloud_task_key") or task.get("cloudTaskKey") or "").strip() == cloud_task_key:
                return index, "cloud_task_key"
    if match_by_name:
        target = (str(cloud_task.get("name") or "").strip(), str(cloud_task.get("brand") or "").strip())
        if all(target):
            candidates = [
                index
                for index, task in enumerate(local_tasks)
                if not _normalize_int(task.get("cloud_task_id") or task.get("cloudTaskId"))
                and not str(task.get("cloud_task_key") or task.get("cloudTaskKey") or "").strip()
                and (str(task.get("name") or "").strip(), str(task.get("brand") or "").strip()) == target
            ]
            if len(candidates) == 1:
                return candidates[0], "name_brand"
    return None, "new"


def _extract_local_task_config(config_json: dict[str, Any]) -> dict[str, Any]:
    for key in ("local_task", "localTask", "task"):
        value = config_json.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _default_local_task() -> dict[str, Any]:
    return {
        "industry_tags": [],
        "region_tags": [],
        "keywords": [],
        "webhook_url": "",
        "weekdays": list(range(7)),
        "enabled": False,
        "inspect": False,
        "recognition_enabled": False,
        "recognition_brands": "",
        "recognition_batch_size": 1,
        "optimization_start_date": "",
        "optimization_end_date": "",
    }


def _cloud_run_record_to_history_entry(
    record: dict[str, Any],
    *,
    local_task: dict[str, Any],
    cloud_task_id: int,
) -> dict[str, Any]:
    result = record.get("result_json") if isinstance(record.get("result_json"), dict) else {}
    idempotency_key = str(record.get("idempotency_key") or "").strip()
    record_id = str(record.get("id") or "").strip()
    stable_id = idempotency_key or f"run-record-{record_id or _cloud_run_record_fingerprint(record, cloud_task_id)}"
    rank = _normalize_int(result.get("rank"))
    rank_value = rank if rank is not None else 99
    extra = {
        "cloud_task_id": cloud_task_id,
        "cloud_run_record_id": record_id,
        "cloud_idempotency_key": idempotency_key,
        "reference_count": _normalize_int(result.get("reference_count")) or 0,
        "body_reference_count": _normalize_int(result.get("body_reference_count")) or 0,
        "total_reference_count": _normalize_int(result.get("total_reference_count")) or 0,
    }
    return {
        "id": f"cloud:{stable_id}",
        "ts": _cloud_history_timestamp(record.get("executed_at")),
        "task_id": str(local_task.get("task_id") or "").strip(),
        "task_name": str(local_task.get("name") or record.get("task_name") or "").strip(),
        "platform": normalize_platform_id(str(record.get("platform") or "").strip()),
        "keyword": str(record.get("keyword") or "").strip(),
        "brand": str(record.get("brand") or local_task.get("brand") or "").strip(),
        "rank": rank_value,
        "success": bool(result.get("success")) and rank_value != 99,
        "review_status": str(result.get("review_status") or "").strip(),
        "review_note": "",
        "reviewed_at": "",
        "screenshot": "",
        "highlight_count": _normalize_int(result.get("highlight_count")) or 0,
        "answer_text": "",
        "evidence": "",
        "error_message": str(result.get("error_message") or "").strip(),
        "diagnostic_id": str(result.get("diagnostic_id") or "").strip(),
        "mode": str(record.get("mode") or "browser").strip(),
        "execution_source": "cloud",
        "extra": extra,
    }


def _cloud_history_timestamp(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return local_now().strftime("%Y-%m-%d %H:%M")
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(local_now().tzinfo)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return text[:16]


def _cloud_run_record_fingerprint(record: dict[str, Any], cloud_task_id: int) -> str:
    parts = [
        str(cloud_task_id),
        str(record.get("executed_at") or ""),
        str(record.get("platform") or ""),
        str(record.get("keyword") or ""),
        str(record.get("brand") or ""),
    ]
    return "|".join(parts)


def _normalize_keywords(
    raw_keywords: Any,
    *,
    brand: Any,
    fallback_platforms: list[str],
    fallback_mode: str,
    fallback_deep_think_platforms: list[str],
) -> list[dict[str, Any]]:
    if not isinstance(raw_keywords, list):
        return []
    normalized: list[dict[str, Any]] = []
    safe_mode = fallback_mode if fallback_mode in {"browser", "recognition", "api", "smart"} else "browser"
    for item in raw_keywords:
        if isinstance(item, dict):
            keyword = str(item.get("keyword") or item.get("name") or "").strip()
            keyword_brand = str(item.get("brand") or brand or "").strip()
            platforms = _normalize_platform_list(item.get("platforms")) or list(fallback_platforms)
            mode = str(item.get("mode") or safe_mode).strip()
            if mode not in {"browser", "recognition", "api", "smart"}:
                mode = safe_mode
            deep_think = _normalize_deep_think(item.get("deep_think"), platforms)
            if not deep_think:
                deep_think_platforms = _normalize_platform_list(item.get("deep_think_platforms")) or fallback_deep_think_platforms
                deep_think = {platform: True for platform in deep_think_platforms if platform in platforms}
        else:
            keyword = str(item or "").strip()
            keyword_brand = str(brand or "").strip()
            platforms = list(fallback_platforms)
            mode = safe_mode
            deep_think = {platform: True for platform in fallback_deep_think_platforms if platform in platforms}
        if not keyword:
            continue
        entry: dict[str, Any] = {
            "keyword": keyword,
            "brand": keyword_brand,
            "platforms": platforms,
            "mode": mode,
        }
        if deep_think:
            entry["deep_think"] = deep_think
        normalized.append(entry)
    return normalized


def _normalize_platform_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    platforms: list[str] = []
    for item in value:
        platform = normalize_platform_id(str(item or "").strip())
        if platform and platform not in platforms:
            platforms.append(platform)
    return platforms


def _normalize_deep_think(value: Any, platforms: list[str]) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    platform_set = set(platforms)
    return {
        normalize_platform_id(str(platform or "").strip()): bool(enabled)
        for platform, enabled in value.items()
        if normalize_platform_id(str(platform or "").strip()) in platform_set and bool(enabled)
    }


def _normalize_access_level(value: Any, *, user_role: Any = None) -> str:
    text = str(value or "").strip().lower()
    if text in CLOUD_ACCESS_LEVELS:
        return text
    role = str(user_role or "").strip().lower()
    return "admin" if role == "admin" else "view"


def _same_cloud_scope(task: dict[str, Any], *, base_url: str, workspace_id: int | None) -> bool:
    task_base_url = str(task.get("cloud_base_url") or "").strip().rstrip("/")
    if base_url and task_base_url and task_base_url != base_url.rstrip("/"):
        return False
    if workspace_id is not None:
        task_workspace_id = _normalize_int(task.get("cloud_workspace_id"))
        if task_workspace_id is not None and task_workspace_id != workspace_id:
            return False
    return True


def _normalize_int(value: Any) -> int | None:
    try:
        result = int(str(value or "").strip())
    except Exception:
        return None
    return result if result > 0 else None


def _empty_summary() -> dict[str, Any]:
    return {
        "received": 0,
        "deleted_received": 0,
        "added": 0,
        "updated": 0,
        "unchanged": 0,
        "revoked": 0,
        "deleted": 0,
        "deleted_backups": 0,
        "deleted_pending": 0,
        "skipped": 0,
        "matched_by": {},
        "task_ids": [],
    }


def _list_deleted_tasks_best_effort(
    client: SurfacedCloudClient,
    store: CloudSessionStore,
    session: dict[str, Any],
    *,
    base_url: str,
    access_token: str,
    refresh_token: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not access_token or not hasattr(client, "list_deleted_tasks"):
        return [], session
    try:
        deleted, next_session = _cloud_request_with_refresh(
            client,
            store,
            session,
            base_url=base_url,
            access_token=access_token,
            refresh_token=refresh_token,
            operation=client.list_deleted_tasks,
        )
        return deleted if isinstance(deleted, list) else [], next_session
    except (CloudClientError, CloudSessionChangedError):
        return [], session
    except Exception:
        return [], session


def _local_deleted_cloud_ids(config: dict[str, Any]) -> set[int]:
    ids: set[int] = set()
    for item in ensure_deleted_tasks(config):
        cloud_task_id = _normalize_int(item.get("cloud_task_id"))
        if cloud_task_id is not None:
            ids.add(cloud_task_id)
    return ids


def _find_deleted_cloud_match(
    task: dict[str, Any],
    *,
    deleted_by_cloud_id: dict[int, dict[str, Any]],
    deleted_by_task_key: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    cloud_task_id = _normalize_int(task.get("cloud_task_id") or task.get("cloudTaskId"))
    if cloud_task_id is not None and cloud_task_id in deleted_by_cloud_id:
        return deleted_by_cloud_id[cloud_task_id]
    cloud_task_key = str(task.get("cloud_task_key") or task.get("cloudTaskKey") or "").strip()
    if cloud_task_key and cloud_task_key in deleted_by_task_key:
        return deleted_by_task_key[cloud_task_key]
    return None


def _is_local_task_formal_running(task: dict[str, Any]) -> bool:
    try:
        status = get_task_day_status(task)
        return bool(status.get("formal_running"))
    except Exception:
        return False


def _cloud_deleted_at(task: dict[str, Any], fallback: str) -> str:
    value = str(task.get("deleted_at") or task.get("deletedAt") or "").strip()
    return value or fallback


def _cloud_delete_expires_at(task: dict[str, Any]) -> str:
    value = str(task.get("delete_expires_at") or task.get("deleteExpiresAt") or "").strip()
    return value or retention_expires_at(local_now())
