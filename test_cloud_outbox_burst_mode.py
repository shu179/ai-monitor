"""Verify cloud outbox burst-mode flushing and diagnostics."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.cloud_outbox import CloudOutbox
from core.cloud_platform_auto_sync import CloudPlatformAutoSync
from core.cloud_run_sync import flush_cloud_outbox
from core.cloud_session_store import CloudSessionStore


class FakeCloudClient:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def post_events(self, access_token: str, events: list[dict]) -> dict:
        del access_token
        self.events.extend(events)
        return {"accepted": len(events), "duplicates": 0}


class CloudOutboxBurstModeTests(unittest.TestCase):
    def _manager(self) -> CloudPlatformAutoSync:
        return CloudPlatformAutoSync(
            pull_tasks=lambda: {"ok": True},
            upload_retry_interval_seconds=20.0,
            upload_burst_interval_seconds=1.0,
            upload_burst_pending_threshold=100,
            event_stream_enabled=False,
            logger=lambda _message: None,
        )

    def test_large_backlog_uses_burst_interval(self) -> None:
        manager = self._manager()

        self.assertEqual(manager._effective_retry_interval(pending_count=500), 1.0)  # noqa: SLF001

    def test_small_backlog_uses_normal_interval(self) -> None:
        manager = self._manager()

        self.assertEqual(manager._effective_retry_interval(pending_count=10), 20.0)  # noqa: SLF001

    def test_threshold_boundary(self) -> None:
        manager = self._manager()

        self.assertEqual(manager._effective_retry_interval(pending_count=99), 20.0)  # noqa: SLF001
        self.assertEqual(manager._effective_retry_interval(pending_count=100), 1.0)  # noqa: SLF001

    def test_invalid_env_values_fall_back_to_constructor_defaults(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "AIBRANDMONITOR_CLOUD_UPLOAD_BURST_INTERVAL_SECONDS": "not-a-float",
                "AIBRANDMONITOR_CLOUD_UPLOAD_BURST_PENDING_THRESHOLD": "not-an-int",
            },
        ):
            manager = self._manager()

        self.assertEqual(manager._effective_retry_interval(pending_count=99), 20.0)  # noqa: SLF001
        self.assertEqual(manager._effective_retry_interval(pending_count=100), 1.0)  # noqa: SLF001

    def test_flush_logs_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            outbox = CloudOutbox(root / "outbox.json")
            store = CloudSessionStore(root / "session.json")
            store.save({
                "base_url": "https://api.example.com",
                "access_token": "access",
                "refresh_token": "refresh",
                "user": {"id": 2, "workspace_id": 1, "role": "operator"},
            })
            outbox.enqueue(
                event_type="run_record",
                idempotency_key="run:diagnostic",
                payload={"task_id": 1, "platform": "kimi"},
            )
            logs: list[str] = []

            with patch("core.cloud_run_sync.print", side_effect=logs.append):
                result = flush_cloud_outbox(client=FakeCloudClient(), session_store=store, outbox=outbox)

        self.assertTrue(result["ok"])
        self.assertEqual(len(logs), 1)
        self.assertIn("[CloudOutbox] flush", logs[0])
        for field in (
            "trace_id=",
            "batch_size=",
            "elapsed_ms=",
            "pending_before=",
            "pending_after=",
            "failed_count=",
            "http_status=",
        ):
            self.assertIn(field, logs[0])


if __name__ == "__main__":
    unittest.main()
