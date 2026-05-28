from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.sync_queue_ops_service import requeue_sync_queue_items  # noqa: E402


class SyncQueueOpsServiceTests(unittest.TestCase):
    def test_requeue_dry_run_selects_bounded_items_without_mutation(self) -> None:
        db = MagicMock()
        db.execute.return_value.all.return_value = [
            SimpleNamespace(
                id=7,
                created_at=datetime(2026, 5, 28, tzinfo=timezone.utc),
                status="blocked",
                partition_key="3:article:abc",
            )
        ]

        result = requeue_sync_queue_items(
            db,
            workspace_id=3,
            statuses=["blocked"],
            partition_key="3:article:abc",
            limit=50,
            dry_run=True,
        )

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["selected"], 1)
        self.assertEqual(result["requeued"], 0)
        self.assertEqual(result["items"][0]["status"], "blocked")
        sql = str(db.execute.call_args.args[0]).lower()
        params = db.execute.call_args.args[1]
        self.assertIn("status = any", sql)
        self.assertIn("partition_key = :partition_key", sql)
        self.assertEqual(params["statuses"], ["blocked"])
        db.commit.assert_not_called()

    def test_requeue_updates_selected_item_keys_to_pending(self) -> None:
        db = MagicMock()
        selected_at = datetime(2026, 5, 28, tzinfo=timezone.utc)
        select_result = MagicMock()
        select_result.all.return_value = [
            SimpleNamespace(
                id=7,
                created_at=selected_at,
                status="dead_letter",
                partition_key="3:article:abc",
            )
        ]
        update_result = MagicMock(rowcount=1)
        db.execute.side_effect = [select_result, update_result]

        result = requeue_sync_queue_items(db, workspace_id=3, statuses=["dead_letter"], limit=50, dry_run=False)

        self.assertFalse(result["dry_run"])
        self.assertEqual(result["selected"], 1)
        self.assertEqual(result["requeued"], 1)
        update_sql = str(db.execute.call_args_list[1].args[0]).lower()
        update_params = db.execute.call_args_list[1].args[1]
        self.assertIn("set status = 'pending'", update_sql)
        self.assertIn("worker_id = null", update_sql)
        self.assertIn("leased_until = null", update_sql)
        self.assertIn("last_error = null", update_sql)
        self.assertIn("item.created_at = selected.created_at", update_sql)
        self.assertEqual(update_params["id_0"], 7)
        self.assertEqual(update_params["created_at_0"], selected_at)
        db.commit.assert_called_once()

    def test_requeue_rejects_missing_workspace(self) -> None:
        with self.assertRaises(ValueError):
            requeue_sync_queue_items(MagicMock(), workspace_id=0)

    def test_admin_route_is_registered(self) -> None:
        from app.api.routes.admin import router

        paths = {route.path for route in router.routes}
        self.assertIn("/ops/sync-queue/requeue", paths)


if __name__ == "__main__":
    unittest.main()
