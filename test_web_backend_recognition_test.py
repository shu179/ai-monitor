#!/usr/bin/env python3

import unittest
from unittest.mock import Mock, patch

from web_backend import (
    AppRuntime,
    GET_EXACT_SESSION_TOKEN_REQUIRED_PATHS,
    _resolve_safe_browser_profile_dir,
    _validate_browser_target_url,
)
from core.app_paths import resolve_app_path


class FakeRecognitionManager:
    def __init__(self, config_getter=None, running=False, has_tasks=True, **kwargs):
        self.config_getter = config_getter
        self._running = running
        self._has_tasks = has_tasks
        self.started = False
        self.stopped = False
        self.imported_state = None
        self.kwargs = kwargs

    def get_runtime_status(self):
        return {"running": self._running}

    def has_recognition_tasks(self):
        return self._has_tasks

    def export_runtime_state(self):
        return {"running": self._running}

    def import_runtime_state(self, state):
        self.imported_state = state

    def _get_enabled_tasks(self):
        if not self._has_tasks:
            return []
        config = self.config_getter() if callable(self.config_getter) else {}
        return list((config or {}).get("tasks") or [])

    def start(self):
        self._running = True
        self.started = True

    def stop(self):
        self._running = False
        self.stopped = True

    def get_keyword_guide_state(self):
        return {
            "items": [
                {
                    "task_name": "品牌任务",
                    "keyword": "品牌词",
                    "brands": ["品牌任务"],
                    "platforms": ["doubao"],
                }
            ],
            "index": 0,
            "manual_mode": False,
        }


class FakeManualPlatform:
    def __init__(self):
        self.shown = False

    def _show_browser(self):
        self.shown = True


class FakeManualPlatformManager:
    def __init__(self):
        self.calls = []
        self.platform = FakeManualPlatform()

    def get_or_create(self, platform_name, factory):
        self.calls.append(platform_name)
        return self.platform

    def close_session(self, platform_name, reason=""):
        self.calls.append(("close", platform_name, reason))


class WebBackendRecognitionTestTests(unittest.TestCase):
    def test_sensitive_get_exact_paths_require_session_token(self):
        self.assertIn("/api/debug/paths", GET_EXACT_SESSION_TOKEN_REQUIRED_PATHS)
        self.assertIn("/api/browser-auth", GET_EXACT_SESSION_TOKEN_REQUIRED_PATHS)
        self.assertIn("/api/settings", GET_EXACT_SESSION_TOKEN_REQUIRED_PATHS)

    def test_browser_target_url_allows_only_https_urls(self):
        self.assertEqual(
            _validate_browser_target_url("https://example.com/chat"),
            "https://example.com/chat",
        )
        for value in (
            "http://example.com",
            "--disable-web-security",
            "https://example.com/\n--flag",
            "https://example.com/chat --flag",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _validate_browser_target_url(value)

    def test_browser_profile_dir_must_stay_under_app_data_roots(self):
        safe_path = resolve_app_path("user_data/test-browser-profile")
        self.assertEqual(_resolve_safe_browser_profile_dir(safe_path), safe_path.resolve())
        browser_profiles_path = resolve_app_path("browser_profiles/doubao/default")
        self.assertEqual(_resolve_safe_browser_profile_dir(browser_profiles_path), browser_profiles_path.resolve())
        with self.assertRaises(ValueError):
            _resolve_safe_browser_profile_dir("/tmp/surfaced-outside-profile")

    def test_sync_recognition_mode_does_not_autostart_listener(self):
        runtime = AppRuntime()
        runtime._recognition_manager = FakeRecognitionManager(running=False, has_tasks=True)

        runtime._sync_recognition_mode({"detection_mode": "recognition"})

        self.assertFalse(runtime._recognition_manager.started)
        self.assertFalse(runtime._recognition_manager.get_runtime_status()["running"])

    def test_default_recognition_manager_records_batches_and_summarizes_on_round_complete(self):
        runtime = AppRuntime()
        runtime._scheduler_reporter = Mock()

        with patch("core.recognition.ClipboardRecognitionManager", FakeRecognitionManager):
            manager = runtime._ensure_recognition_manager()

        self.assertIs(manager.kwargs["on_send_complete"], runtime._scheduler_reporter.record_recognition_result)
        self.assertIs(manager.kwargs["on_round_complete"], runtime._scheduler_reporter.send_recognition_round_summary)

    def test_begin_mode_round_starts_listener_when_scheduler_round_begins(self):
        runtime = AppRuntime()
        runtime._recognition_manager = FakeRecognitionManager(running=False, has_tasks=True)
        runtime.load_config = Mock(return_value={"detection_mode": "recognition", "tasks": []})

        with patch("web_backend.build_query_execution_policy", return_value=Mock(use_session_pool=False, use_platform_serial=False)):
            runtime.begin_mode_round("recognition", [], None, {})

        self.assertTrue(runtime._recognition_manager.started)
        self.assertTrue(runtime._recognition_manager.get_runtime_status()["running"])

    def test_selector_heal_runtime_safety_blocks_when_monitoring_runs(self):
        runtime = AppRuntime()
        runtime._scheduler = Mock()
        runtime._scheduler.get_status.return_value = {"running": True}
        runtime._scheduler.get_running_tasks.return_value = {}

        result = runtime._selector_heal_runtime_safety()

        self.assertFalse(result["runtime_safe"])
        self.assertIn("正式抓取", result["blocking_reason"])
        self.assertTrue(result["checks"]["monitoring_running"])

    def test_selector_heal_runtime_safety_blocks_while_scheduler_is_draining(self):
        runtime = AppRuntime()
        runtime._scheduler = Mock()
        runtime._scheduler.get_status.return_value = {"running": False}
        runtime._scheduler.get_running_tasks.return_value = {"品牌任务 [抓取模式]": 2.5}

        result = runtime._selector_heal_runtime_safety()

        self.assertFalse(result["runtime_safe"])
        self.assertIn("暂停收尾", result["blocking_reason"])
        self.assertFalse(result["checks"]["monitoring_running"])
        self.assertTrue(result["checks"]["scheduler_draining"])
        self.assertEqual(result["checks"]["scheduler_running_tasks"], {"品牌任务 [抓取模式]": 2.5})

    def test_running_base_recognition_manager_does_not_block_recognition_test(self):
        runtime = AppRuntime()
        runtime._recognition_manager = FakeRecognitionManager(running=True)
        runtime.load_config = Mock(return_value={
            "detection_mode": "recognition",
            "tasks": [
                {
                    "task_id": "task-1",
                    "name": "品牌任务",
                    "keywords": [{"keyword": "品牌词", "platforms": ["doubao"]}],
                }
            ],
        })

        with patch.object(runtime, "_start_recognition_test_session", return_value={"ok": True, "recognitionTest": True}) as start_mock:
            result = runtime.start_test_run_task("task-1")

        self.assertTrue(result["ok"])
        start_mock.assert_called_once()

    def test_start_test_run_rechecks_active_runs_before_queueing(self):
        runtime = AppRuntime()
        runtime.load_config = Mock(return_value={
            "detection_mode": "browser",
            "tasks": [
                {
                    "task_id": "task-1",
                    "name": "品牌任务",
                    "keywords": [{"keyword": "品牌词", "platforms": ["tongyi"]}],
                }
            ],
        })
        runtime._test_runs["active"] = {
            "status": "queued",
            "cancelRequested": False,
        }

        with patch.object(runtime, "_get_test_run_block_reason", return_value=""):
            result = runtime.start_test_run_task("task-1")

        self.assertFalse(result["ok"])
        self.assertIn("进行中的测试任务", result["message"])

    def test_recognition_test_message_uses_text_hint_in_dom_mode(self):
        runtime = AppRuntime()
        runtime.load_config = Mock(return_value={
            "recognition": {
                "dom_render_mode": True,
            }
        })
        runtime._recognition_manager = FakeRecognitionManager(running=False)
        task = {
            "task_id": "task-2",
            "name": "品牌任务",
            "recognition_batch_size": 1,
            "keywords": [{"keyword": "品牌词", "platforms": ["doubao"]}],
        }

        with patch("core.recognition.ClipboardRecognitionManager", FakeRecognitionManager):
            result = runtime._start_recognition_test_session(task)

        self.assertTrue(result["ok"])
        self.assertIn("复制新的品牌文本到剪切板", result["message"])
        runtime._stop_recognition_test_session(restore_previous=False)

    def test_start_recognition_test_session_resets_stale_test_success_status(self):
        runtime = AppRuntime()
        runtime.load_config = Mock(return_value={"recognition": {}})
        task = {
            "task_id": "task-3",
            "name": "品牌任务",
            "brand": "品牌任务",
            "recognition_batch_size": 2,
            "keywords": [{"keyword": "品牌词", "platforms": ["doubao"]}],
        }

        with patch.object(runtime, "_prime_manual_test_status") as prime_mock:
            with patch("core.recognition.ClipboardRecognitionManager", FakeRecognitionManager):
                result = runtime._start_recognition_test_session(task)

        self.assertTrue(result["ok"])
        prime_mock.assert_called_once()
        runtime._stop_recognition_test_session(restore_previous=False)

    def test_get_active_recognition_manager_reaps_finished_test_session(self):
        runtime = AppRuntime()
        runtime._recognition_manager = FakeRecognitionManager(running=True)
        runtime._recognition_test_manager = FakeRecognitionManager(running=False)
        runtime._recognition_test_session = {
            "task_id": "task-4",
            "task_name": "测试任务",
            "restore_running": False,
            "previous_state": None,
        }

        manager, scope = runtime._get_active_recognition_manager(init_if_missing=False)

        self.assertIs(manager, runtime._recognition_manager)
        self.assertEqual(scope, "default")
        self.assertIsNone(runtime._recognition_test_manager)

    def test_recognition_action_can_open_platform_from_current_keyword(self):
        runtime = AppRuntime()
        runtime._recognition_manager = FakeRecognitionManager(running=True)
        runtime.load_config = Mock(return_value={"recognition": {}})
        runtime._open_recognition_shared_browser_tab = Mock()

        result = runtime.recognition_action({"action": "open_platform", "platform": "doubao"})

        self.assertTrue(result["ok"])
        runtime._open_recognition_shared_browser_tab.assert_called_once_with("doubao")

    def test_recognition_action_can_trigger_system_screenshot(self):
        runtime = AppRuntime()
        runtime._recognition_manager = FakeRecognitionManager(running=True)
        runtime.load_config = Mock(return_value={"recognition": {"dom_render_mode": False}})

        with patch("web_backend.sys.platform", "darwin"):
            with patch("web_backend.subprocess.Popen") as popen_mock:
                result = runtime.recognition_action({"action": "screenshot"})

        self.assertTrue(result["ok"])
        popen_mock.assert_called_once()

    def test_prepare_app_update_requires_package_hash_before_download(self):
        runtime = AppRuntime()
        runtime.load_config = Mock(return_value={"app_update": {"manifest_url": "/tmp/manifest.json"}})
        with patch("web_backend.build_update_status", return_value={
            "ok": True,
            "download_url": "https://example.com/Surfaced.zip",
            "package_sha256": "",
        }):
            result = runtime.prepare_app_update({})

        self.assertFalse(result["ok"])
        self.assertIn("sha256", result["message"])

    def test_prepare_app_update_downloads_and_returns_install_plan(self):
        runtime = AppRuntime()
        runtime.load_config = Mock(return_value={"app_update": {"manifest_url": "/tmp/manifest.json"}})
        status = {
            "ok": True,
            "download_url": "https://example.com/Surfaced.zip",
            "package_sha256": "1" * 64,
        }
        prepared = {
            "archive_path": "/tmp/Surfaced.zip",
            "sha256": "1" * 64,
            "size": 123,
            "extract_dir": "/tmp/extracted",
            "source_dir": "/tmp/extracted/Surfaced",
        }
        with patch("web_backend.build_update_status", return_value=status), patch(
            "web_backend.prepare_update_package",
            return_value=prepared,
        ) as prepare_mock, patch.object(
            runtime,
            "get_local_update_plan",
            return_value={"ok": True, "plan": {"sourceDir": prepared["source_dir"]}},
        ) as plan_mock:
            result = runtime.prepare_app_update({})

        self.assertTrue(result["ok"])
        self.assertEqual(result["source_dir"], prepared["source_dir"])
        prepare_mock.assert_called_once_with(
            download_url=status["download_url"],
            expected_sha256=status["package_sha256"],
        )
        plan_mock.assert_called_once_with({"source_dir": prepared["source_dir"]})


if __name__ == "__main__":
    unittest.main()
