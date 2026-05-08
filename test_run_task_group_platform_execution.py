import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.daily_task_state as dts
import main
from core import task_executor_impl
from platforms.base import SchedulerStopRequested


class _FakePlatform:
    def __init__(self, platform_name: str, screenshot_dir: Path, calls: list[str]) -> None:
        self.platform_name = platform_name
        self._screenshot_dir = screenshot_dir
        self._calls = calls
        self.page = object()
        self.last_error = ""
        self.last_answer_text = ""
        self.last_screenshot_meta = {"highlight_count": 1}
        self.last_references = []
        self.last_body_references = []
        self.last_run_recovered_manually = False
        self.stop_checker = None

    def start(self):
        return self

    def close(self) -> None:
        return None

    def search(self, keyword: str, brand: str) -> tuple[int, str]:
        self._calls.append(self.platform_name)
        if callable(self.stop_checker) and self.stop_checker():
            self.last_error = "定时任务已关闭，当前浏览器查询已终止"
            raise SchedulerStopRequested(self.last_error)
        screenshot_path = self._screenshot_dir / f"{keyword}_{self.platform_name}.jpg"
        screenshot_path.write_bytes(b"fake-image")
        self.last_answer_text = f"{brand} mention on {self.platform_name}"
        return 1, str(screenshot_path)


class _FakeSmartPlatform(_FakePlatform):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def ensure_logged_in(self, timeout=15) -> None:
        del timeout
        return None

    def start_new_chat(self) -> None:
        return None

    def enable_deep_think(self) -> None:
        return None

    def _get_answer_text(self) -> str:
        return self.last_answer_text

    def type_like_human(self, keyword: str) -> None:
        self._last_keyword = keyword

    def submit_prompt(self) -> None:
        return None

    def _poll_until_complete(self, brand: str, on_complete, **_kwargs) -> None:
        self.last_answer_text = f"{brand} appears in smart answer"
        on_complete(None, self.last_answer_text)

    def has_usable_answer_text(self, final_text: str, **_kwargs) -> bool:
        return bool(final_text)

    def _has_new_answer_content(self, final_text: str, **_kwargs) -> bool:
        return bool(final_text)

    def detect_brand_mention(self, final_text: str, brand: str, **_kwargs):
        return brand in final_text, brand, {"normalized_excerpt": brand}

    def _take_long_screenshot_with_retries(self, brand: str = "") -> str:
        screenshot_path = self._screenshot_dir / f"smart_{brand}_{self.platform_name}.jpg"
        screenshot_path.write_bytes(b"smart-image")
        self.last_screenshot_meta = {"highlight_count": 2}
        return str(screenshot_path)


class RunTaskGroupPlatformExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_state_path = dts.STATE_PATH
        self._original_data_dir = os.environ.get("AIBRANDMONITOR_DATA_DIR")
        os.environ["AIBRANDMONITOR_DATA_DIR"] = self._tmpdir.name
        dts.STATE_PATH = Path(self._tmpdir.name) / "daily_task_status.json"
        self._screenshot_dir = Path(self._tmpdir.name) / "raw_screenshots"
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.calls: list[str] = []

    def tearDown(self) -> None:
        dts.STATE_PATH = self._original_state_path
        if self._original_data_dir is None:
            os.environ.pop("AIBRANDMONITOR_DATA_DIR", None)
        else:
            os.environ["AIBRANDMONITOR_DATA_DIR"] = self._original_data_dir
        self._tmpdir.cleanup()

    def _task(self) -> dict:
        return {
            "name": "品牌平台执行测试",
            "task_id": "brand_platform_execution",
            "keywords": [
                {
                    "keyword": "品牌A 评测",
                    "brand": "品牌A",
                    "platforms": ["doubao", "deepseek"],
                    "mode": "browser",
                }
            ],
        }

    def _create_platform(self, platform_name: str, *, config=None, inspect=False, stop_checker=None):
        del config, inspect
        platform = _FakePlatform(platform_name, self._screenshot_dir, self.calls)
        platform.stop_checker = stop_checker
        return platform

    def _create_smart_platform(self, platform_name: str, *, config=None, inspect=False, stop_checker=None):
        del config, inspect
        platform = _FakeSmartPlatform(platform_name, self._screenshot_dir, self.calls)
        platform.stop_checker = stop_checker
        return platform

    def test_runs_all_selected_platforms_in_same_round(self) -> None:
        task = self._task()

        with patch("core.task_executor_browser.create_browser_platform", side_effect=self._create_platform):
            with patch("core.task_executor_impl._load_today_success_only_query_results", return_value={}):
                with patch("core.task_executor_impl._record_result_history", return_value=None):
                    with patch(
                        "core.task_executor_impl._send_task_notifications",
                        return_value={"attempted": True, "success": True, "found_results": 2, "error_message": ""},
                    ):
                        results, report = main.run_task_group(
                            task,
                            {},
                            {"detection_mode": "browser"},
                            return_report=True,
                        )

        self.assertEqual(self.calls, ["doubao", "deepseek"])
        self.assertEqual([item["platform"] for item in results], ["doubao", "deepseek"])
        self.assertEqual(report["attempted_queries"], 2)
        self.assertEqual(report["success_queries"], 2)
        self.assertEqual(report["round_status"], "success")

    def test_notification_failure_marks_task_failed_and_emits_issue(self) -> None:
        task = self._task()
        issues: list[dict] = []

        with patch("core.task_executor_browser.create_browser_platform", side_effect=self._create_platform):
            with patch("core.task_executor_impl._load_today_success_only_query_results", return_value={}):
                with patch("core.task_executor_impl._record_result_history", return_value=None):
                    with patch(
                        "core.task_executor_impl._send_task_notifications",
                        return_value={
                            "attempted": True,
                            "success": False,
                            "found_results": 2,
                            "error_message": "webhook failed",
                        },
                    ):
                        results, report = main.run_task_group(
                            task,
                            {},
                            {"detection_mode": "browser"},
                            return_report=True,
                            issue_callback=issues.append,
                        )

        self.assertEqual(len(results), 2)
        self.assertEqual(report["round_status"], "success")
        self.assertEqual(report["query_round_status"], "success")
        self.assertEqual(report["task_status"], "failed")
        self.assertEqual(report["task_failure_kind"], "notification")
        self.assertEqual(report["task_failure_message"], "webhook failed")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["title"], "企业微信发送异常")
        self.assertEqual(issues[0]["message"], "webhook failed")

    def test_skips_only_platforms_with_successful_history(self) -> None:
        task = self._task()
        historical_success = {
            ("品牌A 评测", "doubao", "品牌A"): {
                "keyword": "品牌A 评测",
                "platform": "doubao",
                "brand": "品牌A",
                "rank": 1,
                "screenshot": str(self._screenshot_dir / "historical_doubao.jpg"),
            }
        }
        Path(historical_success[("品牌A 评测", "doubao", "品牌A")]["screenshot"]).write_bytes(b"historical-image")

        with patch("core.task_executor_browser.create_browser_platform", side_effect=self._create_platform):
            with patch("core.task_executor_impl._load_today_success_only_query_results", return_value=historical_success):
                with patch("core.task_executor_impl._record_result_history", return_value=None):
                    with patch(
                        "core.task_executor_impl._send_task_notifications",
                        return_value={"attempted": True, "success": True, "found_results": 2, "error_message": ""},
                    ):
                        results, report = main.run_task_group(
                            task,
                            {},
                            {"detection_mode": "browser"},
                            return_report=True,
                        )

        self.assertEqual(self.calls, ["deepseek"])
        self.assertEqual([item["platform"] for item in results], ["deepseek"])
        self.assertEqual(report["attempted_queries"], 1)
        self.assertEqual(report["success_queries"], 1)

    def test_no_pending_queries_sends_historical_results_when_not_sent_today(self) -> None:
        task = self._task()
        historical_success = {
            ("品牌A 评测", "doubao", "品牌A"): {
                "keyword": "品牌A 评测",
                "platform": "doubao",
                "brand": "品牌A",
                "rank": 1,
                "screenshot": "",
            },
            ("品牌A 评测", "deepseek", "品牌A"): {
                "keyword": "品牌A 评测",
                "platform": "deepseek",
                "brand": "品牌A",
                "rank": 1,
                "screenshot": "",
            },
        }

        with patch("core.task_executor_browser.create_browser_platform") as create_platform:
            with patch("core.task_executor_impl._load_today_success_only_query_results", return_value=historical_success):
                with patch("core.task_executor_impl._record_result_history", return_value=None) as record_history:
                    with patch(
                        "core.task_executor_impl._send_task_notifications",
                        return_value={"attempted": True, "success": True, "found_results": 2, "error_message": ""},
                    ) as send_notifications:
                        results, report = main.run_task_group(
                            task,
                            {},
                            {"detection_mode": "browser"},
                            return_report=True,
                        )

        self.assertEqual(results, [])
        create_platform.assert_not_called()
        record_history.assert_not_called()
        send_notifications.assert_called_once()
        self.assertEqual(len(send_notifications.call_args.args[4]), 2)
        self.assertEqual(report["attempted_queries"], 0)
        self.assertEqual(report["round_status"], "skipped")
        self.assertTrue(report["notification_attempted"])
        self.assertTrue(report["notification_success"])

    def test_passes_task_cloud_id_to_history_records(self) -> None:
        task = {
            "name": "云端任务",
            "task_id": "legacy_local_task",
            "cloud_task_id": 42,
            "keywords": [
                {
                    "keyword": "云端关键词",
                    "brand": "云端品牌",
                    "platforms": ["kimi"],
                    "mode": "api",
                }
            ],
        }

        with patch("core.task_executor_impl._load_today_success_only_query_results", return_value={}):
            with patch("core.task_executor_impl._record_result_history", return_value=None) as record_history:
                with patch(
                    "core.task_executor_impl._send_task_notifications",
                    return_value={"attempted": False, "success": False, "found_results": 0, "error_message": ""},
                ):
                    results, report = main.run_task_group(
                        task,
                        {},
                        {"detection_mode": "api", "platforms": {"kimi": {"api_key": ""}}},
                        return_report=True,
                    )

        self.assertEqual(len(results), 1)
        self.assertEqual(report["attempted_queries"], 1)
        self.assertTrue(record_history.called)
        self.assertEqual(record_history.call_args.kwargs.get("cloud_task_id"), 42)

    def test_api_mode_success_uses_api_text_and_rendered_screenshot(self) -> None:
        task = {
            "name": "API 成功任务",
            "task_id": "api_success_task",
            "keywords": [
                {
                    "keyword": "API 关键词",
                    "brand": "品牌API",
                    "platforms": ["kimi"],
                    "mode": "api",
                }
            ],
        }

        with patch("platforms.api_client.resolve_platform_api_model", return_value="kimi-test-model"):
            with patch("platforms.api_client.should_use_platform_deep_think_param", return_value=False):
                with patch("platforms.api_client.query_platform_api", return_value="这里提到了品牌API"):
                    with patch("core.task_executor_impl._load_today_success_only_query_results", return_value={}):
                        with patch("core.task_executor_impl._record_result_history", return_value=None):
                            with patch(
                                "core.task_executor_impl._send_task_notifications",
                                return_value={
                                    "attempted": True,
                                    "success": True,
                                    "found_results": 1,
                                    "error_message": "",
                                },
                            ):
                                results, report = task_executor_impl.run_task_group(
                                    task,
                                    {},
                                    {"detection_mode": "api", "platforms": {"kimi": {"api_key": "key"}}},
                                    return_report=True,
                                )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["mode"], "api")
        self.assertEqual(results[0]["rank"], 1)
        self.assertTrue(Path(results[0]["screenshot"]).exists())
        self.assertEqual(report["success_queries"], 1)

    def test_smart_mode_success_uses_smart_browser_runner(self) -> None:
        task = {
            "name": "Smart 成功任务",
            "task_id": "smart_success_task",
            "keywords": [
                {
                    "keyword": "Smart 关键词",
                    "brand": "品牌Smart",
                    "platforms": ["doubao"],
                    "mode": "smart",
                }
            ],
        }

        with patch("core.task_executor_browser.create_browser_platform", side_effect=self._create_smart_platform):
            with patch("core.task_executor_impl._load_today_success_only_query_results", return_value={}):
                with patch("core.task_executor_impl._record_result_history", return_value=None):
                    with patch(
                        "core.task_executor_impl._send_task_notifications",
                        return_value={"attempted": True, "success": True, "found_results": 1, "error_message": ""},
                    ):
                        results, report = task_executor_impl.run_task_group(
                            task,
                            {},
                            {"detection_mode": "smart"},
                            return_report=True,
                        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["mode"], "smart")
        self.assertEqual(results[0]["rank"], 1)
        self.assertTrue(Path(results[0]["screenshot"]).exists())
        self.assertEqual(results[0]["highlight_count"], 2)
        self.assertEqual(report["success_queries"], 1)

    def test_stop_checker_cancels_browser_round_without_recording_failed_query(self) -> None:
        task = self._task()
        progress_events: list[dict] = []

        with patch("core.task_executor_browser.create_browser_platform", side_effect=self._create_platform):
            with patch("core.task_executor_impl._load_today_success_only_query_results", return_value={}):
                with patch("core.task_executor_impl._record_result_history", return_value=None) as record_history:
                    with patch(
                        "core.task_executor_impl._send_task_notifications",
                        return_value={"attempted": False, "success": False, "found_results": 0, "error_message": ""},
                    ) as send_notifications:
                        results, report = main.run_task_group(
                            task,
                            {},
                            {"detection_mode": "browser"},
                            return_report=True,
                            progress_callback=progress_events.append,
                            stop_checker=lambda: True,
                        )

        self.assertEqual(results, [])
        self.assertEqual(self.calls, [])
        record_history.assert_not_called()
        send_notifications.assert_not_called()
        self.assertTrue(report["_scheduler_cancelled"])
        self.assertEqual(report["round_status"], "pending")
        self.assertEqual(report["query_round_status"], "pending")
        self.assertEqual(report["task_failure_kind"], "cancelled")
        self.assertEqual(report["task_status"], "pending")
        self.assertEqual([event.get("stage") for event in progress_events], ["started", "paused", "finished"])

    def test_fixed_screenshot_quota_stops_after_platform_floor(self) -> None:
        task = {
            "name": "固定截图任务",
            "task_id": "fixed_screenshot_task",
            "fixed_screenshot_enabled": True,
            "fixed_screenshot_count": 1,
            "keywords": [
                {
                    "keyword": "品牌A 第一问",
                    "brand": "品牌A",
                    "platforms": ["doubao", "deepseek"],
                    "mode": "browser",
                },
                {
                    "keyword": "品牌A 第二问",
                    "brand": "品牌A",
                    "platforms": ["doubao", "deepseek"],
                    "mode": "browser",
                },
            ],
        }

        with patch("core.task_executor_browser.create_browser_platform", side_effect=self._create_platform):
            with patch("core.task_executor_impl._load_today_success_only_query_results", return_value={}):
                with patch("core.task_executor_impl._record_result_history", return_value=None):
                    with patch(
                        "core.task_executor_impl._send_task_notifications",
                        return_value={"attempted": True, "success": True, "found_results": 2, "error_message": ""},
                    ):
                        results, report = task_executor_impl.run_task_group(
                            task,
                            {},
                            {"detection_mode": "browser"},
                            return_report=True,
                        )

        self.assertEqual(self.calls, ["doubao", "deepseek"])
        self.assertEqual(len(results), 2)
        self.assertEqual(report["fixed_screenshot_target"], 2)
        self.assertTrue(report["completed_by_quota"])
        self.assertEqual(report["skipped_remaining_queries"], 2)

    def test_progress_callback_reports_query_lifecycle(self) -> None:
        task = self._task()
        progress_events: list[str] = []

        def progress_callback(payload: dict) -> None:
            progress_events.append(str(payload.get("stage") or ""))

        with patch("core.task_executor_browser.create_browser_platform", side_effect=self._create_platform):
            with patch("core.task_executor_impl._load_today_success_only_query_results", return_value={}):
                with patch("core.task_executor_impl._record_result_history", return_value=None):
                    with patch(
                        "core.task_executor_impl._send_task_notifications",
                        return_value={"attempted": True, "success": True, "found_results": 2, "error_message": ""},
                    ):
                        task_executor_impl.run_task_group(
                            task,
                            {},
                            {"detection_mode": "browser"},
                            return_report=True,
                            progress_callback=progress_callback,
                        )

        self.assertEqual(progress_events[0], "started")
        self.assertIn("query_start", progress_events)
        self.assertIn("query_done", progress_events)
        self.assertEqual(progress_events[-1], "finished")


if __name__ == "__main__":
    unittest.main()
