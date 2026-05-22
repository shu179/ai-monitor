from __future__ import annotations

import copy
import time
from datetime import date, datetime
from typing import Any

from .cloud_client import CloudClientError, SurfacedCloudClient
from .diagnostic_events import record_event_safe
from .cloud_event_types import (
    EVENT_REFERENCE_CHANGED,
    EVENT_RUN_RECORD_CHANGED,
    EVENT_TASK_DAY_STATUS_CHANGED,
)
from .cloud_session_store import (
    CloudSessionChangedError,
    CloudSessionStore,
    cloud_session_identity,
    cloud_session_identity_key,
)
from .daily_task_state import (
    SOURCE_MODE_FORMAL,
    apply_task_keyword_updates,
    assign_task_id,
    build_task_state_extra,
    get_task_day_status,
    mark_task_success,
    write_task_status,
)
from .history import get_task_daily_success_bundle, import_records, normalize_platform_id
from .task_recycle_bin import (
    ensure_deleted_tasks,
    mark_task_delete_pending,
    purge_expired_deleted_tasks,
    retention_expires_at,
    upsert_deleted_task_backup,
)
from .time_utils import local_now, local_today


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
CLOUD_PLATFORM_SYNC_STATE_KEY = "cloud_platform_sync"


def pull_cloud_tasks_into_config(
    config: dict[str, Any],
    *,
    client: SurfacedCloudClient | None = None,
    session_store: CloudSessionStore | None = None,
    force_full: bool = False,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    metrics: dict[str, Any] = {
        "mode": "full",
        "changes_ms": 0,
        "task_pull_ms": 0,
        "run_pull_ms": 0,
        "task_day_status_pull_ms": 0,
    }
    store = session_store or CloudSessionStore()
    session = store.load()
    initial_identity_key = cloud_session_identity_key(session)
    base_url = str(session.get("base_url") or "").strip()
    access_token = str(session.get("access_token") or "").strip()
    refresh_token = str(session.get("refresh_token") or "").strip()
    if not base_url or not access_token:
        return {"ok": False, "message": "未登录云端", "summary": _empty_summary()}

    target_client = client or SurfacedCloudClient(base_url)
    changes = None
    task_cursors = _cloud_task_run_cursors(config)
    task_day_status_cursors = _cloud_task_day_status_cursors(config)
    if hasattr(target_client, "sync_changes"):
        changes_started_at = time.perf_counter()
        try:
            changes, session = _cloud_request_with_refresh(
                target_client,
                store,
                session,
                base_url=base_url,
                access_token=access_token,
                refresh_token=refresh_token,
                operation=lambda token: target_client.sync_changes(
                    token,
                    known_snapshot=_cloud_known_snapshot(config),
                    task_cursors=task_cursors,
                    task_day_status_cursors=task_day_status_cursors,
                ),
            )
            access_token = str(session.get("access_token") or access_token).strip()
            refresh_token = str(session.get("refresh_token") or refresh_token).strip()
            metrics["changes_ms"] = _elapsed_ms(changes_started_at)
        except (CloudClientError, CloudSessionChangedError) as exc:
            changes = None
            metrics["changes_ms"] = _elapsed_ms(changes_started_at)
            metrics["changes_error"] = str(exc)

    change_run_task_ids = _normalize_int_list(changes.get("run_record_task_ids")) if isinstance(changes, dict) else []
    change_status_task_ids = _normalize_int_list(changes.get("task_day_status_task_ids")) if isinstance(changes, dict) else []
    changed_task_ids = _normalize_int_list(changes.get("changed_task_ids")) if isinstance(changes, dict) else []
    change_events = _normalize_event_names(changes.get("events")) if isinstance(changes, dict) else []
    run_change_without_task_ids = EVENT_RUN_RECORD_CHANGED in change_events and not change_run_task_ids
    status_change_without_task_ids = EVENT_TASK_DAY_STATUS_CHANGED in change_events and not change_status_task_ids

    if (
        not force_full
        and
        isinstance(changes, dict)
        and not bool(changes.get("full_task_pull_required"))
        and not run_change_without_task_ids
        and not status_change_without_task_ids
    ):
        metrics["mode"] = "changes"
        summary = _empty_summary()
        summary["changes"] = _summarize_changes(changes)
        run_task_ids = change_run_task_ids
        run_started_at = time.perf_counter()
        run_summary = pull_cloud_run_records_into_history(
            target_client,
            access_token,
            config,
            [{"id": task_id} for task_id in run_task_ids],
            deleted_cloud_tasks=[],
            session_store=store,
            session=session,
            base_url=base_url,
            refresh_token=refresh_token,
        ) if run_task_ids else _empty_run_summary()
        metrics["run_pull_ms"] = _elapsed_ms(run_started_at)
        status_started_at = time.perf_counter()
        status_summary = pull_cloud_task_day_status_events_into_state(
            target_client,
            access_token,
            config,
            [{"id": task_id} for task_id in change_status_task_ids],
            session_store=store,
            session=session,
            base_url=base_url,
            refresh_token=refresh_token,
        ) if change_status_task_ids else _empty_task_day_status_summary()
        metrics["task_day_status_pull_ms"] = _elapsed_ms(status_started_at)
        if cloud_session_identity_key(store.load()) != initial_identity_key:
            return {"ok": False, "message": "云端账号已切换，本次拉取已中止", "summary": _empty_summary()}
        summary["run_records"] = run_summary
        summary["task_day_status_events"] = status_summary
        if int(run_summary.get("state_updated") or 0):
            summary["state_updated"] = int(summary.get("state_updated") or 0) + int(run_summary.get("state_updated") or 0)
        if int(status_summary.get("state_updated") or 0):
            summary["state_updated"] = int(summary.get("state_updated") or 0) + int(status_summary.get("state_updated") or 0)
        cached_state_updates = _refresh_visible_cloud_task_day_statuses_from_history(config)
        if cached_state_updates:
            summary["state_updated"] = int(summary.get("state_updated") or 0) + cached_state_updates
        _update_cloud_sync_snapshot(config, changes.get("snapshot"), summary)
        metrics["total_ms"] = _elapsed_ms(started_at)
        metrics["run_record_task_ids"] = run_task_ids
        metrics["task_day_status_task_ids"] = change_status_task_ids
        summary["metrics"] = metrics
        run_changed = (
            int(run_summary.get("imported") or 0)
            + int(run_summary.get("cursor_updates") or 0)
            + int(run_summary.get("backfilled") or 0)
        )
        status_changed = (
            int(status_summary.get("applied") or 0)
            + int(status_summary.get("cursor_updates") or 0)
        )
        state_changed = int(summary.get("state_updated") or 0)
        return {
            "ok": True,
            "message": "云端任务已拉取" if run_changed or status_changed or state_changed else "云端任务无变化",
            "summary": summary,
        }

    can_pull_changed_tasks = (
        not force_full
        and isinstance(changes, dict)
        and bool(changes.get("full_task_pull_required"))
        and bool(changed_task_ids)
        and hasattr(target_client, "list_tasks_by_ids")
    )
    if can_pull_changed_tasks:
        active_access_token = access_token
        try:
            task_pull_started_at = time.perf_counter()
            cloud_tasks, session = _list_tasks_by_ids_with_refresh(
                target_client,
                store,
                session,
                task_ids=changed_task_ids,
                base_url=base_url,
                access_token=active_access_token,
                refresh_token=refresh_token,
            )
            active_access_token = str(session.get("access_token") or active_access_token).strip()
            deleted_cloud_tasks, session = _list_deleted_tasks_best_effort(
                target_client,
                store,
                session,
                base_url=base_url,
                access_token=active_access_token,
                refresh_token=str(session.get("refresh_token") or refresh_token).strip(),
            )
            metrics["task_pull_ms"] = _elapsed_ms(task_pull_started_at)
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
            disable_missing=False,
            missing_cloud_task_ids=changed_task_ids,
        )
        summary["changes"] = _summarize_changes(changes)
        run_pull_task_ids = _unique_ints([*changed_task_ids, *change_run_task_ids])
        status_pull_task_ids = _unique_ints([*changed_task_ids, *change_status_task_ids])
        run_started_at = time.perf_counter()
        run_summary = pull_cloud_run_records_into_history(
            target_client,
            active_access_token,
            config,
            [{"id": task_id} for task_id in run_pull_task_ids],
            deleted_cloud_tasks=deleted_cloud_tasks,
            session_store=store,
            session=session,
            base_url=base_url,
            refresh_token=str(session.get("refresh_token") or refresh_token).strip(),
        ) if run_pull_task_ids else _empty_run_summary()
        metrics["run_pull_ms"] = _elapsed_ms(run_started_at)
        status_started_at = time.perf_counter()
        status_summary = pull_cloud_task_day_status_events_into_state(
            target_client,
            active_access_token,
            config,
            [{"id": task_id} for task_id in status_pull_task_ids],
            session_store=store,
            session=session,
            base_url=base_url,
            refresh_token=str(session.get("refresh_token") or refresh_token).strip(),
        ) if status_pull_task_ids else _empty_task_day_status_summary()
        metrics["task_day_status_pull_ms"] = _elapsed_ms(status_started_at)
        if cloud_session_identity_key(store.load()) != initial_identity_key:
            return {"ok": False, "message": "云端账号已切换，本次拉取已中止", "summary": _empty_summary()}
        summary["run_records"] = run_summary
        summary["task_day_status_events"] = status_summary
        if int(run_summary.get("state_updated") or 0):
            summary["state_updated"] = int(summary.get("state_updated") or 0) + int(run_summary.get("state_updated") or 0)
        if int(status_summary.get("state_updated") or 0):
            summary["state_updated"] = int(summary.get("state_updated") or 0) + int(status_summary.get("state_updated") or 0)
        cached_state_updates = _refresh_visible_cloud_task_day_statuses_from_history(config)
        if cached_state_updates:
            summary["state_updated"] = int(summary.get("state_updated") or 0) + cached_state_updates
        _update_cloud_sync_snapshot(config, changes.get("snapshot"), summary)
        metrics["mode"] = "partial_tasks"
        metrics["total_ms"] = _elapsed_ms(started_at)
        metrics["changed_task_ids"] = changed_task_ids
        metrics["run_record_task_ids"] = run_pull_task_ids
        metrics["task_day_status_task_ids"] = status_pull_task_ids
        metrics["received_tasks"] = int(summary.get("received") or 0)
        metrics["deleted_tasks"] = int(summary.get("deleted") or 0)
        summary["metrics"] = metrics
        changed = (
            int(summary.get("added") or 0)
            + int(summary.get("updated") or 0)
            + int(summary.get("revoked") or 0)
            + int(summary.get("deleted") or 0)
            + int(summary.get("deleted_backups") or 0)
            + int(summary.get("deleted_pending") or 0)
            + int(summary.get("state_updated") or 0)
        )
        run_changed = (
            int(run_summary.get("imported") or 0)
            + int(run_summary.get("cursor_updates") or 0)
            + int(run_summary.get("backfilled") or 0)
        )
        status_changed = (
            int(status_summary.get("applied") or 0)
            + int(status_summary.get("cursor_updates") or 0)
        )
        return {
            "ok": True,
            "message": "云端任务已拉取" if changed or run_changed or status_changed else "云端任务无变化",
            "summary": summary,
        }

    try:
        task_pull_started_at = time.perf_counter()
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
        metrics["task_pull_ms"] = _elapsed_ms(task_pull_started_at)
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
    run_started_at = time.perf_counter()
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
    metrics["run_pull_ms"] = _elapsed_ms(run_started_at)
    status_started_at = time.perf_counter()
    status_summary = pull_cloud_task_day_status_events_into_state(
        target_client,
        active_access_token,
        config,
        cloud_tasks,
        session_store=store,
        session=session,
        base_url=base_url,
        refresh_token=str(session.get("refresh_token") or refresh_token).strip(),
    )
    metrics["task_day_status_pull_ms"] = _elapsed_ms(status_started_at)
    if cloud_session_identity_key(store.load()) != initial_identity_key:
        return {"ok": False, "message": "云端账号已切换，本次拉取已中止", "summary": _empty_summary()}
    summary["run_records"] = run_summary
    summary["task_day_status_events"] = status_summary
    if int(run_summary.get("state_updated") or 0):
        summary["state_updated"] = int(summary.get("state_updated") or 0) + int(run_summary.get("state_updated") or 0)
    if int(status_summary.get("state_updated") or 0):
        summary["state_updated"] = int(summary.get("state_updated") or 0) + int(status_summary.get("state_updated") or 0)
    cached_state_updates = _refresh_visible_cloud_task_day_statuses_from_history(config)
    if cached_state_updates:
        summary["state_updated"] = int(summary.get("state_updated") or 0) + cached_state_updates
    if isinstance(changes, dict):
        summary["changes"] = _summarize_changes(changes)
        _update_cloud_sync_snapshot(config, changes.get("snapshot"), summary)
    metrics["total_ms"] = _elapsed_ms(started_at)
    metrics["received_tasks"] = int(summary.get("received") or 0)
    metrics["deleted_tasks"] = int(summary.get("deleted") or 0)
    summary["metrics"] = metrics
    changed = (
        int(summary.get("added") or 0)
        + int(summary.get("updated") or 0)
        + int(summary.get("revoked") or 0)
        + int(summary.get("deleted") or 0)
        + int(summary.get("deleted_backups") or 0)
        + int(summary.get("deleted_pending") or 0)
        + int(summary.get("state_updated") or 0)
    )
    run_changed = (
        int(run_summary.get("imported") or 0)
        + int(run_summary.get("cursor_updates") or 0)
        + int(run_summary.get("backfilled") or 0)
    )
    status_changed = (
        int(status_summary.get("applied") or 0)
        + int(status_summary.get("cursor_updates") or 0)
    )
    return {
        "ok": True,
        "message": "云端任务已拉取" if changed or run_changed or status_changed else "云端任务无变化",
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
    started_at = time.perf_counter()
    if not access_token or not hasattr(client, "task_run_records"):
        summary = _empty_run_summary()
        summary["duration_ms"] = _elapsed_ms(started_at)
        return summary
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

    summary = _empty_run_summary()
    summary["mode"] = "none"
    summary["request_count"] = 0
    seen_cloud_task_ids: set[int] = set()
    task_pairs: list[tuple[int, dict[str, Any], dict[str, Any], int | None, int | None, bool]] = []
    deleted_cloud_task_ids = {
        cloud_task_id
        for item in (deleted_cloud_tasks or [])
        if isinstance(item, dict) and (cloud_task_id := _normalize_int(item.get("id"))) is not None
    }
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
        if not task_name:
            continue
        previous_cursor = _normalize_int(
            local_task.get("cloud_last_run_record_synced_id")
            or local_task.get("cloudLastRunRecordSyncedId")
        )
        needs_backfill = (
            False
            if cloud_task_id in deleted_cloud_task_ids
            else _needs_cloud_run_record_backfill(local_task, previous_cursor)
        )
        request_cursor = 0 if needs_backfill else previous_cursor
        task_pairs.append((cloud_task_id, local_task, cloud_task, previous_cursor, request_cursor, needs_backfill))

    if not task_pairs:
        summary["duration_ms"] = _elapsed_ms(started_at)
        return summary

    if hasattr(client, "task_run_records_batch"):
        task_cursors = {
            cloud_task_id: request_cursor or 0
            for cloud_task_id, _local_task, _cloud_task, _previous_cursor, request_cursor, _needs_backfill in task_pairs
        }
        try:
            batch_records, active_session = _cloud_request_with_refresh(
                client,
                store,
                active_session,
                base_url=active_base_url,
                access_token=active_access_token,
                refresh_token=active_refresh_token,
                operation=lambda token: client.task_run_records_batch(
                    token,
                    task_cursors,
                    limit_per_task=limit_per_task,
                ),
            )
            active_access_token = str(active_session.get("access_token") or active_access_token).strip()
            active_refresh_token = str(active_session.get("refresh_token") or active_refresh_token).strip()
            if isinstance(batch_records, dict):
                summary["mode"] = "batch"
                summary["request_count"] = 1
                for cloud_task_id, local_task, cloud_task, previous_cursor, _request_cursor, needs_backfill in task_pairs:
                    run_records = batch_records.get(cloud_task_id, [])
                    _merge_cloud_run_records_for_task(
                        summary,
                        local_task=local_task,
                        cloud_task=cloud_task,
                        cloud_task_id=cloud_task_id,
                        previous_cursor=previous_cursor,
                        backfill=needs_backfill,
                        run_records=run_records if isinstance(run_records, list) else [],
                    )
                summary["duration_ms"] = _elapsed_ms(started_at)
                return summary
        except (CloudClientError, CloudSessionChangedError):
            summary["batch_error"] = "批量运行记录接口不可用，已回退逐任务拉取"

    summary["mode"] = "per_task"
    for cloud_task_id, local_task, cloud_task, previous_cursor, request_cursor, needs_backfill in task_pairs:
        try:
            run_records, active_session = _cloud_request_with_refresh(
                client,
                store,
                active_session,
                base_url=active_base_url,
                access_token=active_access_token,
                refresh_token=active_refresh_token,
                operation=lambda token, task_id=cloud_task_id, cursor=request_cursor: client.task_run_records(
                    token,
                    task_id,
                    limit=limit_per_task,
                    since_id=cursor,
                ),
            )
            active_access_token = str(active_session.get("access_token") or active_access_token).strip()
            active_refresh_token = str(active_session.get("refresh_token") or active_refresh_token).strip()
            summary["request_count"] += 1
        except (CloudClientError, CloudSessionChangedError):
            summary["failed"] += 1
            continue
        _merge_cloud_run_records_for_task(
            summary,
            local_task=local_task,
            cloud_task=cloud_task,
            cloud_task_id=cloud_task_id,
            previous_cursor=previous_cursor,
            backfill=needs_backfill,
            run_records=run_records,
        )
    summary["duration_ms"] = _elapsed_ms(started_at)
    return summary


def pull_cloud_task_day_status_events_into_state(
    client: SurfacedCloudClient,
    access_token: str,
    config: dict[str, Any],
    cloud_tasks: list[dict[str, Any]],
    *,
    session_store: CloudSessionStore | None = None,
    session: dict[str, Any] | None = None,
    base_url: str = "",
    refresh_token: str = "",
    limit_per_task: int = 500,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    summary = _empty_task_day_status_summary()
    if not access_token or not hasattr(client, "task_day_status_events_batch"):
        summary["duration_ms"] = _elapsed_ms(started_at)
        return summary

    local_by_cloud_id: dict[int, dict[str, Any]] = {}
    for task in _iter_local_tasks_and_deleted_backups(config):
        cloud_task_id = _normalize_int(task.get("cloud_task_id") or task.get("cloudTaskId"))
        if cloud_task_id is not None and cloud_task_id not in local_by_cloud_id:
            local_by_cloud_id[cloud_task_id] = task

    task_cursors: dict[int, int] = {}
    for cloud_task in cloud_tasks or []:
        if not isinstance(cloud_task, dict):
            continue
        cloud_task_id = _normalize_int(cloud_task.get("id"))
        if cloud_task_id is None or cloud_task_id not in local_by_cloud_id:
            continue
        cursor = _normalize_int(
            local_by_cloud_id[cloud_task_id].get("cloud_last_task_day_status_synced_id")
            or local_by_cloud_id[cloud_task_id].get("cloudLastTaskDayStatusSyncedId")
        )
        task_cursors[cloud_task_id] = cursor or 0

    if not task_cursors:
        summary["duration_ms"] = _elapsed_ms(started_at)
        return summary

    store = session_store
    active_session = session if isinstance(session, dict) else {}
    active_access_token = str(access_token or "").strip()
    active_refresh_token = str(refresh_token or active_session.get("refresh_token") or "").strip()
    active_base_url = str(base_url or active_session.get("base_url") or "").strip()
    try:
        events_by_task, _active_session = _cloud_request_with_refresh(
            client,
            store,
            active_session,
            base_url=active_base_url,
            access_token=active_access_token,
            refresh_token=active_refresh_token,
            operation=lambda token: client.task_day_status_events_batch(
                token,
                task_cursors,
                limit_per_task=limit_per_task,
            ),
        )
    except (CloudClientError, CloudSessionChangedError):
        summary["failed"] = len(task_cursors)
        summary["duration_ms"] = _elapsed_ms(started_at)
        return summary

    if not isinstance(events_by_task, dict):
        summary["duration_ms"] = _elapsed_ms(started_at)
        return summary

    for cloud_task_id, previous_cursor in task_cursors.items():
        local_task = local_by_cloud_id.get(cloud_task_id)
        if not local_task:
            continue
        raw_events = events_by_task.get(cloud_task_id, [])
        if not isinstance(raw_events, list):
            raw_events = []
        summary["tasks"] += 1
        summary["fetched"] += len(raw_events)
        max_event_id = previous_cursor
        for event in raw_events:
            if not isinstance(event, dict):
                continue
            event_id = _normalize_int(event.get("id"))
            if event_id is not None:
                max_event_id = max(max_event_id, event_id)
            if _apply_cloud_task_day_status_event(local_task, event):
                summary["applied"] += 1
                summary["state_updated"] += 1
        if max_event_id and max_event_id != previous_cursor:
            local_task["cloud_last_task_day_status_synced_id"] = max_event_id
            local_task["cloud_task_day_status_synced_at"] = local_now().isoformat(timespec="seconds")
            summary["cursor_updates"] += 1

    summary["duration_ms"] = _elapsed_ms(started_at)
    return summary


def _apply_cloud_task_day_status_event(local_task: dict[str, Any], event: dict[str, Any]) -> bool:
    status = str(event.get("status") or "").strip()
    if status not in {"success", "sent"}:
        return False
    target_date = _task_day_status_event_date(event)
    if target_date is None:
        return False
    image_count = _normalize_int(event.get("actual_screenshot_count")) or _normalize_int(event.get("image_count")) or 0
    extra = build_task_state_extra(
        brands=_normalize_string_list(event.get("brands")),
        completed_keywords=_normalize_string_list(event.get("completed_keywords")),
        detected_platforms=_normalize_string_list(event.get("detected_platforms")),
        supplemented_keywords=_normalize_string_list(event.get("supplemented_keywords")),
        image_count=image_count,
        task_failure_kind="",
        notification_success=bool(event.get("notification_success", True)),
        forced_ignore_failure=bool(event.get("forced_ignore_failure", True)),
        extra={
            "actual_screenshot_count": image_count,
            "cloud_task_day_status_event_id": _normalize_int(event.get("id")) or 0,
            "cloud_task_day_status_synced": True,
        },
    )
    source = str(event.get("source") or "").strip() or "cloud_task_day_status"
    message = str(event.get("message") or "").strip() or "云端人工发送状态已同步"
    write_task_status(
        local_task,
        status="success",
        source=source,
        message=message,
        extra=extra,
        target_date=target_date,
    )
    return True


def _task_day_status_event_date(event: dict[str, Any]) -> date | None:
    text = str(event.get("task_day") or event.get("taskDay") or event.get("date") or "").strip()[:10]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except Exception:
        return None


def _merge_cloud_run_records_for_task(
    summary: dict[str, int],
    *,
    local_task: dict[str, Any],
    cloud_task: dict[str, Any],
    cloud_task_id: int,
    previous_cursor: int | None,
    backfill: bool,
    run_records: list[dict[str, Any]],
) -> None:
    task_name = str(local_task.get("name") or cloud_task.get("name") or cloud_task.get("brand") or "").strip()
    task_id = str(local_task.get("task_id") or "").strip()
    if not task_name:
        return
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
    imported = import_records(task_name, entries, task_id=task_id)
    summary["imported"] += imported
    if _refresh_cloud_task_day_status_from_history(local_task, entries):
        summary["state_updated"] = int(summary.get("state_updated") or 0) + 1
    if max_record_id and max_record_id != (previous_cursor or 0):
        local_task["cloud_last_run_record_synced_id"] = max_record_id
        local_task["cloud_run_records_synced_at"] = local_now().isoformat(timespec="seconds")
        summary["cursor_updates"] += 1
    if backfill:
        local_task["cloud_run_record_backfill_date"] = local_today().isoformat()
        summary["backfilled"] = int(summary.get("backfilled") or 0) + 1


def _refresh_cloud_task_day_status_from_history(local_task: dict[str, Any], entries: list[dict[str, Any]]) -> bool:
    task_name = str(local_task.get("name") or "").strip()
    task_id = str(local_task.get("task_id") or "").strip()
    if not task_name:
        return False

    target_dates = {_entry_local_date(entry.get("ts")) for entry in entries if isinstance(entry, dict)}
    target_dates = {item for item in target_dates if item is not None}
    target_dates.add(local_today())
    changed = False
    for target_date in sorted(target_dates):
        success_bundle = get_task_daily_success_bundle(task_name, target_date=target_date, task_id=task_id)
        if not _bundle_has_success(success_bundle):
            continue

        required_query_keys = _required_query_keys(local_task)
        completed_query_keys = _normalize_query_key_set(success_bundle.get("completed_query_keys"))
        if required_query_keys and not required_query_keys.issubset(completed_query_keys):
            continue

        current_status = get_task_day_status(local_task, target_date)
        if str(current_status.get("brand_status") or "").strip() in {"success", "sent"}:
            continue

        keyword_updates = _cloud_success_keyword_updates(success_bundle, local_task)
        if keyword_updates:
            apply_task_keyword_updates(
                local_task,
                keyword_updates,
                source_mode=SOURCE_MODE_FORMAL,
                target_date=target_date,
            )
        detected_platforms = _normalize_string_list(
            list(success_bundle.get("query_detected_platforms") or [])
            + list(success_bundle.get("detected_platforms") or [])
        )
        completed_keywords = _normalize_string_list(success_bundle.get("completed_keywords"))
        extra = build_task_state_extra(
            brands=_normalize_string_list(success_bundle.get("brands")) or [str(local_task.get("brand") or task_name).strip()],
            completed_keywords=completed_keywords,
            detected_platforms=detected_platforms,
            found_results=len(success_bundle.get("query_results") or []),
            query_round_status="success",
            task_status="success",
            notification_success=False,
        )
        mark_task_success(
            local_task,
            source="cloud_sync",
            message="云端运行数据已同步",
            extra=extra,
            target_date=target_date,
        )
        changed = True
    return changed


def _refresh_visible_cloud_task_day_statuses_from_history(config: dict[str, Any]) -> int:
    updated = 0
    for task in config.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        if _normalize_int(task.get("cloud_task_id") or task.get("cloudTaskId")) is None:
            continue
        if _refresh_cloud_task_day_status_from_history(task, []):
            updated += 1
    return updated


def refresh_visible_cloud_task_day_statuses_from_history(config: dict[str, Any]) -> int:
    """Repair visible cloud task card state from already-imported run history."""
    return _refresh_visible_cloud_task_day_statuses_from_history(config)


def _needs_cloud_run_record_backfill(local_task: dict[str, Any], previous_cursor: int | None) -> bool:
    if not previous_cursor:
        return False
    if str(local_task.get("cloud_run_record_backfill_date") or "").strip() == local_today().isoformat():
        return False
    current_status = get_task_day_status(local_task, local_today())
    if str(current_status.get("brand_status") or "").strip() in {"success", "sent"}:
        return False
    task_name = str(local_task.get("name") or "").strip()
    task_id = str(local_task.get("task_id") or "").strip()
    if not task_name:
        return False
    return not _bundle_has_success(
        get_task_daily_success_bundle(task_name, target_date=local_today(), task_id=task_id)
    )


def _bundle_has_success(success_bundle: dict[str, Any]) -> bool:
    if not isinstance(success_bundle, dict):
        return False
    if success_bundle.get("completed_query_keys"):
        return True
    if success_bundle.get("completed_keywords"):
        return True
    return bool(success_bundle.get("query_results"))


def _cloud_success_keyword_updates(success_bundle: dict[str, Any], local_task: dict[str, Any]) -> list[dict[str, Any]]:
    fallback_brand = str(local_task.get("brand") or local_task.get("name") or "").strip()
    updates: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in success_bundle.get("query_results") or []:
        if not isinstance(item, dict):
            continue
        keyword = str(item.get("keyword") or "").strip()
        if not keyword:
            continue
        platform = normalize_platform_id(str(item.get("platform") or "").strip())
        brand = str(item.get("brand") or fallback_brand).strip()
        key = (_normalize_key_text(keyword), _normalize_key_text(brand), platform)
        if key in seen:
            continue
        seen.add(key)
        updates.append({
            "keyword": keyword,
            "brand": brand,
            "platform": platform,
            "run_success": True,
            "screenshot_saved": True,
            "failure_reason": "",
            "image_path": "",
        })
    if updates:
        return updates

    for keyword in _normalize_string_list(success_bundle.get("completed_keywords")):
        updates.append({
            "keyword": keyword,
            "brand": fallback_brand,
            "platform": "",
            "run_success": True,
            "screenshot_saved": True,
            "failure_reason": "",
            "image_path": "",
        })
    return updates


def _required_query_keys(task: dict[str, Any]) -> set[tuple[str, str, str]]:
    default_brand = str(task.get("brand") or task.get("name") or "").strip()
    keys: set[tuple[str, str, str]] = set()
    for keyword_entry in task.get("keywords") or []:
        if not isinstance(keyword_entry, dict):
            continue
        keyword = str(keyword_entry.get("keyword") or "").strip()
        brand = str(keyword_entry.get("brand") or default_brand).strip()
        if not keyword or not brand:
            continue
        for platform in keyword_entry.get("platforms") or []:
            platform_id = normalize_platform_id(str(platform or "").strip())
            if platform_id:
                keys.add((_normalize_key_text(keyword), _normalize_key_text(brand), platform_id))
    return keys


def _normalize_query_key_set(values: Any) -> set[tuple[str, str, str]]:
    result: set[tuple[str, str, str]] = set()
    for value in values or []:
        if not isinstance(value, (list, tuple)) or len(value) < 3:
            continue
        keyword = str(value[0] or "").strip()
        brand = str(value[1] or "").strip()
        platform = normalize_platform_id(str(value[2] or "").strip())
        if keyword and brand and platform:
            result.add((_normalize_key_text(keyword), _normalize_key_text(brand), platform))
    return result


def _normalize_key_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _entry_local_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


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
    missing_cloud_task_ids: list[int] | set[int] | tuple[int, ...] | None = None,
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

    scoped_missing_cloud_ids = set(_unique_ints(list(missing_cloud_task_ids or [])))
    if disable_missing or scoped_missing_cloud_ids:
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
            if not disable_missing and existing_cloud_id not in scoped_missing_cloud_ids:
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
            if not disable_missing and cloud_task_id not in scoped_missing_cloud_ids:
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


def _list_tasks_by_ids_with_refresh(
    client: SurfacedCloudClient,
    store: CloudSessionStore,
    session: dict[str, Any],
    *,
    task_ids: list[int],
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
        operation=lambda token: client.list_tasks_by_ids(token, task_ids),
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
            record_event_safe(
                "cloud_sync",
                "云端会话已过期（任务同步刷新 401）",
                level="warning",
                event_key=f"cloud_session_expired:{base_url}:{identity['workspace_id']}:{identity['user_id']}",
                throttle_seconds=600,
                details={"base_url": base_url, "workspace_id": identity["workspace_id"]},
                suggestion="请重新登录云端",
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
            record_event_safe(
                "cloud_sync",
                "云端会话已过期（任务同步重试仍 401）",
                level="warning",
                event_key=f"cloud_session_expired:{base_url}:{refreshed_identity['workspace_id']}:{refreshed_identity['user_id']}",
                throttle_seconds=600,
                details={"base_url": base_url, "workspace_id": refreshed_identity["workspace_id"]},
                suggestion="请重新登录云端",
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
    # A viewer can see the task but cannot execute it; execution permission is
    # enforced by the local runtime role checks, not by hiding the task as disabled.
    task["enabled"] = cloud_enabled and access_level in {"admin", "operate", "view"}

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
    safe_mode = fallback_mode if fallback_mode in {"browser", "recognition"} else "browser"
    for item in raw_keywords:
        if isinstance(item, dict):
            keyword = str(item.get("keyword") or item.get("name") or "").strip()
            keyword_brand = str(item.get("brand") or brand or "").strip()
            platforms = _normalize_platform_list(item.get("platforms")) or list(fallback_platforms)
            mode = str(item.get("mode") or safe_mode).strip()
            if mode not in {"browser", "recognition"}:
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


def _normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
    return items


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


def _normalize_int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    return _unique_ints(value)


def _unique_ints(value: list[Any]) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for item in value:
        normalized = _normalize_int(item)
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _normalize_event_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item or "").strip() for item in value if str(item or "").strip()]


def _cloud_task_run_cursors(config: dict[str, Any]) -> dict[int, int]:
    cursors: dict[int, int] = {}
    for task in _iter_local_tasks_and_deleted_backups(config):
        cloud_task_id = _normalize_int(task.get("cloud_task_id") or task.get("cloudTaskId"))
        if cloud_task_id is None:
            continue
        cursor = _normalize_int(
            task.get("cloud_last_run_record_synced_id")
            or task.get("cloudLastRunRecordSyncedId")
        )
        cursors[cloud_task_id] = cursor or 0
    return cursors


def _cloud_task_day_status_cursors(config: dict[str, Any]) -> dict[int, int]:
    cursors: dict[int, int] = {}
    for task in _iter_local_tasks_and_deleted_backups(config):
        cloud_task_id = _normalize_int(task.get("cloud_task_id") or task.get("cloudTaskId"))
        if cloud_task_id is None:
            continue
        cursor = _normalize_int(
            task.get("cloud_last_task_day_status_synced_id")
            or task.get("cloudLastTaskDayStatusSyncedId")
        )
        cursors[cloud_task_id] = cursor or 0
    return cursors


def _cloud_known_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    state = config.get(CLOUD_PLATFORM_SYNC_STATE_KEY) if isinstance(config, dict) else {}
    if not isinstance(state, dict):
        return {}
    snapshot = state.get("event_snapshot")
    return dict(snapshot) if isinstance(snapshot, dict) else {}


def _update_cloud_sync_snapshot(config: dict[str, Any], snapshot: Any, summary: dict[str, Any]) -> None:
    if not isinstance(snapshot, dict):
        return
    state = config.get(CLOUD_PLATFORM_SYNC_STATE_KEY)
    if not isinstance(state, dict):
        state = {}
    previous = state.get("event_snapshot") if isinstance(state.get("event_snapshot"), dict) else {}
    if previous == snapshot:
        return
    state["event_snapshot"] = dict(snapshot)
    state["event_snapshot_synced_at"] = local_now().isoformat(timespec="seconds")
    config[CLOUD_PLATFORM_SYNC_STATE_KEY] = state
    summary["state_updated"] = 1


def _summarize_changes(changes: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(changes.get("event_id") or ""),
        "events": [str(item) for item in (changes.get("events") if isinstance(changes.get("events"), list) else [])],
        "full_task_pull_required": bool(changes.get("full_task_pull_required")),
        "run_record_task_ids": _normalize_int_list(changes.get("run_record_task_ids")),
        "task_day_status_task_ids": _normalize_int_list(changes.get("task_day_status_task_ids")),
        "reference_changed": bool(changes.get(EVENT_REFERENCE_CHANGED)),
    }


def _empty_run_summary() -> dict[str, Any]:
    return {"tasks": 0, "fetched": 0, "imported": 0, "failed": 0, "cursor_updates": 0, "state_updated": 0, "backfilled": 0}


def _empty_task_day_status_summary() -> dict[str, Any]:
    return {"tasks": 0, "fetched": 0, "applied": 0, "failed": 0, "cursor_updates": 0, "state_updated": 0}


def _elapsed_ms(started_at: float) -> int:
    return max(0, int(round((time.perf_counter() - started_at) * 1000)))


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
        "state_updated": 0,
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
