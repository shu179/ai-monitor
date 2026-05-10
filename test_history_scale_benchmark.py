from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from core import history
from scripts.history_scale_benchmark import (
    BenchmarkOptions,
    _prepare_data_dir,
    build_synthetic_history_sources,
    main,
    run_benchmark,
)


class HistoryScaleBenchmarkTests(unittest.TestCase):
    def test_force_prepare_data_dir_removes_stale_benchmark_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _prepare_data_dir(Path(tmpdir), force=True)
            (paths["history"] / "task.json").write_text("[]", encoding="utf-8")
            paths["local_store_db"].write_text("stale", encoding="utf-8")
            Path(f"{paths['local_store_db']}-wal").write_text("stale-wal", encoding="utf-8")
            paths["shadow_db"].write_text("stale", encoding="utf-8")
            Path(f"{paths['shadow_db']}-shm").write_text("stale-shm", encoding="utf-8")

            refreshed = _prepare_data_dir(Path(tmpdir), force=True)

            self.assertFalse((refreshed["history"] / "task.json").exists())
            self.assertFalse(refreshed["local_store_db"].exists())
            self.assertFalse(Path(f"{refreshed['local_store_db']}-wal").exists())
            self.assertFalse(refreshed["shadow_db"].exists())
            self.assertFalse(Path(f"{refreshed['shadow_db']}-shm").exists())

    def test_synthetic_history_sources_cover_tasks_and_review_states(self) -> None:
        sources = build_synthetic_history_sources(24, task_count=4, today=date(2026, 5, 10))

        self.assertEqual(sorted(sources.keys()), ["task-0", "task-1", "task-2", "task-3"])
        self.assertEqual(sum(len(records) for records in sources.values()), 24)
        self.assertTrue(any(
            record.get("review_status") == "pending"
            for records in sources.values()
            for record in records
        ))
        self.assertTrue(all(
            record.get("task_id") == task_id
            for task_id, records in sources.items()
            for record in records
        ))

    def test_small_benchmark_summary_is_isolated_and_complete(self) -> None:
        original_paths = (
            history.DEFAULT_HISTORY_DIR,
            history.HISTORY_DIR,
            history.LOCAL_STORE_DB_FILE,
            history.HISTORY_SHADOW_DB_FILE,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            summary = run_benchmark(
                BenchmarkOptions(
                    record_count=90,
                    task_count=3,
                    page_limit=7,
                    data_dir=Path(tmpdir),
                    force=True,
                )
            )

            self.assertTrue((Path(tmpdir) / "logs" / "history" / "task-0.json").exists())
            self.assertTrue((Path(tmpdir) / "logs" / "article_history_shadow.sqlite3").exists())

        self.assertEqual(
            original_paths,
            (
                history.DEFAULT_HISTORY_DIR,
                history.HISTORY_DIR,
                history.LOCAL_STORE_DB_FILE,
                history.HISTORY_SHADOW_DB_FILE,
            ),
        )
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["input"]["record_count"], 90)
        self.assertEqual(summary["input"]["task_count"], 3)
        self.assertEqual(summary["backend"]["effective_backend"], "sqlite_shadow")
        self.assertEqual(summary["readiness"]["ready"], True)
        self.assertEqual(summary["standards"]["write_path_bottlenecks"]["status"], "known_bottleneck")
        self.assertEqual(
            summary["health"]["authoritativeReadiness"]["reason"],
            "structured_history_is_shadow_only",
        )

        operations = {operation["name"]: operation for operation in summary["operations"]}
        for name in (
            "read_list_get_records_many",
            "read_page_sqlite_shadow",
            "append_record",
            "import_records_merge",
            "apply_review",
        ):
            self.assertIn(name, operations)
            self.assertTrue(operations[name]["ok"], operations[name].get("error", ""))

        page_details = operations["read_page_sqlite_shadow"]["details"]
        self.assertEqual(page_details["effective_backend"], "sqlite_shadow")
        self.assertLessEqual(page_details["returned_count"], 7)
        append_details = operations["append_record"]["details"]
        self.assertTrue(append_details["structured_shadow_writes"])
        self.assertTrue(append_details["known_full_document_rewrite"])

    def test_small_benchmark_supports_structured_authoritative_write_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            summary = run_benchmark(
                BenchmarkOptions(
                    record_count=90,
                    task_count=3,
                    page_limit=7,
                    history_write_backend="sqlite_structured",
                    data_dir=Path(tmpdir),
                    force=True,
                )
            )

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["input"]["history_write_backend"], "sqlite_structured")
        self.assertEqual(summary["backend"]["effective_backend"], "sqlite_structured")
        self.assertEqual(summary["backend"]["storage_backend"], "sqlite_structured")
        self.assertEqual(summary["standards"]["write_path_bottlenecks"]["status"], "pass")
        self.assertTrue(summary["health"]["authoritativeReadiness"]["readyForAuthoritativeSwitch"])

        operations = {operation["name"]: operation for operation in summary["operations"]}
        for name in ("append_record", "import_records_merge", "apply_review"):
            details = operations[name]["details"]
            self.assertEqual(details["effective_backend"], "sqlite_structured")
            self.assertFalse(details["known_full_document_rewrite"])

    def test_small_benchmark_supports_auto_write_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            summary = run_benchmark(
                BenchmarkOptions(
                    record_count=90,
                    task_count=3,
                    page_limit=7,
                    history_write_backend="auto",
                    data_dir=Path(tmpdir),
                    force=True,
                )
            )

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["input"]["history_write_backend"], "auto")
        self.assertEqual(summary["backend"]["storage_backend"], "sqlite_structured")
        self.assertEqual(summary["health"]["writePath"]["requestedBackend"], "auto")
        self.assertEqual(summary["standards"]["write_path_bottlenecks"]["status"], "pass")
        self.assertIn("initial_health", summary)
        self.assertIn("bootstrap", summary)
        self.assertIn("fd", summary)
        self.assertIn("write_timing", summary)

    def test_cli_small_smoke_writes_json_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "summary.json"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main([
                    "--record-count",
                    "30",
                    "--task-count",
                    "2",
                    "--page-limit",
                    "5",
                    "--data-dir",
                    tmpdir,
                    "--force",
                    "--output",
                    str(output_path),
                ])

            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertIn('"ok"', stdout.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["input"]["record_count"], 30)
        self.assertEqual(payload["backend"]["effective_backend"], "sqlite_shadow")

    def test_cli_accepts_auto_history_write_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "summary.json"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main([
                    "--record-count",
                    "30",
                    "--task-count",
                    "2",
                    "--page-limit",
                    "5",
                    "--history-write-backend",
                    "auto",
                    "--data-dir",
                    tmpdir,
                    "--force",
                    "--output",
                    str(output_path),
                ])

            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["input"]["history_write_backend"], "auto")
        self.assertEqual(payload["backend"]["storage_backend"], "sqlite_structured")


if __name__ == "__main__":
    unittest.main()
