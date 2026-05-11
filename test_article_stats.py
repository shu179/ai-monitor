import unittest
import threading

from core.time_utils import local_today
from web_backend import ArticleImportBatchStore, ArticleService


class _ArticleStatsConfigProvider:
    def load(self):
        return {}


class _LightArticleStatsConfigProvider:
    def __init__(self):
        self.light_loads = 0

    def load(self):
        raise AssertionError("article list reads must not run config load hooks")

    def load_without_hooks(self):
        self.light_loads += 1
        return {
            "tasks": [
                {
                    "name": "品牌A",
                    "keywords": [{"keyword": "新品"}],
                }
            ]
        }


def _article_service(articles, sqlite_article_page_loader=None, sqlite_article_compare_recorder=None):
    return ArticleService(
        config_provider=_ArticleStatsConfigProvider(),
        synced_articles_loader=lambda config: list(articles),
        invalidate_article_cache=lambda: None,
        lock=threading.RLock(),
        import_batch_store=ArticleImportBatchStore(),
        sqlite_article_page_loader=sqlite_article_page_loader,
        sqlite_article_compare_recorder=sqlite_article_compare_recorder,
    )


class ArticleStatsTests(unittest.TestCase):
    def test_article_list_counts_today_by_published_date(self):
        today = local_today().isoformat()
        service = _article_service([
            {
                "id": "manual-today",
                "url": "https://example.com/manual",
                "title": "手动录入",
                "media_type": "selfmedia",
                "imported_at": f"{today} 09:30",
                "published_at": today,
                "ts": today,
            },
            {
                "id": "auto-old-published",
                "url": "https://example.com/auto",
                "title": "自动抓取",
                "media_type": "authority",
                "imported_at": f"{today} 10:10",
                "published_at": "2024-03-04",
                "ts": "2024-03-04",
            },
            {
                "id": "old",
                "url": "https://example.com/old",
                "title": "历史录入",
                "media_type": "selfmedia",
                "imported_at": "2024-01-02 08:00",
                "published_at": "2024-01-01",
                "ts": "2024-01-01",
            },
        ])

        result = service.get_articles_filtered(limit=20)

        self.assertEqual(result["total"], 3)
        self.assertEqual(result["today_total"], 1)
        self.assertEqual(len(result["articles"]), 3)

    def test_article_list_uses_light_config_load_without_hooks(self):
        provider = _LightArticleStatsConfigProvider()
        service = ArticleService(
            config_provider=provider,
            synced_articles_loader=lambda config: [
                {
                    "id": "article-a",
                    "url": "https://example.com/a",
                    "title": "品牌A 新品发布",
                    "media_type": "authority",
                    "matched_tasks": ["品牌A"],
                    "published_at": "2026-05-09",
                }
            ],
            invalidate_article_cache=lambda: None,
            lock=threading.RLock(),
            import_batch_store=ArticleImportBatchStore(),
            sqlite_article_page_loader=None,
            sqlite_article_compare_recorder=None,
        )

        result = service.get_articles_filtered(limit=20, include_export_keywords=True)

        self.assertEqual(provider.light_loads, 1)
        self.assertEqual(result["articles"][0]["exportKeywords"], ["新品"])

    def test_article_list_preserves_deleted_task_classification(self):
        provider = _LightArticleStatsConfigProvider()
        service = ArticleService(
            config_provider=provider,
            synced_articles_loader=lambda config: [
                {
                    "id": "article-a",
                    "url": "https://example.com/a",
                    "title": "已删除品牌历史文章",
                    "media_type": "authority",
                    "matched_tasks": ["已删除品牌"],
                    "match_reasons": {"已删除品牌": ["历史归类"]},
                    "published_at": "2026-05-09",
                }
            ],
            invalidate_article_cache=lambda: None,
            lock=threading.RLock(),
            import_batch_store=ArticleImportBatchStore(),
            sqlite_article_page_loader=None,
            sqlite_article_compare_recorder=None,
        )

        result = service.get_articles_filtered(limit=20)

        self.assertEqual(result["articles"][0]["matchedTasks"], ["已删除品牌"])
        self.assertEqual(result["articles"][0]["classificationStatus"], "matched")

    def test_article_list_dedupes_repeated_urls(self):
        service = _article_service([
            {
                "id": "first",
                "url": "https://example.com/news/a?utm_source=x",
                "title": "第一条",
                "media_type": "selfmedia",
                "matched_tasks": ["品牌A"],
                "published_at": "2024-03-04",
                "ts": "2024-03-04",
            },
            {
                "id": "second",
                "url": "https://www.example.com/news/a",
                "title": "重复链接",
                "media_type": "selfmedia",
                "matched_tasks": ["品牌B"],
                "published_at": "2024-03-04",
                "ts": "2024-03-04",
            },
        ])

        result = service.get_articles_filtered(limit=20)

        self.assertEqual(result["total"], 1)
        self.assertEqual(len(result["articles"]), 1)
        self.assertEqual(result["articles"][0]["matchedTasks"], ["品牌A", "品牌B"])

    def test_article_list_fills_missing_url_from_matching_article(self):
        service = _article_service([
            {
                "id": "without-link",
                "url": "",
                "title": "同一篇报道",
                "media_name": "示例媒体",
                "media_type": "selfmedia",
                "published_at": "2024-03-04",
                "ts": "2024-03-04",
            },
            {
                "id": "with-link",
                "url": "https://example.com/same",
                "title": "同一篇报道",
                "media_name": "示例媒体",
                "media_type": "selfmedia",
                "published_at": "2024-03-04",
                "ts": "2024-03-04",
            },
        ])

        result = service.get_articles_filtered(limit=20)

        self.assertEqual(result["articles"][0]["url"], "https://example.com/same")

    def test_task_article_list_fills_missing_url_from_unmatched_duplicate(self):
        service = _article_service([
            {
                "id": "without-link",
                "url": "",
                "title": "同一篇报道",
                "media_name": "示例媒体",
                "media_type": "selfmedia",
                "matched_tasks": ["品牌A"],
                "published_at": "2024-03-04",
                "ts": "2024-03-04",
            },
            {
                "id": "with-link",
                "url": "https://example.com/same",
                "title": "同一篇报道",
                "media_name": "示例媒体",
                "media_type": "selfmedia",
                "matched_tasks": [],
                "published_at": "2024-03-04",
                "ts": "2024-03-04",
            },
        ])

        result = service.get_articles_filtered(limit=20, task_name="品牌A")

        self.assertEqual(result["articles"][0]["url"], "https://example.com/same")

    def test_article_list_can_use_sqlite_page_loader_when_available(self):
        calls = []

        def sqlite_loader(config, *, media_type, limit, task_name):
            calls.append({
                "media_type": media_type,
                "limit": limit,
                "task_name": task_name,
            })
            return {
                "articles": [
                    {
                        "id": "sqlite-article",
                        "url": "https://example.com/sqlite",
                        "title": "SQLite 文章",
                        "media_type": "authority",
                        "matched_tasks": ["品牌A"],
                        "published_at": local_today().isoformat(),
                        "ts": local_today().isoformat(),
                    }
                ],
                "total": 9,
                "today_total": 2,
            }

        service = _article_service(
            [
                {
                    "id": "json-article",
                    "url": "https://example.com/json",
                    "title": "JSON 文章",
                    "media_type": "selfmedia",
                    "matched_tasks": ["品牌B"],
                    "published_at": "2024-03-04",
                    "ts": "2024-03-04",
                }
            ],
            sqlite_article_page_loader=sqlite_loader,
        )

        result = service.get_articles_filtered(media_type="media", limit=20, task_name="品牌A")

        self.assertEqual(calls, [{"media_type": "media", "limit": 20, "task_name": "品牌A"}])
        self.assertEqual(result["total"], 9)
        self.assertEqual(result["today_total"], 2)
        self.assertEqual([article["id"] for article in result["articles"]], ["sqlite-article"])

    def test_article_list_falls_back_to_json_when_sqlite_loader_unavailable(self):
        service = _article_service(
            [
                {
                    "id": "json-article",
                    "url": "https://example.com/json",
                    "title": "JSON 文章",
                    "media_type": "selfmedia",
                    "matched_tasks": ["品牌B"],
                    "published_at": "2024-03-04",
                    "ts": "2024-03-04",
                }
            ],
            sqlite_article_page_loader=lambda config, **kwargs: None,
        )

        result = service.get_articles_filtered(limit=20)

        self.assertEqual(result["total"], 1)
        self.assertEqual([article["id"] for article in result["articles"]], ["json-article"])

    def test_article_list_compare_mode_returns_json_and_records_shadow_page(self):
        comparisons = []
        service = _article_service(
            [
                {
                    "id": "json-article",
                    "url": "https://example.com/json",
                    "title": "JSON 文章",
                    "media_type": "selfmedia",
                    "matched_tasks": ["品牌B"],
                    "published_at": "2024-03-04",
                    "ts": "2024-03-04",
                }
            ],
            sqlite_article_page_loader=lambda config, **kwargs: {
                "compare_only": True,
                "articles": [
                    {
                        "id": "sqlite-article",
                        "url": "https://example.com/sqlite",
                        "title": "SQLite 文章",
                        "media_type": "selfmedia",
                        "matched_tasks": ["品牌B"],
                        "published_at": "2024-03-04",
                        "ts": "2024-03-04",
                    }
                ],
                "total": 1,
                "today_total": 0,
            },
            sqlite_article_compare_recorder=lambda **payload: comparisons.append(payload),
        )

        result = service.get_articles_filtered(limit=20)

        self.assertEqual([article["id"] for article in result["articles"]], ["json-article"])
        self.assertEqual(len(comparisons), 1)
        self.assertEqual(comparisons[0]["query"]["limit"], 20)
        self.assertEqual(comparisons[0]["json_result"]["articles"][0]["id"], "json-article")
        self.assertEqual(comparisons[0]["sqlite_page"]["articles"][0]["id"], "sqlite-article")


if __name__ == "__main__":
    unittest.main()
