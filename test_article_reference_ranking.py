from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import core.article_reference_index as article_reference_index
import core.article_store as article_store
from core.file_lock import CrossProcessRLock
import core.history as history
from backend_lib.article_reference_service import ArticleReferenceService


class _RankingConfigProvider:
    def load(self):
        return {
            "tasks": [
                {
                    "task_id": "task-a",
                    "name": "品牌A",
                    "brand": "品牌A",
                    "keywords": [
                        {"keyword": "关键词A", "brand": "品牌A", "platforms": ["doubao", "deepseek", "kimi"]},
                    ],
                }
            ]
        }


class ArticleReferenceRankingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_history_dir = history.HISTORY_DIR
        self._original_articles_file = article_store.ARTICLES_FILE
        self._original_index_dir = article_reference_index.INDEX_DIR
        history.HISTORY_DIR = Path(self._tmpdir.name) / "logs" / "history"
        article_store.ARTICLES_FILE = Path(self._tmpdir.name) / "logs" / "articles.json"
        article_store.ARTICLES_FILE.parent.mkdir(parents=True, exist_ok=True)
        article_store.ARTICLES_FILE.write_text("[]", encoding="utf-8")
        article_reference_index.INDEX_DIR = Path(self._tmpdir.name) / "logs" / "article_reference_index"
        self.articles = [
            {
                "id": "article-a",
                "url": "http://www.example.com/news/a?utm_source=import#frag",
                "title": "品牌A 报道",
                "media_name": "示例媒体",
                "platform": "示例媒体",
                "media_type": "authority",
                "matched_tasks": ["品牌A"],
            }
        ]
        self.service = ArticleReferenceService(
            config_provider=_RankingConfigProvider(),
            synced_articles_loader=lambda config: list(self.articles),
        )

    def tearDown(self) -> None:
        history.HISTORY_DIR = self._original_history_dir
        article_store.ARTICLES_FILE = self._original_articles_file
        article_reference_index.INDEX_DIR = self._original_index_dir
        self._tmpdir.cleanup()

    def _write_records(self, records: list[dict]) -> None:
        history._save(history._task_file("品牌A"), records)

    def _base_records(self) -> list[dict]:
        url = "http://www.example.com/news/a?utm_source=feed#section"
        return [
            {
                "id": "r1",
                "ts": "2026-05-01 09:00",
                "task_name": "品牌A",
                "platform": "doubao",
                "keyword": "关键词A",
                "brand": "品牌A",
                "rank": 99,
                "success": False,
                "mode": "browser",
                "extra": {
                    "references": [{"url": url}],
                    "body_references": [{"url": "https://example.com/news/a"}],
                },
            },
            {
                "id": "r2",
                "ts": "2026-05-01 10:00",
                "task_name": "品牌A",
                "platform": "doubao",
                "keyword": "关键词A",
                "brand": "品牌A",
                "rank": 99,
                "success": False,
                "mode": "browser",
                "answer_text": f"正文里只有兜底链接 {url}",
                "extra": {},
            },
            {
                "id": "r3",
                "ts": "2026-05-02 11:00",
                "task_name": "品牌A",
                "platform": "recognition",
                "keyword": "clipboard",
                "brand": "品牌A",
                "rank": 1,
                "success": True,
                "mode": "recognition",
                "extra": {
                    "detected_platforms": ["deepseek"],
                    "references": [{"url": "https://example.com/news/a"}],
                },
            },
            {
                "id": "r4-no-platform",
                "ts": "2026-05-02 12:00",
                "task_name": "品牌A",
                "platform": "recognition",
                "keyword": "clipboard",
                "brand": "品牌A",
                "rank": 1,
                "success": True,
                "mode": "recognition",
                "extra": {"references": [{"url": "https://example.com/news/a"}]},
            },
            {
                "id": "r5-manual-test",
                "ts": "2026-05-02 13:00",
                "task_name": "品牌A",
                "platform": "doubao",
                "keyword": "关键词A",
                "brand": "品牌A",
                "rank": 1,
                "success": True,
                "mode": "browser",
                "execution_source": "manual_test",
                "extra": {"references": [{"url": "https://example.com/news/a"}]},
            },
        ]

    def test_reference_index_uses_cross_process_lock_in_index_dir(self):
        self.assertIsInstance(article_reference_index._INDEX_LOCK, CrossProcessRLock)
        with article_reference_index._INDEX_LOCK:
            self.assertTrue((article_reference_index.INDEX_DIR / ".index.lock").exists())

    def test_ranking_dedupes_record_urls_and_uses_detected_recognition_platforms(self):
        self._write_records(self._base_records())

        result = self.service.get_task_article_reference_ranking("task-a")

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["article"]["id"], "article-a")
        self.assertEqual(item["raw_ref_count"], 3)
        self.assertEqual(item["effective_event_count"], 2)
        self.assertEqual(item["platform_count"], 2)
        self.assertEqual(item["active_days"], 2)
        self.assertEqual(item["span_days"], 2)
        platform_stats = {platform["id"]: platform for platform in item["platforms"]}
        self.assertEqual(platform_stats["doubao"]["raw_count"], 2)
        self.assertEqual(platform_stats["doubao"]["event_count"], 1)
        self.assertEqual(platform_stats["deepseek"]["raw_count"], 1)
        self.assertEqual(result["daily_points"], [
            {"date": "2026-05-01", "article_count": 1, "event_count": 1, "raw_count": 2},
            {"date": "2026-05-02", "article_count": 1, "event_count": 1, "raw_count": 1},
        ])
        available = {platform["id"]: platform for platform in result["available_platforms"]}
        self.assertTrue(available["doubao"]["configured"])
        self.assertEqual(available["doubao"]["event_count"], 1)
        self.assertEqual(available["kimi"]["event_count"], 0)

    def test_date_filter_recomputes_platform_count_before_cross_filter(self):
        self._write_records(self._base_records())

        day_one = self.service.get_task_article_reference_ranking(
            "task-a",
            date_from="2026-05-01",
            date_to="2026-05-01",
        )
        self.assertEqual(day_one["items"][0]["platform_count"], 1)
        self.assertEqual(day_one["items"][0]["raw_ref_count"], 2)
        self.assertEqual(day_one["items"][0]["effective_event_count"], 1)

        cross_day_one = self.service.get_task_article_reference_ranking(
            "task-a",
            platform="cross",
            date_from="2026-05-01",
            date_to="2026-05-01",
        )
        self.assertEqual(cross_day_one["items"], [])

        cross_full_range = self.service.get_task_article_reference_ranking("task-a", platform="cross")
        self.assertEqual(len(cross_full_range["items"]), 1)

    def test_referenced_articles_without_history_can_still_enter_ranking(self):
        self.articles = [
            {
                "id": "article-a",
                "url": "http://www.example.com/news/a?utm_source=import#frag",
                "title": "品牌A 报道",
                "media_name": "示例媒体",
                "platform": "示例媒体",
                "media_type": "authority",
                "matched_tasks": [],
                "referenced_tasks": ["品牌A"],
                "reference_hits": {
                    "品牌A": {
                        "count": 2,
                        "last_referenced_at": "2026-05-04 12:30:00",
                        "source": "recognition_clipboard_url",
                    }
                },
            }
        ]
        self._write_records([])

        result = self.service.get_task_article_reference_ranking("task-a")

        self.assertTrue(result["items"])
        item = result["items"][0]
        self.assertEqual(item["article"]["id"], "article-a")
        self.assertEqual(item["raw_ref_count"], 2)
        self.assertEqual(item["effective_event_count"], 1)
        self.assertEqual(item["platform_count"], 0)
        self.assertEqual(item["platforms"], [])

    def test_article_store_events_preserve_platform_day_and_raw_counts(self):
        self.articles = [
            {
                "id": "article-a",
                "url": "https://example.com/news/a",
                "title": "品牌A 报道",
                "media_name": "示例媒体",
                "media_type": "authority",
                "matched_tasks": [],
                "referenced_tasks": ["品牌A"],
                "reference_hits": {
                    "品牌A": {
                        "count": 3,
                        "last_referenced_at": "2026-05-04 11:00:00",
                        "source": "recognition_clipboard_url",
                        "events": [
                            {
                                "event_id": "event-1",
                                "platform": "豆包",
                                "referenced_at": "2026-05-03 09:00:00",
                                "source": "recognition_clipboard_url",
                            },
                            {
                                "event_id": "event-2",
                                "platform": "doubao",
                                "referenced_at": "2026-05-03 10:00:00",
                                "source": "recognition_clipboard_url",
                            },
                            {
                                "event_id": "event-3",
                                "platform": "deepseek",
                                "referenced_at": "2026-05-04 11:00:00",
                                "source": "recognition_clipboard_url",
                            },
                        ],
                    }
                },
            }
        ]
        self._write_records([])

        result = self.service.get_task_article_reference_ranking("task-a")

        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["raw_ref_count"], 3)
        self.assertEqual(item["effective_event_count"], 2)
        self.assertEqual(item["platform_count"], 2)
        self.assertEqual(item["active_days"], 2)
        platform_stats = {platform["id"]: platform for platform in item["platforms"]}
        self.assertEqual(platform_stats["doubao"]["raw_count"], 2)
        self.assertEqual(platform_stats["doubao"]["event_count"], 1)
        self.assertEqual(platform_stats["deepseek"]["raw_count"], 1)
        self.assertEqual(result["daily_points"], [
            {"date": "2026-05-03", "article_count": 1, "event_count": 1, "raw_count": 2},
            {"date": "2026-05-04", "article_count": 1, "event_count": 1, "raw_count": 1},
        ])

        doubao_only = self.service.get_task_article_reference_ranking("task-a", platform="doubao")
        self.assertEqual(doubao_only["items"][0]["raw_ref_count"], 2)
        self.assertEqual(doubao_only["items"][0]["platform_count"], 1)

    def test_snapshot_cache_reuses_loaded_articles_until_source_changes(self):
        original_articles_file = article_store.ARTICLES_FILE
        article_store.ARTICLES_FILE = Path(self._tmpdir.name) / "logs" / "articles.json"
        article_store.ARTICLES_FILE.parent.mkdir(parents=True, exist_ok=True)
        article_store.ARTICLES_FILE.write_text("[]", encoding="utf-8")

        load_count = {"value": 0}

        def load_articles(config):
            load_count["value"] += 1
            return list(self.articles)

        service = ArticleReferenceService(
            config_provider=_RankingConfigProvider(),
            synced_articles_loader=load_articles,
        )

        try:
            self._write_records([
                {
                    "id": "r1",
                    "ts": "2026-05-01 09:00",
                    "task_name": "品牌A",
                    "platform": "doubao",
                    "keyword": "关键词A",
                    "brand": "品牌A",
                    "rank": 1,
                    "success": True,
                    "mode": "browser",
                    "extra": {"references": [{"url": "https://example.com/news/a"}]},
                }
            ])

            first = service.get_task_article_reference_ranking("task-a", platform="all")
            second = service.get_task_article_reference_ranking("task-a", platform="doubao")
            self.assertEqual(load_count["value"], 1)
            self.assertEqual(first["items"][0]["article"]["id"], "article-a")
            self.assertEqual(second["items"][0]["article"]["id"], "article-a")

            self._write_records([
                {
                    "id": "r1",
                    "ts": "2026-05-01 09:00",
                    "task_name": "品牌A",
                    "platform": "doubao",
                    "keyword": "关键词A",
                    "brand": "品牌A",
                    "rank": 1,
                    "success": True,
                    "mode": "browser",
                    "extra": {"references": [{"url": "https://example.com/news/a"}]},
                },
                {
                    "id": "r2",
                    "ts": "2026-05-02 09:00",
                    "task_name": "品牌A",
                    "platform": "deepseek",
                    "keyword": "关键词A",
                    "brand": "品牌A",
                    "rank": 1,
                    "success": True,
                    "mode": "browser",
                    "extra": {"references": [{"url": "https://example.com/news/a"}]},
                },
            ])

            third = service.get_task_article_reference_ranking("task-a")
            self.assertEqual(load_count["value"], 2)
            self.assertEqual(third["items"][0]["effective_event_count"], 2)
        finally:
            article_store.ARTICLES_FILE = original_articles_file

    def test_disk_index_is_persisted_and_reused_without_reloading_history(self):
        self._write_records([
            {
                "id": "r1",
                "ts": "2026-05-01 09:00",
                "task_name": "品牌A",
                "platform": "doubao",
                "keyword": "关键词A",
                "brand": "品牌A",
                "rank": 1,
                "success": True,
                "mode": "browser",
                "extra": {"references": [{"url": "https://example.com/news/a"}]},
            }
        ])

        first = self.service.get_task_article_reference_ranking("task-a")

        self.assertEqual(first["items"][0]["raw_ref_count"], 1)
        index_signature = article_reference_index.get_reference_index_file_signature("品牌A", "task-a")
        self.assertGreater(index_signature[1], 0)
        self.assertGreater(index_signature[2], 0)

        fresh_service = ArticleReferenceService(
            config_provider=_RankingConfigProvider(),
            synced_articles_loader=lambda config: list(self.articles),
        )
        with patch(
            "backend_lib.article_reference_service.get_records",
            side_effect=AssertionError("disk index should avoid full history reload"),
        ):
            second = fresh_service.get_task_article_reference_ranking("task-a")

        self.assertEqual(second["items"][0]["raw_ref_count"], 1)
        self.assertEqual(second["items"][0]["article"]["id"], "article-a")

    def test_empty_data_is_safe_and_order_is_stable(self):
        self._write_records([])
        empty = self.service.get_task_article_reference_ranking("task-a")
        self.assertEqual(empty["items"], [])

        self.articles = [
            {
                "id": "article-b",
                "url": "https://example.com/news/b",
                "title": "B",
                "media_name": "示例媒体",
                "media_type": "authority",
                "matched_tasks": ["品牌A"],
            },
            {
                "id": "article-a",
                "url": "https://example.com/news/a",
                "title": "A",
                "media_name": "示例媒体",
                "media_type": "authority",
                "matched_tasks": ["品牌A"],
            },
        ]
        self._write_records([
            {
                "id": "same-b",
                "ts": "2026-05-03 09:00",
                "task_name": "品牌A",
                "platform": "doubao",
                "keyword": "关键词A",
                "brand": "品牌A",
                "rank": 1,
                "success": True,
                "mode": "browser",
                "extra": {"references": [{"url": "https://example.com/news/b"}]},
            },
            {
                "id": "same-a",
                "ts": "2026-05-03 09:00",
                "task_name": "品牌A",
                "platform": "doubao",
                "keyword": "关键词A",
                "brand": "品牌A",
                "rank": 1,
                "success": True,
                "mode": "browser",
                "extra": {"references": [{"url": "https://example.com/news/a"}]},
            },
        ])

        first = self.service.get_task_article_reference_ranking("task-a")
        second = self.service.get_task_article_reference_ranking("task-a")

        self.assertEqual([item["article"]["id"] for item in first["items"]], ["article-a", "article-b"])
        self.assertEqual(
            [item["article"]["id"] for item in first["items"]],
            [item["article"]["id"] for item in second["items"]],
        )


if __name__ == "__main__":
    unittest.main()
