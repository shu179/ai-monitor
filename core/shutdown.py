"""Process shutdown helpers.

This module keeps exit cleanup centralized so browser/runtime resources are
released on both normal app shutdown and cooperative process termination.
"""

from __future__ import annotations

import atexit
import logging
import signal
import threading
from collections.abc import Callable, Iterable

_Callback = Callable[[], None]

_lock = threading.RLock()
_callbacks: list[tuple[str, _Callback]] = []
_atexit_registered = False
_signals_installed: set[int] = set()
_previous_handlers: dict[int, signal.Handlers] = {}
_running = False
_completed = False


def register_shutdown_callback(name: str, callback: _Callback) -> _Callback:
    """Register or replace a named cleanup callback."""
    if not callable(callback):
        raise TypeError("shutdown callback must be callable")
    normalized = str(name or "").strip() or getattr(callback, "__name__", "callback")
    with _lock:
        _ensure_atexit_registered_locked()
        _callbacks[:] = [(item_name, item) for item_name, item in _callbacks if item_name != normalized]
        _callbacks.append((normalized, callback))
    return callback


def run_shutdown_callbacks(reason: str = "shutdown") -> None:
    """Run registered callbacks once, in reverse registration order."""
    global _completed, _running
    with _lock:
        if _completed or _running:
            return
        _running = True
        callbacks = list(reversed(_callbacks))

    try:
        for name, callback in callbacks:
            try:
                callback()
            except Exception:
                logging.exception("退出清理失败: %s (%s)", name, reason)
    finally:
        with _lock:
            _completed = True
            _running = False


def install_shutdown_handlers(signals_to_handle: Iterable[int] | None = None) -> None:
    """Install a SIGTERM handler and an atexit fallback.

    The handler raises SystemExit after cleanup, so normal finally blocks and
    atexit handlers still get a chance to run. SIGKILL cannot be handled.
    """
    with _lock:
        _ensure_atexit_registered_locked()

    for signum in signals_to_handle or _default_signal_numbers():
        signum_int = int(signum)
        with _lock:
            if signum_int in _signals_installed:
                continue
        try:
            previous = signal.getsignal(signum_int)
        except Exception:
            continue
        if previous == signal.SIG_IGN:
            continue
        try:
            signal.signal(signum_int, _handle_signal)
        except (OSError, RuntimeError, ValueError):
            continue
        with _lock:
            _signals_installed.add(signum_int)
            _previous_handlers[signum_int] = previous


def _default_signal_numbers() -> tuple[int, ...]:
    sigterm = getattr(signal, "SIGTERM", None)
    return (int(sigterm),) if sigterm is not None else ()


def _ensure_atexit_registered_locked() -> None:
    global _atexit_registered
    if _atexit_registered:
        return
    atexit.register(run_shutdown_callbacks, "atexit")
    _atexit_registered = True


def _handle_signal(signum: int, frame) -> None:
    del frame
    signum_int = int(signum)
    try:
        logging.warning("收到退出信号 %s，开始清理运行资源", signum_int)
    except Exception:
        pass
    run_shutdown_callbacks(f"signal {signum_int}")
    previous = _previous_handlers.get(signum_int)
    if callable(previous) and previous is not _handle_signal:
        try:
            previous(signum, None)
        except SystemExit:
            raise
        except Exception:
            logging.exception("执行原信号处理器失败: %s", signum_int)
    raise SystemExit(128 + signum_int)
