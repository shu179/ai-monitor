from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.sync_queue_diagnostics import build_sync_queue_report, format_sync_queue_report


class SyncQueueDiagnosticsTests(unittest.TestCase):
    def test_report_formats_healthy_queue(self) -> None:
        db = _db(
            status_rows=[
                SimpleNamespace(status="pending", count=0),
                SimpleNamespace(status="done", count=10),
            ],
            pending_age=None,
            expired=0,
            dead_letters=[],
            workspace_rows=[],
            in_progress_age=None,
            recent_done_latency=SimpleNamespace(completed=10, avg_ms=12, max_ms=40),
            shard_row=SimpleNamespace(total=2, active=2, expired=0),
        )

        report = build_sync_queue_report(db)

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["counts"]["done"], 10)
        self.assertEqual(report["worker_state"], "active")
        text = format_sync_queue_report(report)
        self.assertIn("status=ok", text)
        self.assertIn("worker_state=active", text)
        self.assertIn("recent_done_latency_ms=", text)
        self.assertIn("queue_by_workspace: none", text)

    def test_report_warns_on_dead_letters_and_expired_items(self) -> None:
        db = _db(
            status_rows=[
                SimpleNamespace(status="pending", count=3),
                SimpleNamespace(status="dead_letter", count=1),
            ],
            pending_age=120,
            expired=2,
            workspace_rows=[
                SimpleNamespace(
                    workspace_id=7,
                    pending=3,
                    in_progress=2,
                    dead_letter=1,
                    oldest_age_seconds=180,
                )
            ],
            in_progress_age=90,
            recent_done_latency=SimpleNamespace(completed=5, avg_ms=24, max_ms=120),
            dead_letters=[
                SimpleNamespace(
                    workspace_id=7,
                    count=1,
                    latest_created_at=datetime(2026, 5, 27, tzinfo=timezone.utc),
                )
            ],
            shard_row=SimpleNamespace(total=4, active=1, expired=3),
        )

        report = build_sync_queue_report(db)

        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["worker_state"], "active")
        self.assertEqual(report["expired_in_progress"], 2)
        self.assertEqual(report["oldest_in_progress_age_seconds"], 90)
        self.assertEqual(report["recent_done_latency_ms"]["max"], 120)
        self.assertEqual(report["queue_by_workspace"][0]["pending"], 3)
        text = format_sync_queue_report(report)
        self.assertIn("queue_by_workspace:", text)
        self.assertIn("oldest_in_progress_age_seconds=90", text)
        self.assertIn("dead_letters_by_workspace:", text)
        self.assertIn("workspace=7", text)

    def test_report_treats_empty_queue_with_expired_leases_as_idle(self) -> None:
        db = _db(
            status_rows=[],
            pending_age=None,
            expired=0,
            workspace_rows=[],
            in_progress_age=None,
            recent_done_latency=SimpleNamespace(completed=0, avg_ms=0, max_ms=0),
            dead_letters=[],
            shard_row=SimpleNamespace(total=1024, active=0, expired=1024),
        )

        report = build_sync_queue_report(db)

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["worker_state"], "idle")
        self.assertIn("worker_state=idle", format_sync_queue_report(report))

    def test_report_warns_when_pending_work_has_no_active_worker(self) -> None:
        db = _db(
            status_rows=[SimpleNamespace(status="pending", count=8)],
            pending_age=15,
            expired=0,
            workspace_rows=[
                SimpleNamespace(
                    workspace_id=7,
                    pending=8,
                    in_progress=0,
                    dead_letter=0,
                    oldest_age_seconds=15,
                )
            ],
            in_progress_age=None,
            recent_done_latency=SimpleNamespace(completed=0, avg_ms=0, max_ms=0),
            dead_letters=[],
            shard_row=SimpleNamespace(total=1024, active=0, expired=1024),
        )

        report = build_sync_queue_report(db)

        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["worker_state"], "stalled_no_active_worker")


def _db(*, status_rows, pending_age, expired, dead_letters, workspace_rows, in_progress_age, recent_done_latency, shard_row):
    db = MagicMock()
    calls = [
        _FakeResult(rows=status_rows),
        _FakeResult(scalar=pending_age),
        _FakeResult(scalar=expired),
        _FakeResult(rows=dead_letters),
        _FakeResult(rows=workspace_rows),
        _FakeResult(scalar=in_progress_age),
        _FakeResult(first=recent_done_latency),
        _FakeResult(first=shard_row),
    ]
    db.execute.side_effect = calls
    return db


class _FakeResult:
    def __init__(self, *, rows=None, scalar=None, first=None):
        self._rows = rows or []
        self._scalar = scalar
        self._first = first

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        return self._scalar

    def first(self):
        return self._first


if __name__ == "__main__":
    unittest.main()
