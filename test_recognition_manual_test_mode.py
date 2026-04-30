import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import core.daily_task_state as dts
from core.recognition import ClipboardRecognitionManager


class RecognitionManualTestModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_state_path = dts.STATE_PATH
        self._original_data_dir = os.environ.get("AIBRANDMONITOR_DATA_DIR")
        os.environ["AIBRANDMONITOR_DATA_DIR"] = self._tmpdir.name
        dts.STATE_PATH = Path(self._tmpdir.name) / "daily_task_status.json"
        self.manager = ClipboardRecognitionManager(config_getter=lambda: {})

    def tearDown(self) -> None:
        dts.STATE_PATH = self._original_state_path
        if self._original_data_dir is None:
            os.environ.pop("AIBRANDMONITOR_DATA_DIR", None)
        else:
            os.environ["AIBRANDMONITOR_DATA_DIR"] = self._original_data_dir
        self._tmpdir.cleanup()

    def test_manual_test_still_shows_keywords_after_success(self) -> None:
        task = {
            "name": "品牌测试任务",
            "task_id": "brand_manual_test",
            "_daily_state_source": "manual_test",
            "brands": ["品牌T"],
            "primary_brand": "品牌T",
            "platform_candidates": ["doubao", "deepseek"],
            "guide_keywords": [
                {
                    "keyword": "品牌T 评测",
                    "brands": ["品牌T"],
                    "platforms": ["doubao", "deepseek"],
                }
            ],
        }

        dts.apply_task_keyword_updates(
            {"task_id": task["task_id"], "name": task["name"]},
            [
                {
                    "keyword": "品牌T 评测",
                    "brand": "品牌T",
                    "run_success": True,
                    "screenshot_saved": True,
                    "failure_reason": "",
                    "platform": "doubao",
                    "image_path": "",
                }
            ],
            source_mode="test",
        )

        pending = self.manager._get_pending_entry_statuses(task["guide_keywords"][0], dict(task))
        self.assertEqual(
            pending,
            [
                {"platform": "deepseek", "failed": False},
                {"platform": "doubao", "failed": False},
            ],
        )

        items = self.manager._build_keyword_guide_items([dict(task)])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["task_name"], task["name"])
        self.assertEqual(items[0]["keyword"], "品牌T 评测")
        self.assertEqual(items[0]["platforms"], ["deepseek", "doubao"])

    def test_manual_test_ignores_schedule_filters(self) -> None:
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        task = {
            "name": "品牌日程测试",
            "task_id": "brand_schedule_test",
            "_daily_state_source": "manual_test",
            "enabled": True,
            "brand": "品牌S",
            "keywords": [
                {
                    "keyword": "品牌S 对比",
                    "brand": "品牌S",
                    "platforms": ["doubao"],
                }
            ],
            "optimization_start_date": tomorrow,
            "optimization_end_date": tomorrow,
            "weekdays": [],
        }

        manager = ClipboardRecognitionManager(
            config_getter=lambda: {
                "detection_mode": "recognition",
                "tasks": [task],
            }
        )

        enabled_tasks = manager._get_enabled_tasks()
        self.assertEqual(len(enabled_tasks), 1)
        self.assertEqual(enabled_tasks[0]["name"], task["name"])
        self.assertEqual(enabled_tasks[0]["_daily_state_source"], "manual_test")


if __name__ == "__main__":
    unittest.main()
