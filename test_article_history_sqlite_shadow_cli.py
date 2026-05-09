from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path
from types import ModuleType


SCRIPT_PATH = Path(__file__).resolve().parent / "scripts" / "article_history_sqlite_shadow.py"


def _load_cli_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("article_history_sqlite_shadow_cli", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load article history sqlite shadow CLI")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ArticleHistorySQLiteShadowCLITests(unittest.TestCase):
    def test_rebuild_command_prints_json_and_returns_success_when_verified(self) -> None:
        cli = _load_cli_module()
        calls = []

        def fake_rebuild(db_path, *, max_workers, verify_tail_limit):
            calls.append((db_path, max_workers, verify_tail_limit))
            return {
                "db_path": str(db_path),
                "history": {"records": 2},
                "verification": {"ok": True},
            }

        cli.rebuild_shadow_store = fake_rebuild
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = cli.main(["rebuild", "--db-path", "shadow.sqlite3", "--workers", "4", "--tail-limit", "7"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [("shadow.sqlite3", 4, 7)])
        parsed = json.loads(output.getvalue())
        self.assertTrue(parsed["verification"]["ok"])
        self.assertEqual(parsed["history"]["records"], 2)

    def test_verify_command_returns_failure_when_shadow_store_mismatches(self) -> None:
        cli = _load_cli_module()

        def fake_verify(db_path, *, max_workers, tail_limit):
            return {
                "ok": False,
                "articles": {"expected_total": 1, "sqlite_total": 0},
                "db_path": str(db_path),
                "workers": max_workers,
                "tail_limit": tail_limit,
            }

        cli.verify_shadow_store = fake_verify
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = cli.main(["verify", "--db-path", "shadow.sqlite3", "--compact"])

        self.assertEqual(exit_code, 1)
        parsed = json.loads(output.getvalue())
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["articles"]["sqlite_total"], 0)


if __name__ == "__main__":
    unittest.main()
