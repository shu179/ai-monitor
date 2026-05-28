from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SURFACED_CLOUD_SECRET_KEY", "test-secret-key-for-sync-route")

from app.api.routes.sync import post_events  # noqa: E402
from app.api.routes.sync_v2 import object_upload_create  # noqa: E402
from app.schemas import SyncEventsRequest  # noqa: E402
from app.schemas import ObjectUploadCreateRequest  # noqa: E402
from app.services.object_storage_service import ObjectStorageQuotaExceeded  # noqa: E402
from app.services.sync_v2_service import SyncBackpressureError  # noqa: E402


class SyncRouteBackpressureTests(unittest.TestCase):
    def test_legacy_sync_events_returns_v2_backpressure_headers(self) -> None:
        payload = SyncEventsRequest(events=[])
        current_user = SimpleNamespace(id=1, workspace_id=7)
        db = object()

        with patch(
            "app.api.routes.sync.accept_sync_events",
            side_effect=SyncBackpressureError(
                retry_after_seconds=12,
                queue_depth_hint=23_000,
                throttle_bucket="sync_metadata",
            ),
        ):
            with self.assertRaises(HTTPException) as caught:
                post_events(payload, current_user, db)  # type: ignore[arg-type]

        exc = caught.exception
        self.assertEqual(exc.status_code, 429)
        self.assertEqual(exc.headers["Retry-After"], "12")
        self.assertEqual(exc.headers["X-Queue-Depth-Hint"], "23000")
        self.assertEqual(exc.headers["X-Throttle-Bucket"], "sync_metadata")
        self.assertEqual(exc.detail["retry_after_seconds"], 12)
        self.assertEqual(exc.detail["queue_depth_hint"], 23_000)

    def test_object_upload_create_returns_backpressure_headers_on_quota_reject(self) -> None:
        payload = ObjectUploadCreateRequest(
            sha256="a" * 64,
            size_bytes=1024,
            storage_size_bytes=1024,
            content_type="text/plain",
        )
        current_user = SimpleNamespace(id=1, workspace_id=7)
        db = object()

        with patch(
            "app.api.routes.sync_v2.create_object_upload",
            side_effect=ObjectStorageQuotaExceeded(
                "server object storage quota exceeded",
                retry_after_seconds=60,
                queue_depth_hint=4096,
                throttle_bucket="object_upload",
            ),
        ):
            with self.assertRaises(HTTPException) as caught:
                object_upload_create(payload, current_user, db)  # type: ignore[arg-type]

        exc = caught.exception
        self.assertEqual(exc.status_code, 429)
        self.assertEqual(exc.headers["Retry-After"], "60")
        self.assertEqual(exc.headers["X-Queue-Depth-Hint"], "4096")
        self.assertEqual(exc.headers["X-Throttle-Bucket"], "object_upload")
        self.assertEqual(exc.detail["retry_after_seconds"], 60)
        self.assertEqual(exc.detail["queue_depth_hint"], 4096)
        self.assertEqual(exc.detail["throttle_bucket"], "object_upload")


if __name__ == "__main__":
    unittest.main()
