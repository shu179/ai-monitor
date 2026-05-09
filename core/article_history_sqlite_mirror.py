from __future__ import annotations

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

from . import article_store, history
from .app_paths import resolve_app_path
from .article_history_sqlite_store import ArticleHistorySQLiteStore
from .config_watcher import load_config
from .daily_task_state import derive_task_id
from .local_account_space import account_scoped_path, current_account_config_path
from .time_utils import local_today, parse_local_date


DEFAULT_SHADOW_DB_FILE = resolve_app_path("logs/article_history_shadow.sqlite3")


def default_shadow_db_path() -> Path:
    return account_scoped_path("logs/article_history_shadow.sqlite3", fallback=DEFAULT_SHADOW_DB_FILE)


def rebuild_shadow_store(
    db_path: str | Path | None = None,
    *,
    max_workers: int | None = None,
    verify_tail_limit: int = 20,
) -> dict[str, Any]:
    """Rebuild the structured SQLite shadow store from the current JSON stores."""
    target_db_path = Path(db_path) if db_path is not None else default_shadow_db_path()
    source = load_json_source(max_workers=max_workers)
    store = ArticleHistorySQLiteStore(
        target_db_path,
        normalize_article_url=article_store.normalize_article_url,
    )

    store.clear_all()
    article_result = store.import_articles(source["articles"], replace=False)
    history_result = store.import_history_sources(source["history"], replace=False)

    verification = verify_shadow_store(
        target_db_path,
        source=source,
        tail_limit=verify_tail_limit,
    )
    return {
        "db_path": str(target_db_path),
        "articles": article_result,
        "history": {
            "storage_keys": history_result["storage_keys"],
            "records": history_result["created"] + history_result["updated"],
            "created": history_result["created"],
            "updated": history_result["updated"],
            "skipped": history_result["skipped"],
            "details": history_result["details"],
        },
        "verification": verification,
    }


def verify_shadow_store(
    db_path: str | Path | None = None,
    *,
    source: dict[str, Any] | None = None,
    max_workers: int | None = None,
    tail_limit: int = 20,
) -> dict[str, Any]:
    target_db_path = Path(db_path) if db_path is not None else default_shadow_db_path()
    expected_source = source if source is not None else load_json_source(max_workers=max_workers)
    store = ArticleHistorySQLiteStore(
        target_db_path,
        normalize_article_url=article_store.normalize_article_url,
    )

    expected_articles = [item for item in expected_source.get("articles", []) if isinstance(item, dict)]
    expected_article_total = len(expected_articles)
    expected_url_total = _normalized_article_url_count(expected_articles)
    sqlite_article_page = store.get_article_page(limit=1)
    sqlite_article_total = int(sqlite_article_page.get("total") or 0)
    sqlite_task_counts = store.get_article_task_counts(relation="matched")
    expected_task_counts = _article_task_counts(expected_articles, relation_field="matched_tasks")

    history_sources = expected_source.get("history") if isinstance(expected_source.get("history"), dict) else {}
    expected_history_keys = sorted(str(key) for key in history_sources.keys())
    sqlite_history_keys = store.list_history_storage_keys()
    history_mismatches: dict[str, dict[str, Any]] = {}

    for storage_key in sorted(set(expected_history_keys) | set(sqlite_history_keys)):
        expected_records = [
            item for item in (history_sources.get(storage_key) or [])
            if isinstance(item, dict)
        ]
        sqlite_count = store.get_history_record_count(storage_key)
        expected_tail = _history_tail_signature(expected_records, tail_limit)
        sqlite_tail = _history_tail_signature(store.get_history_tail(storage_key, limit=tail_limit), tail_limit)
        if sqlite_count != len(expected_records) or sqlite_tail != expected_tail:
            history_mismatches[storage_key] = {
                "expected_count": len(expected_records),
                "sqlite_count": sqlite_count,
                "expected_tail": expected_tail,
                "sqlite_tail": sqlite_tail,
            }

    article_mismatches = {
        "expected_total": expected_article_total,
        "sqlite_total": sqlite_article_total,
        "expected_normalized_url_total": expected_url_total,
        "expected_task_counts": expected_task_counts,
        "sqlite_task_counts": sqlite_task_counts,
    }
    article_ok = (
        sqlite_article_total == expected_url_total
        and _counts_subset_match(expected_task_counts, sqlite_task_counts)
    )
    history_ok = expected_history_keys == sqlite_history_keys and not history_mismatches
    return {
        "ok": bool(article_ok and history_ok),
        "articles": article_mismatches,
        "history": {
            "expected_storage_keys": expected_history_keys,
            "sqlite_storage_keys": sqlite_history_keys,
            "mismatches": history_mismatches,
        },
    }


def compare_article_pages(
    db_path: str | Path | None = None,
    *,
    config: dict[str, Any] | None = None,
    articles: list[dict[str, Any]] | None = None,
    limit: int = 50,
    max_workers: int | None = None,
    rebuild: bool = False,
) -> dict[str, Any]:
    target_db_path = Path(db_path) if db_path is not None else default_shadow_db_path()
    resolved_config = config if config is not None else _load_current_config()
    source_articles = articles if articles is not None else article_store.refresh_article_matches(resolved_config)
    normalized_articles = [item for item in source_articles if isinstance(item, dict)]
    resolved_limit = max(1, min(500, int(limit or 50)))
    queries = build_article_compare_queries(resolved_config)

    store = ArticleHistorySQLiteStore(
        target_db_path,
        normalize_article_url=article_store.normalize_article_url,
    )
    rebuild_result = None
    if rebuild:
        rebuild_result = store.import_articles(normalized_articles, replace=True)

    worker_count = _article_compare_worker_count(queries, max_workers=max_workers)
    if worker_count <= 1:
        results = [
            _compare_article_query(
                store,
                normalized_articles,
                query,
                limit=resolved_limit,
            )
            for query in queries
        ]
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(executor.map(
                lambda query: _compare_article_query(
                    store,
                    normalized_articles,
                    query,
                    limit=resolved_limit,
                ),
                queries,
            ))

    failed = [item for item in results if not bool(item.get("ok"))]
    return {
        "ok": not failed,
        "db_path": str(target_db_path),
        "limit": resolved_limit,
        "workers": worker_count,
        "query_count": len(results),
        "failed_count": len(failed),
        "rebuild": rebuild_result,
        "queries": results,
    }


def compare_history_records(
    db_path: str | Path | None = None,
    *,
    source: dict[str, list[dict[str, Any]]] | None = None,
    max_workers: int | None = None,
    limit: int = 50,
    sample_pages: int = 3,
    rebuild: bool = False,
) -> dict[str, Any]:
    """Compare JSON history files with structured SQLite history reads."""
    target_db_path = Path(db_path) if db_path is not None else default_shadow_db_path()
    history_sources = source if source is not None else load_json_history_sources(max_workers=max_workers)
    normalized_sources = {
        str(storage_key): _ordered_history_records(records)
        for storage_key, records in (history_sources or {}).items()
        if str(storage_key or "").strip()
    }
    resolved_limit = max(1, min(500, int(limit or 50)))
    resolved_sample_pages = max(0, min(20, int(sample_pages or 0)))
    store = ArticleHistorySQLiteStore(
        target_db_path,
        normalize_article_url=article_store.normalize_article_url,
    )
    rebuild_result = None
    if rebuild:
        rebuild_result = store.import_history_sources(normalized_sources, replace=True)

    expected_keys = sorted(normalized_sources.keys())
    sqlite_keys = store.list_history_storage_keys()
    all_keys = sorted(set(expected_keys) | set(sqlite_keys))
    worker_count = _history_compare_worker_count(all_keys, max_workers=max_workers)
    if worker_count <= 1:
        results = [
            _compare_history_storage_key(
                store,
                storage_key,
                normalized_sources.get(storage_key, []),
                limit=resolved_limit,
                sample_pages=resolved_sample_pages,
            )
            for storage_key in all_keys
        ]
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(executor.map(
                lambda storage_key: _compare_history_storage_key(
                    store,
                    storage_key,
                    normalized_sources.get(storage_key, []),
                    limit=resolved_limit,
                    sample_pages=resolved_sample_pages,
                ),
                all_keys,
            ))

    key_mismatches = []
    if expected_keys != sqlite_keys:
        key_mismatches.append("storage_keys")
    failed = [item for item in results if not bool(item.get("ok"))]
    return {
        "ok": not failed and not key_mismatches,
        "db_path": str(target_db_path),
        "limit": resolved_limit,
        "sample_pages": resolved_sample_pages,
        "workers": worker_count,
        "storage_key_count": len(all_keys),
        "failed_count": len(failed) + len(key_mismatches),
        "rebuild": rebuild_result,
        "keys": {
            "expected": expected_keys,
            "sqlite": sqlite_keys,
            "mismatches": key_mismatches,
        },
        "queries": results,
    }


def compare_history_task_reads(
    db_path: str | Path | None = None,
    *,
    config: dict[str, Any] | None = None,
    source: dict[str, list[dict[str, Any]]] | None = None,
    max_workers: int | None = None,
    limit: int = 50,
    sample_pages: int = 3,
    rebuild: bool = False,
) -> dict[str, Any]:
    """Compare config task history reads against the structured SQLite history table."""
    target_db_path = Path(db_path) if db_path is not None else default_shadow_db_path()
    resolved_config = config if config is not None else _load_current_config()
    history_sources = source if source is not None else load_json_history_sources(max_workers=max_workers)
    normalized_sources = {
        str(storage_key): _ordered_history_records(records)
        for storage_key, records in (history_sources or {}).items()
        if str(storage_key or "").strip()
    }
    resolved_limit = max(1, min(500, int(limit or 50)))
    resolved_sample_pages = max(0, min(20, int(sample_pages or 0)))
    queries = build_history_task_read_queries(resolved_config)
    store = ArticleHistorySQLiteStore(
        target_db_path,
        normalize_article_url=article_store.normalize_article_url,
    )
    rebuild_result = None
    if rebuild:
        rebuild_result = store.import_history_sources(normalized_sources, replace=True)

    worker_count = _history_compare_worker_count([item["name"] for item in queries], max_workers=max_workers)
    if worker_count <= 1:
        results = [
            _compare_history_task_read(
                store,
                normalized_sources,
                query,
                limit=resolved_limit,
                sample_pages=resolved_sample_pages,
            )
            for query in queries
        ]
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(executor.map(
                lambda query: _compare_history_task_read(
                    store,
                    normalized_sources,
                    query,
                    limit=resolved_limit,
                    sample_pages=resolved_sample_pages,
                ),
                queries,
            ))

    failed = [item for item in results if not bool(item.get("ok"))]
    return {
        "ok": not failed,
        "db_path": str(target_db_path),
        "limit": resolved_limit,
        "sample_pages": resolved_sample_pages,
        "workers": worker_count,
        "query_count": len(results),
        "failed_count": len(failed),
        "rebuild": rebuild_result,
        "queries": results,
    }


def build_article_compare_queries(config: dict[str, Any] | None) -> list[dict[str, str]]:
    queries: list[dict[str, str]] = [{"name": "all", "task_name": "", "media_type": ""}]
    media_types = (("media", "authority"), ("self-media", "selfmedia"))
    for public_media_type, _storage_media_type in media_types:
        queries.append({
            "name": f"media:{public_media_type}",
            "task_name": "",
            "media_type": public_media_type,
        })

    for task_name in _article_compare_task_names(config):
        queries.append({
            "name": f"task:{task_name}",
            "task_name": task_name,
            "media_type": "",
        })
        for public_media_type, _storage_media_type in media_types:
            queries.append({
                "name": f"task:{task_name}|media:{public_media_type}",
                "task_name": task_name,
                "media_type": public_media_type,
            })
    return queries


def build_history_task_read_queries(config: dict[str, Any] | None) -> list[dict[str, str]]:
    queries: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for task in (config or {}).get("tasks") or []:
        if not isinstance(task, dict):
            continue
        if bool(task.get("delete_pending")):
            continue
        task_id = str(task.get("task_id") or derive_task_id(task)).strip()
        task_name = str(task.get("name") or task_id).strip()
        if not task_id and not task_name:
            continue
        key = (task_id, task_name)
        if key in seen:
            continue
        seen.add(key)
        queries.append({
            "name": f"task:{task_name or task_id}",
            "task_id": task_id,
            "task_name": task_name,
        })
    return queries


def load_json_source(*, max_workers: int | None = None) -> dict[str, Any]:
    return {
        "articles": article_store.get_articles(),
        "history": load_json_history_sources(max_workers=max_workers),
    }


def load_json_history_sources(*, max_workers: int | None = None) -> dict[str, list[dict[str, Any]]]:
    files = _history_record_files()
    worker_count = _history_read_worker_count(files, max_workers=max_workers)
    if worker_count <= 1:
        items = [_read_history_file(path) for path in files]
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            items = list(executor.map(_read_history_file, files))
    return {
        storage_key: records
        for storage_key, records in items
        if storage_key and records
    }


def _history_record_files() -> list[Path]:
    history_dir = history.get_history_dir()
    if not history_dir.exists():
        return []
    return [
        path
        for path in sorted(history_dir.glob("*.json"))
        if _is_history_record_file(path)
    ]


def _is_history_record_file(path: Path) -> bool:
    stem = path.stem
    return not (stem.endswith("_rates") or stem.endswith("_periods") or "_trend_" in stem)


def _history_read_worker_count(files: list[Path], *, max_workers: int | None) -> int:
    if len(files) <= 1:
        return 1
    if max_workers is not None:
        return max(1, min(len(files), int(max_workers or 1)))
    cpu_count = os.cpu_count() or 2
    return max(1, min(len(files), cpu_count, 8))


def _load_current_config() -> dict[str, Any]:
    for config_path in (current_account_config_path(), resolve_app_path("config.yaml")):
        try:
            if config_path.exists():
                return load_config(str(config_path))
        except Exception:
            continue
    return {}


def _compare_article_query(
    store: ArticleHistorySQLiteStore,
    articles: list[dict[str, Any]],
    query: dict[str, str],
    *,
    limit: int,
) -> dict[str, Any]:
    media_type = str(query.get("media_type") or "").strip()
    storage_media_type = _storage_media_type(media_type)
    task_name = str(query.get("task_name") or "").strip()
    json_page = _json_article_page(articles, media_type=media_type, task_name=task_name, limit=limit)
    sqlite_page = store.get_article_page(
        limit=limit,
        task_name=task_name,
        media_type=storage_media_type,
    )
    sqlite_today_total = store.get_article_today_count(
        local_today().isoformat(),
        task_name=task_name,
        media_type=storage_media_type,
    )
    sqlite_ids = [
        str(item.get("id") or "").strip()
        for item in (sqlite_page.get("items") or [])
        if isinstance(item, dict)
    ]
    mismatches: list[str] = []
    if int(json_page["total"]) != int(sqlite_page.get("total") or 0):
        mismatches.append("total")
    if int(json_page["today_total"]) != int(sqlite_today_total):
        mismatches.append("today_total")
    if list(json_page["ids"]) != sqlite_ids:
        mismatches.append("article_ids")
    return {
        "ok": not mismatches,
        "name": str(query.get("name") or ""),
        "query": {
            "task_name": task_name,
            "media_type": media_type,
        },
        "mismatches": mismatches,
        "json": {
            "total": json_page["total"],
            "today_total": json_page["today_total"],
            "ids": json_page["ids"],
        },
        "sqlite": {
            "total": int(sqlite_page.get("total") or 0),
            "today_total": int(sqlite_today_total),
            "ids": sqlite_ids,
        },
    }


def _json_article_page(
    articles: list[dict[str, Any]],
    *,
    media_type: str,
    task_name: str,
    limit: int,
) -> dict[str, Any]:
    filtered = list(articles)
    if task_name:
        filtered = [article for article in filtered if task_name in (article.get("matched_tasks") or [])]
    storage_media_type = _storage_media_type(media_type)
    if storage_media_type:
        filtered = [article for article in filtered if article.get("media_type") == storage_media_type]
    deduped = _dedupe_articles_by_url(filtered)
    today = local_today()
    today_total = sum(1 for article in deduped if _article_published_date(article) == today)
    return {
        "total": len(deduped),
        "today_total": today_total,
        "ids": [
            str(article.get("id") or "").strip()
            for article in deduped[:limit]
        ],
    }


def _article_compare_task_names(config: dict[str, Any] | None) -> list[str]:
    names: list[str] = []
    for task in (config or {}).get("tasks") or []:
        if not isinstance(task, dict):
            continue
        if bool(task.get("delete_pending")):
            continue
        task_name = str(task.get("name") or "").strip()
        if task_name and task_name not in names:
            names.append(task_name)
    return names


def _article_compare_worker_count(queries: list[dict[str, str]], *, max_workers: int | None) -> int:
    if len(queries) <= 1:
        return 1
    if max_workers is not None:
        return max(1, min(len(queries), int(max_workers or 1)))
    cpu_count = os.cpu_count() or 2
    return max(1, min(len(queries), cpu_count, 8))


def _history_compare_worker_count(storage_keys: list[str], *, max_workers: int | None) -> int:
    if len(storage_keys) <= 1:
        return 1
    if max_workers is not None:
        return max(1, min(len(storage_keys), int(max_workers or 1)))
    cpu_count = os.cpu_count() or 2
    return max(1, min(len(storage_keys), cpu_count, 8))


def _compare_history_storage_key(
    store: ArticleHistorySQLiteStore,
    storage_key: str,
    expected_records: list[dict[str, Any]],
    *,
    limit: int,
    sample_pages: int,
) -> dict[str, Any]:
    ordered_expected = _ordered_history_records(expected_records)
    sqlite_count = store.get_history_record_count(storage_key)
    expected_count = len(ordered_expected)
    windows = []
    mismatches: list[str] = []
    if sqlite_count != expected_count:
        mismatches.append("count")
    for window in _history_compare_windows(expected_count, limit=limit, sample_pages=sample_pages):
        offset = int(window["offset"])
        window_limit = int(window["limit"])
        expected_window = ordered_expected[offset:offset + window_limit]
        sqlite_window = store.get_history_records(storage_key, limit=window_limit, offset=offset)
        expected_signature = _history_records_signature(expected_window)
        sqlite_signature = _history_records_signature(sqlite_window)
        window_mismatches = []
        if expected_signature != sqlite_signature:
            window_mismatches.append("records")
            if "records" not in mismatches:
                mismatches.append("records")
        window_report: dict[str, Any] = {
            "name": window["name"],
            "offset": offset,
            "limit": window_limit,
            "ok": not window_mismatches,
            "mismatches": window_mismatches,
            "json": {
                "count": len(expected_window),
            },
            "sqlite": {
                "count": len(sqlite_window),
            },
        }
        if window_mismatches:
            window_report["json_records"] = expected_signature
            window_report["sqlite_records"] = sqlite_signature
        windows.append(window_report)
    return {
        "ok": not mismatches,
        "name": storage_key,
        "mismatches": mismatches,
        "json": {
            "count": expected_count,
        },
        "sqlite": {
            "count": sqlite_count,
        },
        "windows": windows,
    }


def _compare_history_task_read(
    store: ArticleHistorySQLiteStore,
    sources: dict[str, list[dict[str, Any]]],
    query: dict[str, str],
    *,
    limit: int,
    sample_pages: int,
) -> dict[str, Any]:
    task_id = str(query.get("task_id") or "").strip()
    task_name = str(query.get("task_name") or "").strip()
    expected_targets = _history_read_targets_for_compare(
        task_id=task_id,
        task_name=task_name,
        exists=lambda key: key in sources,
    )
    sqlite_targets = _history_read_targets_for_compare(
        task_id=task_id,
        task_name=task_name,
        exists=lambda key: store.get_history_record_count(key) > 0,
    )
    expected_records = _history_runtime_records_from_sources(
        sources,
        expected_targets,
        task_id=task_id,
        task_name=task_name,
    )
    sqlite_records = _history_runtime_records_from_store(
        store,
        sqlite_targets,
        task_id=task_id,
        task_name=task_name,
    )
    expected_count = len(expected_records)
    sqlite_count = len(sqlite_records)
    windows = []
    mismatches: list[str] = []
    if expected_targets != sqlite_targets:
        mismatches.append("targets")
    if expected_count != sqlite_count:
        mismatches.append("count")
    for window in _history_compare_windows(expected_count, limit=limit, sample_pages=sample_pages):
        offset = int(window["offset"])
        window_limit = int(window["limit"])
        expected_window = expected_records[offset:offset + window_limit]
        sqlite_window = sqlite_records[offset:offset + window_limit]
        expected_signature = _history_records_signature(expected_window)
        sqlite_signature = _history_records_signature(sqlite_window)
        window_mismatches = []
        if expected_signature != sqlite_signature:
            window_mismatches.append("records")
            if "records" not in mismatches:
                mismatches.append("records")
        window_report: dict[str, Any] = {
            "name": window["name"],
            "offset": offset,
            "limit": window_limit,
            "ok": not window_mismatches,
            "mismatches": window_mismatches,
            "json": {
                "count": len(expected_window),
            },
            "sqlite": {
                "count": len(sqlite_window),
            },
        }
        if window_mismatches:
            window_report["json_records"] = expected_signature
            window_report["sqlite_records"] = sqlite_signature
        windows.append(window_report)
    return {
        "ok": not mismatches,
        "name": str(query.get("name") or ""),
        "query": {
            "task_id": task_id,
            "task_name": task_name,
        },
        "mismatches": mismatches,
        "json": {
            "count": expected_count,
            "targets": expected_targets,
        },
        "sqlite": {
            "count": sqlite_count,
            "targets": sqlite_targets,
        },
        "windows": windows,
    }


def _history_compare_windows(total: int, *, limit: int, sample_pages: int) -> list[dict[str, int | str]]:
    capped_limit = max(1, min(500, int(limit or 1)))
    if total <= 0:
        return [{"name": "empty", "offset": 0, "limit": capped_limit}]
    offsets = [0]
    if total > capped_limit:
        tail_offset = max(0, total - capped_limit)
        offsets.append(tail_offset)
        if sample_pages > 0:
            available = max(0, tail_offset - capped_limit)
            for index in range(1, sample_pages + 1):
                offset = int(round((available * index) / (sample_pages + 1)))
                offsets.append(max(0, min(tail_offset, offset)))
    unique_offsets = sorted(set(offsets))
    windows = []
    for offset in unique_offsets:
        if offset == 0:
            name = "head"
        elif offset >= max(0, total - capped_limit):
            name = "tail"
        else:
            name = f"sample:{offset}"
        windows.append({"name": name, "offset": offset, "limit": capped_limit})
    return windows


def _ordered_history_records(records: Any) -> list[dict[str, Any]]:
    items = [item for item in (records or []) if isinstance(item, dict)]
    return sorted(items, key=lambda item: str(item.get("ts") or ""))


def _history_records_signature(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "id": str(record.get("id") or ""),
            "ts": str(record.get("ts") or ""),
            "digest": _history_record_digest(record),
        }
        for record in records
        if isinstance(record, dict)
    ]


def _history_record_digest(record: dict[str, Any]) -> str:
    try:
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    except Exception:
        payload = str(record)
    return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()


def _history_read_targets_for_compare(
    *,
    task_id: str,
    task_name: str,
    exists: Any,
) -> list[str]:
    targets: list[str] = []
    normalized_task_id = str(task_id or "").strip()
    normalized_task_name = str(task_name or "").strip()
    primary = normalized_task_id or normalized_task_name
    if primary:
        targets.append(primary)
    if normalized_task_id and callable(exists) and exists(normalized_task_id):
        return targets
    if normalized_task_name and normalized_task_name not in targets:
        targets.append(normalized_task_name)
    return targets


def _history_runtime_records_from_sources(
    sources: dict[str, list[dict[str, Any]]],
    targets: list[str],
    *,
    task_id: str,
    task_name: str,
) -> list[dict[str, Any]]:
    return _history_runtime_records_from_targets(
        targets,
        get_records=lambda key: sources.get(key, []),
        task_id=task_id,
        task_name=task_name,
    )


def _history_runtime_records_from_store(
    store: ArticleHistorySQLiteStore,
    targets: list[str],
    *,
    task_id: str,
    task_name: str,
) -> list[dict[str, Any]]:
    return _history_runtime_records_from_targets(
        targets,
        get_records=lambda key: store.get_history_records(key),
        task_id=task_id,
        task_name=task_name,
    )


def _history_runtime_records_from_targets(
    targets: list[str],
    *,
    get_records: Any,
    task_id: str,
    task_name: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for target in targets:
        for raw_record in get_records(target) or []:
            if not isinstance(raw_record, dict):
                continue
            dedupe_key = _history_record_dedupe_key(raw_record)
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            records.append(_normalize_history_record_for_compare(raw_record, task_id=task_id, task_name=task_name))
    records.sort(key=lambda item: str(item.get("ts") or ""))
    return records


def _normalize_history_record_for_compare(record: dict[str, Any], *, task_id: str, task_name: str) -> dict[str, Any]:
    item = dict(record)
    if task_id and not item.get("task_id"):
        item["task_id"] = task_id
    if task_name and not item.get("task_name"):
        item["task_name"] = task_name
    item.setdefault("id", "")
    item.setdefault("review_status", "pending" if item.get("success") and item.get("rank", 99) != 99 else "")
    item.setdefault("review_note", "")
    item.setdefault("reviewed_at", "")
    item.setdefault("screenshot", "")
    item.setdefault("highlight_count", 0)
    item.setdefault("answer_text", "")
    item.setdefault("evidence", "")
    item.setdefault("error_message", "")
    item.setdefault("diagnostic_id", "")
    item.setdefault("mode", "")
    item.setdefault("execution_source", "")
    return item


def _history_record_dedupe_key(record: dict[str, Any]) -> str:
    record_id = str((record or {}).get("id") or "").strip()
    if record_id:
        return f"id:{record_id}"
    payload = {
        "task_id": str((record or {}).get("task_id") or "").strip(),
        "task_name": str((record or {}).get("task_name") or "").strip(),
        "ts": str((record or {}).get("ts") or "").strip(),
        "platform": str((record or {}).get("platform") or "").strip(),
        "keyword": str((record or {}).get("keyword") or "").strip(),
        "brand": str((record or {}).get("brand") or "").strip(),
        "rank": int((record or {}).get("rank", 99) or 99),
        "mode": str((record or {}).get("mode") or "").strip(),
        "execution_source": str((record or {}).get("execution_source") or "").strip(),
    }
    return "raw:" + json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _storage_media_type(media_type: str) -> str:
    return {
        "media": "authority",
        "self-media": "selfmedia",
    }.get(str(media_type or "").strip(), str(media_type or "").strip())


def _article_published_date(article: dict[str, Any]) -> date | None:
    published_at = article.get("published_at")
    if published_at:
        return parse_local_date(published_at)
    if str(article.get("fetch_method", "") or "").strip() == "manual_table_import":
        return None
    return parse_local_date(article.get("ts"))


def _dedupe_articles_by_url(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    index_by_url: dict[str, int] = {}
    url_by_fingerprint: dict[str, str] = {}
    for article in articles:
        if not isinstance(article, dict):
            continue
        normalized_url = article_store.normalize_article_url(str(article.get("url") or ""))
        fingerprint = _article_url_fingerprint(article)
        if normalized_url and fingerprint and fingerprint not in url_by_fingerprint:
            url_by_fingerprint[fingerprint] = str(article.get("url") or "").strip()

    for article in articles:
        if not isinstance(article, dict):
            continue
        item = dict(article)
        normalized_url = article_store.normalize_article_url(str(item.get("url") or ""))
        if not normalized_url:
            fallback_url = url_by_fingerprint.get(_article_url_fingerprint(item), "")
            if fallback_url:
                item["url"] = fallback_url
                normalized_url = article_store.normalize_article_url(fallback_url)
        if not normalized_url:
            deduped.append(item)
            continue
        existing_index = index_by_url.get(normalized_url)
        if existing_index is None:
            index_by_url[normalized_url] = len(deduped)
            deduped.append(item)
            continue
        deduped[existing_index] = _merge_duplicate_article(deduped[existing_index], item)
    return deduped


def _article_url_fingerprint(article: dict[str, Any]) -> str:
    title = re.sub(r"\s+", " ", str(article.get("title") or "").strip()).lower()
    source = re.sub(
        r"\s+",
        " ",
        str(article.get("media_name") or article.get("source") or article.get("platform") or "").strip(),
    ).lower()
    published = str(article.get("published_at") or article.get("published") or article.get("ts") or "").strip()[:10]
    if not title or not source:
        return ""
    return "|".join([title, source, published])


def _merge_duplicate_article(base: dict[str, Any], duplicate: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key in ("matched_tasks", "referenced_tasks", "cloud_task_ids"):
        merged[key] = _merge_unique_texts(merged.get(key), duplicate.get(key))
    base_reasons = merged.get("match_reasons") if isinstance(merged.get("match_reasons"), dict) else {}
    duplicate_reasons = duplicate.get("match_reasons") if isinstance(duplicate.get("match_reasons"), dict) else {}
    if base_reasons or duplicate_reasons:
        next_reasons: dict[str, list[str]] = {}
        for task_name in set(base_reasons.keys()) | set(duplicate_reasons.keys()):
            next_reasons[str(task_name)] = _merge_unique_texts(
                base_reasons.get(task_name),
                duplicate_reasons.get(task_name),
            )
        merged["match_reasons"] = next_reasons
    base_hits = merged.get("reference_hits") if isinstance(merged.get("reference_hits"), dict) else {}
    duplicate_hits = duplicate.get("reference_hits") if isinstance(duplicate.get("reference_hits"), dict) else {}
    if duplicate_hits:
        merged["reference_hits"] = {**base_hits, **duplicate_hits}
    for key in ("url", "title", "media_name", "platform", "published_at", "ts"):
        if not str(merged.get(key) or "").strip() and str(duplicate.get(key) or "").strip():
            merged[key] = duplicate.get(key)
    return merged


def _merge_unique_texts(left: Any, right: Any) -> list[str]:
    result: list[str] = []
    for values in (left, right):
        if not isinstance(values, list):
            continue
        for value in values:
            text = str(value or "").strip()
            if text and text not in result:
                result.append(text)
    return result


def _read_history_file(path: Path) -> tuple[str, list[dict[str, Any]]]:
    data = _read_json_list(path)
    records = [item for item in data if isinstance(item, dict)]
    records.sort(key=lambda item: str(item.get("ts") or ""))
    return path.stem, records


def _read_json_list(path: Path) -> list[Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _normalized_article_url_count(articles: list[dict[str, Any]]) -> int:
    normalized_urls = {
        article_store.normalize_article_url(str(item.get("url") or ""))
        for item in articles
        if article_store.normalize_article_url(str(item.get("url") or ""))
    }
    no_url_count = sum(
        1
        for item in articles
        if not article_store.normalize_article_url(str(item.get("url") or ""))
    )
    return len(normalized_urls) + no_url_count


def _article_task_counts(articles: list[dict[str, Any]], *, relation_field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for article in articles:
        seen: set[str] = set()
        values = article.get(relation_field)
        if not isinstance(values, list):
            continue
        for value in values:
            task_name = str(value or "").strip()
            if not task_name or task_name in seen:
                continue
            seen.add(task_name)
            counts[task_name] = counts.get(task_name, 0) + 1
    return dict(sorted(counts.items()))


def _counts_subset_match(expected_counts: dict[str, int], sqlite_counts: dict[str, int]) -> bool:
    return {
        key: value
        for key, value in sqlite_counts.items()
        if value
    } == {
        key: value
        for key, value in expected_counts.items()
        if value
    }


def _history_tail_signature(records: list[dict[str, Any]], limit: int) -> list[tuple[str, str]]:
    capped_limit = max(1, int(limit or 1))
    ordered = sorted(
        [item for item in records if isinstance(item, dict)],
        key=lambda item: str(item.get("ts") or ""),
    )
    return [
        (str(item.get("id") or ""), str(item.get("ts") or ""))
        for item in ordered[-capped_limit:]
    ]
