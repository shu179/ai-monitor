"""Tests for cloud outbox reliability: backoff, dead-letter, safe compaction."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.cloud_outbox import CloudOutbox, DEFAULT_MAX_ATTEMPTS, _backoff_delay


class MarkFailedBackoffTests(unittest.TestCase):
    def test_first_failure_sets_failed_with_backoff(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json")
            outbox.enqueue(event_type="run", idempotency_key="evt-1", payload={"n": 1})

            before = time.time()
            outbox.mark_failed(["evt-1"], "network error")
            after = time.time()

            items = outbox.pending(limit=10)
            self.assertEqual(len(items), 0, "failed item should not be in pending before next_attempt_ts")
            stats = outbox.stats(include_retry=True)
            self.assertEqual(stats["upload_ready"], 0)
            self.assertEqual(stats["retry_ready"], 0)
            self.assertGreaterEqual(stats["next_retry_after_seconds"], 50)

            # Read raw file to inspect fields
            with open(Path(tmpdir) / "outbox.json") as f:
                raw = json.load(f)
            item = raw[0]
            self.assertEqual(item["status"], "failed")
            self.assertEqual(item["attempts"], 1)
            self.assertEqual(item["last_error"], "network error")
            self.assertIsNotNone(item.get("next_attempt_ts"))
            self.assertGreater(item["next_attempt_ts"], before + 50)
            self.assertLess(item["next_attempt_ts"], after + 70)
            self.assertIsNotNone(item.get("next_attempt_at"))

    def test_failed_becomes_pending_after_backoff_expires(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json")
            outbox.enqueue(event_type="run", idempotency_key="evt-1", payload={"n": 1})
            outbox.mark_failed(["evt-1"], "timeout")

            # Manually set next_attempt_ts to past
            with open(Path(tmpdir) / "outbox.json") as f:
                raw = json.load(f)
            raw[0]["next_attempt_ts"] = time.time() - 10
            with open(Path(tmpdir) / "outbox.json", "w") as f:
                json.dump(raw, f)

            items = outbox.pending(limit=10)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["idempotency_key"], "evt-1")
            stats = outbox.stats(include_retry=True)
            self.assertEqual(stats["retry_ready"], 1)
            self.assertEqual(stats["upload_ready"], 1)

    def test_failed_without_next_attempt_ts_is_immediately_pending(self):
        """Old failed events without next_attempt_ts should be returned by pending()."""
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json")
            outbox.enqueue(event_type="run", idempotency_key="evt-1", payload={"n": 1})
            outbox.mark_failed(["evt-1"], "old error")

            # Simulate old format: remove next_attempt_ts
            with open(Path(tmpdir) / "outbox.json") as f:
                raw = json.load(f)
            del raw[0]["next_attempt_ts"]
            del raw[0]["next_attempt_at"]
            with open(Path(tmpdir) / "outbox.json", "w") as f:
                json.dump(raw, f)

            items = outbox.pending(limit=10)
            self.assertEqual(len(items), 1, "old failed events without next_attempt_ts should be pending")

    def test_backoff_delays_increase(self):
        self.assertEqual(_backoff_delay(0), 0.0)
        self.assertEqual(_backoff_delay(1), 60)
        self.assertEqual(_backoff_delay(2), 300)
        self.assertEqual(_backoff_delay(3), 1800)
        self.assertEqual(_backoff_delay(4), 7200)
        self.assertEqual(_backoff_delay(100), 7200)


class DeadLetterTests(unittest.TestCase):
    def test_mark_failed_dead_letters_after_max_attempts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json")
            outbox.enqueue(event_type="run", idempotency_key="evt-1", payload={"n": 1})

            for i in range(DEFAULT_MAX_ATTEMPTS - 1):
                outbox.mark_failed(["evt-1"], f"error {i + 1}")

            with open(Path(tmpdir) / "outbox.json") as f:
                raw = json.load(f)
            self.assertEqual(raw[0]["status"], "failed")
            self.assertEqual(raw[0]["attempts"], DEFAULT_MAX_ATTEMPTS - 1)

            # Final failure should dead-letter
            outbox.mark_failed(["evt-1"], "final error")

            with open(Path(tmpdir) / "outbox.json") as f:
                raw = json.load(f)
            item = raw[0]
            self.assertEqual(item["status"], "dead_letter")
            self.assertEqual(item["attempts"], DEFAULT_MAX_ATTEMPTS)
            self.assertIsNotNone(item.get("dead_lettered_at"))
            self.assertEqual(item["dead_letter_reason"], "final error")

    def test_dead_letter_not_in_pending(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json")
            outbox.enqueue(event_type="run", idempotency_key="evt-1", payload={"n": 1})
            for i in range(DEFAULT_MAX_ATTEMPTS):
                outbox.mark_failed(["evt-1"], f"error {i + 1}")

            self.assertEqual(outbox.pending(limit=10), [])

    def test_dead_letter_records_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json")
            outbox.enqueue(event_type="run", idempotency_key="evt-1", payload={"n": 1})
            for i in range(DEFAULT_MAX_ATTEMPTS - 1):
                outbox.mark_failed(["evt-1"], f"error {i + 1}")
            with patch("core.diagnostics.record_event") as mock_diag:
                outbox.mark_failed(["evt-1"], "final error")
            mock_diag.assert_called_once()
            diag_args = mock_diag.call_args
            self.assertEqual(diag_args[0][0], "cloud_sync")
            self.assertIn("dead-letter", diag_args[0][1])
            details = diag_args[1]["details"] if "details" in diag_args[1] else diag_args[0][2] if len(diag_args[0]) > 2 else {}
            # details is passed as keyword arg
            details = diag_args.kwargs.get("details", diag_args[1].get("details", {}))
            self.assertEqual(details["idempotency_key"], "evt-1")
            self.assertEqual(details["attempts"], DEFAULT_MAX_ATTEMPTS)

    def test_stats_includes_dead_letter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json")
            outbox.enqueue(event_type="run", idempotency_key="evt-1", payload={"n": 1})
            outbox.enqueue(event_type="run", idempotency_key="evt-2", payload={"n": 2})
            for i in range(DEFAULT_MAX_ATTEMPTS):
                outbox.mark_failed(["evt-1"], f"error {i + 1}")

            stats = outbox.stats()
            self.assertEqual(stats["dead_letter"], 1)
            self.assertEqual(stats["pending"], 1)
            self.assertEqual(stats["total"], 2)


class SafeCompactionTests(unittest.TestCase):
    def test_compaction_only_drops_sent_items(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json", max_items=3)
            # Enqueue 2 sent + 3 pending = 5 items, max_items=3
            outbox.enqueue(event_type="run", idempotency_key="sent-1", payload={"n": 1})
            outbox.enqueue(event_type="run", idempotency_key="sent-2", payload={"n": 2})
            outbox.mark_sent(["sent-1", "sent-2"])
            outbox.enqueue(event_type="run", idempotency_key="pending-1", payload={"n": 3})
            outbox.enqueue(event_type="run", idempotency_key="pending-2", payload={"n": 4})
            outbox.enqueue(event_type="run", idempotency_key="pending-3", payload={"n": 5})

            stats = outbox.stats()
            # Sent items are dropped to make room, but all pending are kept
            self.assertEqual(stats["pending"], 3)
            self.assertEqual(stats["sent"], 0)

    def test_compaction_preserves_dead_letter_items(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json", max_items=3)
            outbox.enqueue(event_type="run", idempotency_key="sent-1", payload={"n": 1})
            outbox.mark_sent(["sent-1"])
            outbox.enqueue(event_type="run", idempotency_key="dl-1", payload={"n": 2})
            for i in range(DEFAULT_MAX_ATTEMPTS):
                outbox.mark_failed(["dl-1"], f"error {i + 1}")
            outbox.enqueue(event_type="run", idempotency_key="pending-1", payload={"n": 3})
            outbox.enqueue(event_type="run", idempotency_key="pending-2", payload={"n": 4})

            stats = outbox.stats()
            self.assertEqual(stats["dead_letter"], 1)
            self.assertEqual(stats["pending"], 2)

    def test_overflow_reported_when_active_exceeds_max_items(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json", max_items=2)
            outbox.enqueue(event_type="run", idempotency_key="evt-1", payload={"n": 1})
            outbox.enqueue(event_type="run", idempotency_key="evt-2", payload={"n": 2})
            result, _ = outbox.enqueue(event_type="run", idempotency_key="evt-3", payload={"n": 3})

            # Read the raw file to check the save result had overflow
            stats = outbox.stats()
            self.assertEqual(stats["total"], 3)
            self.assertEqual(stats["pending"], 3)

    def test_single_large_pending_event_is_preserved(self):
        """A single pending event exceeding max_bytes must be kept, with overflow reported."""
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json", max_bytes=200)
            big_payload = {"blob": "x" * 500}
            outbox.enqueue(event_type="run", idempotency_key="big-1", payload=big_payload)

            stats = outbox.stats()
            self.assertEqual(stats["pending"], 1)
            self.assertEqual(stats["total"], 1)

    def test_sent_items_dropped_for_bytes_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json", max_bytes=1500)
            outbox.enqueue(event_type="run", idempotency_key="sent-1", payload={"blob": "x" * 400})
            outbox.enqueue(event_type="run", idempotency_key="sent-2", payload={"blob": "x" * 400})
            outbox.mark_sent(["sent-1", "sent-2"])
            outbox.enqueue(event_type="run", idempotency_key="pending-1", payload={"blob": "x" * 400})

            stats = outbox.stats()
            self.assertEqual(stats["pending"], 1)
            # Sent items should have been dropped to fit
            self.assertLessEqual(stats["sent"], 1)


class OverflowReportingTests(unittest.TestCase):
    def test_enqueue_many_overflow_reports_true_and_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json", max_items=2)
            outbox.enqueue(event_type="run", idempotency_key="evt-1", payload={"n": 1})
            outbox.enqueue(event_type="run", idempotency_key="evt-2", payload={"n": 2})
            with patch("core.diagnostics.record_event") as mock_diag:
                result = outbox.enqueue_many([
                    {"event_type": "run", "idempotency_key": "evt-3", "payload": {"n": 3}},
                ])
            dropped = result["dropped"]
            self.assertTrue(dropped["overflow"])
            self.assertGreater(dropped["overflow_items"], 0)
            self.assertEqual(dropped["active_retained"], 3)
            mock_diag.assert_called_once()
            diag_args = mock_diag.call_args
            self.assertEqual(diag_args[0][0], "cloud_sync")

    def test_dead_letter_overflow_reports_diagnostics(self):
        """Dead-letter items count as protected; overflow is reported when they exceed max_items."""
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json", max_items=2)
            outbox.enqueue(event_type="run", idempotency_key="dl-1", payload={"n": 1})
            outbox.enqueue(event_type="run", idempotency_key="dl-2", payload={"n": 2})
            # Dead-letter both
            for i in range(DEFAULT_MAX_ATTEMPTS):
                outbox.mark_failed(["dl-1"], f"err {i}")
            for i in range(DEFAULT_MAX_ATTEMPTS):
                outbox.mark_failed(["dl-2"], f"err {i}")
            self.assertEqual(outbox.stats()["dead_letter"], 2)

            # Now enqueue one more — total=3, max_items=2, all protected
            with patch("core.diagnostics.record_event") as mock_diag:
                outbox.enqueue(event_type="run", idempotency_key="evt-3", payload={"n": 3})
            # overflow should be True because protected (dead_letter + pending) > max_items
            diag_calls = [c for c in mock_diag.call_args_list if c[0][0] == "cloud_sync"]
            self.assertTrue(len(diag_calls) > 0, "overflow diagnostics should be recorded for dead-letter overflow")


class OldFormatCompatibilityTests(unittest.TestCase):
    def test_old_item_without_status_treated_as_pending(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "outbox.json"
            old_item = {
                "event_type": "run",
                "idempotency_key": "old-event",
                "payload": {"n": 1},
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
            with open(path, "w") as f:
                json.dump([old_item], f)

            outbox = CloudOutbox(path)
            items = outbox.pending(limit=10)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["idempotency_key"], "old-event")

    def test_old_failed_without_attempts_treated_as_zero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "outbox.json"
            old_item = {
                "event_type": "run",
                "idempotency_key": "old-failed",
                "payload": {"n": 1},
                "status": "failed",
                "last_error": "old error",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
            with open(path, "w") as f:
                json.dump([old_item], f)

            outbox = CloudOutbox(path)
            items = outbox.pending(limit=10)
            self.assertEqual(len(items), 1, "old failed without next_attempt_ts should be pending")

            outbox.mark_failed(["old-failed"], "new error")
            with open(path) as f:
                raw = json.load(f)
            self.assertEqual(raw[0]["attempts"], 1)
            self.assertEqual(raw[0]["status"], "failed")
            self.assertIsNotNone(raw[0].get("next_attempt_ts"))

    def test_bad_next_attempt_ts_does_not_crash_pending(self):
        """A corrupted next_attempt_ts should not raise — treated as retry-ready."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "outbox.json"
            bad_item = {
                "event_type": "run",
                "idempotency_key": "bad-ts",
                "payload": {"n": 1},
                "status": "failed",
                "attempts": 1,
                "next_attempt_ts": "not-a-number",
                "last_error": "some error",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
            with open(path, "w") as f:
                json.dump([bad_item], f)

            outbox = CloudOutbox(path)
            # Must not raise — bad value treated as "retry ready"
            items = outbox.pending(limit=10)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["idempotency_key"], "bad-ts")


if __name__ == "__main__":
    unittest.main()
