from __future__ import annotations

import argparse
import hashlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import load_sync_v2  # noqa: E402


class LoadSyncV2ScriptTests(unittest.TestCase):
    def test_profile_config_can_override_safe_defaults(self) -> None:
        args = argparse.Namespace(
            profile="dev",
            tenants=2,
            events_per_tenant=1200,
            batch_size=999,
            concurrency=4,
            objects=1,
            object_bytes=1024,
        )

        cfg = load_sync_v2._profile_config(args)

        self.assertEqual(cfg["tenants"], 2)
        self.assertEqual(cfg["events_per_tenant"], 1200)
        self.assertEqual(cfg["batch_size"], 500)
        self.assertEqual(cfg["concurrency"], 4)
        self.assertEqual(cfg["objects"], 1)

    def test_main_refuses_without_explicit_load_marker(self) -> None:
        with patch.dict(os.environ, {"SURFACED_CLOUD_ALLOW_LOAD": ""}, clear=False):
            self.assertEqual(load_sync_v2.main(["--profile", "dev"]), 2)

    def test_metric_summary_reports_percentiles_and_errors(self) -> None:
        metrics = [
            {"elapsed_ms": 10, "status_code": 200, "accepted": 500, "error": ""},
            {"elapsed_ms": 20, "status_code": 200, "accepted": 500, "error": ""},
            {"elapsed_ms": 50, "status_code": 429, "accepted": 0, "error": "rate limited"},
        ]

        summary = load_sync_v2._summarize_metrics(metrics)

        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["accepted"], 1000)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["status_codes"], {"200": 2, "429": 1})
        self.assertGreaterEqual(summary["p95_ms"], summary["p50_ms"])

    def test_deterministic_payload_stream_has_expected_size_and_hash(self) -> None:
        chunks = list(load_sync_v2._iter_deterministic_payload(size_bytes=12345, index=2))
        body = b"".join(chunks)

        self.assertEqual(len(body), 12345)
        self.assertEqual(
            load_sync_v2._deterministic_payload_sha256(size_bytes=12345, index=2),
            hashlib.sha256(body).hexdigest(),
        )

    def test_acceptance_profiles_are_defined(self) -> None:
        for profile in ("l1", "l2", "l3"):
            self.assertIn(profile, load_sync_v2.PROFILE_DEFAULTS)
            self.assertIn(profile, load_sync_v2.PROFILE_THRESHOLDS)

    def test_l3_object_size_matches_current_single_file_limit(self) -> None:
        self.assertEqual(load_sync_v2.PROFILE_DEFAULTS["l3"]["object_bytes"], 512 * 1024 * 1024)

    def test_l3_profile_includes_one_hundred_sse_connections(self) -> None:
        self.assertEqual(load_sync_v2.PROFILE_DEFAULTS["l3"]["sse_connections"], 100)

    def test_evaluate_thresholds_passes_under_limit(self) -> None:
        result = {"batches": {"count": 120, "p95_ms": 800}}
        evaluation = load_sync_v2.evaluate_thresholds(result, {"batches_p95_ms": 1000})
        self.assertTrue(evaluation["passed"])
        self.assertEqual(evaluation["checks"][0]["actual_ms"], 800)

    def test_evaluate_thresholds_fails_over_limit(self) -> None:
        result = {"batches": {"count": 120, "p95_ms": 1500}}
        evaluation = load_sync_v2.evaluate_thresholds(result, {"batches_p95_ms": 1000})
        self.assertFalse(evaluation["passed"])

    def test_evaluate_thresholds_treats_empty_section_as_not_applicable(self) -> None:
        result = {"objects": {"count": 0, "p95_ms": 0}}
        evaluation = load_sync_v2.evaluate_thresholds(result, {"objects_p95_ms": 500})
        self.assertTrue(evaluation["passed"])

    def test_evaluate_thresholds_fails_when_metric_missing(self) -> None:
        # A populated section that reports 0 p95 is suspicious, not a free pass.
        result = {"batches": {"count": 50, "p95_ms": 0}}
        evaluation = load_sync_v2.evaluate_thresholds(result, {"batches_p95_ms": 1000})
        self.assertFalse(evaluation["passed"])

    def test_wait_for_workspace_queue_idle_polls_until_clear(self) -> None:
        with (
            patch("scripts.load_sync_v2._workspace_active_queue_counts", side_effect=[{"pending": 2}, {}]),
            patch("scripts.load_sync_v2.time.sleep") as sleep,
        ):
            counts = load_sync_v2._wait_for_workspace_queue_idle([1], timeout_seconds=5)

        self.assertEqual(counts, {})
        sleep.assert_called_once_with(0.5)

    def test_sse_summary_reports_connection_bytes_and_errors(self) -> None:
        summary = load_sync_v2._summarize_sse_metrics([
            {"elapsed_ms": 10, "status_code": 200, "accepted": 1, "error": "", "events": 1, "bytes": 20},
            {"elapsed_ms": 20, "status_code": 500, "accepted": 0, "error": "boom", "events": 0, "bytes": 0},
        ])

        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["accepted"], 1)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["events"], 1)
        self.assertEqual(summary["bytes"], 20)


if __name__ == "__main__":
    unittest.main()
