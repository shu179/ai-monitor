from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from web_backend import AppRuntime


class _FakeCloudSessionStore:
    def load(self):
        return {}


class ArticleSQLiteReadAdapterTests(unittest.TestCase):
    def test_sqlite_shadow_article_page_is_disabled_by_default(self) -> None:
        runtime = self._runtime()

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AIBRANDMONITOR_ARTICLE_READ_BACKEND", None)
            result = runtime._get_sqlite_shadow_article_page({}, limit=10)

        self.assertIsNone(result)

    def test_sqlite_shadow_article_page_rebuilds_index_and_reads_page(self) -> None:
        today = "2026-05-09"
        articles = [
            {
                "id": "article-a",
                "url": "https://www.example.com/a?utm_source=x",
                "title": "品牌A 今日报道",
                "media_name": "示例媒体",
                "media_type": "authority",
                "published_at": today,
                "ts": today,
                "matched_tasks": ["品牌A"],
            },
            {
                "id": "article-b",
                "url": "https://example.com/b",
                "title": "品牌B 旧报道",
                "media_name": "示例自媒体",
                "media_type": "selfmedia",
                "published_at": "2024-01-01",
                "ts": "2024-01-01",
                "matched_tasks": ["品牌B"],
            },
        ]
        runtime = self._runtime()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "shadow.sqlite3"
            with (
                patch.dict(os.environ, {"AIBRANDMONITOR_ARTICLE_READ_BACKEND": "sqlite_shadow"}),
                patch("web_backend.CloudSessionStore", _FakeCloudSessionStore),
                patch("web_backend.default_shadow_db_path", lambda: db_path),
                patch("web_backend.refresh_article_matches", lambda config: list(articles)),
                patch("web_backend.local_today", lambda: type("FakeDate", (), {"isoformat": lambda self: today})()),
            ):
                result = runtime._get_sqlite_shadow_article_page(
                    {"tasks": [{"name": "品牌A"}]},
                    media_type="media",
                    limit=10,
                    task_name="品牌A",
                )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["today_total"], 1)
        self.assertEqual([item["id"] for item in result["articles"]], ["article-a"])

    def test_sqlite_shadow_compare_mode_marks_page_compare_only(self) -> None:
        today = "2026-05-09"
        runtime = self._runtime()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "shadow.sqlite3"
            with (
                patch.dict(os.environ, {"AIBRANDMONITOR_ARTICLE_READ_BACKEND": "sqlite_shadow_compare"}),
                patch("web_backend.CloudSessionStore", _FakeCloudSessionStore),
                patch("web_backend.default_shadow_db_path", lambda: db_path),
                patch("web_backend.refresh_article_matches", lambda config: [
                    {
                        "id": "article-a",
                        "url": "https://example.com/a",
                        "title": "品牌A 今日报道",
                        "media_type": "authority",
                        "published_at": today,
                        "ts": today,
                        "matched_tasks": ["品牌A"],
                    }
                ]),
                patch("web_backend.local_today", lambda: type("FakeDate", (), {"isoformat": lambda self: today})()),
            ):
                result = runtime._get_sqlite_shadow_article_page(
                    {"tasks": [{"name": "品牌A"}]},
                    media_type="media",
                    limit=10,
                    task_name="品牌A",
                )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result["compare_only"])
        self.assertEqual([item["id"] for item in result["articles"]], ["article-a"])

    def test_compare_recorder_tracks_recent_mismatches_for_diagnostics(self) -> None:
        runtime = self._runtime()

        runtime._record_sqlite_shadow_article_compare(
            query={"media_type": "media", "limit": 20, "task_name": "品牌A"},
            sqlite_page={
                "articles": [{"id": "sqlite-article"}],
                "total": 2,
                "today_total": 1,
            },
            json_result={
                "articles": [{"id": "json-article"}],
                "total": 1,
                "today_total": 1,
            },
        )
        status = runtime.get_article_sqlite_shadow_compare_status()

        self.assertTrue(status["ok"])
        self.assertEqual(status["checked"], 1)
        self.assertEqual(status["mismatches"], 1)
        self.assertEqual(status["last"]["mismatches"], ["total", "article_ids"])
        self.assertEqual(status["last"]["query"]["task_name"], "品牌A")

    def test_sqlite_shadow_read_error_opens_cooldown_and_skips_next_attempt(self) -> None:
        runtime = self._runtime()
        calls = []

        def fail_index(config, *, db_path):
            calls.append(str(db_path))
            raise RuntimeError("boom")

        runtime._ensure_sqlite_shadow_article_index = fail_index  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "shadow.sqlite3"
            with (
                patch.dict(os.environ, {
                    "AIBRANDMONITOR_ARTICLE_READ_BACKEND": "sqlite_shadow",
                    "AIBRANDMONITOR_ARTICLE_SQLITE_ERROR_LIMIT": "1",
                    "AIBRANDMONITOR_ARTICLE_SQLITE_COOLDOWN_SECONDS": "120",
                }),
                patch("web_backend.CloudSessionStore", _FakeCloudSessionStore),
                patch("web_backend.default_shadow_db_path", lambda: db_path),
            ):
                self.assertIsNone(runtime._get_sqlite_shadow_article_page({}, limit=10))
                self.assertIsNone(runtime._get_sqlite_shadow_article_page({}, limit=10))

        status = runtime.get_article_sqlite_shadow_compare_status()
        self.assertEqual(calls, [str(db_path)])
        self.assertTrue(status["health"]["blocked"])
        self.assertEqual(status["health"]["last_fallback_reason"], "read_error")
        self.assertIn("RuntimeError: boom", status["health"]["last_error"])

    def test_compare_mismatch_opens_health_cooldown(self) -> None:
        runtime = self._runtime()

        with patch.dict(os.environ, {
            "AIBRANDMONITOR_ARTICLE_READ_BACKEND": "sqlite_shadow_compare",
            "AIBRANDMONITOR_ARTICLE_SQLITE_COOLDOWN_SECONDS": "120",
        }):
            runtime._record_sqlite_shadow_article_compare(
                query={"media_type": "", "limit": 20, "task_name": ""},
                sqlite_page={"articles": [{"id": "sqlite-article"}], "total": 1, "today_total": 0},
                json_result={"articles": [{"id": "json-article"}], "total": 1, "today_total": 0},
            )

        status = runtime.get_article_sqlite_shadow_compare_status()
        self.assertEqual(status["mismatches"], 1)
        self.assertTrue(status["health"]["blocked"])
        self.assertEqual(status["health"]["last_fallback_reason"], "compare_mismatch")
        self.assertEqual(status["health"]["last_error"], "article_ids")

    def test_sqlite_shadow_fd_growth_falls_back_and_opens_cooldown(self) -> None:
        today = "2026-05-09"
        runtime = self._runtime()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "shadow.sqlite3"
            with (
                patch.dict(os.environ, {
                    "AIBRANDMONITOR_ARTICLE_READ_BACKEND": "sqlite_shadow",
                    "AIBRANDMONITOR_ARTICLE_SQLITE_FD_GROWTH_LIMIT": "1",
                    "AIBRANDMONITOR_ARTICLE_SQLITE_COOLDOWN_SECONDS": "120",
                }),
                patch("web_backend.CloudSessionStore", _FakeCloudSessionStore),
                patch("web_backend.default_shadow_db_path", lambda: db_path),
                patch("web_backend._open_fd_count", side_effect=[4, 20]),
                patch("web_backend.refresh_article_matches", lambda config: [
                    {
                        "id": "article-a",
                        "url": "https://example.com/a",
                        "title": "品牌A 今日报道",
                        "media_type": "authority",
                        "published_at": today,
                        "ts": today,
                        "matched_tasks": ["品牌A"],
                    }
                ]),
                patch("web_backend.local_today", lambda: type("FakeDate", (), {"isoformat": lambda self: today})()),
            ):
                result = runtime._get_sqlite_shadow_article_page(
                    {"tasks": [{"name": "品牌A"}]},
                    media_type="media",
                    limit=10,
                    task_name="品牌A",
                )

        status = runtime.get_article_sqlite_shadow_compare_status()
        self.assertIsNone(result)
        self.assertTrue(status["health"]["blocked"])
        self.assertEqual(status["health"]["last_fallback_reason"], "fd_growth")
        self.assertEqual(status["health"]["last_fd_delta"], 16)

    def _runtime(self) -> AppRuntime:
        runtime = object.__new__(AppRuntime)
        runtime._article_sqlite_shadow_lock = threading.RLock()
        runtime._article_sqlite_shadow_cache_key = None
        runtime._article_sqlite_shadow_compare_lock = threading.RLock()
        runtime._article_sqlite_shadow_compare_recent = []
        runtime._article_sqlite_shadow_health_lock = threading.RLock()
        runtime._article_sqlite_shadow_health = {}
        runtime._is_ordinary_cloud_session = lambda session: False  # type: ignore[method-assign]
        runtime._article_store_version_key = lambda: ("articles.json", 1, 1)  # type: ignore[method-assign]
        runtime._article_match_config_key = lambda config: "config-key"  # type: ignore[method-assign]
        return runtime


if __name__ == "__main__":
    unittest.main()
