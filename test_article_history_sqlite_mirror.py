from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.article_history_sqlite_mirror as sqlite_mirror
import core.article_store as article_store
import core.history as history
from core.article_history_sqlite_mirror import (
    build_article_compare_queries,
    build_history_task_read_queries,
    compare_article_pages,
    compare_history_derived_views,
    compare_history_records,
    compare_history_task_reads,
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

    def test_build_history_task_read_queries_includes_active_config_tasks(self) -> None:
        queries = build_history_task_read_queries({
            "tasks": [
                {"task_id": "task-a", "name": "品牌A"},
                {"task_id": "task-a", "name": "品牌A"},
                {"name": "品牌B", "keywords": [{"keyword": "词", "brand": "品牌B"}]},
                {"task_id": "deleted", "name": "删除中", "delete_pending": True},
            ],
        })

        self.assertEqual(len(queries), 2)
        self.assertEqual(queries[0]["task_id"], "task-a")
        self.assertEqual(queries[0]["task_name"], "品牌A")
        self.assertEqual(queries[1]["task_name"], "品牌B")
        self.assertNotEqual(queries[1]["task_id"], "")

    def test_load_current_config_falls_back_when_account_config_missing(self) -> None:
        missing_account_path = Path(self._tmpdir.name) / "missing" / "config.yaml"
        fallback_path = Path(self._tmpdir.name) / "config.yaml"
        fallback_path.write_text("tasks: []\n", encoding="utf-8")
        calls = []

        def fake_load_config(path: str) -> dict:
            calls.append(path)
            return {"loaded_from": path}

        with (
            patch("core.article_history_sqlite_mirror.current_account_config_path", lambda: missing_account_path),
            patch("core.article_history_sqlite_mirror.resolve_app_path", lambda path: fallback_path),
            patch("core.article_history_sqlite_mirror.load_config", fake_load_config),
        ):
            config = sqlite_mirror._load_current_config()

        self.assertEqual(config, {"loaded_from": str(fallback_path)})
        self.assertEqual(calls, [str(fallback_path)])

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

    def test_compare_history_records_reports_matching_pages(self) -> None:
        source = {
            "task-a": [
                {"id": f"r{index}", "ts": f"2024-01-{index:02d} 09:00", "rank": index}
                for index in range(1, 8)
            ],
            "task-b": [
                {"id": "b1", "ts": "2024-02-01 10:00", "success": True},
            ],
        }
        db_path = Path(self._tmpdir.name) / "shadow.sqlite3"
        store = ArticleHistorySQLiteStore(db_path, normalize_article_url=normalize_article_url)
        store.import_history_sources(source, replace=True)

        result = compare_history_records(
            db_path,
            source=source,
            limit=2,
            sample_pages=2,
            max_workers=2,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["failed_count"], 0)
        self.assertEqual(result["workers"], 2)
        by_name = {item["name"]: item for item in result["queries"]}
        self.assertEqual(by_name["task-a"]["json"]["count"], 7)
        self.assertGreaterEqual(len(by_name["task-a"]["windows"]), 3)
        self.assertEqual(by_name["task-a"]["windows"][0]["json"]["count"], 2)
        self.assertNotIn("json_records", by_name["task-a"]["windows"][0])
        self.assertEqual(by_name["task-b"]["windows"][0]["name"], "head")

    def test_compare_history_records_matches_same_timestamp_missing_id_order(self) -> None:
        source = {
            "task-a": [
                {"ts": "2024-01-01 09:00", "rank": 3, "marker": "first"},
                {"ts": "2024-01-01 09:00", "rank": 1, "marker": "second"},
                {"ts": "2024-01-01 09:00", "rank": 2, "marker": "third"},
            ],
        }
        db_path = Path(self._tmpdir.name) / "shadow.sqlite3"

        result = compare_history_records(
            db_path,
            source=source,
            limit=10,
            sample_pages=1,
            rebuild=True,
        )

        self.assertTrue(result["ok"])
        by_name = {item["name"]: item for item in result["queries"]}
        self.assertEqual(by_name["task-a"]["mismatches"], [])

    def test_compare_history_records_reports_count_and_record_mismatches(self) -> None:
        source = {
            "task-a": [
                {"id": "r1", "ts": "2024-01-01 09:00", "rank": 1},
                {"id": "r2", "ts": "2024-01-02 09:00", "rank": 2},
            ],
        }
        db_path = Path(self._tmpdir.name) / "shadow.sqlite3"
        store = ArticleHistorySQLiteStore(db_path, normalize_article_url=normalize_article_url)
        store.import_history_sources({
            "task-a": [
                {"id": "r1", "ts": "2024-01-01 09:00", "rank": 99},
            ],
            "stale": [
                {"id": "old", "ts": "2023-01-01 00:00"},
            ],
        }, replace=True)

        result = compare_history_records(
            db_path,
            source=source,
            limit=10,
            sample_pages=1,
        )

        self.assertFalse(result["ok"])
        self.assertIn("storage_keys", result["keys"]["mismatches"])
        by_name = {item["name"]: item for item in result["queries"]}
        self.assertIn("count", by_name["task-a"]["mismatches"])
        self.assertIn("records", by_name["task-a"]["mismatches"])
        self.assertIn("json_records", by_name["task-a"]["windows"][0])
        self.assertIn("sqlite_records", by_name["task-a"]["windows"][0])
        self.assertIn("count", by_name["stale"]["mismatches"])

    def test_compare_history_task_reads_matches_primary_history_runtime_semantics(self) -> None:
        source = {
            "task-a": [
                {"ts": "2024-01-01 09:00", "task_id": "task-a", "task_name": "品牌A", "rank": 1},
                {"id": "r2", "ts": "2024-01-02 09:00", "task_id": "task-a", "task_name": "品牌A", "rank": 2},
            ],
            "品牌A": [
                {"id": "legacy-r1", "ts": "2024-01-03 09:00", "task_name": "品牌A", "rank": 3},
            ],
        }
        config = {"tasks": [{"task_id": "task-a", "name": "品牌A"}]}
        db_path = Path(self._tmpdir.name) / "shadow.sqlite3"

        result = compare_history_task_reads(
            db_path,
            config=config,
            source=source,
            limit=10,
            sample_pages=1,
            rebuild=True,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["query_count"], 1)
        query = result["queries"][0]
        self.assertEqual(query["json"]["targets"], ["task-a"])
        self.assertEqual(query["sqlite"]["targets"], ["task-a"])
        self.assertEqual(query["json"]["count"], 2)

    def test_compare_history_task_reads_reports_target_mismatch(self) -> None:
        source = {
            "task-a": [
                {"id": "r1", "ts": "2024-01-01 09:00", "task_id": "task-a", "task_name": "品牌A", "rank": 1},
            ],
        }
        config = {"tasks": [{"task_id": "task-a", "name": "品牌A"}]}
        db_path = Path(self._tmpdir.name) / "shadow.sqlite3"
        store = ArticleHistorySQLiteStore(db_path, normalize_article_url=normalize_article_url)
        store.import_history_sources({
            "品牌A": [
                {"id": "r1", "ts": "2024-01-01 09:00", "task_id": "task-a", "task_name": "品牌A", "rank": 1},
            ],
        }, replace=True)

        result = compare_history_task_reads(
            db_path,
            config=config,
            source=source,
            limit=10,
            sample_pages=1,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_count"], 1)
        self.assertIn("targets", result["queries"][0]["mismatches"])

    def test_compare_history_derived_views_match_runtime_semantics(self) -> None:
        source = {
            "task-a": [
                {
                    "id": "late-first",
                    "ts": "2024-01-03 09:00",
                    "task_name": "原始首条",
                    "rank": "99",
                    "success": True,
                    "review_status": "pending",
                },
                {
                    "id": "early-second",
                    "ts": "2024-01-01 09:00",
                    "task_name": "时间更早",
                    "rank": 1,
                    "success": True,
                    "review_status": "pending",
                },
            ],
            "task-b": [
                {
                    "id": "other",
                    "ts": "2024-01-02 09:00",
                    "rank": 2,
                    "success": True,
                },
            ],
        }
        db_path = Path(self._tmpdir.name) / "shadow.sqlite3"

        result = compare_history_derived_views(
            db_path,
            source=source,
            pending_limit=10,
            rebuild=True,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["view_count"], 2)
        by_name = {item["name"]: item for item in result["views"]}
        self.assertEqual(by_name["task_names"]["json"]["names"], ["原始首条", "task-b"])
        self.assertEqual(by_name["pending_reviews"]["json"]["ids"], ["late-first", "other", "early-second"])

    def test_compare_history_derived_views_reports_pending_mismatch(self) -> None:
        source = {
            "task-a": [
                {
                    "id": "pending",
                    "ts": "2024-01-01 09:00",
                    "task_name": "品牌A",
                    "rank": 1,
                    "success": True,
                    "review_status": "pending",
                },
            ],
        }
        db_path = Path(self._tmpdir.name) / "shadow.sqlite3"
        ArticleHistorySQLiteStore(db_path, normalize_article_url=normalize_article_url).import_history_sources(
            {
                "task-a": [
                    {
                        "id": "approved",
                        "ts": "2024-01-01 09:00",
                        "task_name": "品牌A",
                        "rank": 1,
                        "success": True,
                        "review_status": "approved",
                    },
                ],
            },
            replace=True,
        )

        result = compare_history_derived_views(
            db_path,
            source=source,
            pending_limit=10,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_count"], 1)
        by_name = {item["name"]: item for item in result["views"]}
        self.assertIn("records", by_name["pending_reviews"]["mismatches"])
        self.assertIn("json_records", by_name["pending_reviews"])
        self.assertIn("sqlite_records", by_name["pending_reviews"])

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
