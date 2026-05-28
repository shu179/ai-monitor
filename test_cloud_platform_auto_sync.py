from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import requests

from core.cloud_client import _format_request_exception
from core.cloud_outbox import CloudOutbox
from core.cloud_client import CloudClientError
from core.cloud_object_transfer_store import CloudObjectTransferStore
from core.cloud_platform_auto_sync import CloudPlatformAutoSync
from core.cloud_session_store import CloudSessionStore


class CloudPlatformAutoSyncTests(unittest.TestCase):
    def test_event_connection_error_message_does_not_duplicate_connection_word(self):
        message = _format_request_exception("云端事件连接", requests.ConnectionError("broken"))

        self.assertEqual(message, "云端事件连接失败，将自动重试")

    def test_read_timeout_message_is_normalized_and_transient_logs_are_throttled(self):
        logs: list[str] = []
        manager = CloudPlatformAutoSync(
            pull_tasks=lambda: {"ok": True},
            event_stream_enabled=False,
            logger=logs.append,
        )

        manager._record_error(  # noqa: SLF001 - assert log noise guard for operator-facing output
            "云端事件监听失败：HTTPSConnectionPool(host='api.surfacedlab.com', port=443): Read timed out."
        )
        manager._record_error("云端请求连接失败，将自动重试")  # noqa: SLF001

        self.assertEqual(logs, ["[CloudPlatformAutoSync] 云端事件连接超时，正在自动重连"])
        self.assertEqual(manager.get_status()["last_error"], "云端请求连接失败，将自动重试")

    def test_event_reconnect_delay_uses_exponential_backoff_with_cap(self):
        manager = CloudPlatformAutoSync(
            pull_tasks=lambda: {"ok": True},
            event_stream_enabled=False,
            event_reconnect_seconds=5,
            event_reconnect_max_seconds=30,
            event_reconnect_jitter_ratio=0,
        )

        self.assertEqual(manager._event_reconnect_delay(1), 5.0)  # noqa: SLF001
        self.assertEqual(manager._event_reconnect_delay(2), 10.0)  # noqa: SLF001
        self.assertEqual(manager._event_reconnect_delay(3), 20.0)  # noqa: SLF001
        self.assertEqual(manager._event_reconnect_delay(4), 30.0)  # noqa: SLF001
        self.assertEqual(manager._event_reconnect_delay(8), 30.0)  # noqa: SLF001

    def test_event_reconnect_delay_jitter_does_not_exceed_cap(self):
        manager = CloudPlatformAutoSync(
            pull_tasks=lambda: {"ok": True},
            event_stream_enabled=False,
            event_reconnect_seconds=5,
            event_reconnect_max_seconds=30,
            event_reconnect_jitter_ratio=0.5,
        )

        with patch("core.cloud_platform_auto_sync.random.uniform", return_value=45.0):
            self.assertEqual(manager._event_reconnect_delay(8), 30.0)  # noqa: SLF001

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

    def test_logged_in_session_uses_smart_initial_pull_when_supported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CloudSessionStore(Path(tmpdir) / "session.json")
            store.save(
                {
                    "base_url": "https://api.example.com",
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "user": {"id": 4, "workspace_id": 1, "role": "viewer"},
                }
            )
            calls: list[bool] = []
            manager = CloudPlatformAutoSync(
                session_store=store,
                outbox=CloudOutbox(Path(tmpdir) / "outbox.json"),
                pull_tasks=lambda force=False: calls.append(bool(force)) or {"ok": True},
                event_stream_enabled=False,
                logger=lambda _message: None,
            )

            manager.start()
            try:
                deadline = time.time() + 2.0
                while not calls and time.time() < deadline:
                    time.sleep(0.05)
            finally:
                manager.stop()

            self.assertEqual(calls, [False])

    def test_logged_in_session_processes_state_delta_after_initial_pull(self):
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
            calls: list[tuple[str, dict[str, object]]] = []

            def pull_state_delta(payload):
                calls.append(("pull_delta", dict(payload or {})))
                return {
                    "ok": True,
                    "mode": "delta",
                    "changes": 2,
                    "inbox_created": 2,
                    "streams": {"tasks": 1, "runs": 1},
                    "duration_ms": 12,
                }

            def process_inbox(payload):
                calls.append(("process_inbox", dict(payload or {})))
                return {
                    "ok": True,
                    "claimed": 2,
                    "applied": 2,
                    "failed": 0,
                    "streams": {"tasks": {"applied": 1}, "runs": {"applied": 1}},
                }

            def retry_downloads(payload):
                calls.append(("retry_downloads", dict(payload or {})))
                return {"ok": True, "attempted": 1, "recovered": 1, "failed": 0, "skipped": 0}

            manager = CloudPlatformAutoSync(
                session_store=store,
                outbox=CloudOutbox(Path(tmpdir) / "outbox.json"),
                pull_tasks=lambda: {"ok": True},
                pull_state_delta=pull_state_delta,
                process_state_delta_inbox=process_inbox,
                retry_object_downloads=retry_downloads,
                event_stream_enabled=False,
                logger=lambda _message: None,
            )

            manager.start()
            try:
                deadline = time.time() + 2.0
                while len(calls) < 3 and time.time() < deadline:
                    time.sleep(0.05)
            finally:
                manager.stop()

            self.assertEqual([item[0] for item in calls], ["pull_delta", "process_inbox", "retry_downloads"])
            self.assertEqual(calls[0][1]["source"], "auto_sync")
            self.assertEqual(calls[1][1]["limit"], 500)
            self.assertEqual(calls[2][1]["limit"], 20)
            self.assertEqual(calls[2][1]["source"], "auto_sync")
            status = manager.get_status()
            self.assertTrue(status["last_state_delta_at"])
            self.assertEqual(status["last_state_delta_metrics"]["changes"], 2)
            self.assertEqual(status["last_state_delta_inbox_metrics"]["applied"], 2)
            self.assertEqual(status["last_object_download_retry_metrics"]["recovered"], 1)
            self.assertEqual(status["last_state_delta_error"], "")

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

    def test_logged_in_session_retries_object_uploads_after_initial_login(self):
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
            calls: list[dict[str, object]] = []
            manager = CloudPlatformAutoSync(
                session_store=store,
                outbox=CloudOutbox(Path(tmpdir) / "outbox.json"),
                pull_tasks=lambda: {"ok": True},
                retry_object_uploads=lambda payload=None: calls.append(dict(payload or {})) or {
                    "ok": True,
                    "attempted": 1,
                    "recovered": 1,
                    "failed": 0,
                    "skipped": 0,
                },
                event_stream_enabled=False,
                pull_interval_seconds=3600,
                idle_interval_seconds=0.2,
                logger=lambda _message: None,
            )

            manager.start()
            try:
                deadline = time.time() + 2.0
                while not calls and time.time() < deadline:
                    time.sleep(0.05)
            finally:
                manager.stop()

            self.assertEqual(calls[0]["limit"], 5)
            self.assertEqual(calls[0]["source"], "auto_sync")
            status = manager.get_status()
            self.assertTrue(status["last_object_upload_retry_at"])
            self.assertEqual(status["last_object_upload_retry_metrics"]["recovered"], 1)
            self.assertEqual(status["last_object_upload_retry_error"], "")

    def test_object_upload_retry_failure_is_status_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = CloudPlatformAutoSync(
                session_store=CloudSessionStore(Path(tmpdir) / "session.json"),
                outbox=CloudOutbox(Path(tmpdir) / "outbox.json"),
                pull_tasks=lambda: {"ok": True},
                retry_object_uploads=lambda payload=None: {
                    "ok": False,
                    "attempted": 1,
                    "recovered": 0,
                    "failed": 1,
                    "skipped": 0,
                    "message": "upload still unavailable",
                },
                event_stream_enabled=False,
                logger=lambda _message: None,
            )

            result = manager._invoke_object_upload_retry()  # noqa: SLF001
            manager._update_status(  # noqa: SLF001
                last_object_upload_retry_metrics={
                    "attempted": result["attempted"],
                    "recovered": result["recovered"],
                    "failed": result["failed"],
                    "skipped": result["skipped"],
                },
                last_object_upload_retry_error=str(result.get("message") or ""),
            )

            status = manager.get_status()
            self.assertFalse(result["ok"])
            self.assertEqual(status["last_object_upload_retry_metrics"]["failed"], 1)
            self.assertEqual(status["last_object_upload_retry_error"], "upload still unavailable")
            self.assertEqual(status["last_error"], "")

    def test_object_upload_retry_backpressure_pauses_next_retry(self):
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
            calls: list[float] = []

            def retry_uploads(_payload=None):
                calls.append(time.monotonic())
                if len(calls) == 1:
                    return {
                        "ok": False,
                        "attempted": 1,
                        "recovered": 0,
                        "failed": 1,
                        "skipped": 0,
                        "message": "object storage busy",
                        "retry_after_seconds": 0.8,
                        "queue_depth_hint": 120,
                        "throttle_bucket": "object_upload",
                    }
                return {"ok": True, "attempted": 1, "recovered": 1, "failed": 0, "skipped": 0}

            manager = CloudPlatformAutoSync(
                session_store=store,
                outbox=CloudOutbox(Path(tmpdir) / "outbox.json"),
                pull_tasks=lambda: {"ok": True},
                retry_object_uploads=retry_uploads,
                object_upload_retry_interval_seconds=5,
                pull_interval_seconds=3600,
                idle_interval_seconds=0.2,
                event_stream_enabled=False,
                logger=lambda _message: None,
            )

            manager.start()
            try:
                deadline = time.time() + 0.55
                while time.time() < deadline:
                    time.sleep(0.05)
                status = manager.get_status()
                self.assertEqual(len(calls), 1)
                self.assertEqual(status["object_upload_retry_backpressure_retry_after_seconds"], 0.8)
                self.assertEqual(status["object_upload_retry_backpressure_queue_depth_hint"], 120)
                self.assertEqual(status["object_upload_retry_backpressure_bucket"], "object_upload")

                deadline = time.time() + 1.5
                while len(calls) < 2 and time.time() < deadline:
                    time.sleep(0.05)
            finally:
                manager.stop()

            self.assertGreaterEqual(len(calls), 2)
            self.assertEqual(manager.get_status()["object_upload_retry_backpressure_until"], "")

    def test_object_upload_retry_status_wakes_retry_when_backoff_expires(self):
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
            transfer_store = CloudObjectTransferStore(Path(tmpdir) / "transfers.sqlite3")
            transfer_store.start_transfer(
                transfer_id="upload-1",
                direction="upload",
                sha256="a" * 64,
                size_bytes=12,
                path=str(Path(tmpdir) / "file.bin"),
                content_type="application/octet-stream",
            )
            transfer_store.fail_transfer("upload-1", "busy", retry_after_seconds=0.4)
            calls: list[float] = []

            def retry_uploads(_payload=None):
                calls.append(time.monotonic())
                transfer_store.finish_transfer("upload-1", status="completed")
                return {"ok": True, "attempted": 1, "recovered": 1, "failed": 0, "skipped": 0}

            manager = CloudPlatformAutoSync(
                session_store=store,
                outbox=CloudOutbox(Path(tmpdir) / "outbox.json"),
                pull_tasks=lambda: {"ok": True},
                retry_object_uploads=retry_uploads,
                object_upload_retry_status=lambda _payload=None: {"ok": True, "available": True, **transfer_store.retry_status(direction="upload")},
                object_upload_retry_interval_seconds=60,
                pull_interval_seconds=3600,
                idle_interval_seconds=0.2,
                event_stream_enabled=False,
                logger=lambda _message: None,
            )

            manager.start()
            try:
                deadline = time.time() + 2.0
                while not calls and time.time() < deadline:
                    time.sleep(0.05)
            finally:
                manager.stop()

            self.assertEqual(len(calls), 1)
            status = manager.get_status()
            self.assertEqual(status["object_upload_retry_wait_reason"], "idle")
            self.assertEqual(status["object_upload_retry_ready_count"], 0)
            self.assertTrue(status["last_object_upload_retry_at"])

    def test_upload_backpressure_pauses_flush_until_retry_after(self):
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
            outbox.enqueue(
                event_type="run_record",
                idempotency_key="run:backpressure",
                payload={"task_id": 1, "result": {"rank": 1, "success": True}},
            )
            flush_calls: list[float] = []

            def fake_flush(*, outbox=None, **_kwargs):
                flush_calls.append(time.monotonic())
                if len(flush_calls) == 1:
                    return {
                        "ok": False,
                        "message": "queue overloaded",
                        "metrics": {
                            "retry_after_seconds": 0.8,
                            "queue_depth_hint": 23000,
                            "throttle_bucket": "sync_metadata",
                        },
                        "outbox": outbox.stats(),
                    }
                target_outbox = outbox
                pending = target_outbox.pending()
                target_outbox.mark_sent([item["idempotency_key"] for item in pending])
                return {"ok": True, "outbox": target_outbox.stats(), "metrics": {"event_count": len(pending)}}

            with patch("core.cloud_platform_auto_sync.flush_cloud_outbox", side_effect=fake_flush):
                manager = CloudPlatformAutoSync(
                    session_store=store,
                    outbox=outbox,
                    pull_tasks=lambda: {"ok": True},
                    upload_retry_interval_seconds=5,
                    pull_interval_seconds=3600,
                    idle_interval_seconds=0.2,
                    event_stream_enabled=False,
                    logger=lambda _message: None,
                )
                manager.start()
                try:
                    deadline = time.time() + 0.55
                    while time.time() < deadline:
                        time.sleep(0.05)
                    status = manager.get_status()
                    self.assertEqual(len(flush_calls), 1)
                    self.assertEqual(status["upload_backpressure_retry_after_seconds"], 0.8)
                    self.assertEqual(status["upload_backpressure_queue_depth_hint"], 23000)
                    self.assertEqual(status["upload_backpressure_bucket"], "sync_metadata")
                    self.assertEqual(status["outbox_wait_reason"], "server_backpressure")
                    self.assertGreaterEqual(int(status["next_upload_attempt_after_seconds"] or 0), 0)

                    deadline = time.time() + 1.5
                    while int(outbox.stats().get("sent") or 0) < 1 and time.time() < deadline:
                        time.sleep(0.05)
                finally:
                    manager.stop()

            self.assertGreaterEqual(len(flush_calls), 2)
            self.assertEqual(outbox.stats()["sent"], 1)
            self.assertEqual(manager.get_status()["upload_backpressure_until"], "")

    def test_successful_upload_backpressure_hint_pauses_next_flush(self):
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
            outbox.enqueue(
                event_type="run_record",
                idempotency_key="run:success-backpressure-1",
                payload={"task_id": 1, "result": {"rank": 1, "success": True}},
            )
            outbox.enqueue(
                event_type="run_record",
                idempotency_key="run:success-backpressure-2",
                payload={"task_id": 1, "result": {"rank": 2, "success": True}},
            )
            flush_calls: list[float] = []

            def fake_flush(*, outbox=None, **_kwargs):
                flush_calls.append(time.monotonic())
                target_outbox = outbox
                pending = target_outbox.pending(limit=1)
                target_outbox.mark_sent([item["idempotency_key"] for item in pending])
                if len(flush_calls) == 1:
                    return {
                        "ok": True,
                        "outbox": target_outbox.stats(),
                        "metrics": {
                            "event_count": len(pending),
                            "retry_after_seconds": 0.8,
                            "queue_depth_hint": 12000,
                            "throttle_bucket": "sync_metadata",
                        },
                    }
                return {"ok": True, "outbox": target_outbox.stats(), "metrics": {"event_count": len(pending)}}

            with patch("core.cloud_platform_auto_sync.flush_cloud_outbox", side_effect=fake_flush):
                manager = CloudPlatformAutoSync(
                    session_store=store,
                    outbox=outbox,
                    pull_tasks=lambda: {"ok": True},
                    upload_retry_interval_seconds=5,
                    pull_interval_seconds=3600,
                    idle_interval_seconds=0.2,
                    event_stream_enabled=False,
                    logger=lambda _message: None,
                )
                manager.start()
                try:
                    deadline = time.time() + 0.55
                    while time.time() < deadline:
                        time.sleep(0.05)
                    status = manager.get_status()
                    self.assertEqual(len(flush_calls), 1)
                    self.assertEqual(status["upload_backpressure_retry_after_seconds"], 0.8)
                    self.assertEqual(status["upload_backpressure_queue_depth_hint"], 12000)

                    deadline = time.time() + 1.5
                    while int(outbox.stats().get("sent") or 0) < 2 and time.time() < deadline:
                        time.sleep(0.05)
                finally:
                    manager.stop()

            self.assertGreaterEqual(len(flush_calls), 2)
            self.assertEqual(outbox.stats()["sent"], 2)

    def test_failed_outbox_waiting_for_backoff_does_not_trigger_empty_flush(self):
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
            outbox.enqueue(
                event_type="run_record",
                idempotency_key="run:waiting-backoff",
                payload={"task_id": 1, "result": {"rank": 1, "success": True}},
            )
            outbox.mark_failed(["run:waiting-backoff"], "timeout")
            flush_calls: list[float] = []

            def fake_flush(**_kwargs):
                flush_calls.append(time.monotonic())
                return {"ok": True, "outbox": outbox.stats(), "metrics": {"event_count": 0}}

            with patch("core.cloud_platform_auto_sync.flush_cloud_outbox", side_effect=fake_flush):
                manager = CloudPlatformAutoSync(
                    session_store=store,
                    outbox=outbox,
                    pull_tasks=lambda: {"ok": True},
                    upload_retry_interval_seconds=5,
                    upload_burst_interval_seconds=0.1,
                    upload_burst_pending_threshold=1,
                    pull_interval_seconds=3600,
                    idle_interval_seconds=0.2,
                    event_stream_enabled=False,
                    logger=lambda _message: None,
                )
                manager.start()
                try:
                    time.sleep(0.8)
                    status = manager.get_status()
                finally:
                    manager.stop()

            self.assertEqual(flush_calls, [])
            self.assertEqual(outbox.stats()["failed"], 1)
            self.assertEqual(status["outbox_wait_reason"], "waiting_retry_backoff")
            self.assertGreaterEqual(int(status["outbox_next_retry_after_seconds"] or 0), 1)
            self.assertGreaterEqual(int(status["next_upload_attempt_after_seconds"] or 0), 1)

    def test_initial_login_recovers_local_candidates_before_upload(self):
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
            calls: list[str] = []

            def recover():
                calls.append("recover")
                outbox.enqueue(
                    event_type="run_record",
                    idempotency_key="run:recovered",
                    payload={"task_id": 1, "platform": "doubao"},
                )
                return {"ok": True, "queued": 1}

            def fake_flush(*, outbox=None, **_kwargs):
                calls.append("flush")
                target_outbox = outbox
                pending = target_outbox.pending()
                target_outbox.mark_sent([item["idempotency_key"] for item in pending])
                return {"ok": True, "outbox": target_outbox.stats(), "metrics": {"event_count": len(pending)}}

            with patch("core.cloud_platform_auto_sync.flush_cloud_outbox", side_effect=fake_flush):
                manager = CloudPlatformAutoSync(
                    session_store=store,
                    outbox=outbox,
                    pull_tasks=lambda: {"ok": True},
                    recover_upload_candidates=recover,
                    pull_interval_seconds=3600,
                    event_stream_enabled=False,
                    logger=lambda _message: None,
                )
                manager.start()
                try:
                    deadline = time.time() + 2.0
                    while outbox.stats().get("sent", 0) < 1 and time.time() < deadline:
                        time.sleep(0.05)
                finally:
                    manager.stop()

            self.assertEqual(calls[:2], ["recover", "flush"])
            self.assertEqual(outbox.stats()["sent"], 1)
            status = manager.get_status()
            self.assertFalse(status["startup_recovery_running"])
            self.assertEqual(status["last_startup_recovery_metrics"]["history_recovery"]["queued"], 1)

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

    def test_assignment_event_uses_sync_changes_before_full_pull(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CloudSessionStore(Path(tmpdir) / "session.json")
            calls: list[bool] = []
            manager = CloudPlatformAutoSync(
                session_store=store,
                outbox=CloudOutbox(Path(tmpdir) / "outbox.json"),
                pull_tasks=lambda force=False: calls.append(bool(force)) or {"ok": True},
                event_stream_enabled=False,
                logger=lambda _message: None,
            )

            manager._pull_now_from_event("assignment_changed")

            self.assertEqual(calls, [False])

    def test_run_record_event_uses_incremental_pull(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CloudSessionStore(Path(tmpdir) / "session.json")
            calls: list[bool] = []
            manager = CloudPlatformAutoSync(
                session_store=store,
                outbox=CloudOutbox(Path(tmpdir) / "outbox.json"),
                pull_tasks=lambda force=False: calls.append(bool(force)) or {"ok": True},
                event_stream_enabled=False,
                logger=lambda _message: None,
            )

            manager._pull_now_from_event("run_record_changed")

            self.assertEqual(calls, [False])

    def test_event_pull_processes_state_delta_pipeline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CloudSessionStore(Path(tmpdir) / "session.json")
            calls: list[str] = []
            manager = CloudPlatformAutoSync(
                session_store=store,
                outbox=CloudOutbox(Path(tmpdir) / "outbox.json"),
                pull_tasks=lambda force=False: calls.append("pull") or {"ok": True},
                pull_state_delta=lambda _payload=None: calls.append("state_delta") or {"ok": True, "changes": 1},
                process_state_delta_inbox=lambda _payload=None: calls.append("process") or {"ok": True, "applied": 1},
                event_stream_enabled=False,
                logger=lambda _message: None,
            )

            manager._pull_now_from_event("run_record_changed")

            self.assertEqual(calls, ["pull", "state_delta", "process"])
            self.assertEqual(manager.get_status()["last_state_delta_inbox_metrics"]["applied"], 1)

    def test_object_download_retry_failure_does_not_fail_state_delta_pipeline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = CloudPlatformAutoSync(
                session_store=CloudSessionStore(Path(tmpdir) / "session.json"),
                outbox=CloudOutbox(Path(tmpdir) / "outbox.json"),
                pull_tasks=lambda: {"ok": True},
                pull_state_delta=lambda _payload=None: {"ok": True, "changes": 1},
                process_state_delta_inbox=lambda _payload=None: {"ok": True, "applied": 1},
                retry_object_downloads=lambda _payload=None: {
                    "ok": False,
                    "attempted": 1,
                    "recovered": 0,
                    "failed": 1,
                    "skipped": 0,
                    "message": "temporary download failure",
                },
                event_stream_enabled=False,
                logger=lambda _message: None,
            )

            result = manager._invoke_state_delta_pipeline()  # noqa: SLF001

            self.assertTrue(result["ok"])
            self.assertFalse(result["object_download_retry"]["ok"])
            status = manager.get_status()
            self.assertEqual(status["last_state_delta_error"], "")
            self.assertEqual(status["last_state_delta_inbox_metrics"]["applied"], 1)
            self.assertEqual(status["last_object_download_retry_metrics"]["failed"], 1)

    def test_object_download_retry_backpressure_skips_retry_but_processes_inbox(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            calls: list[str] = []
            manager = CloudPlatformAutoSync(
                session_store=CloudSessionStore(Path(tmpdir) / "session.json"),
                outbox=CloudOutbox(Path(tmpdir) / "outbox.json"),
                pull_tasks=lambda: {"ok": True},
                pull_state_delta=lambda _payload=None: calls.append("state_delta") or {"ok": True, "changes": 1},
                process_state_delta_inbox=lambda _payload=None: calls.append("process") or {"ok": True, "applied": 1},
                retry_object_downloads=lambda _payload=None: calls.append("download_retry") or {
                    "ok": False,
                    "attempted": 1,
                    "recovered": 0,
                    "failed": 1,
                    "skipped": 0,
                    "message": "object download busy",
                    "retry_after_seconds": 0.6,
                    "queue_depth_hint": 17,
                    "throttle_bucket": "object_download",
                },
                event_stream_enabled=False,
                logger=lambda _message: None,
            )

            first = manager._invoke_state_delta_pipeline()  # noqa: SLF001
            second = manager._invoke_state_delta_pipeline()  # noqa: SLF001

            self.assertTrue(first["ok"])
            self.assertTrue(second["ok"])
            self.assertEqual(calls, ["state_delta", "process", "download_retry", "state_delta", "process"])
            status = manager.get_status()
            self.assertEqual(status["object_download_retry_backpressure_retry_after_seconds"], 0.6)
            self.assertEqual(status["object_download_retry_backpressure_queue_depth_hint"], 17)
            self.assertEqual(status["object_download_retry_backpressure_bucket"], "object_download")
            self.assertTrue(status["object_download_retry_backpressure_until"])
            self.assertEqual(status["last_object_download_retry_metrics"]["backpressure_active"], True)

    def test_object_download_retry_exception_does_not_fail_state_delta_pipeline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            def retry_downloads(_payload=None):
                raise RuntimeError("network still down")

            manager = CloudPlatformAutoSync(
                session_store=CloudSessionStore(Path(tmpdir) / "session.json"),
                outbox=CloudOutbox(Path(tmpdir) / "outbox.json"),
                pull_tasks=lambda: {"ok": True},
                pull_state_delta=lambda _payload=None: {"ok": True, "changes": 1},
                process_state_delta_inbox=lambda _payload=None: {"ok": True, "applied": 1},
                retry_object_downloads=retry_downloads,
                event_stream_enabled=False,
                logger=lambda _message: None,
            )

            result = manager._invoke_state_delta_pipeline()  # noqa: SLF001

            self.assertTrue(result["ok"])
            self.assertFalse(result["object_download_retry"]["ok"])
            self.assertIn("对象下载恢复失败", result["object_download_retry"]["message"])
            status = manager.get_status()
            self.assertEqual(status["last_state_delta_error"], "")
            self.assertEqual(status["last_object_download_retry_metrics"]["failed"], 0)

    def test_state_delta_backpressure_skips_remote_pull_but_processes_inbox(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CloudSessionStore(Path(tmpdir) / "session.json")
            calls: list[str] = []
            manager = CloudPlatformAutoSync(
                session_store=store,
                outbox=CloudOutbox(Path(tmpdir) / "outbox.json"),
                pull_tasks=lambda: {"ok": True},
                pull_state_delta=lambda _payload=None: calls.append("state_delta") or {
                    "ok": False,
                    "message": "queue overloaded",
                    "retry_after_seconds": 0.6,
                    "queue_depth_hint": 900,
                    "throttle_bucket": "state_delta",
                },
                process_state_delta_inbox=lambda _payload=None: calls.append("process") or {"ok": True, "applied": 1},
                event_stream_enabled=False,
                logger=lambda _message: None,
            )

            first = manager._invoke_state_delta_pipeline()  # noqa: SLF001
            second = manager._invoke_state_delta_pipeline()  # noqa: SLF001

            self.assertFalse(first["ok"])
            self.assertTrue(second["ok"])
            self.assertEqual(calls, ["state_delta", "process"])
            status = manager.get_status()
            self.assertEqual(status["state_delta_backpressure_retry_after_seconds"], 0.6)
            self.assertEqual(status["state_delta_backpressure_queue_depth_hint"], 900)
            self.assertEqual(status["state_delta_backpressure_bucket"], "state_delta")
            self.assertTrue(status["state_delta_backpressure_until"])
            self.assertEqual(status["last_state_delta_metrics"]["backpressure_active"], True)
            self.assertEqual(status["last_state_delta_inbox_metrics"]["applied"], 1)

    def test_pull_metrics_are_summarized_for_status_ui(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CloudSessionStore(Path(tmpdir) / "session.json")
            manager = CloudPlatformAutoSync(
                session_store=store,
                outbox=CloudOutbox(Path(tmpdir) / "outbox.json"),
                pull_tasks=lambda force=False: {
                    "ok": True,
                    "summary": {
                        "metrics": {
                            "mode": "partial_tasks",
                            "total_ms": 123,
                            "changed_task_ids": [9],
                            "received_tasks": 1,
                        },
                        "run_records": {
                            "request_count": 1,
                            "fetched": 3,
                            "imported": 2,
                        },
                        "task_day_status_events": {
                            "applied": 1,
                        },
                    },
                },
                event_stream_enabled=False,
                logger=lambda _message: None,
            )

            manager._pull_now_from_event("task_changed")

            summary = manager.get_status()["last_pull_summary"]
            self.assertEqual(summary["mode"], "partial_tasks")
            self.assertEqual(summary["pulled_task_count"], 1)
            self.assertEqual(summary["duration_ms"], 123)
            self.assertEqual(summary["run_record_imported"], 2)
            self.assertEqual(summary["task_day_status_applied"], 1)

    def test_refresh_event_token_only_clears_session_on_explicit_401(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CloudSessionStore(Path(tmpdir) / "session.json")
            store.save(
                {
                    "base_url": "https://api.example.com",
                    "access_token": "old-access",
                    "refresh_token": "old-refresh",
                    "user": {"id": 2, "workspace_id": 1, "role": "operator"},
                }
            )
            manager = CloudPlatformAutoSync(
                session_store=store,
                outbox=CloudOutbox(Path(tmpdir) / "outbox.json"),
                pull_tasks=lambda: {"ok": True},
                event_stream_enabled=False,
                logger=lambda _message: None,
            )

            with patch.object(
                manager,
                "_client_factory",
                return_value=type(
                    "FakeClient",
                    (),
                    {"refresh": staticmethod(lambda _token: (_ for _ in ()).throw(CloudClientError("temporary failure")))}
                )(),
            ):
                refreshed, should_clear = manager._refresh_event_token(  # noqa: SLF001
                    base_url="https://api.example.com",
                    access_token="old-access",
                    refresh_token="old-refresh",
                    workspace_id="1",
                    user_id="2",
                )

            self.assertFalse(refreshed)
            self.assertFalse(should_clear)
            self.assertEqual(store.load()["refresh_token"], "old-refresh")

    def test_refresh_event_token_marks_session_clear_on_refresh_401(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CloudSessionStore(Path(tmpdir) / "session.json")
            store.save(
                {
                    "base_url": "https://api.example.com",
                    "access_token": "old-access",
                    "refresh_token": "old-refresh",
                    "user": {"id": 2, "workspace_id": 1, "role": "operator"},
                }
            )
            manager = CloudPlatformAutoSync(
                session_store=store,
                outbox=CloudOutbox(Path(tmpdir) / "outbox.json"),
                pull_tasks=lambda: {"ok": True},
                event_stream_enabled=False,
                logger=lambda _message: None,
            )

            with patch.object(
                manager,
                "_client_factory",
                return_value=type(
                    "FakeClient",
                    (),
                    {"refresh": staticmethod(lambda _token: (_ for _ in ()).throw(CloudClientError("refresh revoked", status_code=401)))}
                )(),
            ):
                refreshed, should_clear = manager._refresh_event_token(  # noqa: SLF001
                    base_url="https://api.example.com",
                    access_token="old-access",
                    refresh_token="old-refresh",
                    workspace_id="1",
                    user_id="2",
                )

            self.assertFalse(refreshed)
            self.assertTrue(should_clear)


if __name__ == "__main__":
    unittest.main()
