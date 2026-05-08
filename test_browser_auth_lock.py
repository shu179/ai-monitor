from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import core.browser_auth as browser_auth
from core.file_lock import CrossProcessRLock


class BrowserAuthMetadataLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_metadata_path = browser_auth._METADATA_PATH  # noqa: SLF001
        browser_auth._METADATA_PATH = Path(self._tmpdir.name) / "user_data" / "browser_auth_profiles.json"  # noqa: SLF001

    def tearDown(self) -> None:
        browser_auth._METADATA_PATH = self._original_metadata_path  # noqa: SLF001
        self._tmpdir.cleanup()

    def test_browser_auth_metadata_uses_cross_process_atomic_write(self) -> None:
        self.assertIsInstance(browser_auth._LOCK, CrossProcessRLock)  # noqa: SLF001

        browser_auth._save_metadata({"platforms": {"doubao": {"active_profile_id": "default"}}})  # noqa: SLF001
        loaded = browser_auth._load_metadata()  # noqa: SLF001

        self.assertEqual(loaded["platforms"]["doubao"]["active_profile_id"], "default")
        self.assertTrue(browser_auth._METADATA_PATH.with_name(".browser_auth_profiles.json.lock").exists())  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
