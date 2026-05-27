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
from app.schemas import SyncEventsRequest  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
