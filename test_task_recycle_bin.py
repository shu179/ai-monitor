from __future__ import annotations

from datetime import datetime
import unittest

from core.task_recycle_bin import (
    deleted_task_snapshots,
    mark_task_delete_pending,
    purge_expired_deleted_tasks,
    restore_deleted_task,
    soft_delete_task,
)


class TaskRecycleBinTests(unittest.TestCase):
    def test_soft_delete_preserves_backup_and_restore_returns_active_task(self):
        config = {
            "tasks": [
                {
                    "task_id": "task-a",
                    "name": "品牌A",
                    "brand": "品牌A",
                    "cloud_task_id": 7,
                    "delete_pending": True,
                    "delete_pending_at": "2026-05-04T09:00:00",
                }
            ]
        }
        now = datetime(2026, 5, 4, 10, 0, 0)

        tombstone = soft_delete_task(config, "task-a", reason="manual_admin_delete", now=now)

        self.assertIsNotNone(tombstone)
        self.assertEqual(config["tasks"], [])
        self.assertEqual(tombstone["id"], "cloud_7")
        self.assertEqual(tombstone["deleted_at"], "2026-05-04T10:00:00")
        self.assertEqual(tombstone["expires_at"], "2026-05-07T10:00:00")
        self.assertNotIn("delete_pending", tombstone["task"])

        result = restore_deleted_task(config, brand_name="品牌A", now=now)

        self.assertTrue(result["ok"])
        self.assertEqual(config["deleted_tasks"], [])
        self.assertEqual(config["tasks"][0]["task_id"], "task-a")
        self.assertNotIn("delete_pending", config["tasks"][0])

    def test_purge_expired_deleted_tasks_keeps_unexpired_backups(self):
        config = {
            "deleted_tasks": [
                {"id": "expired", "expires_at": "2026-05-04T09:59:59", "task": {"name": "旧品牌"}},
                {"id": "active", "expires_at": "2026-05-07T10:00:00", "task": {"name": "新品牌"}},
            ]
        }

        changed = purge_expired_deleted_tasks(config, now=datetime(2026, 5, 4, 10, 0, 0))

        self.assertTrue(changed)
        self.assertEqual([item["id"] for item in config["deleted_tasks"]], ["active"])

    def test_mark_delete_pending_sets_three_day_expiry(self):
        task = {"task_id": "task-a", "name": "品牌A", "delete_pending_error": "旧错误"}

        mark_task_delete_pending(task, now=datetime(2026, 5, 4, 10, 0, 0))

        self.assertTrue(task["delete_pending"])
        self.assertEqual(task["delete_pending_at"], "2026-05-04T10:00:00")
        self.assertEqual(task["delete_pending_expires_at"], "2026-05-07T10:00:00")
        self.assertNotIn("delete_pending_error", task)

    def test_deleted_task_snapshots_match_brand_case_insensitively_after_purge(self):
        config = {
            "deleted_tasks": [
                {
                    "id": "task_demo",
                    "task_id": "demo",
                    "brand": "DemoBrand",
                    "name": "DemoBrand",
                    "deleted_at": "2026-05-04T10:00:00",
                    "expires_at": "2026-05-07T10:00:00",
                    "task": {"task_id": "demo", "name": "DemoBrand", "brand": "DemoBrand"},
                }
            ]
        }

        snapshots = deleted_task_snapshots(config, now=datetime(2026, 5, 4, 10, 0, 0))
        result = restore_deleted_task(config, brand_name="demobrand", now=datetime(2026, 5, 4, 10, 0, 0))

        self.assertEqual(snapshots[0]["brand"], "DemoBrand")
        self.assertTrue(result["ok"])
        self.assertEqual(config["tasks"][0]["brand"], "DemoBrand")


if __name__ == "__main__":
    unittest.main()
