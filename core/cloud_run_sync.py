from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from .cloud_client import CloudClientError, SurfacedCloudClient
from .cloud_outbox import CloudOutbox
from .cloud_session_store import CloudSessionChangedError, CloudSessionStore, cloud_session_identity
from .history import normalize_platform_id
from .reference_urls import normalize_reference_url
from .time_utils import local_now


MAX_CLOUD_TEXT_FIELD_LENGTH = 500


def history_record_to_run_event(record: dict[str, Any], *, cloud_task_id: Any = None) -> dict[str, Any] | None:
    task_id = _normalize_cloud_task_id(cloud_task_id or _record_cloud_task_id(record))
    if task_id is None:
        return None

    record_id = str(record.get("id") or "").strip()
    idempotency_key = f"run:{record_id}" if record_id else f"run:{_record_fingerprint(record, task_id)}"
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
        "event_type": "run_record",
        "idempotency_key": idempotency_key,
        "payload": {
            "task_id": task_id,
            "platform": platform,
            "keyword": str(record.get("keyword") or "").strip(),
            "brand": str(record.get("brand") or "").strip(),
            "mode": str(record.get("mode") or "").strip() or "browser",
            "executed_at": _normalize_executed_at(record.get("ts")),
            "run_started_at": run_started_at,
            "result": result_payload,
        },
    }


def history_record_to_reference_events(record: dict[str, Any], *, cloud_task_id: Any = None) -> list[dict[str, Any]]:
    task_id = _normalize_cloud_task_id(cloud_task_id or _record_cloud_task_id(record))
    if task_id is None:
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
                "event_type": "article_reference_event",
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
    event = history_record_to_run_event(record, cloud_task_id=cloud_task_id)
    if not event:
        return None
    target_outbox = outbox or CloudOutbox()
    queued, _created = target_outbox.enqueue(
        event_type=event["event_type"],
        idempotency_key=event["idempotency_key"],
        payload=event["payload"],
    )
    for reference_event in history_record_to_reference_events(record, cloud_task_id=cloud_task_id):
        target_outbox.enqueue(
            event_type=reference_event["event_type"],
            idempotency_key=reference_event["idempotency_key"],
            payload=reference_event["payload"],
        )
    return queued


def flush_cloud_outbox(
    *,
    client: SurfacedCloudClient | None = None,
    session_store: CloudSessionStore | None = None,
    outbox: CloudOutbox | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    store = session_store or CloudSessionStore()
    session = store.load()
    queue = (outbox or CloudOutbox()).bind_to_session(session)
    base_url = str(session.get("base_url") or "").strip()
    access_token = str(session.get("access_token") or "").strip()
    refresh_token = str(session.get("refresh_token") or "").strip()
    if not base_url or not access_token:
        return {"ok": False, "message": "未登录云端", "outbox": queue.stats()}

    pending = queue.pending(limit=limit)
    if not pending:
        return {"ok": True, "message": "没有待上传数据", "outbox": queue.stats()}

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
                    return {"ok": False, "message": "未登录云端", "outbox": queue.stats()}
            except CloudSessionChangedError as changed_exc:
                return {"ok": False, "message": str(changed_exc), "outbox": queue.stats()}
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
                return {"ok": False, "message": str(refresh_exc), "outbox": queue.stats()}
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
                return {"ok": False, "message": str(refresh_exc), "outbox": queue.stats()}
        else:
            queue.mark_failed(event_keys, str(exc))
            return {"ok": False, "message": str(exc), "outbox": queue.stats()}

    queue.mark_sent(event_keys)
    return {
        "ok": True,
        "message": "上传完成",
        "response": response if isinstance(response, dict) else {},
        "outbox": queue.stats(),
    }


def _record_cloud_task_id(record: dict[str, Any]) -> Any:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    return (
        record.get("cloud_task_id")
        or record.get("cloudTaskId")
        or extra.get("cloud_task_id")
        or extra.get("cloudTaskId")
    )


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
    if event_type != "run_record":
        return dict(payload or {})
    clean_payload = dict(payload or {})
    result = clean_payload.get("result") if isinstance(clean_payload.get("result"), dict) else {}
    clean_payload["result"] = _sanitize_run_result_for_cloud(result)
    return clean_payload


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
