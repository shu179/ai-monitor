from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from typing import Any

from .cloud_client import CloudClientError, SurfacedCloudClient, new_trace_id
from .diagnostic_events import clear_consecutive_failure, record_consecutive_failure, record_event_safe
from .cloud_event_types import (
    EVENT_ARTICLE_REFERENCE,
    EVENT_ARTICLE_TASK_LINKS,
    EVENT_ARTICLE_UPSERT,
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
MAX_ARTICLE_PAYLOAD_TEXT_LENGTH = 500


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


def article_to_cloud_events(article: dict[str, Any], config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    clean_article = _sanitize_article_for_cloud(article)
    canonical_url = str(clean_article.get("canonical_url") or "").strip()
    if not canonical_url:
        return []

    url_hash = hashlib.sha256(canonical_url.encode("utf-8", errors="ignore")).hexdigest()
    identity_hash = _cloud_identity_hash()
    upsert_payload = {
        "local_article_id": clean_article.get("local_article_id"),
        "url": canonical_url,
        "canonical_url": canonical_url,
        "url_hash": url_hash,
        "title": clean_article.get("title"),
        "source": clean_article.get("source"),
        "media_type": clean_article.get("media_type"),
        "published_at": clean_article.get("published_at"),
        "payload": clean_article.get("payload") or {},
    }
    upsert_fingerprint = hashlib.sha256(
        json_stable_dumps(upsert_payload).encode("utf-8", errors="ignore")
    ).hexdigest()[:16]

    events = [
        {
            "event_type": EVENT_ARTICLE_UPSERT,
            "idempotency_key": f"article:{identity_hash}:{url_hash[:16]}:{upsert_fingerprint}",
            "payload": upsert_payload,
        }
    ]

    link_payload = _article_task_links_payload(article, config, canonical_url=canonical_url, url_hash=url_hash)
    if link_payload is not None:
        link_fingerprint = hashlib.sha256(
            json_stable_dumps(link_payload).encode("utf-8", errors="ignore")
        ).hexdigest()[:16]
        events.append(
            {
                "event_type": EVENT_ARTICLE_TASK_LINKS,
                "idempotency_key": f"article-links:{identity_hash}:{url_hash[:16]}:{link_fingerprint}",
                "payload": link_payload,
            }
        )
    return events


def enqueue_article_cloud_sync(
    article: dict[str, Any],
    config: dict[str, Any] | None = None,
    *,
    outbox: CloudOutbox | None = None,
) -> list[dict[str, Any]]:
    events = article_to_cloud_events(article, config)
    if not events:
        return []
    target_outbox = outbox or CloudOutbox()
    result = target_outbox.enqueue_many(events)
    return list(result.get("items") or [])


def enqueue_cloud_articles(
    articles: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    config: dict[str, Any] | None = None,
    *,
    outbox: CloudOutbox | None = None,
    max_articles: int = 5000,
    batch_size: int = 200,
) -> dict[str, int]:
    target_outbox = outbox or CloudOutbox()
    scanned = 0
    candidates = 0
    events_to_enqueue: list[dict[str, Any]] = []
    for article in list(articles or [])[: max(1, int(max_articles or 5000))]:
        if not isinstance(article, dict):
            continue
        scanned += 1
        events = article_to_cloud_events(article, config)
        if not events:
            continue
        candidates += 1
        events_to_enqueue.extend(events)
    safe_batch_size = max(1, int(batch_size or 200))
    enqueue_result = {"created": 0, "requested": 0, "dropped": {"total": 0, "active": 0, "sent": 0}}
    try:
        for index in range(0, len(events_to_enqueue), safe_batch_size):
            batch = events_to_enqueue[index:index + safe_batch_size]
            batch_result = target_outbox.enqueue_many(batch)
            enqueue_result["created"] = int(enqueue_result.get("created") or 0) + int(batch_result.get("created") or 0)
            enqueue_result["requested"] = int(enqueue_result.get("requested") or 0) + int(batch_result.get("requested") or 0)
            dropped = batch_result.get("dropped") if isinstance(batch_result.get("dropped"), dict) else {}
            aggregate_dropped = enqueue_result.get("dropped") if isinstance(enqueue_result.get("dropped"), dict) else {}
            for key in ("total", "active", "sent"):
                aggregate_dropped[key] = int(aggregate_dropped.get(key) or 0) + int((dropped or {}).get(key) or 0)
            enqueue_result["dropped"] = aggregate_dropped
            if index + safe_batch_size < len(events_to_enqueue):
                time.sleep(0.5)
    except Exception:
        enqueue_result = {"created": 0, "requested": len(events_to_enqueue), "dropped": {"total": 0, "active": 0, "sent": 0}}
    dropped = enqueue_result.get("dropped") if isinstance(enqueue_result.get("dropped"), dict) else {}
    return {
        "articles": scanned,
        "candidates": candidates,
        "events": len(events_to_enqueue),
        "queued": max(0, int(enqueue_result.get("created") or 0)),
        "dropped": max(0, int((dropped or {}).get("total") or 0)),
        "dropped_active": max(0, int((dropped or {}).get("active") or 0)),
    }


def json_stable_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _cloud_identity_hash() -> str:
    try:
        identity = cloud_session_identity(CloudSessionStore().load())
        identity_key = "|".join([identity["base_url"], identity["workspace_id"], identity["user_id"]])
    except Exception:
        identity_key = ""
    if not identity_key.strip("|"):
        identity_key = "local"
    return hashlib.sha256(identity_key.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _sanitize_article_for_cloud(article: dict[str, Any]) -> dict[str, Any]:
    source = article if isinstance(article, dict) else {}
    canonical_url = _article_canonical_url(source)
    media_name = _cloud_text_field(source.get("media_name") or source.get("source") or source.get("platform"), limit=128)
    platform = _cloud_text_field(source.get("platform"), limit=128)
    published_at = _normalize_article_time(source.get("published_at") or source.get("ts"))
    payload = {
        "local_article_id": _cloud_text_field(source.get("id"), limit=128),
        "platform": platform,
        "media_name": media_name,
        "account_name": _cloud_text_field(source.get("account_name"), limit=128),
        "account_url": _cloud_text_field(source.get("account_url"), limit=512),
        "excerpt": _cloud_text_field(source.get("excerpt"), limit=MAX_ARTICLE_PAYLOAD_TEXT_LENGTH),
        "imported_at": _cloud_text_field(source.get("imported_at"), limit=64),
        "fetch_method": _cloud_text_field(source.get("fetch_method"), limit=64),
        "import_status": _cloud_text_field(source.get("import_status"), limit=32),
        "import_confirmed_at": _cloud_text_field(source.get("import_confirmed_at"), limit=64),
    }
    payload = {key: value for key, value in payload.items() if value not in ("", None, [], {})}
    return {
        "local_article_id": _cloud_text_field(source.get("id"), limit=128),
        "canonical_url": canonical_url,
        "title": _cloud_text_field(source.get("title"), limit=512),
        "source": media_name or platform,
        "media_type": _normalize_article_media_type(source.get("media_type")),
        "published_at": published_at,
        "payload": payload,
    }


def _article_task_links_payload(
    article: dict[str, Any],
    config: dict[str, Any] | None,
    *,
    canonical_url: str,
    url_hash: str,
) -> dict[str, Any] | None:
    source = article if isinstance(article, dict) else {}
    task_lookup = _cloud_task_lookup(config)
    matched_task_names = [
        name
        for name in (_cloud_text_list(source.get("matched_tasks"), limit=128, max_items=200))
        if name
    ]
    if not matched_task_names:
        return None

    task_ids: list[int] = []
    unresolved_task_names: list[str] = []
    task_name_by_id: dict[str, str] = {}
    for task_name in matched_task_names:
        cloud_task_id = task_lookup.get(task_name.lower())
        if cloud_task_id is None:
            unresolved_task_names.append(task_name)
            continue
        if cloud_task_id not in task_ids:
            task_ids.append(cloud_task_id)
            task_name_by_id[str(cloud_task_id)] = task_name

    reason_json: dict[str, Any] = {}
    raw_reasons = source.get("match_reasons") if isinstance(source.get("match_reasons"), dict) else {}
    for cloud_task_id_text, task_name in task_name_by_id.items():
        reasons = _cloud_text_list(raw_reasons.get(task_name), limit=256, max_items=5)
        if reasons:
            reason_json[cloud_task_id_text] = reasons

    payload = {
        "local_article_id": _cloud_text_field(source.get("id"), limit=128),
        "url": canonical_url,
        "canonical_url": canonical_url,
        "url_hash": url_hash,
        "task_ids": sorted(task_ids),
        "source": "local_rule",
        "confidence": 100,
        "reason_json": reason_json,
        "replace": True,
        "partial": bool(unresolved_task_names),
        "unresolved_task_names": unresolved_task_names[:50],
    }
    return payload


def _article_canonical_url(article: dict[str, Any]) -> str:
    raw_url = str((article or {}).get("url") or (article or {}).get("canonical_url") or "").strip()
    if not raw_url:
        return ""
    normalized = normalize_reference_url(raw_url)
    return normalized or raw_url


def _normalize_article_media_type(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "")
    if text in {"authority", "media", "official"}:
        return "authority"
    if text in {"selfmedia", "self", "ugc"}:
        return "selfmedia"
    return "selfmedia"


def _normalize_article_time(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) == 10:
        try:
            datetime.fromisoformat(text)
            return text
        except Exception:
            return text[:64]
    return _cloud_text_field(_normalize_executed_at(text), limit=64)


def _cloud_task_lookup(config: dict[str, Any] | None) -> dict[str, int]:
    lookup: dict[str, int] = {}
    for task in (config or {}).get("tasks") or []:
        if not isinstance(task, dict):
            continue
        cloud_task_id = _normalize_cloud_task_id(task.get("cloud_task_id") or task.get("cloudTaskId"))
        if cloud_task_id is None:
            continue
        task_id = str(task.get("task_id") or "").strip()
        task_name = str(task.get("name") or task_id).strip()
        brand = str(task.get("brand") or "").strip()
        for candidate in (task_name, brand, task_id, f"cloud_{cloud_task_id}", str(cloud_task_id)):
            key = str(candidate or "").strip().lower()
            if key and key not in lookup:
                lookup[key] = cloud_task_id
    return lookup


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
    started_at = time.monotonic()
    store = session_store or CloudSessionStore()
    session = store.load()
    queue = (outbox or CloudOutbox()).bind_to_session(session)
    initial_stats = queue.stats()
    pending_before = int(initial_stats.get("pending") or 0) + int(initial_stats.get("failed") or 0)
    trace_id = new_trace_id("outbox")

    def finish(result: dict[str, Any], *, batch_size: int, http_status: int | str | None) -> dict[str, Any]:
        final_stats = result.get("outbox") if isinstance(result.get("outbox"), dict) else queue.stats()
        _log_cloud_outbox_flush(
            started_at=started_at,
            batch_size=batch_size,
            pending_before=pending_before,
            pending_after=int((final_stats or {}).get("pending") or 0) + int((final_stats or {}).get("failed") or 0),
            failed_count=int((final_stats or {}).get("failed") or 0),
            http_status=http_status,
            trace_id=trace_id,
        )
        metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else None
        if metrics is not None:
            metrics["trace_id"] = trace_id
            for key in ("retry_after_seconds", "queue_depth_hint", "throttle_bucket"):
                if key in metrics:
                    result[key] = metrics[key]
        return result

    base_url = str(session.get("base_url") or "").strip()
    access_token = str(session.get("access_token") or "").strip()
    refresh_token = str(session.get("refresh_token") or "").strip()
    if not base_url or not access_token:
        return finish(
            {"ok": False, "message": "未登录云端", "outbox": queue.stats(), "metrics": _flush_metrics(started_at, 0, 0)},
            batch_size=0,
            http_status=None,
        )

    pending = queue.pending(limit=limit)
    if not pending:
        return finish(
            {"ok": True, "message": "没有待上传数据", "outbox": queue.stats(), "metrics": _flush_metrics(started_at, 0, 0)},
            batch_size=0,
            http_status=None,
        )
    pending, obsolete_profile_keys = _collapse_profile_update_events(pending)
    if obsolete_profile_keys:
        queue.mark_sent(obsolete_profile_keys)

    identity = cloud_session_identity(session)
    failure_key = f"flush_cloud_outbox:{base_url}:{identity['workspace_id']}"
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
        response = _post_events_with_trace(target_client, access_token, events, trace_id=trace_id)
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
                    record_consecutive_failure(
                        failure_key,
                        operation="flush_cloud_outbox",
                        error="刷新后无 access_token",
                    )
                    queue.mark_failed(event_keys, "未登录云端")
                    outbox_stats = queue.stats()
                    _maybe_warn_outbox_backlog(outbox_stats, base_url, identity)
                    return finish(
                        {"ok": False, "message": "未登录云端", "outbox": outbox_stats, "metrics": _flush_metrics(started_at, len(events), 0)},
                        batch_size=len(events),
                        http_status=401,
                    )
            except CloudSessionChangedError as changed_exc:
                return finish(
                    {"ok": False, "message": str(changed_exc), "outbox": queue.stats(), "metrics": _flush_metrics(started_at, len(events), 0)},
                    batch_size=len(events),
                    http_status=401,
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
                        "云端会话已过期（刷新令牌 401）",
                        level="warning",
                        event_key=f"cloud_session_expired:{base_url}:{identity['workspace_id']}:{identity['user_id']}",
                        throttle_seconds=600,
                        details={"base_url": base_url, "workspace_id": identity["workspace_id"]},
                        suggestion="请重新登录云端",
                    )
                record_consecutive_failure(
                    failure_key,
                    operation="flush_cloud_outbox",
                    error=str(refresh_exc),
                )
                queue.mark_failed(event_keys, str(refresh_exc), retry_after_seconds=refresh_exc.retry_after_seconds)
                outbox_stats = queue.stats()
                _maybe_warn_outbox_backlog(outbox_stats, base_url, identity)
                return finish(
                    {
                        "ok": False,
                        "message": str(refresh_exc),
                        "outbox": outbox_stats,
                        "metrics": _flush_metrics(started_at, len(events), 0, error=refresh_exc),
                    },
                    batch_size=len(events),
                    http_status=refresh_exc.status_code,
                )
            try:
                response = _post_events_with_trace(target_client, refreshed_access_token, events, trace_id=trace_id)
            except CloudClientError as refresh_exc:
                if refresh_exc.status_code == 401:
                    store.clear_if_current(
                        base_url=base_url,
                        access_token=refreshed_access_token,
                        refresh_token=str(refreshed_session.get("refresh_token") or "").strip(),
                        workspace_id=identity["workspace_id"],
                        user_id=identity["user_id"],
                    )
                    record_event_safe(
                        "cloud_sync",
                        "云端会话已过期（刷新后仍 401）",
                        level="warning",
                        event_key=f"cloud_session_expired:{base_url}:{identity['workspace_id']}:{identity['user_id']}",
                        throttle_seconds=600,
                        details={"base_url": base_url, "workspace_id": identity["workspace_id"]},
                        suggestion="请重新登录云端",
                    )
                record_consecutive_failure(
                    failure_key,
                    operation="flush_cloud_outbox",
                    error=str(refresh_exc),
                )
                queue.mark_failed(event_keys, str(refresh_exc), retry_after_seconds=refresh_exc.retry_after_seconds)
                outbox_stats = queue.stats()
                _maybe_warn_outbox_backlog(outbox_stats, base_url, identity)
                return finish(
                    {
                        "ok": False,
                        "message": str(refresh_exc),
                        "outbox": outbox_stats,
                        "metrics": _flush_metrics(started_at, len(events), 0, error=refresh_exc),
                    },
                    batch_size=len(events),
                    http_status=refresh_exc.status_code,
                )
        else:
            record_consecutive_failure(
                failure_key,
                operation="flush_cloud_outbox",
                error=str(exc),
            )
            queue.mark_failed(event_keys, str(exc), retry_after_seconds=exc.retry_after_seconds)
            outbox_stats = queue.stats()
            _maybe_warn_outbox_backlog(outbox_stats, base_url, identity)
            return finish(
                {
                    "ok": False,
                    "message": str(exc),
                    "outbox": outbox_stats,
                    "metrics": _flush_metrics(started_at, len(events), 0, error=exc),
                },
                batch_size=len(events),
                http_status=exc.status_code,
            )

    queue.mark_sent(event_keys)
    clear_consecutive_failure(failure_key)
    outbox_stats = queue.stats()
    _maybe_warn_outbox_backlog(outbox_stats, base_url, identity)
    response_status = _cloud_response_status(response)
    return finish(
        {
            "ok": True,
            "message": "上传完成",
            "response": response if isinstance(response, dict) else {},
            "outbox": outbox_stats,
            "metrics": _flush_metrics(started_at, len(events), int((response or {}).get("accepted") or 0) if isinstance(response, dict) else 0),
        },
        batch_size=len(events),
        http_status=response_status,
    )


_OUTBOX_BACKLOG_THRESHOLD = 100


def _cloud_response_status(response: Any) -> int:
    if isinstance(response, dict):
        for key in ("http_status", "status_code", "status"):
            value = response.get(key)
            try:
                if value is not None:
                    return int(value)
            except Exception:
                continue
    return 200


def _post_events_with_trace(client: Any, access_token: str, events: list[dict[str, Any]], *, trace_id: str) -> dict[str, Any]:
    try:
        return client.post_events(access_token, events, trace_id=trace_id)
    except TypeError as exc:
        if "trace_id" not in str(exc):
            raise
        return client.post_events(access_token, events)


def _log_cloud_outbox_flush(
    *,
    started_at: float,
    batch_size: int,
    pending_before: int,
    pending_after: int,
    failed_count: int,
    http_status: int | str | None,
    trace_id: str,
) -> None:
    elapsed_ms = max(0, int(round((time.monotonic() - started_at) * 1000)))
    status_text = "none" if http_status is None else str(http_status)
    print(
        "[CloudOutbox] flush "
        f"trace_id={trace_id} "
        f"batch_size={max(0, int(batch_size or 0))} "
        f"elapsed_ms={elapsed_ms} "
        f"pending_before={max(0, int(pending_before or 0))} "
        f"pending_after={max(0, int(pending_after or 0))} "
        f"failed_count={max(0, int(failed_count or 0))} "
        f"http_status={status_text}"
    )


def _maybe_warn_outbox_backlog(stats: dict[str, Any], base_url: str, identity: dict[str, Any]) -> None:
    backlog = stats.get("pending", 0) + stats.get("failed", 0)
    if backlog < _OUTBOX_BACKLOG_THRESHOLD:
        return
    record_event_safe(
        "cloud_sync",
        f"Outbox 堆积：{backlog} 条待处理",
        level="warning",
        event_key=f"outbox_backlog:{base_url}:{identity.get('workspace_id', '')}",
        throttle_seconds=600,
        details={
            "backlog": backlog,
            "pending": stats.get("pending", 0),
            "failed": stats.get("failed", 0),
            "dead_letter": stats.get("dead_letter", 0),
        },
        suggestion="检查云端连接状态或手动清理 outbox",
    )


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


def _flush_metrics(
    started_at: float,
    event_count: int,
    accepted_count: int,
    *,
    error: CloudClientError | None = None,
) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "duration_ms": max(0, int(round((time.monotonic() - started_at) * 1000))),
        "event_count": max(0, int(event_count or 0)),
        "accepted_count": max(0, int(accepted_count or 0)),
        "request_count": 1 if int(event_count or 0) > 0 else 0,
    }
    if error is not None:
        if error.retry_after_seconds is not None:
            metrics["retry_after_seconds"] = max(0.0, float(error.retry_after_seconds))
        if error.queue_depth_hint is not None:
            metrics["queue_depth_hint"] = max(0, int(error.queue_depth_hint))
        if error.throttle_bucket:
            metrics["throttle_bucket"] = error.throttle_bucket
    return metrics


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
