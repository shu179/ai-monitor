from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import core.runtime_state as runtime_state
from core.file_lock import CrossProcessRLock


class RuntimeStateLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_state_path = runtime_state.STATE_PATH
        runtime_state.STATE_PATH = Path(self._tmpdir.name) / "user_data" / "runtime_state.json"

    def tearDown(self) -> None:
        runtime_state.STATE_PATH = self._original_state_path
        self._tmpdir.cleanup()

    def test_runtime_state_uses_cross_process_lock_file(self) -> None:
        self.assertIsInstance(runtime_state._LOCK, CrossProcessRLock)  # noqa: SLF001

        state = runtime_state.set_auto_resume_monitoring(True)

        self.assertTrue(state["auto_resume_monitoring"])
        self.assertTrue(runtime_state.should_auto_resume_monitoring())
        self.assertTrue(runtime_state.STATE_PATH.with_name(".runtime_state.json.lock").exists())


if __name__ == "__main__":
    unittest.main()
