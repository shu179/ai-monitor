from __future__ import annotations

import gzip
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

from scripts import backup_postgres  # noqa: E402


class BackupPostgresScriptTests(unittest.TestCase):
    def test_parse_database_url_supports_psycopg_scheme(self) -> None:
        spec = backup_postgres.parse_database_url("postgresql+psycopg://user:pass@db.example:5433/name")

        self.assertEqual(spec["host"], "db.example")
        self.assertEqual(spec["port"], "5433")
        self.assertEqual(spec["user"], "user")
        self.assertEqual(spec["password"], "pass")
        self.assertEqual(spec["database"], "name")

    def test_build_backup_plan_uses_utc_timestamp_and_gzip_suffix(self) -> None:
        plan = backup_postgres.build_backup_plan(
            output_dir=Path("/tmp/backups"),
            database="surfaced_cloud",
            gzip_enabled=True,
            now=datetime(2026, 5, 27, 1, 2, 3, tzinfo=timezone.utc),
        )

        self.assertEqual(plan["output_path"], "/tmp/backups/surfaced_cloud-20260527T010203Z.sql.gz")

    def test_main_refuses_without_marker(self) -> None:
        with patch.dict(os.environ, {"SURFACED_CLOUD_ALLOW_BACKUP": ""}, clear=False):
            self.assertEqual(backup_postgres.main([]), 2)

    def test_gzip_file_and_prune_old_backups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "dump.sql"
            dest = root / "dump.sql.gz"
            source.write_text("hello", encoding="utf-8")

            backup_postgres.gzip_file(source, dest)

            with gzip.open(dest, "rt", encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "hello")

            old = root / "old.sql.gz"
            old.write_text("old", encoding="utf-8")
            old_time = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
            os.utime(old, (old_time, old_time))

            self.assertEqual(backup_postgres.prune_backups(root, retain_days=7), 1)
            self.assertFalse(old.exists())


if __name__ == "__main__":
    unittest.main()
