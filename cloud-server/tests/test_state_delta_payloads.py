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

from app.services.sync_v2_service import build_state_delta, make_reset_token, parse_bootstrap_cursor  # noqa: E402


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

    @patch("app.services.sync_v2_service._stale_cursor_streams", return_value=[])
    @patch("app.services.sync_v2_service.list_workspace_changes")
    @patch("app.services.sync_v2_service.compact_change_snapshot", return_value={"references": 1})
    def test_state_delta_includes_reference_entity(self, _snapshot, list_changes, _stale) -> None:
        list_changes.return_value = [
            {
                "stream": "references",
                "seq": 1,
                "kind": "article.reference",
                "ref_id": "ref-001",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ]
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        db = MagicMock()
        db.scalar.return_value = SimpleNamespace(
            id=31,
            workspace_id=7,
            task_id=5,
            article_id=21,
            normalized_url="https://example.test/a",
            url_hash="a" * 64,
            platform="douyin",
            record_day="2026-01-01",
            source_record_key="record-1",
            idempotency_key="ref-001",
            event_json={"rank": 1},
            created_at=now,
        )

        result = build_state_delta(db, _user(), cursors={"references": 0})

        entity = result["changes"][0]["entity"]
        self.assertEqual(entity["type"], "article_reference")
        self.assertEqual(entity["id"], 31)
        self.assertEqual(entity["event_json"], {"rank": 1})

    @patch("app.services.sync_v2_service._stale_cursor_streams", return_value=[])
    @patch("app.services.sync_v2_service.list_workspace_changes")
    @patch("app.services.sync_v2_service.compact_change_snapshot", return_value={"agent_status": 1})
    def test_state_delta_includes_agent_status_without_executable_payload(self, _snapshot, list_changes, _stale) -> None:
        list_changes.return_value = [
            {
                "stream": "agent_status",
                "seq": 1,
                "kind": "agent.command.completed",
                "ref_id": "command-001",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ]
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        command = SimpleNamespace(
            id="command-001",
            workspace_id=7,
            target_device_id="mac-1",
            target_role=None,
            status="completed",
            visibility_until=None,
            idempotency_key="agent-key-1",
            payload_json={"action": "should-not-be-in-delta"},
            cancel_requested_at=None,
            expires_at=now,
            created_at=now,
        )
        chunk = SimpleNamespace(
            command_id="command-001",
            seq=0,
            payload_json={"text": "done"},
            is_final=True,
            created_at=now,
        )
        db = MagicMock()
        db.scalar.return_value = command
        db.scalars.return_value = [chunk]

        result = build_state_delta(db, _user(), cursors={"agent_status": 0})

        entity = result["changes"][0]["entity"]
        self.assertEqual(entity["type"], "agent_command_status")
        self.assertEqual(entity["status"], "completed")
        self.assertEqual(entity["result_chunks"][0]["payload_json"], {"text": "done"})
        self.assertNotIn("payload_json", entity)

    @patch("app.services.sync_v2_service.compact_change_snapshot", return_value={"runs": 10})
    def test_reset_bootstrap_pages_run_records_by_after_id(self, _snapshot) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = [
            SimpleNamespace(
                id=1,
                workspace_id=7,
                task_id=3,
                executed_by=9,
                platform="douyin",
                keyword="coffee",
                brand="Acme",
                mode="browser",
                result_json={"rank": 1},
                idempotency_key="run-001",
                executed_at=now,
                created_at=now,
            ),
            SimpleNamespace(
                id=2,
                workspace_id=7,
                task_id=3,
                executed_by=9,
                platform="douyin",
                keyword="tea",
                brand="Acme",
                mode="browser",
                result_json={"rank": 2},
                idempotency_key="run-002",
                executed_at=now,
                created_at=now,
            ),
        ]
        db = MagicMock()
        db.scalars.side_effect = [[3], rows]
        token = make_reset_token(7, ["runs"])

        result = build_state_delta(db, _user(), cursors={}, reset_token=token, limit=1)

        self.assertEqual(result["changes"][0]["entity"]["id"], 1)
        self.assertTrue(result["has_more"])
        self.assertEqual(parse_bootstrap_cursor(result["bootstrap_cursor"])["after_id"], 1)

    @patch("app.services.sync_v2_service.compact_change_snapshot", return_value={"profile": 1})
    def test_reset_bootstrap_includes_profile_payload(self, _snapshot) -> None:
        token = make_reset_token(7, ["profile"])
        result = build_state_delta(MagicMock(), _user(display_name="Shu"), cursors={}, reset_token=token)

        self.assertEqual(result["changes"][0]["kind"], "profile.bootstrap")
        self.assertEqual(result["changes"][0]["entity"]["display_name"], "Shu")
        self.assertFalse(result["has_more"])

    @patch("app.services.sync_v2_service.compact_change_snapshot", return_value={"agent_status": 10})
    def test_reset_bootstrap_pages_agent_status_by_cursor_key(self, _snapshot) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = [
            SimpleNamespace(
                id="command-001",
                workspace_id=7,
                target_device_id="mac-1",
                target_role=None,
                status="completed",
                visibility_until=None,
                idempotency_key="agent-key-1",
                payload_json={"action": "hidden"},
                cancel_requested_at=None,
                expires_at=now,
                created_at=now,
            ),
            SimpleNamespace(
                id="command-002",
                workspace_id=7,
                target_device_id="mac-1",
                target_role=None,
                status="running",
                visibility_until=now,
                idempotency_key="agent-key-2",
                payload_json={"action": "hidden"},
                cancel_requested_at=None,
                expires_at=now,
                created_at=now,
            ),
        ]
        db = MagicMock()
        db.scalars.side_effect = [rows, [], []]
        token = make_reset_token(7, ["agent_status"])

        result = build_state_delta(db, _user(), cursors={}, reset_token=token, limit=1)

        self.assertEqual(result["changes"][0]["entity"]["id"], "command-001")
        self.assertTrue(result["has_more"])
        cursor = parse_bootstrap_cursor(result["bootstrap_cursor"])
        self.assertIn("command-001", cursor["after_key"])


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
