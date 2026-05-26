from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class SyncV2PostgresSmokeTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("SURFACED_CLOUD_RUN_POSTGRES_SMOKE", "").strip().lower() in {"1", "true", "yes"},
        "set SURFACED_CLOUD_RUN_POSTGRES_SMOKE=1 and SURFACED_CLOUD_ALLOW_SMOKE=1 for a disposable Postgres database",
    )
    def test_v1_event_flows_through_v2_worker(self) -> None:
        from scripts.smoke_sync_v2 import main

        with patch.dict(os.environ, {"SURFACED_CLOUD_ALLOW_SMOKE": "1"}):
            self.assertEqual(main(), 0)


if __name__ == "__main__":
    unittest.main()
