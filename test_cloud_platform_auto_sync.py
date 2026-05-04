from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from core.cloud_outbox import CloudOutbox
from core.cloud_platform_auto_sync import CloudPlatformAutoSync
from core.cloud_session_store import CloudSessionStore


class CloudPlatformAutoSyncTests(unittest.TestCase):
    def test_logged_in_session_triggers_initial_pull(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CloudSessionStore(Path(tmpdir) / "session.json")
            store.save(
                {
                    "base_url": "https://api.example.com",
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "user": {"id": 2, "workspace_id": 1, "role": "operator"},
                }
            )
            pulls: list[float] = []
            manager = CloudPlatformAutoSync(
                session_store=store,
                outbox=CloudOutbox(Path(tmpdir) / "outbox.json"),
                pull_tasks=lambda: pulls.append(time.time()) or {"ok": True},
                event_stream_enabled=False,
                logger=lambda _message: None,
            )

            manager.start()
            try:
                deadline = time.time() + 2.0
                while not pulls and time.time() < deadline:
                    time.sleep(0.05)
            finally:
                manager.stop()

            self.assertEqual(len(pulls), 1)
            status = manager.get_status()
            self.assertTrue(status["logged_in"])
            self.assertTrue(status["last_pull_at"])

    def test_outbox_enqueue_wakes_upload_without_retry_delay(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CloudSessionStore(Path(tmpdir) / "session.json")
            store.save(
                {
                    "base_url": "https://api.example.com",
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "user": {"id": 2, "workspace_id": 1, "role": "operator"},
                }
            )
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json")
            def fake_flush(*, outbox=None, **_kwargs):
                target_outbox = outbox
                pending = target_outbox.pending()
                target_outbox.mark_sent([item["idempotency_key"] for item in pending])
                return {"ok": True, "outbox": target_outbox.stats()}

            with patch("core.cloud_platform_auto_sync.flush_cloud_outbox", side_effect=fake_flush):
                manager = CloudPlatformAutoSync(
                    session_store=store,
                    outbox=outbox,
                    pull_tasks=lambda: {"ok": True},
                    upload_retry_interval_seconds=60,
                    pull_interval_seconds=3600,
                    idle_interval_seconds=5,
                    event_stream_enabled=False,
                    logger=lambda _message: None,
                )
                manager.start()
                try:
                    time.sleep(0.2)
                    outbox.enqueue(
                        event_type="run_record",
                        idempotency_key="run:wake-test",
                        payload={"task_id": 1, "result": {"rank": 1, "success": True}},
                    )
                    deadline = time.time() + 2.0
                    while int(outbox.stats().get("sent") or 0) < 1 and time.time() < deadline:
                        time.sleep(0.05)
                finally:
                    manager.stop()

            self.assertEqual(outbox.stats()["sent"], 1)

    def test_event_stream_change_triggers_pull_without_waiting_for_interval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CloudSessionStore(Path(tmpdir) / "session.json")
            store.save(
                {
                    "base_url": "https://api.example.com",
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "user": {"id": 2, "workspace_id": 1, "role": "operator"},
                }
            )
            pulls: list[float] = []

            class FakeEventClient:
                def __init__(self) -> None:
                    self.stream_calls = 0

                def stream_events(self, access_token, *, last_event_id="", **_kwargs):
                    del access_token, last_event_id
                    self.stream_calls += 1
                    yield {"event": "task_changed", "id": "event-1", "data": {}}
                    time.sleep(0.2)

            fake_client = FakeEventClient()
            manager = CloudPlatformAutoSync(
                session_store=store,
                outbox=CloudOutbox(Path(tmpdir) / "outbox.json"),
                pull_tasks=lambda: pulls.append(time.time()) or {"ok": True},
                pull_interval_seconds=3600,
                idle_interval_seconds=5,
                event_reconnect_seconds=60,
                client_factory=lambda _base_url: fake_client,
                logger=lambda _message: None,
            )

            manager.start()
            try:
                deadline = time.time() + 2.0
                while len(pulls) < 2 and time.time() < deadline:
                    time.sleep(0.05)
            finally:
                manager.stop()

            self.assertGreaterEqual(len(pulls), 2)
            self.assertTrue(manager.get_status()["last_event_at"])


if __name__ == "__main__":
    unittest.main()
