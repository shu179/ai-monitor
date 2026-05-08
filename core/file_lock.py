from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable


class _PathLockState:
    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._owner_thread_id: int | None = None
        self._depth = 0
        self._handle = None

    def acquire(self, path: Path) -> None:
        thread_id = threading.get_ident()
        with self._condition:
            while self._owner_thread_id is not None and self._owner_thread_id != thread_id:
                self._condition.wait()
            if self._owner_thread_id == thread_id:
                self._depth += 1
                return
            self._owner_thread_id = thread_id
            self._depth = 1

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+b")
            try:
                _lock_file(handle)
            except BaseException:
                handle.close()
                raise
            with self._condition:
                self._handle = handle
        except BaseException:
            with self._condition:
                self._owner_thread_id = None
                self._depth = 0
                self._handle = None
                self._condition.notify_all()
            raise

    def release(self) -> None:
        thread_id = threading.get_ident()
        with self._condition:
            if self._owner_thread_id != thread_id or self._depth <= 0:
                raise RuntimeError("cannot release an unlocked CrossProcessRLock")
            self._depth -= 1
            if self._depth > 0:
                return
            handle = self._handle
            self._handle = None
            try:
                if handle is not None:
                    try:
                        _unlock_file(handle)
                    finally:
                        handle.close()
            finally:
                self._owner_thread_id = None
                self._condition.notify_all()


class CrossProcessRLock:
    """Small reentrant advisory file lock for JSON read-modify-write sections."""

    _global_mutex = threading.Lock()
    _global_states: dict[str, _PathLockState] = {}

    def __init__(self, path: str | Path | Callable[[], str | Path]) -> None:
        self._path = path
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._state: _PathLockState | None = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def acquire(self) -> None:
        self._thread_lock.acquire()
        try:
            if self._depth == 0:
                path = self._resolve_path()
                state = self._get_path_state(path)
                state.acquire(path)
                self._state = state
            self._depth += 1
        except BaseException:
            self._thread_lock.release()
            raise

    def release(self) -> None:
        try:
            if self._depth <= 0:
                raise RuntimeError("cannot release an unlocked CrossProcessRLock")
            self._depth -= 1
            if self._depth == 0:
                state = self._state
                self._state = None
                if state is not None:
                    state.release()
        finally:
            self._thread_lock.release()

    def _resolve_path(self) -> Path:
        if callable(self._path):
            return Path(self._path())
        return Path(self._path)

    @classmethod
    def _get_path_state(cls, path: Path) -> _PathLockState:
        key = str(path.expanduser().resolve(strict=False))
        with cls._global_mutex:
            state = cls._global_states.get(key)
            if state is None:
                state = _PathLockState()
                cls._global_states[key] = state
            return state


if os.name == "nt":
    import msvcrt

    def _lock_file(handle) -> None:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock_file(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_file(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def _unlock_file(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
