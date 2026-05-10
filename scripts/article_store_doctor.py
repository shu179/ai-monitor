#!/usr/bin/env python3
"""Read-only rollout diagnostics and safe repair tools for ArticleStore SQLite."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import article_store
from core.article_sqlite_store import ArticleSQLiteStore
from core.time_utils import local_now

EXIT_HEALTHY = 0
EXIT_REPAIRABLE = 2
EXIT_SEVERE = 1

IMPORT_META_KEYS = (
    "schema_version",
    "article_store_backend",
    "article_store_import_source",
    "article_store_import_reason",
    "article_store_import_source_path",
    "article_store_import_source_signature",
    "article_store_json_source_signature",
    "article_store_import_source_count",
    "article_store_sqlite_article_count",
    "article_store_import_result",
    "article_store_imported_at",
    "article_store_last_ok_at",
    "article_store_last_error",
    "article_store_last_error_at",
)


@dataclass
class DoctorTarget:
    articles_path: Path
    db_path: Path
    source: str
    explicit: bool


@dataclass
class DoctorOptions:
    data_dir: Path | None = None
    articles_path: Path | None = None
    db_path: Path | None = None
    use_current_data: bool = False
    requested_backend: str | None = None
    check_only: bool = True
    rebuild_sqlite: bool = False
    verify_json_sqlite: bool = False
    full_verify: bool = False
    sample_size: int = 20
    export_sqlite_json: Path | None = None
    force: bool = False
    force_json_env_hint: bool = False
    fallback_ok_exit_zero: bool = False


def run_doctor(options: DoctorOptions | None = None) -> dict[str, Any]:
    opts = options or DoctorOptions()
    target = resolve_target(opts)
    if target is None:
        summary = _base_error_summary(
            "target_required",
            (
                "Pass --data-dir, --articles/--db, or --use-current-data. "
                "The doctor intentionally does not inspect the active user data by default."
            ),
        )
        summary["exit_code"] = EXIT_SEVERE
        return summary

    operations: list[dict[str, Any]] = []
    severe_operation_error = False
    repairable_operation_error = False

    if opts.force_json_env_hint:
        operations.append(_force_json_env_hint())

    if opts.rebuild_sqlite:
        operation = rebuild_sqlite_from_json(target)
        operations.append(operation)
        if not operation.get("ok"):
            severe_operation_error = True

    if opts.verify_json_sqlite:
        operation = verify_json_sqlite(
            target,
            full_verify=opts.full_verify,
            sample_size=opts.sample_size,
        )
        operations.append(operation)
        if not operation.get("ok"):
            repairable_operation_error = True

    if opts.export_sqlite_json is not None:
        operation = export_sqlite_json(
            target,
            output_path=opts.export_sqlite_json,
            force=opts.force,
        )
        operations.append(operation)
        if not operation.get("ok"):
            severe_operation_error = True

    summary = build_doctor_summary(target, requested_backend=opts.requested_backend)
    summary["mode"] = {
        "check_only": bool(opts.check_only),
        "rebuild_sqlite": bool(opts.rebuild_sqlite),
        "verify_json_sqlite": bool(opts.verify_json_sqlite),
        "full_verify": bool(opts.full_verify),
        "export_sqlite_json": str(opts.export_sqlite_json) if opts.export_sqlite_json else "",
        "force_json_env_hint": bool(opts.force_json_env_hint),
    }
    summary["operations"] = operations

    if severe_operation_error:
        summary["ok"] = False
        summary["status"] = "severe_error"
        summary["exit_code"] = EXIT_SEVERE
    elif repairable_operation_error:
        summary["ok"] = False
        summary["status"] = "verification_failed"
        summary["exit_code"] = _repairable_exit_code(opts)
    else:
        summary["exit_code"] = _summary_exit_code(summary, opts)
    return summary


def resolve_target(options: DoctorOptions) -> DoctorTarget | None:
    if options.data_dir is not None:
        data_dir = Path(options.data_dir)
        logs_dir = data_dir / "logs"
        if not logs_dir.exists() and (
            (data_dir / "articles.json").exists()
            or (data_dir / "article_store.sqlite3").exists()
        ):
            logs_dir = data_dir
        return DoctorTarget(
            articles_path=logs_dir / "articles.json",
            db_path=logs_dir / "article_store.sqlite3",
            source="data_dir",
            explicit=True,
        )

    if options.articles_path is not None or options.db_path is not None:
        articles_path = Path(options.articles_path) if options.articles_path is not None else None
        db_path = Path(options.db_path) if options.db_path is not None else None
        if articles_path is None and db_path is not None:
            articles_path = db_path.parent / "articles.json"
        if db_path is None and articles_path is not None:
            db_path = articles_path.parent / "article_store.sqlite3"
        if articles_path is None or db_path is None:
            return None
        return DoctorTarget(
            articles_path=articles_path,
            db_path=db_path,
            source="explicit_paths",
            explicit=True,
        )

    if options.use_current_data:
        return DoctorTarget(
            articles_path=article_store.get_articles_file_path(),
            db_path=article_store.get_article_store_db_path(),
            source="current_data",
            explicit=False,
        )

    return None


def build_doctor_summary(
    target: DoctorTarget,
    *,
    requested_backend: str | None = None,
) -> dict[str, Any]:
    backend_request = _backend_request(requested_backend)
    json_info, articles = _read_json_articles(target.articles_path)
    readiness = ArticleSQLiteStore.validate_readiness(target.db_path)
    sqlite_info = _read_sqlite_info(target.db_path, readiness=readiness)
    freshness = _freshness(json_info, sqlite_info)
    runtime_health = _runtime_health_snapshot()
    migration_state = _migration_state_snapshot()
    resolution = _resolve_backend(
        backend_request,
        readiness=readiness,
        freshness=freshness,
        runtime_health=runtime_health,
    )
    status = _doctor_status(
        resolution,
        json_info=json_info,
        readiness=readiness,
        freshness=freshness,
    )
    recommendations = _recommendations(
        status=status,
        resolution=resolution,
        json_info=json_info,
        readiness=readiness,
        freshness=freshness,
    )
    return {
        "ok": status not in {"severe_error"},
        "status": status,
        "generated_at": local_now().isoformat(timespec="seconds"),
        "target": {
            "source": target.source,
            "explicit": target.explicit,
            "articles_json": str(target.articles_path),
            "db_path": str(target.db_path),
        },
        "backend": {
            **backend_request,
            **resolution,
        },
        "db": {
            "path": str(target.db_path),
            "exists": target.db_path.exists(),
            "size_bytes": _path_size(target.db_path),
            "ready": bool(readiness.get("ready")),
            "readiness": readiness,
        },
        "json": _summary_json_info(json_info),
        "sqlite": sqlite_info,
        "freshness": freshness,
        "article_counts": {
            "json": json_info.get("article_count"),
            "sqlite": sqlite_info.get("article_count"),
            "imported": sqlite_info.get("meta", {}).get("article_store_import_source_count"),
        },
        "migration_state": migration_state,
        "match_refresh_job": article_store.get_article_match_refresh_status(),
        "health": runtime_health,
        "recommendations": recommendations,
        "exit_code_policy": {
            "healthy": EXIT_HEALTHY,
            "forced_json": EXIT_HEALTHY,
            "repairable_fallback": EXIT_REPAIRABLE,
            "repairable_fallback_with_fallback_ok_exit_zero": EXIT_HEALTHY,
            "severe_error": EXIT_SEVERE,
        },
        "loaded_article_count_for_diagnostics": len(articles),
    }


def rebuild_sqlite_from_json(target: DoctorTarget) -> dict[str, Any]:
    started = time.perf_counter()
    json_info, articles = _read_json_articles(target.articles_path)
    if not json_info.get("valid"):
        return _operation_result(
            "rebuild_sqlite",
            ok=False,
            started=started,
            error=f"articles_json_invalid:{json_info.get('load_error') or 'unknown'}",
        )

    target.db_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.db_path.name}.rebuild-",
        suffix=".sqlite3",
        dir=str(target.db_path.parent),
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        tmp_path.unlink(missing_ok=True)
    except TypeError:  # pragma: no cover - Python compatibility guard.
        if tmp_path.exists():
            tmp_path.unlink()

    backups: list[str] = []
    try:
        store = _build_store(tmp_path)
        result = store.import_from_articles(articles, replace=True)
        article_store._write_article_sqlite_import_meta(  # noqa: SLF001
            store,
            imported_articles=articles,
            result=result,
            source_signature=str(json_info.get("source_signature") or ""),
            source_file_signature=tuple(json_info.get("file_signature") or ("", 0, 0)),
            reason="doctor_rebuild",
            source_path=target.articles_path,
        )
        _finalize_sqlite_file(tmp_path)

        temp_target = DoctorTarget(
            articles_path=target.articles_path,
            db_path=tmp_path,
            source="temporary_rebuild",
            explicit=True,
        )
        verification = verify_json_sqlite(temp_target, full_verify=False, sample_size=25)
        if not verification.get("ok"):
            return _operation_result(
                "rebuild_sqlite",
                ok=False,
                started=started,
                error="temporary_db_verification_failed",
                details={"verification": verification},
            )

        final_json_info, _ = _read_json_articles(target.articles_path)
        if final_json_info.get("source_signature") != json_info.get("source_signature"):
            return _operation_result(
                "rebuild_sqlite",
                ok=False,
                started=started,
                error="articles_json_changed_during_rebuild",
                details={
                    "source_signature_before": json_info.get("source_signature"),
                    "source_signature_after": final_json_info.get("source_signature"),
                },
            )

        backups = _backup_existing_sqlite_files(target.db_path)
        os.replace(tmp_path, target.db_path)
        _remove_sqlite_sidecars(target.db_path)
        readiness = ArticleSQLiteStore.validate_readiness(target.db_path)
        return _operation_result(
            "rebuild_sqlite",
            ok=bool(readiness.get("ready")),
            started=started,
            details={
                "db_path": str(target.db_path),
                "source_count": len(articles),
                "import_result": result,
                "backup_paths": backups,
                "readiness": readiness,
                "articles_json_unchanged": True,
                "atomic_replace": True,
            },
            error="" if bool(readiness.get("ready")) else f"target_db_not_ready:{readiness.get('reason')}",
        )
    except Exception as exc:
        return _operation_result(
            "rebuild_sqlite",
            ok=False,
            started=started,
            error=f"{exc.__class__.__name__}: {exc}",
            details={"backup_paths": backups},
        )
    finally:
        _cleanup_sqlite_file(tmp_path)


def verify_json_sqlite(
    target: DoctorTarget,
    *,
    full_verify: bool = False,
    sample_size: int = 20,
) -> dict[str, Any]:
    started = time.perf_counter()
    json_info, articles = _read_json_articles(target.articles_path)
    readiness = ArticleSQLiteStore.validate_readiness(target.db_path)
    sqlite_info = _read_sqlite_info(target.db_path, readiness=readiness)
    freshness = _freshness(json_info, sqlite_info)
    if not json_info.get("valid"):
        return _operation_result(
            "verify_json_sqlite",
            ok=False,
            started=started,
            error=f"articles_json_invalid:{json_info.get('load_error') or 'unknown'}",
            details={"json": _summary_json_info(json_info), "readiness": readiness},
        )
    if not bool(readiness.get("ready")):
        return _operation_result(
            "verify_json_sqlite",
            ok=False,
            started=started,
            error=f"sqlite_not_ready:{readiness.get('reason') or 'not_ready'}",
            details={"json": _summary_json_info(json_info), "readiness": readiness},
        )

    sqlite_by_id = _read_sqlite_articles_by_id(target.db_path)
    sampled_indices = _sample_indices(len(articles), sample_size=sample_size, full_verify=full_verify)
    mismatches: list[dict[str, Any]] = []
    skipped_without_id = 0
    for index in sampled_indices:
        raw_article = articles[index]
        expected = _expected_import_article(raw_article)
        article_id = str(expected.get("id") or "").strip()
        if not article_id:
            skipped_without_id += 1
            continue
        actual = sqlite_by_id.get(article_id)
        if actual != expected:
            mismatches.append(
                {
                    "index": index,
                    "id": article_id,
                    "reason": "missing" if actual is None else "content_mismatch",
                    "expected_digest": _payload_digest(expected),
                    "actual_digest": _payload_digest(actual) if actual is not None else "",
                }
            )
            if len(mismatches) >= 20:
                break

    ok = bool(freshness.get("signature_fresh")) and not mismatches
    return _operation_result(
        "verify_json_sqlite",
        ok=ok,
        started=started,
        error="" if ok else "json_sqlite_mismatch",
        details={
            "db_path": str(target.db_path),
            "articles_json": str(target.articles_path),
            "json_count": json_info.get("article_count"),
            "sqlite_count": sqlite_info.get("article_count"),
            "count_equal": json_info.get("article_count") == sqlite_info.get("article_count"),
            "signature_fresh": freshness.get("signature_fresh"),
            "json_source_signature": json_info.get("source_signature"),
            "stored_json_source_signature": sqlite_info.get("stored_json_source_signature"),
            "sampled": len(sampled_indices),
            "full_verify": bool(full_verify),
            "skipped_without_id": skipped_without_id,
            "mismatch_count": len(mismatches),
            "mismatches": mismatches,
        },
    )


def export_sqlite_json(
    target: DoctorTarget,
    *,
    output_path: Path,
    force: bool = False,
) -> dict[str, Any]:
    started = time.perf_counter()
    output = Path(output_path)
    readiness = ArticleSQLiteStore.validate_readiness(target.db_path)
    if not bool(readiness.get("ready")):
        return _operation_result(
            "export_sqlite_json",
            ok=False,
            started=started,
            error=f"sqlite_not_ready:{readiness.get('reason') or 'not_ready'}",
            details={"readiness": readiness},
        )
    if _same_path(output, target.articles_path) and not force:
        return _operation_result(
            "export_sqlite_json",
            ok=False,
            started=started,
            error="refusing_to_overwrite_articles_json_without_force",
            details={"output_path": str(output), "articles_json": str(target.articles_path)},
        )
    output_existed = output.exists()
    if output_existed and not force:
        return _operation_result(
            "export_sqlite_json",
            ok=False,
            started=started,
            error="output_exists",
            details={"output_path": str(output), "force_required": True},
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    articles = _read_sqlite_articles_ordered(target.db_path)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=str(output.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(articles, fp, ensure_ascii=False, indent=2)
            fp.write("\n")
        os.replace(tmp_path, output)
        return _operation_result(
            "export_sqlite_json",
            ok=True,
            started=started,
            details={
                "output_path": str(output),
                "article_count": len(articles),
                "overwrote_existing": bool(force and output_existed),
            },
        )
    except Exception as exc:
        try:
            os.close(fd)
        except OSError:
            pass
        return _operation_result(
            "export_sqlite_json",
            ok=False,
            started=started,
            error=f"{exc.__class__.__name__}: {exc}",
            details={"output_path": str(output)},
        )
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Diagnose ArticleStore SQLite rollout state. Default mode is read-only and "
            "requires --data-dir, --articles/--db, or --use-current-data."
        )
    )
    parser.add_argument("--data-dir", type=Path, default=None, help="Isolated data dir containing logs/articles.json.")
    parser.add_argument("--articles", dest="articles_path", type=Path, default=None, help="Explicit articles.json path.")
    parser.add_argument("--db", dest="db_path", type=Path, default=None, help="Explicit article_store.sqlite3 path.")
    parser.add_argument(
        "--use-current-data",
        action="store_true",
        help="Inspect the active account-scoped ArticleStore paths. This is never the default.",
    )
    parser.add_argument(
        "--article-store-backend",
        dest="requested_backend",
        choices=("auto", "json", "sqlite"),
        default=None,
        help="Backend request to evaluate. Defaults to AIBRANDMONITOR_ARTICLE_STORE_BACKEND/default auto.",
    )
    parser.add_argument("--check-only", action="store_true", help="Read-only check mode. This is the default.")
    parser.add_argument(
        "--rebuild-sqlite",
        action="store_true",
        help="Explicit write action: rebuild SQLite from JSON via a temporary DB, verify, then atomically replace.",
    )
    parser.add_argument(
        "--verify-json-sqlite",
        action="store_true",
        help="Compare JSON signatures/counts and sampled normalized articles with SQLite.",
    )
    parser.add_argument(
        "--full-verify",
        action="store_true",
        help="With --verify-json-sqlite, compare all JSON articles that have IDs.",
    )
    parser.add_argument("--sample-size", type=int, default=20, help="Sample size for --verify-json-sqlite.")
    parser.add_argument(
        "--export-sqlite-json",
        type=Path,
        default=None,
        help="Explicit write action: export SQLite articles to this JSON file.",
    )
    parser.add_argument("--force", action="store_true", help="Allow export overwrite. Rebuild always backs up first.")
    parser.add_argument(
        "--force-json-env-hint",
        action="store_true",
        help="Print the environment variable hint for forcing JSON. Does not modify the shell.",
    )
    parser.add_argument(
        "--fallback-ok-exit-zero",
        action="store_true",
        help="Return exit 0 instead of 2 when SQLite is unhealthy but JSON fallback is usable.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    summary = run_doctor(
        DoctorOptions(
            data_dir=args.data_dir,
            articles_path=args.articles_path,
            db_path=args.db_path,
            use_current_data=bool(args.use_current_data),
            requested_backend=args.requested_backend,
            check_only=True,
            rebuild_sqlite=bool(args.rebuild_sqlite),
            verify_json_sqlite=bool(args.verify_json_sqlite),
            full_verify=bool(args.full_verify),
            sample_size=int(args.sample_size or 20),
            export_sqlite_json=args.export_sqlite_json,
            force=bool(args.force),
            force_json_env_hint=bool(args.force_json_env_hint),
            fallback_ok_exit_zero=bool(args.fallback_ok_exit_zero),
        )
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return int(summary.get("exit_code") or 0)


def _base_error_summary(reason: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "severe_error",
        "generated_at": local_now().isoformat(timespec="seconds"),
        "error": {"reason": reason, "message": message},
        "exit_code_policy": {
            "healthy": EXIT_HEALTHY,
            "repairable_fallback": EXIT_REPAIRABLE,
            "severe_error": EXIT_SEVERE,
        },
    }


def _backend_request(requested_backend: str | None) -> dict[str, str]:
    if requested_backend is not None:
        raw_value = str(requested_backend or "").strip()
        source = "cli"
    else:
        env_value = os.environ.get(article_store.ARTICLE_STORE_BACKEND_ENV)
        raw_value = "" if env_value is None else str(env_value)
        source = "default_auto" if env_value is None or not str(env_value).strip() else "env"
    normalized = raw_value.strip().lower()
    if normalized in article_store.ARTICLE_STORE_BACKEND_JSON_VALUES:
        requested = "json"
    elif normalized in article_store.ARTICLE_STORE_BACKEND_SQLITE_VALUES:
        requested = "sqlite"
    elif normalized in article_store.ARTICLE_STORE_BACKEND_AUTO_VALUES:
        requested = "auto"
    else:
        requested = "auto"
    return {
        "requested_backend": requested,
        "raw_backend": raw_value,
        "backend_source": source,
    }


def _resolve_backend(
    backend_request: dict[str, str],
    *,
    readiness: dict[str, Any],
    freshness: dict[str, Any],
    runtime_health: dict[str, Any],
) -> dict[str, Any]:
    requested = backend_request.get("requested_backend", "auto")
    result: dict[str, Any] = {
        "effective_backend": "json",
        "fallback_reason": "",
        "db_ready": bool(readiness.get("ready")),
        "migration_scheduled": False,
        "cooldown_remaining_seconds": runtime_health.get("cooldown_remaining_seconds", 0.0),
    }
    if requested == "json":
        result["fallback_reason"] = "forced_json"
        return result
    if requested == "auto" and bool(runtime_health.get("blocked")):
        result["fallback_reason"] = "health_cooldown"
        return result
    if not bool(readiness.get("ready")):
        result["fallback_reason"] = (
            "article_store_db_missing"
            if not readiness.get("available")
            else f"article_store_db_{readiness.get('reason') or 'not_ready'}"
        )
        return result
    if requested == "sqlite" or bool(freshness.get("fresh")):
        result["effective_backend"] = "sqlite"
        return result
    result["fallback_reason"] = (
        "article_store_source_stale"
        if not freshness.get("signature_fresh")
        else "article_store_count_stale"
    )
    return result


def _doctor_status(
    resolution: dict[str, Any],
    *,
    json_info: dict[str, Any],
    readiness: dict[str, Any],
    freshness: dict[str, Any],
) -> str:
    if not json_info.get("valid"):
        if resolution.get("effective_backend") == "sqlite" and readiness.get("ready"):
            return "sqlite_healthy_json_unreadable"
        return "severe_error"
    if resolution.get("fallback_reason") == "forced_json":
        return "forced_json"
    if resolution.get("effective_backend") == "sqlite" and readiness.get("ready"):
        return "healthy" if freshness.get("fresh") or resolution.get("db_ready") else "sqlite_available"
    if resolution.get("fallback_reason"):
        return "fallback_repairable"
    return "healthy"


def _recommendations(
    *,
    status: str,
    resolution: dict[str, Any],
    json_info: dict[str, Any],
    readiness: dict[str, Any],
    freshness: dict[str, Any],
) -> list[str]:
    items: list[str] = []
    if status == "fallback_repairable":
        items.append("Run scripts/article_store_doctor.py --rebuild-sqlite with the same target paths to repair SQLite from JSON.")
        items.append("Use --force-json-env-hint to print the rollback environment variable for forcing JSON.")
    if not readiness.get("ready"):
        items.append(f"SQLite DB is not ready: {readiness.get('reason') or 'not_ready'}.")
    if readiness.get("ready") and not freshness.get("signature_fresh"):
        items.append("SQLite import metadata is stale relative to articles.json; rebuild before default rollout.")
    if not json_info.get("valid"):
        items.append("articles.json could not be parsed as a list; fix JSON or export from SQLite to a new file.")
    if resolution.get("fallback_reason") == "health_cooldown":
        items.append("Runtime health cooldown is active; inspect recent SQLite errors before rollout.")
    if not items and status in {"healthy", "forced_json"}:
        items.append("No repair action required.")
    return items


def _runtime_health_snapshot() -> dict[str, Any]:
    with article_store._article_store_backend_health_lock:  # noqa: SLF001
        health = dict(article_store._article_store_backend_health)  # noqa: SLF001
    cooldown_remaining = article_store._article_store_sqlite_cooldown_remaining()  # noqa: SLF001
    health["blocked"] = cooldown_remaining > 0
    health["cooldown_remaining_seconds"] = round(cooldown_remaining, 3)
    health["error_limit"] = article_store._article_sqlite_error_limit()  # noqa: SLF001
    health["cooldown_seconds"] = article_store._article_sqlite_cooldown_seconds()  # noqa: SLF001
    return health


def _migration_state_snapshot() -> dict[str, Any]:
    with article_store._article_store_migration_lock:  # noqa: SLF001
        state = dict(article_store._article_store_migration_state)  # noqa: SLF001
        state["thread_count"] = len(
            [thread for thread in article_store._article_store_migration_threads if thread.is_alive()]  # noqa: SLF001
        )
    return state


def _read_json_articles(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    file_signature = article_store._json_document_signature(  # noqa: SLF001
        "article_store/articles",
        path,
        use_sqlite=False,
    )
    source_signature = _json_source_signature(file_signature)
    info: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "size_bytes": _path_size(path),
        "file_signature": list(file_signature),
        "file_signature_text": _json_file_signature_text(file_signature),
        "source_signature": source_signature,
        "article_count": 0,
        "skipped_non_objects": 0,
        "valid": True,
        "load_error": "",
    }
    if not path.exists():
        return info, []
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        info.update({"valid": False, "load_error": f"{exc.__class__.__name__}: {exc}"})
        return info, []
    if not isinstance(parsed, list):
        info.update({"valid": False, "load_error": "articles_json_not_list"})
        return info, []
    articles = [item for item in parsed if isinstance(item, dict)]
    info["article_count"] = len(articles)
    info["skipped_non_objects"] = len(parsed) - len(articles)
    return info, articles


def _summary_json_info(json_info: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in json_info.items() if key != "articles"}


def _read_sqlite_info(db_path: Path, *, readiness: dict[str, Any]) -> dict[str, Any]:
    info: dict[str, Any] = {
        "path": str(db_path),
        "exists": db_path.exists(),
        "ready": bool(readiness.get("ready")),
        "article_count": None,
        "source_signature": "",
        "file_signature": None,
        "meta": {},
        "stored_json_source_signature": "",
        "stored_json_file_signature": "",
        "read_error": "",
    }
    if not bool(readiness.get("ready")):
        return info
    try:
        with _connect_readonly(db_path) as conn:
            meta = {
                str(row[0] or ""): str(row[1] or "")
                for row in conn.execute(
                    "SELECT key, value FROM store_meta WHERE key IN (%s)"
                    % ",".join("?" for _ in IMPORT_META_KEYS),
                    IMPORT_META_KEYS,
                ).fetchall()
            }
            count_row = conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(updated_at_ns), 0) FROM articles"
            ).fetchone()
            count = int((count_row or [0, 0])[0] or 0)
            max_updated_at_ns = int((count_row or [0, 0])[1] or 0)
            signature = _sqlite_source_signature(conn, db_path, count=count, max_updated_at_ns=max_updated_at_ns)
        info.update(
            {
                "article_count": count,
                "source_signature": signature,
                "file_signature": [signature, max_updated_at_ns, count],
                "meta": meta,
                "stored_json_source_signature": str(meta.get("article_store_json_source_signature") or "").strip(),
                "stored_json_file_signature": str(meta.get("article_store_import_source_signature") or "").strip(),
            }
        )
    except Exception as exc:
        info["read_error"] = f"{exc.__class__.__name__}: {exc}"
    return info


def _freshness(json_info: dict[str, Any], sqlite_info: dict[str, Any]) -> dict[str, Any]:
    stored_json_signature = str(sqlite_info.get("stored_json_source_signature") or "").strip()
    stored_file_signature = str(sqlite_info.get("stored_json_file_signature") or "").strip()
    current_json_signature = str(json_info.get("source_signature") or "").strip()
    current_file_signature = str(json_info.get("file_signature_text") or "").strip()
    signature_fresh = (
        bool(stored_json_signature) and stored_json_signature == current_json_signature
    ) or (
        bool(stored_file_signature) and stored_file_signature == current_file_signature
    )
    imported_count = _safe_int(sqlite_info.get("meta", {}).get("article_store_import_source_count"))
    json_count = json_info.get("article_count")
    sqlite_count = sqlite_info.get("article_count")
    has_import_meta = bool(stored_json_signature or stored_file_signature)
    count_fresh = True if has_import_meta else json_count is not None and sqlite_count == json_count
    return {
        "fresh": bool(signature_fresh and count_fresh),
        "signature_fresh": bool(signature_fresh),
        "count_fresh": bool(count_fresh),
        "count_equal": json_count == sqlite_count,
        "json_source_signature": current_json_signature,
        "json_file_signature": json_info.get("file_signature"),
        "json_file_signature_text": current_file_signature,
        "stored_json_source_signature": stored_json_signature,
        "stored_json_file_signature": stored_file_signature,
        "json_count": json_count,
        "sqlite_count": sqlite_count,
        "imported_count": imported_count,
    }


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)


def _sqlite_source_signature(
    conn: sqlite3.Connection,
    db_path: Path,
    *,
    count: int,
    max_updated_at_ns: int,
) -> str:
    digest = hashlib.sha256()
    for article_id, raw_json in conn.execute(
        "SELECT id, raw_json FROM articles ORDER BY id"
    ).fetchall():
        digest.update(str(article_id or "").strip().encode("utf-8", errors="ignore"))
        digest.update(b"\0")
        digest.update(str(raw_json or "").encode("utf-8", errors="ignore"))
        digest.update(b"\0")
    return f"{db_path}::articles:{count}:{max_updated_at_ns}:{digest.hexdigest()}"


def _read_sqlite_articles_by_id(db_path: Path) -> dict[str, dict[str, Any]]:
    with _connect_readonly(db_path) as conn:
        rows = conn.execute("SELECT id, raw_json FROM articles").fetchall()
    result: dict[str, dict[str, Any]] = {}
    for article_id, raw_json in rows:
        parsed = _json_loads(raw_json)
        result[str(article_id or "").strip()] = parsed
    return result


def _read_sqlite_articles_ordered(db_path: Path) -> list[dict[str, Any]]:
    with _connect_readonly(db_path) as conn:
        rows = conn.execute(
            """
            SELECT raw_json
            FROM articles
            ORDER BY sort_published_ts DESC, sort_imported_ts DESC, id DESC
            """
        ).fetchall()
    return [_json_loads(row[0]) for row in rows]


def _expected_import_article(raw_article: dict[str, Any]) -> dict[str, Any]:
    article = dict(raw_article)
    article.pop("_match_signature", None)
    article.pop("_match_config_signature", None)
    normalized, _ = article_store._normalize_article_entry(article)  # noqa: SLF001
    return normalized


def _sample_indices(total: int, *, sample_size: int, full_verify: bool) -> list[int]:
    if total <= 0:
        return []
    if full_verify:
        return list(range(total))
    capped = max(1, min(total, int(sample_size or 20)))
    if capped == total:
        return list(range(total))
    if capped == 1:
        return [0]
    indices = {0, total - 1}
    step = max(1, total // max(1, capped - 1))
    index = 0
    while len(indices) < capped and index < total:
        indices.add(index)
        index += step
    index = total - 1
    while len(indices) < capped and index >= 0:
        indices.add(index)
        index -= 1
    return sorted(indices)


def _build_store(db_path: Path) -> ArticleSQLiteStore:
    return ArticleSQLiteStore(
        db_path,
        normalize_article_url=article_store.normalize_article_url,
        normalize_article_entry=lambda item: article_store._normalize_article_entry(item)[0],  # noqa: SLF001
        now_text=article_store._article_now_minute_text,  # noqa: SLF001
    )


def _finalize_sqlite_file(db_path: Path) -> None:
    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode=DELETE")
    except Exception:
        pass
    _remove_sqlite_sidecars(db_path)


def _backup_existing_sqlite_files(db_path: Path) -> list[str]:
    backups: list[str] = []
    timestamp = local_now().strftime("%Y%m%d%H%M%S")
    for path in [db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")]:
        if not path.exists():
            continue
        backup = path.with_name(f"{path.name}.bak-{timestamp}")
        suffix = 1
        while backup.exists():
            backup = path.with_name(f"{path.name}.bak-{timestamp}-{suffix}")
            suffix += 1
        shutil.copy2(path, backup)
        backups.append(str(backup))
    return backups


def _remove_sqlite_sidecars(db_path: Path) -> None:
    for sidecar in (Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        try:
            sidecar.unlink()
        except FileNotFoundError:
            pass


def _cleanup_sqlite_file(db_path: Path) -> None:
    for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _force_json_env_hint() -> dict[str, Any]:
    return {
        "name": "force_json_env_hint",
        "ok": True,
        "details": {
            "env": f"{article_store.ARTICLE_STORE_BACKEND_ENV}=json",
            "shell_examples": [
                f"export {article_store.ARTICLE_STORE_BACKEND_ENV}=json",
                f"{article_store.ARTICLE_STORE_BACKEND_ENV}=json python3 main.py",
            ],
            "modified_shell": False,
        },
    }


def _operation_result(
    name: str,
    *,
    ok: bool,
    started: float,
    error: str = "",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "ok": bool(ok),
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "error": str(error or ""),
        "details": details or {},
    }


def _summary_exit_code(summary: dict[str, Any], options: DoctorOptions) -> int:
    status = str(summary.get("status") or "")
    if status in {"severe_error"}:
        return EXIT_SEVERE
    if status in {"fallback_repairable", "verification_failed"}:
        return _repairable_exit_code(options)
    return EXIT_HEALTHY


def _repairable_exit_code(options: DoctorOptions) -> int:
    return EXIT_HEALTHY if options.fallback_ok_exit_zero else EXIT_REPAIRABLE


def _json_source_signature(file_signature: tuple[str, int, int]) -> str:
    payload = {"version": 1, "articles": file_signature}
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _json_file_signature_text(file_signature: tuple[str, int, int]) -> str:
    return json.dumps(file_signature, ensure_ascii=False, default=str, separators=(",", ":"))


def _payload_digest(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _json_loads(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _path_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except FileNotFoundError:
        return 0
    except Exception:
        return -1


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except Exception:
        return str(left) == str(right)


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
