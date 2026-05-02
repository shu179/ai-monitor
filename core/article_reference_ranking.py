from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import date
from typing import Any

from core.history import normalize_platform_id
from core.reference_urls import extract_reference_urls_from_text, normalize_reference_url
from core.time_utils import parse_local_date

ALGORITHM_VERSION = "article_ref_weight_v1"
MANUAL_REFERENCE_PLATFORM_ID = "manual_reference"


def build_article_reference_ranking(
    records: list[dict[str, Any]] | None,
    *,
    article_urls: list[str] | set[str] | tuple[str, ...] | None = None,
    platform: str = "all",
    date_from: str = "",
    date_to: str = "",
) -> dict[str, Any]:
    allowed_urls = _normalize_allowed_urls(article_urls)
    start_date = parse_local_date(date_from)
    end_date = parse_local_date(date_to)
    platform_filter = _normalize_platform_filter(platform)

    buckets_by_url = _collect_reference_buckets(
        records or [],
        allowed_urls=allowed_urls,
        start_date=start_date,
        end_date=end_date,
    )
    available_platforms = _summarize_available_platforms(buckets_by_url)
    items, selected_buckets_by_url = _build_ranked_items(
        buckets_by_url,
        platform_filter=platform_filter,
    )
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "available_platforms": available_platforms,
        "items": items,
        "daily_points": _build_daily_points(selected_buckets_by_url),
    }


def build_article_reference_bucket_snapshot(
    records: list[dict[str, Any]] | None,
    *,
    article_urls: list[str] | set[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    allowed_urls = _normalize_allowed_urls(article_urls)
    buckets_by_url = _collect_reference_buckets(
        records or [],
        allowed_urls=allowed_urls,
        start_date=None,
        end_date=None,
    )
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "allowed_urls": allowed_urls,
        "buckets_by_url": buckets_by_url,
    }


def build_article_reference_ranking_from_snapshot(
    snapshot: dict[str, Any] | None,
    *,
    article_urls: list[str] | set[str] | tuple[str, ...] | None = None,
    platform: str = "all",
    date_from: str = "",
    date_to: str = "",
) -> dict[str, Any]:
    snapshot_data = snapshot if isinstance(snapshot, dict) else {}
    buckets_by_url = snapshot_data.get("buckets_by_url") if isinstance(snapshot_data.get("buckets_by_url"), dict) else {}
    allowed_urls = _normalize_allowed_urls(article_urls)
    start_date = parse_local_date(date_from)
    end_date = parse_local_date(date_to)
    platform_filter = _normalize_platform_filter(platform)

    url_filtered_buckets_by_url = _filter_buckets_by_url(
        buckets_by_url,
        allowed_urls=allowed_urls,
    )
    filtered_buckets_by_url = _filter_buckets_by_date(
        url_filtered_buckets_by_url,
        start_date=start_date,
        end_date=end_date,
    )
    available_platforms = _summarize_available_platforms(filtered_buckets_by_url)
    items, selected_buckets_by_url = _build_ranked_items(
        filtered_buckets_by_url,
        platform_filter=platform_filter,
    )
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "available_platforms": available_platforms,
        "items": items,
        "daily_points": _build_daily_points(selected_buckets_by_url),
    }


def _normalize_allowed_urls(article_urls: list[str] | set[str] | tuple[str, ...] | None) -> set[str] | None:
    if article_urls is None:
        return None
    urls = {
        normalize_reference_url(str(url or "").strip())
        for url in article_urls
        if str(url or "").strip()
    }
    return {url for url in urls if url}


def _normalize_platform_filter(platform: str) -> str:
    raw = str(platform or "all").strip().lower()
    if raw in {"", "all"}:
        return "all"
    if raw == "cross":
        return "cross"
    return normalize_platform_id(raw)


def _collect_reference_buckets(
    records: list[dict[str, Any]],
    *,
    allowed_urls: set[str] | None,
    start_date: date | None,
    end_date: date | None,
) -> dict[str, dict[tuple[str, str], dict[str, Any]]]:
    buckets_by_url: dict[str, dict[tuple[str, str], dict[str, Any]]] = defaultdict(dict)
    for index, record in enumerate(records):
        if not isinstance(record, dict) or _is_test_noise_record(record):
            continue
        record_day = _record_day(record)
        if record_day is None:
            continue
        if start_date and record_day < start_date:
            continue
        if end_date and record_day > end_date:
            continue

        urls = _extract_record_reference_urls(record)
        if allowed_urls is not None:
            urls = [url for url in urls if url in allowed_urls]
        if not urls:
            continue

        platforms = _extract_record_platforms(record)
        if not platforms:
            continue

        record_id = _record_identity(record, index)
        record_ts = str(record.get("ts") or record_day.isoformat()).strip() or record_day.isoformat()
        day_text = record_day.isoformat()
        for url in urls:
            url_buckets = buckets_by_url[url]
            for platform in platforms:
                key = (platform, day_text)
                bucket = url_buckets.setdefault(
                    key,
                    {
                        "url": url,
                        "platform": platform,
                        "day": day_text,
                        "record_ids": set(),
                        "timestamps": [],
                    },
                )
                bucket["record_ids"].add(record_id)
                timestamps = bucket["timestamps"]
                if record_ts not in timestamps:
                    timestamps.append(record_ts)
    return buckets_by_url


def _filter_buckets_by_url(
    buckets_by_url: dict[str, dict[tuple[str, str], dict[str, Any]]],
    *,
    allowed_urls: set[str] | None,
) -> dict[str, dict[tuple[str, str], dict[str, Any]]]:
    if allowed_urls is None:
        return buckets_by_url
    return {
        str(url): url_buckets
        for url, url_buckets in buckets_by_url.items()
        if str(url) in allowed_urls
    }


def _filter_buckets_by_date(
    buckets_by_url: dict[str, dict[tuple[str, str], dict[str, Any]]],
    *,
    start_date: date | None,
    end_date: date | None,
) -> dict[str, dict[tuple[str, str], dict[str, Any]]]:
    if not start_date and not end_date:
        return buckets_by_url

    filtered: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    for url, url_buckets in buckets_by_url.items():
        selected = {
            key: bucket
            for key, bucket in url_buckets.items()
            if _bucket_day_in_range(str(bucket.get("day") or "").strip(), start_date=start_date, end_date=end_date)
        }
        if selected:
            filtered[url] = selected
    return filtered


def _bucket_day_in_range(day_text: str, *, start_date: date | None, end_date: date | None) -> bool:
    day = parse_local_date(day_text)
    if day is None:
        return False
    if start_date and day < start_date:
        return False
    if end_date and day > end_date:
        return False
    return True


def _is_test_noise_record(record: dict[str, Any]) -> bool:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    source_values = (
        record.get("execution_source"),
        record.get("source"),
        record.get("source_mode"),
        record.get("scope"),
        extra.get("execution_source"),
        extra.get("source"),
        extra.get("source_mode"),
        extra.get("scope"),
    )
    return any(_is_test_source(value) for value in source_values)


def _is_test_source(value: Any) -> bool:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return text in {"manual_test", "test"}


def _record_day(record: dict[str, Any]) -> date | None:
    return parse_local_date(record.get("ts"))


def _record_identity(record: dict[str, Any], index: int) -> str:
    record_id = str(record.get("id") or "").strip()
    if record_id:
        return record_id
    payload = {
        "index": index,
        "ts": str(record.get("ts") or "").strip(),
        "task_id": str(record.get("task_id") or "").strip(),
        "task_name": str(record.get("task_name") or "").strip(),
        "platform": str(record.get("platform") or "").strip(),
        "keyword": str(record.get("keyword") or "").strip(),
        "brand": str(record.get("brand") or "").strip(),
        "mode": str(record.get("mode") or "").strip(),
    }
    digest = hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return f"raw:{digest}"


def _extract_record_reference_urls(record: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def append_url(value: Any) -> None:
        normalized = normalize_reference_url(str(value or "").strip())
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        urls.append(normalized)

    for item in _iter_reference_values(record, "references"):
        append_url(item)
    for item in _iter_reference_values(record, "body_references"):
        append_url(item)

    if not urls:
        for url in extract_reference_urls_from_text(str(record.get("answer_text") or "")):
            append_url(url)
    return urls


def _iter_reference_values(record: dict[str, Any], key: str) -> list[Any]:
    values: list[Any] = []
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    for source in (extra.get(key), record.get(key)):
        if not isinstance(source, list):
            continue
        for item in source:
            if isinstance(item, dict):
                for candidate_key in ("url", "link", "href"):
                    candidate = str(item.get(candidate_key) or "").strip()
                    if candidate:
                        values.append(candidate)
                        break
            elif isinstance(item, str):
                values.append(item)
    return values


def _extract_record_platforms(record: dict[str, Any]) -> list[str]:
    mode = str(record.get("mode") or "").strip().lower()
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    raw_platforms: list[Any] = []

    if mode == "recognition":
        detected = extra.get("detected_platforms")
        if isinstance(detected, list):
            raw_platforms.extend(detected)
    else:
        raw_platforms.append(record.get("platform"))

    platforms: list[str] = []
    seen: set[str] = set()
    for value in raw_platforms:
        normalized = normalize_platform_id(str(value or "").strip())
        if not normalized or normalized == "recognition" or normalized in seen:
            continue
        seen.add(normalized)
        platforms.append(normalized)
    return platforms


def _summarize_available_platforms(
    buckets_by_url: dict[str, dict[tuple[str, str], dict[str, Any]]],
) -> list[dict[str, Any]]:
    platform_stats: dict[str, dict[str, int]] = {}
    for url_buckets in buckets_by_url.values():
        for bucket in url_buckets.values():
            platform_id = str(bucket.get("platform") or "").strip()
            if not platform_id or platform_id == MANUAL_REFERENCE_PLATFORM_ID:
                continue
            stats = platform_stats.setdefault(platform_id, {"raw_count": 0, "event_count": 0})
            stats["raw_count"] += len(bucket.get("record_ids") or set())
            stats["event_count"] += 1
    return [
        {"id": platform_id, **stats}
        for platform_id, stats in sorted(
            platform_stats.items(),
            key=lambda pair: (-pair[1]["event_count"], -pair[1]["raw_count"], pair[0]),
        )
    ]


def _build_ranked_items(
    buckets_by_url: dict[str, dict[tuple[str, str], dict[str, Any]]],
    *,
    platform_filter: str,
) -> tuple[list[dict[str, Any]], dict[str, dict[tuple[str, str], dict[str, Any]]]]:
    items: list[dict[str, Any]] = []
    selected_buckets_by_url: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}

    for url, url_buckets in buckets_by_url.items():
        filtered_buckets = {
            key: bucket
            for key, bucket in url_buckets.items()
            if platform_filter in {"all", "cross"} or str(bucket.get("platform") or "") == platform_filter
        }
        if not filtered_buckets:
            continue

        item = _build_item_from_buckets(url, filtered_buckets)
        if platform_filter == "cross" and int(item.get("platform_count") or 0) < 2:
            continue
        selected_buckets_by_url[url] = filtered_buckets
        items.append(item)

    items.sort(key=lambda item: str(item.get("url") or ""))
    items.sort(key=lambda item: str(item.get("last_referenced_at") or ""), reverse=True)
    items.sort(key=lambda item: int(item.get("raw_ref_count") or 0), reverse=True)
    items.sort(key=lambda item: int(item.get("effective_event_count") or 0), reverse=True)
    items.sort(key=lambda item: float(item.get("_score_raw") or 0.0), reverse=True)

    for index, item in enumerate(items, start=1):
        item["rank"] = index
        item.pop("_score_raw", None)
    return items, selected_buckets_by_url


def _build_item_from_buckets(url: str, buckets: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    raw_ref_count = 0
    effective_event_count = 0
    weighted_mention_count = 0.0
    platform_stats: dict[str, dict[str, int]] = {}
    active_days: set[str] = set()
    timestamps: list[str] = []

    for bucket in buckets.values():
        platform_id = str(bucket.get("platform") or "").strip()
        day_text = str(bucket.get("day") or "").strip()
        record_ids = bucket.get("record_ids") or set()
        raw_count = len(record_ids)
        raw_ref_count += raw_count
        effective_event_count += 1
        weighted_mention_count += 1.0 + math.log1p(max(0, raw_count - 1))
        if day_text:
            active_days.add(day_text)
        for ts in bucket.get("timestamps") or []:
            ts_text = str(ts or "").strip()
            if ts_text:
                timestamps.append(ts_text)
        if platform_id and platform_id != MANUAL_REFERENCE_PLATFORM_ID:
            stats = platform_stats.setdefault(platform_id, {"raw_count": 0, "event_count": 0})
            stats["raw_count"] += raw_count
            stats["event_count"] += 1

    sorted_days = sorted(active_days)
    first_day = parse_local_date(sorted_days[0]) if sorted_days else None
    last_day = parse_local_date(sorted_days[-1]) if sorted_days else None
    span_days = max(1, (last_day - first_day).days + 1) if first_day and last_day else 1
    platform_count = len(platform_stats)
    score = _calculate_score(
        weighted_mention_count=weighted_mention_count,
        active_days=len(active_days),
        span_days=span_days,
        platform_count=platform_count,
    )
    platforms = [
        {"id": platform_id, **stats}
        for platform_id, stats in sorted(
            platform_stats.items(),
            key=lambda pair: (-pair[1]["event_count"], -pair[1]["raw_count"], pair[0]),
        )
    ]
    sorted_timestamps = sorted(set(timestamps))
    return {
        "rank": 0,
        "url": url,
        "score": round(score, 6),
        "_score_raw": score,
        "raw_ref_count": raw_ref_count,
        "effective_event_count": effective_event_count,
        "weighted_mention_count": round(weighted_mention_count, 6),
        "active_days": len(active_days),
        "span_days": span_days,
        "platform_count": platform_count,
        "platforms": platforms,
        "first_referenced_at": sorted_timestamps[0] if sorted_timestamps else (sorted_days[0] if sorted_days else ""),
        "last_referenced_at": sorted_timestamps[-1] if sorted_timestamps else (sorted_days[-1] if sorted_days else ""),
    }


def _calculate_score(
    *,
    weighted_mention_count: float,
    active_days: int,
    span_days: int,
    platform_count: int,
) -> float:
    volume = math.log1p(max(0.0, weighted_mention_count))
    persistence = math.log1p(max(0, active_days))
    spread = math.log1p(max(1, span_days))
    platform_bonus = 1.0 + min(0.60, 0.15 * max(0, platform_count - 1))
    return (0.45 * volume + 0.30 * persistence + 0.25 * spread) * platform_bonus


def _build_daily_points(
    selected_buckets_by_url: dict[str, dict[tuple[str, str], dict[str, Any]]],
) -> list[dict[str, Any]]:
    days: dict[str, dict[str, Any]] = {}
    for url, url_buckets in selected_buckets_by_url.items():
        for bucket in url_buckets.values():
            day_text = str(bucket.get("day") or "").strip()
            if not day_text:
                continue
            item = days.setdefault(day_text, {"date": day_text, "article_urls": set(), "event_count": 0, "raw_count": 0})
            item["article_urls"].add(url)
            item["event_count"] += 1
            item["raw_count"] += len(bucket.get("record_ids") or set())

    points: list[dict[str, Any]] = []
    for day_text, item in sorted(days.items()):
        points.append({
            "date": day_text,
            "article_count": len(item.get("article_urls") or set()),
            "event_count": int(item.get("event_count") or 0),
            "raw_count": int(item.get("raw_count") or 0),
        })
    return points
