import tempfile
import unittest
from unittest.mock import patch

from core.browser_platform_factory import apply_browser_runtime_config
from platforms.base import BasePlatform
from platforms.deepseek import DeepSeekPlatform
from platforms.doubao import DoubaoPlatform
from platforms.tongyi import TongyiPlatform
from platforms.yuanbao import YuanbaoPlatform


class FakePlatform(BasePlatform):
    target_url = "https://example.test/chat"


class FakePage:
    url = "https://example.test/chat"


class FakeMouse:
    def __init__(self):
        self.moves = []
        self.wheels = []

    def move(self, x, y):
        self.moves.append((x, y))

    def wheel(self, delta_x, delta_y):
        self.wheels.append((delta_x, delta_y))


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

            self.assertFalse(platform._consume_answer_read_scroll())

            with patch("platforms.base.random.randint", return_value=5):
                platform._begin_answer_capture(keyword="keyword", brand="brand")
                platform._schedule_answer_poll_read()
                self.assertFalse(platform._consume_answer_read_scroll())
                self.assertFalse(platform._consume_answer_read_scroll())

                for _ in range(4):
                    platform._schedule_answer_poll_read()
                    self.assertFalse(platform._consume_answer_read_scroll())

                platform._schedule_answer_poll_read()
                self.assertFalse(platform._consume_answer_read_scroll())

    def test_answer_scroll_uses_virtual_wheel_without_js_scroll_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            platform = FakePlatform(tmpdir)
            mouse = FakeMouse()
            platform.page = type("WheelPage", (), {"mouse": mouse})()
            platform._answer_scroll_target_point = lambda: {"x": 120, "y": 240}  # type: ignore[method-assign]

            with patch("platforms.base.random.randint", return_value=900), patch("platforms.base.random.uniform", return_value=0):
                platform._begin_answer_capture(keyword="keyword", brand="brand")
                platform._schedule_answer_poll_read()
                should_js_scroll = platform._consume_answer_read_scroll()

            self.assertFalse(should_js_scroll)
            self.assertEqual(mouse.moves, [(120.0, 240.0)])
            self.assertEqual(mouse.wheels, [(0, 900)])

    def test_auxiliary_wheel_profile_splits_large_scrolls_into_multiple_pulses(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            platform = FakePlatform(tmpdir)

            with (
                patch("platforms.base.random.uniform", side_effect=[0.3, 0.36, 0.5]),
                patch("platforms.base.random.randint", return_value=360),
                patch("platforms.base.random.random", return_value=0.7),
            ):
                deltas = platform._build_auxiliary_wheel_deltas(remaining=1800)

        self.assertEqual(deltas, [130, 115, 115])

    def test_auxiliary_wheel_profile_keeps_short_scrolls_small(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            platform = FakePlatform(tmpdir)

            with (
                patch("platforms.base.random.uniform", return_value=0.4),
                patch("platforms.base.random.randint", return_value=220),
                patch("platforms.base.random.random", return_value=0.95),
            ):
                deltas = platform._build_auxiliary_wheel_deltas(remaining=260)

        self.assertEqual(deltas, [104])

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
                self.assertFalse(platform._consume_answer_read_scroll())
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

    def test_without_answer_read_scroll_can_clear_pending_scroll(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            platform = FakePlatform(tmpdir)
            platform._answer_scroll_reads_remaining = 1

            with platform._without_answer_read_scroll(restore_pending=False):
                self.assertFalse(platform._consume_answer_read_scroll())

            self.assertEqual(platform._answer_scroll_reads_remaining, 0)

    def test_late_brand_grace_capture_does_not_scroll(self):
        class NoScrollGracePlatform(FakePlatform):
            def __init__(self, user_data_dir: str):
                super().__init__(user_data_dir)
                self.wheel_calls = 0

            def check_for_interruption(self):
                return None

            def _wheel_answer_view(self, *args, **kwargs):
                self.wheel_calls += 1
                return True

            def _capture_answer_snapshot(self):
                self._consume_answer_read_scroll()
                text = "TO 直燃炉厂家推荐\n可迪尔空气技术（北京）有限公司"
                return {
                    "root_key": "",
                    "blocks": [{"key": "answer:0", "order": 0, "text": text, "html": ""}],
                    "raw_text": text,
                    "raw_html": "",
                }

        with tempfile.TemporaryDirectory() as tmpdir:
            platform = NoScrollGracePlatform(tmpdir)
            platform._begin_answer_capture(keyword="TO直燃炉厂家推荐", brand="可迪尔")
            platform._answer_scroll_reads_remaining = 1

            text = platform._capture_late_brand_mention_grace_text(
                lambda: "",
                answer_text="TO 直燃炉厂家推荐",
                keyword="TO直燃炉厂家推荐",
                brand="可迪尔",
            )

            self.assertIn("可迪尔空气技术", text)
            self.assertEqual(platform.wheel_calls, 0)
            self.assertEqual(platform._answer_scroll_reads_remaining, 0)

    def test_doubao_completion_uses_short_dom_only_confirmation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            platform = DoubaoPlatform(tmpdir)

            self.assertLess(platform.generation_min_wait_seconds, BasePlatform.generation_min_wait_seconds)
            self.assertLess(
                platform.generation_completion_confirm_max_seconds,
                BasePlatform.generation_completion_confirm_max_seconds,
            )
            self.assertFalse(platform.generation_completion_confirm_force_scroll)
            self.assertFalse(platform.generation_post_complete_scroll_reads)
            self.assertTrue(platform.generation_defer_initial_scroll_until_new_content)

    def test_yuanbao_and_tongyi_use_same_safe_polling_profile(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            for platform in (YuanbaoPlatform(tmpdir), TongyiPlatform(tmpdir)):
                self.assertLess(platform.generation_min_wait_seconds, BasePlatform.generation_min_wait_seconds)
                self.assertLess(
                    platform.generation_completion_confirm_max_seconds,
                    BasePlatform.generation_completion_confirm_max_seconds,
                )
                self.assertFalse(platform.generation_completion_confirm_force_scroll)
                self.assertFalse(platform.generation_post_complete_scroll_reads)
                self.assertTrue(platform.generation_defer_initial_scroll_until_new_content)

    def test_initial_scroll_can_be_deferred_until_new_answer_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            platform = FakePlatform(tmpdir)
            platform.generation_defer_initial_scroll_until_new_content = True
            platform.page = type("WheelPage", (), {"mouse": FakeMouse()})()
            platform._answer_scroll_target_point = lambda: {"x": 120, "y": 240}  # type: ignore[method-assign]

            with patch("platforms.base.random.randint", return_value=5):
                platform._begin_answer_capture(keyword="keyword", brand="brand")
                platform._schedule_answer_poll_read()
                self.assertEqual(platform._answer_scroll_reads_remaining, 0)
                self.assertFalse(platform._consume_answer_read_scroll())
                self.assertFalse(platform._answer_first_content_scroll_done)

                self.assertTrue(platform._perform_deferred_first_content_scroll())

            self.assertEqual(platform.page.mouse.wheels, [(0, 5)])
            self.assertEqual(platform._answer_scroll_reads_remaining, 0)

    def test_yuanbao_blocks_text_stable_completion_while_streaming(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            platform = YuanbaoPlatform(tmpdir)
            self.assertFalse(
                platform._allow_text_stable_completion(
                    {
                        "stop_visible": False,
                        "loading_visible": False,
                        "stream_icon_visible": True,
                    }
                )
            )

    def test_yuanbao_requires_two_inactive_stable_reads_before_finalize(self):
        class FakeYuanbaoPlatform(YuanbaoPlatform):
            def __init__(self, user_data_dir: str):
                super().__init__(user_data_dir)
                self._debug_states = [
                    {"stop_visible": False, "loading_visible": False, "stream_icon_visible": True},
                    {"stop_visible": False, "loading_visible": False, "stream_icon_visible": True},
                    {"stop_visible": False, "loading_visible": False, "stream_icon_visible": False},
                    {"stop_visible": False, "loading_visible": False, "stream_icon_visible": False},
                ]
                self._texts = [
                    "3. 深圳华南3D打印、增材制造展览会（TCT Shenzhen）",
                    "3. 深圳华南3D打印、增材制造展览会（TCT Shenzhen）",
                    "3. 深圳华南3D打印、增材制造展览会（TCT Shenzhen）\n更多内容",
                    "3. 深圳华南3D打印、增材制造展览会（TCT Shenzhen）\n更多内容",
                ]

            def check_for_interruption(self):
                return None

            def _cooperative_sleep(self, seconds):
                return None

            def _capture_answer_snapshot(self):
                text = self._texts.pop(0) if self._texts else ""
                return {
                    "root_key": "",
                    "blocks": [{"key": "answer:0", "order": 0, "text": text, "html": ""}] if text else [],
                    "raw_text": text,
                    "raw_html": "",
                }

            def _get_generation_debug_state(self):
                return self._debug_states.pop(0) if self._debug_states else {
                    "stop_visible": False,
                    "loading_visible": False,
                    "stream_icon_visible": False,
                }

        with tempfile.TemporaryDirectory() as tmpdir:
            platform = FakeYuanbaoPlatform(tmpdir)
            platform._begin_answer_capture(keyword="3D打印展会", brand="即搜")

            text, usable = platform._get_stable_answer_text(
                lambda: "3. 深圳华南3D打印、增材制造展览会（TCT Shenzhen）",
                keyword="3D打印展会",
                brand="即搜",
                attempts=4,
                interval=0.01,
            )

            self.assertTrue(usable)
            self.assertIn("更多内容", text)

    def test_poll_until_complete_uses_brand_grace_window_for_late_table_rows(self):
        class LateBrandPlatform(FakePlatform):
            def __init__(self, user_data_dir: str):
                super().__init__(user_data_dir)
                self._snapshots = [
                    "TO 直燃炉厂家推荐（工业废气处理专用）\n以下是国内优质 TO 直燃炉厂家推荐。",
                    "TO 直燃炉厂家推荐（工业废气处理专用）\n以下是国内优质 TO 直燃炉厂家推荐。",
                    "TO 直燃炉厂家推荐（工业废气处理专用）\n以下是国内优质 TO 直燃炉厂家推荐。",
                    "TO 直燃炉厂家推荐（工业废气处理专用）\n以下是国内优质 TO 直燃炉厂家推荐。\n可迪尔空气技术（北京）有限公司\n3 大生产基地，服务全球客户。",
                ]

            def check_for_interruption(self):
                return None

            def _cooperative_sleep(self, seconds):
                return None

            def _cooperative_sleep_jittered(self, seconds, spread=0.0):
                return None

            def is_generation_complete(self, page_text: str, start_time: float) -> bool:
                del page_text, start_time
                return True

            def _capture_answer_snapshot(self):
                text = self._snapshots.pop(0) if self._snapshots else ""
                return {
                    "root_key": "",
                    "blocks": [{"key": "answer:0", "order": 0, "text": text, "html": ""}] if text else [],
                    "raw_text": text,
                    "raw_html": "",
                }

        with tempfile.TemporaryDirectory() as tmpdir:
            platform = LateBrandPlatform(tmpdir)
            ranked = []

            platform._poll_until_complete(
                "可迪尔",
                lambda rank, text: ranked.append((rank, text)),
                get_text=lambda: "",
                timeout=2,
                min_wait=0,
                keyword="TO直燃炉厂家推荐",
            )

            self.assertEqual(len(ranked), 1)
            self.assertEqual(ranked[0][0], 99)
            self.assertIn("可迪尔空气技术", ranked[0][1])


if __name__ == "__main__":
    unittest.main()
