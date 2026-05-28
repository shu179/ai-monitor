from __future__ import annotations

import tempfile
import threading
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
        self.assertIn("cloud.object_cache_diagnostics", DAEMON_SUPPORTED_COMMANDS)
        self.assertIn("cloud.object_transfer_diagnostics", DAEMON_SUPPORTED_COMMANDS)
        self.assertIn("cloud.object_transfer_retry_candidates", DAEMON_SUPPORTED_COMMANDS)
        self.assertIn("cloud.retry_object_downloads", DAEMON_SUPPORTED_COMMANDS)
        self.assertIn("cloud.prune_object_cache", DAEMON_SUPPORTED_COMMANDS)
        self.assertIn("cloud.cache_object", DAEMON_SUPPORTED_COMMANDS)
        self.assertIn("cloud.pull_state_delta", DAEMON_SUPPORTED_COMMANDS)
        self.assertIn("cloud.state_delta_diagnostics", DAEMON_SUPPORTED_COMMANDS)
        self.assertIn("cloud.process_state_delta_inbox", DAEMON_SUPPORTED_COMMANDS)

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

    def test_app_runtime_cloud_runtime_command_skips_daemon_for_unsupported_command(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        support = Mock()
        support.handle_command.return_value = {"ok": True, "message": "local"}
        runtime._ensure_cloud_runtime_support = Mock(return_value=support)  # type: ignore[method-assign]
        runtime._cloud_command_client = Mock(send_command=Mock(side_effect=AssertionError("daemon should not be used")))

        result = AppRuntime._cloud_runtime_command(runtime, "cloud.schedule_article_snapshot")

        self.assertEqual(result, {"ok": True, "message": "local"})
        support.handle_command.assert_called_once_with("cloud.schedule_article_snapshot", None)

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
