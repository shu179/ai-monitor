from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.cloud_sync_daemon import (
    InProcessCloudSyncCommandClient,
    UnixSocketCloudSyncCommandClient,
    UnixSocketCloudSyncCommandServer,
    build_cloud_sync_socket_path,
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

    def test_app_runtime_cloud_command_uses_transport_client(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime._cloud_command_transport_lock = threading.RLock()
        runtime._cloud_command_client = None

        client = Mock()
        client.send_command.return_value = {"ok": True, "cloud": {"loggedIn": True}}

        with patch(
            "web_backend.create_in_process_cloud_sync_command_client",
            return_value=client,
        ):
            runtime._ensure_cloud_runtime_support = Mock(return_value=Mock(handle_command=Mock()))  # type: ignore[method-assign]
            result = AppRuntime._cloud_runtime_command(runtime, "cloud.status", {"force": True})

        self.assertEqual(result, {"ok": True, "cloud": {"loggedIn": True}})
        client.send_command.assert_called_once_with("cloud.status", {"force": True})

    def test_app_runtime_start_and_stop_cloud_command_transport(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime.config_path = Path("/tmp/aibrandmonitor-config.yaml")
        runtime._cloud_command_transport_lock = threading.RLock()
        runtime._cloud_command_client = None
        runtime._cloud_command_server = None
        runtime._cloud_command_socket_path = None

        support = Mock()
        support.handle_command = Mock(return_value={"ok": True})
        fallback_client = Mock()
        socket_client = Mock()
        socket_server = Mock()

        with (
            patch("web_backend.create_in_process_cloud_sync_command_client", return_value=fallback_client),
            patch("web_backend.build_cloud_sync_socket_path", return_value=Path("/tmp/cloud-sync.sock")) as build_path,
            patch("web_backend.UnixSocketCloudSyncCommandServer", return_value=socket_server) as server_cls,
            patch("web_backend.UnixSocketCloudSyncCommandClient", return_value=socket_client) as client_cls,
        ):
            runtime._ensure_cloud_runtime_support = Mock(return_value=support)  # type: ignore[method-assign]
            AppRuntime._start_cloud_command_transport(runtime)

        build_path.assert_called_once()
        server_cls.assert_called_once_with(
            Path("/tmp/cloud-sync.sock"),
            command_handler=support.handle_command,
        )
        socket_server.start.assert_called_once()
        client_cls.assert_called_once_with(Path("/tmp/cloud-sync.sock"))
        self.assertIs(runtime._cloud_command_client, socket_client)
        self.assertIs(runtime._cloud_command_server, socket_server)
        self.assertEqual(runtime._cloud_command_socket_path, Path("/tmp/cloud-sync.sock"))

        AppRuntime._stop_cloud_command_transport(runtime)

        socket_server.stop.assert_called_once()
        self.assertIsNone(runtime._cloud_command_client)
        self.assertIsNone(runtime._cloud_command_server)
        self.assertIsNone(runtime._cloud_command_socket_path)

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
