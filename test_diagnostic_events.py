"""Tests for core.diagnostic_events: throttled recording and failure counters."""
from __future__ import annotations

import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from core import diagnostic_events as diag


@contextmanager
def _patched_record_event(fake=None):
    """Patch the exact global that record_event_safe resolves at runtime."""
    replacement = fake if fake is not None else MagicMock()
    globals_dict = diag.record_event_safe.__globals__
    original = globals_dict["record_event"]
    globals_dict["record_event"] = replacement
    diag.record_event = replacement
    try:
        yield replacement
    finally:
        globals_dict["record_event"] = original
        diag.record_event = original


class RecordEventSafeTests(unittest.TestCase):
    def setUp(self):
        diag.reset_throttle_state()

    def test_returns_true_when_event_written(self):
        with _patched_record_event() as mock_record:
            result = diag.record_event_safe("cloud_sync", "test message", event_key="k1")
        self.assertTrue(result)
        mock_record.assert_called_once()

    def test_returns_false_when_throttled(self):
        with _patched_record_event():
            first = diag.record_event_safe("cloud_sync", "msg", event_key="k1", throttle_seconds=600)
            second = diag.record_event_safe("cloud_sync", "msg", event_key="k1", throttle_seconds=600)
        self.assertTrue(first)
        self.assertFalse(second)

    def test_never_raises_on_exception(self):
        with _patched_record_event(MagicMock(side_effect=RuntimeError("boom"))):
            result = diag.record_event_safe("cloud_sync", "msg", event_key="k1")
        self.assertFalse(result)

    def test_no_event_key_skips_throttle(self):
        with _patched_record_event() as mock_record:
            r1 = diag.record_event_safe("cloud_sync", "msg")
            r2 = diag.record_event_safe("cloud_sync", "msg")
        self.assertTrue(r1)
        self.assertTrue(r2)
        self.assertEqual(mock_record.call_count, 2)

    def test_throttle_is_per_event_key(self):
        with _patched_record_event():
            r1 = diag.record_event_safe("cloud_sync", "msg", event_key="key_a", throttle_seconds=600)
            r2 = diag.record_event_safe("cloud_sync", "msg", event_key="key_b", throttle_seconds=600)
        self.assertTrue(r1)
        self.assertTrue(r2)

    def test_throttle_expires_after_seconds(self):
        with _patched_record_event():
            diag.record_event_safe("cloud_sync", "msg", event_key="k_expire", throttle_seconds=1)
        with patch.object(diag, "time") as mock_time:
            mock_time.monotonic.return_value = time.monotonic() + 2
            with _patched_record_event() as mock_record:
                result = diag.record_event_safe("cloud_sync", "msg", event_key="k_expire", throttle_seconds=1)
        self.assertTrue(result)
        mock_record.assert_called_once()

    def test_failure_releases_throttle_so_same_key_can_retry(self):
        call_count = 0

        def failing_then_succeeding(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient failure")

        with _patched_record_event(MagicMock(side_effect=failing_then_succeeding)):
            first = diag.record_event_safe("c", "msg", event_key="retry_key", throttle_seconds=600)
            second = diag.record_event_safe("c", "msg", event_key="retry_key", throttle_seconds=600)
        self.assertFalse(first)
        self.assertTrue(second)

    def test_concurrent_same_key_only_writes_once(self):
        barrier = threading.Event()
        call_count = {"n": 0}

        def blocking_record(*args, **kwargs):
            call_count["n"] += 1
            barrier.wait(timeout=5)

        results = []

        def worker():
            results.append(diag.record_event_safe("c", "msg", event_key="concurrent_key", throttle_seconds=600))

        with _patched_record_event(MagicMock(side_effect=blocking_record)):
            t1 = threading.Thread(target=worker)
            t1.start()
            # Wait until the first thread is inside record_event (inflight acquired)
            deadline = time.monotonic() + 5
            while call_count["n"] < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            t2 = threading.Thread(target=worker)
            t2.start()
            t2.join(timeout=5)
            barrier.set()
            t1.join(timeout=5)

        # Second thread should have been rejected by inflight guard
        self.assertEqual(call_count["n"], 1)
        self.assertEqual(len(results), 2)
        true_count = sum(1 for r in results if r is True)
        false_count = sum(1 for r in results if r is False)
        self.assertEqual(true_count, 1)
        self.assertEqual(false_count, 1)

    def test_passes_all_fields_to_record_event(self):
        with _patched_record_event() as mock_record:
            diag.record_event_safe(
                "browser_runtime",
                "test",
                level="error",
                event_key="f1",
                details={"k": "v"},
                suggestion="fix it",
                task_name="t1",
                platform="doubao",
                keyword="kw",
                brand="b1",
            )
        mock_record.assert_called_once_with(
            "browser_runtime",
            "test",
            level="error",
            task_name="t1",
            platform="doubao",
            keyword="kw",
            brand="b1",
            details={"k": "v"},
            suggestion="fix it",
        )


class ConsecutiveFailureTests(unittest.TestCase):
    def setUp(self):
        diag.reset_failure_counts()
        diag.reset_throttle_state()

    def test_increments_counter(self):
        diag.record_consecutive_failure("op1", operation="test", error="e1")
        self.assertEqual(diag.get_consecutive_failure_count("op1"), 1)
        diag.record_consecutive_failure("op1", operation="test", error="e2")
        self.assertEqual(diag.get_consecutive_failure_count("op1"), 2)

    def test_emits_diagnostics_at_threshold(self):
        with _patched_record_event() as mock_record:
            diag.record_consecutive_failure("op1", threshold=3, operation="test", error="e1")
            diag.record_consecutive_failure("op1", threshold=3, operation="test", error="e2")
            # Not yet at threshold
            mock_record.assert_not_called()
            diag.record_consecutive_failure("op1", threshold=3, operation="test", error="e3")
            # Now at threshold — should emit
            mock_record.assert_called_once()

    def test_does_not_emit_below_threshold(self):
        with _patched_record_event() as mock_record:
            diag.record_consecutive_failure("op1", threshold=5, operation="test", error="e1")
            diag.record_consecutive_failure("op1", threshold=5, operation="test", error="e2")
            diag.record_consecutive_failure("op1", threshold=5, operation="test", error="e3")
            diag.record_consecutive_failure("op1", threshold=5, operation="test", error="e4")
        mock_record.assert_not_called()

    def test_clear_resets_counter(self):
        diag.record_consecutive_failure("op1", operation="test", error="e1")
        diag.record_consecutive_failure("op1", operation="test", error="e2")
        self.assertEqual(diag.get_consecutive_failure_count("op1"), 2)
        diag.clear_consecutive_failure("op1")
        self.assertEqual(diag.get_consecutive_failure_count("op1"), 0)

    def test_emits_once_at_threshold_then_throttled(self):
        """Emits at threshold, then subsequent calls within throttle window are suppressed."""
        with _patched_record_event() as mock_record:
            for i in range(5):
                diag.record_consecutive_failure("op1", threshold=3, operation="test", error=f"e{i}")
        # Only the first call at threshold emits; the rest are throttled
        self.assertEqual(mock_record.call_count, 1)

    def test_failure_diagnostics_includes_count_and_operation(self):
        with _patched_record_event() as mock_record:
            for i in range(3):
                diag.record_consecutive_failure(
                    "flush_cloud_outbox",
                    threshold=3,
                    category="cloud_sync",
                    operation="flush_cloud_outbox",
                    error=f"error-{i}",
                )
        call_kwargs = mock_record.call_args
        self.assertEqual(call_kwargs[0][0], "cloud_sync")
        details = call_kwargs[1]["details"]
        self.assertEqual(details["consecutive_failures"], 3)
        self.assertEqual(details["operation"], "flush_cloud_outbox")


class ResetTests(unittest.TestCase):
    def test_reset_throttle_state_clears_all(self):
        with _patched_record_event():
            diag.record_event_safe("c", "m", event_key="k1")
            diag.record_event_safe("c", "m", event_key="k2")
        diag.reset_throttle_state()
        with _patched_record_event() as mock_record:
            r1 = diag.record_event_safe("c", "m", event_key="k1")
            r2 = diag.record_event_safe("c", "m", event_key="k2")
        self.assertTrue(r1)
        self.assertTrue(r2)
        self.assertEqual(mock_record.call_count, 2)

    def test_reset_failure_counts_clears_all(self):
        diag.record_consecutive_failure("op1", operation="test", error="e1")
        diag.record_consecutive_failure("op2", operation="test", error="e1")
        diag.reset_failure_counts()
        self.assertEqual(diag.get_consecutive_failure_count("op1"), 0)
        self.assertEqual(diag.get_consecutive_failure_count("op2"), 0)


if __name__ == "__main__":
    unittest.main()
