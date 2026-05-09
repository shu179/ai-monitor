from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import UserRole  # noqa: E402
from app.schemas import SyncEventIn  # noqa: E402
from app.services.sync_service import (  # noqa: E402
    _materialize_article_task_links,
    _sanitize_sync_event_payload,
)
from app.sync_event_types import EVENT_ARTICLE_TASK_LINKS, EVENT_ARTICLE_UPSERT  # noqa: E402


class ArticleSyncServiceTests(unittest.TestCase):
    def test_sanitize_article_upsert_keeps_only_compact_metadata(self) -> None:
        clean = _sanitize_sync_event_payload(
            EVENT_ARTICLE_UPSERT,
            {
                "canonical_url": "https://example.com/a",
                "title": "T" * 600,
                "source": "媒体" * 80,
                "media_type": "self-media",
                "published_at": "2026-05-06T08:00:00+08:00-extra",
                "payload": {
                    "local_article_id": "article-1",
                    "excerpt": "摘要" * 400,
                    "answer_text": "不应保存",
                },
            },
        )

        self.assertEqual(clean["url_hash"], "2dce0a4c50441bfccfa9caf4b58c3cba6e06c420505dd829f0436de1aa44baac")
        self.assertEqual(len(clean["title"]), 512)
        self.assertEqual(len(clean["source"]), 128)
        self.assertEqual(clean["media_type"], "selfmedia")
        self.assertLessEqual(len(clean["published_at"]), 64)
        self.assertLessEqual(len(clean["payload"]["excerpt"]), 500)
        self.assertNotIn("answer_text", clean["payload"])

    def test_sanitize_article_task_links_normalizes_scope(self) -> None:
        clean = _sanitize_sync_event_payload(
            EVENT_ARTICLE_TASK_LINKS,
            {
                "url": "https://example.com/a",
                "task_ids": [42, "42", "bad", 43],
                "confidence": 120,
                "partial": True,
                "unresolved_task_names": ["品牌A", "品牌A"],
            },
        )

        self.assertEqual(clean["task_ids"], [42, 43])
        self.assertEqual(clean["confidence"], 100)
        self.assertTrue(clean["partial"])
        self.assertEqual(clean["unresolved_task_names"], ["品牌A"])

    def test_materialize_article_task_links_writes_only_operable_tasks(self) -> None:
        db = Mock()
        user = SimpleNamespace(id=2, workspace_id=1, role=UserRole.operator)
        event = SyncEventIn(
            event_type=EVENT_ARTICLE_TASK_LINKS,
            idempotency_key="article-links-test",
            payload={
                "url": "https://example.com/a",
                "task_ids": [42, 43],
                "reason_json": {"42": ["标题匹配"]},
                "replace": True,
            },
        )

        with (
            patch("app.services.sync_service._ensure_cloud_article", return_value=10),
            patch("app.services.sync_service._operable_task_ids_for_user", return_value=[42, 43]),
            patch("app.services.sync_service.can_operate_task", side_effect=lambda _db, _user, task_id, **_kw: task_id == 42),
            patch("app.services.sync_service.sync_article_classification_state") as sync_classification,
        ):
            _materialize_article_task_links(db, user, event)  # type: ignore[arg-type]

        self.assertEqual(db.execute.call_count, 3)
        sync_classification.assert_called_once()


if __name__ == "__main__":
    unittest.main()
