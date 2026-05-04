from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from core.app_paths import resolve_app_path
from core.article_reference_ranking import (
    ALGORITHM_VERSION,
    build_article_reference_bucket_snapshot,
)
from core.article_store import get_articles_file_signature
from core.history import get_records_file_signature
from core.local_account_space import account_scoped_path
from core.time_utils import local_now

DEFAULT_INDEX_DIR = resolve_app_path("logs/article_reference_index")
INDEX_DIR = DEFAULT_INDEX_DIR
INDEX_FILE_VERSION = 1
_INDEX_LOCK = threading.RLock()


def build_reference_index_source_signature(task_name: str = "", task_id: str = "") -> dict[str, Any]:
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "history": [list(item) for item in get_records_file_signature(task_name, task_id=task_id)],
        "articles": list(get_articles_file_signature()),
    }


def get_reference_index_file_signature(task_name: str = "", task_id: str = "") -> tuple[str, int, int]:
    path = _index_path(task_name, task_id)
    try:
        stat = path.stat()
        return (str(path), int(stat.st_mtime_ns), int(stat.st_size))
    except FileNotFoundError:
        return (str(path), 0, 0)
    except Exception:
        return (str(path), -1, -1)


def load_reference_index_snapshot(
    task_name: str = "",
    task_id: str = "",
    *,
    source_signature: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    payload = _load_index_payload(task_name, task_id)
    if not payload:
        return None
    if str(payload.get("algorithm_version") or "").strip() != ALGORITHM_VERSION:
        return None
    if source_signature is not None and payload.get("source_signature") != source_signature:
        return None
    return _payload_to_snapshot(payload)


def rebuild_reference_index_snapshot(
    records: list[dict[str, Any]] | None,
    *,
    article_urls: list[str] | set[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    return build_article_reference_bucket_snapshot(records, article_urls=article_urls)


def save_reference_index_snapshot(
    task_name: str,
    task_id: str,
    snapshot: dict[str, Any],
    *,
    source_signature: dict[str, Any],
) -> None:
    payload = _snapshot_to_payload(
        snapshot,
        task_name=task_name,
        task_id=task_id,
        source_signature=source_signature,
    )
    _save_index_payload(task_name, task_id, payload)


def update_reference_index_with_records(
    task_name: str,
    task_id: str,
    records: list[dict[str, Any]] | None,
    *,
    previous_source_signature: dict[str, Any] | None,
    current_source_signature: dict[str, Any] | None,
    article_urls: list[str] | set[str] | tuple[str, ...] | None = None,
) -> bool:
    if not records:
        return False

    snapshot = rebuild_reference_index_snapshot(records, article_urls=article_urls)
    with _INDEX_LOCK:
        payload = _load_index_payload(task_name, task_id)
        if payload and payload.get("source_signature") == previous_source_signature:
            existing_snapshot = _payload_to_snapshot(payload)
            _merge_snapshots(existing_snapshot, snapshot)
            _save_index_payload(
                task_name,
                task_id,
                _snapshot_to_payload(
                    existing_snapshot,
                    task_name=task_name,
                    task_id=task_id,
                    source_signature=current_source_signature or payload.get("source_signature") or {},
                ),
            )
            return True

        if payload is None and _is_empty_source_signature(previous_source_signature):
            _save_index_payload(
                task_name,
                task_id,
                _snapshot_to_payload(
                    snapshot,
                    task_name=task_name,
                    task_id=task_id,
                    source_signature=current_source_signature or {},
                ),
            )
            return True

    return False


def _index_storage_key(task_name: str = "", task_id: str = "") -> str:
    storage_key = str(task_id or "").strip() or str(task_name or "").strip() or "__default__"
    return hashlib.sha1(storage_key.encode("utf-8")).hexdigest()[:20]


def _index_path(task_name: str = "", task_id: str = "") -> Path:
    index_dir = _index_dir()
    index_dir.mkdir(parents=True, exist_ok=True)
    return index_dir / f"{_index_storage_key(task_name, task_id)}.json"


def _index_dir() -> Path:
    resolved_default = resolve_app_path("logs/article_reference_index")
    if INDEX_DIR != resolved_default:
        return INDEX_DIR
    return account_scoped_path("logs/article_reference_index", fallback=resolved_default)


def _load_index_payload(task_name: str, task_id: str) -> dict[str, Any] | None:
    path = _index_path(task_name, task_id)
    try:
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if isinstance(payload, dict) and payload.get("version") == INDEX_FILE_VERSION:
            return payload
    except Exception:
        return None
    return None


def _save_index_payload(task_name: str, task_id: str, payload: dict[str, Any]) -> None:
    path = _index_path(task_name, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _INDEX_LOCK:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            Path(tmp).replace(path)
        except BaseException:
            try:
                Path(tmp).unlink(missing_ok=True)
            except Exception:
                pass
            raise


def _snapshot_to_payload(
    snapshot: dict[str, Any],
    *,
    task_name: str,
    task_id: str,
    source_signature: dict[str, Any],
) -> dict[str, Any]:
    buckets_by_url = snapshot.get("buckets_by_url") if isinstance(snapshot.get("buckets_by_url"), dict) else {}
    payload_buckets: dict[str, dict[str, Any]] = {}
    for url, url_buckets in buckets_by_url.items():
        if not isinstance(url_buckets, dict):
            continue
        serialized_buckets: dict[str, Any] = {}
        for bucket_key, bucket in url_buckets.items():
            if not isinstance(bucket, dict):
                continue
            platform_id = str(bucket.get("platform") or "").strip()
            day_text = str(bucket.get("day") or "").strip()
            key = f"{platform_id}|{day_text}"
            serialized_buckets[key] = {
                "url": str(bucket.get("url") or url).strip(),
                "platform": platform_id,
                "day": day_text,
                "record_ids": sorted(
                    str(record_id or "").strip()
                    for record_id in (bucket.get("record_ids") or set())
                    if str(record_id or "").strip()
                ),
                "timestamps": sorted(
                    str(ts or "").strip()
                    for ts in (bucket.get("timestamps") or [])
                    if str(ts or "").strip()
                ),
            }
        if serialized_buckets:
            payload_buckets[str(url)] = serialized_buckets

    return {
        "version": INDEX_FILE_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "task_name": str(task_name or "").strip(),
        "task_id": str(task_id or "").strip(),
        "source_signature": source_signature,
        "updated_at": local_now().strftime("%Y-%m-%d %H:%M:%S"),
        "buckets_by_url": payload_buckets,
    }


def _payload_to_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    buckets_by_url = payload.get("buckets_by_url") if isinstance(payload.get("buckets_by_url"), dict) else {}
    snapshot_buckets: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    for url, url_buckets in buckets_by_url.items():
        if not isinstance(url_buckets, dict):
            continue
        restored_url_buckets: dict[tuple[str, str], dict[str, Any]] = {}
        for bucket_key, bucket in url_buckets.items():
            if not isinstance(bucket, dict):
                continue
            platform_id = str(bucket.get("platform") or "").strip()
            day_text = str(bucket.get("day") or "").strip()
            if not platform_id and isinstance(bucket_key, str) and "|" in bucket_key:
                platform_id = bucket_key.split("|", 1)[0].strip()
            if not day_text and isinstance(bucket_key, str) and "|" in bucket_key:
                day_text = bucket_key.split("|", 1)[1].strip()
            if not platform_id or not day_text:
                continue
            restored_url_buckets[(platform_id, day_text)] = {
                "url": str(bucket.get("url") or url).strip(),
                "platform": platform_id,
                "day": day_text,
                "record_ids": {
                    str(record_id or "").strip()
                    for record_id in (bucket.get("record_ids") or [])
                    if str(record_id or "").strip()
                },
                "timestamps": [
                    str(ts or "").strip()
                    for ts in (bucket.get("timestamps") or [])
                    if str(ts or "").strip()
                ],
            }
        if restored_url_buckets:
            snapshot_buckets[str(url)] = restored_url_buckets
    return {
        "algorithm_version": payload.get("algorithm_version") or ALGORITHM_VERSION,
        "allowed_urls": None,
        "buckets_by_url": snapshot_buckets,
    }


def _merge_snapshots(
    target: dict[str, Any],
    source: dict[str, Any],
) -> None:
    target_buckets = target.get("buckets_by_url") if isinstance(target.get("buckets_by_url"), dict) else {}
    source_buckets = source.get("buckets_by_url") if isinstance(source.get("buckets_by_url"), dict) else {}
    for url, url_buckets in source_buckets.items():
        if not isinstance(url_buckets, dict):
            continue
        target_url_buckets = target_buckets.setdefault(url, {})
        for bucket_key, bucket in url_buckets.items():
            if not isinstance(bucket, dict):
                continue
            platform_id = str(bucket.get("platform") or "").strip()
            day_text = str(bucket.get("day") or "").strip()
            key = (platform_id, day_text)
            target_bucket = target_url_buckets.setdefault(
                key,
                {
                    "url": str(bucket.get("url") or url).strip(),
                    "platform": platform_id,
                    "day": day_text,
                    "record_ids": set(),
                    "timestamps": [],
                },
            )
            for record_id in bucket.get("record_ids") or []:
                record_text = str(record_id or "").strip()
                if record_text:
                    target_bucket.setdefault("record_ids", set()).add(record_text)
            timestamps = target_bucket.setdefault("timestamps", [])
            for ts in bucket.get("timestamps") or []:
                ts_text = str(ts or "").strip()
                if ts_text and ts_text not in timestamps:
                    timestamps.append(ts_text)


def _is_empty_source_signature(source_signature: dict[str, Any] | None) -> bool:
    if not isinstance(source_signature, dict):
        return False
    history = source_signature.get("history")
    articles = source_signature.get("articles")
    if not isinstance(history, list) or not isinstance(articles, list):
        return False
    return all(_is_empty_signature_tuple(item) for item in history) and _is_empty_signature_tuple(articles)


def _is_empty_signature_tuple(value: Any) -> bool:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return False
    return str(value[0] or "").strip() and int(value[1] or 0) == 0 and int(value[2] or 0) == 0
