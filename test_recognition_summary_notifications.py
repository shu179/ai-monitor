import os
from pathlib import Path
import tempfile
import unittest

import core.cycle_state as cycle_state
from main import SchedulerWebhookReporter


class RecognitionSummaryNotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_data_dir = os.environ.get("AIBRANDMONITOR_DATA_DIR")
        self._original_cycle_path = cycle_state.STATE_PATH
        os.environ["AIBRANDMONITOR_DATA_DIR"] = self._tmpdir.name
        cycle_state.STATE_PATH = Path(self._tmpdir.name) / "scheduler_cycle_state.json"

    def tearDown(self) -> None:
        cycle_state.STATE_PATH = self._original_cycle_path
        if self._original_data_dir is None:
            os.environ.pop("AIBRANDMONITOR_DATA_DIR", None)
        else:
            os.environ["AIBRANDMONITOR_DATA_DIR"] = self._original_data_dir
        self._tmpdir.cleanup()

    def _seed_failed_cycle(self) -> None:
        cycle_state.save_cycle_report(
            {
                "executed_rounds": [],
                "task_outcomes": [
                    {
                        "task_id": "summary-task-1",
                        "task_name": "品牌R",
                        "final_status": "failed",
                        "successful_brands": [],
                        "failed_query_details": [
                            {
                                "keyword": "词R",
                                "platform": "doubao",
                                "brand": "品牌R",
                                "error_message": "旧失败",
                            }
                        ],
                        "task_failure_message": "旧失败",
                        "executed_modes": ["browser"],
                    }
                ],
                "summary": {
                    "total_tasks": 1,
                    "success": 0,
                    "failed": 1,
                    "skipped": 0,
                },
            }
        )

    def test_recognition_result_records_without_immediate_summary(self) -> None:
        self._seed_failed_cycle()
        reporter = SchedulerWebhookReporter(lambda: {})
        sent_messages: list[str] = []
        reporter._send_text = lambda content: sent_messages.append(content) or True
        batch = {
            "id": "batch-summary-1",
            "task_name": "品牌R",
            "brands": ["品牌R"],
            "image_paths": ["/tmp/shot.jpg"],
            "detected_platforms": ["doubao"],
            "completed_keywords": ["词R"],
            "supplemented_keywords": ["词R"],
            "task": {"task_id": "summary-task-1", "name": "品牌R"},
        }

        reporter.record_recognition_result(batch, True, "")

        self.assertEqual(sent_messages, [])
        cycle_payload = cycle_state.get_cycle_report()
        self.assertEqual((cycle_payload or {})["summary"]["success"], 1)

        reporter.send_recognition_round_summary({})

        self.assertEqual(len(sent_messages), 1)
        self.assertIn("【自动监控识别补齐汇总】", sent_messages[0])
        self.assertIn("本轮补齐：品牌R", sent_messages[0])
        self.assertIn("仍未成功：无", sent_messages[0])

    def test_recognition_round_summary_is_not_sent_when_no_events_recorded(self) -> None:
        reporter = SchedulerWebhookReporter(lambda: {})
        sent_messages: list[str] = []
        reporter._send_text = lambda content: sent_messages.append(content) or True

        ok = reporter.send_recognition_round_summary({})

        self.assertFalse(ok)
        self.assertEqual(sent_messages, [])


if __name__ == "__main__":
    unittest.main()
