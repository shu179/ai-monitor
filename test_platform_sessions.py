import threading
import time
import unittest
from unittest.mock import patch

from core.platform_sessions import (
    PlatformSessionManager,
    _PlatformWorkerProxy,
    build_query_execution_policy,
    build_session_pool_dispatch_pairs,
)


class FakePlatform:
    def __init__(self):
        self.page = object()
        self.closed = False

    def close(self):
        self.closed = True


class FailingStartPlatform(FakePlatform):
    def __init__(self):
        super().__init__()
        self.page = None

    def start(self):
        raise RuntimeError("startup failed")


class HangingStartPlatform(FakePlatform):
    def __init__(self):
        super().__init__()
        self.page = None
        self.abort_called = threading.Event()
        self.started = threading.Event()
        self.closed = False

    def start(self):
        self.started.set()
        self.abort_called.wait(timeout=2)
        raise RuntimeError("startup aborted")

    def abort_startup(self):
        self.abort_called.set()


class HangingClosePlatform(FakePlatform):
    def __init__(self):
        super().__init__()
        self.user_data_dir = "/tmp/session-profile"
        self.close_started = threading.Event()
        self.allow_close = threading.Event()

    def close(self):
        self.close_started.set()
        self.allow_close.wait(timeout=5)
        super().close()


class FakeDispatchManager:
    def __init__(self):
        self.policy = build_query_execution_policy(
            {
                "query_execution": {
                    "browser": {
                        "strategy": "session_pool",
                        "session_pool_dispatch": "platform_batch",
                        "session_pool_platform_batch_size": 2,
                    }
                }
            },
            "browser",
        )

    def get_dispatch_snapshot(self, platform_name):
        return {
            "has_live_session": platform_name == "doubao",
            "completed_queries": 0,
            "consecutive_structural_failures": 0,
            "dirty_after_manual_recovery": False,
            "restart_cooldown_remaining_seconds": 0.0,
            "session_age_seconds": 0.0,
        }


class PlatformSessionManagerTests(unittest.TestCase):
    def test_dispatch_pairs_fall_back_to_entry_platform_order_without_batch_policy(self):
        entries = [
            {"id": "a", "platforms": ["doubao", "kimi"]},
            {"id": "b", "platforms": ["tongyi"]},
        ]

        pairs = list(build_session_pool_dispatch_pairs(entries, [], {}, None))

        self.assertEqual(
            [(entry["id"], platform) for entry, platform in pairs],
            [("a", "doubao"), ("a", "kimi"), ("b", "tongyi")],
        )

    def test_dispatch_pairs_use_platform_batches_when_session_pool_enabled(self):
        entries_by_platform = {
            "doubao": [
                {"id": "doubao-1"},
                {"id": "doubao-2"},
                {"id": "doubao-3"},
            ],
            "kimi": [
                {"id": "kimi-1"},
            ],
        }
        entries = entries_by_platform["doubao"] + entries_by_platform["kimi"]

        pairs = list(
            build_session_pool_dispatch_pairs(
                entries,
                ["doubao", "kimi"],
                entries_by_platform,
                FakeDispatchManager(),
            )
        )

        self.assertEqual(
            [(entry["id"], platform) for entry, platform in pairs],
            [
                ("doubao-1", "doubao"),
                ("doubao-2", "doubao"),
                ("kimi-1", "kimi"),
                ("doubao-3", "doubao"),
            ],
        )

    def test_concurrent_get_or_create_reuses_single_worker(self):
        manager = PlatformSessionManager(
            "browser",
            build_query_execution_policy({}, "browser"),
            {"tongyi": 5},
        )
        start_event = threading.Event()
        counter_lock = threading.Lock()
        factory_calls = 0
        results = []
        errors = []

        def factory():
            nonlocal factory_calls
            with counter_lock:
                factory_calls += 1
            time.sleep(0.05)
            return FakePlatform()

        def worker():
            try:
                start_event.wait(timeout=2)
                results.append(manager.get_or_create("tongyi", factory))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        start_event.set()
        for thread in threads:
            thread.join(timeout=3)

        try:
            self.assertEqual(errors, [])
            self.assertEqual(factory_calls, 1)
            self.assertEqual(len(results), 6)
            self.assertEqual(len({id(item) for item in results}), 1)
        finally:
            manager.close_all(reason="test cleanup")

    def test_startup_failure_closes_created_platform(self):
        manager = PlatformSessionManager(
            "browser",
            build_query_execution_policy({}, "browser"),
            {"yuanbao": 1},
        )
        holder = {}

        def factory():
            platform = FailingStartPlatform()
            holder["platform"] = platform
            return platform

        with self.assertRaises(RuntimeError):
            manager.get_or_create("yuanbao", factory)

        self.assertTrue(holder["platform"].closed)
        self.assertEqual(manager._sessions, {})

    def test_startup_timeout_aborts_created_platform(self):
        manager = PlatformSessionManager(
            "browser",
            build_query_execution_policy({}, "browser"),
            {"yuanbao": 1},
        )
        holder = {}
        previous_timeout = _PlatformWorkerProxy.STARTUP_TIMEOUT_SECONDS
        _PlatformWorkerProxy.STARTUP_TIMEOUT_SECONDS = 0.1

        def factory():
            platform = HangingStartPlatform()
            holder["platform"] = platform
            return platform

        try:
            with self.assertRaises(TimeoutError):
                manager.get_or_create("yuanbao", factory)
            self.assertTrue(holder["platform"].started.wait(timeout=1))
            self.assertTrue(holder["platform"].abort_called.is_set())
            self.assertEqual(manager._sessions, {})
        finally:
            _PlatformWorkerProxy.STARTUP_TIMEOUT_SECONDS = previous_timeout
            manager.close_all(reason="test cleanup")

    def test_session_age_and_restart_cooldown_use_monotonic_time(self):
        manager = PlatformSessionManager(
            "browser",
            build_query_execution_policy({}, "browser"),
            {"doubao": 1},
        )

        try:
            with patch("core.platform_sessions.time.monotonic", return_value=100.0):
                manager.get_or_create("doubao", FakePlatform)

            with patch("core.platform_sessions.time.monotonic", return_value=160.0):
                snapshot = manager.get_dispatch_snapshot("doubao")
                self.assertEqual(snapshot["session_age_seconds"], 60.0)
                manager.restart_session("doubao", reason="test")

            with patch("core.platform_sessions.time.monotonic", return_value=220.0):
                snapshot = manager.get_dispatch_snapshot("doubao")
                self.assertEqual(snapshot["restart_cooldown_remaining_seconds"], 540.0)
        finally:
            manager.close_all(reason="test cleanup")

    def test_worker_close_timeout_reclaims_profile_processes(self):
        holder = {}
        previous_request_timeout = _PlatformWorkerProxy.CLOSE_REQUEST_TIMEOUT_SECONDS
        previous_join_timeout = _PlatformWorkerProxy.CLOSE_JOIN_TIMEOUT_SECONDS
        previous_reclaim_timeout = _PlatformWorkerProxy.CLOSE_RECLAIM_JOIN_TIMEOUT_SECONDS
        _PlatformWorkerProxy.CLOSE_REQUEST_TIMEOUT_SECONDS = 0.05
        _PlatformWorkerProxy.CLOSE_JOIN_TIMEOUT_SECONDS = 0.05
        _PlatformWorkerProxy.CLOSE_RECLAIM_JOIN_TIMEOUT_SECONDS = 0.05

        def factory():
            platform = HangingClosePlatform()
            holder["platform"] = platform
            return platform

        try:
            proxy = _PlatformWorkerProxy("doubao", factory)
            with patch(
                "core.platform_sessions.browser_profile_owner_pids",
                return_value=[888],
            ), patch(
                "core.platform_sessions.terminate_browser_profile_processes",
                return_value=True,
            ) as terminate:
                proxy.close()

            self.assertTrue(holder["platform"].close_started.is_set())
            terminate.assert_called_once_with("/tmp/session-profile", graceful_timeout=2.0, force=True)
        finally:
            if holder.get("platform"):
                holder["platform"].allow_close.set()
            if "proxy" in locals():
                proxy._thread.join(timeout=1)
            _PlatformWorkerProxy.CLOSE_REQUEST_TIMEOUT_SECONDS = previous_request_timeout
            _PlatformWorkerProxy.CLOSE_JOIN_TIMEOUT_SECONDS = previous_join_timeout
            _PlatformWorkerProxy.CLOSE_RECLAIM_JOIN_TIMEOUT_SECONDS = previous_reclaim_timeout


if __name__ == "__main__":
    unittest.main()
