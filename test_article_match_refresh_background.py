from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import core.article_store as article_store
from core.article_sqlite_store import ArticleSQLiteStore


def _minimal_config(*, task_names: list[str] | None = None) -> dict:
    names = task_names or ["品牌A", "品牌B"]
    return {
        "tasks": [
            {
                "name": name,
                "brand": name,
                "keywords": [{"keyword": name, "brand": name}],
            }
            for name in names
        ],
    }


class ArticleMatchRefreshBackgroundTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._tmpdir.name)
        self._original_backend = os.environ.get(article_store.ARTICLE_STORE_BACKEND_ENV)
        self._original_batch_size = os.environ.get(article_store.ARTICLE_MATCH_REFRESH_BATCH_SIZE_ENV)
        self._original_sleep = os.environ.get(article_store.ARTICLE_MATCH_REFRESH_SLEEP_SECONDS_ENV)
        self._original_paths = {
            "ARTICLES_FILE": article_store.ARTICLES_FILE,
            "DOMAIN_OVERRIDES_FILE": article_store.DOMAIN_OVERRIDES_FILE,
            "DOMAIN_MEDIA_NAMES_FILE": article_store.DOMAIN_MEDIA_NAMES_FILE,
            "EXCLUDED_ARTICLE_URLS_FILE": article_store.EXCLUDED_ARTICLE_URLS_FILE,
            "LOCAL_STORE_DB_FILE": article_store.LOCAL_STORE_DB_FILE,
            "ARTICLE_SHADOW_DB_FILE": article_store.ARTICLE_SHADOW_DB_FILE,
            "ARTICLE_STORE_DB_FILE": article_store.ARTICLE_STORE_DB_FILE,
        }
        article_store.ARTICLES_FILE = root / "logs" / "articles.json"
        article_store.DOMAIN_OVERRIDES_FILE = root / "logs" / "domain_overrides.json"
        article_store.DOMAIN_MEDIA_NAMES_FILE = root / "logs" / "domain_media_names.json"
        article_store.EXCLUDED_ARTICLE_URLS_FILE = root / "logs" / "excluded_article_urls.json"
        article_store.LOCAL_STORE_DB_FILE = root / "logs" / "local_store.sqlite3"
        article_store.ARTICLE_SHADOW_DB_FILE = root / "logs" / "article_history_shadow.sqlite3"
        article_store.ARTICLE_STORE_DB_FILE = root / "logs" / "article_store.sqlite3"
        article_store.ARTICLES_FILE.parent.mkdir(parents=True, exist_ok=True)
        article_store.ARTICLES_FILE.write_text("[]", encoding="utf-8")
        # Use SQLite backend for all tests
        os.environ[article_store.ARTICLE_STORE_BACKEND_ENV] = "sqlite"
        # Use small batch and no sleep for tests
        os.environ[article_store.ARTICLE_MATCH_REFRESH_BATCH_SIZE_ENV] = "50"
        os.environ[article_store.ARTICLE_MATCH_REFRESH_SLEEP_SECONDS_ENV] = "0"

    def tearDown(self) -> None:
        article_store.wait_for_article_store_backend_migration(timeout=2.0)
        self._wait_for_background_refresh(timeout=5.0)
        article_store._reset_article_match_refresh_state_for_tests()  # noqa: SLF001
        if self._original_backend is None:
            os.environ.pop(article_store.ARTICLE_STORE_BACKEND_ENV, None)
        else:
            os.environ[article_store.ARTICLE_STORE_BACKEND_ENV] = self._original_backend
        if self._original_batch_size is None:
            os.environ.pop(article_store.ARTICLE_MATCH_REFRESH_BATCH_SIZE_ENV, None)
        else:
            os.environ[article_store.ARTICLE_MATCH_REFRESH_BATCH_SIZE_ENV] = self._original_batch_size
        if self._original_sleep is None:
            os.environ.pop(article_store.ARTICLE_MATCH_REFRESH_SLEEP_SECONDS_ENV, None)
        else:
            os.environ[article_store.ARTICLE_MATCH_REFRESH_SLEEP_SECONDS_ENV] = self._original_sleep
        article_store.ARTICLES_FILE = self._original_paths["ARTICLES_FILE"]
        article_store.DOMAIN_OVERRIDES_FILE = self._original_paths["DOMAIN_OVERRIDES_FILE"]
        article_store.DOMAIN_MEDIA_NAMES_FILE = self._original_paths["DOMAIN_MEDIA_NAMES_FILE"]
        article_store.EXCLUDED_ARTICLE_URLS_FILE = self._original_paths["EXCLUDED_ARTICLE_URLS_FILE"]
        article_store.LOCAL_STORE_DB_FILE = self._original_paths["LOCAL_STORE_DB_FILE"]
        article_store.ARTICLE_SHADOW_DB_FILE = self._original_paths["ARTICLE_SHADOW_DB_FILE"]
        article_store.ARTICLE_STORE_DB_FILE = self._original_paths["ARTICLE_STORE_DB_FILE"]
        article_store.reset_article_store_backend_health_for_tests()
        self._tmpdir.cleanup()

    def _add_articles(self, count: int) -> None:
        articles = []
        for i in range(count):
            articles.append({
                "id": f"article-{i}",
                "url": f"https://example.com/article/{i}",
                "title": f"品牌{'A' if i % 2 == 0 else 'B'} 文章 {i}",
                "published_at": f"2026-05-{10 - (i % 10):02d}",
                "media_type": "selfmedia",
                "matched_tasks": [],
                "match_reasons": {},
            })
        # Use SQLite import directly to avoid JSON write path
        store = article_store._article_sqlite_store()  # noqa: SLF001
        store.import_from_articles(articles, replace=False)

    def _wait_for_background_refresh(self, timeout: float = 30.0) -> dict:
        waited = 0.0
        interval = 0.05
        while waited < timeout:
            status = article_store.get_article_match_refresh_status()
            if not status.get("running") and not status.get("worker_alive"):
                return status
            time.sleep(interval)
            waited += interval
        return article_store.get_article_match_refresh_status()

    def test_schedule_returns_immediately_without_full_sync(self) -> None:
        self._add_articles(100)
        config = _minimal_config()
        started = time.perf_counter()
        result = article_store.schedule_article_match_refresh(config, reason="test")
        elapsed = time.perf_counter() - started
        self.assertTrue(result.get("scheduled"), f"Expected scheduled=True, got {result}")
        self.assertLess(elapsed, 1.0, "Schedule should return immediately, not block on full sync")

    def test_running_guard_prevents_duplicate_jobs(self) -> None:
        self._add_articles(500)
        config = _minimal_config()
        # Use a brief sleep to ensure the worker doesn't finish before the guard check
        os.environ[article_store.ARTICLE_MATCH_REFRESH_SLEEP_SECONDS_ENV] = "0.01"
        try:
            result1 = article_store.schedule_article_match_refresh(config, reason="test1")
            self.assertTrue(result1.get("scheduled"), f"First schedule should succeed: {result1}")
            result2 = article_store.schedule_article_match_refresh(config, reason="test2")
            self.assertFalse(result2.get("scheduled"), f"Second schedule should be rejected: {result2}")
            self.assertEqual(result2.get("reason"), "already_running")
        finally:
            os.environ[article_store.ARTICLE_MATCH_REFRESH_SLEEP_SECONDS_ENV] = "0"
            self._wait_for_background_refresh(timeout=30.0)

    def test_job_processes_dirty_articles_and_reduces_needs_refresh_to_zero(self) -> None:
        self._add_articles(100)
        config = _minimal_config()
        status_before = article_store.get_article_match_refresh_status()
        self.assertEqual(status_before.get("status"), "idle")
        result = article_store.schedule_article_match_refresh(config, reason="test")
        self.assertTrue(result.get("scheduled"), f"Expected scheduled: {result}")
        # Wait for completion (job may finish very fast with small batch and no sleep)
        final_status = self._wait_for_background_refresh(timeout=30.0)
        self.assertIn(final_status.get("status"), ("finished", "running"),
                      f"Expected finished or running after wait, got {final_status}")
        self.assertEqual(final_status.get("last_error", ""), "")
        # Verify articles were actually matched
        store = article_store._article_sqlite_store()  # noqa: SLF001
        compiled = article_store.compile_article_matcher(config)
        stats = store.get_match_refresh_stats(compiled.config_signature)
        self.assertEqual(stats.get("needs_refresh_count"), 0,
                         f"All articles should be refreshed: {stats}")
        # Check that articles have match data
        articles = store.list_articles()
        matched_count = sum(1 for a in articles if a.get("matched_tasks"))
        self.assertGreater(matched_count, 0, "Some articles should have matched tasks after refresh")
        self.assertEqual(final_status.get("processed_count"), 100)

    def test_warm_noop_schedule_does_not_start_heavy_work(self) -> None:
        self._add_articles(100)
        config = _minimal_config()
        # First sync refresh to make everything fresh
        article_store.refresh_article_matches(config)
        # Now schedule — should find nothing to refresh
        result = article_store.schedule_article_match_refresh(config, reason="test")
        self.assertFalse(result.get("scheduled"), f"Should not schedule for warm path: {result}")
        self.assertEqual(result.get("reason"), "nothing_to_refresh")
        self.assertEqual(result.get("needs_refresh_count"), 0)

    def test_failure_records_last_error_does_not_affect_get_articles(self) -> None:
        self._add_articles(100)
        config = _minimal_config()
        # Inject failure into analyze
        with patch.object(
            article_store,
            "analyze_article_matches",
            side_effect=RuntimeError("injected test error"),
        ):
            result = article_store.schedule_article_match_refresh(config, reason="test")
            self.assertTrue(result.get("scheduled"), f"Expected scheduled: {result}")
            final_status = self._wait_for_background_refresh(timeout=30.0)
        self.assertIn("injected test error", str(final_status.get("last_error", "")))
        self.assertEqual(final_status.get("status"), "error")
        # get_articles should still work
        articles = article_store.get_articles()
        self.assertEqual(len(articles), 100)

    def test_status_reflects_running_finished_progress(self) -> None:
        self._add_articles(200)
        config = _minimal_config()
        status_idle = article_store.get_article_match_refresh_status()
        self.assertEqual(status_idle.get("status"), "idle")
        self.assertFalse(status_idle.get("running"))
        article_store.schedule_article_match_refresh(config, reason="test")
        # Give the worker a moment to start processing
        time.sleep(0.02)
        status_mid = article_store.get_article_match_refresh_status()
        # Middle status should show some progress or already be complete
        self.assertIn(status_mid.get("status"), ("running", "finished", "interrupted"),
                      f"Unexpected mid status: {status_mid}")
        if status_mid.get("status") == "running":
            self.assertTrue(status_mid.get("running"))
            self.assertTrue(status_mid.get("worker_alive"))
            self.assertGreater(int(status_mid.get("total") or 0), 0)
        final_status = self._wait_for_background_refresh(timeout=30.0)
        self.assertEqual(final_status.get("status"), "finished")
        self.assertFalse(final_status.get("running"))
        self.assertTrue(bool(final_status.get("finished_at")))
        self.assertGreater(int(final_status.get("processed_count") or 0), 0)

    def test_sqlite_backend_only_json_behavior_unchanged(self) -> None:
        # Switch to JSON backend
        os.environ[article_store.ARTICLE_STORE_BACKEND_ENV] = "json"
        article_store.reset_article_store_backend_health_for_tests()
        config = _minimal_config()
        result = article_store.schedule_article_match_refresh(config, reason="test")
        self.assertFalse(result.get("scheduled"))
        self.assertEqual(result.get("reason"), "not_sqlite_backend")
        status = article_store.get_article_match_refresh_status()
        self.assertEqual(status.get("backend"), "json")
        # Switch back to SQLite for proper cleanup
        os.environ[article_store.ARTICLE_STORE_BACKEND_ENV] = "sqlite"
        article_store.reset_article_store_backend_health_for_tests()

    def test_force_schedule_even_when_no_dirty(self) -> None:
        self._add_articles(50)
        config = _minimal_config()
        # First sync refresh to clear dirty
        article_store.refresh_article_matches(config)
        # Force schedule
        result = article_store.schedule_article_match_refresh(config, reason="test", force=True)
        self.assertTrue(result.get("scheduled"), f"Force schedule should succeed: {result}")
        final_status = self._wait_for_background_refresh(timeout=30.0)
        self.assertEqual(final_status.get("status"), "finished")

    def test_doctor_summary_includes_match_refresh_job(self) -> None:
        self._add_articles(50)
        config = _minimal_config()
        article_store.schedule_article_match_refresh(config, reason="test")
        self._wait_for_background_refresh(timeout=30.0)
        health = article_store.get_article_store_backend_health()
        # Check that match_refresh_job is in the health output
        self.assertIn("match_refresh_job", health)
        job = health.get("match_refresh_job")
        self.assertIsInstance(job, dict)
        self.assertIn("status", job)
        self.assertEqual(job.get("status"), "finished")

    def test_interrupted_state_detected(self) -> None:
        self._add_articles(100)
        config = _minimal_config()
        # Manually set running=1 without starting a worker thread
        store = article_store._article_sqlite_store()  # noqa: SLF001
        article_store._write_match_refresh_meta(  # noqa: SLF001
            store,
            running="1",
            config_signature="test-sig",
            total="100",
            needs_refresh_count="100",
            processed_count="0",
            updated_count="0",
            analyzed_count="0",
            batch_size="50",
            started_at="2026-05-10T10:00:00",
            updated_at="",
            finished_at="",
            last_error="",
            reason="test",
        )
        status = article_store.get_article_match_refresh_status()
        self.assertEqual(status.get("status"), "interrupted")
        # Clean up
        article_store._write_match_refresh_meta(  # noqa: SLF001
            store,
            running="0",
            finished_at="2026-05-10T10:01:00",
            updated_at="2026-05-10T10:01:00",
        )

    def test_benchmark_background_refresh_measurement(self) -> None:
        from scripts.article_scale_benchmark import _schedule_background_match_refresh
        self._add_articles(100)
        config = _minimal_config(task_names=["品牌A", "品牌B", "品牌C"])
        # First run a background refresh and wait
        result = _schedule_background_match_refresh(config, wait=True)
        self.assertIn("scheduled", result)
        self.assertIn("processed_count", result)
        self.assertGreater(int(result.get("processed_count") or 0), 0)
        self.assertEqual(result.get("status"), "finished")

    def test_reset_waits_for_running_background_refresh(self) -> None:
        self._add_articles(100)
        config = _minimal_config()
        os.environ[article_store.ARTICLE_MATCH_REFRESH_SLEEP_SECONDS_ENV] = "0.02"
        result = article_store.schedule_article_match_refresh(config, reason="test-reset")
        self.assertTrue(result.get("scheduled"), f"Expected scheduled: {result}")

        article_store._reset_article_match_refresh_state_for_tests(timeout=10.0)  # noqa: SLF001

        status = article_store.get_article_match_refresh_status()
        self.assertFalse(status.get("worker_alive"), status)
        self.assertFalse(status.get("running"), status)
        self.assertEqual(status.get("status"), "finished")


if __name__ == "__main__":
    unittest.main()
