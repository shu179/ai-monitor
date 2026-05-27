from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.sync_v2_service import build_state_delta  # noqa: E402


class StateDeltaPayloadTests(unittest.TestCase):
    @patch("app.services.sync_v2_service._stale_cursor_streams", return_value=[])
    @patch("app.services.sync_v2_service.list_workspace_changes")
    @patch("app.services.sync_v2_service.compact_change_snapshot", return_value={"runs": 1})
    def test_state_delta_includes_run_record_entity(self, _snapshot, list_changes, _stale) -> None:
        list_changes.return_value = [
            {
                "stream": "runs",
                "seq": 1,
                "kind": "run.record",
                "ref_id": "run-001",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ]
        db = MagicMock()
        db.scalar.return_value = SimpleNamespace(
            id=11,
            workspace_id=7,
            task_id=3,
            executed_by=9,
            platform="douyin",
            keyword="coffee",
            brand="Acme",
            mode="browser",
            result_json={"rank": 1},
            idempotency_key="run-001",
            executed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            created_at=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
        )

        result = build_state_delta(db, _user(), cursors={"runs": 0})

        entity = result["changes"][0]["entity"]
        self.assertEqual(entity["type"], "run_record")
        self.assertEqual(entity["id"], 11)
        self.assertEqual(entity["result_json"], {"rank": 1})
        self.assertEqual(result["object_refs"], [])

    @patch("app.services.sync_v2_service._stale_cursor_streams", return_value=[])
    @patch("app.services.sync_v2_service.list_workspace_changes")
    @patch("app.services.sync_v2_service.compact_change_snapshot", return_value={"articles": 1})
    def test_state_delta_includes_article_entity_and_object_ref(self, _snapshot, list_changes, _stale) -> None:
        list_changes.return_value = [
            {
                "stream": "articles",
                "seq": 1,
                "kind": "article.upsert",
                "ref_id": "article-001",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ]
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        sync_event = SimpleNamespace(
            id=1,
            workspace_id=7,
            idempotency_key="article-001",
            payload_json={"url_hash": "a" * 64, "canonical_url": "https://example.test/a"},
            created_at=now,
        )
        article = SimpleNamespace(
            id=21,
            workspace_id=7,
            canonical_url="https://example.test/a",
            url_hash="a" * 64,
            title="Article A",
            source="Example",
            media_type="authority",
            published_at=now,
            payload_json={"excerpt": "short"},
            created_at=now,
            updated_at=now,
        )
        link = SimpleNamespace(
            task_id=5,
            source="local_rule",
            confidence=91,
            reason_json={"reasons": ["brand"]},
            created_at=now,
        )
        version = SimpleNamespace(
            version=2,
            inline_text=None,
            content_object_id="object-001",
            content_sha256="b" * 64,
            size_bytes=120_000,
            created_at=now,
        )
        manifest = SimpleNamespace(
            id="object-001",
            sha256="b" * 64,
            size_bytes=120_000,
            storage_size_bytes=20_000,
            content_type="text/plain",
            compression="zstd",
            storage_key="7/bb/bb/" + "b" * 64,
        )
        db = MagicMock()
        db.scalar.side_effect = [sync_event, article, version, manifest]
        db.scalars.return_value = [link]

        result = build_state_delta(db, _user(), cursors={"articles": 0})

        entity = result["changes"][0]["entity"]
        self.assertEqual(entity["type"], "article")
        self.assertEqual(entity["id"], 21)
        self.assertEqual(entity["task_links"][0]["task_id"], 5)
        self.assertEqual(entity["content_ref"]["kind"], "object")
        self.assertEqual(entity["content_ref"]["object_id"], "object-001")
        self.assertEqual(result["object_refs"][0]["object_id"], "object-001")
        self.assertEqual(result["object_refs"][0]["compression"], "zstd")

    @patch("app.services.sync_v2_service._stale_cursor_streams", return_value=[])
    @patch("app.services.sync_v2_service.list_workspace_changes")
    @patch("app.services.sync_v2_service.compact_change_snapshot", return_value={"profile": 1})
    def test_state_delta_includes_profile_entity(self, _snapshot, list_changes, _stale) -> None:
        list_changes.return_value = [
            {
                "stream": "profile",
                "seq": 1,
                "kind": "profile.update",
                "ref_id": "profile-001",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ]
        user = _user(display_name="Shu", birthday=date(1990, 1, 2))

        result = build_state_delta(MagicMock(), user, cursors={"profile": 0})

        entity = result["changes"][0]["entity"]
        self.assertEqual(entity["type"], "profile")
        self.assertEqual(entity["display_name"], "Shu")
        self.assertEqual(entity["birthday"], "1990-01-02")

    @patch("app.services.sync_v2_service._stale_cursor_streams", return_value=[])
    @patch("app.services.sync_v2_service.list_workspace_changes")
    @patch("app.services.sync_v2_service.compact_change_snapshot", return_value={"runs": 1})
    def test_missing_entity_keeps_base_change(self, _snapshot, list_changes, _stale) -> None:
        list_changes.return_value = [
            {
                "stream": "runs",
                "seq": 1,
                "kind": "run.record",
                "ref_id": "missing",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ]
        db = MagicMock()
        db.scalar.return_value = None

        result = build_state_delta(db, _user(), cursors={"runs": 0})

        self.assertEqual(result["changes"][0]["ref_id"], "missing")
        self.assertNotIn("entity", result["changes"][0])


def _user(**overrides):
    base = {
        "id": 9,
        "workspace_id": 7,
        "username": "shu",
        "role": "admin",
        "display_name": None,
        "email": "shu@example.test",
        "email_verified": True,
        "avatar": None,
        "birthday": None,
        "hire_date": None,
        "view_all_tasks": True,
        "enabled": True,
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return SimpleNamespace(**base)
