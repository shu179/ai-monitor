from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.article_history_sqlite_store import ArticleHistorySQLiteStore
from core.article_store import normalize_article_url
from core.time_utils import local_today


class _FakeConnection:
    def __init__(self) -> None:
        self.closed = False
        self.commit_count = 0
        self.rollback_count = 0

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1

    def close(self) -> None:
        self.closed = True


class ArticleHistorySQLiteStoreTests(unittest.TestCase):
    def test_import_articles_supports_structured_paging_and_task_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ArticleHistorySQLiteStore(
                Path(tmpdir) / "local_store.sqlite3",
                normalize_article_url=normalize_article_url,
            )

            result = store.import_articles([
                {
                    "id": "article-old",
                    "url": "https://www.example.com/a/?utm_source=x",
                    "title": "旧标题",
                    "media_name": "示例媒体",
                    "media_type": "authority",
                    "published_at": "2024-01-01",
                    "matched_tasks": ["品牌A"],
                },
                {
                    "id": "article-newer",
                    "url": "https://example.com/b",
                    "title": "较新标题",
                    "media_name": "示例自媒体",
                    "media_type": "selfmedia",
                    "published_at": "2024-01-03",
                    "matched_tasks": ["品牌A", "品牌B"],
                    "referenced_tasks": ["品牌B"],
                },
                {
                    "id": "article-duplicate-url",
                    "url": "https://example.com/a",
                    "title": "URL 更新标题",
                    "media_name": "更新媒体",
                    "media_type": "authority",
                    "published_at": "2024-01-02",
                    "matched_tasks": ["品牌C"],
                },
            ])

            self.assertEqual(result, {"created": 2, "updated": 1, "skipped": 0})

            page = store.get_article_page(limit=10)
            self.assertEqual(page["total"], 2)
            self.assertEqual([item["id"] for item in page["items"]], ["article-newer", "article-old"])
            self.assertEqual(page["items"][1]["title"], "URL 更新标题")

            brand_a_page = store.get_article_page(task_name="品牌A")
            self.assertEqual([item["id"] for item in brand_a_page["items"]], ["article-newer"])

            brand_b_referenced = store.get_article_page(task_name="品牌B", relation="referenced")
            self.assertEqual([item["id"] for item in brand_b_referenced["items"]], ["article-newer"])

            selfmedia_page = store.get_article_page(media_type="selfmedia")
            self.assertEqual([item["id"] for item in selfmedia_page["items"]], ["article-newer"])

            search_page = store.get_article_page(search="更新")
            self.assertEqual([item["id"] for item in search_page["items"]], ["article-old"])

    def test_import_history_records_dedupes_by_storage_key_and_record_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ArticleHistorySQLiteStore(Path(tmpdir) / "local_store.sqlite3")

            result = store.import_history_records("task-a", [
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
                    "ts": "2024-01-03 09:00",
                    "task_id": "task-a",
                    "task_name": "品牌A",
                    "platform": "doubao",
                    "keyword": "关键词",
                    "brand": "品牌A",
                    "rank": 1,
                    "success": True,
                },
            ])

            self.assertEqual(result, {"created": 2, "updated": 1, "skipped": 0})
            records = store.get_history_records("task-a")
            self.assertEqual([record["id"] for record in records], ["r1", "r2"])
            self.assertEqual(records[-1]["ts"], "2024-01-03 09:00")
            self.assertEqual(store.list_history_storage_keys(), ["task-a"])

            limited = store.get_history_records("task-a", limit=1, offset=1)
            self.assertEqual([record["id"] for record in limited], ["r2"])

    def test_import_history_sources_batches_multiple_keys_in_one_store_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ArticleHistorySQLiteStore(Path(tmpdir) / "local_store.sqlite3")
            store.import_history_records("stale", [{"id": "old", "ts": "2023-01-01 00:00"}])

            result = store.import_history_sources(
                {
                    "task-a": [
                        {"id": "r1", "ts": "2024-01-01 09:00", "task_name": "品牌A"},
                        {"id": "r2", "ts": "2024-01-02 09:00", "task_name": "品牌A"},
                    ],
                    "task-b": [
                        {"id": "r3", "ts": "2024-01-03 09:00", "task_name": "品牌B"},
                        "bad-record",
                    ],
                },
                replace=True,
            )

            self.assertEqual(result["storage_keys"], 2)
            self.assertEqual(result["created"], 3)
            self.assertEqual(result["updated"], 0)
            self.assertEqual(result["skipped"], 1)
            self.assertEqual(store.list_history_storage_keys(), ["task-a", "task-b"])
            self.assertEqual(store.get_history_record_count("stale"), 0)
            self.assertEqual(store.get_history_record_count("task-a"), 2)
            self.assertEqual(store.get_history_record_count("task-b"), 1)

    def test_large_article_import_supports_bounded_page_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ArticleHistorySQLiteStore(
                Path(tmpdir) / "local_store.sqlite3",
                normalize_article_url=normalize_article_url,
            )
            articles = [
                {
                    "id": f"article-{index}",
                    "url": f"https://example.com/articles/{index}",
                    "title": f"文章 {index}",
                    "media_name": "示例媒体",
                    "media_type": "authority" if index % 2 == 0 else "selfmedia",
                    "published_at": f"2024-01-{(index % 28) + 1:02d}",
                    "matched_tasks": [f"品牌{index % 5}"],
                }
                for index in range(5000)
            ]

            result = store.import_articles(articles, replace=True)
            page = store.get_article_page(limit=50)
            brand_page = store.get_article_page(task_name="品牌3", limit=25)

            self.assertEqual(result, {"created": 5000, "updated": 0, "skipped": 0})
            self.assertEqual(page["total"], 5000)
            self.assertEqual(len(page["items"]), 50)
            self.assertEqual(brand_page["total"], 1000)
            self.assertEqual(len(brand_page["items"]), 25)

    def test_article_today_count_matches_runtime_published_date_rules(self) -> None:
        today = local_today().isoformat()
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ArticleHistorySQLiteStore(
                Path(tmpdir) / "local_store.sqlite3",
                normalize_article_url=normalize_article_url,
            )

            store.import_articles(
                [
                    {
                        "id": "published-today",
                        "url": "https://example.com/today",
                        "title": "今日发布",
                        "media_type": "authority",
                        "published_at": today,
                        "ts": "2024-01-01",
                        "matched_tasks": ["品牌A"],
                    },
                    {
                        "id": "auto-ts-today",
                        "url": "https://example.com/auto",
                        "title": "自动抓取今日",
                        "media_type": "selfmedia",
                        "published_at": "",
                        "ts": today,
                        "fetch_method": "html",
                        "matched_tasks": ["品牌A", "品牌B"],
                    },
                    {
                        "id": "manual-ts-today",
                        "url": "https://example.com/manual",
                        "title": "手工导入今日",
                        "media_type": "selfmedia",
                        "published_at": "",
                        "ts": today,
                        "fetch_method": "manual_table_import",
                        "matched_tasks": ["品牌A"],
                    },
                    {
                        "id": "old",
                        "url": "https://example.com/old",
                        "title": "旧文章",
                        "media_type": "authority",
                        "published_at": "2024-01-01",
                        "ts": "2024-01-01",
                        "matched_tasks": ["品牌A"],
                    },
                ],
                replace=True,
            )

            self.assertEqual(store.get_article_today_count(today), 2)
            self.assertEqual(store.get_article_today_count(today, task_name="品牌A"), 2)
            self.assertEqual(store.get_article_today_count(today, task_name="品牌B"), 1)
            self.assertEqual(store.get_article_today_count(today, media_type="authority"), 1)

    def test_initialize_creates_expected_tables_and_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "local_store.sqlite3"
            store = ArticleHistorySQLiteStore(db_path)

            store.initialize()

            with sqlite3.connect(db_path) as conn:
                names = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
                    ).fetchall()
                }
                schema_version = conn.execute(
                    "SELECT value FROM store_meta WHERE key = 'schema_version'"
                ).fetchone()[0]
            self.assertEqual(schema_version, "1")
            self.assertIn("articles", names)
            self.assertIn("article_task_links", names)
            self.assertIn("history_records", names)
            self.assertIn("idx_article_task_links_lookup", names)
            self.assertIn("idx_history_storage_ts", names)

    def test_connection_context_closes_after_success_and_error(self) -> None:
        store = ArticleHistorySQLiteStore(Path("unused.sqlite3"))
        success_conn = _FakeConnection()
        store._connect = lambda: success_conn  # type: ignore[method-assign]

        with store._connection() as conn:
            self.assertIs(conn, success_conn)

        self.assertTrue(success_conn.closed)
        self.assertEqual(success_conn.commit_count, 1)
        self.assertEqual(success_conn.rollback_count, 0)

        error_conn = _FakeConnection()
        store._connect = lambda: error_conn  # type: ignore[method-assign]

        with self.assertRaises(RuntimeError):
            with store._connection():
                raise RuntimeError("boom")

        self.assertTrue(error_conn.closed)
        self.assertEqual(error_conn.commit_count, 0)
        self.assertEqual(error_conn.rollback_count, 1)


if __name__ == "__main__":
    unittest.main()
