"""Windows-specific bootstrap: event loop policy, DPI awareness, console ctrl handler.

Safe to import on any platform — all Windows-only code is guarded by sys.platform checks.
Call install_windows_bootstrap() once at the earliest possible point in each entry point.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Callable

logger = logging.getLogger(__name__)

_installed = False  # idempotency guard


def install_windows_bootstrap() -> None:
    """Install all Windows-specific bootstrap hooks. No-op on non-Windows."""
    global _installed
    if _installed:
        logger.debug("windows_bootstrap already installed, skipping")
        return
    if sys.platform != "win32":
        return

    _installed = True
    _install_event_loop_policy()
    _set_process_dpi_awareness()
    _install_console_ctrl_handler()
    _install_crash_log_fallback()
    logger.info("windows_bootstrap installed")


# ---------------------------------------------------------------------------
# Event loop policy
# ---------------------------------------------------------------------------

def _install_event_loop_policy() -> None:
    """Set WindowsSelectorEventLoopPolicy so asyncio works with subprocesses."""
    try:
        policy = asyncio.WindowsSelectorEventLoopPolicy()
        asyncio.set_event_loop_policy(policy)
        logger.debug("WindowsSelectorEventLoopPolicy set")
    except AttributeError:
        logger.warning("WindowsSelectorEventLoopPolicy not available in this Python build")
    except Exception:
        logger.warning("Failed to set WindowsSelectorEventLoopPolicy", exc_info=True)


# ---------------------------------------------------------------------------
# DPI awareness
# ---------------------------------------------------------------------------

def _set_process_dpi_awareness() -> None:
    """Enable Per-Monitor DPI awareness so the app renders crisply on HiDPI."""
    S_OK = 0
    E_ACCESSDENIED = 0x80070005  # already set by manifest or another call

    try:
        import ctypes
        result = ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        if result == S_OK:
            logger.debug("SetProcessDpiAwareness(2) succeeded")
            return
        if result == E_ACCESSDENIED:
            logger.debug("DPI awareness already set (E_ACCESSDENIED), skipping")
            return
        logger.debug("SetProcessDpiAwareness(2) returned 0x%08x, falling back", result)
    except Exception:
        pass

    try:
        import ctypes
        ctypes.windll.user32.SetProcessDPIAware()
        logger.debug("SetProcessDPIAware() fallback succeeded")
    except Exception:
        logger.debug("DPI awareness calls failed — non-critical, continuing")


# ---------------------------------------------------------------------------
# Console ctrl handler
# ---------------------------------------------------------------------------

# Must be kept alive at module level to prevent GC of the ctypes callback.
_ctrl_handler: Callable | None = None


def _install_console_ctrl_handler() -> None:
    """Register a Windows console ctrl handler that triggers shutdown."""
    global _ctrl_handler

    try:
        import ctypes
        from ctypes import wintypes

        from core.shutdown import run_shutdown_callbacks

        CTRL_C_EVENT = 0
        CTRL_BREAK_EVENT = 1
        CTRL_CLOSE_EVENT = 2
        CTRL_LOGOFF_EVENT = 5
        CTRL_SHUTDOWN_EVENT = 6

        _HANDLED_EVENTS = {CTRL_C_EVENT, CTRL_BREAK_EVENT, CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT}
        _EVENT_NAMES = {
            CTRL_C_EVENT: "CTRL_C",
            CTRL_BREAK_EVENT: "CTRL_BREAK",
            CTRL_CLOSE_EVENT: "CTRL_CLOSE",
            CTRL_LOGOFF_EVENT: "CTRL_LOGOFF",
            CTRL_SHUTDOWN_EVENT: "CTRL_SHUTDOWN",
        }

        _shutdown_triggered = False

        HANDLER_TYPE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        @HANDLER_TYPE
        def _handler(ctrl_type: int) -> bool:
            nonlocal _shutdown_triggered
            if ctrl_type not in _HANDLED_EVENTS:
                return False
            if _shutdown_triggered:
                return True
            _shutdown_triggered = True
            reason = f"windows console ctrl {_EVENT_NAMES.get(ctrl_type, ctrl_type)}"
            logger.info("Received %s, triggering shutdown", reason)
            try:
                run_shutdown_callbacks(reason)
            except Exception:
                logger.warning("Error in shutdown callback from console ctrl handler", exc_info=True)
            return True

        _ctrl_handler = _handler  # prevent GC
        if not ctypes.windll.kernel32.SetConsoleCtrlHandler(_handler, True):
            logger.warning("SetConsoleCtrlHandler returned 0 (failure), last_error=%s",
                           ctypes.get_last_error())
        else:
            logger.debug("Console ctrl handler registered")
    except Exception:
        logger.warning("Failed to install console ctrl handler", exc_info=True)


# ---------------------------------------------------------------------------
# Crash log fallback (for console=False PyInstaller builds)
# ---------------------------------------------------------------------------

def _install_crash_log_fallback() -> None:
    """Redirect unhandled exceptions to a crash log file when running as a GUI app.

    Only activates when sys.stderr is None or not a TTY (common with
    console=False PyInstaller builds). Does NOT interfere with normal
    terminal/dev usage where stderr is a real TTY.
    """
    if sys.stderr is not None:
        try:
            if sys.stderr.isatty():
                return
        except Exception:
            pass

    try:
        from core.app_paths import resolve_app_dir
        log_dir = str(resolve_app_dir("logs"))
    except Exception:
        import os
        log_dir = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "logs")
        os.makedirs(log_dir, exist_ok=True)

    import os
    crash_log_path = os.path.join(log_dir, "crash.log")

    try:
        crash_file = open(crash_log_path, "a", encoding="utf-8")  # noqa: SIM115

        _original_excepthook = sys.excepthook

        def _crash_excepthook(exc_type, exc_value, exc_tb):
            import traceback
            try:
                crash_file.write("".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
                crash_file.flush()
            except Exception:
                pass
            if _original_excepthook is not None:
                try:
                    _original_excepthook(exc_type, exc_value, exc_tb)
                except Exception:
                    pass

        sys.excepthook = _crash_excepthook

        if sys.stderr is None:
            sys.stderr = crash_file
        logger.debug("Crash log fallback installed at %s", crash_log_path)
    except Exception:
        logger.debug("Crash log fallback setup failed — non-critical", exc_info=True)
