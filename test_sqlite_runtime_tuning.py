"""Verify each SQLite store applies the standard runtime PRAGMAs."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.article_history_sqlite_store import ArticleHistorySQLiteStore
from core.article_sqlite_store import ArticleSQLiteStore
from core.sqlite_json_store import SQLiteJsonDocumentStore


EXPECTED = {
    "journal_mode": "wal",
    "synchronous": 1,
    "foreign_keys": 1,
    "temp_store": 2,
    "cache_size": -65536,
    "mmap_size": 268_435_456,
    "wal_autocheckpoint": 1000,
}


def _read_pragmas(conn):
    out = {}
    for key in EXPECTED:
        row = conn.execute(f"PRAGMA {key}").fetchone()
        out[key] = row[0] if row else None
    return out


class SQLiteRuntimeTuningTests(unittest.TestCase):
    def test_article_store_applies_expected_pragmas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArticleSQLiteStore(Path(tmp) / "articles.sqlite3")
            store.initialize()
            with store._connection() as conn:
                pragmas = _read_pragmas(conn)
        for key, expected in EXPECTED.items():
            self.assertEqual(pragmas[key], expected, msg=f"{key} != {expected}")

    def test_json_doc_store_applies_expected_pragmas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteJsonDocumentStore(Path(tmp) / "store.sqlite3")
            with store._connection() as conn:
                pragmas = _read_pragmas(conn)
        for key, expected in EXPECTED.items():
            self.assertEqual(pragmas[key], expected, msg=f"{key} != {expected}")

    def test_history_store_applies_expected_pragmas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArticleHistorySQLiteStore(Path(tmp) / "history.sqlite3")
            store.initialize()
            with store._connection() as conn:
                pragmas = _read_pragmas(conn)
        for key, expected in EXPECTED.items():
            self.assertEqual(pragmas[key], expected, msg=f"{key} != {expected}")


if __name__ == "__main__":
    unittest.main()
