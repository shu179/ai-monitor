from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import article_store, history
from .app_paths import resolve_app_path
from .article_history_sqlite_store import ArticleHistorySQLiteStore
from .local_account_space import account_scoped_path


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
    article_result = store.import_articles(source["articles"], replace=True)
    history_results: dict[str, dict[str, int]] = {}
    for storage_key, records in source["history"].items():
        history_results[storage_key] = store.import_history_records(storage_key, records, replace=True)

    verification = verify_shadow_store(
        target_db_path,
        source=source,
        tail_limit=verify_tail_limit,
    )
    return {
        "db_path": str(target_db_path),
        "articles": article_result,
        "history": {
            "storage_keys": len(history_results),
            "records": sum(item.get("created", 0) + item.get("updated", 0) for item in history_results.values()),
            "details": history_results,
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


def _read_history_file(path: Path) -> tuple[str, list[dict[str, Any]]]:
    data = _read_json_list(path)
    records = [item for item in data if isinstance(item, dict)]
    records.sort(key=lambda item: (str(item.get("ts") or ""), str(item.get("id") or "")))
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
        key=lambda item: (str(item.get("ts") or ""), str(item.get("id") or "")),
    )
    return [
        (str(item.get("id") or ""), str(item.get("ts") or ""))
        for item in ordered[-capped_limit:]
    ]
