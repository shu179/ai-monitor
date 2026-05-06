from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from .cloud_client import CloudClientError, SurfacedCloudClient
from .cloud_event_types import (
    EVENT_ARTICLE_REFERENCE,
    EVENT_PROFILE_UPDATE,
    EVENT_RUN_RECORD,
    EVENT_TASK_DAY_STATUS,
    MANUAL_TEST_HISTORY_SOURCES,
)
from .cloud_outbox import CloudOutbox
from .cloud_session_store import CloudSessionChangedError, CloudSessionStore, cloud_session_identity
from .history import get_records, normalize_platform_id
from .reference_urls import normalize_reference_url
from .time_utils import local_now, local_today, parse_local_date


MAX_CLOUD_TEXT_FIELD_LENGTH = 500


def enqueue_profile_update(
    payload: dict[str, Any],
    *,
    outbox: CloudOutbox | None = None,
) -> dict[str, Any] | None:
    clean = _sanitize_profile_update_payload(payload)
    if not clean:
        return None
    identity = cloud_session_identity(CloudSessionStore().load())
    identity_key = "|".join([identity["base_url"], identity["workspace_id"], identity["user_id"]])
    identity_hash = hashlib.sha256(identity_key.encode("utf-8", errors="ignore")).hexdigest()[:12]
    fingerprint = hashlib.sha256(
        repr(sorted(clean.items())).encode("utf-8", errors="ignore")
    ).hexdigest()[:24]
    target_outbox = outbox or CloudOutbox()
    queued, _created = target_outbox.enqueue(
        event_type=EVENT_PROFILE_UPDATE,
        idempotency_key=f"profile:{identity_hash}:{fingerprint}",
        payload=clean,
    )
    return queued


def enqueue_task_day_status(
    payload: dict[str, Any],
    *,
    outbox: CloudOutbox | None = None,
) -> dict[str, Any] | None:
    clean = _sanitize_task_day_status_payload(payload)
    task_id = _normalize_cloud_task_id(clean.get("task_id"))
    if task_id is None:
        return None
    task_day = str(clean.get("task_day") or local_today().isoformat()).strip()[:10]
    fingerprint = hashlib.sha256(
        repr(sorted(clean.items())).encode("utf-8", errors="ignore")
    ).hexdigest()[:24]
    target_outbox = outbox or CloudOutbox()
    queued, _created = target_outbox.enqueue(
        event_type=EVENT_TASK_DAY_STATUS,
        idempotency_key=f"task-day:{task_id}:{task_day}:{fingerprint}",
        payload=clean,
    )
    return queued


def history_record_to_run_event(record: dict[str, Any], *, cloud_task_id: Any = None) -> dict[str, Any] | None:
    events = history_record_to_run_events(record, cloud_task_id=cloud_task_id)
    return events[0] if events else None


def history_record_to_run_events(record: dict[str, Any], *, cloud_task_id: Any = None) -> list[dict[str, Any]]:
    task_id = _normalize_cloud_task_id(cloud_task_id or _record_cloud_task_id(record))
    if task_id is None:
        return []
    if _is_manual_test_history_record(record):
        return []
    return [
        event
        for expanded_record in _expand_history_record_for_cloud_run(record)
        if (event := _history_record_to_run_event_single(expanded_record, cloud_task_id=task_id)) is not None
    ]


def _history_record_to_run_event_single(record: dict[str, Any], *, cloud_task_id: int) -> dict[str, Any] | None:
    record_id = str(record.get("id") or "").strip()
    idempotency_key = f"run:{record_id}" if record_id else f"run:{_record_fingerprint(record, cloud_task_id)}"
    platform = normalize_platform_id(str(record.get("platform") or "").strip())
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    run_started_at = _normalize_executed_at(
        extra.get("run_started_at")
        or record.get("run_started_at")
        or record.get("started_at")
        or record.get("ts")
    )
    result_payload = {
        "rank": int(record.get("rank") or 99),
        "success": bool(record.get("success")),
        "review_status": str(record.get("review_status") or "").strip(),
        "highlight_count": int(record.get("highlight_count") or 0),
        "reference_count": int(extra.get("reference_count") or 0),
        "body_reference_count": int(extra.get("body_reference_count") or 0),
        "total_reference_count": int(extra.get("total_reference_count") or 0),
        "error_message": _cloud_text_field(record.get("error_message")),
        "diagnostic_id": _cloud_text_field(record.get("diagnostic_id"), limit=128),
    }
    return {
        "event_type": EVENT_RUN_RECORD,
        "idempotency_key": idempotency_key,
        "payload": {
            "task_id": cloud_task_id,
            "platform": platform,
            "keyword": str(record.get("keyword") or "").strip(),
            "brand": str(record.get("brand") or "").strip(),
            "mode": str(record.get("mode") or "").strip() or "browser",
            "executed_at": _normalize_executed_at(record.get("ts")),
            "run_started_at": run_started_at,
            "result": result_payload,
        },
    }


def _expand_history_record_for_cloud_run(record: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(record, dict):
        return []
    if str(record.get("mode") or "").strip() != "recognition" or not bool(record.get("success")):
        return [record]

    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    matched_pairs = extra.get("matched_pairs") if isinstance(extra.get("matched_pairs"), list) else []
    if not matched_pairs:
        platform = normalize_platform_id(str(record.get("platform") or "").strip())
        keyword = str(record.get("keyword") or "").strip()
        return [record] if keyword and keyword != "clipboard" and platform and platform != "recognition" else []

    detected_platforms = [
        platform
        for platform in (normalize_platform_id(str(item or "").strip()) for item in (extra.get("detected_platforms") or []))
        if platform and platform != "recognition"
    ]
    base_id = str(record.get("id") or "").strip() or _record_fingerprint(record, _normalize_cloud_task_id(_record_cloud_task_id(record)) or 0)
    expanded: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in matched_pairs:
        if not isinstance(item, dict):
            continue
        keyword = str(item.get("keyword") or "").strip()
        brand = str(item.get("brand") or record.get("brand") or "").strip()
        pair_platforms = [
            platform
            for platform in (normalize_platform_id(str(value or "").strip()) for value in (item.get("platforms") or []))
            if platform and platform != "recognition"
        ] or detected_platforms
        if not keyword or not brand or not pair_platforms:
            continue
        for platform in pair_platforms:
            key = (keyword, brand, platform)
            if key in seen:
                continue
            seen.add(key)
            cloud_record = dict(record)
            cloud_record["id"] = f"{base_id}:{hashlib.sha1('|'.join(key).encode('utf-8')).hexdigest()[:12]}"
            cloud_record["keyword"] = keyword
            cloud_record["brand"] = brand
            cloud_record["platform"] = platform
            cloud_record["mode"] = "recognition"
            cloud_extra = dict(extra)
            cloud_extra["detected_platforms"] = [platform]
            cloud_extra["matched_pairs"] = [{"keyword": keyword, "brand": brand, "platforms": [platform]}]
            cloud_record["extra"] = cloud_extra
            expanded.append(cloud_record)

    return expanded or [record]


def history_record_to_reference_events(record: dict[str, Any], *, cloud_task_id: Any = None) -> list[dict[str, Any]]:
    task_id = _normalize_cloud_task_id(cloud_task_id or _record_cloud_task_id(record))
    if task_id is None:
        return []
    if _is_manual_test_history_record(record):
        return []

    urls = _record_reference_urls(record)
    if not urls:
        return []

    record_id = str(record.get("id") or "").strip()
    source_record_key = f"run:{record_id}" if record_id else f"run:{_record_fingerprint(record, task_id)}"
    platform = normalize_platform_id(str(record.get("platform") or "").strip()) or "unknown"
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    executed_at = _normalize_executed_at(record.get("ts"))
    run_started_at = _normalize_executed_at(
        extra.get("run_started_at")
        or record.get("run_started_at")
        or record.get("started_at")
        or record.get("ts")
    )
    record_day = str(executed_at or "")[:10]
    events: list[dict[str, Any]] = []
    for url in urls:
        reference_hash = hashlib.sha256(f"{source_record_key}\n{task_id}\n{url}".encode("utf-8")).hexdigest()[:32]
        events.append(
            {
                "event_type": EVENT_ARTICLE_REFERENCE,
                "idempotency_key": f"ref:{reference_hash}",
                "payload": {
                    "task_id": task_id,
                    "platform": platform,
                    "record_day": record_day,
                    "source_record_key": source_record_key,
                    "run_started_at": run_started_at,
                    "normalized_url": url,
                    "url": url,
                    "keyword": str(record.get("keyword") or "").strip(),
                    "brand": str(record.get("brand") or "").strip(),
                },
            }
        )
    return events


def enqueue_run_record_from_history(
    record: dict[str, Any],
    *,
    cloud_task_id: Any = None,
    outbox: CloudOutbox | None = None,
) -> dict[str, Any] | None:
    events = history_record_to_run_events(record, cloud_task_id=cloud_task_id)
    if not events:
        return None
    target_outbox = outbox or CloudOutbox()
    first_queued: dict[str, Any] | None = None
    for event in events:
        queued, _created = target_outbox.enqueue(
            event_type=event["event_type"],
            idempotency_key=event["idempotency_key"],
            payload=event["payload"],
        )
        if first_queued is None:
            first_queued = queued
    for reference_event in history_record_to_reference_events(record, cloud_task_id=cloud_task_id):
        target_outbox.enqueue(
            event_type=reference_event["event_type"],
            idempotency_key=reference_event["idempotency_key"],
            payload=reference_event["payload"],
        )
    return first_queued


def enqueue_recent_cloud_run_records_from_history(
    config: dict[str, Any],
    *,
    outbox: CloudOutbox | None = None,
    days: int = 7,
    max_records_per_task: int = 1000,
) -> dict[str, int]:
    target_outbox = outbox or CloudOutbox()
    before_total = int(target_outbox.stats().get("total") or 0)
    cutoff = local_today().toordinal() - max(0, int(days or 0))
    scanned_tasks = 0
    scanned_records = 0
    candidate_records = 0

    for task in (config or {}).get("tasks") or []:
        if not isinstance(task, dict):
            continue
        cloud_task_id = _normalize_cloud_task_id(task.get("cloud_task_id") or task.get("cloudTaskId"))
        if cloud_task_id is None:
            continue
        task_name = str(task.get("name") or task.get("brand") or "").strip()
        task_id = str(task.get("task_id") or "").strip()
        if not task_name:
            continue
        scanned_tasks += 1
        try:
            records = get_records(task_name, task_id=task_id)
        except Exception:
            continue
        for record in records[-max(1, int(max_records_per_task or 1000)):]:
            if not isinstance(record, dict):
                continue
            if _is_cloud_imported_history_record(record):
                continue
            record_date = parse_local_date(record.get("ts"))
            if record_date is not None and record_date.toordinal() < cutoff:
                continue
            scanned_records += 1
            if not history_record_to_run_events(record, cloud_task_id=cloud_task_id):
                continue
            candidate_records += 1
            try:
                enqueue_run_record_from_history(record, cloud_task_id=cloud_task_id, outbox=target_outbox)
            except Exception:
                continue

    after_total = int(target_outbox.stats().get("total") or 0)
    return {
        "tasks": scanned_tasks,
        "records": scanned_records,
        "candidates": candidate_records,
        "queued": max(0, after_total - before_total),
    }


def _is_cloud_imported_history_record(record: dict[str, Any]) -> bool:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    return (
        str(record.get("execution_source") or "").strip() == "cloud"
        or str(record.get("id") or "").strip().startswith("cloud:")
        or bool(extra.get("cloud_run_record_id") or extra.get("cloud_idempotency_key"))
    )


def _is_manual_test_history_record(record: dict[str, Any]) -> bool:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    for value in (
        record.get("execution_source"),
        record.get("source"),
        record.get("source_mode"),
        record.get("scope"),
        extra.get("execution_source"),
        extra.get("source"),
        extra.get("source_mode"),
        extra.get("scope"),
    ):
        text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        if text in MANUAL_TEST_HISTORY_SOURCES:
            return True
    return False


def flush_cloud_outbox(
    *,
    client: SurfacedCloudClient | None = None,
    session_store: CloudSessionStore | None = None,
    outbox: CloudOutbox | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    started_at = datetime.now().timestamp()
    store = session_store or CloudSessionStore()
    session = store.load()
    queue = (outbox or CloudOutbox()).bind_to_session(session)
    base_url = str(session.get("base_url") or "").strip()
    access_token = str(session.get("access_token") or "").strip()
    refresh_token = str(session.get("refresh_token") or "").strip()
    if not base_url or not access_token:
        return {"ok": False, "message": "未登录云端", "outbox": queue.stats(), "metrics": _flush_metrics(started_at, 0, 0)}

    pending = queue.pending(limit=limit)
    if not pending:
        return {"ok": True, "message": "没有待上传数据", "outbox": queue.stats(), "metrics": _flush_metrics(started_at, 0, 0)}
    pending, obsolete_profile_keys = _collapse_profile_update_events(pending)
    if obsolete_profile_keys:
        queue.mark_sent(obsolete_profile_keys)

    identity = cloud_session_identity(session)
    target_client = client or SurfacedCloudClient(base_url)
    event_keys = [str(item.get("idempotency_key") or "") for item in pending]
    events = [
        {
            "event_type": item.get("event_type"),
            "idempotency_key": item.get("idempotency_key"),
            "payload": _sanitize_event_payload_for_upload(
                str(item.get("event_type") or ""),
                item.get("payload") if isinstance(item.get("payload"), dict) else {},
            ),
        }
        for item in pending
    ]
    try:
        response = target_client.post_events(access_token, events)
    except CloudClientError as exc:
        if exc.status_code == 401 and refresh_token:
            try:
                refreshed_session = store.refresh_login_if_current(
                    base_url=base_url,
                    access_token=access_token,
                    refresh_token=refresh_token,
                    refresh=target_client.refresh,
                    workspace_id=identity["workspace_id"],
                    user_id=identity["user_id"],
                )
                refreshed_access_token = str(refreshed_session.get("access_token") or "").strip()
                if not refreshed_access_token:
                    queue.mark_failed(event_keys, "未登录云端")
                    return {"ok": False, "message": "未登录云端", "outbox": queue.stats(), "metrics": _flush_metrics(started_at, len(events), 0)}
            except CloudSessionChangedError as changed_exc:
                return {"ok": False, "message": str(changed_exc), "outbox": queue.stats(), "metrics": _flush_metrics(started_at, len(events), 0)}
            except CloudClientError as refresh_exc:
                if refresh_exc.status_code == 401:
                    store.clear_if_current(
                        base_url=base_url,
                        access_token=access_token,
                        refresh_token=refresh_token,
                        workspace_id=identity["workspace_id"],
                        user_id=identity["user_id"],
                )
                queue.mark_failed(event_keys, str(refresh_exc))
                return {"ok": False, "message": str(refresh_exc), "outbox": queue.stats(), "metrics": _flush_metrics(started_at, len(events), 0)}
            try:
                response = target_client.post_events(refreshed_access_token, events)
            except CloudClientError as refresh_exc:
                if refresh_exc.status_code == 401:
                    store.clear_if_current(
                        base_url=base_url,
                        access_token=refreshed_access_token,
                        refresh_token=str(refreshed_session.get("refresh_token") or "").strip(),
                        workspace_id=identity["workspace_id"],
                        user_id=identity["user_id"],
                )
                queue.mark_failed(event_keys, str(refresh_exc))
                return {"ok": False, "message": str(refresh_exc), "outbox": queue.stats(), "metrics": _flush_metrics(started_at, len(events), 0)}
        else:
            queue.mark_failed(event_keys, str(exc))
            return {"ok": False, "message": str(exc), "outbox": queue.stats(), "metrics": _flush_metrics(started_at, len(events), 0)}

    queue.mark_sent(event_keys)
    return {
        "ok": True,
        "message": "上传完成",
        "response": response if isinstance(response, dict) else {},
        "outbox": queue.stats(),
        "metrics": _flush_metrics(started_at, len(events), int((response or {}).get("accepted") or 0) if isinstance(response, dict) else 0),
    }


def _collapse_profile_update_events(pending: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    latest_profile_index = -1
    for index, item in enumerate(pending):
        if str(item.get("event_type") or "").strip() == EVENT_PROFILE_UPDATE:
            latest_profile_index = index
    if latest_profile_index < 0:
        return pending, []

    collapsed: list[dict[str, Any]] = []
    obsolete_keys: list[str] = []
    for index, item in enumerate(pending):
        if str(item.get("event_type") or "").strip() == EVENT_PROFILE_UPDATE and index != latest_profile_index:
            key = str(item.get("idempotency_key") or "").strip()
            if key:
                obsolete_keys.append(key)
            continue
        collapsed.append(item)
    return collapsed, obsolete_keys


def _flush_metrics(started_at: float, event_count: int, accepted_count: int) -> dict[str, int]:
    return {
        "duration_ms": max(0, int(round((datetime.now().timestamp() - started_at) * 1000))),
        "event_count": max(0, int(event_count or 0)),
        "accepted_count": max(0, int(accepted_count or 0)),
        "request_count": 1 if int(event_count or 0) > 0 else 0,
    }


def _record_cloud_task_id(record: dict[str, Any]) -> Any:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    return (
        record.get("cloud_task_id")
        or record.get("cloudTaskId")
        or extra.get("cloud_task_id")
        or extra.get("cloudTaskId")
        or _cloud_task_id_from_local_task_id(record.get("task_id"))
    )


def _cloud_task_id_from_local_task_id(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text.startswith("cloud_"):
        return None
    try:
        task_id = int(text.split("_", 1)[1])
    except Exception:
        return None
    return task_id if task_id > 0 else None


def _normalize_cloud_task_id(value: Any) -> int | None:
    try:
        task_id = int(str(value or "").strip())
    except Exception:
        return None
    return task_id if task_id > 0 else None


def _normalize_executed_at(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return local_now().isoformat(timespec="seconds")
    normalized = text.replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=local_now().tzinfo).isoformat(timespec="seconds")
        except Exception:
            pass
    try:
        return datetime.fromisoformat(normalized).isoformat(timespec="seconds")
    except Exception:
        return local_now().isoformat(timespec="seconds")


def _record_fingerprint(record: dict[str, Any], cloud_task_id: int) -> str:
    parts = [
        str(cloud_task_id),
        str(record.get("ts") or ""),
        str(record.get("platform") or ""),
        str(record.get("keyword") or ""),
        str(record.get("brand") or ""),
        str(record.get("rank") or ""),
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:24]


def _cloud_text_field(value: Any, *, limit: int = MAX_CLOUD_TEXT_FIELD_LENGTH) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit]


def _record_reference_urls(record: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    for source in (extra.get("references"), record.get("references"), extra.get("body_references"), record.get("body_references")):
        if not isinstance(source, list):
            continue
        for item in source:
            raw_url = ""
            if isinstance(item, dict):
                for key in ("url", "link", "href"):
                    raw_url = str(item.get(key) or "").strip()
                    if raw_url:
                        break
            else:
                raw_url = str(item or "").strip()
            normalized = normalize_reference_url(raw_url)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            urls.append(normalized)
    return urls


def _sanitize_event_payload_for_upload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if event_type == EVENT_PROFILE_UPDATE:
        return _sanitize_profile_update_payload(payload)
    if event_type == EVENT_TASK_DAY_STATUS:
        return _sanitize_task_day_status_payload(payload)
    if event_type != EVENT_RUN_RECORD:
        return dict(payload or {})
    clean_payload = dict(payload or {})
    result = clean_payload.get("result") if isinstance(clean_payload.get("result"), dict) else {}
    clean_payload["result"] = _sanitize_run_result_for_cloud(result)
    return clean_payload


def _sanitize_profile_update_payload(payload: dict[str, Any]) -> dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    clean: dict[str, Any] = {}
    for key in ("display_name", "avatar", "birthday", "hire_date"):
        if key not in source:
            continue
        value = source.get(key)
        if value is None:
            clean[key] = None
            continue
        text = str(value or "").strip()
        if key == "avatar":
            clean[key] = text[:1_000_000]
        elif key == "display_name":
            clean[key] = text[:128] or None
        else:
            clean[key] = text[:10] or None
    return clean


def _sanitize_task_day_status_payload(payload: dict[str, Any]) -> dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    task_id = _normalize_cloud_task_id(source.get("task_id") or source.get("taskId"))
    if task_id is None:
        return {}
    task_day = str(source.get("task_day") or source.get("taskDay") or source.get("date") or local_today().isoformat()).strip()[:10]
    status = str(source.get("status") or "success").strip() or "success"
    clean: dict[str, Any] = {
        "task_id": task_id,
        "task_day": task_day,
        "status": status[:32],
    }
    for key, limit in (
        ("source", 64),
        ("message", 256),
        ("updated_at", 64),
        ("run_started_at", 64),
    ):
        if key in source:
            clean[key] = _cloud_text_field(source.get(key), limit=limit)
    for key in ("brands", "completed_keywords", "detected_platforms", "supplemented_keywords"):
        values = _cloud_text_list(source.get(key) or source.get(_camel_case_key(key)), limit=128)
        if values:
            clean[key] = values
    for key in ("image_count", "actual_screenshot_count", "fixed_screenshot_target"):
        if key not in source and _camel_case_key(key) not in source:
            continue
        try:
            clean[key] = max(0, int(source.get(key, source.get(_camel_case_key(key))) or 0))
        except Exception:
            clean[key] = 0
    for key in ("notification_success", "forced_ignore_failure", "completed_by_quota"):
        if key in source or _camel_case_key(key) in source:
            clean[key] = bool(source.get(key, source.get(_camel_case_key(key))))
    extra = source.get("extra") if isinstance(source.get("extra"), dict) else {}
    if extra:
        clean_extra: dict[str, Any] = {}
        for key in ("task_failure_kind", "query_round_status", "task_status"):
            if key in extra:
                clean_extra[key] = _cloud_text_field(extra.get(key), limit=64)
        if clean_extra:
            clean["extra"] = clean_extra
    return clean


def _sanitize_run_result_for_cloud(result: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    allowed = {
        "rank",
        "success",
        "review_status",
        "highlight_count",
        "reference_count",
        "body_reference_count",
        "total_reference_count",
        "error_message",
        "diagnostic_id",
    }
    for key in allowed:
        if key in result:
            clean[key] = result.get(key)
    for key in ("error_message",):
        if key in clean:
            clean[key] = _cloud_text_field(clean.get(key))
    if "diagnostic_id" in clean:
        clean["diagnostic_id"] = _cloud_text_field(clean.get("diagnostic_id"), limit=128)
    if "review_status" in clean:
        clean["review_status"] = _cloud_text_field(clean.get("review_status"), limit=64)
    for key in ("rank", "highlight_count", "reference_count", "body_reference_count", "total_reference_count"):
        if key not in clean:
            continue
        try:
            clean[key] = int(clean.get(key) or 0)
        except Exception:
            clean[key] = 0
    if "success" in clean:
        clean["success"] = bool(clean.get("success"))
    return clean


def _cloud_text_list(value: Any, *, limit: int = 128, max_items: int = 200) -> list[str]:
    source = value if isinstance(value, list) else []
    result: list[str] = []
    seen: set[str] = set()
    for item in source:
        text = _cloud_text_field(item, limit=limit)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= max_items:
            break
    return result


def _camel_case_key(value: str) -> str:
    parts = str(value or "").split("_")
    if not parts:
        return value
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])
