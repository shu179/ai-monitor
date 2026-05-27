from __future__ import annotations

import sys
import unittest
import os
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SURFACED_CLOUD_SECRET_KEY", "test-secret-key-for-event-streams")

from app.api.routes import events  # noqa: E402


class _FakeChangeListener:
    def __init__(self, *, workspace_id: int) -> None:
        self.workspace_id = workspace_id
        self.calls = 0

    def __enter__(self) -> "_FakeChangeListener":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def wait(self, *, timeout_seconds: float) -> list[dict[str, object]]:
        self.calls += 1
        if self.calls == 1:
            return [{"workspace_id": self.workspace_id, "stream": "articles", "seq": 6}]
        return []


class ChangeEventGeneratorTests(unittest.TestCase):
    def test_notify_event_does_not_force_snapshot_reconcile(self) -> None:
        user_context = {"id": 1, "workspace_id": 7, "token_version": 0}
        load_changes = MagicMock(return_value={"articles": 5})

        with (
            patch.object(events, "STREAM_MAX_SECONDS", 0.5),
            patch.object(events, "STREAM_HEARTBEAT_SECONDS", 999.0),
            patch.object(events, "CHANGE_STREAM_RECONCILE_SECONDS", 999.0),
            patch.object(events, "WorkspaceChangeNotificationListener", _FakeChangeListener),
            patch.object(events, "_load_change_snapshot_or_none", load_changes),
            patch.object(events, "_load_current_user", return_value=object()),
        ):
            generator = events._change_event_generator(user_context)
            hello = next(generator)
            article_event = next(generator)

        self.assertIn("event: hello", hello)
        self.assertIn("event: article_changed", article_event)
        self.assertIn('"stream":"articles"', article_event)
        self.assertIn('"seq":6', article_event)
        load_changes.assert_called_once_with(user_context)


if __name__ == "__main__":
    unittest.main()
