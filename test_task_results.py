import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import main
import core.daily_task_state as dts
from core.cloud_outbox import CloudOutbox
from core.task_results import (
    build_execution_report,
    count_task_keywords,
    count_task_queries,
    finalize_daily_pool_keyword_results,
    finalize_execution_report,
    make_query_result_key,
    result_has_usable_screenshot,
    record_result_history,
)


class TaskResultsTests(unittest.TestCase):
    def test_main_compatibility_exports_point_to_task_result_helpers(self):
        self.assertIs(main._count_task_queries, count_task_queries)
        self.assertIs(main._build_execution_report, build_execution_report)
        self.assertIs(main._finalize_execution_report, finalize_execution_report)

    def test_counts_only_executable_keywords_and_queries(self):
        keywords = [
            {"keyword": "词1", "brand": "品牌A", "platforms": ["doubao", "kimi"]},
            {"keyword": "词1", "brand": "品牌A", "platforms": ["tongyi"]},
            {"keyword": "", "brand": "品牌A", "platforms": ["doubao"]},
            {"keyword": "词2", "brand": "", "platforms": []},
            {"keyword": "词3", "brand": "", "platforms": ["doubao"]},
        ]

        self.assertEqual(count_task_queries(keywords, "默认品牌"), 4)
        self.assertEqual(count_task_keywords(keywords, "默认品牌"), 2)

    def test_execution_report_and_notification_finalization_preserve_status_rules(self):
        task = {"task_id": "task-a", "name": "品牌A"}
        report = build_execution_report(
            task,
            [
                {"keyword": "词1", "platform": "doubao", "brand": "品牌A", "rank": 1, "mode": "browser"},
                {
                    "keyword": "词2",
                    "platform": "kimi",
                    "brand": "品牌A",
                    "rank": 99,
                    "mode": "browser",
                    "error_message": "未配置 api_key",
                },
            ],
            1.234,
        )

        self.assertEqual(report["round_status"], "partial")
        self.assertEqual(report["failure_kind"], "structural")
        self.assertEqual(report["successful_brands"], ["品牌A"])
        self.assertEqual(report["failed_query_details"][0]["failure_type"], "structural_error")

        finalized = finalize_execution_report(
            {"round_status": "success", "failure_kind": ""},
            {"success": False, "error_message": "企业微信发送失败"},
        )
        self.assertEqual(finalized["task_status"], "failed")
        self.assertEqual(finalized["task_failure_kind"], "notification")

    def test_query_key_normalizes_platform_and_screenshot_requires_existing_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            screenshot = Path(tmpdir) / "hit.jpg"
            screenshot.write_bytes(b"fake-image")

            self.assertEqual(
                make_query_result_key(" 词 ", "豆包", " 品牌 "),
                ("词", "doubao", "品牌"),
            )
            self.assertTrue(result_has_usable_screenshot({"screenshot": str(screenshot)}))
            self.assertFalse(result_has_usable_screenshot({"screenshot": str(screenshot) + ".missing"}))

    def test_finalizes_multi_platform_keyword_screenshots_separately(self):
        task = {
            "task_id": "task_multi_platform",
            "name": "品牌A",
            "brand": "品牌A",
            "keywords": [
                {"keyword": "奶粉推荐", "brand": "品牌A", "platforms": ["doubao", "kimi"], "mode": "browser"},
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            original_state_path = dts.STATE_PATH
            original_data_dir = os.environ.get("AIBRANDMONITOR_DATA_DIR")
            os.environ["AIBRANDMONITOR_DATA_DIR"] = tmpdir
            dts.STATE_PATH = Path(tmpdir) / "daily_task_status.json"
            try:
                shot_doubao = Path(tmpdir) / "doubao.jpg"
                shot_kimi = Path(tmpdir) / "kimi.jpg"
                shot_doubao.write_bytes(b"doubao")
                shot_kimi.write_bytes(b"kimi")

                finalize_daily_pool_keyword_results(
                    task,
                    all_results=[
                        {
                            "keyword": "奶粉推荐",
                            "platform": "doubao",
                            "brand": "品牌A",
                            "rank": 1,
                            "screenshot": str(shot_doubao),
                        },
                        {
                            "keyword": "奶粉推荐",
                            "platform": "kimi",
                            "brand": "品牌A",
                            "rank": 1,
                            "screenshot": str(shot_kimi),
                        },
                    ],
                    execution_source="manual_test",
                    historical_keyword_states={},
                )

                status = dts.get_task_day_status(task)
                keyword_state = status["keyword_states"]["奶粉推荐"]
                platform_states = keyword_state["platform_states"]
                self.assertTrue(platform_states["doubao"]["screenshot_saved"])
                self.assertTrue(platform_states["kimi"]["screenshot_saved"])
                self.assertNotEqual(platform_states["doubao"]["image_path"], platform_states["kimi"]["image_path"])
                self.assertEqual(status["actual_screenshot_count"], 2)
            finally:
                dts.STATE_PATH = original_state_path
                if original_data_dir is None:
                    os.environ.pop("AIBRANDMONITOR_DATA_DIR", None)
                else:
                    os.environ["AIBRANDMONITOR_DATA_DIR"] = original_data_dir

    def test_record_result_history_persists_body_references_and_supports_task_id_lookup(self):
        from core import history

        with tempfile.TemporaryDirectory() as tmpdir:
            original_history_dir = history.HISTORY_DIR
            history.HISTORY_DIR = Path(tmpdir) / "logs" / "history"
            try:
                record_result_history(
                    "品牌A",
                    {
                        "platform": "doubao",
                        "keyword": "关键词A",
                        "brand": "品牌A",
                        "rank": 1,
                        "mode": "browser",
                        "answer_text": "这段回答正文不应该写入本地历史",
                        "evidence": "命中片段也不落盘",
                        "references": [{"url": "https://example.com/a"}],
                        "body_references": [{"url": "https://example.com/b"}],
                    },
                    task_id="task-a",
                    run_started_at="2026-05-03T08:00:00+08:00",
                )

                records = history.get_records("品牌A", task_id="task-a")
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0].get("answer_text"), "")
                self.assertEqual(records[0].get("evidence"), "")
                extra = records[0].get("extra") or {}
                self.assertEqual([item["url"] for item in extra.get("references") or []], ["https://example.com/a"])
                self.assertEqual([item["url"] for item in extra.get("body_references") or []], ["https://example.com/b"])
                self.assertEqual(extra.get("body_reference_count"), 1)
                self.assertEqual(extra.get("total_reference_count"), 2)
                self.assertEqual(extra.get("run_started_at"), "2026-05-03T08:00:00+08:00")
            finally:
                history.HISTORY_DIR = original_history_dir

    def test_record_result_history_enqueues_cloud_event_from_cloud_task_id(self):
        from core import history

        with tempfile.TemporaryDirectory() as tmpdir:
            original_history_dir = history.HISTORY_DIR
            history.HISTORY_DIR = Path(tmpdir) / "logs" / "history"
            outbox = CloudOutbox(Path(tmpdir) / "cloud_outbox.json")
            try:
                with patch("core.cloud_run_sync.CloudOutbox", return_value=outbox):
                    record_result_history(
                        "云端品牌",
                        {
                            "platform": "doubao",
                            "keyword": "云端关键词",
                            "brand": "云端品牌",
                            "rank": 99,
                            "mode": "browser",
                            "error_message": "未识别到品牌名",
                        },
                        execution_source="manual",
                        task_id="cloud_9",
                    )

                pending = outbox.pending()
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]["event_type"], "run_record")
                self.assertEqual(pending[0]["payload"]["task_id"], 9)
                self.assertEqual(pending[0]["payload"]["brand"], "云端品牌")
            finally:
                history.HISTORY_DIR = original_history_dir


if __name__ == "__main__":
    unittest.main()
