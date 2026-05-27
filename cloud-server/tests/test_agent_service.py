from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import AgentCommand  # noqa: E402
from app.services.agent_service import (  # noqa: E402
    AGENT_COMMAND_NOTIFY_CHANNEL,
    AgentCommandNotificationListener,
    _decode_agent_command_notify_payload,
    agent_command_notify_payload,
    append_agent_result_chunk,
    claim_agent_command,
    create_agent_command,
)


class AgentServiceTests(unittest.TestCase):
    def test_notify_payload_contains_only_routing_fields(self) -> None:
        payload = json.loads(agent_command_notify_payload(workspace_id=7, device_id="mac-1"))

        self.assertEqual(payload, {"workspace_id": 7, "device_id": "mac-1"})
        self.assertLess(len(json.dumps(payload)), 8000)

    def test_notify_payload_decoder_rejects_non_json(self) -> None:
        self.assertEqual(_decode_agent_command_notify_payload("not-json"), {})

    def test_notify_listener_routes_workspace_and_device(self) -> None:
        listener = AgentCommandNotificationListener(workspace_id=7, device_id="mac-1")

        self.assertTrue(listener._payload_matches_device({"workspace_id": 7, "device_id": "mac-1"}))
        self.assertTrue(listener._payload_matches_device({"workspace_id": 7, "device_id": ""}))
        self.assertFalse(listener._payload_matches_device({"workspace_id": 8, "device_id": "mac-1"}))
        self.assertFalse(listener._payload_matches_device({"workspace_id": 7, "device_id": "mac-2"}))

    @patch("app.services.agent_service.record_workspace_change", return_value=1)
    def test_create_agent_command_reserves_idempotency_and_notifies(self, _change) -> None:
        db = MagicMock()
        db.scalar.side_effect = ["agent-key-1", None]
        user = SimpleNamespace(id=2, workspace_id=7)

        result = create_agent_command(
            db,
            user,  # type: ignore[arg-type]
            idempotency_key="agent-key-1",
            payload_json={"action": "ping"},
            target_device_id="mac-1",
        )

        self.assertEqual(result["target_device_id"], "mac-1")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(db.add.call_count, 1)
        notify_sql = str(db.execute.call_args.args[0])
        self.assertIn(f"pg_notify('{AGENT_COMMAND_NOTIFY_CHANNEL}'", notify_sql)
        db.commit.assert_called_once()

    @patch("app.services.agent_service.record_workspace_change", return_value=2)
    def test_claim_agent_command_marks_delivered_and_sets_visibility(self, _change) -> None:
        now = datetime.now(timezone.utc)
        command = AgentCommand(
            id="command-1",
            workspace_id=7,
            target_device_id=None,
            target_role=None,
            status="pending",
            visibility_until=now - timedelta(seconds=1),
            idempotency_key="agent-key-1",
            payload_json={"action": "ping"},
            expires_at=now + timedelta(minutes=5),
            created_at=now,
        )
        db = MagicMock()
        db.scalar.return_value = command
        user = SimpleNamespace(workspace_id=7)

        result = claim_agent_command(db, user, device_id="mac-1")  # type: ignore[arg-type]

        self.assertEqual(result["id"], "command-1")
        self.assertEqual(command.status, "delivered")
        self.assertEqual(command.target_device_id, "mac-1")
        self.assertGreater(command.visibility_until, now)
        db.commit.assert_called_once()

    @patch("app.services.agent_service.record_workspace_change", return_value=3)
    def test_append_final_result_chunk_marks_completed(self, _change) -> None:
        now = datetime.now(timezone.utc)
        command = AgentCommand(
            id="command-1",
            workspace_id=7,
            target_device_id="mac-1",
            target_role=None,
            status="running",
            visibility_until=now + timedelta(seconds=30),
            idempotency_key="agent-key-1",
            payload_json={},
            expires_at=now + timedelta(minutes=5),
            created_at=now,
        )
        db = MagicMock()
        db.scalar.return_value = command
        user = SimpleNamespace(workspace_id=7)

        ack = append_agent_result_chunk(
            db,
            user,  # type: ignore[arg-type]
            command_id="command-1",
            device_id="mac-1",
            seq=2,
            payload_json={"text": "done"},
            is_final=True,
            final_status="completed",
        )

        self.assertEqual(ack["status"], "completed")
        self.assertIsNone(command.visibility_until)
        stmt = db.execute.call_args.args[0]
        self.assertIn("ON CONFLICT", str(stmt.compile()).upper())
        db.commit.assert_called_once()


class AgentRouteRegistrationTests(unittest.TestCase):
    def test_agent_routes_are_registered(self) -> None:
        os.environ.setdefault("SURFACED_CLOUD_SECRET_KEY", "test-secret-key-for-agent-routes")
        from app.api.routes.agent import router

        paths = {route.path for route in router.routes}

        self.assertIn("/commands", paths)
        self.assertIn("/commands:claim", paths)
        self.assertIn("/commands/{command_id}/chunks", paths)
        self.assertIn("/ws", paths)


if __name__ == "__main__":
    unittest.main()
