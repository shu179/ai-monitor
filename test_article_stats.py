import unittest
import threading

from core.time_utils import local_today
from web_backend import ArticleImportBatchStore, ArticleService


class _ArticleStatsConfigProvider:
    def load(self):
        return {}


def _article_service(articles, sqlite_article_page_loader=None):
    return ArticleService(
        config_provider=_ArticleStatsConfigProvider(),
        synced_articles_loader=lambda config: list(articles),
        sqlite_article_page_loader=sqlite_article_page_loader,
        invalidate_article_cache=lambda: None,
        lock=threading.RLock(),
        import_batch_store=ArticleImportBatchStore(),
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


if __name__ == "__main__":
    unittest.main()
