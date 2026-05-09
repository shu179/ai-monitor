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
                sqlite_mode = mode == "sqlite_shadow"
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
                    "historyStorage": {
                        "enabled": sqlite_mode,
                        "available": True,
                        "backend": "sqlite_shadow" if sqlite_mode else "",
                        "last_status": "success" if sqlite_mode else "",
                        "success_count": 1 if sqlite_mode else 0,
                        "fallback_count": 0,
                        "consecutive_errors": 0,
                    },
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

    def test_compare_history_runtime_snapshot_requires_sqlite_health(self) -> None:
        cli = _load_cli_module()

        class FakeRuntime:
            def snapshot(self) -> dict:
                return {
                    "stats": {
                        "enabledTasks": 1,
                        "totalTasks": 1,
                        "todayRecords": 0,
                        "hitRecords": 0,
                        "errorRecords": 0,
                    },
                    "dashboard": {
                        "todayTaskCount": 1,
                        "completedCount": 0,
                        "runningCount": 0,
                        "failedTaskCount": 0,
                        "todayIntercepted": 0,
                        "trend": {"timeRange": "week", "data": []},
                    },
                    "pendingReviews": [],
                    "historyStorage": {
                        "enabled": False,
                        "available": False,
                        "last_status": "fallback",
                        "fallback_count": 1,
                    },
                }

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "shadow.sqlite3"
            db_path.touch()
            result = cli.compare_history_runtime_snapshot(
                db_path,
                rounds=1,
                runtime_factory=FakeRuntime,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_checks"], ["history_storage"])
        self.assertEqual(result["storage_mismatch_count"], 1)
        self.assertEqual(
            result["comparisons"][0]["storage_mismatches"],
            ["enabled", "available", "last_status", "success_count", "fallback_count"],
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

    def test_stress_api_mixed_command_prints_report(self) -> None:
        cli = _load_cli_module()
        calls = []

        def fake_stress(
            db_path,
            *,
            max_workers,
            snapshot_rounds,
            article_rounds,
            article_limit,
            timeout,
            include_export_keywords,
            fd_growth_limit,
            max_p95_ms,
            max_failed_requests,
            max_mismatches,
        ):
            calls.append((
                db_path,
                max_workers,
                snapshot_rounds,
                article_rounds,
                article_limit,
                timeout,
                include_export_keywords,
                fd_growth_limit,
                max_p95_ms,
                max_failed_requests,
                max_mismatches,
            ))
            return {
                "ok": True,
                "failed_count": 0,
                "status": {"ok": True, "rounds": snapshot_rounds},
                "articles": {"ok": True, "rounds": article_rounds},
            }

        cli.stress_mixed_api_guards = fake_stress
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = cli.main([
                "stress-api-mixed",
                "--db-path",
                "shadow.sqlite3",
                "--workers",
                "4",
                "--snapshot-rounds",
                "5",
                "--article-rounds",
                "6",
                "--article-limit",
                "40",
                "--timeout",
                "7.5",
                "--fd-growth-limit",
                "3",
                "--max-p95-ms",
                "100",
                "--max-failed-requests",
                "1",
                "--max-mismatches",
                "2",
                "--include-export-keywords",
            ])

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [("shadow.sqlite3", 4, 5, 6, 40, 7.5, True, 3, 100.0, 1, 2)])
        parsed = json.loads(output.getvalue())
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["status"]["rounds"], 5)
        self.assertEqual(parsed["articles"]["rounds"], 6)

    def test_validate_history_sqlite_readiness_command_prints_report(self) -> None:
        cli = _load_cli_module()
        calls = []

        def fake_validate(
            db_path,
            *,
            max_workers,
            read_limit,
            sample_pages,
            pending_limit,
            snapshot_rounds,
            api_rounds,
            timeout,
            fd_growth_limit,
            max_p95_ms,
        ):
            calls.append((
                db_path,
                max_workers,
                read_limit,
                sample_pages,
                pending_limit,
                snapshot_rounds,
                api_rounds,
                timeout,
                fd_growth_limit,
                max_p95_ms,
            ))
            return {
                "ok": True,
                "failed_checks": [],
                "checks": {"api_snapshot": {"ok": True}},
            }

        cli.validate_history_sqlite_readiness = fake_validate
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = cli.main([
                "validate-history-sqlite-readiness",
                "--db-path",
                "shadow.sqlite3",
                "--workers",
                "3",
                "--read-limit",
                "80",
                "--sample-pages",
                "4",
                "--pending-limit",
                "120",
                "--snapshot-rounds",
                "5",
                "--api-rounds",
                "6",
                "--timeout",
                "4.5",
                "--fd-growth-limit",
                "2",
                "--max-p95-ms",
                "90",
            ])

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [("shadow.sqlite3", 3, 80, 4, 120, 5, 6, 4.5, 2, 90.0)])
        parsed = json.loads(output.getvalue())
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["checks"]["api_snapshot"]["ok"], True)

    def test_validate_history_sqlite_readiness_reports_failed_guard(self) -> None:
        cli = _load_cli_module()
        calls = []

        def fake_rebuild(db_path, *, max_workers, verify_tail_limit):
            calls.append(("rebuild", str(db_path), max_workers, verify_tail_limit))
            return {
                "history": {"storage_keys": 1, "records": 2, "created": 2, "updated": 0, "skipped": 0},
                "verification": {"ok": True},
            }

        def fake_reads(db_path, *, max_workers, limit, sample_pages, rebuild):
            calls.append(("reads", str(db_path), max_workers, limit, sample_pages, rebuild))
            return {"ok": True, "failed_count": 0, "query_count": 1, "queries": []}

        def fake_derived(db_path, *, max_workers, pending_limit, rebuild):
            calls.append(("derived", str(db_path), max_workers, pending_limit, rebuild))
            return {"ok": True, "failed_count": 0, "view_count": 2, "views": []}

        def fake_snapshot(db_path, *, max_workers, rounds, fd_growth_limit, rebuild):
            calls.append(("snapshot", str(db_path), max_workers, rounds, fd_growth_limit, rebuild))
            return {"ok": True, "failed_checks": [], "comparisons": []}

        def fake_api_snapshot(
            db_path,
            *,
            max_workers,
            rounds,
            timeout,
            fd_growth_limit,
            max_p95_ms,
            max_failed_requests,
            max_mismatches,
            mode,
        ):
            calls.append((
                "api_snapshot",
                str(db_path),
                max_workers,
                rounds,
                timeout,
                fd_growth_limit,
                max_p95_ms,
                max_failed_requests,
                max_mismatches,
                mode,
            ))
            if mode == "auto":
                return {
                    "ok": True,
                    "failed_checks": [],
                    "failed_count": 0,
                    "rounds": rounds,
                    "workers": max_workers,
                    "modes": {"auto": {"request_count": rounds}},
                }
            return {
                "ok": False,
                "failed_checks": ["history_storage"],
                "failed_count": 1,
                "rounds": rounds,
                "workers": max_workers,
                "storage_mismatch_count": 1,
                "modes": {"sqlite_shadow": {"request_count": rounds}},
            }

        cli.rebuild_shadow_store = fake_rebuild
        cli.compare_history_task_reads = fake_reads
        cli.compare_history_derived_views = fake_derived
        cli.compare_history_runtime_snapshot = fake_snapshot
        cli.stress_snapshot_api = fake_api_snapshot

        result = cli.validate_history_sqlite_readiness(
            "shadow.sqlite3",
            max_workers=2,
            read_limit=75,
            sample_pages=2,
            pending_limit=50,
            snapshot_rounds=3,
            api_rounds=4,
            timeout=5.0,
            fd_growth_limit=1,
            max_p95_ms=100.0,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_checks"], ["api_snapshot"])
        self.assertEqual(result["checks"]["api_snapshot"]["failed_checks"], ["history_storage"])
        self.assertTrue(result["checks"]["api_snapshot_auto"]["ok"])
        self.assertEqual(calls[0], ("rebuild", "shadow.sqlite3", 2, 1))
        self.assertEqual(calls[-2], ("api_snapshot", "shadow.sqlite3", 2, 4, 5.0, 1, 100.0, 0, 0, "both"))
        self.assertEqual(calls[-1], ("api_snapshot", "shadow.sqlite3", 2, 4, 5.0, 1, 100.0, 0, 0, "auto"))

    def test_stress_api_snapshot_command_prints_report(self) -> None:
        cli = _load_cli_module()
        calls = []

        def fake_stress(
            db_path,
            *,
            max_workers,
            rounds,
            timeout,
            fd_growth_limit,
            max_p95_ms,
            max_failed_requests,
            max_mismatches,
            mode,
        ):
            calls.append((
                db_path,
                max_workers,
                rounds,
                timeout,
                fd_growth_limit,
                max_p95_ms,
                max_failed_requests,
                max_mismatches,
                mode,
            ))
            return {
                "ok": True,
                "failed_count": 0,
                "modes": {"sqlite_shadow": {"request_count": 4}},
            }

        cli.stress_snapshot_api = fake_stress
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = cli.main([
                "stress-api-snapshot",
                "--db-path",
                "shadow.sqlite3",
                "--workers",
                "3",
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
            ])

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [("shadow.sqlite3", 3, 7, 4.5, 2, 80.0, 1, 2, "sqlite_shadow")])
        parsed = json.loads(output.getvalue())
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["modes"]["sqlite_shadow"]["request_count"], 4)

    def test_stress_api_snapshot_command_accepts_auto_mode(self) -> None:
        cli = _load_cli_module()
        calls = []

        def fake_stress(
            db_path,
            *,
            max_workers,
            rounds,
            timeout,
            fd_growth_limit,
            max_p95_ms,
            max_failed_requests,
            max_mismatches,
            mode,
        ):
            calls.append((db_path, max_workers, rounds, timeout, fd_growth_limit, max_p95_ms, max_failed_requests, max_mismatches, mode))
            return {
                "ok": True,
                "failed_count": 0,
                "modes": {"auto": {"request_count": 2}},
            }

        cli.stress_snapshot_api = fake_stress
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = cli.main([
                "stress-api-snapshot",
                "--db-path",
                "shadow.sqlite3",
                "--rounds",
                "2",
                "--mode",
                "auto",
            ])

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [("shadow.sqlite3", None, 2, 20.0, 8, 0.0, 0, 0, "auto")])
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["modes"]["auto"]["request_count"], 2)

    def test_sqlite_snapshot_batch_storage_mismatches_require_successful_health(self) -> None:
        cli = _load_cli_module()

        result = cli._sqlite_snapshot_batch_storage_mismatches({
            "results": [
                {
                    "ok": True,
                    "round": 1,
                    "history_storage": {
                        "enabled": True,
                        "available": True,
                        "last_status": "fallback",
                        "success_count": 0,
                        "fallback_count": 1,
                        "consecutive_errors": 0,
                    },
                },
                {
                    "ok": True,
                    "round": 2,
                    "history_storage": {
                        "enabled": True,
                        "available": True,
                        "last_status": "success",
                        "success_count": 1,
                        "fallback_count": 0,
                        "consecutive_errors": 0,
                    },
                },
            ],
        })

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["round"], 1)
        self.assertEqual(
            result[0]["mismatches"],
            ["last_status", "success_count", "fallback_count"],
        )

    def test_history_storage_summary_keeps_backend_guard_fields(self) -> None:
        cli = _load_cli_module()

        summary = cli._history_storage_summary({
            "historyStorage": {
                "enabled": True,
                "available": True,
                "ready": True,
                "fresh": True,
                "backend": "auto",
                "backendSource": "config",
                "requestedBackend": "auto",
                "effectiveBackend": "sqlite_shadow",
                "shadowWritesEnabled": True,
                "last_status": "success",
                "success_count": 2,
            },
        })

        self.assertEqual(summary["backend"], "auto")
        self.assertEqual(summary["backend_source"], "config")
        self.assertEqual(summary["requested_backend"], "auto")
        self.assertEqual(summary["effective_backend"], "sqlite_shadow")
        self.assertTrue(summary["shadow_writes_enabled"])

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
