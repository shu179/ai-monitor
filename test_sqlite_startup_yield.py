"""Verify startup yield and cloud article enqueue batching."""

from __future__ import annotations

import inspect
import unittest
from unittest.mock import patch


class StartupYieldEnvVarTests(unittest.TestCase):
    def test_refresh_worker_startup_delay_env_var(self) -> None:
        import core.article_store as article_store

        source = inspect.getsource(article_store._run_article_match_refresh_worker)
        self.assertIn("ARTICLE_MATCH_REFRESH_STARTUP_DELAY_SECONDS", source)

    def test_cloud_snapshot_startup_delay_env_var(self) -> None:
        from web_backend import AppRuntime

        source = inspect.getsource(AppRuntime._run_cloud_articles_snapshot_worker)
        self.assertIn("CLOUD_ARTICLES_SNAPSHOT_STARTUP_DELAY_SECONDS", source)


class EnqueueBatchingTests(unittest.TestCase):
    def test_enqueue_cloud_articles_respects_batch_size(self) -> None:
        from core.cloud_run_sync import enqueue_cloud_articles

        class FakeOutbox:
            def __init__(self) -> None:
                self.batches: list[int] = []

            def enqueue_many(self, events):
                self.batches.append(len(events))
                return {"created": len(events), "requested": len(events), "dropped": {"total": 0}}

        outbox = FakeOutbox()
        articles = [
            {
                "id": f"a{i}",
                "url": f"https://example.com/{i}",
                "title": f"标题 {i}",
                "media_name": "示例媒体",
                "media_type": "selfmedia",
                "published_at": "2026-05-26",
            }
            for i in range(500)
        ]

        with patch("core.cloud_run_sync.time.sleep"):
            enqueue_cloud_articles(articles, {"tasks": []}, outbox=outbox, batch_size=200)

        self.assertGreater(len(outbox.batches), 1)
        self.assertTrue(all(n <= 200 for n in outbox.batches))


if __name__ == "__main__":
    unittest.main()
