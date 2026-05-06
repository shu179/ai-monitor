import tempfile
import unittest
from unittest.mock import patch

from core.browser_platform_factory import apply_browser_runtime_config
from platforms.base import BasePlatform
from platforms.deepseek import DeepSeekPlatform


class FakePlatform(BasePlatform):
    target_url = "https://example.test/chat"


class FakePage:
    url = "https://example.test/chat"


class FakeCDPSession:
    def __init__(self):
        self.commands = []

    def send(self, command):
        self.commands.append(command)


class FakeConnection:
    def __init__(self, session):
        self.session = session

    def new_browser_cdp_session(self):
        return self.session


class FakeProcess:
    pid = 12345

    def __init__(self):
        self.closed = False

    def poll(self):
        return 0 if self.closed else None

    def wait(self, timeout=None):
        self.closed = True
        return 0


class BrowserPollingAndCDPTests(unittest.TestCase):
    def test_answer_reads_scroll_once_per_scheduled_poll(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            platform = FakePlatform(tmpdir)

            self.assertTrue(platform._consume_answer_read_scroll())

            with patch("platforms.base.random.randint", return_value=5):
                platform._begin_answer_capture(keyword="keyword", brand="brand")
                platform._schedule_answer_poll_read()
                self.assertTrue(platform._consume_answer_read_scroll())
                self.assertFalse(platform._consume_answer_read_scroll())

                for _ in range(4):
                    platform._schedule_answer_poll_read()
                    self.assertFalse(platform._consume_answer_read_scroll())

                platform._schedule_answer_poll_read()
                self.assertTrue(platform._consume_answer_read_scroll())

    def test_overlay_scan_is_throttled_for_polling_checks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            platform = FakePlatform(tmpdir)
            platform.page = FakePage()
            calls = {"count": 0}

            def fake_scan():
                calls["count"] += 1
                return {"matched": False}

            platform._scan_blocking_overlay = fake_scan
            platform._collect_interruption_state(check_input_visible=False)
            platform._collect_interruption_state(check_input_visible=False)
            self.assertEqual(calls["count"], 1)

            platform._collect_interruption_state(check_input_visible=True)
            self.assertEqual(calls["count"], 2)

    def test_full_text_reads_are_cadenced_after_initial_polls(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            platform = FakePlatform(tmpdir)
            platform.debug_poll_metrics = True

            with patch("platforms.base.random.randint", return_value=4):
                platform._begin_answer_capture(keyword="keyword", brand="brand")

                platform._schedule_answer_poll_read()
                self.assertTrue(
                    platform._should_read_answer_text_for_poll(
                        elapsed=0.0,
                        min_wait=8,
                        has_cached_text=False,
                    )
                )
                platform._mark_answer_text_read_cadence(elapsed=0.0, min_wait=8)

                platform._schedule_answer_poll_read()
                self.assertTrue(
                    platform._should_read_answer_text_for_poll(
                        elapsed=1.0,
                        min_wait=8,
                        has_cached_text=True,
                    )
                )
                platform._mark_answer_text_read_cadence(elapsed=1.0, min_wait=8)

                platform._schedule_answer_poll_read()
                self.assertFalse(
                    platform._should_read_answer_text_for_poll(
                        elapsed=2.0,
                        min_wait=8,
                        has_cached_text=True,
                    )
                )

                platform._schedule_answer_poll_read()
                self.assertTrue(
                    platform._should_read_answer_text_for_poll(
                        elapsed=3.0,
                        min_wait=8,
                        has_cached_text=True,
                    )
                )

    def test_text_fallback_uses_tighter_but_cadenced_reads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            platform = FakePlatform(tmpdir)

            with patch("platforms.base.random.randint", return_value=3):
                platform._begin_answer_capture(keyword="keyword", brand="brand")
                platform._answer_poll_iteration = 20
                platform._mark_answer_text_read_cadence(
                    elapsed=30.0,
                    min_wait=8,
                    fallback_active=True,
                )

            self.assertEqual(platform._answer_next_full_text_poll, 23)

    def test_external_browser_close_prefers_cdp_browser_close(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            platform = FakePlatform(tmpdir)
            session = FakeCDPSession()
            process = FakeProcess()
            platform._browser_connection = FakeConnection(session)
            platform._external_browser_process = process
            platform._external_browser_port = 45678

            platform._terminate_external_browser_process()

            self.assertEqual(session.commands, ["Browser.close"])
            self.assertTrue(process.closed)
            self.assertIsNone(platform._external_browser_process)
            self.assertEqual(platform._external_browser_port, 0)

    def test_deepseek_keeps_automation_control_flag_enabled(self):
        self.assertTrue(DeepSeekPlatform.use_automation_control_flag)

    def test_poll_metrics_count_reads_scrolls_and_overlay_scans(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            platform = FakePlatform(tmpdir)
            platform.debug_poll_metrics = True
            platform.page = FakePage()
            calls = {"count": 0}

            def fake_scan():
                calls["count"] += 1
                return {"matched": False}

            platform._scan_blocking_overlay = fake_scan

            with patch("platforms.base.random.randint", return_value=5):
                platform._begin_answer_capture(keyword="keyword", brand="brand")
                platform._schedule_answer_poll_read()
                self.assertTrue(platform._consume_answer_read_scroll())
                platform._schedule_answer_poll_read()
                self.assertFalse(platform._consume_answer_read_scroll())

            platform._collect_interruption_state(check_input_visible=False)
            platform._collect_interruption_state(check_input_visible=False)
            platform._finish_answer_poll_metrics(
                outcome="ranked",
                final_text="品牌回答文本",
                keyword="keyword",
                brand="brand",
            )

            metrics = platform.last_answer_poll_metrics
            self.assertEqual(metrics["poll_schedules"], 2)
            self.assertEqual(metrics["answer_reads"], 2)
            self.assertEqual(metrics["answer_scrolls"], 1)
            self.assertEqual(metrics["overlay_checks"], 2)
            self.assertEqual(metrics["overlay_scans"], 1)
            self.assertEqual(metrics["overlay_scan_skips"], 1)
            self.assertEqual(metrics["outcome"], "ranked")
            self.assertGreater(metrics["final_answer_chars"], 0)

    def test_runtime_config_accepts_boolean_poll_metrics_flag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            platform = FakePlatform(tmpdir)

            apply_browser_runtime_config(
                platform,
                "fake",
                {"browser_automation": {"fake": {"debug_poll_metrics": True}}},
            )

            self.assertTrue(platform.debug_poll_metrics)


if __name__ == "__main__":
    unittest.main()
