from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.cloud_sync_daemon import (
    InProcessCloudSyncCommandClient,
    UnixSocketCloudSyncCommandClient,
    UnixSocketCloudSyncCommandServer,
    build_cloud_sync_socket_path,
)


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


if __name__ == "__main__":
    unittest.main()
