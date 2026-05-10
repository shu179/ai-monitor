"""Tests for atomic cooldown reservation preventing concurrent duplicate sends."""

from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from core.notifier import WeComNotifier


class CooldownRaceTests(unittest.TestCase):
    def setUp(self) -> None:
        with WeComNotifier._shared_lock:
            WeComNotifier._shared_last_sent.clear()
            WeComNotifier._shared_pending.clear()
        with WeComNotifier._shared_post_lock:
            WeComNotifier._shared_last_post.clear()
            WeComNotifier._shared_post_locks.clear()
        self.webhook_url = f"https://example.invalid/wecom-test/{uuid4().hex}"

    def _make_notifier(self, cooldown_minutes: int = 30) -> WeComNotifier:
        return WeComNotifier(
            webhook_url=self.webhook_url,
            cooldown_minutes=cooldown_minutes,
            send_interval=0,
        )

    def test_concurrent_send_only_one_enters(self) -> None:
        """Two threads calling send() simultaneously for the same key — only one should succeed."""
        notifier_a = self._make_notifier()
        notifier_b = self._make_notifier()

        send_barrier = threading.Barrier(2, timeout=5)
        results: dict[str, bool] = {}
        send_count = {"value": 0}
        send_count_lock = threading.Lock()

        original_send_text = WeComNotifier._send_text

        def counting_send_text(self_inner, message: str) -> bool:
            with send_count_lock:
                send_count["value"] += 1
            # Wait here so both threads try to send at the same time
            try:
                send_barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass
            return True

        def thread_fn(name: str, notifier: WeComNotifier) -> None:
            with patch.object(WeComNotifier, "_send_text", counting_send_text):
                result = notifier.send(
                    platform="doubao",
                    keyword="关键词A",
                    brand="品牌A",
                )
                results[name] = result

        t1 = threading.Thread(target=thread_fn, args=("a", notifier_a))
        t2 = threading.Thread(target=thread_fn, args=("b", notifier_b))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        # Both threads got True (send succeeded), but only one actually sent
        # because the second should be blocked by the pending reservation
        self.assertTrue(results.get("a") or results.get("b"))
        # The key invariant: only one _send_text call should have been made
        # (the second thread should have been rejected by _reserve_cooldown)
        self.assertEqual(send_count["value"], 1, "Expected exactly one send, got duplicate")

    def test_failed_send_releases_reservation(self) -> None:
        """After a failed send, the reservation is released and a retry can proceed."""
        notifier = self._make_notifier()

        with patch.object(WeComNotifier, "_send_text", return_value=False):
            result = notifier.send(platform="doubao", keyword="关键词A", brand="品牌A")

        self.assertFalse(result)
        # Reservation should be released — no pending marker
        key = notifier._cooldown_key("doubao", "品牌A", "关键词A")
        self.assertNotIn(key, WeComNotifier._shared_pending)

        # A subsequent call should be able to reserve again
        with patch.object(WeComNotifier, "_send_text", return_value=True):
            result2 = notifier.send(platform="doubao", keyword="关键词A", brand="品牌A")

        self.assertTrue(result2)

    def test_successful_send_sets_cooldown(self) -> None:
        """After a successful send, the cooldown prevents immediate re-send."""
        notifier = self._make_notifier(cooldown_minutes=30)

        with patch.object(WeComNotifier, "_send_text", return_value=True):
            notifier.send(platform="doubao", keyword="关键词A", brand="品牌A")

        # Pending should be cleared
        key = notifier._cooldown_key("doubao", "品牌A", "关键词A")
        self.assertNotIn(key, WeComNotifier._shared_pending)
        # But cooldown should be active
        self.assertFalse(notifier.should_notify("doubao", "品牌A", "关键词A"))
        self.assertEqual(notifier.last_skip_reason, "cooldown")

    def test_pending_reservation_blocks_second_thread(self) -> None:
        """While one send is in-flight, another thread cannot reserve the same key."""
        notifier = self._make_notifier()
        key = notifier._cooldown_key("doubao", "品牌A", "关键词A")

        # Simulate an in-flight send by manually setting pending
        with WeComNotifier._shared_lock:
            WeComNotifier._shared_pending[key] = True

        # Second thread should be blocked
        result = notifier._reserve_cooldown("doubao", "品牌A", "关键词A")
        self.assertFalse(result)
        self.assertEqual(notifier.last_skip_reason, "pending")

    def test_bypass_cooldown_skips_reservation(self) -> None:
        """bypass_cooldown=True skips the reservation entirely."""
        notifier = self._make_notifier()

        # Mark as pending — normal send would be blocked
        key = notifier._cooldown_key("doubao", "品牌A", "关键词A")
        with WeComNotifier._shared_lock:
            WeComNotifier._shared_pending[key] = True

        with patch.object(WeComNotifier, "_send_text", return_value=True):
            result = notifier.send(
                platform="doubao",
                keyword="关键词A",
                brand="品牌A",
                bypass_cooldown=True,
            )

        self.assertTrue(result)
        # Pending from another thread must NOT be cleared by a bypass send
        self.assertIn(key, WeComNotifier._shared_pending)

    def test_bypass_send_does_not_clear_other_threads_pending(self) -> None:
        """bypass_cooldown send on success/failure must not corrupt another thread's pending."""
        notifier = self._make_notifier()
        key = notifier._cooldown_key("doubao", "品牌A", "关键词A")

        # Simulate another thread's in-flight reservation
        with WeComNotifier._shared_lock:
            WeComNotifier._shared_pending[key] = True

        # Bypass send succeeds — should NOT clear the other thread's pending
        with patch.object(WeComNotifier, "_send_text", return_value=True):
            notifier.send(platform="doubao", keyword="关键词A", brand="品牌A", bypass_cooldown=True)
        self.assertIn(key, WeComNotifier._shared_pending)

        # Bypass send fails — should also NOT clear pending
        with patch.object(WeComNotifier, "_send_text", return_value=False):
            notifier.send(platform="doubao", keyword="关键词A", brand="品牌A", bypass_cooldown=True)
        self.assertIn(key, WeComNotifier._shared_pending)


if __name__ == "__main__":
    unittest.main()
