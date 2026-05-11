from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core.article_history_sqlite_store import ArticleHistorySQLiteStore
from core.article_store import normalize_article_url
from web_backend import AppRuntime


class _FakeCloudSessionStore:
    def load(self):
        return {}


class ArticleSQLiteReadAdapterTests(unittest.TestCase):
    def test_sqlite_shadow_article_page_defaults_to_auto_and_reads_fresh_index(self) -> None:
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
            }
        ]
        runtime = self._runtime()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "shadow.sqlite3"
            self._write_fresh_article_shadow(db_path, articles)
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AIBRANDMONITOR_ARTICLE_READ_BACKEND", None)
                with (
                    patch("web_backend.CloudSessionStore", _FakeCloudSessionStore),
                    patch("web_backend.default_shadow_db_path", lambda: db_path),
                    patch("web_backend.get_article_source_signature", lambda: "source-key"),
                    patch("web_backend.refresh_article_matches", side_effect=AssertionError("auto page read must not rebuild inline")),
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
        self.assertEqual(result["requested_backend"], "auto")
        self.assertEqual(result["backend_source"], "default")
        self.assertFalse(result["compare_only"])
        self.assertEqual(result["total"], 1)
        self.assertEqual([item["id"] for item in result["articles"]], ["article-a"])

    def test_sqlite_shadow_article_page_disabled_env_values_skip_sqlite(self) -> None:
        for value in ("json", "off", "disabled"):
            runtime = self._runtime()
            with self.subTest(value=value):
                with (
                    patch.dict(os.environ, {"AIBRANDMONITOR_ARTICLE_READ_BACKEND": value}),
                    patch("web_backend.default_shadow_db_path", side_effect=AssertionError("disabled backend must not inspect sqlite")),
                ):
                    result = runtime._get_sqlite_shadow_article_page({}, limit=10)

                self.assertIsNone(result)

    def test_sqlite_shadow_status_reports_auto_freshness_and_rebuild_state(self) -> None:
        today = "2026-05-09"
        runtime = self._runtime()
        runtime.load_config = lambda: {"tasks": [{"name": "品牌A"}]}  # type: ignore[method-assign]
        runtime._article_sqlite_shadow_rebuild_state = {
            "running": False,
            "last_reason": "article_shadow_source_stale",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "shadow.sqlite3"
            self._write_fresh_article_shadow(
                db_path,
                [
                    {
                        "id": "article-a",
                        "url": "https://example.com/a",
                        "title": "品牌A 今日报道",
                        "media_type": "authority",
                        "published_at": today,
                        "ts": today,
                        "matched_tasks": ["品牌A"],
                    }
                ],
            )
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AIBRANDMONITOR_ARTICLE_READ_BACKEND", None)
                with (
                    patch("web_backend.CloudSessionStore", _FakeCloudSessionStore),
                    patch("web_backend.default_shadow_db_path", lambda: db_path),
                    patch("web_backend.get_article_source_signature", lambda: "source-key"),
                ):
                    status = runtime.get_article_sqlite_shadow_compare_status()

        self.assertEqual(status["requested_backend"], "auto")
        self.assertEqual(status["backend_source"], "default")
        self.assertEqual(status["effectiveBackend"], "sqlite_shadow")
        self.assertEqual(status["fallbackReason"], "")
        self.assertTrue(status["readiness"]["ready"])
        self.assertTrue(status["freshness"]["fresh"])
        self.assertEqual(status["freshness"]["stored_article_source_signature"], "source-key")
        self.assertEqual(status["freshness"]["stored_article_match_config_signature"], "config-key")
        self.assertEqual(status["rebuild"]["last_reason"], "article_shadow_source_stale")

    def test_sqlite_shadow_article_page_reads_fresh_index(self) -> None:
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
            self._write_fresh_article_shadow(db_path, articles)
            with (
                patch.dict(os.environ, {"AIBRANDMONITOR_ARTICLE_READ_BACKEND": "sqlite_shadow"}),
                patch("web_backend.CloudSessionStore", _FakeCloudSessionStore),
                patch("web_backend.default_shadow_db_path", lambda: db_path),
                patch("web_backend.get_article_source_signature", lambda: "source-key"),
                patch("web_backend.refresh_article_matches", side_effect=AssertionError("page read must not rebuild inline")),
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

    def test_sqlite_shadow_article_page_uses_paged_store_query(self) -> None:
        today = "2026-05-09"
        runtime = self._runtime()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "shadow.sqlite3"
            ArticleHistorySQLiteStore(db_path).import_articles(
                [
                    {
                        "id": "article-a",
                        "url": "https://example.com/a",
                        "title": "品牌A 今日报道",
                        "media_type": "authority",
                        "published_at": today,
                        "ts": today,
                        "matched_tasks": ["品牌A"],
                    },
                    {
                        "id": "article-b",
                        "url": "https://example.com/b",
                        "title": "品牌A 旧报道",
                        "media_type": "authority",
                        "published_at": "2024-01-01",
                        "ts": "2024-01-01",
                        "matched_tasks": ["品牌A"],
                    },
                ],
                replace=True,
            )
            runtime._ensure_sqlite_shadow_article_index = lambda config, *, db_path: True  # type: ignore[method-assign]
            with (
                patch.dict(os.environ, {"AIBRANDMONITOR_ARTICLE_READ_BACKEND": "sqlite_shadow"}),
                patch("web_backend.CloudSessionStore", _FakeCloudSessionStore),
                patch("web_backend.default_shadow_db_path", lambda: db_path),
                patch("web_backend.local_today", lambda: type("FakeDate", (), {"isoformat": lambda self: today})()),
                patch.object(
                    ArticleHistorySQLiteStore,
                    "get_article_items",
                    side_effect=AssertionError("regular page path must not read all articles"),
                ),
            ):
                result = runtime._get_sqlite_shadow_article_page(
                    {"tasks": [{"name": "品牌A"}]},
                    media_type="media",
                    limit=1,
                    task_name="品牌A",
                )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["today_total"], 1)
        self.assertEqual([item["id"] for item in result["articles"]], ["article-a"])

    def test_sqlite_shadow_compare_mode_marks_page_compare_only(self) -> None:
        today = "2026-05-09"
        runtime = self._runtime()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "shadow.sqlite3"
            articles = [
                {
                    "id": "article-a",
                    "url": "https://example.com/a",
                    "title": "品牌A 今日报道",
                    "media_type": "authority",
                    "published_at": today,
                    "ts": today,
                    "matched_tasks": ["品牌A"],
                }
            ]
            self._write_fresh_article_shadow(db_path, articles)
            with (
                patch.dict(os.environ, {"AIBRANDMONITOR_ARTICLE_READ_BACKEND": "sqlite_shadow_compare"}),
                patch("web_backend.CloudSessionStore", _FakeCloudSessionStore),
                patch("web_backend.default_shadow_db_path", lambda: db_path),
                patch("web_backend.get_article_source_signature", lambda: "source-key"),
                patch("web_backend.refresh_article_matches", side_effect=AssertionError("page read must not rebuild inline")),
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
        status = runtime.get_article_sqlite_shadow_compare_status()
        self.assertEqual(status["health"].get("last_effective_backend"), "json")
        self.assertEqual(status["health"].get("last_probe_backend"), "sqlite_shadow")

    def test_sqlite_shadow_article_index_uses_json_page_deduping(self) -> None:
        today = "2026-05-09"
        articles = [
            {
                "id": "article-a",
                "url": "https://example.com/a",
                "title": "同一篇报道",
                "media_name": "示例媒体",
                "media_type": "authority",
                "published_at": today,
                "ts": today,
                "matched_tasks": ["品牌A"],
            },
            {
                "id": "article-b",
                "url": "",
                "title": "同一篇报道",
                "media_name": "示例媒体",
                "media_type": "authority",
                "published_at": today,
                "ts": today,
                "matched_tasks": ["品牌B"],
            },
        ]
        runtime = self._runtime()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "shadow.sqlite3"
            self._write_fresh_article_shadow(db_path, articles)
            with (
                patch.dict(os.environ, {"AIBRANDMONITOR_ARTICLE_READ_BACKEND": "sqlite_shadow"}),
                patch("web_backend.CloudSessionStore", _FakeCloudSessionStore),
                patch("web_backend.default_shadow_db_path", lambda: db_path),
                patch("web_backend.get_article_source_signature", lambda: "source-key"),
                patch("web_backend.refresh_article_matches", side_effect=AssertionError("page read must not rebuild inline")),
                patch("web_backend.local_today", lambda: type("FakeDate", (), {"isoformat": lambda self: today})()),
            ):
                all_result = runtime._get_sqlite_shadow_article_page(
                    {"tasks": [{"name": "品牌A"}, {"name": "品牌B"}]},
                    media_type="media",
                    limit=10,
                    task_name="",
                )
                result = runtime._get_sqlite_shadow_article_page(
                    {"tasks": [{"name": "品牌B"}]},
                    media_type="media",
                    limit=10,
                    task_name="品牌B",
                )

        self.assertIsNotNone(all_result)
        assert all_result is not None
        self.assertEqual(all_result["total"], 1)
        self.assertEqual(all_result["today_total"], 1)
        self.assertEqual([item["id"] for item in all_result["articles"]], ["article-b"])
        self.assertEqual(all_result["articles"][0]["matched_tasks"], ["品牌B", "品牌A"])
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["today_total"], 1)
        self.assertEqual([item["id"] for item in result["articles"]], ["article-b"])
        status = runtime.get_article_sqlite_shadow_compare_status()
        self.assertFalse(status["health"].get("last_fallback_reason", ""))
        self.assertEqual(status["health"].get("fallback_count", 0), 0)

    def test_sqlite_shadow_article_page_falls_back_when_limit_exceeds_guard(self) -> None:
        runtime = self._runtime()

        with patch.dict(os.environ, {"AIBRANDMONITOR_ARTICLE_READ_BACKEND": "sqlite_shadow"}):
            result = runtime._get_sqlite_shadow_article_page({}, limit=5001)

        self.assertIsNone(result)
        status = runtime.get_article_sqlite_shadow_compare_status()
        self.assertEqual(status["health"]["last_fallback_reason"], "limit_exceeded")

    def test_sqlite_shadow_article_page_falls_back_when_shadow_stale_without_inline_refresh(self) -> None:
        today = "2026-05-09"
        runtime = self._runtime()
        scheduled: list[str] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "shadow.sqlite3"
            self._write_fresh_article_shadow(
                db_path,
                [
                    {
                        "id": "article-stale",
                        "url": "https://example.com/stale",
                        "title": "旧索引文章",
                        "media_type": "authority",
                        "published_at": today,
                        "ts": today,
                        "matched_tasks": ["品牌A"],
                    }
                ],
                source_signature="old-source",
            )
            runtime._schedule_sqlite_shadow_article_rebuild = (  # type: ignore[method-assign]
                lambda config, *, db_path, reason: scheduled.append(str(reason)) or True
            )
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AIBRANDMONITOR_ARTICLE_READ_BACKEND", None)
                with (
                    patch("web_backend.CloudSessionStore", _FakeCloudSessionStore),
                    patch("web_backend.default_shadow_db_path", lambda: db_path),
                    patch("web_backend.get_article_source_signature", lambda: "new-source"),
                    patch("web_backend.refresh_article_matches", side_effect=AssertionError("stale read must not rebuild inline")),
                ):
                    result = runtime._get_sqlite_shadow_article_page(
                        {"tasks": [{"name": "品牌A"}]},
                        media_type="media",
                        limit=10,
                        task_name="品牌A",
                    )

        self.assertIsNone(result)
        self.assertEqual(scheduled, ["article_shadow_source_stale"])
        status = runtime.get_article_sqlite_shadow_compare_status()
        self.assertEqual(status["health"]["last_fallback_reason"], "article_shadow_source_stale")

    def test_sqlite_shadow_article_page_falls_back_when_match_config_stale_without_inline_refresh(self) -> None:
        today = "2026-05-09"
        runtime = self._runtime()
        scheduled: list[str] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "shadow.sqlite3"
            self._write_fresh_article_shadow(
                db_path,
                [
                    {
                        "id": "article-stale",
                        "url": "https://example.com/stale",
                        "title": "旧配置索引文章",
                        "media_type": "authority",
                        "published_at": today,
                        "ts": today,
                        "matched_tasks": ["品牌A"],
                    }
                ],
                match_signature="old-config",
            )
            runtime._schedule_sqlite_shadow_article_rebuild = (  # type: ignore[method-assign]
                lambda config, *, db_path, reason: scheduled.append(str(reason)) or True
            )
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AIBRANDMONITOR_ARTICLE_READ_BACKEND", None)
                with (
                    patch("web_backend.CloudSessionStore", _FakeCloudSessionStore),
                    patch("web_backend.default_shadow_db_path", lambda: db_path),
                    patch("web_backend.get_article_source_signature", lambda: "source-key"),
                    patch("web_backend.refresh_article_matches", side_effect=AssertionError("stale read must not rebuild inline")),
                ):
                    result = runtime._get_sqlite_shadow_article_page(
                        {"tasks": [{"name": "品牌A"}]},
                        media_type="media",
                        limit=10,
                        task_name="品牌A",
                    )

        self.assertIsNone(result)
        self.assertEqual(scheduled, ["article_shadow_match_config_stale"])
        status = runtime.get_article_sqlite_shadow_compare_status()
        self.assertEqual(status["health"]["last_fallback_reason"], "article_shadow_match_config_stale")

    def test_sqlite_shadow_article_page_skips_ordinary_cloud_session(self) -> None:
        runtime = self._runtime()
        runtime._is_ordinary_cloud_session = lambda session: True  # type: ignore[method-assign]
        runtime._ensure_sqlite_shadow_article_index = (  # type: ignore[method-assign]
            lambda config, *, db_path: (_ for _ in ()).throw(AssertionError("ordinary cloud must skip sqlite"))
        )

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AIBRANDMONITOR_ARTICLE_READ_BACKEND", None)
            with (
                patch("web_backend.CloudSessionStore", _FakeCloudSessionStore),
                patch("web_backend.default_shadow_db_path", lambda: Path("/tmp/unused-shadow.sqlite3")),
            ):
                result = runtime._get_sqlite_shadow_article_page({}, limit=10)

        self.assertIsNone(result)
        status = runtime.get_article_sqlite_shadow_compare_status()
        self.assertEqual(status["health"]["last_fallback_reason"], "ordinary_cloud_session")

    def test_sqlite_shadow_article_page_merges_duplicate_url_tasks_like_json_page(self) -> None:
        today = "2026-05-09"
        articles = [
            {
                "id": "article-first",
                "url": "https://www.example.com/a?utm_source=x",
                "title": "同一篇报道",
                "media_name": "示例媒体",
                "media_type": "authority",
                "published_at": today,
                "ts": today,
                "matched_tasks": ["品牌A"],
                "referenced_tasks": ["品牌R1"],
            },
            {
                "id": "article-second",
                "url": "https://example.com/a",
                "title": "重复报道",
                "media_name": "示例媒体",
                "media_type": "authority",
                "published_at": today,
                "ts": today,
                "matched_tasks": ["品牌B"],
                "referenced_tasks": ["品牌R2"],
            },
        ]
        runtime = self._runtime()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "shadow.sqlite3"
            self._write_fresh_article_shadow(db_path, articles)
            with (
                patch.dict(os.environ, {"AIBRANDMONITOR_ARTICLE_READ_BACKEND": "sqlite_shadow"}),
                patch("web_backend.CloudSessionStore", _FakeCloudSessionStore),
                patch("web_backend.default_shadow_db_path", lambda: db_path),
                patch("web_backend.get_article_source_signature", lambda: "source-key"),
                patch("web_backend.refresh_article_matches", side_effect=AssertionError("page read must not rebuild inline")),
                patch("web_backend.local_today", lambda: type("FakeDate", (), {"isoformat": lambda self: today})()),
            ):
                result = runtime._get_sqlite_shadow_article_page(
                    {"tasks": [{"name": "品牌A"}, {"name": "品牌B"}]},
                    media_type="media",
                    limit=10,
                    task_name="品牌B",
                )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["total"], 1)
        self.assertEqual([item["id"] for item in result["articles"]], ["article-first"])
        self.assertEqual(result["articles"][0]["matched_tasks"], ["品牌A", "品牌B"])
        status = runtime.get_article_sqlite_shadow_compare_status()
        self.assertFalse(status["health"].get("last_fallback_reason", ""))

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
            self._write_fresh_article_shadow(db_path, [
                {
                    "id": "article-a",
                    "url": "https://example.com/a",
                    "title": "品牌A 今日报道",
                    "media_type": "authority",
                    "published_at": today,
                    "ts": today,
                    "matched_tasks": ["品牌A"],
                }
            ])
            with (
                patch.dict(os.environ, {
                    "AIBRANDMONITOR_ARTICLE_READ_BACKEND": "sqlite_shadow",
                    "AIBRANDMONITOR_ARTICLE_SQLITE_FD_GROWTH_LIMIT": "1",
                    "AIBRANDMONITOR_ARTICLE_SQLITE_COOLDOWN_SECONDS": "120",
                }),
                patch("web_backend.CloudSessionStore", _FakeCloudSessionStore),
                patch("web_backend.default_shadow_db_path", lambda: db_path),
                patch("web_backend._open_fd_count", return_value=4),
                patch("web_backend._open_sqlite_fd_count", side_effect=[4, 20]),
                patch("web_backend.get_article_source_signature", lambda: "source-key"),
                patch("web_backend.refresh_article_matches", side_effect=AssertionError("page read must not rebuild inline")),
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

    def test_sqlite_shadow_fd_growth_in_index_rebuild_falls_back(self) -> None:
        today = "2026-05-09"
        runtime = self._runtime()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "shadow.sqlite3"
            fd_state = {"count": 4}

            def fake_ensure_index(config, *, db_path):
                ArticleHistorySQLiteStore(db_path).import_articles(
                    [
                        {
                            "id": "article-a",
                            "url": "https://example.com/a",
                            "title": "品牌A 今日报道",
                            "media_type": "authority",
                            "published_at": today,
                            "ts": today,
                            "matched_tasks": ["品牌A"],
                        }
                    ],
                    replace=True,
                )
                fd_state["count"] = 20
                return True

            runtime._ensure_sqlite_shadow_article_index = fake_ensure_index  # type: ignore[method-assign]
            with (
                patch.dict(os.environ, {
                    "AIBRANDMONITOR_ARTICLE_READ_BACKEND": "sqlite_shadow",
                    "AIBRANDMONITOR_ARTICLE_SQLITE_FD_GROWTH_LIMIT": "1",
                    "AIBRANDMONITOR_ARTICLE_SQLITE_COOLDOWN_SECONDS": "120",
                }),
                patch("web_backend.CloudSessionStore", _FakeCloudSessionStore),
                patch("web_backend.default_shadow_db_path", lambda: db_path),
                patch("web_backend._open_sqlite_fd_count", lambda path: fd_state["count"]),
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
        runtime._article_sqlite_shadow_unready_reason = ""
        runtime._article_sqlite_shadow_rebuild_lock = threading.RLock()
        runtime._article_sqlite_shadow_rebuild_state = {}
        runtime._article_sqlite_shadow_compare_lock = threading.RLock()
        runtime._article_sqlite_shadow_compare_recent = []
        runtime._article_sqlite_shadow_health_lock = threading.RLock()
        runtime._article_sqlite_shadow_health = {}
        runtime._is_ordinary_cloud_session = lambda session: False  # type: ignore[method-assign]
        runtime._article_store_version_key = lambda: ("articles.json", 1, 1)  # type: ignore[method-assign]
        runtime._article_match_config_key = lambda config: "config-key"  # type: ignore[method-assign]
        return runtime

    def _write_fresh_article_shadow(
        self,
        db_path: Path,
        articles: list[dict],
        *,
        source_signature: str = "source-key",
        match_signature: str = "config-key",
    ) -> None:
        store = ArticleHistorySQLiteStore(db_path, normalize_article_url=normalize_article_url)
        store.import_articles(articles, replace=True)
        store.set_meta("article_source_signature", source_signature)
        store.set_meta("article_match_config_signature", match_signature)


if __name__ == "__main__":
    unittest.main()
