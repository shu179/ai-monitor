from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.cloud_state_delta_inbox import CloudStateDeltaInbox, process_state_delta_inbox


class CloudStateDeltaInboxTests(unittest.TestCase):
    def test_record_changes_is_idempotent_by_identity_stream_seq_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = CloudStateDeltaInbox(Path(tmp) / "inbox.sqlite3")
            changes = [
                {
                    "stream": "articles",
                    "seq": 7,
                    "kind": "article.upsert",
                    "ref_id": "article:1",
                    "entity": {"id": 1, "object_id": "obj-1"},
                }
            ]
            object_refs = [{"object_id": "obj-1", "sha256": "abc"}]

            first = inbox.record_changes(identity_key="account-a", changes=changes, object_refs=object_refs)
            second = inbox.record_changes(identity_key="account-a", changes=changes, object_refs=object_refs)
            diagnostics = inbox.diagnostics()

        self.assertEqual(first["created"], 1)
        self.assertEqual(first["duplicates"], 0)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(diagnostics["total"], 1)
        self.assertEqual(diagnostics["by_status"], {"pending": 1})
        self.assertEqual(diagnostics["by_stream"], {"articles": {"pending": 1}})
        self.assertEqual(diagnostics["newest"][0]["stream"], "articles")

    def test_record_changes_is_scoped_by_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = CloudStateDeltaInbox(Path(tmp) / "inbox.sqlite3")
            change = {"stream": "tasks", "seq": 1, "kind": "task.changed", "ref_id": "task:1"}

            left = inbox.record_changes(identity_key="account-a", changes=[change])
            right = inbox.record_changes(identity_key="account-b", changes=[change])
            diagnostics = inbox.diagnostics()

        self.assertEqual(left["created"], 1)
        self.assertEqual(right["created"], 1)
        self.assertEqual(diagnostics["total"], 2)
        self.assertEqual(diagnostics["by_stream"], {"tasks": {"pending": 2}})

    def test_diagnostics_does_not_expose_change_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = CloudStateDeltaInbox(Path(tmp) / "inbox.sqlite3")
            inbox.record_changes(
                identity_key="account-a",
                changes=[
                    {
                        "stream": "profile",
                        "seq": 1,
                        "kind": "profile.update",
                        "ref_id": "profile:1",
                        "entity": {"token": "secret"},
                    }
                ],
            )

            diagnostics = inbox.diagnostics()

        self.assertNotIn("entity", diagnostics["newest"][0])
        self.assertNotIn("change_json", diagnostics["newest"][0])

    def test_claim_pending_marks_items_applying_in_seq_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = CloudStateDeltaInbox(Path(tmp) / "inbox.sqlite3")
            inbox.record_changes(
                identity_key="account-a",
                changes=[
                    {"stream": "tasks", "seq": 2, "kind": "task.changed", "ref_id": "task:2"},
                    {"stream": "tasks", "seq": 1, "kind": "task.changed", "ref_id": "task:1"},
                ],
            )

            claimed = inbox.claim_pending(limit=10, streams=["tasks"])
            diagnostics = inbox.diagnostics()

        self.assertEqual([item["seq"] for item in claimed], [1, 2])
        self.assertEqual([item["status"] for item in claimed], ["applying", "applying"])
        self.assertEqual([item["attempts"] for item in claimed], [1, 1])
        self.assertEqual(diagnostics["by_status"], {"applying": 2})

    def test_mark_applied_and_failed_update_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = CloudStateDeltaInbox(Path(tmp) / "inbox.sqlite3")
            inbox.record_changes(
                identity_key="account-a",
                changes=[
                    {"stream": "tasks", "seq": 1, "kind": "task.changed", "ref_id": "task:1"},
                    {"stream": "runs", "seq": 1, "kind": "run.changed", "ref_id": "run:1"},
                ],
            )
            claimed = inbox.claim_pending(limit=10)

            inbox.mark_applied([claimed[0]["id"]])
            inbox.mark_failed([claimed[1]["id"]], "boom")
            diagnostics = inbox.diagnostics()

        self.assertEqual(diagnostics["by_status"], {"applied": 1, "failed": 1})
        self.assertEqual(diagnostics["failed"][0]["last_error"], "boom")

    def test_process_state_delta_inbox_applies_registered_streams_only(self) -> None:
        applied: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp:
            inbox = CloudStateDeltaInbox(Path(tmp) / "inbox.sqlite3")
            inbox.record_changes(
                identity_key="account-a",
                changes=[
                    {"stream": "tasks", "seq": 1, "kind": "task.changed", "ref_id": "task:1", "entity": {"id": 1}},
                    {"stream": "articles", "seq": 1, "kind": "article.changed", "ref_id": "article:1"},
                ],
            )

            result = process_state_delta_inbox(
                inbox=inbox,
                appliers={"tasks": lambda item: applied.append(item)},
                limit=10,
            )
            diagnostics = inbox.diagnostics()

        self.assertTrue(result["ok"])
        self.assertEqual(result["claimed"], 1)
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(applied[0]["entity"], {"id": 1})
        self.assertEqual(diagnostics["by_status"], {"applied": 1, "pending": 1})

    def test_process_state_delta_inbox_marks_applier_errors_failed(self) -> None:
        def fail(_item):
            raise RuntimeError("cannot apply")

        with tempfile.TemporaryDirectory() as tmp:
            inbox = CloudStateDeltaInbox(Path(tmp) / "inbox.sqlite3")
            inbox.record_changes(
                identity_key="account-a",
                changes=[{"stream": "tasks", "seq": 1, "kind": "task.changed", "ref_id": "task:1"}],
            )

            result = process_state_delta_inbox(inbox=inbox, appliers={"tasks": fail})
            diagnostics = inbox.diagnostics()

        self.assertFalse(result["ok"])
        self.assertEqual(result["failed"], 1)
        self.assertEqual(diagnostics["by_status"], {"failed": 1})
        self.assertEqual(diagnostics["failed"][0]["last_error"], "cannot apply")


if __name__ == "__main__":
    unittest.main()
