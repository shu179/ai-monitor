from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from types import ModuleType


SCRIPT_PATH = Path(__file__).resolve().parent / "scripts" / "article_history_sqlite_shadow.py"


def _load_cli_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("article_history_sqlite_shadow_cli_api", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load article history sqlite shadow CLI")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ArticleApiSQLiteShadowCompareTests(unittest.TestCase):
    def test_compare_article_api_batches_reports_matching_payloads(self) -> None:
        cli = _load_cli_module()
        json_results = [_api_result("all", "a1")]
        sqlite_results = [_api_result("all", "a1")]

        comparisons = cli._compare_article_api_batches(json_results, sqlite_results)

        self.assertEqual(len(comparisons), 1)
        self.assertTrue(comparisons[0]["ok"])
        self.assertEqual(comparisons[0]["mismatches"], [])
        self.assertEqual(comparisons[0]["json"]["ids"], ["a1"])
        self.assertEqual(comparisons[0]["sqlite"]["ids"], ["a1"])

    def test_compare_article_api_batches_reports_id_and_field_mismatch(self) -> None:
        cli = _load_cli_module()
        json_results = [_api_result("all", "a1", title="JSON title")]
        sqlite_results = [_api_result("all", "sqlite-a1", title="SQLite title")]

        comparisons = cli._compare_article_api_batches(json_results, sqlite_results)

        self.assertFalse(comparisons[0]["ok"])
        self.assertIn("article_ids", comparisons[0]["mismatches"])
        self.assertIn("article_fields", comparisons[0]["mismatches"])
        self.assertEqual(comparisons[0]["json"]["ids"], ["a1"])
        self.assertEqual(comparisons[0]["sqlite"]["ids"], ["sqlite-a1"])
        self.assertEqual(comparisons[0]["field_mismatches"][0]["fields"]["title"]["json"], "JSON title")

    def test_temporary_env_restores_previous_value(self) -> None:
        cli = _load_cli_module()
        previous = os.environ.get("AIBRANDMONITOR_ARTICLE_READ_BACKEND")
        os.environ["AIBRANDMONITOR_ARTICLE_READ_BACKEND"] = "sqlite_shadow_compare"
        try:
            with cli._temporary_env("AIBRANDMONITOR_ARTICLE_READ_BACKEND", "sqlite_shadow"):
                self.assertEqual(os.environ.get("AIBRANDMONITOR_ARTICLE_READ_BACKEND"), "sqlite_shadow")
            self.assertEqual(os.environ.get("AIBRANDMONITOR_ARTICLE_READ_BACKEND"), "sqlite_shadow_compare")
            with cli._temporary_env("AIBRANDMONITOR_ARTICLE_READ_BACKEND", None):
                self.assertIsNone(os.environ.get("AIBRANDMONITOR_ARTICLE_READ_BACKEND"))
            self.assertEqual(os.environ.get("AIBRANDMONITOR_ARTICLE_READ_BACKEND"), "sqlite_shadow_compare")
        finally:
            if previous is None:
                os.environ.pop("AIBRANDMONITOR_ARTICLE_READ_BACKEND", None)
            else:
                os.environ["AIBRANDMONITOR_ARTICLE_READ_BACKEND"] = previous

    def test_stress_article_api_batches_compare_each_round(self) -> None:
        cli = _load_cli_module()
        json_results = [
            {**_api_result("all", "a1"), "round": 1},
            {**_api_result("all", "a2"), "round": 2},
        ]
        sqlite_results = [
            {**_api_result("all", "a1"), "round": 1},
            {**_api_result("all", "sqlite-a2"), "round": 2},
        ]

        comparisons = cli._compare_stress_article_api_batches(json_results, sqlite_results)

        self.assertEqual(len(comparisons), 2)
        self.assertTrue(comparisons[0]["ok"])
        self.assertFalse(comparisons[1]["ok"])
        self.assertEqual(comparisons[1]["round"], 2)
        self.assertIn("article_ids", comparisons[1]["mismatches"])

    def test_stress_batch_summary_reports_latency_and_fd_growth(self) -> None:
        cli = _load_cli_module()
        summary = cli._stress_batch_summary({
            "mode": "sqlite_shadow",
            "elapsed_ms": 100.0,
            "fd_before": 4,
            "fd_after": 9,
            "fd_delta": 5,
            "fd_ok": False,
            "results": [
                {**_api_result("all", "a1"), "round": 1, "elapsed_ms": 10.0},
                {**_api_result("media:media", "a2"), "round": 1, "elapsed_ms": 20.0},
                {
                    "ok": False,
                    "round": 2,
                    "name": "all",
                    "status": 500,
                    "error": "HTTPError",
                    "message": "boom",
                    "elapsed_ms": 30.0,
                },
            ],
        })

        self.assertEqual(summary["request_count"], 3)
        self.assertEqual(summary["failed_count"], 1)
        self.assertEqual(summary["latency_ms"]["p50"], 10.0)
        self.assertEqual(summary["latency_ms"]["p95"], 20.0)
        self.assertFalse(summary["fd_ok"])
        self.assertEqual(summary["fd_delta"], 5)
        self.assertEqual(summary["failures"][0]["status"], 500)

    def test_latency_summary_handles_empty_and_percentiles(self) -> None:
        cli = _load_cli_module()

        self.assertEqual(cli._latency_summary([])["count"], 0)
        summary = cli._latency_summary([5.0, 1.0, 3.0, 9.0])

        self.assertEqual(summary["min"], 1.0)
        self.assertEqual(summary["p50"], 3.0)
        self.assertEqual(summary["p95"], 9.0)
        self.assertEqual(summary["max"], 9.0)

    def test_stress_modes(self) -> None:
        cli = _load_cli_module()

        self.assertEqual(cli._stress_modes("json"), [("json", None)])
        self.assertEqual(cli._stress_modes("sqlite_shadow"), [("sqlite_shadow", "sqlite_shadow")])
        self.assertEqual(
            cli._stress_modes("both"),
            [("json", None), ("sqlite_shadow", "sqlite_shadow")],
        )


def _api_result(name: str, article_id: str, *, title: str = "Example title") -> dict:
    return {
        "ok": True,
        "name": name,
        "query": {"task_name": "", "media_type": ""},
        "status": 200,
        "elapsed_ms": 1.0,
        "payload": {
            "articles": [
                {
                    "id": article_id,
                    "source": "Example Media",
                    "title": title,
                    "type": "media",
                    "category": "媒体",
                    "url": "https://example.com/a1",
                    "ts": "2026-05-09",
                    "media_name": "Example Media",
                    "matchedTasks": [],
                }
            ],
            "total": 1,
            "today_total": 1,
        },
    }


if __name__ == "__main__":
    unittest.main()
