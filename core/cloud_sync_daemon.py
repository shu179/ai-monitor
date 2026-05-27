"""Local cloud sync daemon IPC helpers.

Phase 3b starts with a minimal command transport that can run in-process today
and move to a Unix-socket daemon later without changing the command payloads.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable


DAEMON_SUPPORTED_COMMANDS = frozenset(
    {
        "cloud.flush_outbox",
        "cloud.list_admin_users",
        "cloud.list_admin_article_classification_jobs",
        "cloud.resolve_admin_article_classification_job",
        "cloud.ignore_admin_article_classification_job",
        "cloud.create_admin_user",
        "cloud.update_admin_user",
        "cloud.delete_admin_user",
        "cloud.update_admin_task",
        "cloud.delete_admin_task",
        "cloud.restore_admin_task",
    }
)


def build_cloud_sync_socket_path(scope_hint: str | os.PathLike[str]) -> Path:
    """Build a short, stable Unix-socket path for a given account/runtime scope."""
    digest = hashlib.sha1(os.fsdecode(scope_hint).encode("utf-8", errors="ignore")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"aibrandmonitor-cloud-sync-{digest}.sock"


class InProcessCloudSyncCommandClient:
    """Small adapter that keeps today's in-process command flow transport-shaped."""

    def __init__(self, command_handler: Callable[[str, dict[str, Any] | None], dict[str, Any]]) -> None:
        self._command_handler = command_handler

    def send_command(self, command: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._command_handler(command, payload)


class UnixSocketCloudSyncCommandClient:
    """Command client for a local Unix-socket cloud sync daemon."""

    def __init__(self, socket_path: str | os.PathLike[str], *, timeout_seconds: float = 5.0) -> None:
        self.socket_path = os.fspath(socket_path)
        self.timeout_seconds = max(0.1, float(timeout_seconds or 5.0))

    def send_command(self, command: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not hasattr(socket, "AF_UNIX"):
            return {"ok": False, "message": "当前平台不支持 Unix socket"}
        request = {"command": str(command or "").strip(), "payload": payload if isinstance(payload, dict) else None}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout_seconds)
                sock.connect(self.socket_path)
                sock.sendall(_encode_message(request))
                sock.shutdown(socket.SHUT_WR)
                response = _decode_message(_recv_until_eof(sock))
        except Exception as exc:
            return {"ok": False, "daemon_unavailable": True, "message": f"云同步 daemon 不可用: {exc}"}
        return response if isinstance(response, dict) else {"ok": False, "message": "云同步 daemon 返回无效响应"}


class CloudSyncCommandDaemonProcess:
    """Manage a child-process Unix-socket daemon for cloud sync commands."""

    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        *,
        supported_commands: set[str] | frozenset[str] | None = None,
        startup_timeout_seconds: float = 5.0,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.supported_commands = tuple(sorted(set(supported_commands or DAEMON_SUPPORTED_COMMANDS)))
        self.startup_timeout_seconds = max(0.5, float(startup_timeout_seconds or 5.0))
        self._process: multiprocessing.Process | None = None

    @property
    def process(self) -> multiprocessing.Process | None:
        return self._process

    def start(self) -> None:
        process = self._process
        if process is not None and process.is_alive():
            return
        try:
            if self.socket_path.exists():
                self.socket_path.unlink()
        except FileNotFoundError:
            pass
        ctx = multiprocessing.get_context("spawn")
        process = ctx.Process(
            target=run_cloud_sync_command_daemon,
            args=(str(self.socket_path), self.supported_commands),
            name="cloud-sync-daemon",
            daemon=True,
        )
        process.start()
        self._process = process
        self._wait_until_ready()

    def stop(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            self._cleanup_socket_path()
            return
        try:
            if process.is_alive():
                client = UnixSocketCloudSyncCommandClient(self.socket_path, timeout_seconds=0.5)
                client.send_command("cloud.daemon.shutdown")
                process.join(timeout=2.0)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2.0)
        finally:
            self._cleanup_socket_path()

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + self.startup_timeout_seconds
        client = UnixSocketCloudSyncCommandClient(self.socket_path, timeout_seconds=0.2)
        last_error = "daemon startup timed out"
        while time.monotonic() < deadline:
            process = self._process
            if process is not None and not process.is_alive():
                raise RuntimeError("云同步 daemon 启动失败：子进程已退出")
            if self.socket_path.exists():
                result = client.send_command("cloud.daemon.ping")
                if bool(result.get("ok")) and bool(result.get("daemon")):
                    return
                last_error = str(result.get("message") or last_error)
            time.sleep(0.05)
        raise RuntimeError(f"云同步 daemon 启动超时: {last_error}")

    def _cleanup_socket_path(self) -> None:
        try:
            if self.socket_path.exists():
                self.socket_path.unlink()
        except FileNotFoundError:
            pass


class UnixSocketCloudSyncCommandServer:
    """Tiny JSON-over-Unix-socket server for local cloud sync commands."""

    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        *,
        command_handler: Callable[[str, dict[str, Any] | None], dict[str, Any]],
        thread_factory: Callable[..., Any] = threading.Thread,
    ) -> None:
        self.socket_path = Path(socket_path)
        self._command_handler = command_handler
        self._thread_factory = thread_factory
        self._server_socket: socket.socket | None = None
        self._thread: Any = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._server_socket is not None:
            return
        if not hasattr(socket, "AF_UNIX"):
            raise RuntimeError("当前平台不支持 Unix socket")
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.socket_path.exists():
                self.socket_path.unlink()
        except FileNotFoundError:
            pass
        server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_socket.bind(str(self.socket_path))
        server_socket.listen(16)
        self._server_socket = server_socket
        self._stop_event.clear()
        self._thread = self._thread_factory(
            target=self._serve_loop,
            name="cloud-sync-daemon-ipc",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        server_socket = self._server_socket
        self._server_socket = None
        if server_socket is not None:
            try:
                server_socket.close()
            except Exception:
                pass
        thread = self._thread
        if (
            thread is not None
            and thread is not threading.current_thread()
            and callable(getattr(thread, "join", None))
        ):
            try:
                thread.join(timeout=1.0)
            except Exception:
                pass
        self._thread = None
        try:
            if self.socket_path.exists():
                self.socket_path.unlink()
        except FileNotFoundError:
            pass

    def _serve_loop(self) -> None:
        server_socket = self._server_socket
        if server_socket is None:
            return
        while not self._stop_event.is_set():
            try:
                conn, _addr = server_socket.accept()
            except OSError:
                if self._stop_event.is_set():
                    return
                continue
            try:
                self._handle_connection(conn)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            request = _decode_message(_recv_until_eof(conn))
            if not isinstance(request, dict):
                response = {"ok": False, "message": "请求格式无效"}
            else:
                command = str(request.get("command") or "").strip()
                payload = request.get("payload")
                if payload is not None and not isinstance(payload, dict):
                    payload = None
                response = self._command_handler(command, payload)
                if not isinstance(response, dict):
                    response = {"ok": False, "message": "命令处理结果无效"}
        except Exception as exc:
            response = {"ok": False, "message": str(exc)}
        conn.sendall(_encode_message(response))


def _encode_message(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _decode_message(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    return json.loads(text)


def _recv_until_eof(sock: socket.socket) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def run_cloud_sync_command_daemon(
    socket_path: str | os.PathLike[str],
    supported_commands: tuple[str, ...] | list[str] | set[str] | frozenset[str] | None = None,
) -> None:
    """Run the child-process cloud sync daemon until a shutdown command arrives."""
    stop_event = threading.Event()
    allowed = {str(item or "").strip() for item in (supported_commands or DAEMON_SUPPORTED_COMMANDS)}
    server_holder: dict[str, UnixSocketCloudSyncCommandServer | None] = {"server": None}
    support = _build_daemon_runtime_support()

    def command_handler(command: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        normalized = str(command or "").strip()
        if normalized in {"cloud.daemon.ping", "daemon.ping"}:
            return {"ok": True, "daemon": True, "pid": os.getpid()}
        if normalized in {"cloud.daemon.shutdown", "daemon.shutdown"}:
            stop_event.set()
            server = server_holder.get("server")
            if server is not None:
                server.stop()
            support.stop()
            return {"ok": True, "daemon": True, "message": "云同步 daemon 已停止"}
        if normalized not in allowed:
            return {
                "ok": False,
                "unsupported_by_daemon": True,
                "message": f"命令仍需由主进程处理: {normalized}",
            }
        return support.handle_command(normalized, payload)

    server = UnixSocketCloudSyncCommandServer(
        socket_path,
        command_handler=command_handler,
    )
    server_holder["server"] = server
    server.start()
    try:
        while not stop_event.wait(0.25):
            continue
    finally:
        support.stop()
        server.stop()


def _build_daemon_runtime_support() -> Any:
    from core.cloud_sync_runtime import AppCloudRuntimeSupport

    class _DaemonOwner:
        def __init__(self) -> None:
            self._lock = threading.RLock()

    return AppCloudRuntimeSupport(
        owner=_DaemonOwner(),
        auto_sync_status_getter=lambda: {},
    )
