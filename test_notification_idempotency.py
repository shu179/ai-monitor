import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import notification_idempotency


class NotificationIdempotencyTests(unittest.TestCase):
    def test_records_successful_notification_by_payload_hash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)

            def scoped_path(relative_path, *, fallback=None):
                return base / relative_path

            with patch.object(notification_idempotency, "account_scoped_path", scoped_path):
                payload_hash = notification_idempotency.build_payload_hash({"brand": "Acme"})
                identity = {
                    "webhook_url": "https://example.com/webhook?key=secret",
                    "task_id": "task-1",
                    "task_name": "Acme",
                    "channel": "single_query",
                    "run_date": "2026-05-08",
                    "round_id": "single_query:task-1:2026-05-08",
                    "payload_hash": payload_hash,
                }

                self.assertFalse(notification_idempotency.notification_already_sent(**identity))
                notification_idempotency.record_notification_sent(**identity)
                self.assertTrue(notification_idempotency.notification_already_sent(**identity))

                changed = dict(identity)
                changed["payload_hash"] = notification_idempotency.build_payload_hash({"brand": "Acme", "count": 2})
                self.assertFalse(notification_idempotency.notification_already_sent(**changed))


if __name__ == "__main__":
    unittest.main()
