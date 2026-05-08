import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import cycle_state
from core import notification_idempotency
from core.scheduler_notifications import SchedulerWebhookReporter


class FakeNotifier:
    last_error = ""

    def __init__(self, sent_messages: list[str]) -> None:
        self._sent_messages = sent_messages

    def send_text_message(self, content: str) -> bool:
        self._sent_messages.append(content)
        return True


class SchedulerNotificationIdempotencyTests(unittest.TestCase):
    def _scoped_idempotency_db(self, tmpdir: str):
        base = Path(tmpdir)

        def scoped_path(relative_path, *, fallback=None):
            return base / relative_path

        return patch.object(notification_idempotency, "account_scoped_path", scoped_path)

    def _config(self) -> dict:
        return {
            "scheduler": {
                "notification_webhook_url": "https://example.com/webhook?key=scheduler",
            },
            "default_notification": {
                "failure_alert_threshold": 1,
                "failure_alert_cooldown_minutes": 5,
                "send_interval": 0,
            },
        }

    def test_force_issue_is_idempotent_across_reporter_instances(self) -> None:
        sent_messages: list[str] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            with self._scoped_idempotency_db(tmpdir):
                with patch(
                    "core.scheduler_notifications._build_scheduler_notifier",
                    return_value=FakeNotifier(sent_messages),
                ):
                    first = SchedulerWebhookReporter(lambda: self._config())
                    second = SchedulerWebhookReporter(lambda: self._config())

                    self.assertTrue(first.send_issue("调度器异常", "同一个错误", force=True))
                    self.assertTrue(second.send_issue("调度器异常", "同一个错误", force=True))

        self.assertEqual(len(sent_messages), 1)
        self.assertIn("同一个错误", sent_messages[0])

    def test_mode_summary_is_idempotent_for_same_payload(self) -> None:
        sent_messages: list[str] = []

        payload = {
            "mode": "browser",
            "mode_label": "抓取模式",
            "summary": {"success": 1, "failed": 0, "skipped": 0},
            "reports": [
                {
                    "task_id": "task-a",
                    "task_name": "品牌A",
                    "task_status": "success",
                    "successful_brands": ["品牌A"],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with self._scoped_idempotency_db(tmpdir):
                with patch(
                    "core.scheduler_notifications._build_scheduler_notifier",
                    return_value=FakeNotifier(sent_messages),
                ):
                    reporter = SchedulerWebhookReporter(lambda: self._config())

                    self.assertTrue(reporter.send_mode_summary(payload))
                    self.assertTrue(reporter.send_mode_summary(payload))

        self.assertEqual(len(sent_messages), 1)
        self.assertIn("【自动监控模式汇总】抓取模式", sent_messages[0])

    def test_cycle_summary_and_all_success_are_idempotent_for_same_payload(self) -> None:
        sent_messages: list[str] = []
        payload = {
            "date": "2026-05-08",
            "scheduled_time": "09:30",
            "summary": {"total_tasks": 1, "success": 1, "failed": 0, "skipped": 0},
            "task_outcomes": [
                {
                    "task_id": "task-a",
                    "task_name": "品牌A",
                    "final_status": "success",
                    "successful_brands": ["品牌A"],
                    "failed_query_details": [],
                }
            ],
            "executed_rounds": [{"mode": "browser", "mode_label": "抓取模式"}],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            original_cycle_path = cycle_state.STATE_PATH
            cycle_state.STATE_PATH = Path(tmpdir) / "scheduler_cycle_state.json"
            try:
                with self._scoped_idempotency_db(tmpdir):
                    with patch(
                        "core.scheduler_notifications._build_scheduler_notifier",
                        return_value=FakeNotifier(sent_messages),
                    ):
                        first = SchedulerWebhookReporter(lambda: self._config())
                        second = SchedulerWebhookReporter(lambda: self._config())

                        self.assertTrue(first.send_cycle_summary(payload))
                        self.assertTrue(second.send_cycle_summary(payload))
            finally:
                cycle_state.STATE_PATH = original_cycle_path

        self.assertEqual(len(sent_messages), 2)
        self.assertIn("【自动监控整轮汇总】", sent_messages[0])
        self.assertIn("当日任务已全部完成", sent_messages[1])


if __name__ == "__main__":
    unittest.main()
