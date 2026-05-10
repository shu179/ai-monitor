import unittest
from unittest.mock import Mock, patch

from core import browser_processes
from platforms.base import BasePlatform, _is_safe_external_chrome_target_url


class BrowserProcessesTests(unittest.TestCase):
    def test_windows_powershell_owner_pids_parses_unique_pids(self):
        completed = Mock(stdout="123\nabc\n123\n456\n")
        with patch.object(browser_processes.sys, "platform", "win32"), patch.object(
            browser_processes.subprocess,
            "run",
            return_value=completed,
        ):
            self.assertEqual(
                browser_processes._profile_owner_pids_windows_powershell("C:/Profiles/Doubao"),
                [123, 456],
            )

    def test_windows_powershell_owner_pids_passes_profile_as_argument(self):
        completed = Mock(stdout="")
        target = "C:/Profiles/Bad'); Write-Output 999; ('"
        with patch.object(browser_processes.subprocess, "run", return_value=completed) as run:
            browser_processes._profile_owner_pids_windows_powershell(target)

        command = run.call_args.args[0]
        self.assertEqual(command[-1], target)
        self.assertIn("$args[0]", command[-2])
        self.assertNotIn(target, command[-2])

    def test_profile_owner_pids_uses_windows_fallback_without_psutil(self):
        with patch.object(browser_processes.sys, "platform", "win32"), patch.object(
            browser_processes,
            "_profile_owner_pids_psutil",
            return_value=[],
        ), patch.object(
            browser_processes,
            "_profile_owner_pids_windows_powershell",
            return_value=[789],
        ):
            self.assertEqual(browser_processes.browser_profile_owner_pids("C:/Profiles/Kimi"), [789])

    def test_profile_owner_pids_uses_lsof_on_non_windows_without_psutil(self):
        with patch.object(browser_processes.sys, "platform", "darwin"), patch.object(
            browser_processes,
            "_profile_owner_pids_psutil",
            return_value=[],
        ), patch.object(
            browser_processes,
            "_profile_owner_pids_lsof",
            return_value=[321],
        ):
            self.assertEqual(browser_processes.browser_profile_owner_pids("/tmp/profile"), [321])

    def test_extract_user_data_dirs_supports_equals_and_split_args(self):
        self.assertEqual(
            browser_processes._extract_user_data_dirs([
                "chrome",
                "--user-data-dir=/tmp/Profile A",
                "--flag",
                "--user-data-dir",
                "/tmp/Profile B",
            ]),
            ["/tmp/Profile A", "/tmp/Profile B"],
        )

    def test_profile_process_tree_is_orphaned_allows_children_of_orphan_root(self):
        with patch.object(browser_processes, "browser_profile_owner_pids", return_value=[10, 11, 12]), patch.object(
            browser_processes,
            "process_parent_pid",
            side_effect=lambda pid: {10: 1, 11: 10, 12: 10}[pid],
        ):
            self.assertTrue(browser_processes.browser_profile_process_tree_is_orphaned("/tmp/profile"))

    def test_profile_process_tree_is_not_orphaned_when_root_has_external_parent(self):
        with patch.object(browser_processes, "browser_profile_owner_pids", return_value=[10, 11]), patch.object(
            browser_processes,
            "process_parent_pid",
            side_effect=lambda pid: {10: 999, 11: 10}[pid],
        ):
            self.assertFalse(browser_processes.browser_profile_process_tree_is_orphaned("/tmp/profile"))

    def test_platform_release_reclaims_residual_profile_processes(self):
        platform = BasePlatform("/tmp/profile")
        platform.context = Mock()
        platform.context.pages = []
        platform._browser_connection = Mock()
        platform._external_browser_process = None
        platform._playwright = None

        with patch("platforms.base.browser_profile_owner_pids", return_value=[123, 456]) as owner_pids, patch(
            "platforms.base.terminate_browser_profile_processes",
            return_value=True,
        ) as terminate:
            platform._release_browser_handles(stop_playwright=True)

        owner_pids.assert_called_once_with("/tmp/profile")
        terminate.assert_called_once_with("/tmp/profile", graceful_timeout=3.0, force=True)
        platform.context = None
        platform.page = None

    def test_browser_extra_args_filters_profile_and_debug_overrides(self):
        platform = BasePlatform("/tmp/profile")
        platform.browser_extra_args = (
            "--disable-features=Translate --user-data-dir=/tmp/evil "
            "--remote-debugging-port=9222 https://example.com --lang=zh-CN"
        )

        self.assertEqual(
            platform._browser_extra_args_list(),
            ["--disable-features=Translate", "--lang=zh-CN"],
        )

    def test_external_chrome_target_url_rejects_flags_and_whitespace(self):
        self.assertTrue(_is_safe_external_chrome_target_url("https://example.com/chat"))
        for value in ("http://example.com", "--disable-web-security", "https://example.com/chat --flag"):
            with self.subTest(value=value):
                self.assertFalse(_is_safe_external_chrome_target_url(value))

    def test_taskkill_nonzero_records_diagnostics_and_returns_false(self):
        failed_proc = Mock(returncode=1)
        with patch.object(browser_processes.sys, "platform", "win32"), patch.object(
            browser_processes,
            "browser_profile_owner_pids",
            return_value=[999],
        ), patch.object(
            browser_processes,
            "wait_for_pids_exit",
            return_value=False,
        ), patch.object(
            browser_processes,
            "pid_is_alive",
            return_value=False,
        ), patch.object(
            browser_processes.os,
            "kill",
            side_effect=OSError("no such process"),
        ), patch.object(
            browser_processes.subprocess,
            "run",
            return_value=failed_proc,
        ), patch.object(
            browser_processes,
            "record_event_safe",
        ) as mock_diag:
            result = browser_processes.terminate_browser_profile_processes("/tmp/profile", force=True)
        self.assertFalse(result)
        taskkill_calls = [c for c in mock_diag.call_args_list if "taskkill" in c[0][1]]
        self.assertEqual(len(taskkill_calls), 1)
        self.assertEqual(taskkill_calls[0][1]["details"]["returncode"], 1)


if __name__ == "__main__":
    unittest.main()
