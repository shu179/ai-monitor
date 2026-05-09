from __future__ import annotations

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
    try:
        return load_config(str(current_account_config_path()))
    except Exception:
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
