from __future__ import annotations

import unittest
from unittest.mock import patch

from core.notifier import WeComNotifier


class NotifierTimeSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        WeComNotifier._shared_last_sent = {}
        WeComNotifier._shared_pending = {}
        WeComNotifier._shared_last_post = {}
        WeComNotifier._shared_post_locks = {}

    def test_cooldown_uses_monotonic_time(self) -> None:
        notifier = WeComNotifier(
            webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=PLACEHOLDER",
            cooldown_minutes=1,
            send_interval=0,
        )

        with patch("core.notifier.time.monotonic", side_effect=[100.0, 100.5, 161.0]):
            notifier._mark_sent("doubao", "品牌A", "关键词A")
            self.assertFalse(notifier.should_notify("doubao", "品牌A", "关键词A"))
            self.assertTrue(notifier.should_notify("doubao", "品牌A", "关键词A"))

    def test_post_interval_uses_monotonic_time(self) -> None:
        notifier = WeComNotifier(
            webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=PLACEHOLDER",
            cooldown_minutes=0,
            send_interval=2,
        )

        with patch("core.notifier.time.monotonic", side_effect=[100.0, 100.25]):
            with patch("core.notifier.time.sleep") as sleep_mock:
                notifier._mark_post_sent_unlocked()
                notifier._wait_for_post_slot_unlocked()

        sleep_mock.assert_called_once()
        self.assertAlmostEqual(float(sleep_mock.call_args.args[0]), 1.75)


if __name__ == "__main__":
    unittest.main()
