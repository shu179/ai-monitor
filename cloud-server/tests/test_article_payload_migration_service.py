from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("SURFACED_CLOUD_SECRET_KEY", "test-secret-key-for-article-migration")

from app.models import Article, ArticleVersion, ObjectManifest  # noqa: E402
from app.services.article_payload_migration_service import (  # noqa: E402
    ARTICLE_TEXT_COMPRESSION,
    build_article_payload_migration_report,
    extract_article_payload_text,
    migrate_article_payloads_once,
)


class ArticlePayloadMigrationServiceTests(unittest.TestCase):
    def test_extract_article_payload_text_prefers_known_fields(self) -> None:
        extracted = extract_article_payload_text({"excerpt": "short", "body": "full body"})

        self.assertIsNotNone(extracted)
        self.assertEqual(extracted.text, "full body")
        self.assertEqual(extracted.source_key, "body")

    def test_migrates_small_text_inline(self) -> None:
        article = _article(1, {"body": "hello article"})
        db = _db_with_articles([article])

        stats = migrate_article_payloads_once(db, inline_threshold_bytes=1024)

        self.assertEqual(stats["completed"], 1)
        added_versions = [item for item in db.add.call_args_list if isinstance(item.args[0], ArticleVersion)]
        self.assertEqual(len(added_versions), 1)
        version = added_versions[0].args[0]
        self.assertEqual(version.inline_text, "hello article")
        self.assertIsNone(version.content_object_id)
        self.assertEqual(db.commit.call_count, 1)

    def test_migrates_large_text_to_local_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            text = "x" * 5000
            article = _article(2, {"payload": {"content": text}})
            db = _db_with_articles([article])

            stats = migrate_article_payloads_once(db, inline_threshold_bytes=10, settings=_settings(tmp))

            self.assertEqual(stats["completed"], 1)
            manifests = [item.args[0] for item in db.add.call_args_list if isinstance(item.args[0], ObjectManifest)]
            versions = [item.args[0] for item in db.add.call_args_list if isinstance(item.args[0], ArticleVersion)]
            self.assertEqual(len(manifests), 1)
            self.assertEqual(len(versions), 1)
            self.assertEqual(manifests[0].compression, ARTICLE_TEXT_COMPRESSION)
            self.assertIsNotNone(versions[0].content_object_id)
            object_path = Path(tmp) / manifests[0].storage_key
            self.assertTrue(object_path.exists())

    def test_skips_article_without_text(self) -> None:
        article = _article(3, {"excerpt": "not enough for full text"})
        db = _db_with_articles([article])

        stats = migrate_article_payloads_once(db)

        self.assertEqual(stats["skipped"], 1)
        self.assertFalse(any(isinstance(item.args[0], ArticleVersion) for item in db.add.call_args_list))

    def test_report_counts_versions_and_object_backed_versions(self) -> None:
        db = MagicMock()
        db.execute.return_value.all.return_value = [SimpleNamespace(status="completed", count=2)]
        db.scalar.side_effect = [3, 1]

        report = build_article_payload_migration_report(db)

        self.assertEqual(report["status_counts"], {"completed": 2})
        self.assertEqual(report["article_versions"], 3)
        self.assertEqual(report["object_backed_versions"], 1)


def _article(article_id: int, payload: dict) -> Article:
    return Article(id=article_id, workspace_id=7, canonical_url=f"https://example.com/{article_id}", url_hash=f"h{article_id}", payload_json=payload)


def _db_with_articles(articles: list[Article]):
    db = MagicMock()
    db.scalars.return_value = articles
    db.scalar.return_value = None
    return db


def _settings(path: str):
    return SimpleNamespace(
        object_storage_local_dir=path,
        object_storage_total_quota_bytes=10 * 1024 * 1024 * 1024,
        object_storage_workspace_quota_bytes=5 * 1024 * 1024 * 1024,
        object_storage_max_file_bytes=512 * 1024 * 1024,
        object_storage_min_free_bytes=1,
    )


if __name__ == "__main__":
    unittest.main()
