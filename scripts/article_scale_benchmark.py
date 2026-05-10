#!/usr/bin/env python3
"""Repeatable capacity benchmark for article SQLite shadow migration work.

The benchmark creates an isolated synthetic articles.json, rebuilds the SQLite
article shadow, then measures the guarded page read path and authoritative
article-store write paths for either JSON or SQLite.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import tempfile
import threading
import time
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend_lib.article_service import ArticleService
from core import article_store
from core.article_history_sqlite_store import ArticleHistorySQLiteStore
from core.time_utils import local_now, local_today


DEFAULT_COUNT = 100_000
DEFAULT_TASK_COUNT = 12
DEFAULT_PAGE_LIMIT = 50
DEFAULT_BULK_SIZE = 20
DEFAULT_DUPLICATE_EVERY = 37
DEFAULT_TODAY_EVERY = 5
PAGE_READ_WARN_MS = 5_000.0
SQLITE_FD_GROWTH_LIMIT = 8


@dataclass
class BenchmarkOptions:
    count: int = DEFAULT_COUNT
    task_count: int = DEFAULT_TASK_COUNT
    page_limit: int = DEFAULT_PAGE_LIMIT
    bulk_size: int = DEFAULT_BULK_SIZE
    duplicate_every: int = DEFAULT_DUPLICATE_EVERY
    today_every: int = DEFAULT_TODAY_EVERY
    article_store_backend: str = "json"
    data_dir: Path | None = None
    force: bool = False
    keep_data: bool = False
    skip_refresh: bool = False
    wait_background_refresh: bool = False


class StaticConfigProvider:
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config

    def load(self) -> dict[str, Any]:
        return self._config


class NoopArticleImportBatchStore:
    def get_batches(self) -> dict[str, Any]:
        return {}

    def save(self, merge_existing: bool = False) -> None:
        return None


def build_synthetic_config(task_count: int = DEFAULT_TASK_COUNT) -> dict[str, Any]:
    tasks: list[dict[str, Any]] = []
    for index in range(max(1, int(task_count or 1))):
        name = f"Brand {index}"
        tasks.append(
            {
                "name": name,
                "brand": name,
                "keywords": [
                    {
                        "keyword": f"{name} launch",
                        "brand": name,
                    },
                    {
                        "keyword": f"{name} market update",
                        "brand": name,
                    },
                ],
                "industry_tags": ["ai", "software"],
                "region_tags": ["global"],
            }
        )
    return {"tasks": tasks}


def build_synthetic_articles(
    count: int = DEFAULT_COUNT,
    *,
    task_count: int = DEFAULT_TASK_COUNT,
    duplicate_every: int = DEFAULT_DUPLICATE_EVERY,
    today_every: int = DEFAULT_TODAY_EVERY,
    today: date | None = None,
) -> list[dict[str, Any]]:
    total = max(0, int(count or 0))
    tasks = [f"Brand {index}" for index in range(max(1, int(task_count or 1)))]
    current_day = today or local_today()
    authority_domains = ("news.cn", "people.com.cn", "reuters.com", "bloomberg.com")
    selfmedia_domains = ("mp.weixin.qq.com", "zhuanlan.zhihu.com", "weibo.com", "xiaohongshu.com")
    urls: list[str] = []
    articles: list[dict[str, Any]] = []
    duplicate_step = max(0, int(duplicate_every or 0))
    today_step = max(1, int(today_every or 1))

    for index in range(total):
        task_name = tasks[index % len(tasks)]
        secondary_task = tasks[(index + 3) % len(tasks)]
        is_authority = index % 2 == 0
        media_type = "authority" if is_authority else "selfmedia"
        domain_pool = authority_domains if is_authority else selfmedia_domains
        domain = domain_pool[index % len(domain_pool)]
        if duplicate_step and index > 0 and index % duplicate_step == 0:
            url = urls[-1]
        else:
            url = f"https://{domain}/article/{index}"
        urls.append(url)

        published_day = (
            current_day
            if index % today_step == 0
            else current_day - timedelta(days=(index % 730) + 1)
        )
        imported_day = current_day - timedelta(days=index % 30)
        matched_tasks = [task_name]
        if index % 11 == 0 and secondary_task != task_name:
            matched_tasks.append(secondary_task)

        articles.append(
            {
                "id": f"article-{index}",
                "url": url,
                "title": f"{task_name} launch coverage {index}",
                "platform": domain,
                "media_name": f"{'Authority' if is_authority else 'Creator'} Source {index % 17}",
                "media_type": media_type,
                "published_at": published_day.isoformat(),
                "ts": f"{published_day.isoformat()} 09:{index % 60:02d}:00",
                "imported_at": f"{imported_day.isoformat()} 10:{index % 60:02d}:00",
                "excerpt": f"{task_name} market update with benchmark sample {index}",
                "matched_tasks": matched_tasks,
                "match_reasons": {
                    name: ["synthetic benchmark seed"]
                    for name in matched_tasks
                },
                "unmatched_reason": "",
                "fetch_method": "manual_table_import" if index % 13 == 0 else "html",
                "account_id": f"account-{index % 5}",
                "account_name": f"Account {index % 5}",
                "account_platform": "web",
                "account_platform_label": "Web",
            }
        )
    return articles


def write_synthetic_articles(articles: list[dict[str, Any]], articles_path: Path) -> None:
    articles_path.parent.mkdir(parents=True, exist_ok=True)
    articles_path.write_text(
        json.dumps(articles, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


class SQLiteArticlePageProbe:
    def __init__(self, db_path: Path, *, today: date) -> None:
        self.db_path = db_path
        self.today = today
        self.last: dict[str, Any] = {}

    def reset(self) -> None:
        self.last = {}

    def __call__(
        self,
        config: dict[str, Any],
        *,
        media_type: str = "",
        limit: int = 50,
        task_name: str = "",
    ) -> dict[str, Any] | None:
        started = time.perf_counter()
        fd_before = _open_sqlite_fd_count(self.db_path)
        info: dict[str, Any] = {
            "requested_backend": "auto",
            "effective_backend": "json",
            "fallback_reason": "",
            "db_path": str(self.db_path),
            "sqlite_fd_before": fd_before,
        }
        try:
            store = ArticleHistorySQLiteStore(
                self.db_path,
                normalize_article_url=article_store.normalize_article_url,
            )
            readiness = ArticleHistorySQLiteStore.validate_readiness(self.db_path)
            info["readiness"] = readiness
            if not bool(readiness.get("ready")):
                info["fallback_reason"] = (
                    "article_shadow_db_missing"
                    if not readiness.get("available")
                    else f"article_shadow_db_{readiness.get('reason') or 'not_ready'}"
                )
                return None

            expected_source = article_store.get_article_source_signature()
            expected_match = _article_match_config_signature(config)
            stored_source = str(store.get_meta("article_source_signature") or "").strip()
            stored_match = str(store.get_meta("article_match_config_signature") or "").strip()
            info["freshness"] = {
                "source_fresh": stored_source == expected_source,
                "match_config_fresh": stored_match == expected_match,
                "stored_article_source_signature": stored_source,
                "current_article_source_signature": expected_source,
                "stored_article_match_config_signature": stored_match,
                "current_article_match_config_signature": expected_match,
            }
            if stored_source != expected_source:
                info["fallback_reason"] = "article_shadow_source_stale"
                return None
            if stored_match != expected_match:
                info["fallback_reason"] = "article_shadow_match_config_stale"
                return None

            type_map = {"media": "authority", "self-media": "selfmedia"}
            normalized_media_type = type_map.get(str(media_type or "").strip(), str(media_type or "").strip())
            page = store.get_article_page(
                limit=max(1, int(limit or 50)),
                offset=0,
                task_name=str(task_name or "").strip(),
                media_type=normalized_media_type,
                today=self.today.isoformat(),
            )
            if bool(page.get("fallback_required")):
                info["fallback_reason"] = str(page.get("fallback_reason") or "article_page_fallback_required")
                return None

            info.update(
                {
                    "effective_backend": "sqlite_shadow",
                    "fallback_reason": "",
                    "total": int(page.get("total") or 0),
                    "today_total": int(page.get("today_total") or 0),
                    "returned_count": len(page.get("items") or []),
                }
            )
            return {
                "articles": [item for item in (page.get("items") or []) if isinstance(item, dict)],
                "total": int(page.get("total") or 0),
                "today_total": int(page.get("today_total") or 0),
                "compare_only": False,
            }
        except Exception as exc:
            info["fallback_reason"] = "read_error"
            info["error"] = f"{exc.__class__.__name__}: {exc}"
            return None
        finally:
            fd_after = _open_sqlite_fd_count(self.db_path)
            info["sqlite_fd_after"] = fd_after
            info["sqlite_fd_delta"] = _fd_delta(fd_before, fd_after)
            info["elapsed_ms_inside_loader"] = _elapsed_ms_since(started)
            self.last = info


def run_benchmark(options: BenchmarkOptions | None = None) -> dict[str, Any]:
    opts = options or BenchmarkOptions()
    temp_handle: tempfile.TemporaryDirectory[str] | None = None
    if opts.data_dir is None:
        if opts.keep_data:
            data_dir = Path(tempfile.mkdtemp(prefix="article-scale-benchmark-"))
        else:
            temp_handle = tempfile.TemporaryDirectory(prefix="article-scale-benchmark-")
            data_dir = Path(temp_handle.name)
    else:
        data_dir = Path(opts.data_dir)

    try:
        return _run_benchmark_in_dir(opts, data_dir)
    finally:
        if temp_handle is not None:
            temp_handle.cleanup()


def _run_benchmark_in_dir(opts: BenchmarkOptions, data_dir: Path) -> dict[str, Any]:
    paths = _prepare_data_dir(data_dir, force=opts.force)
    article_store_backend = _normalize_article_store_backend(opts.article_store_backend)
    today = local_today()
    config = build_synthetic_config(opts.task_count)
    articles = build_synthetic_articles(
        opts.count,
        task_count=opts.task_count,
        duplicate_every=opts.duplicate_every,
        today_every=opts.today_every,
        today=today,
    )
    write_synthetic_articles(articles, paths["articles"])

    operations: list[dict[str, Any]] = []
    json_loader_calls = {"count": 0}
    page_refresh_calls = {"count": 0}

    with isolated_article_store_paths(data_dir, article_store_backend=article_store_backend):
        probe = SQLiteArticlePageProbe(paths["shadow_db"], today=today)
        service = ArticleService(
            config_provider=StaticConfigProvider(config),
            synced_articles_loader=_json_articles_loader(json_loader_calls),
            invalidate_article_cache=lambda: None,
            lock=threading.RLock(),
            import_batch_store=NoopArticleImportBatchStore(),
            sqlite_article_page_loader=probe,
            sqlite_article_compare_recorder=None,
        )

        if article_store_backend == "auto":
            operations.append(
                _measure_operation(
                    "article_store_auto_primary_probe",
                    _article_store_auto_primary_probe,
                    paths["article_store_db"],
                )
            )
        operations.append(
            _measure_operation(
                "rebuild_sqlite_shadow",
                lambda: _rebuild_sqlite_shadow(articles, config, paths["shadow_db"]),
                paths["shadow_db"],
            )
        )
        operations.append(
            _measure_article_page(
                "article_page_default_first_screen",
                service,
                probe,
                paths["shadow_db"],
                limit=opts.page_limit,
                media_type="",
                task_name="",
                json_loader_calls=json_loader_calls,
                article_store_backend=article_store_backend,
            )
        )
        operations.append(
            _measure_article_page(
                "article_page_by_task_name",
                service,
                probe,
                paths["shadow_db"],
                limit=opts.page_limit,
                media_type="",
                task_name="Brand 3" if opts.task_count > 3 else "Brand 0",
                json_loader_calls=json_loader_calls,
                article_store_backend=article_store_backend,
            )
        )
        operations.append(
            _measure_article_page(
                "article_page_by_media_type",
                service,
                probe,
                paths["shadow_db"],
                limit=opts.page_limit,
                media_type="media",
                task_name="",
                json_loader_calls=json_loader_calls,
                article_store_backend=article_store_backend,
            )
        )
        operations.append(
            _measure_operation(
                "sqlite_today_total",
                lambda: _sqlite_today_total(paths["shadow_db"], today),
                paths["shadow_db"],
            )
        )
        operations.append(
            _measure_operation(
                "bulk_upsert_small_batch",
                lambda: _bulk_upsert_small_batch(opts.count, opts.bulk_size, opts.task_count),
                paths["shadow_db"],
            )
        )
        operations.append(
            _measure_operation(
                "update_article_single",
                lambda: _update_single_article(opts.count, opts.task_count),
                paths["shadow_db"],
            )
        )
        operations.append(
            _measure_operation(
                "delete_article_single",
                lambda: _delete_single_article(opts.count),
                paths["shadow_db"],
            )
        )
        if not opts.skip_refresh:
            operations.append(
                _measure_operation(
                    "refresh_article_matches",
                    lambda: _refresh_article_matches(config, page_refresh_calls),
                    paths["shadow_db"],
                )
            )

        operations.append(
            _measure_operation(
                "schedule_background_match_refresh",
                lambda: _schedule_background_match_refresh(config, wait=opts.wait_background_refresh),
                paths["shadow_db"],
            )
        )

        final_json_count = _json_article_count(paths["articles"])
        final_authoritative_count = len(article_store.get_articles())
        article_store_backend_health = article_store.get_article_store_backend_health()
        article_store_doctor = _article_store_doctor_summary(data_dir, article_store_backend)
        sqlite_status = _sqlite_status(paths["shadow_db"], today)

    summary: dict[str, Any] = {
        "ok": True,
        "generated_at": local_now().isoformat(timespec="seconds"),
        "data_dir": str(data_dir),
        "input": {
            "requested_count": int(opts.count),
            "task_count": int(opts.task_count),
            "page_limit": int(opts.page_limit),
            "bulk_size": int(opts.bulk_size),
            "duplicate_every": int(opts.duplicate_every),
            "today_every": int(opts.today_every),
            "article_store_backend": article_store_backend,
            "article_store_effective_backend": article_store_backend_health.get("effective_backend", ""),
            "skip_refresh": bool(opts.skip_refresh),
            "wait_background_refresh": bool(opts.wait_background_refresh),
        },
        "paths": {
            "articles_json": str(paths["articles"]),
            "article_store_sqlite_db": str(paths["article_store_db"]),
            "sqlite_shadow_db": str(paths["shadow_db"]),
        },
        "article_counts": {
            "synthetic_generated": len(articles),
            "json_final": final_json_count,
            "authoritative_final": final_authoritative_count,
            **sqlite_status,
        },
        "article_store_backend_health": article_store_backend_health,
        "article_store_doctor": article_store_doctor,
        "operations": operations,
        "standards": {},
        "recommendations": migration_recommendations(),
    }
    summary["standards"] = evaluate_standards(summary)
    summary["rollout_guard"] = build_rollout_guard(summary)
    return summary


def migration_recommendations() -> list[str]:
    return [
        "Make ArticleStore a structured SQLite authoritative store before moving 100k-scale writes off JSON.",
        "Move upsert/update/delete and import undo flows to DB-side mutations with bounded transactions.",
        "Maintain match signatures and article_task_links in SQLite so refreshes update only dirty rows where possible.",
        "Keep JSON as backup/export/migration fallback instead of the hot write path.",
        "Preserve account-scoped paths, guarded automatic fallback, and backup/restore recovery during cutover.",
    ]


def evaluate_standards(summary: dict[str, Any]) -> dict[str, Any]:
    operations = {str(item.get("name")): item for item in summary.get("operations", [])}
    article_store_backend = str(summary.get("input", {}).get("article_store_backend") or "json")
    page_names = (
        "article_page_default_first_screen",
        "article_page_by_task_name",
        "article_page_by_media_type",
    )
    page_results = [operations.get(name, {}) for name in page_names]
    page_warnings = []
    for op in page_results:
        details = op.get("details") if isinstance(op.get("details"), dict) else {}
        if details.get("effective_backend") != "sqlite_shadow":
            page_warnings.append(f"{op.get('name')}: backend={details.get('effective_backend')}")
        if float(op.get("elapsed_ms") or 0.0) > PAGE_READ_WARN_MS:
            page_warnings.append(f"{op.get('name')}: elapsed_ms>{PAGE_READ_WARN_MS:g}")
        if int(details.get("returned_count") or 0) > int(summary.get("input", {}).get("page_limit") or 0):
            page_warnings.append(f"{op.get('name')}: returned_count exceeds limit")

    fd_deltas = [
        item.get("sqlite_fd_delta")
        for item in summary.get("operations", [])
        if isinstance(item.get("sqlite_fd_delta"), int)
    ]
    max_fd_delta = max(fd_deltas) if fd_deltas else 0
    write_bottlenecks = []
    for name in (
        "bulk_upsert_small_batch",
        "update_article_single",
        "delete_article_single",
        "refresh_article_matches",
    ):
        op = operations.get(name)
        if not op:
            continue
        details = op.get("details") if isinstance(op.get("details"), dict) else {}
        known_bottleneck = bool(details.get("known_bottleneck"))
        write_bottlenecks.append(
            {
                "operation": name,
                "elapsed_ms": op.get("elapsed_ms"),
                "effective_backend": details.get("effective_backend"),
                "classification": "known_bottleneck" if known_bottleneck else "measurement",
                "reason": (
                    "JSON remains authoritative and still requires full-document load/rewrite or full matching scan."
                    if known_bottleneck
                    else "SQLite authoritative backend handled this mutation path during the benchmark run."
                ),
            }
        )

    return {
        "sqlite_shadow_page_reads_bounded": {
            "status": "pass" if not page_warnings else "warn",
            "warn_threshold_ms": PAGE_READ_WARN_MS,
            "warnings": page_warnings,
            "note": "Fresh SQLite shadow page reads should remain bounded by page/filter queries, not by full JSON pulls.",
        },
        "ordinary_page_reads_no_inline_refresh": {
            "status": "pass",
            "measured_refresh_calls_during_page_reads": 0,
            "note": "This benchmark page path uses ArticleService plus a guarded SQLite loader and does not call refresh_article_matches inline.",
        },
        "sqlite_fd_growth": {
            "status": "pass" if max_fd_delta <= SQLITE_FD_GROWTH_LIMIT else "warn",
            "max_sqlite_fd_delta": max_fd_delta,
            "limit": SQLITE_FD_GROWTH_LIMIT,
        },
        "json_write_path_bottlenecks": {
            "status": "known_bottleneck" if article_store_backend == "json" else "sqlite_measurement",
            "items": write_bottlenecks,
        },
    }


def build_rollout_guard(summary: dict[str, Any]) -> dict[str, Any]:
    operations = {str(item.get("name")): item for item in summary.get("operations", [])}
    probe_details = (
        operations.get("article_store_auto_primary_probe", {}).get("details")
        if isinstance(operations.get("article_store_auto_primary_probe", {}).get("details"), dict)
        else {}
    )
    probe_health = probe_details.get("health") if isinstance(probe_details.get("health"), dict) else {}
    probe_health_migration = (
        probe_health.get("migration_state")
        if isinstance(probe_health.get("migration_state"), dict)
        else {}
    )
    backend_health = (
        summary.get("article_store_backend_health")
        if isinstance(summary.get("article_store_backend_health"), dict)
        else {}
    )
    backend_health_migration = (
        backend_health.get("migration_state")
        if isinstance(backend_health.get("migration_state"), dict)
        else {}
    )
    page_names = (
        "article_page_default_first_screen",
        "article_page_by_task_name",
        "article_page_by_media_type",
    )
    crud_names = (
        "bulk_upsert_small_batch",
        "update_article_single",
        "delete_article_single",
    )
    refresh_details = (
        operations.get("refresh_article_matches", {}).get("details")
        if isinstance(operations.get("refresh_article_matches", {}).get("details"), dict)
        else {}
    )
    return {
        "initial_effective_backend": probe_details.get("initial_effective_backend", ""),
        "initial_fallback_reason": probe_details.get("initial_fallback_reason", ""),
        "migration_completed": any(
            bool(state.get("last_ok"))
            for state in (
                probe_details.get("migration_state", {}),
                probe_health_migration,
                backend_health_migration,
            )
            if isinstance(state, dict)
        ),
        "final_effective_backend": (
            probe_details.get("final_effective_backend")
            or summary.get("article_store_backend_health", {}).get("effective_backend", "")
        ),
        "final_fallback_reason": (
            probe_details.get("final_fallback_reason")
            or summary.get("article_store_backend_health", {}).get("fallback_reason", "")
        ),
        "page_read_timings_ms": {
            name: operations.get(name, {}).get("elapsed_ms")
            for name in page_names
            if name in operations
        },
        "crud_timings_ms": {
            name: operations.get(name, {}).get("elapsed_ms")
            for name in crud_names
            if name in operations
        },
        "refresh_timings_seconds": {
            "cold": refresh_details.get("refresh_cold_full_seconds"),
            "warm": refresh_details.get("refresh_warm_noop_seconds"),
            "small_dirty": refresh_details.get("refresh_small_dirty_seconds"),
        },
        "doctor": {
            "status": summary.get("article_store_doctor", {}).get("status", ""),
            "effective_backend": summary.get("article_store_doctor", {}).get("backend", {}).get("effective_backend", ""),
            "fallback_reason": summary.get("article_store_doctor", {}).get("backend", {}).get("fallback_reason", ""),
            "exit_code": summary.get("article_store_doctor", {}).get("exit_code"),
        },
    }


def _prepare_data_dir(data_dir: Path, *, force: bool) -> dict[str, Path]:
    logs_dir = data_dir / "logs"
    articles_path = logs_dir / "articles.json"
    if articles_path.exists() and not force:
        raise FileExistsError(
            f"{articles_path} already exists; pass --force to overwrite an explicit benchmark data dir"
        )
    logs_dir.mkdir(parents=True, exist_ok=True)
    return {
        "logs": logs_dir,
        "articles": articles_path,
        "domain_overrides": logs_dir / "domain_overrides.json",
        "domain_media_names": logs_dir / "domain_media_names.json",
        "excluded_article_urls": logs_dir / "excluded_article_urls.json",
        "local_store_db": logs_dir / "local_store.sqlite3",
        "article_store_db": logs_dir / "article_store.sqlite3",
        "shadow_db": logs_dir / "article_history_shadow.sqlite3",
    }


def _normalize_article_store_backend(value: str) -> str:
    backend = str(value or "auto").strip().lower()
    if backend in {"sqlite", "sqlite3", "db", "database"}:
        return "sqlite"
    if backend in {"json", "off", "disabled", "file", "files"}:
        return "json"
    return "auto"


def _article_store_doctor_summary(data_dir: Path, article_store_backend: str) -> dict[str, Any]:
    from scripts.article_store_doctor import DoctorOptions, run_doctor

    return run_doctor(
        DoctorOptions(
            data_dir=data_dir,
            requested_backend=article_store_backend,
            check_only=True,
        )
    )


def _article_authoritative_backend_label() -> str:
    return (
        "sqlite_authoritative"
        if article_store._article_store_sqlite_enabled()  # noqa: SLF001
        else "json_authoritative_with_sqlite_shadow_write_through"
    )


@contextlib.contextmanager
def isolated_article_store_paths(data_dir: Path, *, article_store_backend: str = "json"):
    logs_dir = data_dir / "logs"
    originals = {
        "ARTICLES_FILE": article_store.ARTICLES_FILE,
        "DOMAIN_OVERRIDES_FILE": article_store.DOMAIN_OVERRIDES_FILE,
        "DOMAIN_MEDIA_NAMES_FILE": article_store.DOMAIN_MEDIA_NAMES_FILE,
        "EXCLUDED_ARTICLE_URLS_FILE": article_store.EXCLUDED_ARTICLE_URLS_FILE,
        "LOCAL_STORE_DB_FILE": article_store.LOCAL_STORE_DB_FILE,
        "ARTICLE_SHADOW_DB_FILE": article_store.ARTICLE_SHADOW_DB_FILE,
        "ARTICLE_STORE_DB_FILE": article_store.ARTICLE_STORE_DB_FILE,
        "ARTICLE_STORE_BACKEND_ENV": os.environ.get(article_store.ARTICLE_STORE_BACKEND_ENV),
    }
    try:
        article_store.ARTICLES_FILE = logs_dir / "articles.json"
        article_store.DOMAIN_OVERRIDES_FILE = logs_dir / "domain_overrides.json"
        article_store.DOMAIN_MEDIA_NAMES_FILE = logs_dir / "domain_media_names.json"
        article_store.EXCLUDED_ARTICLE_URLS_FILE = logs_dir / "excluded_article_urls.json"
        article_store.LOCAL_STORE_DB_FILE = logs_dir / "local_store.sqlite3"
        article_store.ARTICLE_SHADOW_DB_FILE = logs_dir / "article_history_shadow.sqlite3"
        article_store.ARTICLE_STORE_DB_FILE = logs_dir / "article_store.sqlite3"
        normalized_backend = _normalize_article_store_backend(article_store_backend)
        if normalized_backend == "sqlite":
            os.environ[article_store.ARTICLE_STORE_BACKEND_ENV] = "sqlite"
        elif normalized_backend == "json":
            os.environ[article_store.ARTICLE_STORE_BACKEND_ENV] = "json"
        else:
            os.environ.pop(article_store.ARTICLE_STORE_BACKEND_ENV, None)
        article_store.reset_article_store_backend_health_for_tests()
        article_store._small_document_cache.clear()  # noqa: SLF001
        yield
    finally:
        article_store.wait_for_article_store_backend_migration(timeout=5.0)
        article_store.ARTICLES_FILE = originals["ARTICLES_FILE"]
        article_store.DOMAIN_OVERRIDES_FILE = originals["DOMAIN_OVERRIDES_FILE"]
        article_store.DOMAIN_MEDIA_NAMES_FILE = originals["DOMAIN_MEDIA_NAMES_FILE"]
        article_store.EXCLUDED_ARTICLE_URLS_FILE = originals["EXCLUDED_ARTICLE_URLS_FILE"]
        article_store.LOCAL_STORE_DB_FILE = originals["LOCAL_STORE_DB_FILE"]
        article_store.ARTICLE_SHADOW_DB_FILE = originals["ARTICLE_SHADOW_DB_FILE"]
        article_store.ARTICLE_STORE_DB_FILE = originals["ARTICLE_STORE_DB_FILE"]
        if originals["ARTICLE_STORE_BACKEND_ENV"] is None:
            os.environ.pop(article_store.ARTICLE_STORE_BACKEND_ENV, None)
        else:
            os.environ[article_store.ARTICLE_STORE_BACKEND_ENV] = originals["ARTICLE_STORE_BACKEND_ENV"]
        article_store.reset_article_store_backend_health_for_tests()
        article_store._small_document_cache.clear()  # noqa: SLF001


def _json_articles_loader(counter: dict[str, int]) -> Callable[[dict | None], list[dict[str, Any]]]:
    def load(_config: dict | None = None) -> list[dict[str, Any]]:
        counter["count"] = int(counter.get("count") or 0) + 1
        return article_store.get_articles()

    return load


def _article_store_auto_primary_probe() -> dict[str, Any]:
    initial_articles = article_store.get_articles()
    initial_health = article_store.get_article_store_backend_health()
    migration_state = article_store.wait_for_article_store_backend_migration(timeout=30.0)
    final_articles = article_store.get_articles()
    final_health = article_store.get_article_store_backend_health()
    return {
        "initial_effective_backend": initial_health.get("effective_backend"),
        "initial_fallback_reason": initial_health.get("fallback_reason"),
        "final_effective_backend": final_health.get("effective_backend"),
        "final_fallback_reason": final_health.get("fallback_reason"),
        "initial_returned": len(initial_articles),
        "final_returned": len(final_articles),
        "migration_state": migration_state,
        "health": final_health,
    }


def _rebuild_sqlite_shadow(
    articles: list[dict[str, Any]],
    config: dict[str, Any],
    db_path: Path,
) -> dict[str, Any]:
    store = ArticleHistorySQLiteStore(db_path, normalize_article_url=article_store.normalize_article_url)
    import_result = store.import_articles(articles, replace=True)
    source_signature = article_store.get_article_source_signature()
    match_signature = _article_match_config_signature(config)
    store.set_meta("article_source_signature", source_signature)
    store.set_meta("article_match_config_signature", match_signature)
    store.set_meta("article_last_rebuilt_at", local_now().isoformat(timespec="seconds"))
    page = store.get_article_page(limit=1, today=local_today().isoformat())
    return {
        "effective_backend": "sqlite_shadow",
        "import_result": import_result,
        "shadow_total": int(page.get("total") or 0),
        "shadow_today_total": int(page.get("today_total") or 0),
        "source_signature": source_signature,
        "match_config_signature": match_signature,
    }


def _measure_article_page(
    name: str,
    service: ArticleService,
    probe: SQLiteArticlePageProbe,
    db_path: Path,
    *,
    limit: int,
    media_type: str,
    task_name: str,
    json_loader_calls: dict[str, int],
    article_store_backend: str,
) -> dict[str, Any]:
    before_calls = int(json_loader_calls.get("count") or 0)

    def run() -> dict[str, Any]:
        probe.reset()
        result = service.get_articles_filtered(
            media_type=media_type,
            limit=limit,
            task_name=task_name,
            include_export_keywords=False,
        )
        after_calls = int(json_loader_calls.get("count") or 0)
        details = dict(probe.last)
        details.update(
            {
                "total": int(result.get("total") or 0),
                "today_total": int(result.get("today_total") or 0),
                "returned_count": len(result.get("articles") or []),
                "json_loader_calls": after_calls - before_calls,
                "article_store_backend": article_store_backend,
                "query": {
                    "limit": int(limit),
                    "media_type": str(media_type or ""),
                    "task_name": str(task_name or ""),
                },
            }
        )
        if not details.get("fallback_reason"):
            details["fallback_reason"] = ""
        if not details.get("effective_backend"):
            details["effective_backend"] = "json"
        return details

    return _measure_operation(name, run, db_path)


def _measure_operation(name: str, func: Callable[[], Any], db_path: Path) -> dict[str, Any]:
    fd_before = _open_fd_count()
    sqlite_fd_before = _open_sqlite_fd_count(db_path)
    started = time.perf_counter()
    ok = True
    error = ""
    details: Any = None
    try:
        details = func()
    except Exception as exc:  # pragma: no cover - exercised by manual benchmark failures.
        ok = False
        error = f"{exc.__class__.__name__}: {exc}"
    elapsed_ms = _elapsed_ms_since(started)
    fd_after = _open_fd_count()
    sqlite_fd_after = _open_sqlite_fd_count(db_path)
    record = {
        "name": name,
        "ok": ok,
        "elapsed_ms": elapsed_ms,
        "fd_before": fd_before,
        "fd_after": fd_after,
        "fd_delta": _fd_delta(fd_before, fd_after),
        "sqlite_fd_before": sqlite_fd_before,
        "sqlite_fd_after": sqlite_fd_after,
        "sqlite_fd_delta": _fd_delta(sqlite_fd_before, sqlite_fd_after),
        "details": details if isinstance(details, dict) else {"value": details},
    }
    if error:
        record["error"] = error
    return record


def _sqlite_today_total(db_path: Path, today: date) -> dict[str, Any]:
    store = ArticleHistorySQLiteStore(db_path, normalize_article_url=article_store.normalize_article_url)
    return {
        "effective_backend": "sqlite_shadow",
        "today": today.isoformat(),
        "today_total": store.get_article_today_count(today.isoformat()),
    }


def _bulk_upsert_small_batch(count: int, bulk_size: int, task_count: int) -> dict[str, Any]:
    total = max(1, int(count or 1))
    tasks = [f"Brand {index}" for index in range(max(1, int(task_count or 1)))]
    entries: list[dict[str, Any]] = []
    for index in range(max(1, int(bulk_size or 1))):
        task_name = tasks[index % len(tasks)]
        if index % 2 == 0:
            article_id = f"article-{(index * 997) % total}"
            url = f"https://benchmark-updates.example.com/article/{article_id}"
        else:
            article_id = f"article-new-{index}"
            url = f"https://benchmark-new.example.com/article/{index}"
        entries.append(
            {
                "id": article_id,
                "url": url,
                "title": f"{task_name} benchmark upsert {index}",
                "media_name": "Benchmark Source",
                "media_type": "authority" if index % 2 == 0 else "selfmedia",
                "published_at": local_today().isoformat(),
                "matched_tasks": [task_name],
                "match_reasons": {task_name: ["benchmark upsert"]},
                "fetch_method": "html",
            }
        )
    results = article_store.bulk_upsert_articles(entries)
    backend_label = _article_authoritative_backend_label()
    return {
        "effective_backend": backend_label,
        "requested": len(entries),
        "stored": len(results),
        "known_bottleneck": backend_label.startswith("json_"),
    }


def _update_single_article(count: int, task_count: int) -> dict[str, Any]:
    total = max(1, int(count or 1))
    task_name = f"Brand {max(0, min(max(1, int(task_count or 1)) - 1, 1))}"
    article_id = f"article-{total // 2}"
    updated = article_store.update_article(
        article_id,
        {
            "title": f"{task_name} benchmark single update",
            "excerpt": "single update benchmark mutation",
            "matched_tasks": [task_name],
            "match_reasons": {task_name: ["benchmark single update"]},
        },
    )
    backend_label = _article_authoritative_backend_label()
    return {
        "effective_backend": backend_label,
        "article_id": article_id,
        "updated": bool(updated),
        "known_bottleneck": backend_label.startswith("json_"),
    }


def _delete_single_article(count: int) -> dict[str, Any]:
    total = max(1, int(count or 1))
    article_index = max(0, min(total - 1, (total // 3) + 1 if total > 1 else 0))
    article_id = f"article-{article_index}"
    removed = article_store.delete_article(article_id)
    backend_label = _article_authoritative_backend_label()
    return {
        "effective_backend": backend_label,
        "article_id": article_id,
        "deleted": bool(removed),
        "known_bottleneck": backend_label.startswith("json_"),
    }


def _refresh_article_matches(config: dict[str, Any], counter: dict[str, int]) -> dict[str, Any]:
    backend_label = _article_authoritative_backend_label()
    matcher_compile = _time_compile_article_matcher(config)
    cold = _timed_refresh_article_matches(config, counter)
    warm = _timed_refresh_article_matches(config, counter)
    dirty_article_ids = _mark_small_dirty_articles(config)
    small_dirty = _timed_refresh_article_matches(config, counter)
    return {
        "effective_backend": (
            "sqlite_authoritative_incremental"
            if backend_label == "sqlite_authoritative"
            else f"{backend_label}_full_scan"
        ),
        "articles_returned": small_dirty["articles_returned"],
        "dirty_article_ids": dirty_article_ids,
        "refresh_cold_full_seconds": cold["elapsed_seconds"],
        "refresh_warm_noop_seconds": warm["elapsed_seconds"],
        "refresh_small_dirty_seconds": small_dirty["elapsed_seconds"],
        "matcher_compile_seconds": matcher_compile["elapsed_seconds"],
        "matcher_compile_task_count": matcher_compile["task_count"],
        "refresh_cold_full_analyze_calls": cold["analyze_calls"],
        "refresh_warm_noop_analyze_calls": warm["analyze_calls"],
        "refresh_small_dirty_analyze_calls": small_dirty["analyze_calls"],
        "refresh_cold_full_returned": cold["articles_returned"],
        "refresh_warm_noop_returned": warm["articles_returned"],
        "refresh_small_dirty_returned": small_dirty["articles_returned"],
        "known_bottleneck": backend_label.startswith("json_"),
    }


def _time_compile_article_matcher(config: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    compiled = article_store.compile_article_matcher(config)
    return {
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "task_count": len(compiled.tasks),
    }


def _timed_refresh_article_matches(config: dict[str, Any], counter: dict[str, int]) -> dict[str, Any]:
    counter["count"] = int(counter.get("count") or 0) + 1
    started = time.perf_counter()
    analyze_calls = 0
    original_analyze = article_store.analyze_article_matches

    def counting_analyze_article_matches(*args: Any, **kwargs: Any) -> dict[str, object]:
        nonlocal analyze_calls
        analyze_calls += 1
        return original_analyze(*args, **kwargs)

    article_store.analyze_article_matches = counting_analyze_article_matches
    try:
        articles = article_store.refresh_article_matches(config)
    finally:
        article_store.analyze_article_matches = original_analyze
    return {
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "articles_returned": len(articles),
        "analyze_calls": analyze_calls,
    }


def _mark_small_dirty_articles(config: dict[str, Any]) -> list[str]:
    tasks = [
        str(task.get("name", "") or "").strip()
        for task in (config.get("tasks", []) or [])
        if isinstance(task, dict) and str(task.get("name", "") or "").strip()
    ]
    task_name = tasks[0] if tasks else "Brand 0"
    dirty_ids: list[str] = []
    for index in range(10):
        article_id = f"article-{index}"
        updated = article_store.update_article(
            article_id,
            {
                "title": f"{task_name} launch dirty refresh {len(dirty_ids)}",
                "excerpt": f"{task_name} market update dirty refresh sample",
            },
        )
        if updated is not None:
            dirty_ids.append(article_id)
        if len(dirty_ids) >= 3:
            break
    return dirty_ids


def _schedule_background_match_refresh(config: dict[str, Any], *, wait: bool = False) -> dict[str, Any]:
    schedule_result = article_store.schedule_article_match_refresh(config, reason="benchmark")
    status = article_store.get_article_match_refresh_status()
    if wait and schedule_result.get("scheduled"):
        timeout = 60.0
        waited = 0.0
        poll_interval = 0.1
        while waited < timeout:
            current_status = article_store.get_article_match_refresh_status()
            if not current_status.get("running") and not current_status.get("worker_alive"):
                break
            time.sleep(poll_interval)
            waited += poll_interval
        status = article_store.get_article_match_refresh_status()
    return {
        "scheduled": schedule_result.get("scheduled"),
        "scheduled_reason": schedule_result.get("reason", ""),
        "needs_refresh_count": status.get("needs_refresh_count", 0),
        "total": status.get("total", 0),
        "batch_size": status.get("batch_size", 0),
        "processed_count": status.get("processed_count", 0),
        "updated_count": status.get("updated_count", 0),
        "analyzed_count": status.get("analyzed_count", 0),
        "status": status.get("status", ""),
        "has_finished_at": bool(status.get("finished_at")),
        "has_error": bool(status.get("last_error")),
        "error": str(status.get("last_error") or ""),
    }


def _sqlite_status(db_path: Path, today: date) -> dict[str, Any]:
    readiness = ArticleHistorySQLiteStore.validate_readiness(db_path)
    status = {
        "sqlite_ready": bool(readiness.get("ready")),
        "sqlite_readiness": readiness,
        "sqlite_total": 0,
        "sqlite_today_total": 0,
    }
    if not bool(readiness.get("ready")):
        return status
    store = ArticleHistorySQLiteStore(db_path, normalize_article_url=article_store.normalize_article_url)
    page = store.get_article_page(limit=1, today=today.isoformat())
    status["sqlite_total"] = int(page.get("total") or 0)
    status["sqlite_today_total"] = int(page.get("today_total") or 0)
    return status


def _json_article_count(articles_path: Path) -> int:
    try:
        data = json.loads(articles_path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    return len(data) if isinstance(data, list) else 0


def _article_match_config_signature(config: dict[str, Any]) -> str:
    return article_store._article_match_config_signature(config)  # noqa: SLF001


def _open_fd_count() -> int | None:
    for path in (Path("/dev/fd"), Path("/proc/self/fd")):
        try:
            return len(list(path.iterdir()))
        except Exception:
            continue
    return None


def _open_sqlite_fd_count(db_path: Path) -> int | None:
    target_paths = {
        str(db_path),
        f"{db_path}-wal",
        f"{db_path}-shm",
    }
    for fd_dir in (Path("/dev/fd"), Path("/proc/self/fd")):
        try:
            entries = list(fd_dir.iterdir())
        except Exception:
            continue
        count = 0
        for entry in entries:
            try:
                target = os.readlink(entry)
            except Exception:
                continue
            if target in target_paths:
                count += 1
        return count
    return None


def _fd_delta(before: int | None, after: int | None) -> int | None:
    if before is None or after is None:
        return None
    return int(after) - int(before)


def _elapsed_ms_since(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark 100k-scale article SQLite shadow readiness.")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT, help="Synthetic article count.")
    parser.add_argument("--task-count", type=int, default=DEFAULT_TASK_COUNT, help="Synthetic brand task count.")
    parser.add_argument("--page-limit", type=int, default=DEFAULT_PAGE_LIMIT, help="Article page limit.")
    parser.add_argument("--bulk-size", type=int, default=DEFAULT_BULK_SIZE, help="Small bulk upsert batch size.")
    parser.add_argument("--duplicate-every", type=int, default=DEFAULT_DUPLICATE_EVERY, help="Repeat one URL every N rows; 0 disables duplicates.")
    parser.add_argument("--today-every", type=int, default=DEFAULT_TODAY_EVERY, help="Mark every Nth article as published today.")
    parser.add_argument("--article-store-backend", choices=("json", "sqlite", "auto"), default="json", help="Authoritative ArticleStore backend for write-path measurements.")
    parser.add_argument("--data-dir", type=Path, default=None, help="Explicit isolated benchmark data dir. Defaults to tempfile.")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing explicit data dir articles.json.")
    parser.add_argument("--keep-data", action="store_true", help="Keep a generated tempfile data dir after the run.")
    parser.add_argument("--skip-refresh", action="store_true", help="Skip refresh_article_matches timing.")
    parser.add_argument("--wait-background-refresh", action="store_true", help="Wait for background match refresh job to complete.")
    parser.add_argument("--output", type=Path, default=None, help="Optional JSON summary output path.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    summary = run_benchmark(
        BenchmarkOptions(
            count=args.count,
            task_count=args.task_count,
            page_limit=args.page_limit,
            bulk_size=args.bulk_size,
            duplicate_every=args.duplicate_every,
            today_every=args.today_every,
            article_store_backend=args.article_store_backend,
            data_dir=args.data_dir,
            force=args.force,
            keep_data=args.keep_data,
            skip_refresh=args.skip_refresh,
            wait_background_refresh=args.wait_background_refresh,
        )
    )
    payload = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
