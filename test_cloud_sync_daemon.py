from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.cloud_sync_daemon import (
    CloudSyncCommandDaemonProcess,
    InProcessCloudSyncCommandClient,
    DAEMON_SUPPORTED_COMMANDS,
    UnixSocketCloudSyncCommandClient,
    UnixSocketCloudSyncCommandServer,
    build_cloud_sync_socket_path,
    run_cloud_sync_command_daemon,
)
from web_backend import AppRuntime, WebAppServer


class CloudSyncDaemonTests(unittest.TestCase):
    def test_build_cloud_sync_socket_path_is_stable_and_short(self) -> None:
        left = build_cloud_sync_socket_path("/tmp/account-a")
        right = build_cloud_sync_socket_path("/tmp/account-a")
        other = build_cloud_sync_socket_path("/tmp/account-b")

        self.assertEqual(left, right)
        self.assertNotEqual(left, other)
        self.assertLess(len(str(left)), 108)

    def test_in_process_command_client_forwards_payload(self) -> None:
        seen: list[tuple[str, dict[str, object] | None]] = []

        client = InProcessCloudSyncCommandClient(lambda command, payload: seen.append((command, payload)) or {"ok": True})
        result = client.send_command("cloud.status", {"force": True})

        self.assertEqual(result, {"ok": True})
        self.assertEqual(seen, [("cloud.status", {"force": True})])

    @unittest.skipUnless(hasattr(__import__("socket"), "AF_UNIX"), "Unix socket unsupported on this platform")
    def test_unix_socket_command_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = Path(tmpdir) / "cloud-sync.sock"
            seen: list[tuple[str, dict[str, object] | None]] = []
            server = UnixSocketCloudSyncCommandServer(
                socket_path,
                command_handler=lambda command, payload: seen.append((command, payload)) or {"ok": True, "echo": payload},
            )
            server.start()
            try:
                client = UnixSocketCloudSyncCommandClient(socket_path)
                result = client.send_command("cloud.flush_outbox", {"limit": 7})
            finally:
                server.stop()

        self.assertEqual(result, {"ok": True, "echo": {"limit": 7}})
        self.assertEqual(seen, [("cloud.flush_outbox", {"limit": 7})])

    @unittest.skipUnless(hasattr(__import__("socket"), "AF_UNIX"), "Unix socket unsupported on this platform")
    def test_child_process_daemon_ping_and_supported_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = Path(tmpdir) / "cloud-sync.sock"
            daemon = CloudSyncCommandDaemonProcess(socket_path)
            daemon.start()
            try:
                client = UnixSocketCloudSyncCommandClient(socket_path)
                ping = client.send_command("cloud.daemon.ping")
                status = client.send_command("cloud.status")
            finally:
                daemon.stop()

        self.assertTrue(ping.get("ok"))
        self.assertTrue(ping.get("daemon"))
        self.assertTrue(status.get("ok"))
        self.assertIn("cloud", status)

    def test_run_cloud_sync_command_daemon_exports_supported_command_list(self) -> None:
        self.assertIn("cloud.flush_outbox", DAEMON_SUPPORTED_COMMANDS)
        self.assertIn("cloud.outbox_diagnostics", DAEMON_SUPPORTED_COMMANDS)
        self.assertIn("cloud.capabilities", DAEMON_SUPPORTED_COMMANDS)
        self.assertIn("cloud.object_cache_diagnostics", DAEMON_SUPPORTED_COMMANDS)
        self.assertIn("cloud.object_transfer_diagnostics", DAEMON_SUPPORTED_COMMANDS)
        self.assertIn("cloud.object_transfer_retry_candidates", DAEMON_SUPPORTED_COMMANDS)
        self.assertIn("cloud.retry_object_downloads", DAEMON_SUPPORTED_COMMANDS)
        self.assertIn("cloud.retry_object_uploads", DAEMON_SUPPORTED_COMMANDS)
        self.assertIn("cloud.prune_object_cache", DAEMON_SUPPORTED_COMMANDS)
        self.assertIn("cloud.cache_object", DAEMON_SUPPORTED_COMMANDS)
        self.assertIn("cloud.pull_state_delta", DAEMON_SUPPORTED_COMMANDS)
        self.assertIn("cloud.state_delta_diagnostics", DAEMON_SUPPORTED_COMMANDS)
        self.assertIn("cloud.process_state_delta_inbox", DAEMON_SUPPORTED_COMMANDS)
        self.assertNotIn("cloud.admin_object_storage_report", DAEMON_SUPPORTED_COMMANDS)
        self.assertNotIn("cloud.update_admin_task", DAEMON_SUPPORTED_COMMANDS)
        self.assertNotIn("cloud.delete_admin_task", DAEMON_SUPPORTED_COMMANDS)

    def test_app_runtime_cloud_command_uses_transport_client(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime._cloud_command_transport_lock = threading.RLock()
        runtime._cloud_command_client = None

        client = Mock()
        client.send_command.return_value = {"ok": True, "message": "ok"}

        with patch(
            "web_backend.create_in_process_cloud_sync_command_client",
            return_value=client,
        ):
            runtime._ensure_cloud_runtime_support = Mock(return_value=Mock(handle_command=Mock()))  # type: ignore[method-assign]
            result = AppRuntime._cloud_runtime_command(runtime, "cloud.flush_outbox", {"limit": 7})

        self.assertEqual(result, {"ok": True, "message": "ok"})
        client.send_command.assert_called_once_with("cloud.flush_outbox", {"limit": 7})

    def test_app_runtime_start_and_stop_cloud_command_transport(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime.config_path = Path("/tmp/aibrandmonitor-config.yaml")
        runtime._cloud_command_transport_lock = threading.RLock()
        runtime._cloud_command_client = None
        runtime._cloud_command_server = None
        runtime._cloud_command_daemon = None
        runtime._cloud_command_socket_path = None

        support = Mock()
        support.handle_command = Mock(return_value={"ok": True})
        fallback_client = Mock()
        socket_client = Mock()
        daemon = Mock()

        with (
            patch("web_backend.create_in_process_cloud_sync_command_client", return_value=fallback_client),
            patch("web_backend.build_cloud_sync_socket_path", return_value=Path("/tmp/cloud-sync.sock")) as build_path,
            patch("web_backend.CloudSyncCommandDaemonProcess", return_value=daemon) as daemon_cls,
            patch("web_backend.UnixSocketCloudSyncCommandClient", return_value=socket_client) as client_cls,
        ):
            runtime._ensure_cloud_runtime_support = Mock(return_value=support)  # type: ignore[method-assign]
            AppRuntime._start_cloud_command_transport(runtime)

        build_path.assert_called_once()
        daemon_cls.assert_called_once_with(
            Path("/tmp/cloud-sync.sock"),
            supported_commands=DAEMON_SUPPORTED_COMMANDS,
        )
        daemon.start.assert_called_once()
        client_cls.assert_called_once_with(Path("/tmp/cloud-sync.sock"))
        self.assertIs(runtime._cloud_command_client, socket_client)
        self.assertIs(runtime._cloud_command_daemon, daemon)
        self.assertEqual(runtime._cloud_command_socket_path, Path("/tmp/cloud-sync.sock"))

        AppRuntime._stop_cloud_command_transport(runtime)

        daemon.stop.assert_called_once()
        self.assertIsNone(runtime._cloud_command_client)
        self.assertIsNone(runtime._cloud_command_daemon)
        self.assertIsNone(runtime._cloud_command_socket_path)

    def test_app_runtime_stop_cloud_command_transport_cancels_recovery_timer_and_joins_thread(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime._cloud_command_transport_lock = threading.RLock()
        server = Mock()
        daemon = Mock()
        recovery_thread = Mock()
        recovery_thread.join = Mock()
        timer = Mock()
        runtime._cloud_command_server = server
        runtime._cloud_command_daemon = daemon
        runtime._cloud_command_client = Mock()
        runtime._cloud_command_socket_path = Path("/tmp/cloud-sync.sock")
        runtime._cloud_command_transport_recovery_thread = recovery_thread
        runtime._cloud_command_transport_recovery_timer = timer
        runtime._cloud_command_transport_next_recovery_at = time.monotonic() + 30
        runtime._cloud_command_transport_next_recovery_after = "2026-05-28T12:00:30"

        AppRuntime._stop_cloud_command_transport(runtime)

        timer.cancel.assert_called_once()
        recovery_thread.join.assert_called_once_with(timeout=1.0)
        daemon.stop.assert_called_once()
        server.stop.assert_called_once()
        self.assertIsNone(runtime._cloud_command_transport_recovery_timer)
        self.assertEqual(runtime._cloud_command_transport_next_recovery_at, 0.0)
        self.assertEqual(runtime._cloud_command_transport_next_recovery_after, "")
        self.assertEqual(runtime._cloud_command_transport_mode, "stopped")

    def test_app_runtime_cloud_command_transport_status_reports_child_daemon(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        process = Mock()
        process.is_alive.return_value = True
        daemon = Mock(process=process)
        runtime._cloud_command_daemon = daemon
        runtime._cloud_command_server = None
        runtime._cloud_command_client = Mock()
        runtime._cloud_command_socket_path = Path("/tmp/cloud-sync.sock")
        runtime._cloud_command_transport_mode = "child_daemon"
        runtime._cloud_command_transport_error = ""

        status = AppRuntime._cloud_command_transport_status(runtime)

        self.assertEqual(status["mode"], "child_daemon")
        self.assertTrue(status["client_active"])
        self.assertTrue(status["daemon_active"])
        self.assertTrue(status["daemon_process_alive"])
        self.assertFalse(status["responsive"])
        self.assertFalse(status["socket_ping"]["ok"])
        self.assertEqual(status["socket_path"], "/tmp/cloud-sync.sock")

    def test_app_runtime_cloud_command_transport_status_pings_child_daemon(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        process = Mock()
        process.is_alive.return_value = True
        runtime._cloud_command_daemon = Mock(process=process)
        runtime._cloud_command_server = None
        runtime._cloud_command_client = Mock()
        runtime._cloud_command_socket_path = Path("/tmp/cloud-sync.sock")
        runtime._cloud_command_transport_mode = "child_daemon"
        runtime._cloud_command_transport_error = ""
        ping_client = Mock()
        ping_client.send_command.return_value = {"ok": True, "daemon": True, "pid": 1234}

        with patch("web_backend.UnixSocketCloudSyncCommandClient", return_value=ping_client) as client_cls:
            status = AppRuntime._cloud_command_transport_status(runtime)

        client_cls.assert_called_once_with(Path("/tmp/cloud-sync.sock"), timeout_seconds=0.25)
        ping_client.send_command.assert_called_once_with("cloud.daemon.ping")
        self.assertTrue(status["responsive"])
        self.assertTrue(status["socket_ping"]["attempted"])
        self.assertTrue(status["socket_ping"]["ok"])
        self.assertTrue(status["socket_ping"]["daemon"])
        self.assertEqual(status["socket_ping"]["pid"], 1234)

    def test_app_runtime_cloud_command_transport_status_reports_direct_fallback(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime._cloud_command_daemon = None
        runtime._cloud_command_server = None
        runtime._cloud_command_client = Mock()
        runtime._cloud_command_socket_path = None
        runtime._cloud_command_transport_recovery_thread = None
        runtime._cloud_command_transport_last_recovery_attempt_at = 0.0
        runtime._cloud_command_transport_last_recovery_attempt = ""
        runtime._cloud_command_transport_last_recovery_reason = ""
        runtime._cloud_command_transport_last_recovery_completed_at = ""
        runtime._cloud_command_transport_last_recovery_result = ""
        runtime._cloud_command_transport_last_recovery_error = ""
        runtime._cloud_command_transport_recovery_timer = None
        runtime._cloud_command_transport_next_recovery_at = 0.0
        runtime._cloud_command_transport_next_recovery_after = ""
        runtime._cloud_command_transport_recovery_interval_seconds = 30.0
        runtime._cloud_command_transport_mode = "in_process_direct"
        runtime._cloud_command_transport_error = "AF_UNIX unavailable"

        status = AppRuntime._cloud_command_transport_status(runtime)

        self.assertEqual(status["mode"], "in_process_direct")
        self.assertTrue(status["client_active"])
        self.assertFalse(status["daemon_active"])
        self.assertFalse(status["daemon_process_alive"])
        self.assertTrue(status["responsive"])
        self.assertFalse(status["socket_ping"]["attempted"])
        self.assertFalse(status["recovery_running"])
        self.assertEqual(status["last_recovery_attempt"], "")
        self.assertEqual(status["last_recovery_reason"], "")
        self.assertEqual(status["next_recovery_allowed_in_seconds"], 0)
        self.assertEqual(status["last_error"], "AF_UNIX unavailable")
        self.assertTrue(status["transport_blocked"])
        self.assertEqual(status["transport_blockers"][0]["kind"], "daemon_degraded")
        self.assertEqual(
            status["next_transport_action"],
            {"kind": "recover_daemon", "reason": "daemon_degraded", "retry_after_seconds": 0},
        )

    def test_app_runtime_cloud_command_transport_status_reports_recovery_cooldown(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime._cloud_command_daemon = None
        runtime._cloud_command_server = None
        runtime._cloud_command_client = Mock()
        runtime._cloud_command_socket_path = None
        runtime._cloud_command_transport_recovery_thread = None
        runtime._cloud_command_transport_last_recovery_attempt_at = time.monotonic()
        runtime._cloud_command_transport_last_recovery_attempt = "2026-05-28T12:00:00"
        runtime._cloud_command_transport_last_recovery_reason = "timed out"
        runtime._cloud_command_transport_last_recovery_completed_at = ""
        runtime._cloud_command_transport_last_recovery_result = "running"
        runtime._cloud_command_transport_last_recovery_error = ""
        runtime._cloud_command_transport_recovery_timer = None
        runtime._cloud_command_transport_next_recovery_at = time.monotonic() + 30
        runtime._cloud_command_transport_next_recovery_after = "2026-05-28T12:00:30"
        runtime._cloud_command_transport_recovery_interval_seconds = 30.0
        runtime._cloud_command_transport_mode = "in_process_direct"
        runtime._cloud_command_transport_error = "timed out"

        status = AppRuntime._cloud_command_transport_status(runtime)

        self.assertTrue(status["transport_blocked"])
        self.assertEqual(status["transport_blockers"][0]["kind"], "daemon_recovery_cooldown")
        self.assertEqual(status["next_transport_action"]["kind"], "recover_daemon")
        self.assertEqual(status["next_transport_action"]["reason"], "cooldown")
        self.assertGreater(status["next_transport_action"]["retry_after_seconds"], 0)

    def test_app_runtime_cloud_command_transport_status_reports_scheduled_recovery(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime._cloud_command_daemon = None
        runtime._cloud_command_server = None
        runtime._cloud_command_client = Mock()
        runtime._cloud_command_socket_path = None
        runtime._cloud_command_transport_recovery_thread = None
        timer = Mock()
        timer.is_alive.return_value = True
        runtime._cloud_command_transport_recovery_timer = timer
        runtime._cloud_command_transport_last_recovery_attempt_at = time.monotonic()
        runtime._cloud_command_transport_last_recovery_attempt = "2026-05-28T12:00:00"
        runtime._cloud_command_transport_last_recovery_reason = "timed out"
        runtime._cloud_command_transport_last_recovery_completed_at = "2026-05-28T12:00:01"
        runtime._cloud_command_transport_last_recovery_result = "failed"
        runtime._cloud_command_transport_last_recovery_error = "socket busy"
        runtime._cloud_command_transport_next_recovery_at = time.monotonic() + 30
        runtime._cloud_command_transport_next_recovery_after = "2026-05-28T12:00:30"
        runtime._cloud_command_transport_recovery_interval_seconds = 30.0
        runtime._cloud_command_transport_mode = "in_process_direct"
        runtime._cloud_command_transport_error = "socket busy"

        status = AppRuntime._cloud_command_transport_status(runtime)

        self.assertTrue(status["recovery_scheduled"])
        self.assertEqual(status["transport_blockers"][0]["kind"], "daemon_recovery_scheduled")
        self.assertEqual(status["next_transport_action"]["reason"], "scheduled")
        self.assertEqual(status["next_recovery_after"], "2026-05-28T12:00:30")

    def test_app_runtime_cloud_runtime_command_falls_back_when_daemon_cannot_handle_command(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime._cloud_command_transport_lock = threading.RLock()
        runtime._cloud_command_client = Mock(
            send_command=Mock(
                return_value={
                    "ok": False,
                    "unsupported_by_daemon": True,
                    "message": "命令仍需由主进程处理: cloud.status",
                }
            )
        )
        support = Mock()
        support.handle_command.return_value = {"ok": True, "cloud": {"loggedIn": True}}
        runtime._ensure_cloud_runtime_support = Mock(return_value=support)  # type: ignore[method-assign]

        result = AppRuntime._cloud_runtime_command(runtime, "cloud.status")

        self.assertEqual(result, {"ok": True, "cloud": {"loggedIn": True}})
        support.handle_command.assert_called_once_with("cloud.status", None)

    def test_app_runtime_cloud_runtime_command_degrades_when_daemon_unavailable(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime._cloud_command_transport_lock = threading.RLock()
        runtime._cloud_command_client = Mock(
            send_command=Mock(
                return_value={
                    "ok": False,
                    "daemon_unavailable": True,
                    "message": "云同步 daemon 不可用: timed out",
                }
            )
        )
        runtime._cloud_command_daemon = Mock()
        runtime._cloud_command_server = None
        runtime._cloud_command_socket_path = Path("/tmp/cloud-sync.sock")
        runtime._cloud_command_transport_mode = "child_daemon"
        runtime._cloud_command_transport_error = ""
        support = Mock()
        support.handle_command.return_value = {"ok": True, "message": "local fallback"}
        fallback_client = Mock()

        with (
            patch("web_backend.create_in_process_cloud_sync_command_client", return_value=fallback_client),
            patch.object(runtime, "_schedule_cloud_command_transport_recovery", return_value=True) as schedule_recovery,
        ):
            runtime._ensure_cloud_runtime_support = Mock(return_value=support)  # type: ignore[method-assign]
            result = AppRuntime._cloud_runtime_command(runtime, "cloud.flush_outbox")

        self.assertEqual(result, {"ok": True, "message": "local fallback"})
        self.assertIs(runtime._cloud_command_client, fallback_client)
        self.assertIsNone(runtime._cloud_command_daemon)
        self.assertIsNone(runtime._cloud_command_socket_path)
        self.assertEqual(runtime._cloud_command_transport_mode, "in_process_direct")
        self.assertIn("timed out", runtime._cloud_command_transport_error)
        schedule_recovery.assert_called_once()
        support.handle_command.assert_called_once_with("cloud.flush_outbox", None)

    def test_app_runtime_schedule_cloud_command_transport_recovery_starts_background_thread(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime._cloud_command_transport_lock = threading.RLock()
        runtime._cloud_command_daemon = None
        runtime._cloud_command_server = None
        runtime._cloud_command_client = Mock()
        runtime._cloud_command_transport_mode = "in_process_direct"
        runtime._cloud_command_transport_stop_requested = False
        runtime._cloud_command_transport_recovery_thread = None
        runtime._cloud_command_transport_last_recovery_attempt_at = 0.0
        runtime._cloud_command_transport_last_recovery_attempt = ""
        runtime._cloud_command_transport_last_recovery_reason = ""
        runtime._cloud_command_transport_last_recovery_completed_at = ""
        runtime._cloud_command_transport_last_recovery_result = ""
        runtime._cloud_command_transport_last_recovery_error = ""
        runtime._cloud_command_transport_recovery_timer = None
        runtime._cloud_command_transport_next_recovery_at = 0.0
        runtime._cloud_command_transport_next_recovery_after = ""
        runtime._cloud_command_transport_recovery_interval_seconds = 30.0
        recovery_thread = Mock()
        recovery_thread.is_alive.return_value = True

        with patch("web_backend.threading.Thread", return_value=recovery_thread) as thread_cls:
            scheduled = AppRuntime._schedule_cloud_command_transport_recovery(runtime, "timed out")

        self.assertTrue(scheduled)
        thread_cls.assert_called_once()
        recovery_thread.start.assert_called_once()
        self.assertEqual(runtime._cloud_command_transport_last_recovery_reason, "timed out")
        self.assertEqual(runtime._cloud_command_transport_last_recovery_result, "running")
        self.assertEqual(runtime._cloud_command_transport_last_recovery_error, "")
        self.assertNotEqual(runtime._cloud_command_transport_last_recovery_attempt, "")

    def test_app_runtime_schedule_cloud_command_transport_recovery_skips_existing_timer(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime._cloud_command_transport_lock = threading.RLock()
        runtime._cloud_command_daemon = None
        runtime._cloud_command_server = None
        runtime._cloud_command_client = Mock()
        runtime._cloud_command_transport_mode = "in_process_direct"
        runtime._cloud_command_transport_stop_requested = False
        runtime._cloud_command_transport_recovery_thread = None
        runtime._cloud_command_transport_last_recovery_attempt_at = 123.0
        runtime._cloud_command_transport_last_recovery_attempt = "2026-05-28T12:00:00"
        runtime._cloud_command_transport_last_recovery_reason = "timed out"
        runtime._cloud_command_transport_last_recovery_completed_at = ""
        runtime._cloud_command_transport_last_recovery_result = ""
        runtime._cloud_command_transport_last_recovery_error = ""
        timer = Mock()
        timer.is_alive.return_value = True
        runtime._cloud_command_transport_recovery_timer = timer
        runtime._cloud_command_transport_next_recovery_at = 0.0
        runtime._cloud_command_transport_next_recovery_after = ""
        runtime._cloud_command_transport_recovery_interval_seconds = 30.0

        with (
            patch("web_backend.time.monotonic", return_value=130.0),
            patch("web_backend.threading.Timer") as timer_cls,
            patch("web_backend.threading.Thread") as thread_cls,
        ):
            scheduled = AppRuntime._schedule_cloud_command_transport_recovery(runtime, "timed out")

        self.assertFalse(scheduled)
        timer_cls.assert_not_called()
        thread_cls.assert_not_called()

    def test_app_runtime_schedule_cloud_command_transport_recovery_sets_timer_during_cooldown(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime._cloud_command_transport_lock = threading.RLock()
        runtime._cloud_command_daemon = None
        runtime._cloud_command_server = None
        runtime._cloud_command_client = Mock()
        runtime._cloud_command_transport_mode = "in_process_direct"
        runtime._cloud_command_transport_stop_requested = False
        runtime._cloud_command_transport_recovery_thread = None
        runtime._cloud_command_transport_recovery_timer = None
        runtime._cloud_command_transport_last_recovery_attempt_at = 123.0
        runtime._cloud_command_transport_last_recovery_attempt = "2026-05-28T12:00:00"
        runtime._cloud_command_transport_last_recovery_reason = "timed out"
        runtime._cloud_command_transport_last_recovery_completed_at = "2026-05-28T12:00:01"
        runtime._cloud_command_transport_last_recovery_result = "failed"
        runtime._cloud_command_transport_last_recovery_error = "socket busy"
        runtime._cloud_command_transport_next_recovery_at = 0.0
        runtime._cloud_command_transport_next_recovery_after = ""
        runtime._cloud_command_transport_recovery_interval_seconds = 30.0
        timer = Mock()
        timer.is_alive.return_value = False

        with (
            patch("web_backend.time.monotonic", return_value=130.0),
            patch("web_backend.threading.Timer", return_value=timer) as timer_cls,
            patch("web_backend.threading.Thread") as thread_cls,
        ):
            scheduled = AppRuntime._schedule_cloud_command_transport_recovery(runtime, "socket busy")

        self.assertTrue(scheduled)
        timer_cls.assert_called_once()
        timer.start.assert_called_once()
        thread_cls.assert_not_called()
        self.assertIs(runtime._cloud_command_transport_recovery_timer, timer)
        self.assertGreater(runtime._cloud_command_transport_next_recovery_at, 0)
        self.assertNotEqual(runtime._cloud_command_transport_next_recovery_after, "")

    def test_app_runtime_recover_cloud_command_transport_records_success(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime._cloud_command_transport_lock = threading.RLock()
        runtime._cloud_command_transport_stop_requested = False
        runtime._cloud_command_transport_recovery_thread = threading.current_thread()
        runtime._cloud_command_transport_last_recovery_completed_at = ""
        runtime._cloud_command_transport_last_recovery_result = "running"
        runtime._cloud_command_transport_last_recovery_error = ""
        runtime._cloud_command_transport_recovery_timer = None
        runtime._cloud_command_transport_next_recovery_at = 0.0
        runtime._cloud_command_transport_next_recovery_after = ""

        def start_transport() -> None:
            runtime._cloud_command_transport_mode = "child_daemon"
            runtime._cloud_command_transport_error = ""

        with (
            patch.object(runtime, "_start_cloud_command_transport", side_effect=start_transport),
            patch.object(
                runtime,
                "_cloud_command_transport_status",
                return_value={"responsive": True, "mode": "child_daemon", "last_error": ""},
            ),
        ):
            AppRuntime._recover_cloud_command_transport_worker(runtime)

        self.assertIsNone(runtime._cloud_command_transport_recovery_thread)
        self.assertEqual(runtime._cloud_command_transport_last_recovery_result, "recovered")
        self.assertEqual(runtime._cloud_command_transport_last_recovery_error, "")
        self.assertNotEqual(runtime._cloud_command_transport_last_recovery_completed_at, "")

    def test_app_runtime_recover_cloud_command_transport_records_failure(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime._cloud_command_transport_lock = threading.RLock()
        runtime._cloud_command_transport_stop_requested = False
        runtime._cloud_command_transport_recovery_thread = threading.current_thread()
        runtime._cloud_command_transport_last_recovery_completed_at = ""
        runtime._cloud_command_transport_last_recovery_result = "running"
        runtime._cloud_command_transport_last_recovery_error = ""
        runtime._cloud_command_transport_recovery_timer = None
        runtime._cloud_command_transport_next_recovery_at = 0.0
        runtime._cloud_command_transport_next_recovery_after = ""

        with (
            patch.object(runtime, "_start_cloud_command_transport", side_effect=RuntimeError("socket busy")),
            patch.object(
                runtime,
                "_cloud_command_transport_status",
                return_value={"responsive": False, "mode": "in_process_direct", "last_error": "socket busy"},
            ),
            patch.object(runtime, "_schedule_cloud_command_transport_recovery", return_value=True) as schedule,
        ):
            AppRuntime._recover_cloud_command_transport_worker(runtime)

        self.assertIsNone(runtime._cloud_command_transport_recovery_thread)
        self.assertEqual(runtime._cloud_command_transport_last_recovery_result, "failed")
        self.assertEqual(runtime._cloud_command_transport_last_recovery_error, "socket busy")
        self.assertNotEqual(runtime._cloud_command_transport_last_recovery_completed_at, "")
        schedule.assert_called_once_with("socket busy", force=False)

    def test_app_runtime_cloud_runtime_command_skips_daemon_for_unsupported_command(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        support = Mock()
        support.handle_command.return_value = {"ok": True, "message": "local"}
        runtime._ensure_cloud_runtime_support = Mock(return_value=support)  # type: ignore[method-assign]
        runtime._cloud_command_client = Mock(send_command=Mock(side_effect=AssertionError("daemon should not be used")))

        result = AppRuntime._cloud_runtime_command(runtime, "cloud.schedule_article_snapshot")

        self.assertEqual(result, {"ok": True, "message": "local"})
        support.handle_command.assert_called_once_with("cloud.schedule_article_snapshot", None)

    def test_app_runtime_cloud_runtime_command_keeps_admin_task_updates_on_main_process(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        support = Mock()
        support.handle_command.return_value = {"ok": True, "message": "main process"}
        runtime._ensure_cloud_runtime_support = Mock(return_value=support)  # type: ignore[method-assign]
        runtime._cloud_command_client = Mock(send_command=Mock(side_effect=AssertionError("daemon should not be used")))

        result = AppRuntime._cloud_runtime_command(runtime, "cloud.update_admin_task", {"task_id": "task-1"})

        self.assertEqual(result, {"ok": True, "message": "main process"})
        support.handle_command.assert_called_once_with("cloud.update_admin_task", {"task_id": "task-1"})

    def test_app_runtime_cloud_status_merges_local_auto_sync_state(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime._cloud_command_client = Mock(send_command=Mock(return_value={"ok": True, "cloud": {"loggedIn": True}}))
        runtime._cloud_platform_auto_sync = Mock()
        runtime._cloud_platform_auto_sync.get_status.return_value = {"running": True}

        result = AppRuntime._cloud_runtime_command(runtime, "cloud.status")

        self.assertEqual(result["cloud"]["autoSync"], {"running": True})

    def test_app_runtime_logout_stays_on_main_process_support(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        support = Mock()
        support.handle_command.return_value = {"ok": True, "message": "local logout"}
        runtime._ensure_cloud_runtime_support = Mock(return_value=support)  # type: ignore[method-assign]
        runtime._cloud_command_client = Mock(send_command=Mock(side_effect=AssertionError("daemon should not be used")))

        result = AppRuntime._cloud_runtime_command(runtime, "cloud.logout")

        self.assertEqual(result, {"ok": True, "message": "local logout"})
        support.handle_command.assert_called_once_with("cloud.logout", None)

    def test_web_app_server_start_boots_cloud_command_transport(self) -> None:
        runtime = Mock()
        http_server = Mock()
        thread = Mock()

        with (
            patch("web_backend.AppRuntime", return_value=runtime),
            patch("web_backend.ThreadingHTTPServer", return_value=http_server),
            patch("web_backend.threading.Thread", return_value=thread),
            patch.object(WebAppServer, "_pick_port", return_value=51082),
        ):
            server = WebAppServer()
            url = server.start()
            server.stop()

        self.assertEqual(url, "http://127.0.0.1:51082/")
        runtime._start_cloud_command_transport.assert_called_once()
        runtime.restore_monitoring_if_needed.assert_called_once()
        runtime.schedule_context_snapshot_startup_refresh.assert_called_once()
        runtime.shutdown.assert_called_once()
        http_server.shutdown.assert_called_once()
        http_server.server_close.assert_called_once()
        thread.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
