#!/usr/bin/env python3
"""Synthetic history storage scale and readiness benchmark.

The benchmark never touches the user's real history by default. It creates an
isolated data dir, seeds synthetic JSON history plus a fresh structured SQLite
shadow, then measures the current high-level read/write paths and a bounded
SQLite page read.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import history
from core.article_history_sqlite_store import ArticleHistorySQLiteStore
from core.time_utils import local_now, local_today


DEFAULT_RECORD_COUNT = 4_800
DEFAULT_TASK_COUNT = 12
DEFAULT_PAGE_LIMIT = 50
SQLITE_FD_GROWTH_LIMIT = 8


@dataclass
class BenchmarkOptions:
    record_count: int = DEFAULT_RECORD_COUNT
    task_count: int = DEFAULT_TASK_COUNT
    page_limit: int = DEFAULT_PAGE_LIMIT
    history_read_backend: str = "auto"
    history_write_backend: str = "json"
    history_storage_backend: str = "json"
    shadow_writes: bool = True
    data_dir: Path | None = None
    force: bool = False
    keep_data: bool = False


def build_synthetic_history_sources(
    record_count: int = DEFAULT_RECORD_COUNT,
    *,
    task_count: int = DEFAULT_TASK_COUNT,
    today: date | None = None,
) -> dict[str, list[dict[str, Any]]]:
    total = max(0, int(record_count or 0))
    task_total = max(1, int(task_count or 1))
    current_day = today or local_today()
    sources: dict[str, list[dict[str, Any]]] = {}
    for index in range(total):
        task_index = index % task_total
        task_id = f"task-{task_index}"
        task_name = f"Brand {task_index}"
        storage_key = task_id
        day = current_day - timedelta(days=index % 45)
        success = index % 4 != 0
        rank = (index % 8) + 1 if success else 99
        record = {
            "id": f"record-{index}",
            "ts": f"{day.isoformat()} {index % 24:02d}:{index % 60:02d}",
            "task_id": task_id,
            "task_name": task_name,
            "platform": ("doubao", "kimi", "deepseek", "chatgpt")[index % 4],
            "keyword": f"{task_name} keyword {index % 11}",
            "brand": task_name,
            "rank": rank,
            "success": success,
            "review_status": "pending" if success and rank != 99 and index % 5 == 0 else "",
            "review_note": "",
            "reviewed_at": "",
            "mode": "recognition" if index % 13 == 0 else "",
            "execution_source": "scheduled",
        }
        sources.setdefault(storage_key, []).append(record)
    return sources


def run_benchmark(options: BenchmarkOptions | None = None) -> dict[str, Any]:
    opts = options or BenchmarkOptions()
    temp_handle: tempfile.TemporaryDirectory[str] | None = None
    if opts.data_dir is None:
        if opts.keep_data:
            data_dir = Path(tempfile.mkdtemp(prefix="history-scale-benchmark-"))
        else:
            temp_handle = tempfile.TemporaryDirectory(prefix="history-scale-benchmark-")
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
    today = local_today()
    sources = build_synthetic_history_sources(
        opts.record_count,
        task_count=opts.task_count,
        today=today,
    )
    task_specs = [(f"Brand {index}", f"task-{index}") for index in range(max(1, int(opts.task_count or 1)))]

    with isolated_history_paths(
        data_dir,
        history_read_backend=opts.history_read_backend,
        history_write_backend=opts.history_write_backend,
        history_storage_backend=opts.history_storage_backend,
        shadow_writes=opts.shadow_writes,
    ):
        _seed_history_sources(sources)
        readiness = ArticleHistorySQLiteStore.validate_readiness(paths["shadow_db"])
        operations = [
            _measure_operation(
                "read_list_get_records_many",
                lambda: _read_list_get_records_many(task_specs),
                [paths["shadow_db"], paths["local_store_db"]],
            ),
            _measure_operation(
                "read_page_sqlite_shadow",
                lambda: _read_page_sqlite_shadow("task-0", opts.page_limit, paths["shadow_db"]),
                [paths["shadow_db"], paths["local_store_db"]],
            ),
            _measure_operation(
                "append_record",
                lambda: _append_record(),
                [paths["shadow_db"], paths["local_store_db"]],
            ),
            _measure_operation(
                "import_records_merge",
                lambda: _import_records_merge(),
                [paths["shadow_db"], paths["local_store_db"]],
            ),
            _measure_operation(
                "apply_review",
                lambda: _apply_pending_review(),
                [paths["shadow_db"], paths["local_store_db"]],
            ),
        ]
        health = history.get_structured_read_health()
        final_readiness = ArticleHistorySQLiteStore.validate_readiness(paths["shadow_db"])
        counts = _history_counts(paths["shadow_db"], sources)

    summary: dict[str, Any] = {
        "ok": all(bool(item.get("ok")) for item in operations),
        "generated_at": local_now().isoformat(timespec="seconds"),
        "data_dir": str(data_dir),
        "input": {
            "record_count": int(opts.record_count),
            "task_count": int(opts.task_count),
            "page_limit": int(opts.page_limit),
            "history_read_backend": _normalize_history_read_backend(opts.history_read_backend),
            "history_write_backend": _normalize_history_write_backend(opts.history_write_backend),
            "history_storage_backend": _normalize_history_storage_backend(opts.history_storage_backend),
            "shadow_writes": bool(opts.shadow_writes),
        },
        "paths": {
            "history_dir": str(paths["history"]),
            "local_store_db": str(paths["local_store_db"]),
            "sqlite_shadow_db": str(paths["shadow_db"]),
        },
        "counts": counts,
        "backend": {
            "effective_backend": health.get("effectiveBackend", "json"),
            "requested_backend": health.get("requestedBackend", "json"),
            "storage_backend": health.get("writePath", {}).get("effectiveBackend", "json_file"),
            "shadow_writes_enabled": health.get("shadowWritesEnabled", False),
        },
        "readiness": final_readiness,
        "initial_readiness": readiness,
        "fallback": {
            "last_fallback_reason": health.get("last_fallback_reason", ""),
            "readiness_reason": health.get("readinessReason", ""),
            "authoritative_reason": health.get("authoritativeReadiness", {}).get("reason", ""),
        },
        "health": health,
        "operations": operations,
        "standards": evaluate_standards(operations, health),
    }
    return summary


def _prepare_data_dir(data_dir: Path, *, force: bool) -> dict[str, Path]:
    logs_dir = data_dir / "logs"
    paths = {
        "logs": logs_dir,
        "history": logs_dir / "history",
        "local_store_db": logs_dir / "local_store.sqlite3",
        "shadow_db": logs_dir / "article_history_shadow.sqlite3",
    }
    if paths["history"].exists() and any(paths["history"].glob("*.json")) and not force:
        raise FileExistsError(
            f"{paths['history']} already has JSON files; pass --force to overwrite an explicit benchmark data dir"
        )
    logs_dir.mkdir(parents=True, exist_ok=True)
    paths["history"].mkdir(parents=True, exist_ok=True)
    if force:
        for path in list(paths["history"].glob("*.json")):
            path.unlink()
        lock_dir = paths["history"] / ".locks"
        if lock_dir.exists():
            shutil.rmtree(lock_dir)
        for path in (paths["local_store_db"], paths["shadow_db"]):
            for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass
    return paths


@contextlib.contextmanager
def isolated_history_paths(
    data_dir: Path,
    *,
    history_read_backend: str = "auto",
    history_write_backend: str = "json",
    history_storage_backend: str = "json",
    shadow_writes: bool = True,
):
    logs_dir = data_dir / "logs"
    original_paths = {
        "DEFAULT_HISTORY_DIR": history.DEFAULT_HISTORY_DIR,
        "HISTORY_DIR": history.HISTORY_DIR,
        "LOCAL_STORE_DB_FILE": history.LOCAL_STORE_DB_FILE,
        "HISTORY_SHADOW_DB_FILE": history.HISTORY_SHADOW_DB_FILE,
    }
    original_env = {
        history.STORAGE_BACKEND_ENV: os.environ.get(history.STORAGE_BACKEND_ENV),
        history.STRUCTURED_READ_BACKEND_ENV: os.environ.get(history.STRUCTURED_READ_BACKEND_ENV),
        history.STRUCTURED_SHADOW_WRITE_ENV: os.environ.get(history.STRUCTURED_SHADOW_WRITE_ENV),
        history.STRUCTURED_WRITE_BACKEND_ENV: os.environ.get(history.STRUCTURED_WRITE_BACKEND_ENV),
    }
    try:
        history.DEFAULT_HISTORY_DIR = logs_dir / "history"
        history.HISTORY_DIR = history.DEFAULT_HISTORY_DIR
        history.LOCAL_STORE_DB_FILE = logs_dir / "local_store.sqlite3"
        history.HISTORY_SHADOW_DB_FILE = logs_dir / "article_history_shadow.sqlite3"
        storage_backend = _normalize_history_storage_backend(history_storage_backend)
        if storage_backend == "sqlite_document":
            os.environ[history.STORAGE_BACKEND_ENV] = "sqlite"
        else:
            os.environ[history.STORAGE_BACKEND_ENV] = "json"
        read_backend = _normalize_history_read_backend(history_read_backend)
        if read_backend == "json":
            os.environ[history.STRUCTURED_READ_BACKEND_ENV] = "json"
        elif read_backend == "sqlite_shadow":
            os.environ[history.STRUCTURED_READ_BACKEND_ENV] = "sqlite_shadow"
        else:
            os.environ[history.STRUCTURED_READ_BACKEND_ENV] = "auto"
        write_backend = _normalize_history_write_backend(history_write_backend)
        if write_backend == "sqlite_structured":
            os.environ[history.STRUCTURED_WRITE_BACKEND_ENV] = "sqlite_structured"
        else:
            os.environ[history.STRUCTURED_WRITE_BACKEND_ENV] = "json"
        os.environ[history.STRUCTURED_SHADOW_WRITE_ENV] = "1" if shadow_writes else "0"
        history.configure_structured_history_storage({})
        history.reset_structured_read_health()
        yield
    finally:
        for name, value in original_paths.items():
            setattr(history, name, value)
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        history.configure_structured_history_storage({})
        history.reset_structured_read_health()


def _seed_history_sources(sources: dict[str, list[dict[str, Any]]]) -> None:
    history.get_history_dir().mkdir(parents=True, exist_ok=True)
    sqlite_sources: dict[str, list[dict[str, Any]]] = {}
    for storage_key, records in sources.items():
        task_name = str((records[0] if records else {}).get("task_name") or storage_key)
        primary_records = [dict(record) for record in records]
        legacy_key = history._safe_name(task_name)  # noqa: SLF001
        legacy_records = [dict(record) for record in records]
        _write_json(history._task_file(storage_key), primary_records)  # noqa: SLF001
        _write_json(history._task_file(task_name), legacy_records)  # noqa: SLF001
        sqlite_sources[storage_key] = primary_records
        sqlite_sources[legacy_key] = legacy_records
    store = ArticleHistorySQLiteStore(history.HISTORY_SHADOW_DB_FILE)
    store.import_history_sources(sqlite_sources, replace=True)
    store.set_meta("history_source_signature", history.get_history_source_signature())


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _read_list_get_records_many(task_specs: list[tuple[str, str]]) -> dict[str, Any]:
    records = history.get_records_many(task_specs)
    health = history.get_structured_read_health()
    return {
        "effective_backend": health.get("effectiveBackend", "json"),
        "task_count": len(task_specs),
        "records_returned": sum(len(items) for items in records),
        "per_task_counts": [len(items) for items in records[:10]],
        "fallback_reason": health.get("last_fallback_reason", ""),
    }


def _read_page_sqlite_shadow(storage_key: str, page_limit: int, db_path: Path) -> dict[str, Any]:
    readiness = ArticleHistorySQLiteStore.validate_readiness(db_path)
    if not bool(readiness.get("ready")):
        return {
            "effective_backend": "json",
            "returned_count": 0,
            "fallback_reason": str(readiness.get("reason") or "shadow_db_not_ready"),
            "readiness": readiness,
        }
    store = ArticleHistorySQLiteStore(db_path)
    records = store.get_history_records(storage_key, limit=max(1, int(page_limit or 1)), offset=0)
    return {
        "effective_backend": "sqlite_shadow",
        "storage_key": storage_key,
        "returned_count": len(records),
        "limit": max(1, int(page_limit or 1)),
        "first_id": str((records[0] if records else {}).get("id") or ""),
        "fallback_reason": "",
        "readiness": readiness,
    }


def _append_record() -> dict[str, Any]:
    before = len(history.get_records("Brand 0", task_id="task-0"))
    entry = history.record(
        "Brand 0",
        "doubao",
        "Brand 0 benchmark append",
        "Brand 0",
        1,
        True,
        {"execution_source": "benchmark"},
        task_id="task-0",
    )
    after = len(history.get_records("Brand 0", task_id="task-0"))
    health = history.get_structured_read_health()
    return {
        "effective_backend": health.get("writePath", {}).get("effectiveBackend", "json_file"),
        "structured_shadow_writes": health.get("shadowWritesEnabled", False),
        "known_full_document_rewrite": health.get("writePath", {}).get("knownFullDocumentRewrite", True),
        "record_id": entry.get("id", ""),
        "before_count": before,
        "after_count": after,
    }


def _import_records_merge() -> dict[str, Any]:
    entry = {
        "id": "benchmark-import-new",
        "ts": local_now().strftime("%Y-%m-%d %H:%M"),
        "task_id": "task-1",
        "task_name": "Brand 1",
        "platform": "kimi",
        "keyword": "Brand 1 benchmark import",
        "brand": "Brand 1",
        "rank": 2,
        "success": True,
        "review_status": "pending",
        "execution_source": "benchmark",
    }
    imported = history.import_records("Brand 1", [entry], task_id="task-1")
    health = history.get_structured_read_health()
    return {
        "effective_backend": health.get("writePath", {}).get("effectiveBackend", "json_file"),
        "structured_shadow_writes": health.get("shadowWritesEnabled", False),
        "known_full_document_rewrite": health.get("writePath", {}).get("knownFullDocumentRewrite", True),
        "imported": imported,
        "record_id": entry["id"],
    }


def _apply_pending_review() -> dict[str, Any]:
    pending = history.get_pending_reviews(limit=1)
    record = pending[0] if pending else {}
    task_name = str(record.get("task_name") or "Brand 0")
    task_id = str(record.get("task_id") or "task-0")
    record_id = str(record.get("id") or "")
    changed = history.apply_review(task_name, record_id, "approved", "benchmark", task_id=task_id) if record_id else False
    health = history.get_structured_read_health()
    return {
        "effective_backend": health.get("writePath", {}).get("effectiveBackend", "json_file"),
        "structured_shadow_writes": health.get("shadowWritesEnabled", False),
        "known_full_document_rewrite": health.get("writePath", {}).get("knownFullDocumentRewrite", True),
        "record_id": record_id,
        "changed": changed,
    }


def _history_counts(db_path: Path, sources: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    readiness = ArticleHistorySQLiteStore.validate_readiness(db_path)
    sqlite_storage_keys = []
    sqlite_record_count = 0
    if bool(readiness.get("ready")):
        store = ArticleHistorySQLiteStore(db_path)
        sqlite_storage_keys = store.list_history_storage_keys()
        sqlite_record_count = sum(store.get_history_record_count(key) for key in sqlite_storage_keys)
    json_files = list(history.get_history_dir().glob("*.json"))
    return {
        "record_count": sum(len(records) for records in sources.values()),
        "task_count": len(sources),
        "json_file_count": len(json_files),
        "sqlite_storage_key_count": len(sqlite_storage_keys),
        "sqlite_record_count": sqlite_record_count,
    }


def evaluate_standards(operations: list[dict[str, Any]], health: dict[str, Any]) -> dict[str, Any]:
    operation_map = {str(item.get("name") or ""): item for item in operations}
    fd_deltas = [
        item.get("sqlite_fd_delta")
        for item in operations
        if isinstance(item.get("sqlite_fd_delta"), int)
    ]
    max_fd_delta = max(fd_deltas) if fd_deltas else 0
    read_page = operation_map.get("read_page_sqlite_shadow", {})
    read_page_details = read_page.get("details") if isinstance(read_page.get("details"), dict) else {}
    write_path = health.get("writePath") if isinstance(health.get("writePath"), dict) else {}
    known_rewrite = bool(write_path.get("knownFullDocumentRewrite", True))
    return {
        "sqlite_shadow_page_read": {
            "status": "pass" if read_page_details.get("effective_backend") == "sqlite_shadow" else "warn",
            "returned_count": read_page_details.get("returned_count", 0),
            "fallback_reason": read_page_details.get("fallback_reason", ""),
        },
        "sqlite_fd_growth": {
            "status": "pass" if max_fd_delta <= SQLITE_FD_GROWTH_LIMIT else "warn",
            "max_sqlite_fd_delta": max_fd_delta,
            "limit": SQLITE_FD_GROWTH_LIMIT,
        },
        "write_path_bottlenecks": {
            "status": "known_bottleneck" if known_rewrite else "pass",
            "effective_backend": write_path.get("effectiveBackend", "json_file"),
            "operations": write_path.get("operations", {}),
            "note": (
                (
                    "Runtime append/import/review still keep JSON documents as authoritative and rewrite the task document. "
                    "Structured SQLite is currently a guarded read/shadow-write path."
                )
                if known_rewrite
                else "Runtime append/import/review used the explicit structured SQLite authoritative write path."
            ),
        },
        "authoritative_readiness": health.get("authoritativeReadiness", {}),
    }


def _measure_operation(name: str, func: Callable[[], Any], db_paths: list[Path]) -> dict[str, Any]:
    fd_before = _open_fd_count()
    sqlite_fd_before = _open_sqlite_fd_count(db_paths)
    started = time.perf_counter()
    ok = True
    error = ""
    details: Any = None
    try:
        details = func()
    except Exception as exc:  # pragma: no cover - intended for manual benchmark failures.
        ok = False
        error = f"{exc.__class__.__name__}: {exc}"
    elapsed_ms = _elapsed_ms_since(started)
    fd_after = _open_fd_count()
    sqlite_fd_after = _open_sqlite_fd_count(db_paths)
    result = {
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
        result["error"] = error
    return result


def _normalize_history_read_backend(value: str) -> str:
    backend = str(value or "auto").strip().lower()
    if backend in {"sqlite", "sqlite_shadow", "sqlite_structured", "structured"}:
        return "sqlite_shadow"
    if backend in {"json", "file", "files", "off", "disabled"}:
        return "json"
    return "auto"


def _normalize_history_storage_backend(value: str) -> str:
    backend = str(value or "json").strip().lower().replace("-", "_")
    if backend in {"sqlite", "sqlite_document", "sqlite_json_document", "db", "database"}:
        return "sqlite_document"
    return "json"


def _normalize_history_write_backend(value: str) -> str:
    backend = str(value or "json").strip().lower().replace("-", "_")
    if backend in {"sqlite", "sqlite_structured", "structured_sqlite", "structured"}:
        return "sqlite_structured"
    return "json"


def _open_fd_count() -> int | None:
    for path in (Path("/dev/fd"), Path("/proc/self/fd")):
        try:
            return len(list(path.iterdir()))
        except Exception:
            continue
    return None


def _open_sqlite_fd_count(db_paths: list[Path]) -> int | None:
    target_paths = set()
    for db_path in db_paths:
        target_paths.update({str(db_path), f"{db_path}-wal", f"{db_path}-shm"})
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
    parser = argparse.ArgumentParser(description="Benchmark synthetic history SQLite readiness and write paths.")
    parser.add_argument("--record-count", type=int, default=DEFAULT_RECORD_COUNT, help="Synthetic history record count.")
    parser.add_argument("--task-count", type=int, default=DEFAULT_TASK_COUNT, help="Synthetic task count.")
    parser.add_argument("--page-limit", type=int, default=DEFAULT_PAGE_LIMIT, help="Bounded SQLite page read limit.")
    parser.add_argument(
        "--history-read-backend",
        choices=("json", "auto", "sqlite_shadow"),
        default="auto",
        help="High-level history read backend to exercise.",
    )
    parser.add_argument(
        "--history-storage-backend",
        choices=("json", "sqlite_document"),
        default="json",
        help="Authoritative JSON-document storage path to exercise.",
    )
    parser.add_argument(
        "--history-write-backend",
        choices=("json", "sqlite_structured"),
        default="json",
        help="Runtime history write backend for record/import/review measurements.",
    )
    parser.add_argument("--disable-shadow-writes", action="store_true", help="Disable structured shadow write-through.")
    parser.add_argument("--data-dir", type=Path, default=None, help="Explicit isolated benchmark data dir. Defaults to tempfile.")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing explicit benchmark data dir.")
    parser.add_argument("--keep-data", action="store_true", help="Keep a generated tempfile data dir after the run.")
    parser.add_argument("--output", type=Path, default=None, help="Optional JSON summary output path.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    summary = run_benchmark(
        BenchmarkOptions(
            record_count=args.record_count,
            task_count=args.task_count,
            page_limit=args.page_limit,
            history_read_backend=args.history_read_backend,
            history_write_backend=args.history_write_backend,
            history_storage_backend=args.history_storage_backend,
            shadow_writes=not args.disable_shadow_writes,
            data_dir=args.data_dir,
            force=args.force,
            keep_data=args.keep_data,
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
