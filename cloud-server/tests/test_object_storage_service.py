from __future__ import annotations

import sys
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.object_storage_service import (  # noqa: E402
    INLINE_STRATEGY,
    MULTIPART_STRATEGY,
    SINGLE_PUT_STRATEGY,
    ObjectStorageQuotaExceeded,
    S3CompatibleObjectStorageClient,
    LocalDiskObjectStorageClient,
    normalize_compression,
    object_storage_key,
    store_local_object_upload_content,
    upload_strategy_for_size,
)
from app.services.sync_v2_service import LIMITS  # noqa: E402


SHA = "a" * 64


class ObjectStoragePureFunctionTests(unittest.TestCase):
    def test_upload_strategy_thresholds(self) -> None:
        self.assertEqual(upload_strategy_for_size(LIMITS["inline_blob_max_bytes"]), INLINE_STRATEGY)
        self.assertEqual(upload_strategy_for_size(LIMITS["inline_blob_max_bytes"] + 1), SINGLE_PUT_STRATEGY)
        self.assertEqual(upload_strategy_for_size(LIMITS["single_put_max_bytes"] + 1), MULTIPART_STRATEGY)

    def test_storage_key_is_workspace_scoped_and_sharded(self) -> None:
        self.assertEqual(object_storage_key(7, SHA), f"7/{SHA[:2]}/{SHA[2:4]}/{SHA}")

    def test_text_payload_defaults_to_zstd_above_inline_threshold(self) -> None:
        self.assertEqual(
            normalize_compression(
                None,
                content_type="text/plain",
                size_bytes=LIMITS["inline_blob_max_bytes"] + 1,
            ),
            "zstd",
        )

    def test_presigned_url_uses_cdn_compatible_virtual_host_style(self) -> None:
        settings = SimpleNamespace(
            object_storage_endpoint_url="https://r2.example.com",
            object_storage_bucket="bucket",
            object_storage_region="auto",
            object_storage_access_key_id="key",
            object_storage_secret_access_key="secret",
            object_storage_force_path_style=False,
        )
        url = S3CompatibleObjectStorageClient(settings).presign_put_object("7/aa/bb/key.txt", expires_seconds=900)

        self.assertTrue(url.startswith("https://bucket.r2.example.com/7/aa/bb/key.txt?"))
        self.assertIn("X-Amz-Algorithm=AWS4-HMAC-SHA256", url)
        self.assertIn("X-Amz-Signature=", url)


class ObjectUploadFlowTests(unittest.TestCase):
    def test_inline_upload_does_not_require_object_storage_config(self) -> None:
        from app.services.object_storage_service import create_object_upload

        db = MagicMock()
        db.scalar.return_value = None
        user = SimpleNamespace(workspace_id=7)

        result = create_object_upload(
            db,
            user,  # type: ignore[arg-type]
            sha256=SHA,
            size_bytes=100,
            content_type="text/plain",
        )

        self.assertEqual(result["strategy"], INLINE_STRATEGY)
        self.assertIsNone(result["session_id"])
        db.commit.assert_not_called()

    def test_non_inline_upload_falls_back_to_local_disk_session(self) -> None:
        from app.services.object_storage_service import create_object_upload

        db = MagicMock()
        session_holder: dict[str, object] = {}
        db.scalar.return_value = None
        db.add.side_effect = lambda obj: session_holder.setdefault("session", obj)
        db.refresh.side_effect = lambda _obj: None
        user = SimpleNamespace(workspace_id=7)

        with (
            patch("app.services.object_storage_service.object_storage_client") as client_factory,
            patch("app.services.object_storage_service._enforce_object_limits"),
        ):
            client_factory.return_value = LocalDiskObjectStorageClient(_settings_with_local_dir("/tmp/object-data"))
            result = create_object_upload(
                    db,
                    user,  # type: ignore[arg-type]
                    sha256=SHA,
                    size_bytes=LIMITS["inline_blob_max_bytes"] + 1,
                    content_type="application/octet-stream",
                )

        self.assertEqual(result["strategy"], SINGLE_PUT_STRATEGY)
        self.assertIn(f"/api/v2/objects/uploads/{result['session_id']}/content", result["upload"]["url"])
        self.assertTrue(str(session_holder["session"].storage_provider_upload_id).startswith("local:"))
        db.commit.assert_called_once()

    @patch("app.services.object_storage_service._enforce_object_limits")
    @patch("app.services.object_storage_service.object_storage_client")
    def test_multipart_upload_records_session_and_presigns_parts(self, client_factory, _quota) -> None:
        from app.services.object_storage_service import create_object_upload, presign_object_upload_parts

        client = MagicMock()
        client.is_local = False
        client.create_multipart_upload.return_value = "provider-upload-1"
        client.presign_upload_part.side_effect = lambda _key, upload_id, part_number, expires_seconds: (
            f"https://upload.example/part-{part_number}?uploadId={upload_id}&expires={expires_seconds}"
        )
        client_factory.return_value = client
        session_holder: dict[str, object] = {}

        db = MagicMock()
        user = SimpleNamespace(workspace_id=7)

        def scalar_side_effect(stmt):
            text = str(stmt)
            if "object_manifests" in text:
                return None
            if "object_upload_sessions" in text:
                return session_holder.get("session")
            return None

        def add_side_effect(obj):
            session_holder["session"] = obj

        db.scalar.side_effect = scalar_side_effect
        db.add.side_effect = add_side_effect
        db.refresh.side_effect = lambda _obj: None

        result = create_object_upload(
            db,
            user,  # type: ignore[arg-type]
            sha256=SHA,
            size_bytes=LIMITS["multipart_part_bytes"] * 2 + 1,
            content_type="application/octet-stream",
        )

        self.assertEqual(result["strategy"], MULTIPART_STRATEGY)
        self.assertIsNotNone(result["session_id"])
        self.assertGreater(result["parts_total"], 1)
        client.create_multipart_upload.assert_called_once()
        db.commit.assert_called_once()

        presigned = presign_object_upload_parts(
            db,
            user,  # type: ignore[arg-type]
            session_id=str(result["session_id"]),
            part_numbers=[2, 1, 1],
        )

        self.assertEqual([item["part_number"] for item in presigned["upload_urls"]], [1, 2])
        self.assertIn("uploadId=provider-upload-1", presigned["upload_urls"][0]["url"])

    @patch("app.services.object_storage_service.object_storage_client")
    def test_complete_upload_creates_manifest(self, client_factory) -> None:
        from app.models import ObjectUploadSession
        from app.services.object_storage_service import complete_object_upload

        client = MagicMock()
        client_factory.return_value = client
        user = SimpleNamespace(workspace_id=7)
        upload_session = ObjectUploadSession(
            id="session-1",
            workspace_id=7,
            sha256=SHA,
            size_bytes=1024,
            content_type="text/plain",
            storage_provider_upload_id="single-upload",
            status="initiated",
            part_size_bytes=LIMITS["multipart_part_bytes"],
            parts_total=1,
            parts_completed=0,
            expires_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc)
            + __import__("datetime").timedelta(minutes=10),
        )
        db = MagicMock()
        db.scalar.side_effect = [upload_session, None, 0, 0]
        db.refresh.side_effect = lambda _obj: None

        with patch("app.services.object_storage_service.get_settings", return_value=_settings_with_local_dir("/tmp/object-data")):
            result = complete_object_upload(
                db,
                user,  # type: ignore[arg-type]
                session_id="session-1",
                storage_size_bytes=900,
                compression="zstd",
            )

        self.assertEqual(result["status"], "active")
        self.assertEqual(result["storage_key"], f"7/{SHA[:2]}/{SHA[2:4]}/{SHA}")
        added_manifest = db.add.call_args.args[0]
        self.assertEqual(added_manifest.workspace_id, 7)
        self.assertEqual(added_manifest.sha256, SHA)
        self.assertEqual(added_manifest.compression, "zstd")
        db.commit.assert_called_once()

    @patch("app.services.object_storage_service.object_storage_client")
    def test_complete_upload_rechecks_quota_before_manifest(self, client_factory) -> None:
        from app.models import ObjectUploadSession
        from app.services.object_storage_service import complete_object_upload

        client = MagicMock()
        client.is_local = False
        client_factory.return_value = client
        user = SimpleNamespace(workspace_id=7)
        upload_session = ObjectUploadSession(
            id="session-1",
            workspace_id=7,
            sha256=SHA,
            size_bytes=1024,
            content_type="text/plain",
            storage_provider_upload_id="single-upload",
            status="initiated",
            part_size_bytes=LIMITS["multipart_part_bytes"],
            parts_total=1,
            parts_completed=0,
            expires_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc)
            + __import__("datetime").timedelta(minutes=10),
        )
        db = MagicMock()
        db.scalar.side_effect = [
            upload_session,
            None,
            10 * 1024 * 1024 * 1024,
            0,
        ]

        with patch("app.services.object_storage_service.get_settings", return_value=_settings_with_local_dir("/tmp/object-data")):
            with self.assertRaises(ObjectStorageQuotaExceeded):
                complete_object_upload(
                    db,
                    user,  # type: ignore[arg-type]
                    session_id="session-1",
                    storage_size_bytes=900,
                    compression="zstd",
                )

        self.assertEqual(db.add.call_count, 0)
        db.commit.assert_not_called()

    def test_local_upload_stream_writes_file_and_completes_manifest(self) -> None:
        from app.models import ObjectUploadSession

        body = b"hello local object"
        sha = __import__("hashlib").sha256(body).hexdigest()
        user = SimpleNamespace(workspace_id=7)
        upload_session = ObjectUploadSession(
            id="session-1",
            workspace_id=7,
            sha256=sha,
            size_bytes=len(body),
            content_type="application/octet-stream",
            storage_provider_upload_id="local:upload-1",
            status="initiated",
            part_size_bytes=LIMITS["multipart_part_bytes"],
            parts_total=1,
            parts_completed=0,
            expires_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc)
            + __import__("datetime").timedelta(minutes=10),
        )
        db = MagicMock()
        db.scalar.side_effect = [upload_session, upload_session, None, 0, 0]
        db.refresh.side_effect = lambda _obj: None

        async def chunks():
            yield body[:5]
            yield body[5:]

        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings_with_local_dir(tmp)
            with patch("app.services.object_storage_service.get_settings", return_value=settings):
                result = __import__("asyncio").run(
                    store_local_object_upload_content(
                        db,
                        user,  # type: ignore[arg-type]
                        session_id="session-1",
                        chunks=chunks(),
                    )
                )

            expected_path = Path(tmp) / f"7/{sha[:2]}/{sha[2:4]}/{sha}"
            self.assertEqual(expected_path.read_bytes(), body)
            self.assertEqual(result["status"], "active")

    def test_local_disk_headroom_rejects_when_below_threshold(self) -> None:
        from app.services.object_storage_service import _enforce_local_disk_headroom

        settings = _settings_with_local_dir("/tmp/object-data")
        settings.object_storage_min_free_bytes = 999_999_999_999_999

        with self.assertLogs("app.services.object_storage_service", level="WARNING") as logs:
            with self.assertRaises(ObjectStorageQuotaExceeded):
                _enforce_local_disk_headroom(1, settings=settings)
        self.assertTrue(any("reason=disk_headroom_exceeded" in line for line in logs.output))

    def test_local_upload_rechecks_headroom_while_streaming_and_cleans_tmp(self) -> None:
        from app.models import ObjectUploadSession

        body = b"hello local object"
        sha = __import__("hashlib").sha256(body).hexdigest()
        user = SimpleNamespace(workspace_id=7)
        upload_session = ObjectUploadSession(
            id="session-1",
            workspace_id=7,
            sha256=sha,
            size_bytes=len(body),
            content_type="application/octet-stream",
            storage_provider_upload_id="local:upload-1",
            status="initiated",
            part_size_bytes=LIMITS["multipart_part_bytes"],
            parts_total=1,
            parts_completed=0,
            expires_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc)
            + __import__("datetime").timedelta(minutes=10),
        )
        db = MagicMock()
        db.scalar.return_value = upload_session

        async def chunks():
            yield body[:5]
            yield body[5:]

        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings_with_local_dir(tmp)
            calls = []

            def fake_headroom(_incoming_size, *, settings):
                calls.append(_incoming_size)
                if len(calls) >= 2:
                    raise ObjectStorageQuotaExceeded("server disk free space is below object storage safety threshold")

            with (
                patch("app.services.object_storage_service.get_settings", return_value=settings),
                patch("app.services.object_storage_service._enforce_local_disk_headroom", side_effect=fake_headroom),
            ):
                with self.assertRaises(ObjectStorageQuotaExceeded):
                    __import__("asyncio").run(
                        store_local_object_upload_content(
                            db,
                            user,  # type: ignore[arg-type]
                            session_id="session-1",
                            chunks=chunks(),
                        )
                    )

            self.assertGreaterEqual(len(calls), 2)
            self.assertFalse(list(Path(tmp).rglob("*.tmp-*")))
            self.assertFalse((Path(tmp) / f"7/{sha[:2]}/{sha[2:4]}/{sha}").exists())


if __name__ == "__main__":
    unittest.main()


def _settings_with_local_dir(path: str):
    return SimpleNamespace(
        object_storage_endpoint_url="",
        object_storage_bucket="",
        object_storage_region="auto",
        object_storage_access_key_id="",
        object_storage_secret_access_key="",
        object_storage_force_path_style=False,
        object_storage_local_dir=path,
        object_storage_local_base_url="",
        object_storage_total_quota_bytes=10 * 1024 * 1024 * 1024,
        object_storage_workspace_quota_bytes=5 * 1024 * 1024 * 1024,
        object_storage_max_file_bytes=512 * 1024 * 1024,
        object_storage_min_free_bytes=8 * 1024 * 1024 * 1024,
    )
