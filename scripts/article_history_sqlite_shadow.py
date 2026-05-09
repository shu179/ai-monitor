#!/usr/bin/env python3
"""Rebuild or verify the article/history SQLite shadow store."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.article_history_sqlite_mirror import (
    build_article_compare_queries,
    compare_article_pages,
    default_shadow_db_path,
    rebuild_shadow_store,
    verify_shadow_store,
)

ARTICLE_API_COMPARE_FIELDS = (
    "id",
    "source",
    "title",
    "type",
    "category",
    "url",
    "ts",
    "media_name",
    "matchedTasks",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Maintain the structured SQLite shadow copy of article/history JSON data.",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db-path",
        default=None,
        help="SQLite database path. Defaults to the account-scoped shadow DB under logs/.",
    )
    common.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Maximum parallel workers for JSON reads or article page comparisons.",
    )
    common.add_argument(
        "--compact",
        action="store_true",
        help="Print compact JSON instead of indented JSON.",
    )
    verify_common = argparse.ArgumentParser(add_help=False)
    verify_common.add_argument(
        "--tail-limit",
        type=int,
        default=20,
        help="Number of tail records per history file to compare during verification.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "rebuild",
        parents=[common, verify_common],
        help="Rebuild the shadow SQLite DB from JSON and verify it.",
    )
    subparsers.add_parser(
        "verify",
        parents=[common, verify_common],
        help="Verify an existing shadow SQLite DB against JSON.",
    )
    compare_parser = subparsers.add_parser(
        "compare-articles",
        parents=[common],
        help="Compare JSON article pages with the SQLite shadow article index.",
    )
    compare_parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Number of article IDs to compare per query.",
    )
    compare_parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Rebuild the SQLite shadow article index before comparing.",
    )
    api_compare_parser = subparsers.add_parser(
        "compare-api-articles",
        parents=[common],
        help="Compare /api/articles JSON responses with the SQLite shadow article reader.",
    )
    api_compare_parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Number of article IDs to compare per API query.",
    )
    api_compare_parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="HTTP timeout in seconds for each API request.",
    )
    api_compare_parser.add_argument(
        "--include-export-keywords",
        action="store_true",
        help="Ask /api/articles to include export keyword fields in the compared payload.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "rebuild":
        result = rebuild_shadow_store(
            args.db_path,
            max_workers=args.workers,
            verify_tail_limit=args.tail_limit,
        )
        ok = bool(result.get("verification", {}).get("ok"))
    elif args.command == "compare-articles":
        result = compare_article_pages(
            args.db_path,
            max_workers=args.workers,
            limit=args.limit,
            rebuild=bool(args.rebuild),
        )
        ok = bool(result.get("ok"))
    elif args.command == "compare-api-articles":
        result = compare_article_api_pages(
            args.db_path,
            max_workers=args.workers,
            limit=args.limit,
            timeout=args.timeout,
            include_export_keywords=bool(args.include_export_keywords),
        )
        ok = bool(result.get("ok"))
    else:
        result = verify_shadow_store(
            args.db_path,
            max_workers=args.workers,
            tail_limit=args.tail_limit,
        )
        ok = bool(result.get("ok"))

    print(_to_json(result, compact=bool(args.compact)))
    return 0 if ok else 1


def _to_json(value: dict[str, Any], *, compact: bool) -> str:
    if compact:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def compare_article_api_pages(
    db_path: str | Path | None = None,
    *,
    max_workers: int | None = None,
    limit: int = 50,
    timeout: float = 20.0,
    include_export_keywords: bool = False,
    runtime_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Compare real /api/articles responses across JSON and SQLite shadow read modes."""
    from http.server import ThreadingHTTPServer

    import web_backend

    target_db_path = Path(db_path) if db_path is not None else default_shadow_db_path()
    queries = build_article_compare_queries(_load_api_compare_config())
    resolved_limit = max(1, min(500, int(limit or 50)))
    worker_count = _api_compare_worker_count(queries, max_workers=max_workers)

    with _patched_web_backend_shadow_db_path(web_backend, target_db_path):
        runtime = runtime_factory() if runtime_factory is not None else web_backend.AppRuntime()
        server = ThreadingHTTPServer(("127.0.0.1", 0), web_backend.WebRequestHandler)
        server.runtime = runtime  # type: ignore[attr-defined]
        runtime.port = int(server.server_address[1])
        if hasattr(runtime, "_server"):
            runtime._server = server
        thread = threading.Thread(target=server.serve_forever, name="article-api-shadow-compare", daemon=True)
        thread.start()
        try:
            json_batch = _fetch_article_api_batch(
                int(server.server_address[1]),
                queries,
                mode="json",
                read_backend=None,
                limit=resolved_limit,
                timeout=float(timeout or 20.0),
                include_export_keywords=include_export_keywords,
                workers=worker_count,
            )
            sqlite_batch = _fetch_article_api_batch(
                int(server.server_address[1]),
                queries,
                mode="sqlite_shadow",
                read_backend="sqlite_shadow",
                limit=resolved_limit,
                timeout=float(timeout or 20.0),
                include_export_keywords=include_export_keywords,
                workers=worker_count,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    comparisons = _compare_article_api_batches(json_batch["results"], sqlite_batch["results"])
    failed = [item for item in comparisons if not bool(item.get("ok"))]
    return {
        "ok": not failed,
        "db_path": str(target_db_path),
        "limit": resolved_limit,
        "workers": worker_count,
        "query_count": len(comparisons),
        "failed_count": len(failed),
        "include_export_keywords": bool(include_export_keywords),
        "json": _batch_summary(json_batch),
        "sqlite_shadow": _batch_summary(sqlite_batch),
        "queries": comparisons,
    }


def _load_api_compare_config() -> dict[str, Any] | None:
    try:
        from core.article_history_sqlite_mirror import _load_current_config

        return _load_current_config()
    except Exception:
        return None


@contextlib.contextmanager
def _patched_web_backend_shadow_db_path(web_backend: Any, db_path: Path):
    original = web_backend.default_shadow_db_path
    web_backend.default_shadow_db_path = lambda: db_path
    try:
        yield
    finally:
        web_backend.default_shadow_db_path = original


def _fetch_article_api_batch(
    port: int,
    queries: list[dict[str, str]],
    *,
    mode: str,
    read_backend: str | None,
    limit: int,
    timeout: float,
    include_export_keywords: bool,
    workers: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    fd_before = _open_fd_count()
    with _temporary_env("AIBRANDMONITOR_ARTICLE_READ_BACKEND", read_backend):
        if workers <= 1:
            results = [
                _fetch_article_api_query(
                    port,
                    query,
                    limit=limit,
                    timeout=timeout,
                    include_export_keywords=include_export_keywords,
                )
                for query in queries
            ]
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                results = list(executor.map(
                    lambda query: _fetch_article_api_query(
                        port,
                        query,
                        limit=limit,
                        timeout=timeout,
                        include_export_keywords=include_export_keywords,
                    ),
                    queries,
                ))
    fd_after = _open_fd_count()
    return {
        "mode": mode,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "fd_before": fd_before,
        "fd_after": fd_after,
        "results": results,
    }


def _fetch_article_api_query(
    port: int,
    query: dict[str, str],
    *,
    limit: int,
    timeout: float,
    include_export_keywords: bool,
) -> dict[str, Any]:
    params: dict[str, str] = {"limit": str(limit)}
    media_type = str(query.get("media_type") or "").strip()
    task_name = str(query.get("task_name") or "").strip()
    if media_type:
        params["type"] = media_type
    if task_name:
        params["task_name"] = task_name
    if include_export_keywords:
        params["include_export_keywords"] = "1"
    url = f"http://127.0.0.1:{port}/api/articles?{urlparse.urlencode(params)}"
    started = time.perf_counter()
    try:
        with urlrequest.urlopen(url, timeout=timeout) as response:
            raw = response.read()
            payload = json.loads(raw.decode("utf-8"))
            return {
                "ok": True,
                "name": str(query.get("name") or ""),
                "query": {
                    "task_name": task_name,
                    "media_type": media_type,
                },
                "status": int(getattr(response, "status", 0) or 0),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "payload": payload,
            }
    except urlerror.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return _article_api_error_result(query, task_name, media_type, started, exc, status=int(exc.code), body=body)
    except Exception as exc:
        return _article_api_error_result(query, task_name, media_type, started, exc)


def _article_api_error_result(
    query: dict[str, str],
    task_name: str,
    media_type: str,
    started: float,
    exc: Exception,
    *,
    status: int = 0,
    body: str = "",
) -> dict[str, Any]:
    return {
        "ok": False,
        "name": str(query.get("name") or ""),
        "query": {
            "task_name": task_name,
            "media_type": media_type,
        },
        "status": status,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "error": exc.__class__.__name__,
        "message": str(exc),
        "body": body[:500],
    }


def _compare_article_api_batches(
    json_results: list[dict[str, Any]],
    sqlite_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sqlite_by_name = {str(item.get("name") or ""): item for item in sqlite_results}
    comparisons: list[dict[str, Any]] = []
    for json_item in json_results:
        name = str(json_item.get("name") or "")
        sqlite_item = sqlite_by_name.get(name, {})
        comparisons.append(_compare_article_api_query(json_item, sqlite_item))
    return comparisons


def _compare_article_api_query(
    json_item: dict[str, Any],
    sqlite_item: dict[str, Any],
) -> dict[str, Any]:
    name = str(json_item.get("name") or sqlite_item.get("name") or "")
    query = json_item.get("query") if isinstance(json_item.get("query"), dict) else sqlite_item.get("query")
    json_payload = json_item.get("payload") if isinstance(json_item.get("payload"), dict) else {}
    sqlite_payload = sqlite_item.get("payload") if isinstance(sqlite_item.get("payload"), dict) else {}

    mismatches: list[str] = []
    if not bool(json_item.get("ok")) or not bool(sqlite_item.get("ok")):
        mismatches.append("request")
    json_total = int(json_payload.get("total") or 0)
    sqlite_total = int(sqlite_payload.get("total") or 0)
    json_today_total = int(json_payload.get("today_total") or 0)
    sqlite_today_total = int(sqlite_payload.get("today_total") or 0)
    json_signatures = _article_api_signatures(json_payload)
    sqlite_signatures = _article_api_signatures(sqlite_payload)
    json_ids = [str(item.get("id") or "").strip() for item in json_signatures]
    sqlite_ids = [str(item.get("id") or "").strip() for item in sqlite_signatures]

    if json_total != sqlite_total:
        mismatches.append("total")
    if json_today_total != sqlite_today_total:
        mismatches.append("today_total")
    if json_ids != sqlite_ids:
        mismatches.append("article_ids")
    field_mismatches = _article_field_mismatches(json_signatures, sqlite_signatures)
    if field_mismatches:
        mismatches.append("article_fields")

    return {
        "ok": not mismatches,
        "name": name,
        "query": query or {},
        "mismatches": mismatches,
        "json": {
            "status": int(json_item.get("status") or 0),
            "elapsed_ms": json_item.get("elapsed_ms"),
            "total": json_total,
            "today_total": json_today_total,
            "ids": json_ids,
            "error": json_item.get("error", ""),
            "message": json_item.get("message", ""),
        },
        "sqlite": {
            "status": int(sqlite_item.get("status") or 0),
            "elapsed_ms": sqlite_item.get("elapsed_ms"),
            "total": sqlite_total,
            "today_total": sqlite_today_total,
            "ids": sqlite_ids,
            "error": sqlite_item.get("error", ""),
            "message": sqlite_item.get("message", ""),
        },
        "field_mismatches": field_mismatches[:5],
    }


def _article_api_signatures(payload: dict[str, Any]) -> list[dict[str, Any]]:
    articles = payload.get("articles") if isinstance(payload, dict) else []
    if not isinstance(articles, list):
        return []
    signatures: list[dict[str, Any]] = []
    for article in articles:
        if not isinstance(article, dict):
            continue
        signatures.append({
            field: _stable_article_api_value(article.get(field))
            for field in ARTICLE_API_COMPARE_FIELDS
        })
    return signatures


def _stable_article_api_value(value: Any) -> Any:
    if isinstance(value, list):
        return [str(item) for item in value]
    if value is None:
        return ""
    return value


def _article_field_mismatches(
    json_signatures: list[dict[str, Any]],
    sqlite_signatures: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    for index, (json_item, sqlite_item) in enumerate(zip(json_signatures, sqlite_signatures)):
        changed = {
            field: {
                "json": json_item.get(field),
                "sqlite": sqlite_item.get(field),
            }
            for field in ARTICLE_API_COMPARE_FIELDS
            if json_item.get(field) != sqlite_item.get(field)
        }
        if changed:
            mismatches.append({
                "index": index,
                "id": json_item.get("id") or sqlite_item.get("id") or "",
                "fields": changed,
            })
    if len(json_signatures) != len(sqlite_signatures):
        mismatches.append({
            "index": min(len(json_signatures), len(sqlite_signatures)),
            "id": "",
            "fields": {
                "length": {
                    "json": len(json_signatures),
                    "sqlite": len(sqlite_signatures),
                },
            },
        })
    return mismatches


def _batch_summary(batch: dict[str, Any]) -> dict[str, Any]:
    results = batch.get("results") if isinstance(batch.get("results"), list) else []
    failed = [item for item in results if not bool(item.get("ok"))]
    return {
        "elapsed_ms": batch.get("elapsed_ms"),
        "fd_before": batch.get("fd_before"),
        "fd_after": batch.get("fd_after"),
        "failed_count": len(failed),
    }


@contextlib.contextmanager
def _temporary_env(name: str, value: str | None):
    marker = object()
    previous = os.environ.get(name, marker)  # type: ignore[arg-type]
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    try:
        yield
    finally:
        if previous is marker:
            os.environ.pop(name, None)
        else:
            os.environ[name] = str(previous)


def _api_compare_worker_count(queries: list[dict[str, str]], *, max_workers: int | None) -> int:
    if len(queries) <= 1:
        return 1
    if max_workers is not None:
        return max(1, min(len(queries), int(max_workers or 1)))
    cpu_count = os.cpu_count() or 2
    return max(1, min(len(queries), cpu_count, 8))


def _open_fd_count() -> int | None:
    for path in (Path("/dev/fd"), Path("/proc/self/fd")):
        try:
            return len(list(path.iterdir()))
        except Exception:
            continue
    return None


if __name__ == "__main__":
    raise SystemExit(main())
