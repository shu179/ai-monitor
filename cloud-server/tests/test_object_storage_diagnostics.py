from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import ObjectManifest
from app.services.object_storage_diagnostics import build_object_storage_report, format_object_storage_report


class ObjectStorageDiagnosticsTests(unittest.TestCase):
    def test_report_is_ok_when_manifest_matches_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sha = "b" * 64
            path = Path(tmp) / f"7/{sha[:2]}/{sha[2:4]}/{sha}"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"hello")
            db = _db_with_manifests(
                [
                    ObjectManifest(
                        id="object-1",
                        workspace_id=7,
                        sha256=sha,
                        size_bytes=5,
                        storage_size_bytes=5,
                        content_type="text/plain",
                        storage_key=f"7/{sha[:2]}/{sha[2:4]}/{sha}",
                        compression="none",
                        status="active",
                    )
                ]
            )

            with patch("app.services.object_storage_diagnostics.get_settings", return_value=_settings(tmp)):
                report = build_object_storage_report(db)

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["local_size_bytes"], 5)
        self.assertEqual(report["manifest_total_bytes"], 5)
        self.assertEqual(report["workspace_usage"][0]["workspace_id"], 7)
        self.assertIn("max_safe_upload_bytes", report["pressure"])
        self.assertEqual(report["pressure"]["warnings"], [])
        self.assertIn("status=ok", format_object_storage_report(report))

    def test_report_flags_missing_and_orphan_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orphan = Path(tmp) / "orphan.bin"
            orphan.write_bytes(b"orphan")
            db = _db_with_manifests(
                [
                    ObjectManifest(
                        id="object-1",
                        workspace_id=7,
                        sha256="c" * 64,
                        size_bytes=5,
                        storage_size_bytes=5,
                        content_type="text/plain",
                        storage_key=f"7/{'c' * 2}/{'c' * 2}/{'c' * 64}",
                        compression="none",
                        status="active",
                    )
                ]
            )

            with patch("app.services.object_storage_diagnostics.get_settings", return_value=_settings(tmp)):
                report = build_object_storage_report(db)

        self.assertEqual(report["status"], "error")
        self.assertEqual(len(report["missing_files"]), 1)
        self.assertEqual(len(report["orphan_files"]), 1)
        text = format_object_storage_report(report)
        self.assertIn("missing_files:", text)
        self.assertIn("orphan_files:", text)

    def test_report_flags_impossible_capacity_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _db_with_manifests([])

            with (
                patch("app.services.object_storage_diagnostics.get_settings", return_value=_settings(tmp)),
                patch("app.services.object_storage_diagnostics.shutil.disk_usage") as disk_usage,
            ):
                disk_usage.return_value = SimpleNamespace(total=12_000, used=2_000, free=10_000)
                report = build_object_storage_report(db)

        self.assertEqual(report["status"], "error")
        self.assertTrue(report["writable"])
        self.assertIn("total_quota_exceeds_disk_capacity_after_min_free", report["capacity_errors"])
        self.assertIn("not_enough_free_space_for_max_file_upload", report["capacity_errors"])
        text = format_object_storage_report(report)
        self.assertIn("capacity_errors=", text)

    def test_report_warns_when_quota_pressure_is_high(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sha = "f" * 64
            path = Path(tmp) / f"7/{sha[:2]}/{sha[2:4]}/{sha}"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"123456789")
            db = _db_with_manifests(
                [
                    ObjectManifest(
                        id="object-1",
                        workspace_id=7,
                        sha256=sha,
                        size_bytes=9,
                        storage_size_bytes=9,
                        content_type="text/plain",
                        storage_key=f"7/{sha[:2]}/{sha[2:4]}/{sha}",
                        compression="none",
                        status="active",
                    )
                ]
            )
            settings = _settings(tmp)
            settings.object_storage_total_quota_bytes = 10
            settings.object_storage_workspace_quota_bytes = 10
            settings.object_storage_max_file_bytes = 1
            settings.object_storage_min_free_bytes = 1

            with patch("app.services.object_storage_diagnostics.get_settings", return_value=settings):
                report = build_object_storage_report(db)

        self.assertEqual(report["status"], "warn")
        self.assertIn("total_quota_above_80_percent", report["pressure"]["warnings"])
        self.assertIn("workspace_7_quota_above_80_percent", report["pressure"]["warnings"])
        self.assertEqual(report["workspace_usage"][0]["remaining_quota_bytes"], 1)
        text = format_object_storage_report(report)
        self.assertIn("pressure=", text)
        self.assertIn("quota_used=", text)


def _db_with_manifests(manifests: list[ObjectManifest]):
    db = MagicMock()

    def execute_side_effect(stmt):
        text = str(stmt)
        if "GROUP BY object_manifests.workspace_id" in text:
            grouped: dict[int, list[ObjectManifest]] = {}
            for manifest in manifests:
                grouped.setdefault(int(manifest.workspace_id), []).append(manifest)
            return _FakeResult(
                [
                    SimpleNamespace(
                        workspace_id=workspace_id,
                        storage_size_bytes=sum(int(item.storage_size_bytes) for item in items),
                        object_count=len(items),
                    )
                    for workspace_id, items in grouped.items()
                ]
            )
        return _FakeResult(
            [
                SimpleNamespace(
                    id=manifest.id,
                    workspace_id=manifest.workspace_id,
                    storage_key=manifest.storage_key,
                    storage_size_bytes=manifest.storage_size_bytes,
                )
                for manifest in manifests
            ]
        )

    db.execute.side_effect = execute_side_effect
    return db


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

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
