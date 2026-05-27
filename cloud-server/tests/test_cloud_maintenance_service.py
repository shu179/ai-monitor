from __future__ import annotations

import sys
import tempfile
import unittest
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("SURFACED_CLOUD_SECRET_KEY", "test-secret-key-for-cloud-maintenance")

from app.services.cloud_maintenance_service import run_cloud_maintenance  # noqa: E402


class CloudMaintenanceServiceTests(unittest.TestCase):
    def test_dry_run_counts_cleanup_without_deleting_or_committing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orphan = Path(tmp) / "orphan.bin"
            orphan.write_bytes(b"orphan")
            db = _db(expired_session_ids=["session-1"], parts_count=2, dead_letters=3, change_rows=4)

            result = run_cloud_maintenance(db, dry_run=True, settings=_settings(tmp))

            self.assertEqual(result["expired_upload_sessions"], {"sessions": 1, "parts": 2})
            self.assertEqual(result["dead_letters"], {"dead_letters": 3})
            self.assertEqual(result["change_log"], {"rows": 4})
            self.assertEqual(result["orphan_files"], {"files": 1, "bytes": 6})
            self.assertTrue(orphan.exists())
            db.commit.assert_not_called()

    def test_execute_deletes_orphan_file_and_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orphan = Path(tmp) / "orphan.bin"
            orphan.write_bytes(b"orphan")
            db = _db(expired_session_ids=[], parts_count=0, dead_letters=0, change_rows=0)

            result = run_cloud_maintenance(db, dry_run=False, settings=_settings(tmp))

            self.assertEqual(result["orphan_files"], {"files": 1, "bytes": 6})
            self.assertFalse(orphan.exists())
            db.commit.assert_called_once()


class CloudMaintenanceRouteRegistrationTests(unittest.TestCase):
    def test_admin_ops_routes_are_registered(self) -> None:
        from app.api.routes.admin import router

        paths = {route.path for route in router.routes}

        self.assertIn("/ops/sync-queue", paths)
        self.assertIn("/ops/object-storage", paths)
        self.assertIn("/ops/maintenance", paths)


def _db(*, expired_session_ids: list[str], parts_count: int, dead_letters: int, change_rows: int):
    db = MagicMock()
    db.scalars.return_value = expired_session_ids
    db.scalar.return_value = parts_count
    db.execute.side_effect = [
        _FakeResult(scalar=dead_letters),
        _FakeResult(scalar=change_rows),
        _FakeResult(rows=[]),
        _FakeResult(rows=[]),
    ]
    return db


class _FakeResult:
    def __init__(self, *, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = rows or []

    def scalar_one(self):
        return self._scalar

    def all(self):
        return self._rows


def _settings(path: str):
    return SimpleNamespace(
        object_storage_local_dir=path,
        object_storage_total_quota_bytes=10 * 1024 * 1024 * 1024,
        object_storage_workspace_quota_bytes=5 * 1024 * 1024 * 1024,
        object_storage_max_file_bytes=512 * 1024 * 1024,
        object_storage_min_free_bytes=1,
    )


if __name__ == "__main__":
    unittest.main()
