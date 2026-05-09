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

    def test_article_page_order_matches_runtime_display_sort_tiebreakers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ArticleHistorySQLiteStore(
                Path(tmpdir) / "local_store.sqlite3",
                normalize_article_url=normalize_article_url,
            )

            store.import_articles(
                [
                    {
                        "id": "article-a",
                        "url": "https://example.com/a",
                        "title": "A",
                        "published_at": "2024-01-03",
                        "imported_at": "2024-01-03 09:00",
                    },
                    {
                        "id": "article-b",
                        "url": "https://example.com/b",
                        "title": "B",
                        "published_at": "2024-01-03",
                        "imported_at": "2024-01-03 10:00",
                    },
                    {
                        "id": "article-c",
                        "url": "https://example.com/c",
                        "title": "C",
                        "published_at": "2024-01-03",
                        "imported_at": "2024-01-03 10:00",
                    },
                    {
                        "id": "article-d",
                        "url": "https://example.com/d",
                        "title": "D",
                        "published_at": "2024-01-02",
                        "imported_at": "2024-01-04 12:00",
                    },
                ],
                replace=True,
            )

            page = store.get_article_page(limit=10)

            self.assertEqual(
                [item["id"] for item in page["items"]],
                ["article-c", "article-b", "article-a", "article-d"],
            )

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

    def test_get_history_records_for_keys_reads_multiple_keys_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ArticleHistorySQLiteStore(Path(tmpdir) / "local_store.sqlite3")
            store.import_history_sources(
                {
                    "task-a": [
                        {"id": "a2", "ts": "2024-01-02 09:00", "task_name": "品牌A"},
                        {"id": "a1", "ts": "2024-01-01 09:00", "task_name": "品牌A"},
                    ],
                    "品牌A": [
                        {"id": "legacy-1", "ts": "2024-01-03 09:00", "task_name": "品牌A"},
                    ],
                    "task-b": [
                        {"id": "b1", "ts": "2024-01-04 09:00", "task_name": "品牌B"},
                    ],
                },
                replace=True,
            )

            records_by_key = store.get_history_records_for_keys(["task-a", "品牌A", "task-a", "missing"])

            self.assertEqual([item["id"] for item in records_by_key["task-a"]], ["a1", "a2"])
            self.assertEqual([item["id"] for item in records_by_key["品牌A"]], ["legacy-1"])
            self.assertEqual(records_by_key["missing"], [])
            self.assertNotIn("task-b", records_by_key)

    def test_history_records_preserve_source_order_for_same_timestamp_missing_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ArticleHistorySQLiteStore(Path(tmpdir) / "local_store.sqlite3")
            records = [
                {"ts": "2024-01-01 09:00", "rank": 3, "marker": "first"},
                {"ts": "2024-01-01 09:00", "rank": 1, "marker": "second"},
                {"ts": "2024-01-01 09:00", "rank": 2, "marker": "third"},
            ]

            result = store.import_history_records("task-a", records, replace=True)

            self.assertEqual(result, {"created": 3, "updated": 0, "skipped": 0})
            self.assertEqual(
                [record["marker"] for record in store.get_history_records("task-a")],
                ["first", "second", "third"],
            )
            self.assertEqual(
                [record["marker"] for record in store.get_history_tail("task-a", limit=2)],
                ["second", "third"],
            )

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

    def test_append_history_record_preserves_runtime_storage_targets_and_trims(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ArticleHistorySQLiteStore(Path(tmpdir) / "local_store.sqlite3")
            for index in range(1, 5):
                entry = {
                    "id": f"r{index}",
                    "ts": f"2024-01-0{index} 09:00",
                    "task_id": "task-a",
                    "task_name": "品牌A",
                    "platform": "doubao",
                    "keyword": "关键词",
                    "brand": "品牌A",
                    "rank": 1,
                    "success": True,
                    "review_status": "pending",
                }
                primary_result = store.append_history_record("task-a", entry, max_records=3)
                legacy_result = store.append_history_record("品牌A", entry, max_records=3)

            self.assertEqual(primary_result, {"created": 1, "updated": 0, "skipped": 0, "pruned": 1})
            self.assertEqual(legacy_result, {"created": 1, "updated": 0, "skipped": 0, "pruned": 1})
            self.assertEqual([item["id"] for item in store.get_history_records("task-a")], ["r2", "r3", "r4"])
            self.assertEqual([item["id"] for item in store.get_history_records("品牌A")], ["r2", "r3", "r4"])

    def test_apply_history_review_updates_primary_and_legacy_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ArticleHistorySQLiteStore(Path(tmpdir) / "local_store.sqlite3")
            entry = {
                "id": "pending-1",
                "ts": "2024-01-01 09:00",
                "task_id": "task-review",
                "task_name": "复核任务",
                "platform": "doubao",
                "keyword": "关键词",
                "brand": "品牌A",
                "rank": 1,
                "success": True,
                "review_status": "pending",
                "review_note": "",
                "reviewed_at": "",
            }
            store.append_history_record("task-review", entry)
            store.append_history_record("复核任务", dict(entry))

            result = store.apply_history_review(
                ["task-review", "复核任务"],
                "pending-1",
                "approved",
                "ok",
                reviewed_at="2024-01-02 10:00:00",
            )

            self.assertEqual(result, {"changed": 2, "storage_keys": ["task-review", "复核任务"]})
            primary = store.get_history_records("task-review")[0]
            legacy = store.get_history_records("复核任务")[0]
            self.assertEqual(primary["review_status"], "approved")
            self.assertEqual(legacy["review_status"], "approved")
            self.assertEqual(primary["review_note"], "ok")
            self.assertEqual(primary["reviewed_at"], "2024-01-02 10:00:00")
            self.assertEqual(store.get_pending_reviews(limit=10), [])

    def test_history_task_names_follow_first_record_per_storage_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ArticleHistorySQLiteStore(Path(tmpdir) / "local_store.sqlite3")
            store.import_history_sources(
                {
                    "task-a": [
                        {"id": "r2", "ts": "2024-01-02 09:00", "task_name": "品牌A"},
                        {"id": "r1", "ts": "2024-01-01 09:00", "task_name": "品牌A"},
                    ],
                    "task-b": [
                        {"id": "r3", "ts": "2024-01-03 09:00"},
                    ],
                    "task-c": [
                        {"id": "r4", "ts": "2024-01-04 09:00", "task_name": "品牌A"},
                    ],
                },
                replace=True,
            )

            self.assertEqual(store.get_history_task_names(), ["品牌A", "task-b"])

    def test_pending_reviews_dedupes_legacy_copies_and_applies_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ArticleHistorySQLiteStore(Path(tmpdir) / "local_store.sqlite3")
            pending_record = {
                "id": "pending-1",
                "ts": "2024-01-03 09:00",
                "task_id": "task-a",
                "task_name": "品牌A",
                "rank": 1,
                "success": True,
                "review_status": "pending",
            }
            store.import_history_sources(
                {
                    "task-a": [
                        pending_record,
                        {
                            "id": "approved",
                            "ts": "2024-01-02 09:00",
                            "task_name": "品牌A",
                            "rank": 1,
                            "success": True,
                            "review_status": "approved",
                        },
                    ],
                    "品牌A": [dict(pending_record)],
                    "task-b": [
                        {
                            "id": "pending-2",
                            "ts": "2024-01-04 09:00",
                            "task_name": "品牌B",
                            "rank": 2,
                            "success": True,
                            "review_status": "pending",
                        },
                        {
                            "id": "miss",
                            "ts": "2024-01-05 09:00",
                            "task_name": "品牌B",
                            "rank": 99,
                            "success": False,
                            "review_status": "pending",
                        },
                        {
                            "id": "success-without-review",
                            "ts": "2024-01-06 09:00",
                            "task_name": "品牌B",
                            "rank": 1,
                            "success": True,
                            "review_status": "",
                        },
                    ],
                },
                replace=True,
            )

            reviews = store.get_pending_reviews(limit=1)

            self.assertEqual([item["id"] for item in reviews], ["pending-2"])
            self.assertEqual([item["id"] for item in store.get_pending_reviews(limit=10)], ["pending-2", "pending-1"])

    def test_pending_reviews_preserve_json_stable_order_for_equal_sort_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ArticleHistorySQLiteStore(Path(tmpdir) / "local_store.sqlite3")
            store.import_history_sources(
                {
                    "task-a": [
                        {
                            "id": "pending-a1",
                            "ts": "2024-01-03 09:00",
                            "task_name": "品牌A",
                            "rank": 1,
                            "success": True,
                            "review_status": "pending",
                        },
                        {
                            "id": "pending-a2",
                            "ts": "2024-01-03 09:00",
                            "task_name": "品牌A",
                            "rank": 2,
                            "success": True,
                            "review_status": "pending",
                        },
                    ],
                    "task-b": [
                        {
                            "id": "pending-b1",
                            "ts": "2024-01-03 09:00",
                            "task_name": "品牌A",
                            "rank": 3,
                            "success": True,
                            "review_status": "pending",
                        },
                    ],
                },
                replace=True,
            )

            reviews = store.get_pending_reviews(limit=10)

            self.assertEqual([item["id"] for item in reviews], ["pending-a1", "pending-a2", "pending-b1"])

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
            self.assertIn("idx_history_storage_order", names)

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
