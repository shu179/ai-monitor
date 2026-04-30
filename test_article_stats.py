import unittest
import threading

from core.time_utils import local_today
from web_backend import ArticleImportBatchStore, ArticleService


class _ArticleStatsConfigProvider:
    def load(self):
        return {}


def _article_service(articles):
    return ArticleService(
        config_provider=_ArticleStatsConfigProvider(),
        synced_articles_loader=lambda config: list(articles),
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


if __name__ == "__main__":
    unittest.main()
