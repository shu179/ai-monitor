import os
from pathlib import Path
import tempfile
import unittest

import core.daily_task_state as dts


TASK = {
    "name": "品牌A",
    "brand": "品牌A",
    "task_id": "task_a",
    "keywords": [
        {"keyword": "词1", "brand": "品牌A", "platforms": ["doubao"], "mode": "browser"},
        {"keyword": "词2", "brand": "品牌A", "platforms": ["deepseek"], "mode": "browser"},
    ],
}


class DailyTaskStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_state_path = dts.STATE_PATH
        self._original_data_dir = os.environ.get("AIBRANDMONITOR_DATA_DIR")
        os.environ["AIBRANDMONITOR_DATA_DIR"] = self._tmpdir.name
        dts.STATE_PATH = Path(self._tmpdir.name) / "daily_task_status.json"

    def tearDown(self) -> None:
        dts.STATE_PATH = self._original_state_path
        if self._original_data_dir is None:
            os.environ.pop("AIBRANDMONITOR_DATA_DIR", None)
        else:
            os.environ["AIBRANDMONITOR_DATA_DIR"] = self._original_data_dir
        self._tmpdir.cleanup()

    def test_formal_gap_then_test_fill_then_sent(self) -> None:
        dts.start_formal_task_run(TASK, source="auto", message="running")
        self.assertEqual(dts.get_task_day_status(TASK)["brand_status"], "running")

        dts.apply_task_keyword_updates(
            TASK,
            [
                {
                    "keyword": "词1",
                    "brand": "品牌A",
                    "run_success": True,
                    "screenshot_saved": True,
                    "failure_reason": "",
                    "platform": "doubao",
                },
                {
                    "keyword": "词2",
                    "brand": "品牌A",
                    "run_success": True,
                    "screenshot_saved": False,
                    "failure_reason": "screenshot_save_failed",
                    "platform": "deepseek",
                },
            ],
            source_mode="formal",
        )
        dts.finish_formal_task_run(TASK, message="done")

        gap_status = dts.get_task_day_status(TASK)
        self.assertEqual(gap_status["brand_status"], "gap")
        self.assertIn("词2：截图保存失败", gap_status["gap_reasons"])

        dts.apply_task_keyword_updates(
            TASK,
            [
                {
                    "keyword": "词2",
                    "brand": "品牌A",
                    "run_success": True,
                    "screenshot_saved": True,
                    "failure_reason": "",
                    "platform": "deepseek",
                }
            ],
            source_mode="test",
        )

        success_status = dts.get_task_day_status(TASK)
        self.assertEqual(success_status["brand_status"], "success")
        self.assertFalse(success_status["sent_today"])

        dts.mark_task_sent(TASK, source_mode="test", message="sent")
        sent_status = dts.get_task_day_status(TASK)
        self.assertEqual(sent_status["brand_status"], "sent")
        self.assertTrue(sent_status["sent_today"])
        self.assertTrue(sent_status["formal_started"])

        dts.mark_task_sent(TASK, source_mode="formal", message="sent")
        formal_sent_status = dts.get_task_day_status(TASK)
        self.assertEqual(formal_sent_status["brand_status"], "sent")
        self.assertTrue(formal_sent_status["sent_today"])

    def test_manual_test_failure_does_not_write_pool(self) -> None:
        task = {
            "name": "品牌B",
            "brand": "品牌B",
            "task_id": "task_b",
            "keywords": [
                {"keyword": "词A", "brand": "品牌B", "platforms": ["doubao"], "mode": "browser"},
            ],
        }

        dts.apply_task_keyword_updates(
            task,
            [
                {
                    "keyword": "词A",
                    "brand": "品牌B",
                    "run_success": False,
                    "screenshot_saved": False,
                    "failure_reason": "run_failed",
                }
            ],
            source_mode="test",
        )

        status = dts.get_task_day_status(task)
        self.assertEqual(status["brand_status"], "pending")

    def test_manual_test_failure_after_success_does_not_downgrade_day(self) -> None:
        task = {
            "name": "品牌B2",
            "brand": "品牌B2",
            "task_id": "task_b2",
            "keywords": [
                {"keyword": "词B2", "brand": "品牌B2", "platforms": ["doubao"], "mode": "browser"},
            ],
        }

        dts.write_task_status(
            task,
            status="success",
            source="manual_test",
            scope="test",
            message="测试成功",
            extra=dts.build_task_state_extra(
                completed_keywords=["词B2"],
                detected_platforms=["doubao"],
                notification_success=True,
            ),
        )
        dts.write_task_status(
            task,
            status="query_failed",
            source="manual_test",
            scope="test",
            message="后续测试失败",
            extra=dts.build_task_state_extra(task_failure_kind="query"),
        )

        status = dts.get_task_day_status(task)
        test_status = dts.get_task_test_status(task)
        self.assertIn(status["brand_status"], {"success", "sent"})
        self.assertEqual(test_status["status"], "success")
        self.assertEqual(status["completed_keywords"], ["词B2"])

    def test_official_failure_after_success_does_not_downgrade_official_status(self) -> None:
        task = {
            "name": "品牌B3",
            "brand": "品牌B3",
            "task_id": "task_b3",
            "keywords": [
                {"keyword": "词B3", "brand": "品牌B3", "platforms": ["doubao"], "mode": "browser"},
            ],
        }

        dts.write_task_status(
            task,
            status="success",
            source="auto",
            message="正式成功",
            extra=dts.build_task_state_extra(
                completed_keywords=["词B3"],
                detected_platforms=["doubao"],
                notification_success=True,
            ),
        )
        dts.write_task_status(
            task,
            status="query_failed",
            source="auto",
            message="后续正式失败",
            extra=dts.build_task_state_extra(task_failure_kind="query"),
        )

        status = dts.get_task_day_status(task)
        self.assertEqual(status["official_status"], "success")
        self.assertIn(status["brand_status"], {"success", "sent"})
        self.assertNotEqual(status["official_message"], "后续正式失败")

    def test_success_without_completed_keywords_uses_required_keywords_for_progress(self) -> None:
        task = {
            "name": "品牌B4",
            "brand": "品牌B4",
            "task_id": "task_b4",
            "keywords": [
                {"keyword": "词B4-1", "brand": "品牌B4", "platforms": ["doubao"], "mode": "browser"},
                {"keyword": "词B4-2", "brand": "品牌B4", "platforms": ["deepseek"], "mode": "browser"},
            ],
        }

        dts.write_task_status(
            task,
            status="success",
            source="auto",
            message="正式成功",
            extra=dts.build_task_state_extra(notification_success=True),
        )

        status = dts.get_task_day_status(task)
        self.assertEqual(status["brand_status"], "sent")
        self.assertEqual(status["completed_keywords"], ["词B4-1", "词B4-2"])
        self.assertFalse(status["has_gap"])

    def test_manual_test_success_moves_screenshot_into_formal_pool_path(self) -> None:
        task = {
            "name": "品牌C",
            "brand": "品牌C",
            "task_id": "task_c",
            "keywords": [
                {"keyword": "词C", "brand": "品牌C", "platforms": ["doubao"], "mode": "browser"},
            ],
        }
        screenshots_dir = Path(self._tmpdir.name) / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        source_path = screenshots_dir / "20260422_品牌C_词C_doubao_test.jpg"
        source_path.write_bytes(b"fake-image")

        dts.apply_task_keyword_updates(
            task,
            [
                {
                    "keyword": "词C",
                    "brand": "品牌C",
                    "run_success": True,
                    "screenshot_saved": True,
                    "failure_reason": "",
                    "platform": "doubao",
                    "image_path": str(source_path),
                }
            ],
            source_mode="test",
        )

        status = dts.get_task_day_status(task)
        keyword_state = dict(status["keyword_states"].get("词C") or {})
        expected_path = Path(
            dts.build_canonical_screenshot_path(
                task,
                keyword="词C",
                brand="品牌C",
                platform="doubao",
                source_mode=dts.SOURCE_MODE_FORMAL,
                original_path=str(source_path),
            )
        )

        self.assertTrue(keyword_state.get("run_success"))
        self.assertTrue(keyword_state.get("screenshot_saved"))
        self.assertEqual(keyword_state.get("image_path"), str(expected_path))
        self.assertFalse(source_path.exists())
        self.assertTrue(expected_path.exists())

    def test_manual_test_send_failure_does_not_write_pool_failure(self) -> None:
        task = {
            "name": "品牌D",
            "brand": "品牌D",
            "task_id": "task_d",
            "keywords": [
                {"keyword": "词D", "brand": "品牌D", "platforms": ["doubao"], "mode": "browser"},
            ],
        }

        dts.mark_task_send_failure(task, source_mode="test", message="测试发送失败")
        status = dts.get_task_day_status(task)

        self.assertEqual(status["brand_status"], "pending")
        self.assertFalse(status["sent_today"])

    def test_new_screenshot_replaces_existing_canonical_file(self) -> None:
        task = {
            "name": "品牌F",
            "brand": "品牌F",
            "task_id": "task_f",
            "keywords": [
                {"keyword": "词F", "brand": "品牌F", "platforms": ["doubao"], "mode": "browser"},
            ],
        }
        screenshots_dir = Path(self._tmpdir.name) / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        source_path = screenshots_dir / "fresh.jpg"
        source_path.write_bytes(b"fresh-image")
        target_path = Path(
            dts.build_canonical_screenshot_path(
                task,
                keyword="词F",
                brand="品牌F",
                platform="doubao",
                source_mode=dts.SOURCE_MODE_FORMAL,
                original_path=str(source_path),
            )
        )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"stale-image")

        dts.apply_task_keyword_updates(
            task,
            [
                {
                    "keyword": "词F",
                    "brand": "品牌F",
                    "run_success": True,
                    "screenshot_saved": True,
                    "failure_reason": "",
                    "platform": "doubao",
                    "image_path": str(source_path),
                }
            ],
            source_mode="formal",
        )

        status = dts.get_task_day_status(task)
        keyword_state = dict(status["keyword_states"].get("词F") or {})
        self.assertEqual(keyword_state.get("image_path"), str(target_path))
        self.assertTrue(target_path.exists())
        self.assertEqual(target_path.read_bytes(), b"fresh-image")
        self.assertFalse(source_path.exists())

    def test_manual_test_send_marks_shared_sent_without_blocking_manual_scope(self) -> None:
        task = {
            "name": "品牌E",
            "brand": "品牌E",
            "task_id": "task_e",
            "keywords": [
                {"keyword": "词E", "brand": "品牌E", "platforms": ["doubao"], "mode": "browser"},
            ],
        }

        dts.write_task_status(
            task,
            status="success",
            source="manual_test",
            scope="test",
            message="测试发送成功",
            extra=dts.build_task_state_extra(
                completed_keywords=["词E"],
                detected_platforms=["doubao"],
                notification_success=True,
            ),
        )

        status = dts.get_task_day_status(task)
        self.assertTrue(status["sent_today"])
        self.assertEqual(status["brand_status"], "sent")
        self.assertEqual(status["test_status"], "success")
        self.assertEqual(status["official_status"], "pending")
        self.assertFalse(status["formal_started"])


if __name__ == "__main__":
    unittest.main()
