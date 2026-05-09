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

    def test_compare_articles_command_prints_report_and_returns_failure_on_mismatch(self) -> None:
        cli = _load_cli_module()
        calls = []

        def fake_compare(db_path, *, max_workers, limit, rebuild):
            calls.append((db_path, max_workers, limit, rebuild))
            return {
                "ok": False,
                "failed_count": 1,
                "queries": [{"name": "all", "mismatches": ["total"]}],
            }

        cli.compare_article_pages = fake_compare
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = cli.main([
                "compare-articles",
                "--db-path",
                "shadow.sqlite3",
                "--workers",
                "3",
                "--limit",
                "25",
                "--rebuild",
            ])

        self.assertEqual(exit_code, 1)
        self.assertEqual(calls, [("shadow.sqlite3", 3, 25, True)])
        parsed = json.loads(output.getvalue())
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["queries"][0]["mismatches"], ["total"])

    def test_compare_api_articles_command_prints_report(self) -> None:
        cli = _load_cli_module()
        calls = []

        def fake_compare(db_path, *, max_workers, limit, timeout, include_export_keywords):
            calls.append((db_path, max_workers, limit, timeout, include_export_keywords))
            return {
                "ok": True,
                "failed_count": 0,
                "queries": [{"name": "all", "mismatches": []}],
            }

        cli.compare_article_api_pages = fake_compare
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = cli.main([
                "compare-api-articles",
                "--db-path",
                "shadow.sqlite3",
                "--workers",
                "2",
                "--limit",
                "20",
                "--timeout",
                "3.5",
                "--include-export-keywords",
            ])

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [("shadow.sqlite3", 2, 20, 3.5, True)])
        parsed = json.loads(output.getvalue())
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["queries"][0]["name"], "all")

    def test_stress_api_articles_command_prints_report(self) -> None:
        cli = _load_cli_module()
        calls = []

        def fake_stress(
            db_path,
            *,
            max_workers,
            limit,
            rounds,
            timeout,
            include_export_keywords,
            fd_growth_limit,
            max_p95_ms,
            max_failed_requests,
            max_mismatches,
            mode,
        ):
            calls.append((
                db_path,
                max_workers,
                limit,
                rounds,
                timeout,
                include_export_keywords,
                fd_growth_limit,
                max_p95_ms,
                max_failed_requests,
                max_mismatches,
                mode,
            ))
            return {
                "ok": True,
                "failed_count": 0,
                "modes": {"sqlite_shadow": {"request_count": 6}},
            }

        cli.stress_article_api_pages = fake_stress
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = cli.main([
                "stress-api-articles",
                "--db-path",
                "shadow.sqlite3",
                "--workers",
                "3",
                "--limit",
                "30",
                "--rounds",
                "7",
                "--timeout",
                "4.5",
                "--fd-growth-limit",
                "2",
                "--max-p95-ms",
                "80",
                "--max-failed-requests",
                "1",
                "--max-mismatches",
                "2",
                "--mode",
                "sqlite_shadow",
                "--include-export-keywords",
            ])

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [("shadow.sqlite3", 3, 30, 7, 4.5, True, 2, 80.0, 1, 2, "sqlite_shadow")])
        parsed = json.loads(output.getvalue())
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["modes"]["sqlite_shadow"]["request_count"], 6)


if __name__ == "__main__":
    unittest.main()
