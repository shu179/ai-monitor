from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402

from app.services.admin_service import _new_task_key_variant, _resolve_create_task_key  # noqa: E402
from app.services.sync_service import _task_allows_operation  # noqa: E402


class SoftDeleteGuardTests(unittest.TestCase):
    def test_deleted_task_accepts_only_pre_delete_run(self) -> None:
        deleted_at = datetime(2026, 5, 4, 2, 0, 0, tzinfo=timezone.utc)
        task = SimpleNamespace(deleted_at=deleted_at, delete_expires_at=deleted_at + timedelta(days=3))

        self.assertTrue(_task_allows_operation(task, run_started_at="2026-05-04T09:59:59+08:00"))
        self.assertTrue(_task_allows_operation(task, run_started_at="2026-05-04T10:00:00+08:00"))
        self.assertFalse(_task_allows_operation(task, run_started_at="2026-05-04T10:00:01+08:00"))
        self.assertFalse(_task_allows_operation(task, run_started_at=""))

    def test_expired_deleted_task_is_rejected(self) -> None:
        task = SimpleNamespace(
            deleted_at=datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc),
            delete_expires_at=datetime(2026, 4, 2, 10, 0, 0, tzinfo=timezone.utc),
        )

        self.assertFalse(_task_allows_operation(task, run_started_at="2026-04-01T09:00:00+00:00"))

    def test_new_task_key_variant_is_bounded(self) -> None:
        candidate = _new_task_key_variant("a" * 128, 1)

        self.assertLessEqual(len(candidate), 128)
        self.assertTrue(candidate.endswith("-new"))
        self.assertNotEqual(candidate, "a" * 128)

    def test_create_task_key_reuses_only_after_soft_deleted_collision(self) -> None:
        db = Mock()
        db.scalar.side_effect = [
            SimpleNamespace(deleted_at=datetime(2026, 5, 4, tzinfo=timezone.utc)),
            None,
        ]

        self.assertEqual(_resolve_create_task_key(db, 1, "brand-key"), "brand-key-new")

    def test_create_task_key_keeps_active_collision_as_conflict(self) -> None:
        db = Mock()
        db.scalar.return_value = SimpleNamespace(deleted_at=None)

        with self.assertRaises(HTTPException) as caught:
            _resolve_create_task_key(db, 1, "brand-key")
        self.assertEqual(caught.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
