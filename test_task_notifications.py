from pathlib import Path
import tempfile
import unittest

import main
from core.task_notifications import (
    build_notification_result,
    send_task_notifications,
)


class FakeNotifier:
    def __init__(self, *, ok: bool = True, skip_reason: str = "") -> None:
        self.ok = ok
        self.last_error = "" if ok else "send failed"
        self.last_skip_reason = skip_reason
        self.sent = []

    def send(self, **kwargs):
        self.sent.append(("single", kwargs))
        return self.ok

    def send_summary(self, **kwargs):
        self.sent.append(("summary", kwargs))
        return self.ok

    def send_detected_images(self, **kwargs):
        self.sent.append(("fixed", kwargs))
        return self.ok


class TaskNotificationsTests(unittest.TestCase):
    def test_main_compatibility_exports_point_to_notification_helpers(self):
        self.assertIs(main._build_notification_result, build_notification_result)
        self.assertIs(main._send_task_notifications, send_task_notifications)

    def test_multi_query_waits_until_every_query_has_success_screenshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            screenshot = Path(tmpdir) / "hit.jpg"
            screenshot.write_bytes(b"fake-image")
            notifier = FakeNotifier()

            result = send_task_notifications(
                {"name": "品牌A"},
                "品牌A",
                notifier,
                [{"keyword": "词1", "brand": "品牌A", "platforms": ["doubao", "kimi"]}],
                [
                    {
                        "keyword": "词1",
                        "platform": "doubao",
                        "brand": "品牌A",
                        "rank": 1,
                        "screenshot": str(screenshot),
                    }
                ],
                expected_query_count=2,
            )

        self.assertFalse(result["attempted"])
        self.assertFalse(result["success"])
        self.assertIn("任务组未补齐", result["error_message"])
        self.assertEqual(notifier.sent, [])

    def test_missing_notifier_reports_hit_without_sending(self):
        result = send_task_notifications(
            {"name": "品牌A"},
            "品牌A",
            None,
            [{"keyword": "词1", "brand": "品牌A", "platforms": ["doubao"]}],
            [{"keyword": "词1", "platform": "doubao", "brand": "品牌A", "rank": 1}],
            expected_query_count=1,
        )

        self.assertTrue(result["attempted"])
        self.assertFalse(result["success"])
        self.assertEqual(result["found_results"], 1)
        self.assertEqual(result["error_message"], "未配置有效 webhook")

    def test_fixed_screenshot_task_waits_for_ready_quota(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            screenshot = Path(tmpdir) / "fixed.jpg"
            screenshot.write_bytes(b"fake-image")
            hit = {
                "keyword": "词1",
                "platform": "doubao",
                "brand": "品牌A",
                "rank": 1,
                "screenshot": str(screenshot),
            }
            notifier = FakeNotifier()

            result = send_task_notifications(
                {"name": "品牌A", "fixed_screenshot_enabled": True},
                "品牌A",
                notifier,
                [{"keyword": "词1", "brand": "品牌A", "platforms": ["doubao"]}],
                [hit],
                selected_results=[hit],
                fixed_screenshot_target=2,
                fixed_screenshot_ready=False,
                expected_query_count=1,
            )

        self.assertFalse(result["attempted"])
        self.assertFalse(result["success"])
        self.assertEqual(result["found_results"], 1)
        self.assertIn("截图未补满", result["error_message"])
        self.assertEqual(notifier.sent, [])

    def test_force_single_query_notification_sends_with_bypass_cooldown(self):
        notifier = FakeNotifier()

        result = send_task_notifications(
            {"name": "品牌A", "task_id": "task-a"},
            "品牌A",
            notifier,
            [{"keyword": "词1", "brand": "品牌A", "platforms": ["doubao"]}],
            [{"keyword": "词1", "platform": "doubao", "brand": "品牌A", "rank": 1}],
            expected_query_count=1,
            force_notify=True,
        )

        self.assertTrue(result["attempted"])
        self.assertTrue(result["success"])
        self.assertEqual(notifier.sent[0][0], "single")
        self.assertTrue(notifier.sent[0][1]["bypass_cooldown"])

    def test_multi_query_sends_summary_when_complete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            first = Path(tmpdir) / "first.jpg"
            second = Path(tmpdir) / "second.jpg"
            first.write_bytes(b"fake-image")
            second.write_bytes(b"fake-image")
            notifier = FakeNotifier()

            result = send_task_notifications(
                {"name": "品牌A"},
                "品牌A",
                notifier,
                [{"keyword": "词1", "brand": "品牌A", "platforms": ["doubao", "kimi"]}],
                [
                    {"keyword": "词1", "platform": "doubao", "brand": "品牌A", "rank": 1, "screenshot": str(first)},
                    {"keyword": "词1", "platform": "kimi", "brand": "品牌A", "rank": 1, "screenshot": str(second)},
                ],
                expected_query_count=2,
            )

        self.assertTrue(result["attempted"])
        self.assertTrue(result["success"])
        self.assertEqual(result["found_results"], 2)
        self.assertEqual(notifier.sent[0][0], "summary")


if __name__ == "__main__":
    unittest.main()
