#!/usr/bin/env python3

from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from web_backend import (
    AppRuntime,
    GET_EXACT_SESSION_TOKEN_REQUIRED_PATHS,
    _resolve_safe_browser_profile_dir,
    _validate_browser_target_url,
)
from core.app_paths import resolve_app_path
from core.time_utils import local_now


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

    def test_browser_auth_sessions_are_pruned_after_ttl(self):
        runtime = AppRuntime()
        old_time = (local_now() - timedelta(hours=7)).isoformat(timespec="seconds")
        fresh_time = local_now().isoformat(timespec="seconds")
        runtime._browser_auth_state.set("doubao", {
            "platform": None,
            "opened_at": old_time,
            "profile_path": "/tmp/doubao-auth",
            "external_in_use": True,
        })
        runtime._browser_auth_state.set("kimi", {
            "platform": None,
            "opened_at": fresh_time,
            "profile_path": "/tmp/kimi-auth",
            "external_in_use": True,
        })

        with patch.object(runtime, "_terminate_browser_profile_processes", return_value=True) as terminate:
            runtime._prune_browser_auth_sessions()

        self.assertNotIn("doubao", runtime._browser_auth_state.list_platforms())
        self.assertIn("kimi", runtime._browser_auth_state.list_platforms())
        terminate.assert_called_once_with("/tmp/doubao-auth", graceful_timeout=8.0, force=True)

    def test_recent_external_browser_auth_session_can_be_alive_without_pid(self):
        runtime = AppRuntime()
        session = {
            "platform": None,
            "opened_at": local_now().isoformat(timespec="seconds"),
            "profile_path": "/tmp/doubao-auth",
            "external_in_use": True,
        }

        with patch.object(runtime, "_is_browser_profile_in_use", return_value=True):
            self.assertTrue(runtime._is_browser_auth_session_alive(session))

    def test_sync_recognition_mode_does_not_autostart_listener(self):
        runtime = AppRuntime()
        runtime._recognition_manager = FakeRecognitionManager(running=False, has_tasks=True)

        runtime._sync_recognition_mode({"detection_mode": "recognition"})

        self.assertFalse(runtime._recognition_manager.started)
        self.assertFalse(runtime._recognition_manager.get_runtime_status()["running"])

    def test_terminal_test_runs_are_pruned_after_ttl(self):
        runtime = AppRuntime()
        old_time = (local_now() - timedelta(hours=7)).isoformat(timespec="seconds")
        fresh_time = local_now().isoformat(timespec="seconds")
        with runtime._test_run_lock:
            runtime._test_run_state.create("old-run", {
                "runId": "old-run",
                "status": "success",
                "finishedAt": old_time,
                "updatedAt": old_time,
            }, cancel_event=Mock())
            runtime._test_run_state.create("active-run", {
                "runId": "active-run",
                "status": "running",
                "updatedAt": fresh_time,
            })

        result = runtime.get_test_run_status("old-run")

        self.assertFalse(result["ok"])
        self.assertEqual(runtime._test_run_state.get("old-run"), {})
        self.assertIsNone(runtime._test_run_state.get_cancel_event("old-run"))
        self.assertIn("active-run", runtime._test_run_state.active_run_ids())

    def test_search_file_caches_prune_missing_and_stale_entries(self):
        runtime = AppRuntime()
        old_time = (local_now() - timedelta(days=2)).isoformat(timespec="seconds")
        fresh_time = local_now().isoformat(timespec="seconds")
        with tempfile.TemporaryDirectory() as tmpdir:
            fresh_path = Path(tmpdir) / "fresh.xlsx"
            stale_path = Path(tmpdir) / "stale.xlsx"
            fresh_path.write_bytes(b"fresh")
            stale_path.write_bytes(b"stale")
            with runtime._lock:
                runtime._search_uploads = {
                    "fresh": {"path": str(fresh_path), "uploaded_at": fresh_time},
                    "stale": {"path": str(stale_path), "uploaded_at": old_time},
                    "missing": {"path": str(Path(tmpdir) / "missing.xlsx"), "uploaded_at": fresh_time},
                }
                runtime._search_outputs = {
                    "fresh-output": {"path": str(fresh_path), "created_at": fresh_time},
                    "stale-output": {"path": str(stale_path), "created_at": old_time},
                }
                runtime._prune_search_file_caches_locked()

        self.assertEqual(sorted(runtime._search_uploads.keys()), ["fresh"])
        self.assertEqual(sorted(runtime._search_outputs.keys()), ["fresh-output"])

    def test_default_recognition_manager_records_batches_and_summarizes_on_round_complete(self):
        runtime = AppRuntime()
        runtime._scheduler_reporter = Mock()

        with patch("core.recognition.ClipboardRecognitionManager", FakeRecognitionManager):
            manager = runtime._ensure_recognition_manager()

        self.assertIs(manager.kwargs["on_send_complete"], runtime._scheduler_reporter.record_recognition_result)
        payload = {"mode": "recognition", "completed": 1}
        with patch.object(runtime, "_process_pending_task_deletions") as process_pending:
            manager.kwargs["on_round_complete"](payload)
        runtime._scheduler_reporter.send_recognition_round_summary.assert_called_once_with(payload)
        process_pending.assert_called_once()

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
        runtime._test_run_state.create("active", {
            "status": "queued",
            "cancelRequested": False,
        })

        with patch.object(runtime, "_get_test_run_block_reason", return_value=""):
            result = runtime.start_test_run_task("task-1")

        self.assertFalse(result["ok"])
        self.assertIn("进行中的测试任务", result["message"])

    def test_failed_test_run_status_exposes_force_send_when_screenshots_exist(self):
        runtime = AppRuntime()
        runtime.load_config = Mock(return_value={
            "tasks": [
                {
                    "task_id": "task-1",
                    "name": "品牌任务",
                    "brand": "品牌A",
                }
            ],
        })
        runtime._test_run_state.create("run-1", {
            "runId": "run-1",
            "taskId": "task-1",
            "status": "failed",
            "message": "测试失败",
        })

        with patch("web_backend._collect_today_successful_task_payload", return_value={
            "screenshotPaths": ["/tmp/ok.png"],
            "actualScreenshotCount": 1,
        }):
            status = runtime.get_test_run_status("run-1")

        self.assertTrue(status["ok"])
        self.assertTrue(status["canForceSendSuccess"])
        self.assertEqual(status["sendableSuccessCount"], 1)

        with patch("web_backend._collect_today_successful_task_payload", return_value={
            "screenshotPaths": [],
            "actualScreenshotCount": 0,
        }):
            status = runtime.get_test_run_status("run-1")

        self.assertFalse(status["canForceSendSuccess"])
        self.assertEqual(status["sendableSuccessCount"], 0)

    def test_force_send_successful_task_results_marks_failed_test_run_success(self):
        runtime = AppRuntime()
        task = {
            "task_id": "task-1",
            "name": "品牌任务",
            "brand": "品牌A",
            "cloud_task_id": 9,
            "webhook_url": "https://example.com/test-webhook",
        }
        runtime.load_config = Mock(return_value={"tasks": [task]})
        runtime._set_test_failure_notice("task-1", "测试失败", run_id="run-1")
        self.assertEqual(runtime._get_test_failure_notice("task-1")["runId"], "run-1")
        runtime._test_run_state.create("run-1", {
            "runId": "run-1",
            "taskId": "task-1",
            "status": "failed",
            "message": "测试失败",
            "result": "failed",
            "errorMessage": "未补齐",
            "failureDetails": [{"keyword": "词1"}],
        })
        notifier = Mock()
        notifier.last_error = ""
        notifier.send_detected_images.return_value = True

        with patch("web_backend._collect_today_successful_task_payload", return_value={
            "taskName": "品牌任务",
            "brands": ["品牌A"],
            "completedKeywords": ["词1"],
            "detectedPlatforms": ["doubao"],
            "screenshotPaths": ["/tmp/ok.png"],
            "actualScreenshotCount": 1,
        }):
            with patch("web_backend.WeComNotifier", return_value=notifier):
                with patch("web_backend.write_task_status"):
                    with patch("web_backend.update_cycle_report_with_forced_success", return_value=None):
                        with patch("web_backend.enqueue_task_day_status") as enqueue_status:
                            result = runtime.force_send_successful_task_results("task-1")

        self.assertTrue(result["ok"])
        enqueue_status.assert_called_once()
        cloud_payload = enqueue_status.call_args.args[0]
        self.assertEqual(cloud_payload["task_id"], 9)
        self.assertEqual(cloud_payload["source"], "dashboard_force_send")
        self.assertTrue(cloud_payload["notification_success"])
        self.assertTrue(cloud_payload["forced_ignore_failure"])
        status = runtime.get_test_run_status("run-1")
        self.assertEqual(status["status"], "success")
        self.assertEqual(status["result"], "success")
        self.assertFalse(status["canForceSendSuccess"])
        self.assertEqual(status["failureDetails"], [])
        self.assertIsNone(runtime._get_test_failure_notice("task-1"))

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

    def test_finished_recognition_test_session_stays_active_until_window_close(self):
        runtime = AppRuntime()
        runtime._recognition_manager = FakeRecognitionManager(running=True)
        runtime._recognition_test_manager = FakeRecognitionManager(running=False)
        runtime._recognition_test_session = {
            "task_id": "task-4",
            "task_name": "测试任务",
            "restore_running": True,
            "previous_state": {"manual_index": 1},
            "keep_until_closed": True,
        }

        manager, scope = runtime._get_active_recognition_manager(init_if_missing=False)

        self.assertIs(manager, runtime._recognition_test_manager)
        self.assertEqual(scope, "test")
        self.assertIsNotNone(runtime._recognition_test_manager)

        result = runtime.recognition_action({"action": "stop"})

        self.assertTrue(result["ok"])
        self.assertIsNone(runtime._recognition_test_manager)
        self.assertTrue(runtime._recognition_manager.started)
        self.assertEqual(runtime._recognition_manager.imported_state, {"manual_index": 1})

    def test_recognition_action_can_open_platform_from_current_keyword(self):
        runtime = AppRuntime()
        runtime._recognition_manager = FakeRecognitionManager(running=True)
        runtime.load_config = Mock(return_value={"recognition": {}})
        runtime._open_recognition_shared_browser_tab = Mock()

        result = runtime.recognition_action({"action": "open_platform", "platform": "doubao"})

        self.assertTrue(result["ok"])
        runtime._open_recognition_shared_browser_tab.assert_called_once_with("doubao")

    def test_toggle_recognition_browser_uses_actual_minimized_state(self):
        runtime = AppRuntime()
        runtime._recognition_browser_minimized = False

        with patch.object(runtime, "_recognition_browser_window_state", return_value="minimized"), patch.object(
            runtime,
            "_restore_recognition_shared_browser",
            return_value=True,
        ) as restore_mock, patch.object(runtime, "_minimize_recognition_shared_browser") as minimize_mock:
            toggled_to, changed = runtime._toggle_recognition_shared_browser()

        self.assertEqual(toggled_to, "restore")
        self.assertTrue(changed)
        self.assertFalse(runtime._recognition_browser_minimized)
        restore_mock.assert_called_once()
        minimize_mock.assert_not_called()

    def test_toggle_recognition_browser_actual_state_overrides_stale_flag(self):
        runtime = AppRuntime()
        runtime._recognition_browser_minimized = True

        with patch.object(runtime, "_recognition_browser_window_state", return_value="normal"), patch.object(
            runtime,
            "_minimize_recognition_shared_browser",
            return_value=True,
        ) as minimize_mock, patch.object(runtime, "_restore_recognition_shared_browser") as restore_mock:
            toggled_to, changed = runtime._toggle_recognition_shared_browser()

        self.assertEqual(toggled_to, "minimize")
        self.assertTrue(changed)
        self.assertTrue(runtime._recognition_browser_minimized)
        minimize_mock.assert_called_once()
        restore_mock.assert_not_called()

    def test_recognition_browser_window_state_reads_cdp_window_bounds(self):
        runtime = AppRuntime()
        runtime._recognition_browser_tabs = {"doubao": "tab-1"}

        def fake_browser_json(path, *, timeout=1.0):
            if path == "/json/list":
                return [{"id": "tab-1", "type": "page", "url": "https://example.com"}]
            return None

        runtime._recognition_browser_json = Mock(side_effect=fake_browser_json)
        runtime._cdp_browser_command = Mock(return_value={
            "id": 1,
            "result": {"windowId": 7, "bounds": {"windowState": "minimized"}},
        })

        self.assertEqual(runtime._recognition_browser_window_state(), "minimized")
        runtime._cdp_browser_command.assert_called_once_with(
            "Browser.getWindowForTarget",
            {"targetId": "tab-1"},
            timeout=1.5,
        )

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
