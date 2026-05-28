from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import basebackup_postgres  # noqa: E402


class BasebackupPostgresScriptTests(unittest.TestCase):
    def test_build_basebackup_plan_uses_utc_timestamp(self) -> None:
        plan = basebackup_postgres.build_basebackup_plan(
            output_dir=Path("/tmp/backups"),
            database="surfaced_cloud",
            now=datetime(2026, 5, 29, 1, 2, 3, tzinfo=timezone.utc),
        )

        self.assertEqual(plan["output_path"], "/tmp/backups/basebackup-surfaced_cloud-20260529T010203Z")

    def test_main_refuses_without_marker(self) -> None:
        with patch.dict(os.environ, {"SURFACED_CLOUD_ALLOW_BASEBACKUP": ""}, clear=False):
            self.assertEqual(basebackup_postgres.main([]), 2)

    def test_prune_basebackups_removes_old_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "basebackup-surfaced_cloud-old"
            fresh = root / "basebackup-surfaced_cloud-fresh"
            old.mkdir()
            fresh.mkdir()
            old_time = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
            os.utime(old, (old_time, old_time))

            self.assertEqual(basebackup_postgres.prune_basebackups(root, retain_days=7), 1)

            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())

    def test_run_pg_basebackup_uses_streamed_wal(self) -> None:
        spec = {
            "host": "postgres",
            "port": "5432",
            "user": "surfaced",
            "password": "secret",
            "database": "surfaced_cloud",
        }

        with patch("scripts.basebackup_postgres.subprocess.run") as run:
            basebackup_postgres.run_pg_basebackup(spec, Path("/tmp/basebackup"))

        command = run.call_args.args[0]
        self.assertIn("pg_basebackup", command)
        self.assertIn("--wal-method=stream", command)
        self.assertIn("--format=tar", command)
        self.assertEqual(run.call_args.kwargs["env"]["PGPASSWORD"], "secret")


if __name__ == "__main__":
    unittest.main()
