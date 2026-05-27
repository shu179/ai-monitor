from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.cloud_state_delta_inbox import CloudStateDeltaInbox


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


if __name__ == "__main__":
    unittest.main()
