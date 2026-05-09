from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.event_service import _changed_visible_task_ids, diff_workspace_event_names, event_id_for_snapshot  # noqa: E402


class EventServiceTests(unittest.TestCase):
    def test_assignment_event_without_visible_change_does_not_force_task_pull(self) -> None:
        previous = {
            "task_updated_at": "2026-05-05T00:00:00+00:00",
            "task_count": 2,
            "assignment_event_id": 10,
            "visible_task_count": 1,
            "visible_task_signature": "7:1:1:2026-05-05T00:00:00+00:00:operate",
        }
        current = {
            **previous,
            "assignment_event_id": 11,
        }

        self.assertEqual(diff_workspace_event_names(previous, current), [])

    def test_assignment_event_with_visible_change_requests_task_pull(self) -> None:
        previous = {
            "task_updated_at": "2026-05-05T00:00:00+00:00",
            "task_count": 2,
            "assignment_event_id": 10,
            "visible_task_count": 1,
            "visible_task_signature": "7:1:1:2026-05-05T00:00:00+00:00:operate",
        }
        current = {
            **previous,
            "assignment_event_id": 11,
            "visible_task_count": 2,
            "visible_task_signature": "7:1:1:2026-05-05T00:00:00+00:00:operate|8:1:1:2026-05-05T00:00:00+00:00:operate",
        }

        self.assertEqual(diff_workspace_event_names(previous, current), ["assignment_changed"])
        self.assertEqual(_changed_visible_task_ids(previous, current), [8])

    def test_changed_visible_task_ids_detects_update_and_removal(self) -> None:
        previous = {
            "visible_task_signature": (
                "7:1:1:2026-05-05T00:00:00+00:00:operate"
                "|8:1:1:2026-05-05T00:00:00+00:00:operate"
            ),
        }
        current = {
            "visible_task_signature": "7:2:1:2026-05-05T01:00:00+00:00:operate",
        }

        self.assertEqual(_changed_visible_task_ids(previous, current), [7, 8])

    def test_visible_scope_signature_change_without_assignment_event_requests_task_pull(self) -> None:
        previous = {
            "task_updated_at": "2026-05-05T00:00:00+00:00",
            "task_count": 2,
            "assignment_event_id": 10,
            "visible_task_count": 2,
            "visible_task_signature": "7:1:1:2026-05-05T00:00:00+00:00:|8:1:1:2026-05-05T00:00:00+00:00:",
        }
        current = {
            **previous,
            "visible_task_signature": (
                "7:1:1:2026-05-05T00:00:00+00:00:|8:1:1:2026-05-05T00:00:00+00:00:"
                "|viewer_scope:viewer_all:3"
            ),
        }

        self.assertEqual(diff_workspace_event_names(previous, current), ["task_changed"])

    def test_task_day_status_event_requests_status_pull(self) -> None:
        previous = {
            "task_updated_at": "2026-05-05T00:00:00+00:00",
            "task_count": 1,
            "assignment_event_id": 10,
            "run_record_id": 20,
            "task_day_status_event_id": 30,
            "article_event_id": 0,
            "reference_event_id": 40,
            "sync_event_id": 50,
            "visible_task_count": 1,
            "visible_task_signature": "7:1:1:2026-05-05T00:00:00+00:00:operate",
        }
        current = {
            **previous,
            "task_day_status_event_id": 31,
            "sync_event_id": 51,
        }

        self.assertEqual(diff_workspace_event_names(previous, current), ["task_day_status_changed"])

    def test_article_event_requests_article_pull(self) -> None:
        previous = {
            "task_updated_at": "2026-05-05T00:00:00+00:00",
            "task_count": 1,
            "assignment_event_id": 10,
            "run_record_id": 20,
            "task_day_status_event_id": 30,
            "article_event_id": 40,
            "reference_event_id": 50,
            "sync_event_id": 60,
            "visible_task_count": 1,
            "visible_task_signature": "7:1:1:2026-05-05T00:00:00+00:00:operate",
        }
        current = {
            **previous,
            "article_event_id": 41,
            "sync_event_id": 61,
        }

        self.assertEqual(diff_workspace_event_names(previous, current), ["article_changed"])

    def test_event_id_for_snapshot_stays_compact_with_large_visible_signature(self) -> None:
        snapshot = {
            "task_updated_at": "2026-05-06T07:48:01.737663+00:00",
            "task_count": 20,
            "assignment_event_id": 12,
            "run_record_id": 47,
            "task_day_status_event_id": 0,
            "article_event_id": 0,
            "reference_event_id": 0,
            "sync_event_id": 51,
            "visible_task_count": 18,
            "visible_task_signature": "|".join(
                f"{task_id}:1:1:2026-05-06T07:47:{task_id:02d}+00:00:view:3"
                for task_id in range(1, 80)
            ),
        }

        event_id = event_id_for_snapshot(snapshot)

        self.assertLessEqual(len(event_id), 80)
        self.assertNotIn("visible_task_signature", event_id)


if __name__ == "__main__":
    unittest.main()
