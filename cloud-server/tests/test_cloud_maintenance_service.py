from __future__ import annotations

import sys
import tempfile
import unittest
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("SURFACED_CLOUD_SECRET_KEY", "test-secret-key-for-cloud-maintenance")

from app.services.cloud_maintenance_service import run_cloud_maintenance  # noqa: E402
from app.models import ObjectManifest  # noqa: E402


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
            self.assertEqual(result["soft_deleted_objects"], {"objects": 0, "bytes": 0})
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

    def test_execute_deletes_only_stale_temporary_upload_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fresh = Path(tmp) / "7/aa/bb/a.tmp-fresh"
            stale = Path(tmp) / "7/aa/bb/a.tmp-stale"
            fresh.parent.mkdir(parents=True)
            fresh.write_bytes(b"fresh")
            stale.write_bytes(b"stale")
            old = 100_000
            os.utime(stale, (old, old))
            db = _db(expired_session_ids=[], parts_count=0, dead_letters=0, change_rows=0)

            with patch("app.services.object_storage_diagnostics.time.time", return_value=old + 24 * 60 * 60 + 1):
                result = run_cloud_maintenance(db, dry_run=False, settings=_settings(tmp))

            self.assertEqual(result["orphan_files"], {"files": 0, "bytes": 0})
            self.assertEqual(result["temporary_files"], {"files": 1, "bytes": 5})
            self.assertTrue(fresh.exists())
            self.assertFalse(stale.exists())
            db.commit.assert_called_once()

    def test_execute_deletes_expired_soft_deleted_object_after_rechecking_ref_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sha = "d" * 64
            storage_key = f"7/{sha[:2]}/{sha[2:4]}/{sha}"
            path = Path(tmp) / storage_key
            path.parent.mkdir(parents=True)
            path.write_bytes(b"soft-delete-me")
            manifest = ObjectManifest(
                id="object-1",
                workspace_id=7,
                sha256=sha,
                size_bytes=14,
                storage_size_bytes=14,
                content_type="text/plain",
                storage_key=storage_key,
                compression="none",
                ref_count=0,
                status="deleting",
                deleted_after=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
            db = _db(expired_session_ids=[], parts_count=0, dead_letters=0, change_rows=0, soft_deleted=[manifest])

            result = run_cloud_maintenance(db, dry_run=False, settings=_settings(tmp))

            self.assertEqual(result["soft_deleted_objects"], {"objects": 1, "bytes": 14})
            self.assertFalse(path.exists())
            db.refresh.assert_called_once_with(manifest)
            db.delete.assert_called_once_with(manifest)

    def test_execute_skips_soft_deleted_object_if_reference_was_restored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sha = "e" * 64
            storage_key = f"7/{sha[:2]}/{sha[2:4]}/{sha}"
            path = Path(tmp) / storage_key
            path.parent.mkdir(parents=True)
            path.write_bytes(b"keep-me")
            manifest = ObjectManifest(
                id="object-1",
                workspace_id=7,
                sha256=sha,
                size_bytes=7,
                storage_size_bytes=7,
                content_type="text/plain",
                storage_key=storage_key,
                compression="none",
                ref_count=0,
                status="deleting",
                deleted_after=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
            db = _db(expired_session_ids=[], parts_count=0, dead_letters=0, change_rows=0, soft_deleted=[manifest])

            def restore_reference(_manifest):
                manifest.ref_count = 1
                manifest.status = "active"

            db.refresh.side_effect = restore_reference

            result = run_cloud_maintenance(db, dry_run=False, settings=_settings(tmp))

            self.assertEqual(result["soft_deleted_objects"], {"objects": 0, "bytes": 0})
            self.assertTrue(path.exists())
            db.delete.assert_not_called()


class CloudMaintenanceRouteRegistrationTests(unittest.TestCase):
    def test_admin_ops_routes_are_registered(self) -> None:
        from app.api.routes.admin import router

        paths = {route.path for route in router.routes}

        self.assertIn("/ops/sync-queue", paths)
        self.assertIn("/ops/object-storage", paths)
        self.assertIn("/ops/maintenance", paths)


def _db(
    *,
    expired_session_ids: list[str],
    parts_count: int,
    dead_letters: int,
    change_rows: int,
    soft_deleted: list[ObjectManifest] | None = None,
):
    db = MagicMock()
    db.scalars.return_value = expired_session_ids
    db.scalar.return_value = parts_count
    db.execute.side_effect = [
        _FakeResult(scalar=dead_letters),
        _FakeResult(scalar=change_rows),
        _FakeResult(rows=_manifest_rows(soft_deleted or [])),
        _FakeResult(rows=_workspace_rows(soft_deleted or [])),
    ]
    db.scalars.side_effect = [
        expired_session_ids,
        soft_deleted or [],
    ]
    return db


def _manifest_rows(manifests: list[ObjectManifest]):
    return [
        SimpleNamespace(
            id=manifest.id,
            workspace_id=manifest.workspace_id,
            storage_key=manifest.storage_key,
            storage_size_bytes=manifest.storage_size_bytes,
        )
        for manifest in manifests
    ]


def _workspace_rows(manifests: list[ObjectManifest]):
    if not manifests:
        return []
    grouped: dict[int, list[ObjectManifest]] = {}
    for manifest in manifests:
        grouped.setdefault(int(manifest.workspace_id), []).append(manifest)
    return [
        SimpleNamespace(
            workspace_id=workspace_id,
            storage_size_bytes=sum(int(item.storage_size_bytes or 0) for item in items),
            object_count=len(items),
        )
        for workspace_id, items in grouped.items()
    ]


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
