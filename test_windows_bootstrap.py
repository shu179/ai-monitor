"""Tests for core.windows_bootstrap.

Tests run on any platform but mock sys.platform to exercise the Windows branch.
Patch ordering matters: sys.platform must be the LAST @patch (outermost) so it takes
effect first when the patched function runs.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch


def _reload_wb():
    """Force-reload windows_bootstrap module and reset state."""
    import importlib
    import core.windows_bootstrap
    importlib.reload(core.windows_bootstrap)
    core.windows_bootstrap._installed = False
    core.windows_bootstrap._ctrl_handler = None
    return core.windows_bootstrap


@contextmanager
def _temp_hide_attr(obj, name):
    """Temporarily remove *name* from *obj*, restoring it on exit."""
    had = hasattr(obj, name)
    saved = getattr(obj, name, None)
    if had:
        delattr(obj, name)
    try:
        yield
    finally:
        if had:
            setattr(obj, name, saved)


# ---------------------------------------------------------------------------
# Top-level install_windows_bootstrap()
# ---------------------------------------------------------------------------

class InstallBootstrapTests(unittest.TestCase):
    """Tests for install_windows_bootstrap()."""

    def test_non_windows_is_noop(self) -> None:
        """On non-Windows, no Windows sub-functions are called."""
        wb = _reload_wb()
        with patch.object(wb, "_install_event_loop_policy") as mock_loop, \
             patch.object(wb, "_set_process_dpi_awareness") as mock_dpi, \
             patch.object(wb, "_install_console_ctrl_handler") as mock_ctrl, \
             patch.object(wb, "_install_crash_log_fallback") as mock_crash, \
             patch("sys.platform", "linux"):  # must be outermost
            wb.install_windows_bootstrap()
            mock_loop.assert_not_called()
            mock_dpi.assert_not_called()
            mock_ctrl.assert_not_called()
            mock_crash.assert_not_called()

    def test_non_windows_keeps_installed_false(self) -> None:
        """On non-Windows, _installed stays False so the function is re-entrant."""
        wb = _reload_wb()
        wb._installed = False
        with patch("sys.platform", "linux"):
            wb.install_windows_bootstrap()
        self.assertFalse(wb._installed)

    def test_windows_calls_all_sub_functions(self) -> None:
        """On Windows, every sub-function is called exactly once."""
        wb = _reload_wb()
        wb._installed = False
        with patch.object(wb, "_install_event_loop_policy") as mock_loop, \
             patch.object(wb, "_set_process_dpi_awareness") as mock_dpi, \
             patch.object(wb, "_install_console_ctrl_handler") as mock_ctrl, \
             patch.object(wb, "_install_crash_log_fallback") as mock_crash, \
             patch("sys.platform", "win32"):  # must be outermost
            wb.install_windows_bootstrap()
            mock_loop.assert_called_once()
            mock_dpi.assert_called_once()
            mock_ctrl.assert_called_once()
            mock_crash.assert_called_once()

    def test_idempotent_second_call_skips(self) -> None:
        """Second call on Windows does nothing (idempotency)."""
        wb = _reload_wb()
        wb._installed = False
        with patch.object(wb, "_install_event_loop_policy") as mock_loop, \
             patch.object(wb, "_set_process_dpi_awareness") as mock_dpi, \
             patch.object(wb, "_install_console_ctrl_handler") as mock_ctrl, \
             patch.object(wb, "_install_crash_log_fallback") as mock_crash, \
             patch("sys.platform", "win32"):  # must be outermost
            wb.install_windows_bootstrap()  # first call: _installed=True
            wb.install_windows_bootstrap()  # second call: returns immediately
            mock_loop.assert_called_once()  # only called once, not twice


# ---------------------------------------------------------------------------
# _install_event_loop_policy
# ---------------------------------------------------------------------------

class EventLoopPolicyTests(unittest.TestCase):
    """Tests for _install_event_loop_policy."""

    def test_calls_set_event_loop_policy_with_windows_policy(self) -> None:
        """set_event_loop_policy is called with a WindowsSelectorEventLoopPolicy instance."""
        wb = _reload_wb()
        mock_policy_cls = MagicMock()
        mock_policy_instance = mock_policy_cls.return_value
        with patch.object(asyncio, "WindowsSelectorEventLoopPolicy", mock_policy_cls, create=True), \
             patch.object(asyncio, "set_event_loop_policy") as mock_set:
            wb._install_event_loop_policy()
        mock_policy_cls.assert_called_once()
        mock_set.assert_called_once_with(mock_policy_instance)

    def test_attr_error_is_swallowed(self) -> None:
        """AttributeError (missing WindowsSelectorEventLoopPolicy) is caught, not raised."""
        wb = _reload_wb()
        with _temp_hide_attr(asyncio, "WindowsSelectorEventLoopPolicy"), \
             patch.object(asyncio, "set_event_loop_policy") as mock_set:
            wb._install_event_loop_policy()  # must not raise
        mock_set.assert_not_called()

    def test_generic_exception_is_swallowed(self) -> None:
        """Generic Exception from set_event_loop_policy is caught, not raised."""
        wb = _reload_wb()
        mock_policy_cls = MagicMock()
        with patch.object(asyncio, "WindowsSelectorEventLoopPolicy", mock_policy_cls, create=True), \
             patch.object(asyncio, "set_event_loop_policy", side_effect=RuntimeError("boom")):
            wb._install_event_loop_policy()  # must not raise


# ---------------------------------------------------------------------------
# _set_process_dpi_awareness
# ---------------------------------------------------------------------------

class DpiAwarenessTests(unittest.TestCase):
    """Tests for _set_process_dpi_awareness."""

    @staticmethod
    def _mock_windll(shcore_raises: bool = False,
                     shcore_return: int = 0,
                     user32_raises: bool = False) -> MagicMock:
        mock_shcore = MagicMock()
        if shcore_raises:
            mock_shcore.SetProcessDpiAwareness.side_effect = Exception("not available")
        else:
            mock_shcore.SetProcessDpiAwareness.return_value = shcore_return
        mock_user32 = MagicMock()
        if user32_raises:
            mock_user32.SetProcessDPIAware.side_effect = Exception("not available")
        mock_windll = MagicMock()
        mock_windll.shcore = mock_shcore
        mock_windll.user32 = mock_user32
        return mock_windll

    def test_tries_shcore_first(self) -> None:
        """SetProcessDpiAwareness(2) is tried before SetProcessDPIAware()."""
        wb = _reload_wb()
        mock_windll = self._mock_windll(shcore_return=0)
        with patch("sys.platform", "win32"), \
             patch.dict(sys.modules, {"ctypes": MagicMock(windll=mock_windll)}):
            wb._set_process_dpi_awareness()
        mock_windll.shcore.SetProcessDpiAwareness.assert_called_once_with(2)
        mock_windll.user32.SetProcessDPIAware.assert_not_called()

    def test_e_accessdenied_skips_user32(self) -> None:
        """E_ACCESSDENIED means DPI already set — no fallback needed."""
        wb = _reload_wb()
        mock_windll = self._mock_windll(shcore_return=0x80070005)
        with patch("sys.platform", "win32"), \
             patch.dict(sys.modules, {"ctypes": MagicMock(windll=mock_windll)}):
            wb._set_process_dpi_awareness()
        mock_windll.user32.SetProcessDPIAware.assert_not_called()

    def test_non_zero_hresult_falls_back_to_user32(self) -> None:
        """Non-zero HRESULT (not E_ACCESSDENIED) triggers user32 fallback."""
        wb = _reload_wb()
        mock_windll = self._mock_windll(shcore_return=1)
        with patch("sys.platform", "win32"), \
             patch.dict(sys.modules, {"ctypes": MagicMock(windll=mock_windll)}):
            wb._set_process_dpi_awareness()
        mock_windll.user32.SetProcessDPIAware.assert_called_once_with()

    def test_falls_back_to_user32_when_shcore_raises(self) -> None:
        """When shcore raises, SetProcessDPIAware() is tried as fallback."""
        wb = _reload_wb()
        mock_windll = self._mock_windll(shcore_raises=True)
        with patch("sys.platform", "win32"), \
             patch.dict(sys.modules, {"ctypes": MagicMock(windll=mock_windll)}):
            wb._set_process_dpi_awareness()
        mock_windll.user32.SetProcessDPIAware.assert_called_once_with()

    def test_both_fail_is_non_fatal(self) -> None:
        """If both DPI calls fail, no exception propagates."""
        wb = _reload_wb()
        mock_windll = self._mock_windll(shcore_raises=True, user32_raises=True)
        with patch("sys.platform", "win32"), \
             patch.dict(sys.modules, {"ctypes": MagicMock(windll=mock_windll)}):
            wb._set_process_dpi_awareness()  # must not raise


# ---------------------------------------------------------------------------
# _install_console_ctrl_handler
# ---------------------------------------------------------------------------

class ConsoleCtrlHandlerTests(unittest.TestCase):
    """Tests for _install_console_ctrl_handler."""

    @staticmethod
    def _make_ctrl_mocks(kernel32_side_effect=None, kernel32_return=1):
        mock_kernel32 = MagicMock()
        if kernel32_side_effect is not None:
            mock_kernel32.SetConsoleCtrlHandler.side_effect = kernel32_side_effect
        else:
            mock_kernel32.SetConsoleCtrlHandler.return_value = kernel32_return
        mock_windll = MagicMock(kernel32=mock_kernel32)
        mock_wintypes = MagicMock()
        mock_wintypes.BOOL = int
        mock_wintypes.DWORD = int
        mock_ctypes = MagicMock()
        mock_ctypes.windll = mock_windll
        mock_ctypes.WINFUNCTYPE = MagicMock(return_value=lambda f: f)
        mock_ctypes.wintypes = mock_wintypes
        return mock_ctypes, mock_wintypes, mock_kernel32

    def test_calls_setconsolectrlhandler_success(self) -> None:
        """SetConsoleCtrlHandler is called with (handler, True) and returns success."""
        wb = _reload_wb()
        mock_ctypes, mock_wintypes, mock_kernel32 = self._make_ctrl_mocks(kernel32_return=1)
        with patch("sys.platform", "win32"), \
             patch.dict(sys.modules, {
                 "ctypes": mock_ctypes,
                 "ctypes.wintypes": mock_wintypes,
             }), \
             patch("core.shutdown.run_shutdown_callbacks"):
            wb._install_console_ctrl_handler()
        mock_kernel32.SetConsoleCtrlHandler.assert_called_once()
        call_args = mock_kernel32.SetConsoleCtrlHandler.call_args[0]
        self.assertEqual(call_args[1], True)  # second arg is True = add handler

    def test_handler_saved_at_module_level(self) -> None:
        """Handler is saved to module-level _ctrl_handler to prevent GC."""
        wb = _reload_wb()
        mock_ctypes, mock_wintypes, _ = self._make_ctrl_mocks()
        with patch("sys.platform", "win32"), \
             patch.dict(sys.modules, {
                 "ctypes": mock_ctypes,
                 "ctypes.wintypes": mock_wintypes,
             }), \
             patch("core.shutdown.run_shutdown_callbacks"):
            wb._install_console_ctrl_handler()
        self.assertIsNotNone(wb._ctrl_handler)

    def test_return_value_zero_logs_warning(self) -> None:
        """SetConsoleCtrlHandler returning 0 (failure) logs a warning but does not raise."""
        wb = _reload_wb()
        mock_ctypes, mock_wintypes, mock_kernel32 = self._make_ctrl_mocks(kernel32_return=0)
        with patch("sys.platform", "win32"), \
             patch.dict(sys.modules, {
                 "ctypes": mock_ctypes,
                 "ctypes.wintypes": mock_wintypes,
             }), \
             patch("core.shutdown.run_shutdown_callbacks"):
            wb._install_console_ctrl_handler()  # must not raise

    def test_failure_is_non_fatal(self) -> None:
        """If SetConsoleCtrlHandler raises, exception is caught and not re-raised."""
        wb = _reload_wb()
        mock_kernel32 = MagicMock()
        mock_kernel32.SetConsoleCtrlHandler.side_effect = OSError("not available")
        mock_windll = MagicMock(kernel32=mock_kernel32)
        mock_wintypes = MagicMock()
        mock_wintypes.BOOL = int
        mock_wintypes.DWORD = int
        mock_ctypes = MagicMock()
        mock_ctypes.windll = mock_windll
        mock_ctypes.WINFUNCTYPE = MagicMock(return_value=lambda f: f)
        mock_ctypes.wintypes = mock_wintypes
        with patch("sys.platform", "win32"), \
             patch.dict(sys.modules, {
                 "ctypes": mock_ctypes,
                 "ctypes.wintypes": mock_wintypes,
             }):
            wb._install_console_ctrl_handler()  # must not raise


# ---------------------------------------------------------------------------
# _install_crash_log_fallback
# ---------------------------------------------------------------------------

class CrashLogFallbackTests(unittest.TestCase):
    """Tests for _install_crash_log_fallback."""

    def test_skips_when_stderr_is_tty(self) -> None:
        """When stderr is a TTY (dev usage), excepthook is not replaced."""
        wb = _reload_wb()
        mock_stderr = MagicMock()
        mock_stderr.isatty.return_value = True
        orig_hook = sys.excepthook
        with patch("sys.platform", "win32"), \
             patch.object(sys, "stderr", mock_stderr):
            wb._install_crash_log_fallback()
        self.assertIs(sys.excepthook, orig_hook)

    def test_installs_excepthook_when_no_tty(self) -> None:
        """When stderr is not a TTY (console=False build), excepthook is replaced."""
        wb = _reload_wb()
        mock_stderr = MagicMock()
        mock_stderr.isatty.return_value = False
        orig_hook = sys.excepthook
        with patch("sys.platform", "win32"), \
             patch.object(sys, "stderr", mock_stderr), \
             patch("builtins.open", MagicMock()):
            # resolve_app_dir is imported inside the function; patch the source
            with patch("core.app_paths.resolve_app_dir", side_effect=Exception("unavailable")):
                wb._install_crash_log_fallback()
        self.assertIsNot(sys.excepthook, orig_hook)


if __name__ == "__main__":
    unittest.main()
