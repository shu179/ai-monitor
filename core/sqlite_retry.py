"""Helpers to retry transient SQLite lock errors."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from typing import TypeVar


T = TypeVar("T")

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 0.2


def call_with_locked_retry(
    operation: Callable[[], T],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
) -> T:
    """Run operation and retry transient SQLite lock contention."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "database is locked" not in message and "database table is locked" not in message:
                raise
            if attempt >= max_attempts:
                raise
            time.sleep(backoff_seconds * attempt)
