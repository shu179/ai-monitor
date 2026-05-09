from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.article_store as article_store
from core.article_sqlite_store import ArticleSQLiteStore


def _normalize_article_for_test(item: dict) -> dict:
    return article_store._normalize_article_entry(item)[0]  # noqa: SLF001


class ArticleSQLiteStoreTests(unittest.TestCase):
    def _store(self, db_path: Path) -> ArticleSQLiteStore:
        return ArticleSQLiteStore(
            db_path,
            normalize_article_url=article_store.normalize_article_url,
            normalize_article_entry=_normalize_article_for_test,
            now_text=lambda: "2026-05-09 10:00",
        )

    def test_import_page_meta_and_task_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(Path(tmpdir) / "article_store.sqlite3")

            result = store.import_from_articles(
                [
                    {
                        "id": "article-a",
                        "url": "https://www.example.com/a?utm_source=x",
                        "title": "A",
                        "media_name": "Example",
                        "media_type": "authority",
                        "published_at": "2026-05-09",
                        "ts": "2026-05-09 08:00",
                        "matched_tasks": ["Brand A"],
                    },
                    {
                        "id": "article-b",
                        "url": "https://mp.weixin.qq.com/s/article-b",
                        "title": "B",
                        "media_name": "Account",
                        "media_type": "selfmedia",
                        "published_at": "2026-05-08",
                        "ts": "2026-05-08 08:00",
                        "matched_tasks": ["Brand B"],
                        "referenced_tasks": ["Brand A"],
                    },
                ],
                replace=True,
            )

            page = store.get_article_page(task_name="Brand A", today="2026-05-09")
            media_page = store.get_article_page(media_type="selfmedia", today="2026-05-09")
            by_url = store.get_article_by_url("https://example.com/a")
            counts = store.get_article_task_counts()
            store.set_meta("article_source_signature", "sig-1")

            self.assertEqual(result, {"created": 2, "updated": 0, "skipped": 0})
            self.assertEqual(page["total"], 1)
            self.assertEqual(page["today_total"], 1)
            self.assertEqual([item["id"] for item in page["items"]], ["article-a"])
            self.assertEqual([item["id"] for item in media_page["items"]], ["article-b"])
            self.assertEqual(by_url["id"], "article-a")
            self.assertEqual(counts, {"Brand A": 1, "Brand B": 1})
            self.assertEqual(store.get_meta("article_source_signature"), "sig-1")
            self.assertTrue(ArticleSQLiteStore.validate_readiness(store.db_path)["ready"])

    def test_crud_preserves_article_store_upsert_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(Path(tmpdir) / "article_store.sqlite3")

            first = store.add_article(
                {
                    "id": "article-a",
                    "url": "https://www.example.com/a?utm_source=x",
                    "title": "Original",
                    "published_at": "2026-05-08",
                    "ts": "2026-05-08 08:00",
                    "imported_at": "2026-05-08 08:01",
                    "matched_tasks": ["Brand A"],
                }
            )
            duplicate = store.add_article(
                {
                    "id": "article-duplicate",
                    "url": "https://example.com/a",
                    "title": "Duplicate",
                }
            )
            upserts = store.bulk_upsert_articles(
                [
                    {
                        "id": "incoming-by-url",
                        "url": "https://example.com/a",
                        "title": "Updated by URL",
                        "published_at": "2026-05-10",
                        "matched_tasks": ["Brand B"],
                    },
                    {
                        "id": "article-b",
                        "url": "https://example.com/b",
                        "title": "B",
                        "published_at": "2026-05-09",
                        "ts": "2026-05-09 09:00",
                        "matched_tasks": ["Brand A"],
                    },
                ]
            )
            updated = store.update_article("article-b", {"title": "B final", "matched_tasks": ["Brand C"]})
            deleted = store.delete_article("article-a")

            self.assertEqual(first["id"], "article-a")
            self.assertEqual(duplicate["id"], "article-a")
            self.assertEqual(upserts[0]["id"], "article-a")
            self.assertEqual(upserts[0]["title"], "Updated by URL")
            self.assertEqual(upserts[0]["ts"], "2026-05-08 08:00")
            self.assertEqual(updated["title"], "B final")
            self.assertEqual(deleted["id"], "article-a")
            self.assertEqual([item["id"] for item in store.list_articles()], ["article-b"])
            self.assertEqual(store.get_article_page(task_name="Brand C")["total"], 1)
            self.assertIsNone(store.get_article_by_url("https://example.com/a"))


class ArticleStoreSQLiteParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_env = {
            article_store.ARTICLE_STORE_BACKEND_ENV: os.environ.get(article_store.ARTICLE_STORE_BACKEND_ENV),
            article_store.ARTICLE_SHADOW_WRITE_ENV: os.environ.get(article_store.ARTICLE_SHADOW_WRITE_ENV),
        }
        self._original_paths = {
            "ARTICLES_FILE": article_store.ARTICLES_FILE,
            "DOMAIN_OVERRIDES_FILE": article_store.DOMAIN_OVERRIDES_FILE,
            "DOMAIN_MEDIA_NAMES_FILE": article_store.DOMAIN_MEDIA_NAMES_FILE,
            "EXCLUDED_ARTICLE_URLS_FILE": article_store.EXCLUDED_ARTICLE_URLS_FILE,
            "ARTICLE_SHADOW_DB_FILE": article_store.ARTICLE_SHADOW_DB_FILE,
            "ARTICLE_STORE_DB_FILE": article_store.ARTICLE_STORE_DB_FILE,
        }
        os.environ[article_store.ARTICLE_SHADOW_WRITE_ENV] = "0"

    def tearDown(self) -> None:
        for key, value in self._original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        article_store.ARTICLES_FILE = self._original_paths["ARTICLES_FILE"]
        article_store.DOMAIN_OVERRIDES_FILE = self._original_paths["DOMAIN_OVERRIDES_FILE"]
        article_store.DOMAIN_MEDIA_NAMES_FILE = self._original_paths["DOMAIN_MEDIA_NAMES_FILE"]
        article_store.EXCLUDED_ARTICLE_URLS_FILE = self._original_paths["EXCLUDED_ARTICLE_URLS_FILE"]
        article_store.ARTICLE_SHADOW_DB_FILE = self._original_paths["ARTICLE_SHADOW_DB_FILE"]
        article_store.ARTICLE_STORE_DB_FILE = self._original_paths["ARTICLE_STORE_DB_FILE"]
        article_store._small_document_cache.clear()  # noqa: SLF001
        self._tmpdir.cleanup()

    def test_public_crud_parity_between_json_and_sqlite_backend(self) -> None:
        json_result = self._run_public_crud_scenario("json")
        sqlite_result = self._run_public_crud_scenario("sqlite")

        self.assertEqual(sqlite_result, json_result)

    def test_sqlite_backend_is_env_gated_and_leaves_articles_json_unchanged(self) -> None:
        self._configure_paths("sqlite-gated")
        os.environ[article_store.ARTICLE_STORE_BACKEND_ENV] = "sqlite"

        article_store.add_article(
            {
                "id": "article-a",
                "url": "https://example.com/a",
                "title": "A",
                "ts": "2026-05-09 09:00",
                "imported_at": "2026-05-09 09:01",
            }
        )

        self.assertEqual(article_store.ARTICLES_FILE.read_text(encoding="utf-8"), "[]")
        self.assertTrue(article_store.ARTICLE_STORE_DB_FILE.exists())
        self.assertEqual([item["id"] for item in article_store.get_articles()], ["article-a"])

    def _configure_paths(self, label: str) -> None:
        logs_dir = Path(self._tmpdir.name) / label / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        article_store.ARTICLES_FILE = logs_dir / "articles.json"
        article_store.DOMAIN_OVERRIDES_FILE = logs_dir / "domain_overrides.json"
        article_store.DOMAIN_MEDIA_NAMES_FILE = logs_dir / "domain_media_names.json"
        article_store.EXCLUDED_ARTICLE_URLS_FILE = logs_dir / "excluded_article_urls.json"
        article_store.ARTICLE_SHADOW_DB_FILE = logs_dir / "article_history_shadow.sqlite3"
        article_store.ARTICLE_STORE_DB_FILE = logs_dir / "article_store.sqlite3"
        article_store.ARTICLES_FILE.write_text("[]", encoding="utf-8")
        article_store._small_document_cache.clear()  # noqa: SLF001

    def _run_public_crud_scenario(self, backend: str) -> dict:
        self._configure_paths(backend)
        if backend == "sqlite":
            os.environ[article_store.ARTICLE_STORE_BACKEND_ENV] = "sqlite"
        else:
            os.environ.pop(article_store.ARTICLE_STORE_BACKEND_ENV, None)

        with (
            patch.object(article_store, "_article_now_minute_text", return_value="2026-05-09 10:00"),
            patch.object(article_store, "_article_now_second_text", return_value="2026-05-09 10:00:30"),
        ):
            article_store.exclude_article_url("https://example.com/news/a?b=2", title="Deleted")
            first = article_store.add_article(
                {
                    "id": "article-a",
                    "url": "https://www.example.com/news/a/?utm_source=feed&b=2",
                    "title": "A original",
                    "media_name": "Example",
                    "media_type": "selfmedia",
                    "published_at": "2026-05-08",
                    "ts": "2026-05-08 09:00",
                    "imported_at": "2026-05-08 09:30",
                    "matched_tasks": ["Brand A"],
                    "match_reasons": {"Brand A": ["seed"]},
                }
            )
            excluded_after_add = article_store.is_article_url_excluded("https://example.com/news/a?b=2")
            duplicate = article_store.add_article(
                {
                    "id": "article-duplicate",
                    "url": "https://example.com/news/a?b=2",
                    "title": "A duplicate",
                }
            )
            bulk = article_store.bulk_upsert_articles(
                [
                    {
                        "id": "article-b",
                        "url": "https://example.com/news/b",
                        "title": "B original",
                        "media_name": "Example",
                        "media_type": "authority",
                        "published_at": "2026-05-09",
                        "ts": "2026-05-09 09:00",
                        "imported_at": "2026-05-09 09:05",
                        "matched_tasks": ["Brand B"],
                    },
                    {
                        "id": "incoming-by-url",
                        "url": "https://example.com/news/a?b=2",
                        "title": "A by URL",
                        "published_at": "2026-05-10",
                        "matched_tasks": ["Brand C"],
                    },
                ]
            )
            by_id = article_store.bulk_upsert_articles(
                [
                    {
                        "id": "article-b",
                        "url": "https://example.com/news/b",
                        "title": "B by ID",
                        "matched_tasks": ["Brand A", "Brand B"],
                        "ts": "",
                    }
                ]
            )
            updated = article_store.update_article(
                "article-b",
                {
                    "title": "B final",
                    "published_at": "2026-05-11",
                    "matched_tasks": ["Brand A"],
                    "match_reasons": {"Brand A": ["manual"]},
                },
            )
            found = article_store.find_article_by_url("https://www.example.com/news/a?utm_medium=x&b=2")
            before_delete = article_store.get_articles()
            deleted = article_store.delete_article("article-a")
            excluded_after_delete = article_store.get_excluded_article_urls()
            restored = article_store.restore_excluded_article_urls(["https://example.com/news/a?b=2"])
            after_delete = article_store.get_articles()

        return {
            "first": first,
            "excluded_after_add": excluded_after_add,
            "duplicate": duplicate,
            "bulk": bulk,
            "by_id": by_id,
            "updated": updated,
            "found": found,
            "before_delete": before_delete,
            "deleted": deleted,
            "excluded_after_delete_urls": [item["url"] for item in excluded_after_delete],
            "restore": {
                "ok": restored["ok"],
                "removed_count": restored["removed_count"],
                "urls": restored["urls"],
            },
            "after_delete": after_delete,
        }


if __name__ == "__main__":
    unittest.main()
