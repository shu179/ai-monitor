from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
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

    def test_compare_history_command_prints_report(self) -> None:
        cli = _load_cli_module()
        calls = []

        def fake_compare(db_path, *, max_workers, limit, sample_pages, rebuild):
            calls.append((db_path, max_workers, limit, sample_pages, rebuild))
            return {
                "ok": True,
                "failed_count": 0,
                "queries": [{"name": "task-a", "mismatches": []}],
            }

        cli.compare_history_records = fake_compare
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = cli.main([
                "compare-history",
                "--db-path",
                "shadow.sqlite3",
                "--workers",
                "4",
                "--limit",
                "25",
                "--sample-pages",
                "5",
                "--rebuild",
            ])

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [("shadow.sqlite3", 4, 25, 5, True)])
        parsed = json.loads(output.getvalue())
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["queries"][0]["name"], "task-a")

    def test_compare_history_reads_command_prints_report(self) -> None:
        cli = _load_cli_module()
        calls = []

        def fake_compare(db_path, *, max_workers, limit, sample_pages, rebuild):
            calls.append((db_path, max_workers, limit, sample_pages, rebuild))
            return {
                "ok": True,
                "failed_count": 0,
                "query_count": 1,
                "queries": [{"name": "task-a", "mismatches": []}],
            }

        cli.compare_history_task_reads = fake_compare
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = cli.main([
                "compare-history-reads",
                "--db-path",
                "shadow.sqlite3",
                "--workers",
                "4",
                "--limit",
                "25",
                "--sample-pages",
                "5",
                "--rebuild",
            ])

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [("shadow.sqlite3", 4, 25, 5, True)])
        parsed = json.loads(output.getvalue())
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["queries"][0]["name"], "task-a")

    def test_compare_history_derived_command_compact_output_keeps_failed_views(self) -> None:
        cli = _load_cli_module()
        calls = []

        def fake_compare(db_path, *, max_workers, pending_limit, rebuild):
            calls.append((db_path, max_workers, pending_limit, rebuild))
            return {
                "ok": False,
                "db_path": str(db_path),
                "pending_limit": pending_limit,
                "view_count": 2,
                "failed_count": 1,
                "views": [
                    {"name": "task_names", "ok": True, "mismatches": []},
                    {
                        "name": "pending_reviews",
                        "ok": False,
                        "mismatches": ["records"],
                        "json": {"count": 1},
                        "sqlite": {"count": 0},
                    },
                ],
                "rebuild": {
                    "storage_keys": 1,
                    "created": 1,
                    "updated": 0,
                    "skipped": 0,
                    "details": {"task-a": {"created": 1}},
                },
            }

        cli.compare_history_derived_views = fake_compare
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = cli.main([
                "compare-history-derived",
                "--db-path",
                "shadow.sqlite3",
                "--workers",
                "4",
                "--pending-limit",
                "25",
                "--rebuild",
                "--compact",
            ])

        self.assertEqual(exit_code, 1)
        self.assertEqual(calls, [("shadow.sqlite3", 4, 25, True)])
        parsed = json.loads(output.getvalue())
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["pending_limit"], 25)
        self.assertEqual(parsed["view_count"], 2)
        self.assertEqual([item["name"] for item in parsed["failed_views"]], ["pending_reviews"])
        self.assertEqual(parsed["failed_queries"], [])
        self.assertNotIn("views", parsed)
        self.assertNotIn("details", parsed["rebuild"])

    def test_compare_history_snapshot_command_prints_report(self) -> None:
        cli = _load_cli_module()
        calls = []

        def fake_compare(db_path, *, max_workers, rounds, fd_growth_limit, rebuild):
            calls.append((db_path, max_workers, rounds, fd_growth_limit, rebuild))
            return {
                "ok": True,
                "failed_count": 0,
                "comparisons": [{"round": 1, "ok": True, "mismatches": []}],
            }

        cli.compare_history_runtime_snapshot = fake_compare
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = cli.main([
                "compare-history-snapshot",
                "--db-path",
                "shadow.sqlite3",
                "--workers",
                "2",
                "--rounds",
                "5",
                "--fd-growth-limit",
                "1",
                "--rebuild",
            ])

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [("shadow.sqlite3", 2, 5, 1, True)])
        parsed = json.loads(output.getvalue())
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["comparisons"][0]["round"], 1)

    def test_compare_history_runtime_snapshot_uses_sqlite_read_env(self) -> None:
        cli = _load_cli_module()

        class FakeRuntime:
            def snapshot(self) -> dict:
                mode = os.environ.get("AIBRANDMONITOR_HISTORY_READ_BACKEND") or "json"
                return {
                    "stats": {
                        "enabledTasks": 2,
                        "totalTasks": 3,
                        "todayRecords": 4,
                        "hitRecords": 5,
                        "errorRecords": 1,
                    },
                    "dashboard": {
                        "todayTaskCount": 2,
                        "completedCount": 1,
                        "runningCount": 1,
                        "failedTaskCount": 0,
                        "todayIntercepted": 4,
                        "trend": {
                            "timeRange": "week",
                            "mode": "stable",
                            "data": [{"name": mode, "value": 10}],
                        },
                    },
                    "pendingReviews": [{"id": "review-1"}],
                }

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "shadow.sqlite3"
            db_path.touch()
            result = cli.compare_history_runtime_snapshot(
                db_path,
                max_workers=2,
                rounds=2,
                fd_growth_limit=4,
                runtime_factory=FakeRuntime,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_checks"], ["snapshot_fields"])
        self.assertEqual(result["mismatch_count"], 2)
        self.assertEqual(
            result["comparisons"][0]["field_mismatches"][0]["field"],
            "dashboardTrend",
        )

    def test_compare_history_compact_output_keeps_only_failures_and_summary(self) -> None:
        cli = _load_cli_module()

        def fake_compare(db_path, *, max_workers, limit, sample_pages, rebuild):
            return {
                "ok": True,
                "db_path": str(db_path),
                "limit": limit,
                "sample_pages": sample_pages,
                "workers": max_workers,
                "storage_key_count": 2,
                "failed_count": 0,
                "keys": {
                    "expected": ["task-a", "task-b"],
                    "sqlite": ["task-a", "task-b"],
                    "mismatches": [],
                },
                "queries": [
                    {"name": "task-a", "ok": True, "mismatches": [], "windows": [{"json": {"count": 50}}]},
                    {"name": "task-b", "ok": True, "mismatches": [], "windows": [{"json": {"count": 10}}]},
                ],
                "rebuild": {
                    "storage_keys": 2,
                    "created": 60,
                    "updated": 0,
                    "skipped": 0,
                    "details": {"task-a": {"created": 50}},
                },
            }

        cli.compare_history_records = fake_compare
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = cli.main([
                "compare-history",
                "--db-path",
                "shadow.sqlite3",
                "--workers",
                "4",
                "--compact",
            ])

        self.assertEqual(exit_code, 0)
        parsed = json.loads(output.getvalue())
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["keys"]["expected_count"], 2)
        self.assertEqual(parsed["failed_queries"], [])
        self.assertNotIn("queries", parsed)
        self.assertNotIn("details", parsed["rebuild"])

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

    def test_stress_history_writes_command_prints_report(self) -> None:
        cli = _load_cli_module()
        calls = []

        def fake_stress(
            db_path,
            *,
            max_workers,
            task_count,
            records_per_task,
            imports_per_task,
            review_every,
            max_records,
            fd_growth_limit,
        ):
            calls.append((
                db_path,
                max_workers,
                task_count,
                records_per_task,
                imports_per_task,
                review_every,
                max_records,
                fd_growth_limit,
            ))
            return {
                "ok": True,
                "write_summary": {"recorded": 12},
                "checks": {"records": {"ok": True}},
            }

        cli.stress_history_shadow_writes = fake_stress
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = cli.main([
                "stress-history-writes",
                "--db-path",
                "shadow.sqlite3",
                "--workers",
                "3",
                "--task-count",
                "5",
                "--records-per-task",
                "11",
                "--imports-per-task",
                "4",
                "--review-every",
                "2",
                "--max-records",
                "30",
                "--fd-growth-limit",
                "1",
                "--compact",
            ])

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [("shadow.sqlite3", 3, 5, 11, 4, 2, 30, 1)])
        parsed = json.loads(output.getvalue())
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["write_summary"]["recorded"], 12)


if __name__ == "__main__":
    unittest.main()
