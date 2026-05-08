from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

import core.article_store as article_store
from core.file_lock import CrossProcessRLock


class ArticleStoreLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        self._original_paths = {
            "ARTICLES_FILE": article_store.ARTICLES_FILE,
            "DOMAIN_OVERRIDES_FILE": article_store.DOMAIN_OVERRIDES_FILE,
            "DOMAIN_MEDIA_NAMES_FILE": article_store.DOMAIN_MEDIA_NAMES_FILE,
            "EXCLUDED_ARTICLE_URLS_FILE": article_store.EXCLUDED_ARTICLE_URLS_FILE,
            "MAX_REFERENCE_EVENTS_PER_TASK": article_store.MAX_REFERENCE_EVENTS_PER_TASK,
            "MAX_EXCLUDED_ARTICLE_URLS": article_store.MAX_EXCLUDED_ARTICLE_URLS,
        }
        article_store.ARTICLES_FILE = root / "logs" / "articles.json"
        article_store.DOMAIN_OVERRIDES_FILE = root / "logs" / "domain_overrides.json"
        article_store.DOMAIN_MEDIA_NAMES_FILE = root / "logs" / "domain_media_names.json"
        article_store.EXCLUDED_ARTICLE_URLS_FILE = root / "logs" / "excluded_article_urls.json"
        article_store.ARTICLES_FILE.parent.mkdir(parents=True, exist_ok=True)
        article_store.ARTICLES_FILE.write_text("[]", encoding="utf-8")

    def tearDown(self) -> None:
        article_store.ARTICLES_FILE = self._original_paths["ARTICLES_FILE"]
        article_store.DOMAIN_OVERRIDES_FILE = self._original_paths["DOMAIN_OVERRIDES_FILE"]
        article_store.DOMAIN_MEDIA_NAMES_FILE = self._original_paths["DOMAIN_MEDIA_NAMES_FILE"]
        article_store.EXCLUDED_ARTICLE_URLS_FILE = self._original_paths["EXCLUDED_ARTICLE_URLS_FILE"]
        article_store.MAX_REFERENCE_EVENTS_PER_TASK = self._original_paths["MAX_REFERENCE_EVENTS_PER_TASK"]
        article_store.MAX_EXCLUDED_ARTICLE_URLS = self._original_paths["MAX_EXCLUDED_ARTICLE_URLS"]
        self._tmpdir.cleanup()

    def test_article_store_lock_follows_active_articles_file(self) -> None:
        self.assertIsInstance(article_store._lock, CrossProcessRLock)  # noqa: SLF001

        article_store.add_article({
            "id": "article-a",
            "url": "https://example.com/a",
            "title": "文章 A",
        })

        expected_lock_file = article_store.ARTICLES_FILE.with_name("article_store.lock")
        self.assertTrue(expected_lock_file.exists())

    def test_concurrent_add_article_keeps_all_unique_urls(self) -> None:
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                article_store.add_article({
                    "id": f"article-{index}",
                    "url": f"https://example.com/{index}",
                    "title": f"文章 {index}",
                })
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(index,))
            for index in range(20)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2.0)

        if errors:
            raise AssertionError(errors)

        articles = article_store.get_articles()
        self.assertEqual(len(articles), 20)
        self.assertEqual(
            {article["url"] for article in articles},
            {f"https://example.com/{index}" for index in range(20)},
        )

    def test_reference_events_are_compacted_without_losing_count(self) -> None:
        article_store.MAX_REFERENCE_EVENTS_PER_TASK = 3
        article_store.add_article({
            "id": "article-a",
            "url": "https://example.com/reference",
            "title": "引用文章",
            "matched_tasks": ["品牌A"],
        })

        for index in range(5):
            article_store.mark_articles_referenced_by_urls(
                ["品牌A"],
                ["https://example.com/reference"],
                referenced_at=f"2026-05-0{index + 1} 09:00:00",
                event_id=f"event-{index}",
                platform="doubao",
            )

        article = article_store.find_article_by_url("https://example.com/reference")
        hit = article["reference_hits"]["品牌A"]
        events = hit["events"]

        self.assertEqual(hit["count"], 5)
        self.assertTrue(hit["events_compacted"])
        self.assertEqual(len(events), 3)
        self.assertEqual([event["event_id"] for event in events], ["event-2", "event-3", "event-4"])

    def test_excluded_article_urls_are_compacted_to_recent_entries(self) -> None:
        article_store.MAX_EXCLUDED_ARTICLE_URLS = 3

        for index in range(5):
            article_store.exclude_article_url(
                f"https://example.com/deleted-{index}",
                title=f"删除 {index}",
            )

        stored = json.loads(article_store.EXCLUDED_ARTICLE_URLS_FILE.read_text(encoding="utf-8"))

        self.assertEqual(len(stored), 3)
        self.assertEqual(
            set(stored.keys()),
            {f"https://example.com/deleted-{index}" for index in range(2, 5)},
        )


if __name__ == "__main__":
    unittest.main()
