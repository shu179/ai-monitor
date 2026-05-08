import tempfile
import threading
import unittest
from pathlib import Path

from core.file_lock import CrossProcessRLock
from core.cloud_outbox import CloudOutbox


class CloudOutboxCompactionTests(unittest.TestCase):
    def test_uses_cross_process_lock_next_to_outbox_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "outbox.json"
            outbox = CloudOutbox(path)

            self.assertIsInstance(outbox._lock, CrossProcessRLock)
            outbox.enqueue(event_type="run", idempotency_key="event-1", payload={"n": 1})

            self.assertTrue(path.with_name("outbox.json.lock").exists())

    def test_two_instances_for_same_path_preserve_both_enqueues(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "outbox.json"
            start = threading.Event()
            errors = []

            def enqueue(key: str) -> None:
                try:
                    start.wait(timeout=2)
                    CloudOutbox(path).enqueue(event_type="run", idempotency_key=key, payload={"key": key})
                except Exception as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=enqueue, args=("event-1",)),
                threading.Thread(target=enqueue, args=("event-2",)),
            ]
            for thread in threads:
                thread.start()
            start.set()
            for thread in threads:
                thread.join(timeout=3)

            self.assertEqual(errors, [])
            keys = sorted(item["idempotency_key"] for item in CloudOutbox(path).pending(limit=10))
            self.assertEqual(keys, ["event-1", "event-2"])

    def test_compaction_drops_sent_items_before_pending_items(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json", max_items=3)
            outbox.enqueue(event_type="run", idempotency_key="sent-1", payload={"n": 1})
            outbox.enqueue(event_type="run", idempotency_key="sent-2", payload={"n": 2})
            outbox.enqueue(event_type="run", idempotency_key="pending-1", payload={"n": 3})
            outbox.mark_sent(["sent-1", "sent-2"])

            outbox.enqueue(event_type="run", idempotency_key="pending-2", payload={"n": 4})
            outbox.enqueue(event_type="run", idempotency_key="pending-3", payload={"n": 5})

            self.assertEqual(outbox.stats(), {"total": 3, "pending": 3, "failed": 0, "sent": 0})
            pending_keys = [item["idempotency_key"] for item in outbox.pending(limit=10)]
            self.assertEqual(pending_keys, ["pending-1", "pending-2", "pending-3"])

    def test_mark_failed_does_not_regress_sent_items(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json")
            outbox.enqueue(event_type="run", idempotency_key="event-1", payload={"n": 1})
            outbox.mark_sent(["event-1"])

            CloudOutbox(Path(tmpdir) / "outbox.json").mark_failed(["event-1"], "late failure")

            self.assertEqual(outbox.stats(), {"total": 1, "pending": 0, "failed": 0, "sent": 1})

    def test_sent_retention_drops_oldest_sent_items_below_total_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json", max_items=10, max_sent_items=2)
            outbox.enqueue(event_type="run", idempotency_key="sent-1", payload={"n": 1})
            outbox.enqueue(event_type="run", idempotency_key="sent-2", payload={"n": 2})
            outbox.enqueue(event_type="run", idempotency_key="sent-3", payload={"n": 3})
            outbox.mark_sent(["sent-1", "sent-2", "sent-3"])

            self.assertEqual(outbox.stats(), {"total": 2, "pending": 0, "failed": 0, "sent": 2})
            self.assertFalse(outbox.enqueue(event_type="run", idempotency_key="sent-2", payload={"n": 2})[1])
            self.assertFalse(outbox.enqueue(event_type="run", idempotency_key="sent-3", payload={"n": 3})[1])

    def test_max_items_drops_oldest_when_no_sent_items_exist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json", max_items=2)
            outbox.enqueue(event_type="run", idempotency_key="event-1", payload={"n": 1})
            outbox.enqueue(event_type="run", idempotency_key="event-2", payload={"n": 2})
            outbox.enqueue(event_type="run", idempotency_key="event-3", payload={"n": 3})

            pending_keys = [item["idempotency_key"] for item in outbox.pending(limit=10)]
            self.assertEqual(pending_keys, ["event-2", "event-3"])

    def test_max_bytes_drops_only_as_many_old_pending_items_as_needed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CloudOutbox(Path(tmpdir) / "outbox.json", max_bytes=1500)
            outbox.enqueue(event_type="run", idempotency_key="event-1", payload={"blob": "x" * 400})
            outbox.enqueue(event_type="run", idempotency_key="event-2", payload={"blob": "x" * 400})
            outbox.enqueue(event_type="run", idempotency_key="event-3", payload={"blob": "x" * 400})

            pending_keys = [item["idempotency_key"] for item in outbox.pending(limit=10)]
            self.assertEqual(pending_keys, ["event-2", "event-3"])


if __name__ == "__main__":
    unittest.main()
