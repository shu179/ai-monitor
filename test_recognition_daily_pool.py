import os
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import core.daily_task_state as dts
from core.recognition import ClipboardRecognitionManager
from core.notifier import WeComNotifier
from core.notification_retry import clear_retry_queue, load_retry_queue, stop_notification_retry_worker
from PIL import Image


TASK = {
    "name": "品牌R",
    "task_id": "task_r",
    "brands": ["品牌R"],
    "primary_brand": "品牌R",
    "platform_candidates": ["doubao", "deepseek"],
    "guide_keywords": [
        {
            "keyword": "词R",
            "brands": ["品牌R"],
            "platforms": ["doubao", "deepseek"],
        }
    ],
}


class RecognitionDailyPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_state_path = dts.STATE_PATH
        self._original_data_dir = os.environ.get("AIBRANDMONITOR_DATA_DIR")
        os.environ["AIBRANDMONITOR_DATA_DIR"] = self._tmpdir.name
        dts.STATE_PATH = Path(self._tmpdir.name) / "daily_task_status.json"
        ClipboardRecognitionManager._active_listener = None
        ClipboardRecognitionManager._last_opened_capture_platform = ""
        WeComNotifier._shared_last_post = {}
        WeComNotifier._shared_post_locks = {}
        clear_retry_queue()
        self.manager = ClipboardRecognitionManager(config_getter=lambda: {})

    def tearDown(self) -> None:
        stop_notification_retry_worker()
        clear_retry_queue()
        dts.STATE_PATH = self._original_state_path
        ClipboardRecognitionManager._active_listener = None
        ClipboardRecognitionManager._last_opened_capture_platform = ""
        if self._original_data_dir is None:
            os.environ.pop("AIBRANDMONITOR_DATA_DIR", None)
        else:
            os.environ["AIBRANDMONITOR_DATA_DIR"] = self._original_data_dir
        self._tmpdir.cleanup()

    def test_clipboard_image_timeout_seconds_is_clamped(self) -> None:
        low = ClipboardRecognitionManager(
            config_getter=lambda: {"recognition": {"clipboard_image_timeout_seconds": 0.01}}
        )
        high = ClipboardRecognitionManager(
            config_getter=lambda: {"recognition": {"clipboard_image_timeout_seconds": 99}}
        )

        self.assertEqual(low._clipboard_image_timeout_seconds(), 0.5)
        self.assertEqual(high._clipboard_image_timeout_seconds(), 30.0)

    def test_clipboard_image_grab_timeout_does_not_block_poll_thread(self) -> None:
        manager = ClipboardRecognitionManager(
            config_getter=lambda: {"recognition": {"clipboard_image_timeout_seconds": 0.05}}
        )
        started = threading.Event()
        release = threading.Event()
        calls = 0

        def slow_grabclipboard():
            nonlocal calls
            calls += 1
            started.set()
            release.wait(timeout=2)
            return object()

        try:
            with patch("core.recognition.ImageGrab.grabclipboard", side_effect=slow_grabclipboard):
                start = time.perf_counter()
                clip = manager._grab_clipboard_image_source()
                elapsed = time.perf_counter() - start

                self.assertIsNone(clip)
                self.assertLess(elapsed, 1.0)
                self.assertTrue(started.is_set())
                self.assertTrue(manager._clipboard_grab_inflight)

                self.assertIsNone(manager._grab_clipboard_image_source())
                self.assertEqual(calls, 1)
        finally:
            release.set()
            deadline = time.monotonic() + 1.0
            while manager._clipboard_grab_inflight and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertFalse(manager._clipboard_grab_inflight)

    def test_runtime_idle_seconds_use_monotonic_clock(self) -> None:
        manager = ClipboardRecognitionManager(config_getter=lambda: {})
        manager._last_image_seen_at = datetime.now() + timedelta(days=1)
        manager._last_image_seen_monotonic = time.monotonic() - 75.0

        status = manager.get_runtime_status(include_overview=False)

        self.assertGreaterEqual(status["idle_seconds"], 74.0)
        self.assertLess(status["idle_seconds"], 76.0)
        self.assertEqual(status["idle_minutes"], 1.3)

    def test_import_runtime_state_restores_monotonic_from_wall_clock(self) -> None:
        manager = ClipboardRecognitionManager(config_getter=lambda: {})
        exported_at = datetime.now() - timedelta(seconds=45)

        manager.import_runtime_state({
            "last_image_seen_at": exported_at,
            "work_started": True,
        })

        elapsed = manager._last_image_elapsed_seconds()
        self.assertIsNotNone(elapsed)
        self.assertGreaterEqual(elapsed, 44.0)
        self.assertLess(elapsed, 46.0)

    def test_pending_statuses_follow_daily_pool_keyword_state(self) -> None:
        pending = self.manager._get_pending_entry_statuses(TASK["guide_keywords"][0], dict(TASK))
        self.assertEqual(
            pending,
            [
                {"platform": "deepseek", "failed": False},
                {"platform": "doubao", "failed": False},
            ],
        )

        dts.apply_task_keyword_updates(
            dict(TASK),
            [
                {
                    "keyword": "词R",
                    "brand": "品牌R",
                    "run_success": False,
                    "screenshot_saved": False,
                    "failure_reason": "run_failed",
                    "platform": "deepseek",
                }
            ],
            source_mode="formal",
        )

        failed_pending = self.manager._get_pending_entry_statuses(TASK["guide_keywords"][0], dict(TASK))
        self.assertEqual(
            failed_pending,
            [
                {"platform": "deepseek", "failed": True},
                {"platform": "doubao", "failed": False},
            ],
        )

        dts.apply_task_keyword_updates(
            dict(TASK),
            [
                {
                    "keyword": "词R",
                    "brand": "品牌R",
                    "run_success": True,
                    "screenshot_saved": True,
                    "failure_reason": "",
                    "platform": "doubao",
                    "image_path": "",
                }
            ],
            source_mode="formal",
        )

        completed_pending = self.manager._get_pending_entry_statuses(TASK["guide_keywords"][0], dict(TASK))
        self.assertEqual(completed_pending, [{"platform": "deepseek", "failed": True}])

    def test_keyword_with_multiple_platforms_stays_visible_until_all_platforms_complete(self) -> None:
        screenshots_dir = Path(self._tmpdir.name) / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = screenshots_dir / "doubao.jpg"
        screenshot_path.write_bytes(b"fake-image")
        task = {
            **TASK,
            "keywords": [
                {"keyword": "词R", "brand": "品牌R", "platforms": ["doubao", "deepseek"], "mode": "recognition"},
            ],
        }

        dts.apply_task_keyword_updates(
            {"task_id": TASK["task_id"], "name": TASK["name"], **task},
            [
                {
                    "keyword": "词R",
                    "brand": "品牌R",
                    "run_success": True,
                    "screenshot_saved": True,
                    "failure_reason": "",
                    "platform": "doubao",
                    "image_path": str(screenshot_path),
                }
            ],
            source_mode="formal",
        )

        pending = self.manager._get_pending_entry_statuses(task["guide_keywords"][0], dict(task))
        self.assertEqual(pending, [{"platform": "deepseek", "failed": False}])
        items = self.manager._build_keyword_guide_items([dict(task)])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["keyword"], "词R")
        self.assertEqual(items[0]["platforms"], ["deepseek"])
        self.assertEqual(items[0]["completed_keyword_count"], 0)
        self.assertEqual(items[0]["total_keyword_count"], 1)

    def test_runtime_buffered_platform_is_removed_from_guide_before_send(self) -> None:
        screenshots_dir = Path(self._tmpdir.name) / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = screenshots_dir / "doubao_buffered.jpg"
        screenshot_path.write_bytes(b"fake-image")

        with self.manager._lock:
            self.manager._buffers[TASK["name"]] = [
                {
                    "path": str(screenshot_path),
                    "brands": ["品牌R"],
                    "summary": "命中品牌R",
                    "ocr_text": "品牌R",
                    "matched_pairs": self.manager._build_matched_pairs("词R", ["品牌R"], ["doubao"]),
                }
            ]

        items = self.manager._build_keyword_guide_items([dict(TASK)])

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["keyword"], "词R")
        self.assertEqual(items[0]["platforms"], ["deepseek"])
        self.assertIn("未完成平台：DeepSeek", items[0]["detail_text"])
        self.assertNotIn("豆包", items[0]["detail_text"])

    def test_manual_capture_uses_active_platform_hint_for_runtime_guide(self) -> None:
        screenshot_path = Path(self._tmpdir.name) / "deepseek_manual.jpg"
        screenshot_path.write_bytes(b"fake-image")

        self.manager._route_manual_capture(str(screenshot_path), [dict(TASK)], platform_hint="deepseek")

        items = self.manager._build_keyword_guide_items([dict(TASK)])

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["keyword"], "词R")
        self.assertEqual(items[0]["platforms"], ["doubao"])
        self.assertIn("未完成平台：豆包", items[0]["detail_text"])
        self.assertNotIn("DeepSeek", items[0]["detail_text"])

    def test_manual_capture_without_platform_hint_does_not_guess_multi_platform(self) -> None:
        screenshot_path = Path(self._tmpdir.name) / "unknown_platform_manual.jpg"
        screenshot_path.write_bytes(b"fake-image")

        self.manager._route_manual_capture(str(screenshot_path), [dict(TASK)], platform_hint="")

        items = self.manager._build_keyword_guide_items([dict(TASK)])

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["keyword"], "词R")
        self.assertEqual(items[0]["platforms"], ["deepseek", "doubao"])
        self.assertIn("未完成平台：DeepSeek、豆包", items[0]["detail_text"])

    def test_clipboard_image_payload_includes_active_platform_hint(self) -> None:
        screenshot_path = Path(self._tmpdir.name) / "clipboard_hint.jpg"
        screenshot_path.write_bytes(b"fake-image")
        queued_payloads = []

        class FakeQueue:
            def put_nowait(self, payload):
                queued_payloads.append(payload)

        original_queue = self.manager._recognition_queue
        self.manager._recognition_queue = FakeQueue()
        self.manager.set_active_capture_platform("deepseek")
        try:
            with patch("core.recognition.ImageGrab.grabclipboard", return_value=object()):
                with patch.object(self.manager, "_extract_clipboard_image", side_effect=lambda clip: clip):
                    with patch.object(self.manager, "_prepare_clipboard_image", return_value=("hash-clipboard-hint", b"bytes")):
                        with patch.object(self.manager, "_persist_clipboard_image", return_value=screenshot_path):
                            self.manager._poll_once()
        finally:
            self.manager._recognition_queue = original_queue

        self.assertEqual(len(queued_payloads), 1)
        self.assertEqual(queued_payloads[0]["platform_hint"], "deepseek")

    def test_local_ocr_route_keeps_platform_hint(self) -> None:
        config = {
            "detection_mode": "recognition",
            "tasks": [
                {
                    "name": "品牌R",
                    "task_id": "task_r_ocr_hint",
                    "enabled": True,
                    "brand": "品牌R",
                    "weekdays": [0, 1, 2, 3, 4, 5, 6],
                    "recognition_batch_size": 2,
                    "keywords": [
                        {
                            "keyword": "词R",
                            "brand": "品牌R",
                            "platforms": ["doubao", "deepseek"],
                        }
                    ],
                }
            ],
        }
        manager = ClipboardRecognitionManager(config_getter=lambda: config)
        routed = {}

        def fake_route_to_batches(*args, **kwargs):
            routed["args"] = args
            routed["kwargs"] = kwargs

        manager._route_to_batches = fake_route_to_batches

        with patch(
            "core.recognition.extract_text_from_image",
            return_value=("品牌R 回答正文", {"matched_brands": ["品牌R"], "provider": "unit"}),
        ):
            manager._recognize_one({"path": "/tmp/ocr_hint.jpg", "platform_hint": "deepseek"})

        self.assertEqual(routed["kwargs"].get("platform_hint"), "deepseek")

    def test_runtime_completed_keyword_keeps_waiting_guide_visible_until_send_state_writes(self) -> None:
        screenshots_dir = Path(self._tmpdir.name) / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        doubao_path = screenshots_dir / "doubao_buffered.jpg"
        deepseek_path = screenshots_dir / "deepseek_buffered.jpg"
        doubao_path.write_bytes(b"fake-image")
        deepseek_path.write_bytes(b"fake-image")

        with self.manager._lock:
            self.manager._buffers[TASK["name"]] = [
                {
                    "path": str(doubao_path),
                    "brands": ["品牌R"],
                    "summary": "命中品牌R",
                    "ocr_text": "品牌R",
                    "matched_pairs": self.manager._build_matched_pairs("词R", ["品牌R"], ["doubao"]),
                },
                {
                    "path": str(deepseek_path),
                    "brands": ["品牌R"],
                    "summary": "命中品牌R",
                    "ocr_text": "品牌R",
                    "matched_pairs": self.manager._build_matched_pairs("词R", ["品牌R"], ["deepseek"]),
                },
            ]

        items = self.manager._build_keyword_guide_items([dict(TASK)])

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["keyword"], "词R")
        self.assertEqual(items[0]["platforms"], [])
        self.assertIn("已捕获平台", items[0]["detail_text"])
        self.assertIn("等待发送", items[0]["detail_text"])

    def test_prepare_send_images_reuses_bottom_hint_for_platform_and_trimming(self) -> None:
        screenshots_dir = Path(self._tmpdir.name) / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = screenshots_dir / "doubao_send.jpg"
        screenshot_path.write_bytes(b"fake-image")
        task = {
            **TASK,
            "keywords": [
                {"keyword": "词R", "brand": "品牌R", "platforms": ["doubao", "deepseek"], "mode": "recognition"},
            ],
        }
        batch = {
            "task": task,
            "task_name": TASK["name"],
            "brands": ["品牌R"],
            "image_items": [{"path": str(screenshot_path), "ocr_text": "正文", "source_text": ""}],
            "matched_pairs": [
                {"keyword": "词R", "brand": "品牌R", "platforms": ["doubao", "deepseek"]},
            ],
        }
        hint_text = "发送消息或输入/选择技能"

        with patch.object(self.manager, "_extract_bottom_hint_text", return_value=hint_text) as extract_mock:
            with patch.object(
                self.manager,
                "_build_decorated_send_image",
                side_effect=lambda **kwargs: kwargs["image_path"],
            ) as decorate_mock:
                processed_paths, detected_platforms = self.manager._prepare_send_images(batch, {})

        self.assertEqual(processed_paths, [str(screenshot_path)])
        self.assertEqual(detected_platforms, ["doubao"])
        self.assertEqual(extract_mock.call_count, 1)
        self.assertEqual(decorate_mock.call_args.kwargs["bubble_hint_text"], hint_text)

    def test_guide_progress_counts_keywords_for_current_platform_only(self) -> None:
        screenshots_dir = Path(self._tmpdir.name) / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        first_path = screenshots_dir / "first_doubao.jpg"
        first_path.write_bytes(b"fake-image")
        second_path = screenshots_dir / "second_doubao.jpg"
        second_path.write_bytes(b"fake-image")
        task = {
            **TASK,
            "guide_keywords": [
                {"keyword": "词1", "brands": ["品牌R"], "platforms": ["doubao", "deepseek"]},
                {"keyword": "词2", "brands": ["品牌R"], "platforms": ["doubao"]},
            ],
            "keywords": [
                {"keyword": "词1", "brand": "品牌R", "platforms": ["doubao", "deepseek"], "mode": "recognition"},
                {"keyword": "词2", "brand": "品牌R", "platforms": ["doubao"], "mode": "recognition"},
            ],
        }

        dts.apply_task_keyword_updates(
            dict(task),
            [
                {
                    "keyword": "词1",
                    "brand": "品牌R",
                    "run_success": True,
                    "screenshot_saved": True,
                    "failure_reason": "",
                    "platform": "doubao",
                    "image_path": str(first_path),
                },
                {
                    "keyword": "词2",
                    "brand": "品牌R",
                    "run_success": True,
                    "screenshot_saved": True,
                    "failure_reason": "",
                    "platform": "doubao",
                    "image_path": str(second_path),
                },
            ],
            source_mode="formal",
        )

        items = self.manager._build_keyword_guide_items([dict(task)])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["keyword"], "词1")
        self.assertEqual(items[0]["platforms"], ["deepseek"])
        self.assertEqual(items[0]["completed_keyword_count"], 0)
        self.assertEqual(items[0]["total_keyword_count"], 1)

    def test_progress_status_does_not_keep_previous_failure_label(self) -> None:
        task = {**TASK, "recognition_batch_size": 3}
        dts.write_task_status(
            task,
            status="send_failed",
            source="recognition",
            message="旧失败",
            extra={"task_failure_kind": "notification"},
        )

        self.manager._persist_task_progress_status(
            task,
            current_count=1,
            batch_size=3,
            historical_count=0,
            status_message="识别模式补图中",
        )

        status = dts.get_task_day_status(task)
        self.assertEqual(status.get("status"), "running")
        self.assertNotEqual((status.get("extra") or {}).get("task_failure_kind"), "notification")

    def test_running_task_with_gap_is_not_reported_as_failed_today(self) -> None:
        task = {
            **TASK,
            "task_id": "task_running_gap",
            "keywords": [
                {"keyword": "词1", "brand": "品牌R", "platforms": ["doubao"], "mode": "recognition"},
                {"keyword": "词2", "brand": "品牌R", "platforms": ["doubao"], "mode": "recognition"},
            ],
        }
        screenshot_path = Path(self._tmpdir.name) / "screenshots" / "running_gap.jpg"
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        screenshot_path.write_bytes(b"fake-image")

        dts.start_formal_task_run(task, source="recognition", message="识别模式补图中")
        dts.apply_task_keyword_updates(
            task,
            [
                {
                    "keyword": "词1",
                    "brand": "品牌R",
                    "run_success": True,
                    "screenshot_saved": True,
                    "failure_reason": "",
                    "platform": "doubao",
                    "image_path": str(screenshot_path),
                }
            ],
            source_mode="formal",
        )

        from web_backend import _build_task_failure_summary

        status = dts.get_task_day_status(task)
        self.assertEqual(status.get("brand_status"), "running")
        self.assertTrue(status.get("has_gap"))

        failure_summary = _build_task_failure_summary(task)
        self.assertFalse(failure_summary.get("failedToday"))
        self.assertEqual(failure_summary.get("failedQueries"), [])

    def test_daily_progress_reads_completed_keywords_and_screenshots_from_pool(self) -> None:
        screenshots_dir = Path(self._tmpdir.name) / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = screenshots_dir / "recognition_success.jpg"
        screenshot_path.write_bytes(b"fake-image")

        dts.apply_task_keyword_updates(
            {
                "task_id": TASK["task_id"],
                "name": TASK["name"],
            },
            [
                {
                    "keyword": "词R",
                    "brand": "品牌R",
                    "run_success": True,
                    "screenshot_saved": True,
                    "failure_reason": "",
                    "platform": "doubao",
                    "image_path": str(screenshot_path),
                }
            ],
            source_mode="formal",
        )

        progress = self.manager._get_task_daily_progress(dict(TASK))
        expected_path = dts.get_task_day_status(
            {"task_id": TASK["task_id"], "name": TASK["name"]}
        )["keyword_states"]["词R"]["image_path"]
        self.assertEqual(progress.get("completed_keywords"), ["词R"])
        self.assertEqual(progress.get("historical_screenshot_paths"), [expected_path])
        self.assertEqual(progress.get("historical_query_platforms"), ["doubao"])
        self.assertEqual(progress.get("historical_screenshot_count"), 1)
        self.assertEqual(progress.get("historical_image_count_by_brand"), {"品牌R": 1})

    def test_send_batch_requires_unique_screenshot_for_each_keyword(self) -> None:
        send_path = Path(self._tmpdir.name) / "screenshots" / "send_once.jpg"
        send_path.parent.mkdir(parents=True, exist_ok=True)
        send_path.write_bytes(b"fake-image")

        task = {
            "name": "品牌R",
            "task_id": "task_r_send",
            "webhook_url": "https://example.com/webhook",
            "recognition_batch_size": 2,
            "keywords": [
                {"keyword": "词1", "brand": "品牌R", "platforms": ["doubao"], "mode": "recognition"},
                {"keyword": "词2", "brand": "品牌R", "platforms": ["deepseek"], "mode": "recognition"},
            ],
        }
        batch = {
            "id": "batch-1",
            "task_name": "品牌R",
            "brands": ["品牌R"],
            "image_paths": [str(send_path)],
            "image_items": [{"path": str(send_path), "ocr_text": "示例"}],
            "matched_pairs": [
                {"keyword": "词1", "brand": "品牌R", "platforms": ["doubao"]},
                {"keyword": "词2", "brand": "品牌R", "platforms": ["deepseek"]},
            ],
            "task": task,
        }

        notifier_calls: list[tuple] = []

        class FakeNotifier:
            def __init__(self, *args, **kwargs):
                self.last_error = ""

            def send_detected_images(self, *args, **kwargs):
                notifier_calls.append((args, kwargs))
                return True

        with patch("core.recognition.WeComNotifier", FakeNotifier):
            with patch.object(
                self.manager,
                "_prepare_send_images",
                return_value=([str(send_path)], ["doubao"]),
            ):
                self.manager._send_batch(batch)

        status = dts.get_task_day_status({"task_id": task["task_id"], "name": task["name"]})
        keyword_states = dict(status.get("keyword_states") or {})
        expected_first_path = keyword_states["词1"]["image_path"]

        self.assertEqual(notifier_calls, [])
        self.assertTrue(keyword_states["词1"]["run_success"])
        self.assertTrue(keyword_states["词1"]["screenshot_saved"])
        self.assertTrue(expected_first_path.endswith("_品牌R_词1_doubao.jpg"))
        self.assertTrue(keyword_states["词2"]["run_success"])
        self.assertFalse(keyword_states["词2"]["screenshot_saved"])
        self.assertEqual(keyword_states["词2"]["failure_reason"], "screenshot_save_failed")
        self.assertTrue(status.get("has_gap"))
        self.assertIn("词2：截图保存失败", status.get("gap_reasons") or [])

    def test_send_batch_uses_canonical_path_after_daily_state_moves_image(self) -> None:
        send_path = Path(self._tmpdir.name) / "screenshots" / "decorated" / "send_move.jpg"
        send_path.parent.mkdir(parents=True, exist_ok=True)
        send_path.write_bytes(b"fake-image")

        task = {
            "name": "品牌R",
            "task_id": "task_r_send_move",
            "webhook_url": "https://example.com/webhook",
            "recognition_batch_size": 1,
            "keywords": [
                {"keyword": "词R", "brand": "品牌R", "platforms": ["doubao"], "mode": "recognition"},
            ],
        }
        batch = {
            "id": "batch-move-1",
            "task_name": "品牌R",
            "brands": ["品牌R"],
            "image_paths": [str(send_path)],
            "image_items": [{"path": str(send_path), "ocr_text": "示例"}],
            "matched_pairs": [
                {"keyword": "词R", "brand": "品牌R", "platforms": ["doubao"]},
            ],
            "task": task,
        }

        notifier_calls: list[tuple] = []

        class FakeNotifier:
            def __init__(self, *args, **kwargs):
                self.last_error = ""

            def send_detected_images(self, *args, **kwargs):
                notifier_calls.append((args, kwargs))
                return True

        with patch("core.recognition.WeComNotifier", FakeNotifier):
            with patch.object(
                self.manager,
                "_prepare_send_images",
                return_value=([str(send_path)], ["doubao"]),
            ):
                self.manager._send_batch(batch)

        self.assertEqual(len(notifier_calls), 1)
        sent_paths = notifier_calls[0][1]["screenshot_paths"]
        self.assertEqual(len(sent_paths), 1)
        self.assertNotEqual(sent_paths[0], str(send_path))
        self.assertTrue(Path(sent_paths[0]).exists())
        self.assertTrue(sent_paths[0].endswith("_品牌R_词R_doubao.jpg"))

    def test_send_batch_enqueues_wecom_retry_on_notification_failure(self) -> None:
        send_path = Path(self._tmpdir.name) / "screenshots" / "retry" / "send_retry.jpg"
        send_path.parent.mkdir(parents=True, exist_ok=True)
        send_path.write_bytes(b"fake-image")

        task = {
            "name": "品牌R",
            "task_id": "task_r_send_retry",
            "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=PLACEHOLDER",
            "recognition_batch_size": 1,
            "keywords": [
                {"keyword": "词R", "brand": "品牌R", "platforms": ["doubao"], "mode": "recognition"},
            ],
        }
        batch = {
            "id": "batch-retry-1",
            "task_name": "品牌R",
            "brands": ["品牌R"],
            "image_paths": [str(send_path)],
            "image_items": [{"path": str(send_path), "ocr_text": "示例"}],
            "matched_pairs": [
                {"keyword": "词R", "brand": "品牌R", "platforms": ["doubao"]},
            ],
            "task": task,
        }
        notifier_calls: list[tuple] = []

        class FakeNotifier:
            def __init__(self, *args, **kwargs):
                self.last_error = "offline"

            def send_detected_images(self, *args, **kwargs):
                notifier_calls.append((args, kwargs))
                return False

        with patch("core.recognition.WeComNotifier", FakeNotifier):
            with patch.object(
                self.manager,
                "_prepare_send_images",
                return_value=([str(send_path)], ["doubao"]),
            ):
                self.manager._send_batch(batch)

        entries = load_retry_queue()
        status = dts.get_task_day_status({"task_id": task["task_id"], "name": task["name"]})
        retry_extra = status.get("official_extra") or {}

        self.assertEqual(len(notifier_calls), 1)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["last_error"], "offline")
        self.assertEqual(entries[0]["send_args"]["completed_keywords"], ["词R"])
        self.assertEqual(entries[0]["idempotency"]["task_id"], "task_r_send_retry")
        self.assertEqual(entries[0]["idempotency"]["channel"], "recognition_detected_images")
        self.assertTrue(entries[0]["idempotency"]["payload_hash"])
        self.assertTrue(Path(entries[0]["send_args"]["screenshot_paths"][0]).exists())
        self.assertEqual(status.get("status"), "send_failed")
        self.assertEqual(retry_extra.get("retry_queue_id"), entries[0]["id"])
        self.assertEqual(retry_extra.get("notification_success"), False)

    def test_manual_test_send_is_not_blocked_by_sent_today_flag(self) -> None:
        send_path = Path(self._tmpdir.name) / "screenshots" / "manual_test_send.jpg"
        send_path.parent.mkdir(parents=True, exist_ok=True)
        send_path.write_bytes(b"fake-image")

        task = {
            "name": "品牌R",
            "task_id": "task_r_manual_send",
            "brand": "品牌R",
            "webhook_url": "https://example.com/webhook",
            "recognition_batch_size": 1,
            "_daily_state_source": "manual_test",
            "keywords": [
                {"keyword": "词R", "brand": "品牌R", "platforms": ["doubao"], "mode": "recognition"},
            ],
        }
        dts.mark_task_sent(task, source_mode="formal", message="正式任务今日已发送")

        batch = {
            "id": "manual-batch-1",
            "task_name": "品牌R",
            "brands": ["品牌R"],
            "image_paths": [str(send_path)],
            "image_items": [{"path": str(send_path), "ocr_text": "示例", "source_text": "示例"}],
            "matched_pairs": [
                {"keyword": "词R", "brand": "品牌R", "platforms": ["doubao"]},
            ],
            "task": task,
        }

        notifier_calls: list[tuple] = []

        class FakeNotifier:
            def __init__(self, *args, **kwargs):
                self.last_error = ""

            def send_detected_images(self, *args, **kwargs):
                notifier_calls.append((args, kwargs))
                return True

        with patch("core.recognition.WeComNotifier", FakeNotifier):
            with patch.object(
                self.manager,
                "_prepare_send_images",
                return_value=([str(send_path)], ["doubao"]),
            ):
                self.manager._send_batch(batch)

        status = dts.get_task_day_status({"task_id": task["task_id"], "name": task["name"]})
        self.assertEqual(len(notifier_calls), 1)
        self.assertTrue(status["sent_today"])
        self.assertEqual(status["brand_status"], "sent")

    def test_manual_test_send_keeps_current_image_when_pool_has_previous_success(self) -> None:
        previous_path = Path(self._tmpdir.name) / "screenshots" / "previous_success.jpg"
        current_path = Path(self._tmpdir.name) / "screenshots" / "current_success.jpg"
        previous_path.parent.mkdir(parents=True, exist_ok=True)
        previous_path.write_bytes(b"previous-image")
        current_path.write_bytes(b"current-image")

        task = {
            "name": "品牌R",
            "task_id": "task_r_manual_pool_reuse",
            "brand": "品牌R",
            "webhook_url": "https://example.com/webhook",
            "recognition_batch_size": 1,
            "_daily_state_source": "manual_test",
            "keywords": [
                {"keyword": "词R", "brand": "品牌R", "platforms": ["doubao"], "mode": "recognition"},
            ],
        }
        batch = {
            "id": "manual-batch-current",
            "task_name": "品牌R",
            "brands": ["品牌R"],
            "image_paths": [str(current_path)],
            "image_items": [{"path": str(current_path), "ocr_text": "新一轮示例", "source_text": "新一轮示例"}],
            "matched_pairs": [
                {"keyword": "词R", "brand": "品牌R", "platforms": ["doubao"]},
            ],
            "task": task,
        }

        notifier_calls: list[tuple] = []

        class FakeNotifier:
            def __init__(self, *args, **kwargs):
                self.last_error = ""

            def send_detected_images(self, *args, **kwargs):
                notifier_calls.append((args, kwargs))
                return True

        stale_pool = {
            "keywords": {
                "词R": {
                    "run_success": True,
                    "screenshot_saved": True,
                    "image_path": str(previous_path),
                    "platform": "doubao",
                }
            }
        }

        with patch("core.recognition.WeComNotifier", FakeNotifier):
            with patch.object(
                self.manager,
                "_prepare_send_images",
                return_value=([str(current_path)], ["doubao"]),
            ):
                with patch("core.recognition.apply_task_keyword_updates", return_value=stale_pool):
                    with patch(
                        "core.recognition.get_task_day_status",
                        return_value={"has_gap": False, "completed_keywords": ["词R"]},
                    ):
                        with patch.object(
                            self.manager,
                            "_extract_completed_send_state_from_pool",
                            return_value=([str(previous_path)], ["doubao"]),
                        ) as extract_from_pool:
                            self.manager._send_batch(batch)

        self.assertEqual(len(notifier_calls), 1)
        self.assertEqual(notifier_calls[0][1]["screenshot_paths"], [str(current_path)])
        extract_from_pool.assert_not_called()

    def test_manual_test_send_uses_canonical_path_after_state_move(self) -> None:
        current_path = Path(self._tmpdir.name) / "screenshots" / "recognition" / "decorated" / "current_dom.jpg"
        current_path.parent.mkdir(parents=True, exist_ok=True)
        current_path.write_bytes(b"current-image")

        task = {
            "name": "品牌R",
            "task_id": "task_r_manual_moved_path",
            "brand": "品牌R",
            "webhook_url": "https://example.com/webhook",
            "recognition_batch_size": 1,
            "_daily_state_source": "manual_test",
            "keywords": [
                {"keyword": "词R", "brand": "品牌R", "platforms": ["doubao"], "mode": "recognition"},
            ],
        }
        batch = {
            "id": "manual-batch-moved-path",
            "task_name": "品牌R",
            "brands": ["品牌R"],
            "image_paths": [str(current_path)],
            "image_items": [{"path": str(current_path), "ocr_text": "新一轮示例", "source_text": "新一轮示例"}],
            "matched_pairs": [
                {"keyword": "词R", "brand": "品牌R", "platforms": ["doubao"]},
            ],
            "task": task,
        }

        notifier_calls: list[tuple] = []

        class FakeNotifier:
            def __init__(self, *args, **kwargs):
                self.last_error = ""

            def send_detected_images(self, *args, **kwargs):
                notifier_calls.append((args, kwargs))
                return True

        with patch("core.recognition.WeComNotifier", FakeNotifier):
            with patch.object(
                self.manager,
                "_prepare_send_images",
                return_value=([str(current_path)], ["doubao"]),
            ):
                self.manager._send_batch(batch)

        self.assertEqual(len(notifier_calls), 1)
        sent_paths = notifier_calls[0][1]["screenshot_paths"]
        self.assertEqual(len(sent_paths), 1)
        self.assertNotEqual(sent_paths[0], str(current_path))
        self.assertTrue(Path(sent_paths[0]).exists())

    def test_dom_text_send_rerenders_template_with_matched_keyword(self) -> None:
        send_path = Path(self._tmpdir.name) / "screenshots" / "dom_text.jpg"
        send_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (640, 960), "#ffffff").save(send_path, format="JPEG")

        batch = {
            "task_name": "品牌R",
            "brands": ["品牌R"],
            "task": dict(TASK),
            "image_items": [
                {
                    "path": str(send_path),
                    "ocr_text": "这是一段原始正文",
                    "source_text": "这是一段原始正文",
                }
            ],
            "matched_pairs": [
                {"keyword": "词R", "brand": "品牌R", "platforms": ["doubao"]},
            ],
        }

        render_calls: list[dict] = []

        def _fake_render_text_to_screenshot(text, platform, keyword="", brand="", output_path=None, include_badges=True):
            render_calls.append({
                "text": text,
                "platform": platform,
                "keyword": keyword,
                "brand": brand,
                "output_path": output_path,
                "include_badges": include_badges,
            })
            Path(output_path).write_bytes(b"rendered")
            return str(output_path)

        with patch.object(self.manager, "_resolve_image_platform", return_value="doubao"):
            with patch("platforms.html_renderer.render_text_to_screenshot", side_effect=_fake_render_text_to_screenshot):
                paths, platforms = self.manager._prepare_send_images(batch, {})

        self.assertEqual(platforms, ["doubao"])
        self.assertEqual(len(paths), 1)
        self.assertEqual(len(render_calls), 1)
        self.assertEqual(render_calls[0]["text"], "这是一段原始正文")
        self.assertEqual(render_calls[0]["keyword"], "词R")
        self.assertEqual(render_calls[0]["brand"], "品牌R")
        self.assertTrue(render_calls[0]["include_badges"])

    def test_dom_text_mode_queues_source_text_without_intermediate_render(self) -> None:
        config = {
            "detection_mode": "recognition",
            "recognition": {
                "dom_render_mode": True,
                "dom_render_min_length": 10,
                "dom_render_max_length": 50000,
            },
        }
        manager = ClipboardRecognitionManager(config_getter=lambda: config)
        manager._clipboard_armed = True
        manager.set_active_capture_platform("doubao")

        with patch.object(manager, "_poll_clipboard_text", return_value="这是一段包含品牌R的原始回答正文"):
            with patch.object(manager, "_match_brands_from_text", return_value=["品牌R"]):
                manager._poll_once_text_mode()

        self.assertEqual(manager._recognition_queue.qsize(), 1)
        payload = manager._recognition_queue.get_nowait()
        self.assertEqual(payload["path"], "")
        self.assertEqual(payload["source_text"], "这是一段包含品牌R的原始回答正文")
        self.assertEqual(payload["matched_brands"], ["品牌R"])
        self.assertTrue(payload["skip_ai_recognition"])
        self.assertEqual(payload["platform_hint"], "doubao")

    def test_dom_text_mode_skips_short_text_when_length_config_missing(self) -> None:
        config = {
            "detection_mode": "recognition",
            "recognition": {
                "dom_render_mode": True,
            },
        }
        manager = ClipboardRecognitionManager(config_getter=lambda: config)
        manager._clipboard_armed = True
        manager.set_active_capture_platform("doubao")

        with patch.object(manager, "_poll_clipboard_text", return_value="品牌R短文本"):
            with patch.object(manager, "_match_brands_from_text", return_value=["品牌R"]) as match_mock:
                manager._poll_once_text_mode()

        self.assertEqual(manager._dom_render_text_length_limits(), (500, 5000))
        self.assertEqual(manager._recognition_queue.qsize(), 0)
        self.assertIn("500-5000", manager._guide_status_text)
        match_mock.assert_not_called()

    def test_dom_text_send_rerenders_template_without_intermediate_image(self) -> None:
        batch = {
            "task_name": "品牌R",
            "brands": ["品牌R"],
            "task": dict(TASK),
            "image_items": [
                {
                    "path": "",
                    "ocr_text": "这是一段原始正文",
                    "source_text": "这是一段原始正文",
                }
            ],
            "matched_pairs": [
                {"keyword": "词R", "brand": "品牌R", "platforms": ["doubao"]},
            ],
        }

        render_calls: list[dict] = []

        def _fake_render_text_to_screenshot(text, platform, keyword="", brand="", output_path=None, include_badges=True):
            render_calls.append({
                "text": text,
                "platform": platform,
                "keyword": keyword,
                "brand": brand,
                "output_path": output_path,
                "include_badges": include_badges,
            })
            Path(output_path).write_bytes(b"rendered")
            return str(output_path)

        with patch.object(self.manager, "_resolve_image_platform", return_value="doubao"):
            with patch("platforms.html_renderer.render_text_to_screenshot", side_effect=_fake_render_text_to_screenshot):
                paths, platforms = self.manager._prepare_send_images(batch, {})

        self.assertEqual(platforms, ["doubao"])
        self.assertEqual(len(paths), 1)
        self.assertTrue(Path(paths[0]).exists())
        self.assertEqual(len(render_calls), 1)
        self.assertEqual(render_calls[0]["text"], "这是一段原始正文")
        self.assertEqual(render_calls[0]["keyword"], "词R")
        self.assertEqual(render_calls[0]["brand"], "品牌R")
        self.assertTrue(render_calls[0]["include_badges"])

    def test_dom_text_send_prefers_single_matched_platform_without_inference(self) -> None:
        batch = {
            "task_name": "品牌R",
            "brands": ["品牌R"],
            "task": dict(TASK),
            "image_items": [
                {
                    "path": "",
                    "ocr_text": "这是一段原始正文",
                    "source_text": "这是一段原始正文",
                }
            ],
            "matched_pairs": [
                {"keyword": "词R", "brand": "品牌R", "platforms": ["doubao"]},
            ],
        }

        render_calls: list[dict] = []

        def _fake_render_text_to_screenshot(text, platform, keyword="", brand="", output_path=None, include_badges=True):
            render_calls.append({
                "platform": platform,
                "keyword": keyword,
                "brand": brand,
            })
            Path(output_path).write_bytes(b"rendered")
            return str(output_path)

        with patch.object(self.manager, "_resolve_image_platform", side_effect=AssertionError("should not infer platform")):
            with patch("platforms.html_renderer.render_text_to_screenshot", side_effect=_fake_render_text_to_screenshot):
                paths, platforms = self.manager._prepare_send_images(batch, {})

        self.assertEqual(len(paths), 1)
        self.assertEqual(platforms, ["doubao"])
        self.assertEqual(render_calls, [{"platform": "doubao", "keyword": "词R", "brand": "品牌R"}])

    def test_dom_text_send_falls_back_to_original_image_when_template_render_fails(self) -> None:
        send_path = Path(self._tmpdir.name) / "screenshots" / "dom_text_fallback.jpg"
        send_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (640, 960), "#ffffff").save(send_path, format="JPEG")

        with patch("platforms.html_renderer.render_text_to_screenshot", return_value=""):
            result = self.manager._build_text_mode_send_image(
                image_path=str(send_path),
                source_text="原始正文",
                platform_name="doubao",
                brand="品牌R",
                keyword="词R",
            )

        self.assertEqual(result, str(send_path))

    def test_dom_text_no_platform_hint_sends_after_all_pending_platforms_complete(self) -> None:
        task = {
            "name": "品牌R",
            "task_id": "task_r_dom_no_hint",
            "brand": "品牌R",
            "enabled": True,
            "weekdays": [0, 1, 2, 3, 4, 5, 6],
            "webhook_url": "https://example.com/webhook",
            "recognition_batch_size": 1,
            "keywords": [
                {"keyword": "词R", "brand": "品牌R", "platforms": ["doubao", "deepseek"], "mode": "recognition"},
            ],
        }
        config = {
            "detection_mode": "recognition",
            "recognition": {"dom_render_mode": True},
            "tasks": [task],
        }
        manager = ClipboardRecognitionManager(config_getter=lambda: config)
        manager.set_active_capture_platform("deepseek")

        render_count = 0

        def _fake_render_text_to_screenshot(text, platform, keyword="", brand="", output_path=None, include_badges=True):
            nonlocal render_count
            render_count += 1
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(f"rendered-{platform}".encode("utf-8"))
            return str(output_path)

        notifier_calls: list[tuple] = []

        class FakeNotifier:
            def __init__(self, *args, **kwargs):
                self.last_error = ""

            def send_detected_images(self, *args, **kwargs):
                notifier_calls.append((args, kwargs))
                return True

        with patch("core.recognition.WeComNotifier", FakeNotifier):
            with patch("platforms.html_renderer.render_text_to_screenshot", side_effect=_fake_render_text_to_screenshot):
                tasks = manager._get_enabled_tasks()
                manager._route_to_batches(
                    image_path="",
                    brands=["品牌R"],
                    summary="检测到品牌: 品牌R",
                    tasks=tasks,
                    ocr_text="DeepSeek 回答提到品牌R",
                    platform_hint="",
                )
                first_batch = manager._send_queue.get_nowait()
                manager._send_batch(first_batch)

                self.assertEqual(notifier_calls, [])
                first_status = dts.get_task_day_status({"task_id": task["task_id"], "name": task["name"], **task})
                self.assertTrue(first_status.get("has_gap"))
                platform_states = dict(first_status["keyword_states"]["词R"].get("platform_states") or {})
                self.assertTrue(platform_states["deepseek"]["screenshot_saved"])
                self.assertFalse(dict(platform_states.get("doubao") or {}).get("screenshot_saved", False))

                tasks = manager._get_enabled_tasks()
                manager._route_to_batches(
                    image_path="",
                    brands=["品牌R"],
                    summary="检测到品牌: 品牌R",
                    tasks=tasks,
                    ocr_text="豆包回答提到品牌R",
                    platform_hint="",
                )
                second_batch = manager._send_queue.get_nowait()
                manager._send_batch(second_batch)

        self.assertEqual(render_count, 2)
        self.assertEqual(len(notifier_calls), 1)
        sent_paths = notifier_calls[0][1]["screenshot_paths"]
        self.assertEqual(len(sent_paths), 2)
        final_status = dts.get_task_day_status({"task_id": task["task_id"], "name": task["name"], **task})
        self.assertFalse(final_status.get("has_gap"))
        self.assertEqual(final_status.get("completed_keywords"), ["词R"])

    def test_manual_test_progress_ignores_official_history_for_batch_threshold(self) -> None:
        screenshots_dir = Path(self._tmpdir.name) / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        official_path = screenshots_dir / "official.jpg"
        current_path = screenshots_dir / "current.jpg"
        official_path.write_bytes(b"official-image")
        current_path.write_bytes(b"current-image")

        dts.apply_task_keyword_updates(
            {
                "task_id": TASK["task_id"],
                "name": TASK["name"],
            },
            [
                {
                    "keyword": "词R",
                    "brand": "品牌R",
                    "run_success": True,
                    "screenshot_saved": True,
                    "failure_reason": "",
                    "platform": "doubao",
                    "image_path": str(official_path),
                }
            ],
            source_mode="formal",
        )

        manual_task = {
            "name": "品牌R",
            "task_id": TASK["task_id"],
            "brand": "品牌R",
            "enabled": True,
            "recognition_enabled": True,
            "recognition_batch_size": 2,
            "_daily_state_source": "manual_test",
            "keywords": [
                {"keyword": "词R1", "brand": "品牌R", "platforms": ["doubao"], "mode": "recognition"},
                {"keyword": "词R2", "brand": "品牌R", "platforms": ["deepseek"], "mode": "recognition"},
            ],
        }
        config = {
            "detection_mode": "recognition",
            "tasks": [manual_task],
            "recognition": {"safe_mode_ocr_enabled": True},
        }
        manager = ClipboardRecognitionManager(config_getter=lambda: config)
        tasks = manager._get_enabled_tasks()

        manager._route_to_batches(
            image_path=str(current_path),
            brands=["品牌R"],
            summary="命中品牌R",
            tasks=tasks,
            ocr_text="品牌R",
        )

        self.assertEqual(manager._send_queue.qsize(), 0)
        self.assertEqual(len(manager._buffers.get("品牌R", [])), 1)
        progress = manager._get_task_daily_progress(tasks[0])
        self.assertEqual(progress.get("historical_screenshot_count"), 0)

    def test_manual_test_success_task_is_removed_from_enabled_queue(self) -> None:
        manual_task = {
            "name": "品牌R",
            "task_id": TASK["task_id"],
            "brand": "品牌R",
            "enabled": True,
            "recognition_enabled": True,
            "recognition_batch_size": 2,
            "_daily_state_source": "manual_test",
            "keywords": [
                {"keyword": "词R1", "brand": "品牌R", "platforms": ["doubao"], "mode": "recognition"},
            ],
        }
        dts.write_task_status(
            manual_task,
            status="success",
            source="manual_test",
            scope="test",
            message="识别模式测试发送成功",
            extra={},
        )
        config = {
            "detection_mode": "recognition",
            "tasks": [manual_task],
        }
        manager = ClipboardRecognitionManager(config_getter=lambda: config)

        self.assertEqual(manager._get_enabled_tasks(), [])

    def test_check_cycle_complete_keeps_recognition_running_when_keywords_remain(self) -> None:
        config = {
            "detection_mode": "recognition",
            "tasks": [
                {
                    "name": "标头",
                    "brand": "标头",
                    "enabled": True,
                    "recognition_enabled": True,
                    "recognition_batch_size": 2,
                    "weekdays": [0, 1, 2, 3, 4, 5, 6],
                    "keywords": [
                        {
                            "keyword": "标头炸串",
                            "brand": "标头",
                            "platforms": ["deepseek"],
                            "mode": "browser",
                        },
                        {
                            "keyword": "酸辣粉加盟",
                            "brand": "标头",
                            "platforms": ["deepseek"],
                            "mode": "browser",
                        },
                    ],
                }
            ],
        }
        manager = ClipboardRecognitionManager(config_getter=lambda: config)
        manager._work_started = True
        manager._matched_in_cycle = True
        task_id = dts.derive_task_id(config["tasks"][0])
        done_path = Path(self._tmpdir.name) / "screenshots" / "done.jpg"
        done_path.parent.mkdir(parents=True, exist_ok=True)
        done_path.write_bytes(b"done")

        dts.apply_task_keyword_updates(
            {
                "task_id": task_id,
                "name": "标头",
            },
            [
                {
                    "keyword": "标头炸串",
                    "brand": "标头",
                    "run_success": True,
                    "screenshot_saved": True,
                    "failure_reason": "",
                    "platform": "deepseek",
                    "image_path": str(done_path),
                }
            ],
            source_mode="formal",
        )

        stop_calls: list[str] = []
        mode_changes: list[tuple[str, str]] = []
        manager.stop = lambda: stop_calls.append("stop")
        manager._notify_mode_change = lambda mode, reason="": mode_changes.append((mode, reason))

        manager._check_cycle_complete()

        self.assertEqual(stop_calls, [])
        self.assertEqual(mode_changes, [])

    def test_claim_active_listener_stops_previous_manager(self) -> None:
        previous = ClipboardRecognitionManager(config_getter=lambda: {})
        current = ClipboardRecognitionManager(config_getter=lambda: {})
        stop_calls: list[str] = []
        previous.stop = lambda: stop_calls.append("stopped")

        previous._claim_active_listener()
        current._claim_active_listener()

        self.assertEqual(stop_calls, ["stopped"])
        self.assertIs(ClipboardRecognitionManager._active_listener, current)

    def test_cycle_complete_invokes_round_complete_callback(self) -> None:
        calls: list[dict] = []
        manager = ClipboardRecognitionManager(
            config_getter=lambda: {},
            on_round_complete=lambda payload: calls.append(dict(payload or {})),
        )
        manager._work_started = True
        manager._matched_in_cycle = True
        manager._has_unfinished_work = lambda: False
        manager._get_enabled_tasks = lambda: []
        manager._build_keyword_guide_items = lambda tasks: []
        manager.stop = lambda: None
        manager._notify_mode_change = lambda mode, reason="": None

        manager._check_cycle_complete()

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["mode"], "recognition")

    def test_send_detected_images_exposes_partial_failure_when_image_send_fails(self) -> None:
        send_path = Path(self._tmpdir.name) / "screenshots" / "notify.jpg"
        send_path.parent.mkdir(parents=True, exist_ok=True)
        send_path.write_bytes(b"fake-image")

        notifier = WeComNotifier(
            webhook_url=(
                "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
                "?key=PLACEHOLDER"
            ),
            cooldown_minutes=0,
            send_interval=0,
        )

        with patch.object(notifier, "_send_text", return_value=True):
            with patch.object(notifier, "_send_image", return_value=False):
                ok = notifier.send_detected_images(
                    task_name="品牌R",
                    brands=["品牌R"],
                    screenshot_paths=[str(send_path)],
                    completed_keywords=["词R"],
                )

        self.assertFalse(ok)
        self.assertIn("图片发送失败", notifier.last_error)

    def test_send_detected_images_does_not_sleep_before_local_image_processing(self) -> None:
        send_path = Path(self._tmpdir.name) / "screenshots" / "notify_fast.jpg"
        send_path.parent.mkdir(parents=True, exist_ok=True)
        send_path.write_bytes(b"fake-image")

        notifier = WeComNotifier(
            webhook_url=(
                "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
                "?key=PLACEHOLDER"
            ),
            cooldown_minutes=0,
            send_interval=3,
        )

        with patch.object(notifier, "_send_text", return_value=True):
            with patch.object(notifier, "_send_image", return_value=True):
                with patch("core.notifier.time.sleep") as sleep_mock:
                    ok = notifier.send_detected_images(
                        task_name="品牌R",
                        brands=["品牌R"],
                        screenshot_paths=[str(send_path)],
                        completed_keywords=["词R"],
                    )

        self.assertTrue(ok)
        sleep_mock.assert_not_called()

    def test_wecom_post_interval_is_shared_for_same_webhook(self) -> None:
        class FakeResponse:
            def json(self):
                return {"errcode": 0}

        webhook_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=PLACEHOLDER"
        first = WeComNotifier(webhook_url=webhook_url, cooldown_minutes=0, send_interval=2)
        second = WeComNotifier(webhook_url=webhook_url, cooldown_minutes=0, send_interval=2)
        first._session.post = lambda *args, **kwargs: FakeResponse()
        second._session.post = lambda *args, **kwargs: FakeResponse()

        with patch("core.notifier.time.sleep") as sleep_mock:
            self.assertTrue(first._post({"msgtype": "text", "text": {"content": "one"}}))
            self.assertTrue(second._post({"msgtype": "text", "text": {"content": "two"}}))

        self.assertTrue(sleep_mock.called)
        self.assertGreater(float(sleep_mock.call_args.args[0]), 0)


if __name__ == "__main__":
    unittest.main()
