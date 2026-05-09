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

    def _runtime(self) -> AppRuntime:
        runtime = object.__new__(AppRuntime)
        runtime._article_sqlite_shadow_lock = threading.RLock()
        runtime._article_sqlite_shadow_cache_key = None
        runtime._is_ordinary_cloud_session = lambda session: False  # type: ignore[method-assign]
        runtime._article_store_version_key = lambda: ("articles.json", 1, 1)  # type: ignore[method-assign]
        runtime._article_match_config_key = lambda config: "config-key"  # type: ignore[method-assign]
        return runtime


if __name__ == "__main__":
    unittest.main()
