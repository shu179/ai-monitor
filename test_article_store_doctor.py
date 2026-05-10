from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import core.article_store as article_store
from core.article_sqlite_store import ArticleSQLiteStore
from scripts.article_store_doctor import DoctorOptions, run_doctor


@pytest.fixture(autouse=True)
def reset_article_store_runtime_state():
    original_backend = os.environ.get(article_store.ARTICLE_STORE_BACKEND_ENV)
    article_store.reset_article_store_backend_health_for_tests()
    try:
        yield
    finally:
        if original_backend is None:
            os.environ.pop(article_store.ARTICLE_STORE_BACKEND_ENV, None)
        else:
            os.environ[article_store.ARTICLE_STORE_BACKEND_ENV] = original_backend
        article_store.reset_article_store_backend_health_for_tests()


def test_doctor_requires_explicit_target_by_default() -> None:
    summary = run_doctor(DoctorOptions())

    assert summary["ok"] is False
    assert summary["status"] == "severe_error"
    assert summary["error"]["reason"] == "target_required"
    assert summary["exit_code"] == 1


def test_doctor_check_only_does_not_create_missing_db_and_recommends_repair(tmp_path: Path) -> None:
    articles_path, db_path = _seed_articles(tmp_path, [{"id": "article-a", "url": "https://example.com/a", "title": "A"}])
    before_text = articles_path.read_text(encoding="utf-8")

    summary = run_doctor(
        DoctorOptions(
            articles_path=articles_path,
            db_path=db_path,
            requested_backend="auto",
            check_only=True,
        )
    )

    assert summary["status"] == "fallback_repairable"
    assert summary["backend"]["effective_backend"] == "json"
    assert summary["backend"]["fallback_reason"] == "article_store_db_missing"
    assert any("--rebuild-sqlite" in item for item in summary["recommendations"])
    assert summary["exit_code"] == 2
    assert not db_path.exists()
    assert articles_path.read_text(encoding="utf-8") == before_text


def test_doctor_reports_fresh_sqlite_as_healthy(tmp_path: Path) -> None:
    _seed_articles(tmp_path, [{"id": "article-a", "url": "https://example.com/a", "title": "A"}])

    rebuild = run_doctor(
        DoctorOptions(
            data_dir=tmp_path,
            requested_backend="auto",
            rebuild_sqlite=True,
            verify_json_sqlite=True,
        )
    )
    summary = run_doctor(DoctorOptions(data_dir=tmp_path, requested_backend="auto"))

    assert rebuild["operations"][0]["name"] == "rebuild_sqlite"
    assert rebuild["operations"][0]["ok"] is True
    assert summary["status"] == "healthy"
    assert summary["backend"]["effective_backend"] == "sqlite"
    assert summary["freshness"]["fresh"] is True
    assert summary["db"]["readiness"]["ready"] is True
    assert summary["exit_code"] == 0


def test_doctor_reports_match_refresh_job_from_target_db(tmp_path: Path) -> None:
    _articles_path, db_path = _seed_articles(tmp_path, [{"id": "article-a", "url": "https://example.com/a", "title": "A"}])
    rebuild = run_doctor(
        DoctorOptions(
            data_dir=tmp_path,
            requested_backend="auto",
            rebuild_sqlite=True,
        )
    )
    assert _operation(rebuild, "rebuild_sqlite")["ok"] is True
    store = ArticleSQLiteStore(db_path, normalize_article_url=article_store.normalize_article_url)
    store.set_meta("match_refresh_running", "0")
    store.set_meta("match_refresh_config_signature", "target-config")
    store.set_meta("match_refresh_total", "10")
    store.set_meta("match_refresh_needs_refresh_count", "4")
    store.set_meta("match_refresh_processed_count", "4")
    store.set_meta("match_refresh_updated_count", "3")
    store.set_meta("match_refresh_analyzed_count", "4")
    store.set_meta("match_refresh_batch_size", "50")
    store.set_meta("match_refresh_started_at", "2026-05-10T10:00:00")
    store.set_meta("match_refresh_updated_at", "2026-05-10T10:00:01")
    store.set_meta("match_refresh_finished_at", "2026-05-10T10:00:01")
    store.set_meta("match_refresh_last_error", "")
    store.set_meta("match_refresh_reason", "target-test")

    summary = run_doctor(DoctorOptions(data_dir=tmp_path, requested_backend="auto"))

    job = summary["match_refresh_job"]
    assert job["status"] == "finished"
    assert job["reason"] == "target-test"
    assert job["config_signature"] == "target-config"
    assert job["processed_count"] == 4
    assert job["batch_size"] == 50


def test_rebuild_sqlite_uses_temp_db_and_does_not_modify_articles_json(tmp_path: Path) -> None:
    articles = [
        {"id": "article-a", "url": "https://example.com/a", "title": "A"},
        {"id": "article-b", "url": "https://example.com/b", "title": "B"},
    ]
    articles_path, db_path = _seed_articles(tmp_path, articles)
    db_path.write_bytes(b"old invalid sqlite")
    before_text = articles_path.read_text(encoding="utf-8")

    summary = run_doctor(
        DoctorOptions(
            data_dir=tmp_path,
            requested_backend="auto",
            rebuild_sqlite=True,
        )
    )
    operation = _operation(summary, "rebuild_sqlite")

    assert operation["ok"] is True
    assert operation["details"]["source_count"] == 2
    assert operation["details"]["backup_paths"]
    assert ArticleSQLiteStore.validate_readiness(db_path)["ready"] is True
    assert articles_path.read_text(encoding="utf-8") == before_text
    assert not list((tmp_path / "logs").glob("*.rebuild-*.sqlite3"))


def test_verify_json_sqlite_detects_stale_json_mismatch(tmp_path: Path) -> None:
    articles_path, _db_path = _seed_articles(
        tmp_path,
        [{"id": "article-a", "url": "https://example.com/a", "title": "A"}],
    )
    rebuild = run_doctor(
        DoctorOptions(
            data_dir=tmp_path,
            requested_backend="auto",
            rebuild_sqlite=True,
        )
    )
    assert _operation(rebuild, "rebuild_sqlite")["ok"] is True
    articles_path.write_text(
        json.dumps(
            [
                {"id": "article-a", "url": "https://example.com/a", "title": "A changed"},
                {"id": "article-b", "url": "https://example.com/b", "title": "B"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summary = run_doctor(
        DoctorOptions(
            data_dir=tmp_path,
            requested_backend="auto",
            verify_json_sqlite=True,
        )
    )
    verify = _operation(summary, "verify_json_sqlite")

    assert summary["status"] == "verification_failed"
    assert summary["exit_code"] == 2
    assert verify["ok"] is False
    assert verify["details"]["signature_fresh"] is False
    assert verify["details"]["mismatch_count"] >= 1


def test_export_sqlite_json_refuses_to_overwrite_articles_without_force(tmp_path: Path) -> None:
    articles_path, _db_path = _seed_articles(tmp_path, [{"id": "article-a", "url": "https://example.com/a", "title": "A"}])
    rebuild = run_doctor(
        DoctorOptions(
            data_dir=tmp_path,
            requested_backend="auto",
            rebuild_sqlite=True,
        )
    )
    assert _operation(rebuild, "rebuild_sqlite")["ok"] is True

    summary = run_doctor(
        DoctorOptions(
            data_dir=tmp_path,
            requested_backend="auto",
            export_sqlite_json=articles_path,
        )
    )
    export = _operation(summary, "export_sqlite_json")

    assert export["ok"] is False
    assert export["error"] == "refusing_to_overwrite_articles_json_without_force"


def _seed_articles(tmp_path: Path, articles: list[dict]) -> tuple[Path, Path]:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    articles_path = logs_dir / "articles.json"
    db_path = logs_dir / "article_store.sqlite3"
    articles_path.write_text(json.dumps(articles, ensure_ascii=False), encoding="utf-8")
    return articles_path, db_path


def _operation(summary: dict, name: str) -> dict:
    for operation in summary.get("operations", []):
        if operation.get("name") == name:
            return operation
    raise AssertionError(f"missing operation {name}: {summary.get('operations')}")
