from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import core.article_store as article_store
import core.history as history
from core.article_history_sqlite_mirror import (
    build_article_compare_queries,
    compare_article_pages,
    load_json_history_sources,
    rebuild_shadow_store,
    verify_shadow_store,
)
from core.article_history_sqlite_store import ArticleHistorySQLiteStore
from core.article_store import normalize_article_url


class ArticleHistorySQLiteMirrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        self._original_article_paths = {
            "ARTICLES_FILE": article_store.ARTICLES_FILE,
            "DOMAIN_OVERRIDES_FILE": article_store.DOMAIN_OVERRIDES_FILE,
            "DOMAIN_MEDIA_NAMES_FILE": article_store.DOMAIN_MEDIA_NAMES_FILE,
            "EXCLUDED_ARTICLE_URLS_FILE": article_store.EXCLUDED_ARTICLE_URLS_FILE,
        }
        self._original_history_dir = history.HISTORY_DIR
        article_store.ARTICLES_FILE = root / "logs" / "articles.json"
        article_store.DOMAIN_OVERRIDES_FILE = root / "logs" / "domain_overrides.json"
        article_store.DOMAIN_MEDIA_NAMES_FILE = root / "logs" / "domain_media_names.json"
        article_store.EXCLUDED_ARTICLE_URLS_FILE = root / "logs" / "excluded_article_urls.json"
        history.HISTORY_DIR = root / "logs" / "history"
        article_store.ARTICLES_FILE.parent.mkdir(parents=True, exist_ok=True)
        history.HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        article_store.ARTICLES_FILE = self._original_article_paths["ARTICLES_FILE"]
        article_store.DOMAIN_OVERRIDES_FILE = self._original_article_paths["DOMAIN_OVERRIDES_FILE"]
        article_store.DOMAIN_MEDIA_NAMES_FILE = self._original_article_paths["DOMAIN_MEDIA_NAMES_FILE"]
        article_store.EXCLUDED_ARTICLE_URLS_FILE = self._original_article_paths["EXCLUDED_ARTICLE_URLS_FILE"]
        history.HISTORY_DIR = self._original_history_dir
        self._tmpdir.cleanup()

    def test_rebuild_shadow_store_imports_json_sources_and_verifies(self) -> None:
        self._write_articles([
            {
                "id": "article-a",
                "url": "https://www.example.com/a?utm_source=x",
                "title": "品牌A 新闻",
                "media_name": "示例媒体",
                "media_type": "authority",
                "published_at": "2024-01-02",
                "matched_tasks": ["品牌A"],
            },
            {
                "id": "article-b",
                "url": "https://example.com/b",
                "title": "品牌B 新闻",
                "media_name": "示例自媒体",
                "media_type": "selfmedia",
                "published_at": "2024-01-03",
                "matched_tasks": ["品牌A", "品牌B"],
            },
        ])
        self._write_history_file("task-a", [
            {
                "id": "r1",
                "ts": "2024-01-01 09:00",
                "task_id": "task-a",
                "task_name": "品牌A",
                "platform": "doubao",
                "keyword": "关键词",
                "brand": "品牌A",
                "rank": 1,
                "success": True,
            },
            {
                "id": "r2",
                "ts": "2024-01-02 09:00",
                "task_id": "task-a",
                "task_name": "品牌A",
                "platform": "doubao",
                "keyword": "关键词",
                "brand": "品牌A",
                "rank": 99,
                "success": False,
            },
        ])
        self._write_history_file("brand_a_rates", [{"date": "2024-01-01", "rate": 80}])
        self._write_history_file("brand_a_trend_abcd", [{"date": "2024-01-01", "rate": 80}])
        db_path = Path(self._tmpdir.name) / "shadow.sqlite3"
        stale_store = ArticleHistorySQLiteStore(db_path, normalize_article_url=normalize_article_url)
        stale_store.import_history_records("stale", [{"id": "old", "ts": "2023-01-01 00:00"}])

        result = rebuild_shadow_store(db_path, max_workers=2)

        self.assertTrue(result["verification"]["ok"])
        self.assertEqual(result["articles"], {"created": 2, "updated": 0, "skipped": 0})
        self.assertEqual(result["history"]["storage_keys"], 1)
        self.assertEqual(result["history"]["records"], 2)

        store = ArticleHistorySQLiteStore(db_path, normalize_article_url=normalize_article_url)
        self.assertEqual(store.list_history_storage_keys(), ["task-a"])
        self.assertEqual(store.get_article_page(task_name="品牌A")["total"], 2)
        self.assertEqual(store.get_article_page(task_name="品牌B")["total"], 1)
        self.assertTrue(verify_shadow_store(db_path, max_workers=2)["ok"])

    def test_verify_shadow_store_reports_mismatches(self) -> None:
        self._write_articles([
            {
                "id": "article-a",
                "url": "https://example.com/a",
                "title": "品牌A 新闻",
                "matched_tasks": ["品牌A"],
            },
        ])
        self._write_history_file("task-a", [
            {
                "id": "r1",
                "ts": "2024-01-01 09:00",
                "task_name": "品牌A",
                "success": True,
            },
        ])
        db_path = Path(self._tmpdir.name) / "shadow.sqlite3"
        store = ArticleHistorySQLiteStore(db_path, normalize_article_url=normalize_article_url)
        store.initialize()

        verification = verify_shadow_store(db_path)

        self.assertFalse(verification["ok"])
        self.assertEqual(verification["articles"]["expected_normalized_url_total"], 1)
        self.assertEqual(verification["articles"]["sqlite_total"], 0)
        self.assertEqual(verification["history"]["expected_storage_keys"], ["task-a"])
        self.assertEqual(verification["history"]["sqlite_storage_keys"], [])

    def test_load_json_history_sources_can_read_files_in_parallel(self) -> None:
        self._write_history_file("task-a", [{"id": "r1", "ts": "2024-01-02 09:00"}])
        self._write_history_file("task-b", [{"id": "r2", "ts": "2024-01-01 09:00"}])
        self._write_history_file("brand_b_periods", [{"start": "2024-01-01", "end": "2024-01-31"}])

        sources = load_json_history_sources(max_workers=4)

        self.assertEqual(sorted(sources.keys()), ["task-a", "task-b"])
        self.assertEqual([record["id"] for record in sources["task-a"]], ["r1"])
        self.assertEqual([record["id"] for record in sources["task-b"]], ["r2"])

    def test_build_article_compare_queries_includes_task_and_media_combinations(self) -> None:
        queries = build_article_compare_queries({
            "tasks": [
                {"name": "品牌A"},
                {"name": "品牌B"},
                {"name": "品牌A"},
                {"name": "删除中", "delete_pending": True},
            ],
        })

        names = [query["name"] for query in queries]
        self.assertIn("all", names)
        self.assertIn("media:media", names)
        self.assertIn("media:self-media", names)
        self.assertIn("task:品牌A", names)
        self.assertIn("task:品牌A|media:media", names)
        self.assertIn("task:品牌B|media:self-media", names)
        self.assertNotIn("task:删除中", names)

    def test_compare_article_pages_reports_matching_json_and_sqlite_pages(self) -> None:
        articles = [
            {
                "id": "article-b",
                "url": "https://example.com/b",
                "title": "品牌B 新闻",
                "media_name": "示例自媒体",
                "media_type": "selfmedia",
                "published_at": "2024-01-03",
                "matched_tasks": ["品牌A", "品牌B"],
            },
            {
                "id": "article-a",
                "url": "https://example.com/a",
                "title": "品牌A 新闻",
                "media_name": "示例媒体",
                "media_type": "authority",
                "published_at": "2024-01-02",
                "matched_tasks": ["品牌A"],
            },
        ]
        db_path = Path(self._tmpdir.name) / "shadow.sqlite3"
        store = ArticleHistorySQLiteStore(db_path, normalize_article_url=normalize_article_url)
        store.import_articles(articles, replace=True)

        result = compare_article_pages(
            db_path,
            config={"tasks": [{"name": "品牌A"}, {"name": "品牌B"}]},
            articles=articles,
            limit=10,
            max_workers=2,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["failed_count"], 0)
        self.assertGreaterEqual(result["query_count"], 5)
        by_name = {item["name"]: item for item in result["queries"]}
        self.assertEqual(by_name["all"]["json"]["ids"], ["article-b", "article-a"])
        self.assertEqual(by_name["task:品牌B"]["json"]["ids"], ["article-b"])
        self.assertEqual(by_name["task:品牌A|media:media"]["json"]["ids"], ["article-a"])

    def test_compare_article_pages_reports_mismatches(self) -> None:
        articles = [
            {
                "id": "article-a",
                "url": "https://example.com/a",
                "title": "品牌A 新闻",
                "media_type": "authority",
                "published_at": "2024-01-02",
                "matched_tasks": ["品牌A"],
            },
        ]
        db_path = Path(self._tmpdir.name) / "shadow.sqlite3"
        ArticleHistorySQLiteStore(db_path, normalize_article_url=normalize_article_url).initialize()

        result = compare_article_pages(
            db_path,
            config={"tasks": [{"name": "品牌A"}]},
            articles=articles,
            limit=10,
        )

        self.assertFalse(result["ok"])
        self.assertGreater(result["failed_count"], 0)
        by_name = {item["name"]: item for item in result["queries"]}
        self.assertIn("total", by_name["all"]["mismatches"])
        self.assertIn("article_ids", by_name["all"]["mismatches"])

    def _write_articles(self, articles: list[dict]) -> None:
        article_store.ARTICLES_FILE.write_text(
            json.dumps(articles, ensure_ascii=False),
            encoding="utf-8",
        )

    def _write_history_file(self, stem: str, records: list[dict]) -> None:
        (history.HISTORY_DIR / f"{stem}.json").write_text(
            json.dumps(records, ensure_ascii=False),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
