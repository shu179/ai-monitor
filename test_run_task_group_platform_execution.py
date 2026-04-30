import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.daily_task_state as dts
import main


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

    def start(self):
        return self

    def close(self) -> None:
        return None

    def search(self, keyword: str, brand: str) -> tuple[int, str]:
        self._calls.append(self.platform_name)
        screenshot_path = self._screenshot_dir / f"{keyword}_{self.platform_name}.jpg"
        screenshot_path.write_bytes(b"fake-image")
        self.last_answer_text = f"{brand} mention on {self.platform_name}"
        return 1, str(screenshot_path)


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
        del config, inspect, stop_checker
        return _FakePlatform(platform_name, self._screenshot_dir, self.calls)

    def test_runs_all_selected_platforms_in_same_round(self) -> None:
        task = self._task()

        with patch("main.create_browser_platform", side_effect=self._create_platform):
            with patch("main._load_today_success_only_query_results", return_value={}):
                with patch("main._record_result_history", return_value=None):
                    with patch(
                        "main._send_task_notifications",
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

        with patch("main.create_browser_platform", side_effect=self._create_platform):
            with patch("main._load_today_success_only_query_results", return_value=historical_success):
                with patch("main._record_result_history", return_value=None):
                    with patch(
                        "main._send_task_notifications",
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


if __name__ == "__main__":
    unittest.main()
