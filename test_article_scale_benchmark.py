from __future__ import annotations

import tempfile
import unittest
from collections import Counter
from datetime import date
from pathlib import Path

from core import article_store
from scripts.article_scale_benchmark import (
    BenchmarkOptions,
    build_synthetic_articles,
    build_synthetic_config,
    run_benchmark,
)


class ArticleScaleBenchmarkTests(unittest.TestCase):
    def test_synthetic_articles_include_scale_dimensions(self) -> None:
        articles = build_synthetic_articles(
            30,
            task_count=4,
            duplicate_every=7,
            today_every=3,
            today=date(2026, 5, 9),
        )
        urls = [str(article.get("url") or "") for article in articles]
        url_counts = Counter(urls)

        self.assertEqual(len(articles), 30)
        self.assertTrue(all(str(article.get("id") or "").startswith("article-") for article in articles))
        self.assertIn("authority", {article.get("media_type") for article in articles})
        self.assertIn("selfmedia", {article.get("media_type") for article in articles})
        self.assertGreaterEqual(sum(1 for article in articles if article.get("published_at") == "2026-05-09"), 10)
        self.assertTrue(any(count > 1 for count in url_counts.values()))
        self.assertTrue(all(article.get("matched_tasks") for article in articles))

    def test_synthetic_config_contains_multiple_brand_tasks(self) -> None:
        config = build_synthetic_config(5)

        self.assertEqual(len(config["tasks"]), 5)
        self.assertEqual(config["tasks"][0]["name"], "Brand 0")
        self.assertTrue(config["tasks"][0]["keywords"])

    def test_small_benchmark_summary_is_isolated_and_complete(self) -> None:
        original_articles_file = article_store.ARTICLES_FILE
        original_shadow_file = article_store.ARTICLE_SHADOW_DB_FILE

        with tempfile.TemporaryDirectory() as tmpdir:
            summary = run_benchmark(
                BenchmarkOptions(
                    count=120,
                    task_count=4,
                    page_limit=10,
                    bulk_size=6,
                    duplicate_every=11,
                    today_every=4,
                    article_store_backend="json",
                    data_dir=Path(tmpdir),
                    force=True,
                )
            )

            self.assertTrue((Path(tmpdir) / "logs" / "articles.json").exists())
            self.assertTrue((Path(tmpdir) / "logs" / "article_history_shadow.sqlite3").exists())

        self.assertEqual(article_store.ARTICLES_FILE, original_articles_file)
        self.assertEqual(article_store.ARTICLE_SHADOW_DB_FILE, original_shadow_file)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["input"]["requested_count"], 120)
        self.assertEqual(summary["input"]["article_store_backend"], "json")
        self.assertIn("article_counts", summary)
        self.assertIn("standards", summary)
        self.assertIn("recommendations", summary)

        operations = {operation["name"]: operation for operation in summary["operations"]}
        for name in (
            "rebuild_sqlite_shadow",
            "article_page_default_first_screen",
            "article_page_by_task_name",
            "article_page_by_media_type",
            "sqlite_today_total",
            "bulk_upsert_small_batch",
            "update_article_single",
            "delete_article_single",
            "refresh_article_matches",
        ):
            self.assertIn(name, operations)
            self.assertTrue(operations[name]["ok"], operations[name].get("error", ""))

        default_page = operations["article_page_default_first_screen"]["details"]
        self.assertEqual(default_page["effective_backend"], "sqlite_shadow")
        self.assertEqual(default_page["fallback_reason"], "")
        self.assertEqual(default_page["json_loader_calls"], 0)
        self.assertLessEqual(default_page["returned_count"], 10)
        self.assertEqual(
            summary["standards"]["json_write_path_bottlenecks"]["status"],
            "known_bottleneck",
        )

    def test_small_benchmark_supports_sqlite_authoritative_backend(self) -> None:
        original_articles_file = article_store.ARTICLES_FILE
        original_shadow_file = article_store.ARTICLE_SHADOW_DB_FILE

        with tempfile.TemporaryDirectory() as tmpdir:
            summary = run_benchmark(
                BenchmarkOptions(
                    count=120,
                    task_count=4,
                    page_limit=10,
                    bulk_size=6,
                    duplicate_every=11,
                    today_every=4,
                    article_store_backend="sqlite",
                    data_dir=Path(tmpdir),
                    force=True,
                )
            )

            self.assertTrue((Path(tmpdir) / "logs" / "articles.json").exists())
            self.assertTrue((Path(tmpdir) / "logs" / "article_store.sqlite3").exists())
            self.assertTrue((Path(tmpdir) / "logs" / "article_history_shadow.sqlite3").exists())

        self.assertEqual(article_store.ARTICLES_FILE, original_articles_file)
        self.assertEqual(article_store.ARTICLE_SHADOW_DB_FILE, original_shadow_file)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["input"]["article_store_backend"], "sqlite")
        self.assertEqual(
            summary["standards"]["json_write_path_bottlenecks"]["status"],
            "sqlite_measurement",
        )
        operations = {operation["name"]: operation for operation in summary["operations"]}
        self.assertEqual(
            operations["bulk_upsert_small_batch"]["details"]["effective_backend"],
            "sqlite_authoritative",
        )
        self.assertEqual(
            operations["update_article_single"]["details"]["effective_backend"],
            "sqlite_authoritative",
        )
        self.assertEqual(
            operations["delete_article_single"]["details"]["effective_backend"],
            "sqlite_authoritative",
        )
        self.assertEqual(
            operations["refresh_article_matches"]["details"]["effective_backend"],
            "sqlite_authoritative_full_scan",
        )


if __name__ == "__main__":
    unittest.main()
