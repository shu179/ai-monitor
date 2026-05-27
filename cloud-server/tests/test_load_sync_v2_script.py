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


if __name__ == "__main__":
    unittest.main()
