"""Tests for workspace-scoped idempotency across sync_events, run_records, article_reference_events."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import ArticleReferenceEvent, RunRecord, SyncEvent  # noqa: E402
from app.schemas import SyncEventIn  # noqa: E402
from app.services.sync_service import (  # noqa: E402
    _materialize_article_reference_event,
    _materialize_run_record,
    accept_sync_events,
)
from app.sync_event_types import (  # noqa: E402
    EVENT_ARTICLE_REFERENCE,
    EVENT_RUN_RECORD,
)


def _make_user(workspace_id: int, user_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        workspace_id=workspace_id,
        role="operator",
    )


def _make_event(idempotency_key: str = "run:rec-001", event_type: str = EVENT_RUN_RECORD) -> SyncEventIn:
    return SyncEventIn(
        event_type=event_type,
        idempotency_key=idempotency_key,
        payload={"record_id": "rec-001", "task_id": 1, "status": "ok"},
    )


def _extract_on_conflict_columns(stmt) -> list[str]:
    """Extract column names from the ON CONFLICT clause of a compiled INSERT statement."""
    compiled = stmt.compile()
    sql_str = str(compiled).lower()
    # Match: on conflict (col1, col2)
    import re
    m = re.search(r"on\s+conflict\s*\(([^)]+)\)", sql_str)
    if m:
        return [c.strip() for c in m.group(1).split(",")]
    return []


class AcceptSyncEventsIdempotencyTests(unittest.TestCase):
    """Verify that idempotency keys are scoped per workspace."""

    def _mock_db(self, insert_results: list[int | None]) -> MagicMock:
        """Return a mock Session whose scalar() returns values from *insert_results* in order."""
        db = MagicMock()
        db.scalar.side_effect = insert_results
        return db

    @patch("app.services.sync_service._materialize_known_event")
    def test_same_workspace_duplicate_is_rejected(self, _mat) -> None:
        """Two events with the same key in the same workspace: second is a duplicate."""
        user = _make_user(workspace_id=10)
        events = [_make_event("run:rec-001"), _make_event("run:rec-001")]
        db = self._mock_db([42, None])  # first accepted, second rejected

        accepted, duplicates = accept_sync_events(db, user, events)

        self.assertEqual(accepted, 1)
        self.assertEqual(duplicates, 1)
        _mat.assert_called_once()

    @patch("app.services.sync_service._materialize_known_event")
    def test_different_workspace_same_key_both_accepted(self, _mat) -> None:
        """Same idempotency_key in different workspaces should both be accepted."""
        user_a = _make_user(workspace_id=10, user_id=1)
        user_b = _make_user(workspace_id=20, user_id=2)
        events = [_make_event("run:rec-001")]
        db_a = self._mock_db([42])
        db_b = self._mock_db([43])

        acc_a, dup_a = accept_sync_events(db_a, user_a, events)
        acc_b, dup_b = accept_sync_events(db_b, user_b, events)

        self.assertEqual(acc_a, 1)
        self.assertEqual(dup_a, 0)
        self.assertEqual(acc_b, 1)
        self.assertEqual(dup_b, 0)
        self.assertEqual(_mat.call_count, 2)

    @patch("app.services.sync_service._materialize_known_event")
    def test_mixed_accepted_and_duplicate_counts(self, _mat) -> None:
        """Batch with a mix of new and duplicate keys returns correct counts."""
        user = _make_user(workspace_id=10)
        events = [
            _make_event("run:aaaa-001"),
            _make_event("run:bbbb-001"),
            _make_event("run:aaaa-001"),  # duplicate
            _make_event("run:cccc-001"),
            _make_event("run:bbbb-001"),  # duplicate
        ]
        # Insert results: first 3 unique accepted, last 2 are duplicates
        db = self._mock_db([101, 102, None, 103, None])

        accepted, duplicates = accept_sync_events(db, user, events)

        self.assertEqual(accepted, 3)
        self.assertEqual(duplicates, 2)
        self.assertEqual(_mat.call_count, 3)

    @patch("app.services.sync_service._materialize_known_event")
    def test_sync_events_insert_conflict_target_is_workspace_scoped(self, _mat) -> None:
        """Verify the INSERT ON CONFLICT targets (workspace_id, idempotency_key)."""
        user = _make_user(workspace_id=10)
        events = [_make_event("run:rec-999")]
        db = self._mock_db([1])

        accept_sync_events(db, user, events)

        stmt = db.scalar.call_args.args[0]
        conflict_cols = _extract_on_conflict_columns(stmt)
        self.assertEqual(conflict_cols, ["workspace_id", "idempotency_key"])


class MaterializationConflictTargetTests(unittest.TestCase):
    """Verify that materialized tables also use workspace-scoped conflict targets."""

    def test_run_record_conflict_target_is_workspace_scoped(self) -> None:
        """_materialize_run_record should ON CONFLICT (workspace_id, idempotency_key)."""
        db = MagicMock()
        user = _make_user(workspace_id=10)
        event = SyncEventIn(
            event_type=EVENT_RUN_RECORD,
            idempotency_key="run:rec-001",
            payload={"task_id": 1, "platform": "doubao", "keyword": "k", "brand": "b", "mode": "browser"},
        )

        with patch("app.services.sync_service.can_operate_task", return_value=True):
            _materialize_run_record(db, user, event)  # type: ignore[arg-type]

        stmt = db.execute.call_args.args[0]
        conflict_cols = _extract_on_conflict_columns(stmt)
        self.assertEqual(conflict_cols, ["workspace_id", "idempotency_key"])

    def test_article_reference_event_conflict_target_is_workspace_scoped(self) -> None:
        """_materialize_article_reference_event should ON CONFLICT (workspace_id, idempotency_key)."""
        db = MagicMock()
        user = _make_user(workspace_id=10)
        event = SyncEventIn(
            event_type=EVENT_ARTICLE_REFERENCE,
            idempotency_key="ref:hash-001",
            payload={
                "task_id": 1,
                "url": "https://example.com/a",
                "platform": "doubao",
                "record_day": "2026-05-10",
                "source_record_key": "run:rec-001",
            },
        )

        with (
            patch("app.services.sync_service.can_operate_task", return_value=True),
            patch("app.services.sync_service._ensure_cloud_article", return_value=42),
        ):
            _materialize_article_reference_event(db, user, event)  # type: ignore[arg-type]

        stmt = db.execute.call_args.args[0]
        conflict_cols = _extract_on_conflict_columns(stmt)
        self.assertEqual(conflict_cols, ["workspace_id", "idempotency_key"])


class ModelConstraintTests(unittest.TestCase):
    """Verify that the SQLAlchemy models define workspace-scoped unique constraints."""

    def _get_unique_constraints(self, model) -> list[tuple[str, ...]]:
        """Return list of (col_names,) for each UniqueConstraint on the model."""
        from sqlalchemy import UniqueConstraint
        constraints = []
        for arg in model.__table_args__:
            if isinstance(arg, UniqueConstraint):
                constraints.append(tuple(c.name for c in arg.columns))
        return constraints

    def test_sync_event_has_workspace_scoped_unique(self) -> None:
        cols = self._get_unique_constraints(SyncEvent)
        self.assertIn(("workspace_id", "idempotency_key"), cols)

    def test_run_record_has_workspace_scoped_unique(self) -> None:
        cols = self._get_unique_constraints(RunRecord)
        self.assertIn(("workspace_id", "idempotency_key"), cols)

    def test_article_reference_event_has_workspace_scoped_unique(self) -> None:
        cols = self._get_unique_constraints(ArticleReferenceEvent)
        self.assertIn(("workspace_id", "idempotency_key"), cols)

    def test_sync_event_no_global_idempotency_unique(self) -> None:
        """Ensure there is no single-column unique constraint on idempotency_key."""
        from sqlalchemy import UniqueConstraint
        for arg in SyncEvent.__table_args__:
            if isinstance(arg, UniqueConstraint):
                col_names = [c.name for c in arg.columns]
                self.assertNotEqual(col_names, ["idempotency_key"],
                                    "Global unique on idempotency_key should have been removed")


if __name__ == "__main__":
    unittest.main()
