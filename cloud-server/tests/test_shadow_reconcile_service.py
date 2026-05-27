from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.shadow_reconcile_service import build_shadow_reconcile_report, format_shadow_reconcile_report  # noqa: E402


class ShadowReconcileServiceTests(unittest.TestCase):
    def test_report_is_ok_when_streams_have_coverage_and_migration_done(self) -> None:
        db = _db(
            entity_rows=[
                SimpleNamespace(name="tasks", count=1),
                SimpleNamespace(name="runs", count=2),
                SimpleNamespace(name="articles", count=3),
                SimpleNamespace(name="article_links", count=3),
                SimpleNamespace(name="references", count=4),
                SimpleNamespace(name="agent_commands", count=1),
                SimpleNamespace(name="article_versions", count=3),
            ],
            change_rows=[
                SimpleNamespace(stream="tasks", count=1, max_seq=1),
                SimpleNamespace(stream="runs", count=2, max_seq=2),
                SimpleNamespace(stream="articles", count=3, max_seq=3),
                SimpleNamespace(stream="references", count=4, max_seq=4),
                SimpleNamespace(stream="agent_status", count=1, max_seq=1),
            ],
            migration_rows=[SimpleNamespace(status="completed", count=3)],
            payload_candidates=3,
            missing_versions=0,
            sample_rows=[
                [SimpleNamespace(workspace_id=7, id=1, idempotency_key="run-1", payload="{}")],
                [SimpleNamespace(workspace_id=7, id=2, idempotency_key="url-hash", payload="{}")],
                [],
            ],
        )

        report = build_shadow_reconcile_report(db)

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["coverage"]["articles"]["covered"], True)
        self.assertEqual(report["migration"]["fallback_required"], False)
        self.assertEqual(len(report["samples"]), 2)
        self.assertIn("mismatches: none", format_shadow_reconcile_report(report))

    def test_report_warns_on_missing_change_log_and_migration_tail(self) -> None:
        db = _db(
            entity_rows=[
                SimpleNamespace(name="tasks", count=1),
                SimpleNamespace(name="runs", count=0),
                SimpleNamespace(name="articles", count=1),
                SimpleNamespace(name="article_links", count=0),
                SimpleNamespace(name="references", count=0),
                SimpleNamespace(name="agent_commands", count=0),
                SimpleNamespace(name="article_versions", count=0),
            ],
            change_rows=[],
            migration_rows=[SimpleNamespace(status="failed", count=1)],
            payload_candidates=1,
            missing_versions=1,
            sample_rows=[[], [], []],
        )

        report = build_shadow_reconcile_report(db)

        self.assertEqual(report["status"], "warn")
        codes = {item["code"] for item in report["mismatches"]}
        self.assertIn("tasks.missing_change_log", codes)
        self.assertIn("articles.payload_migration_incomplete", codes)
        text = format_shadow_reconcile_report(report)
        self.assertIn("mismatches:", text)
        self.assertIn("fallback_required=True", text)

    def test_admin_route_is_registered(self) -> None:
        from app.api.routes.admin import router

        paths = {route.path for route in router.routes}

        self.assertIn("/ops/shadow-reconcile", paths)


def _db(*, entity_rows, change_rows, migration_rows, payload_candidates, missing_versions, sample_rows):
    db = MagicMock()
    db.execute.side_effect = [
        _FakeResult(rows=entity_rows),
        _FakeResult(rows=change_rows),
        _FakeResult(rows=migration_rows),
        _FakeResult(scalar=payload_candidates),
        _FakeResult(scalar=missing_versions),
        *[_FakeResult(rows=rows) for rows in sample_rows],
    ]
    return db


class _FakeResult:
    def __init__(self, *, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def all(self):
        return self._rows

    def scalar_one(self):
        return self._scalar


if __name__ == "__main__":
    unittest.main()
