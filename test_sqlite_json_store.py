from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.sqlite_json_store import SQLiteJsonDocumentStore


class _FakeResult:
    def __init__(self, *, row=None, rows=None) -> None:
        self._row = row
        self._rows = rows or []

    def fetchone(self):
        return self._row

    def fetchall(self):
        return list(self._rows)


class _FakeConnection:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.closed = False
        self.commit_count = 0
        self.rollback_count = 0

    def execute(self, sql, params=()):
        if self.fail:
            raise RuntimeError("boom")
        text = str(sql)
        if "SELECT value_json" in text:
            return _FakeResult(row=('{"ok": true}',))
        if "SELECT 1" in text:
            return _FakeResult(row=(1,))
        if "SELECT updated_at_ns" in text:
            return _FakeResult(row=(123, 5))
        if "SELECT key" in text:
            return _FakeResult(rows=[("prefix/key",)])
        return _FakeResult()

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1

    def close(self) -> None:
        self.closed = True


class SQLiteJsonDocumentStoreTests(unittest.TestCase):
    def test_public_operations_close_connections(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "store.sqlite3"
            db_path.write_text("", encoding="utf-8")
            store = SQLiteJsonDocumentStore(db_path)
            connections: list[_FakeConnection] = []

            def connect():
                conn = _FakeConnection()
                connections.append(conn)
                return conn

            store._connect = connect  # type: ignore[method-assign]

            self.assertEqual(store.load("doc"), {"ok": True})
            store.save("doc", {"saved": True})
            self.assertTrue(store.exists("doc"))
            self.assertEqual(store.signature("doc"), (f"{db_path}::doc", 123, 5))
            self.assertEqual(store.list_keys("prefix/"), ["prefix/key"])

            self.assertEqual(len(connections), 5)
            self.assertTrue(all(conn.closed for conn in connections))
            self.assertTrue(all(conn.commit_count == 1 for conn in connections))

    def test_connection_closes_after_operation_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteJsonDocumentStore(Path(tmpdir) / "store.sqlite3")
            connections: list[_FakeConnection] = []

            def connect():
                conn = _FakeConnection(fail=True)
                connections.append(conn)
                return conn

            store._connect = connect  # type: ignore[method-assign]

            with self.assertRaises(RuntimeError):
                store.save("doc", {"saved": True})

            self.assertEqual(len(connections), 1)
            self.assertTrue(connections[0].closed)
            self.assertEqual(connections[0].rollback_count, 1)


if __name__ == "__main__":
    unittest.main()
