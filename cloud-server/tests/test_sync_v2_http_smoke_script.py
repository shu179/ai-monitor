from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class SyncV2HttpSmokeScriptTests(unittest.TestCase):
    def test_refuses_to_run_without_explicit_marker(self) -> None:
        from scripts import smoke_sync_v2_http

        with patch.dict(os.environ, {"SURFACED_CLOUD_ALLOW_SMOKE": ""}):
            self.assertEqual(smoke_sync_v2_http.main([]), 2)


if __name__ == "__main__":
    unittest.main()
