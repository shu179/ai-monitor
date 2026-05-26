"""Verify retry helper and WAL self-healing."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from core.sqlite_retry import call_with_locked_retry
from core.sqlite_tuning import apply_runtime_pragmas, truncate_wal_if_oversized


class CallWithLockedRetryTests(unittest.TestCase):
    def test_returns_value_on_first_success(self) -> None:
        op = MagicMock(return_value=42)
        self.assertEqual(call_with_locked_retry(op), 42)
        self.assertEqual(op.call_count, 1)

    def test_retries_locked_then_succeeds(self) -> None:
        attempts = []

        def op():
            attempts.append(None)
            if len(attempts) < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        self.assertEqual(call_with_locked_retry(op, backoff_seconds=0), "ok")
        self.assertEqual(len(attempts), 3)

    def test_passes_through_non_lock_errors(self) -> None:
        def op():
            raise sqlite3.OperationalError("no such table: foo")

        with self.assertRaises(sqlite3.OperationalError):
            call_with_locked_retry(op, backoff_seconds=0)

    def test_gives_up_after_max_attempts(self) -> None:
        def op():
            raise sqlite3.OperationalError("database is locked")

        with self.assertRaises(sqlite3.OperationalError):
            call_with_locked_retry(op, max_attempts=2, backoff_seconds=0)


class WalTruncateTests(unittest.TestCase):
    def test_truncate_skips_small_wal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "small.sqlite3"
            conn = sqlite3.connect(db_path)
            apply_runtime_pragmas(conn)
            conn.execute("CREATE TABLE t(a INTEGER)")
            conn.execute("INSERT INTO t VALUES (1)")
            conn.commit()
            self.assertFalse(truncate_wal_if_oversized(conn, db_path, threshold_bytes=10_000_000))
            conn.close()

    def test_truncate_fires_when_wal_grows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "big.sqlite3"
            conn = sqlite3.connect(db_path)
            apply_runtime_pragmas(conn, wal_autocheckpoint_pages=10_000_000)
            conn.execute("CREATE TABLE t(a INTEGER, b TEXT)")
            for i in range(5000):
                conn.execute("INSERT INTO t VALUES (?, ?)", (i, "x" * 200))
            conn.commit()
            triggered = truncate_wal_if_oversized(conn, db_path, threshold_bytes=10_000)
            self.assertTrue(triggered)
            conn.close()


if __name__ == "__main__":
    unittest.main()
