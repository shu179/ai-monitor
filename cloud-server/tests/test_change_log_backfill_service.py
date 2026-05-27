from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("SURFACED_CLOUD_SECRET_KEY", "test-secret-key-for-change-log-backfill")

from app.services.change_log_backfill_service import backfill_workspace_change_logs  # noqa: E402
from scripts.backfill_change_log import _workspace_ids  # noqa: E402


class ChangeLogBackfillServiceTests(unittest.TestCase):
    @patch("app.services.change_log_backfill_service.record_workspace_change")
    def test_backfills_each_stream_without_notify(self, record_change) -> None:
        db = MagicMock()
        db.execute.side_effect = [
            _ScalarResult(["1"]),
            _ScalarResult(["11", "12"]),
            _ScalarResult(["21"]),
            _ScalarResult([]),
        ]

        stats = backfill_workspace_change_logs(db, workspace_id=7, limit=100)

        self.assertEqual(stats, {"tasks": 1, "runs": 2, "articles": 1, "references": 0})
        self.assertEqual(record_change.call_count, 4)
        self.assertEqual(record_change.call_args_list[0].kwargs["stream"], "tasks")
        self.assertEqual(record_change.call_args_list[0].kwargs["kind"], "task.updated")
        self.assertEqual(record_change.call_args_list[0].kwargs["ref_id"], "1")
        self.assertTrue(all(call.kwargs["notify"] is False for call in record_change.call_args_list))


class BackfillChangeLogScriptTests(unittest.TestCase):
    def test_workspace_ids_returns_explicit_workspace_without_query(self) -> None:
        db = MagicMock()

        self.assertEqual(_workspace_ids(db, workspace_id=42), [42])
        db.execute.assert_not_called()


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def __iter__(self):
        return iter(self._rows)


if __name__ == "__main__":
    unittest.main()
