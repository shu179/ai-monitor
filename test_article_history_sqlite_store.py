from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.article_history_sqlite_store import ArticleHistorySQLiteStore
from core.article_store import normalize_article_url


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
