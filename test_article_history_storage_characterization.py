from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import core.article_store as article_store
import core.history as history
from core.article_history_sqlite_store import ArticleHistorySQLiteStore


class ArticleStorageCharacterizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        self._original_paths = {
            "ARTICLES_FILE": article_store.ARTICLES_FILE,
            "DOMAIN_OVERRIDES_FILE": article_store.DOMAIN_OVERRIDES_FILE,
            "DOMAIN_MEDIA_NAMES_FILE": article_store.DOMAIN_MEDIA_NAMES_FILE,
            "EXCLUDED_ARTICLE_URLS_FILE": article_store.EXCLUDED_ARTICLE_URLS_FILE,
        }
        article_store.ARTICLES_FILE = root / "logs" / "articles.json"
        article_store.DOMAIN_OVERRIDES_FILE = root / "logs" / "domain_overrides.json"
        article_store.DOMAIN_MEDIA_NAMES_FILE = root / "logs" / "domain_media_names.json"
        article_store.EXCLUDED_ARTICLE_URLS_FILE = root / "logs" / "excluded_article_urls.json"
        article_store.ARTICLES_FILE.parent.mkdir(parents=True, exist_ok=True)
        article_store.ARTICLES_FILE.write_text("[]", encoding="utf-8")

    def tearDown(self) -> None:
        article_store.ARTICLES_FILE = self._original_paths["ARTICLES_FILE"]
        article_store.DOMAIN_OVERRIDES_FILE = self._original_paths["DOMAIN_OVERRIDES_FILE"]
        article_store.DOMAIN_MEDIA_NAMES_FILE = self._original_paths["DOMAIN_MEDIA_NAMES_FILE"]
        article_store.EXCLUDED_ARTICLE_URLS_FILE = self._original_paths["EXCLUDED_ARTICLE_URLS_FILE"]
        self._tmpdir.cleanup()

    def test_bulk_upsert_updates_by_normalized_url_and_clears_exclusion(self) -> None:
        original = article_store.add_article({
            "id": "original-id",
            "url": "https://www.example.com/news/a/?utm_source=feed&b=2",
            "title": "旧标题",
            "media_name": "旧媒体",
            "published_at": "2024-01-01",
            "ts": "2024-01-01",
            "matched_tasks": ["品牌A"],
        })
        article_store.exclude_article_url("https://example.com/news/a?b=2", title="已删除")

        results = article_store.bulk_upsert_articles([{
            "id": "incoming-id",
            "url": "https://example.com/news/a?b=2",
            "title": "新标题",
            "media_name": "新媒体",
            "published_at": "2024-01-02",
            "matched_tasks": ["品牌B"],
        }])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], original["id"])
        self.assertEqual(results[0]["title"], "新标题")
        self.assertEqual(results[0]["published_at"], "2024-01-02")
        self.assertEqual(results[0]["ts"], "2024-01-01")
        self.assertEqual(results[0]["matched_tasks"], ["品牌B"])
        self.assertFalse(article_store.is_article_url_excluded("https://example.com/news/a?b=2"))
        self.assertEqual(len(article_store.get_articles()), 1)

    def test_get_articles_normalizes_legacy_json_entries_on_read(self) -> None:
        article_store.ARTICLES_FILE.write_text(
            json.dumps([
                "bad-row",
                {
                    "id": "legacy-id",
                    "url": "https://www.toutiao.com/article/123/?utm_campaign=x",
                    "title": "旧记录",
                    "media_name": "某账号",
                    "account_name": "某账号",
                    "account_platform_label": "头条号",
                    "matched_tasks": ["品牌A", "品牌A", "品牌B"],
                    "excluded_tasks": ["品牌B"],
                    "referenced_tasks": ["品牌A", "品牌B"],
                    "reference_hits": {
                        "品牌A": {
                            "count": "2",
                            "events": [
                                {"id": "event-1", "referenced_at": "2024-01-03 10:00"},
                                {"event_id": "event-1", "referenced_at": "2024-01-03 11:00"},
                            ],
                        },
                        "品牌B": {"count": 1},
                    },
                },
            ], ensure_ascii=False),
            encoding="utf-8",
        )

        articles = article_store.get_articles()

        self.assertEqual(len(articles), 1)
        article = articles[0]
        self.assertEqual(article["url"], "https://toutiao.com/article/123")
        self.assertEqual(article["media_name"], "今日头条")
        self.assertEqual(article["media_type"], "selfmedia")
        self.assertEqual(article["matched_tasks"], ["品牌A"])
        self.assertEqual(article["referenced_tasks"], ["品牌A"])
        self.assertEqual(set(article["reference_hits"].keys()), {"品牌A"})
        self.assertEqual(article["reference_hits"]["品牌A"]["count"], 2)
        self.assertEqual(len(article["reference_hits"]["品牌A"]["events"]), 1)

        persisted = json.loads(article_store.ARTICLES_FILE.read_text(encoding="utf-8"))
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0]["url"], "https://toutiao.com/article/123")

    def test_import_bundle_merge_dedupes_incoming_articles_before_updating_store(self) -> None:
        article_store.add_article({
            "id": "local-id",
            "url": "https://example.com/article/1",
            "title": "本地标题",
            "media_name": "本地媒体",
        })

        result = article_store.import_article_store_bundle({
            "articles": [
                {
                    "id": "remote-first",
                    "url": "https://www.example.com/article/1?utm_source=x",
                    "title": "远端标题",
                    "media_name": "远端媒体",
                },
                {
                    "id": "remote-duplicate",
                    "url": "https://example.com/article/1",
                    "title": "重复远端标题",
                },
                {
                    "id": "remote-new",
                    "url": "https://example.com/article/2",
                    "title": "新增标题",
                },
            ],
            "domain_overrides": {"Example.COM": "authority", "bad": "other"},
            "domain_media_names": {"Example.COM": "示例媒体"},
            "excluded_article_urls": {
                "https://www.example.com/deleted?utm_source=x": {"title": "删除项"},
            },
        })

        self.assertEqual(result["articles_created"], 1)
        self.assertEqual(result["articles_updated"], 1)
        self.assertEqual(result["articles_total"], 2)
        articles_by_url = {
            article_store.normalize_article_url(item["url"]): item
            for item in article_store.get_articles()
        }
        self.assertEqual(articles_by_url["https://example.com/article/1"]["id"], "remote-first")
        self.assertEqual(articles_by_url["https://example.com/article/1"]["title"], "远端标题")
        self.assertEqual(articles_by_url["https://example.com/article/2"]["id"], "remote-new")

        overrides = json.loads(article_store.DOMAIN_OVERRIDES_FILE.read_text(encoding="utf-8"))
        media_names = json.loads(article_store.DOMAIN_MEDIA_NAMES_FILE.read_text(encoding="utf-8"))
        excluded_urls = json.loads(article_store.EXCLUDED_ARTICLE_URLS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(overrides, {"example.com": "authority"})
        self.assertEqual(media_names, {"example.com": "示例媒体"})
        self.assertEqual(list(excluded_urls.keys()), ["https://example.com/deleted"])


class ArticleSQLiteStorageMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        self._original_storage_backend = os.environ.get(article_store.STORAGE_BACKEND_ENV)
        self._original_paths = {
            "DEFAULT_ARTICLES_FILE": article_store.DEFAULT_ARTICLES_FILE,
            "ARTICLES_FILE": article_store.ARTICLES_FILE,
            "DEFAULT_DOMAIN_OVERRIDES_FILE": article_store.DEFAULT_DOMAIN_OVERRIDES_FILE,
            "DOMAIN_OVERRIDES_FILE": article_store.DOMAIN_OVERRIDES_FILE,
            "DEFAULT_DOMAIN_MEDIA_NAMES_FILE": article_store.DEFAULT_DOMAIN_MEDIA_NAMES_FILE,
            "DOMAIN_MEDIA_NAMES_FILE": article_store.DOMAIN_MEDIA_NAMES_FILE,
            "DEFAULT_EXCLUDED_ARTICLE_URLS_FILE": article_store.DEFAULT_EXCLUDED_ARTICLE_URLS_FILE,
            "EXCLUDED_ARTICLE_URLS_FILE": article_store.EXCLUDED_ARTICLE_URLS_FILE,
            "LOCAL_STORE_DB_FILE": article_store.LOCAL_STORE_DB_FILE,
        }
        article_store.DEFAULT_ARTICLES_FILE = root / "logs" / "articles.json"
        article_store.ARTICLES_FILE = article_store.DEFAULT_ARTICLES_FILE
        article_store.DEFAULT_DOMAIN_OVERRIDES_FILE = root / "logs" / "domain_overrides.json"
        article_store.DOMAIN_OVERRIDES_FILE = article_store.DEFAULT_DOMAIN_OVERRIDES_FILE
        article_store.DEFAULT_DOMAIN_MEDIA_NAMES_FILE = root / "logs" / "domain_media_names.json"
        article_store.DOMAIN_MEDIA_NAMES_FILE = article_store.DEFAULT_DOMAIN_MEDIA_NAMES_FILE
        article_store.DEFAULT_EXCLUDED_ARTICLE_URLS_FILE = root / "logs" / "excluded_article_urls.json"
        article_store.EXCLUDED_ARTICLE_URLS_FILE = article_store.DEFAULT_EXCLUDED_ARTICLE_URLS_FILE
        article_store.LOCAL_STORE_DB_FILE = root / "logs" / "local_store.sqlite3"
        article_store.ARTICLES_FILE.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        if self._original_storage_backend is None:
            os.environ.pop(article_store.STORAGE_BACKEND_ENV, None)
        else:
            os.environ[article_store.STORAGE_BACKEND_ENV] = self._original_storage_backend
        article_store.DEFAULT_ARTICLES_FILE = self._original_paths["DEFAULT_ARTICLES_FILE"]
        article_store.ARTICLES_FILE = self._original_paths["ARTICLES_FILE"]
        article_store.DEFAULT_DOMAIN_OVERRIDES_FILE = self._original_paths["DEFAULT_DOMAIN_OVERRIDES_FILE"]
        article_store.DOMAIN_OVERRIDES_FILE = self._original_paths["DOMAIN_OVERRIDES_FILE"]
        article_store.DEFAULT_DOMAIN_MEDIA_NAMES_FILE = self._original_paths["DEFAULT_DOMAIN_MEDIA_NAMES_FILE"]
        article_store.DOMAIN_MEDIA_NAMES_FILE = self._original_paths["DOMAIN_MEDIA_NAMES_FILE"]
        article_store.DEFAULT_EXCLUDED_ARTICLE_URLS_FILE = self._original_paths["DEFAULT_EXCLUDED_ARTICLE_URLS_FILE"]
        article_store.EXCLUDED_ARTICLE_URLS_FILE = self._original_paths["EXCLUDED_ARTICLE_URLS_FILE"]
        article_store.LOCAL_STORE_DB_FILE = self._original_paths["LOCAL_STORE_DB_FILE"]
        self._tmpdir.cleanup()

    def test_default_article_store_keeps_json_backend(self) -> None:
        os.environ.pop(article_store.STORAGE_BACKEND_ENV, None)

        article_store.add_article({
            "id": "json-id",
            "url": "https://example.com/json",
            "title": "JSON 文章",
        })

        self.assertTrue(article_store.ARTICLES_FILE.exists())
        stored = json.loads(article_store.ARTICLES_FILE.read_text(encoding="utf-8"))
        self.assertEqual([item["id"] for item in stored], ["json-id"])
        self.assertFalse(article_store.LOCAL_STORE_DB_FILE.exists())

    def test_sqlite_opt_in_article_store_migrates_legacy_json(self) -> None:
        os.environ[article_store.STORAGE_BACKEND_ENV] = "sqlite"
        article_store.ARTICLES_FILE.write_text(
            json.dumps([
                {
                    "id": "legacy-id",
                    "url": "https://www.example.com/a/?utm_source=x",
                    "title": "旧文章",
                    "media_name": "示例媒体",
                },
            ], ensure_ascii=False),
            encoding="utf-8",
        )
        legacy_text = article_store.ARTICLES_FILE.read_text(encoding="utf-8")

        articles = article_store.get_articles()
        article_store.add_article({
            "id": "new-id",
            "url": "https://example.com/b",
            "title": "新文章",
        })

        self.assertEqual([item["id"] for item in articles], ["legacy-id"])
        self.assertEqual(article_store.ARTICLES_FILE.read_text(encoding="utf-8"), legacy_text)
        signature = article_store.get_articles_file_signature()
        self.assertIn("local_store.sqlite3::article_store/articles", signature[0])

        with sqlite3.connect(article_store.LOCAL_STORE_DB_FILE) as conn:
            payload = conn.execute(
                "SELECT value_json FROM json_documents WHERE key = ?",
                ("article_store/articles",),
            ).fetchone()
        stored = json.loads(payload[0])
        self.assertEqual([item["id"] for item in stored], ["legacy-id", "new-id"])
        self.assertEqual(stored[0]["url"], "https://example.com/a")


class HistoryStorageCharacterizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_history_dir = history.HISTORY_DIR
        self._original_max_records = history.MAX_RECORDS
        history.HISTORY_DIR = Path(self._tmpdir.name) / "logs" / "history"

    def tearDown(self) -> None:
        history.HISTORY_DIR = self._original_history_dir
        history.MAX_RECORDS = self._original_max_records
        self._tmpdir.cleanup()

    def test_record_with_task_id_writes_primary_and_legacy_history_files(self) -> None:
        entry = history.record(
            "重名任务",
            "doubao",
            "关键词",
            "品牌A",
            1,
            True,
            task_id="task-001",
        )

        primary_file = history._task_file("task-001")
        legacy_file = history._task_file("重名任务")
        self.assertTrue(primary_file.exists())
        self.assertTrue(legacy_file.exists())
        self.assertTrue(history._rates_file("重名任务").exists())

        primary_records = history.get_records("重名任务", task_id="task-001")
        legacy_records = history.get_records("重名任务")
        self.assertEqual([item["id"] for item in primary_records], [entry["id"]])
        self.assertEqual([item["id"] for item in legacy_records], [entry["id"]])
        self.assertEqual(primary_records[0]["task_id"], "task-001")
        self.assertEqual(primary_records[0]["task_name"], "重名任务")
        self.assertEqual(primary_records[0]["review_status"], "pending")

    def test_import_records_dedupes_sorts_and_trims_each_storage_target(self) -> None:
        history.MAX_RECORDS = 3
        entries = [
            {
                "id": "r3",
                "ts": "2024-01-03 09:00",
                "platform": "doubao",
                "keyword": "关键词",
                "brand": "品牌A",
                "rank": 99,
                "success": False,
            },
            {
                "id": "r1",
                "ts": "2024-01-01 09:00",
                "platform": "doubao",
                "keyword": "关键词",
                "brand": "品牌A",
                "rank": 1,
                "success": True,
            },
            {
                "id": "r2",
                "ts": "2024-01-02 09:00",
                "platform": "doubao",
                "keyword": "关键词",
                "brand": "品牌A",
                "rank": 1,
                "success": True,
            },
            {
                "id": "r2",
                "ts": "2024-01-02 10:00",
                "platform": "doubao",
                "keyword": "关键词",
                "brand": "品牌A",
                "rank": 1,
                "success": True,
            },
            {
                "id": "r4",
                "ts": "2024-01-04 09:00",
                "platform": "doubao",
                "keyword": "关键词",
                "brand": "品牌A",
                "rank": 1,
                "success": True,
            },
        ]

        imported = history.import_records("导入任务", entries, task_id="task-import")

        self.assertEqual(imported, 4)
        primary_records = json.loads(history._task_file("task-import").read_text(encoding="utf-8"))
        legacy_records = json.loads(history._task_file("导入任务").read_text(encoding="utf-8"))
        self.assertEqual([item["id"] for item in primary_records], ["r2", "r3", "r4"])
        self.assertEqual([item["id"] for item in legacy_records], ["r2", "r3", "r4"])
        self.assertTrue(all(item["task_id"] == "task-import" for item in primary_records))
        self.assertTrue(all(item["task_name"] == "导入任务" for item in legacy_records))

    def test_apply_review_with_task_id_updates_primary_and_legacy_history_files(self) -> None:
        entry = history.record(
            "复核任务",
            "doubao",
            "关键词",
            "品牌A",
            1,
            True,
            task_id="task-review",
        )

        changed = history.apply_review("复核任务", entry["id"], "approved", "ok", task_id="task-review")

        self.assertTrue(changed)
        primary_records = json.loads(history._task_file("task-review").read_text(encoding="utf-8"))
        legacy_records = json.loads(history._task_file("复核任务").read_text(encoding="utf-8"))
        self.assertEqual(primary_records[0]["review_status"], "approved")
        self.assertEqual(legacy_records[0]["review_status"], "approved")
        self.assertEqual(history.get_pending_reviews(), [])


class HistorySQLiteStorageMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        self._original_storage_backend = os.environ.get(history.STORAGE_BACKEND_ENV)
        self._original_shadow_write = os.environ.get(history.STRUCTURED_SHADOW_WRITE_ENV)
        self._original_read_backend = os.environ.get(history.STRUCTURED_READ_BACKEND_ENV)
        self._original_paths = {
            "DEFAULT_HISTORY_DIR": history.DEFAULT_HISTORY_DIR,
            "HISTORY_DIR": history.HISTORY_DIR,
            "LOCAL_STORE_DB_FILE": history.LOCAL_STORE_DB_FILE,
            "HISTORY_SHADOW_DB_FILE": history.HISTORY_SHADOW_DB_FILE,
        }
        history.DEFAULT_HISTORY_DIR = root / "logs" / "history"
        history.HISTORY_DIR = history.DEFAULT_HISTORY_DIR
        history.LOCAL_STORE_DB_FILE = root / "logs" / "local_store.sqlite3"
        history.HISTORY_SHADOW_DB_FILE = root / "logs" / "article_history_shadow.sqlite3"
        history.HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        if self._original_storage_backend is None:
            os.environ.pop(history.STORAGE_BACKEND_ENV, None)
        else:
            os.environ[history.STORAGE_BACKEND_ENV] = self._original_storage_backend
        if self._original_shadow_write is None:
            os.environ.pop(history.STRUCTURED_SHADOW_WRITE_ENV, None)
        else:
            os.environ[history.STRUCTURED_SHADOW_WRITE_ENV] = self._original_shadow_write
        if self._original_read_backend is None:
            os.environ.pop(history.STRUCTURED_READ_BACKEND_ENV, None)
        else:
            os.environ[history.STRUCTURED_READ_BACKEND_ENV] = self._original_read_backend
        history.DEFAULT_HISTORY_DIR = self._original_paths["DEFAULT_HISTORY_DIR"]
        history.HISTORY_DIR = self._original_paths["HISTORY_DIR"]
        history.LOCAL_STORE_DB_FILE = self._original_paths["LOCAL_STORE_DB_FILE"]
        history.HISTORY_SHADOW_DB_FILE = self._original_paths["HISTORY_SHADOW_DB_FILE"]
        self._tmpdir.cleanup()

    def test_default_history_keeps_json_backend(self) -> None:
        os.environ.pop(history.STORAGE_BACKEND_ENV, None)

        written = history.record(
            "默认历史",
            "doubao",
            "关键词",
            "品牌A",
            1,
            True,
            task_id="default-task",
        )

        self.assertTrue(history._task_file("default-task").exists())
        self.assertEqual(
            [item["id"] for item in history.get_records("默认历史", task_id="default-task")],
            [written["id"]],
        )
        self.assertFalse(history.LOCAL_STORE_DB_FILE.exists())
        self.assertFalse(history.HISTORY_SHADOW_DB_FILE.exists())

    def test_sqlite_opt_in_history_migrates_legacy_files(self) -> None:
        os.environ[history.STORAGE_BACKEND_ENV] = "sqlite"
        legacy_file = history._task_file("旧任务")
        legacy_file.write_text(
            json.dumps([
                {
                    "id": "legacy-record",
                    "ts": "2024-01-01 09:00",
                    "task_name": "旧任务",
                    "platform": "doubao",
                    "keyword": "关键词",
                    "brand": "品牌A",
                    "rank": 1,
                    "success": True,
                },
            ], ensure_ascii=False),
            encoding="utf-8",
        )

        self.assertEqual([item["id"] for item in history.get_records("旧任务")], ["legacy-record"])
        written = history.record(
            "新任务",
            "doubao",
            "关键词",
            "品牌B",
            1,
            True,
            task_id="task-new",
        )

        self.assertFalse(history._task_file("task-new").exists())
        self.assertEqual([item["id"] for item in history.get_records("新任务", task_id="task-new")], [written["id"]])
        self.assertIn("旧任务", history.get_all_task_names())
        self.assertIn("新任务", history.get_all_task_names())
        signature = history.get_records_file_signature("新任务", task_id="task-new")
        self.assertIn("local_store.sqlite3::history/task-new.json", signature[0][0])

        with sqlite3.connect(history.LOCAL_STORE_DB_FILE) as conn:
            keys = {
                row[0]
                for row in conn.execute("SELECT key FROM json_documents ORDER BY key").fetchall()
            }
        self.assertIn("history/旧任务.json", keys)
        self.assertIn("history/task-new.json", keys)
        self.assertIn("history/新任务.json", keys)

    def test_sqlite_opt_in_apply_review_updates_primary_and_legacy_documents(self) -> None:
        os.environ[history.STORAGE_BACKEND_ENV] = "sqlite"

        entry = history.record(
            "复核任务",
            "doubao",
            "关键词",
            "品牌A",
            1,
            True,
            task_id="task-review",
        )
        changed = history.apply_review("复核任务", entry["id"], "approved", "ok", task_id="task-review")

        self.assertTrue(changed)
        self.assertFalse(history._task_file("task-review").exists())
        self.assertEqual(history.get_pending_reviews(), [])
        with sqlite3.connect(history.LOCAL_STORE_DB_FILE) as conn:
            rows = dict(
                conn.execute(
                    "SELECT key, value_json FROM json_documents WHERE key IN (?, ?)",
                    ("history/task-review.json", "history/复核任务.json"),
                ).fetchall()
            )
        self.assertEqual(sorted(rows.keys()), ["history/task-review.json", "history/复核任务.json"])
        self.assertEqual(json.loads(rows["history/task-review.json"])[0]["review_status"], "approved")
        self.assertEqual(json.loads(rows["history/复核任务.json"])[0]["review_status"], "approved")

    def test_structured_shadow_writes_follow_record_and_review_writes_when_enabled(self) -> None:
        os.environ.pop(history.STORAGE_BACKEND_ENV, None)
        os.environ[history.STRUCTURED_SHADOW_WRITE_ENV] = "1"

        entry = history.record(
            "影子任务",
            "doubao",
            "关键词",
            "品牌A",
            1,
            True,
            task_id="task_write_path",
        )
        changed = history.apply_review("影子任务", entry["id"], "approved", "ok", task_id="task_write_path")

        self.assertTrue(changed)
        self.assertTrue(history._task_file("task_write_path").exists())
        store = ArticleHistorySQLiteStore(history.HISTORY_SHADOW_DB_FILE)
        self.assertEqual([item["id"] for item in store.get_history_records("task_write_path")], [entry["id"]])
        self.assertEqual([item["id"] for item in store.get_history_records("影子任务")], [entry["id"]])
        primary = store.get_history_records("task_write_path")[0]
        legacy = store.get_history_records("影子任务")[0]
        self.assertEqual(primary["review_status"], "approved")
        self.assertEqual(legacy["review_status"], "approved")
        self.assertEqual(primary["review_note"], "ok")
        self.assertEqual(store.get_pending_reviews(), [])

    def test_structured_shadow_writes_follow_import_trimmed_records_when_enabled(self) -> None:
        os.environ.pop(history.STORAGE_BACKEND_ENV, None)
        os.environ[history.STRUCTURED_SHADOW_WRITE_ENV] = "1"
        history.MAX_RECORDS = 2

        imported = history.import_records(
            "导入影子",
            [
                {"id": "r3", "ts": "2024-01-03 09:00", "rank": 1, "success": True},
                {"id": "r1", "ts": "2024-01-01 09:00", "rank": 1, "success": True},
                {"id": "r2", "ts": "2024-01-02 09:00", "rank": 1, "success": True},
            ],
            task_id="task_import_write_path",
        )

        self.assertEqual(imported, 3)
        store = ArticleHistorySQLiteStore(history.HISTORY_SHADOW_DB_FILE)
        self.assertEqual(
            [item["id"] for item in store.get_history_records("task_import_write_path")],
            ["r2", "r3"],
        )
        self.assertEqual(
            [item["id"] for item in store.get_history_records("导入影子")],
            ["r2", "r3"],
        )

    def test_structured_read_backend_reads_records_and_derived_views_when_enabled(self) -> None:
        os.environ.pop(history.STORAGE_BACKEND_ENV, None)
        os.environ[history.STRUCTURED_READ_BACKEND_ENV] = "sqlite_shadow"
        store = ArticleHistorySQLiteStore(history.HISTORY_SHADOW_DB_FILE)
        pending = {
            "id": "pending-1",
            "ts": "2024-01-02 09:00",
            "task_id": "task_read_path",
            "task_name": "读取影子",
            "platform": "doubao",
            "keyword": "关键词",
            "brand": "品牌A",
            "rank": 1,
            "success": True,
            "review_status": "pending",
        }
        legacy = {
            "id": "legacy-1",
            "ts": "2024-01-01 09:00",
            "task_name": "读取影子",
            "platform": "doubao",
            "keyword": "旧关键词",
            "brand": "品牌A",
            "rank": 1,
            "success": True,
            "review_status": "approved",
        }
        store.import_history_sources(
            {
                "task_read_path": [pending],
                "读取影子": [legacy],
            },
            replace=True,
        )

        self.assertFalse(history._task_file("task_read_path").exists())
        self.assertEqual(
            [item["id"] for item in history.get_records("读取影子", task_id="task_read_path")],
            ["pending-1"],
        )
        self.assertEqual([item["id"] for item in history.get_records("读取影子")], ["legacy-1"])
        self.assertIn("读取影子", history.get_all_task_names())
        self.assertEqual([item["id"] for item in history.get_pending_reviews(limit=10)], ["pending-1"])
        signature = history.get_records_file_signature("读取影子", task_id="task_read_path")
        self.assertIn("article_history_shadow.sqlite3::history_records/task_read_path", signature[0][0])

    def test_structured_read_backend_falls_back_to_json_when_shadow_db_missing(self) -> None:
        os.environ.pop(history.STORAGE_BACKEND_ENV, None)
        os.environ[history.STRUCTURED_READ_BACKEND_ENV] = "sqlite_shadow"
        legacy_file = history._task_file("json-fallback")
        legacy_file.write_text(
            json.dumps([
                {
                    "id": "json-1",
                    "ts": "2024-01-01 09:00",
                    "task_name": "JSON 回退",
                    "rank": 1,
                    "success": True,
                },
            ], ensure_ascii=False),
            encoding="utf-8",
        )

        self.assertFalse(history.HISTORY_SHADOW_DB_FILE.exists())
        self.assertEqual([item["id"] for item in history.get_records("json-fallback")], ["json-1"])
        self.assertIn("JSON 回退", history.get_all_task_names())


if __name__ == "__main__":
    unittest.main()
