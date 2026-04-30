import os
from pathlib import Path
import tempfile
import unittest

import core.daily_task_state as dts
import core.cycle_state as cycle_state
from core.notification_retry import (
    clear_retry_queue,
    enqueue_wecom_notification,
    load_retry_queue,
    retry_due_notifications,
    stop_notification_retry_worker,
)


class NotificationRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_data_dir = os.environ.get("AIBRANDMONITOR_DATA_DIR")
        self._original_state_path = dts.STATE_PATH
        self._original_cycle_path = cycle_state.STATE_PATH
        os.environ["AIBRANDMONITOR_DATA_DIR"] = self._tmpdir.name
        dts.STATE_PATH = Path(self._tmpdir.name) / "daily_task_status.json"
        cycle_state.STATE_PATH = Path(self._tmpdir.name) / "scheduler_cycle_state.json"
        clear_retry_queue()

    def tearDown(self) -> None:
        stop_notification_retry_worker()
        clear_retry_queue()
        dts.STATE_PATH = self._original_state_path
        cycle_state.STATE_PATH = self._original_cycle_path
        if self._original_data_dir is None:
            os.environ.pop("AIBRANDMONITOR_DATA_DIR", None)
        else:
            os.environ["AIBRANDMONITOR_DATA_DIR"] = self._original_data_dir
        self._tmpdir.cleanup()

    def _image_path(self, name: str = "shot.jpg") -> str:
        path = Path(self._tmpdir.name) / "screenshots" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-image")
        return str(path)

    def _payload(self, image_path: str | None = None) -> dict:
        screenshot_paths = [image_path or self._image_path()]
        return {
            "task": {
                "name": "品牌R",
                "task_id": "task_retry_r",
                "webhook_url": "https://example.invalid/webhook/test",
            },
            "daily_state_source": "recognition",
            "daily_state_scope": "official",
            "notifier": {
                "webhook_url": "https://example.invalid/webhook/test",
                "cooldown_minutes": 0,
                "send_interval": 0,
            },
            "send_args": {
                "task_name": "品牌R",
                "brands": ["品牌R"],
                "screenshot_paths": screenshot_paths,
                "detected_platforms": ["doubao"],
                "source": "识别模式",
                "completed_keywords": ["词R"],
                "supplemented_keywords": ["词R"],
                "total_screenshot_count": len(screenshot_paths),
            },
        }

    def test_enqueue_dedupes_same_notification_payload(self) -> None:
        payload = self._payload()

        first = enqueue_wecom_notification(payload, initial_delay_seconds=0, now=1000)
        second = enqueue_wecom_notification(payload, initial_delay_seconds=0, now=1001)

        entries = load_retry_queue()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["attempt_count"], 0)

    def test_retry_success_removes_entry_and_marks_daily_state_success(self) -> None:
        payload = self._payload()
        cycle_state.save_cycle_report(
            {
                "executed_rounds": [],
                "task_outcomes": [
                    {
                        "task_id": "task_retry_r",
                        "task_name": "品牌R",
                        "final_status": "failed",
                        "successful_brands": [],
                        "failed_query_details": [
                            {
                                "keyword": "词R",
                                "platform": "doubao",
                                "brand": "品牌R",
                                "error_message": "old failure",
                            }
                        ],
                        "task_failure_message": "old failure",
                        "executed_modes": ["browser"],
                    }
                ],
                "summary": {"total_tasks": 1, "success": 0, "failed": 1, "skipped": 0},
            }
        )
        enqueue_wecom_notification(payload, initial_delay_seconds=0, now=1000)
        calls: list[dict] = []

        class FakeNotifier:
            def __init__(self, *args, **kwargs):
                self.last_error = ""

            def send_detected_images(self, *args, **kwargs):
                calls.append(kwargs)
                return True

        stats = retry_due_notifications(limit=1, notifier_factory=FakeNotifier, now=1000)

        self.assertEqual(stats["succeeded"], 1)
        self.assertEqual(load_retry_queue(), [])
        self.assertEqual(calls[0]["screenshot_paths"], payload["send_args"]["screenshot_paths"])
        status = dts.get_task_day_status({"name": "品牌R", "task_id": "task_retry_r"})
        self.assertEqual(status.get("status"), "sent")
        self.assertTrue(status.get("sent_today"))
        cycle_payload = cycle_state.get_cycle_report()
        self.assertEqual((cycle_payload or {})["summary"]["success"], 1)
        self.assertEqual((cycle_payload or {})["task_outcomes"][0]["final_status"], "success")

    def test_retry_failure_keeps_entry_with_backoff(self) -> None:
        enqueue_wecom_notification(self._payload(), initial_delay_seconds=0, now=1000)

        class FakeNotifier:
            def __init__(self, *args, **kwargs):
                self.last_error = ""

            def send_detected_images(self, *args, **kwargs):
                self.last_error = "offline"
                return False

        stats = retry_due_notifications(limit=1, notifier_factory=FakeNotifier, now=1000)

        entries = load_retry_queue()
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["attempt_count"], 1)
        self.assertEqual(entries[0]["last_error"], "offline")
        self.assertEqual(entries[0]["next_attempt_ts"], 1060)

    def test_retry_disables_entry_when_generated_screenshot_is_missing(self) -> None:
        missing_path = str(Path(self._tmpdir.name) / "screenshots" / "missing.jpg")
        enqueue_wecom_notification(self._payload(missing_path), initial_delay_seconds=0, now=1000)
        calls: list[str] = []

        class FakeNotifier:
            def __init__(self, *args, **kwargs):
                self.last_error = ""

            def send_detected_images(self, *args, **kwargs):
                calls.append("called")
                return True

        stats = retry_due_notifications(limit=1, notifier_factory=FakeNotifier, now=1000)

        entries = load_retry_queue(include_inactive=True)
        self.assertEqual(stats["disabled"], 1)
        self.assertEqual(calls, [])
        self.assertEqual(entries[0]["status"], "disabled")
        self.assertIn("补发截图文件不存在", entries[0]["last_error"])


if __name__ == "__main__":
    unittest.main()
