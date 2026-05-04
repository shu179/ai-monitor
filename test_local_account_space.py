from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import core.article_reference_index as article_reference_index
from core.diagnostics import record_event
from core.cloud_outbox import CloudOutbox
from core.cloud_session_store import CloudSessionStore
from core.monitoring_sync import export_monitoring_sync_bundle
from core.local_account_space import (
    account_profile_dir_from_session,
    account_profile_key,
    ensure_account_space,
)


def _session(user_id: int, workspace_id: int = 1) -> dict:
    return {
        "base_url": "https://api.surfacedlab.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {
            "id": user_id,
            "workspace_id": workspace_id,
            "username": f"user{user_id}",
            "role": "operator",
        },
    }


class LocalAccountSpaceTests(unittest.TestCase):
    def test_profile_key_separates_users(self):
        first = account_profile_key(_session(2))
        second = account_profile_key(_session(3))

        self.assertNotEqual(first, second)
        self.assertIn("workspace1_user2", first)
        self.assertIn("workspace1_user3", second)

    def test_account_space_copies_legacy_files_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_root = Path(tmpdir) / "profiles"
            with patch("core.local_account_space.ACCOUNT_PROFILE_ROOT", profile_root):
                profile_dir = account_profile_dir_from_session(_session(2))
                assert profile_dir is not None

                legacy_config = Path(tmpdir) / "config.yaml"
                legacy_config.write_text("tasks: []\n", encoding="utf-8")
                target_config = profile_dir / "config.yaml"
                target_config.parent.mkdir(parents=True, exist_ok=True)
                target_config.write_text("existing: true\n", encoding="utf-8")

                with patch("core.local_account_space.get_data_root", return_value=Path(tmpdir)):
                    ensure_account_space(_session(2), copy_legacy=True)

                self.assertEqual(target_config.read_text(encoding="utf-8"), "existing: true\n")
                marker = json.loads((profile_dir / "profile_meta.json").read_text(encoding="utf-8"))
                self.assertEqual(marker["user_id"], 2)

    def test_default_outbox_uses_current_account_space(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_root = Path(tmpdir) / "profiles"
            store_path = Path(tmpdir) / "session.json"
            store = CloudSessionStore(store_path)
            session = store.save(_session(2))
            with patch("core.local_account_space.ACCOUNT_PROFILE_ROOT", profile_root):
                profile_dir = account_profile_dir_from_session(session)
                assert profile_dir is not None

                with patch("core.local_account_space.CloudSessionStore", return_value=store):
                    outbox = CloudOutbox()
                    self.assertEqual(outbox.path, profile_dir / "user_data" / "cloud_outbox.json")

    def test_account_scoped_helpers_use_current_profile(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_root = Path(tmpdir) / "profiles"
            store_path = Path(tmpdir) / "session.json"
            store = CloudSessionStore(store_path)
            session = store.save(_session(2))
            with patch("core.local_account_space.ACCOUNT_PROFILE_ROOT", profile_root):
                profile_dir = account_profile_dir_from_session(session)
                assert profile_dir is not None
                ensure_account_space(session, copy_legacy=False)

                history_dir = profile_dir / "logs" / "history"
                history_dir.mkdir(parents=True, exist_ok=True)
                (history_dir / "品牌A.json").write_text(
                    json.dumps([{"id": "run-1"}], ensure_ascii=False),
                    encoding="utf-8",
                )
                daily_path = profile_dir / "user_data" / "daily_task_status.json"
                daily_path.parent.mkdir(parents=True, exist_ok=True)
                daily_path.write_text('{"task-a": {"status": "success"}}', encoding="utf-8")

                with patch("core.local_account_space.CloudSessionStore", return_value=store):
                    record_event("sync", "account scoped diagnostic")
                    bundle = export_monitoring_sync_bundle()
                    index_signature = article_reference_index.get_reference_index_file_signature("品牌A", "task-a")

                self.assertTrue((profile_dir / "logs" / "diagnostics.json").exists())
                self.assertEqual(bundle["daily_task_status"]["task-a"]["status"], "success")
                self.assertIn("品牌A.json", bundle["history_files"])
                self.assertTrue(str(index_signature[0]).startswith(str(profile_dir / "logs" / "article_reference_index")))


if __name__ == "__main__":
    unittest.main()
