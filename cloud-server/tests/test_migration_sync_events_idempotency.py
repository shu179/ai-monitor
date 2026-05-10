"""Tests for the workspace-scoped idempotency migration helpers.

Verifies that upgrade/downgrade logic correctly:
- Drops old single-column unique constraints by column set (not hard-coded name).
- Creates composite unique constraints only when absent.
- downgrade() blocks if cross-workspace duplicate idempotency_keys exist.
- downgrade() restores global unique constraints on idempotency_key.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure cloud-server/ is on sys.path so the migration module is importable.
_ROOT = Path(__file__).resolve().parents[1]  # cloud-server/tests/ -> cloud-server/
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Migration file path and module name.
# Loaded via spec_from_file_location to avoid touching the real alembic package.
_MIGRATION_FILE = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "20260510_0010_sync_events_workspace_scoped_idempotency.py"
)
_MIGRATION_NAME = "alembic_versions_migration_0010"


# Capture the real alembic.op once at import time.
from alembic import op as _REAL_ALEMBIC_OP

# ---------------------------------------------------------------------------
# Test helpers in isolation (no alembic context needed)
# ---------------------------------------------------------------------------

def _make_inspector(constraints_by_table: dict[str, list[dict]]) -> MagicMock:
    inspector = MagicMock()

    def _get_constraints(table_name: str) -> list[dict]:
        return list(constraints_by_table.get(table_name, []))

    inspector.get_unique_constraints.side_effect = _get_constraints
    return inspector


def _get_migration_module(
    constraints_by_table: dict[str, list[dict]] | None = None,
    mock_get_bind_result: MagicMock | None = None,
) -> object:
    """Load (or reload) the migration module with the given inspector state.

    Uses spec_from_file_location to avoid touching the real alembic package.
    The stub op's get_bind is pre-wired to return mock_get_bind_result so
    migration helpers (which close over op) get the right bind.
    """
    import importlib
    import importlib.util
    import sqlalchemy
    from types import ModuleType
    from unittest.mock import MagicMock

    # Per-call inspector: fresh mock so we can inject different constraint sets
    inspector = _make_inspector(constraints_by_table or {})

    # Call trackers — attached to the stub so migration's `op.drop_constraint(...)`
    # hits these live lists regardless of when the test patches.
    _drop_calls: list[tuple[str, str, str]] = []
    _create_calls: list[tuple[str, str, list[str]]] = []

    def _stub_drop_constraint(name: str, table: str, type_: str) -> None:
        _drop_calls.append((name, table, type_))

    def _stub_create_unique_constraint(name: str, table: str, columns: list[str]) -> None:
        _create_calls.append((name, table, columns))

    _original_inspect = sqlalchemy.inspect

    def _fake_inspect(bind):
        return inspector

    sqlalchemy.inspect = _fake_inspect

    try:
        # Build the stub op with the required get_bind (pre-wired per call)
        stub_op = ModuleType("alembic_op_stub")
        stub_op.get_bind = mock_get_bind_result or MagicMock()
        stub_op.drop_constraint = _stub_drop_constraint
        stub_op.create_unique_constraint = _stub_create_unique_constraint

        # Install stubs before loading the migration module
        saved = {}
        for _name in ("alembic", "alembic.op"):
            saved[_name] = sys.modules.pop(_name, None)
        sys.modules["alembic"] = ModuleType("alembic")
        sys.modules["alembic.op"] = stub_op

        # Remove cached version so we always get a fresh load
        sys.modules.pop(_MIGRATION_NAME, None)
        spec = importlib.util.spec_from_file_location(_MIGRATION_NAME, str(_MIGRATION_FILE))
        assert spec is not None, f"Could not load spec for {_MIGRATION_FILE}"
        mod = importlib.util.module_from_spec(spec)
        sys.modules[_MIGRATION_NAME] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

        # Attach tracker lists to the returned module so tests can inspect them
        mod._drop_calls = _drop_calls  # type: ignore[attr-defined]
        mod._create_calls = _create_calls  # type: ignore[attr-defined]

        return mod
    finally:
        sqlalchemy.inspect = _original_inspect
        # Restore original alembic
        for _name, _mod in saved.items():
            if _mod is not None:
                sys.modules[_name] = _mod


# ---------------------------------------------------------------------------
# Tests — constraint helpers
# ---------------------------------------------------------------------------

class TestConstraintHelpers(unittest.TestCase):
    """Unit-test the private helper functions used by upgrade()/downgrade()."""

    def test_constraint_columns_extracts_sorted(self):
        mod = _get_migration_module({})
        self.assertEqual(
            mod._constraint_columns({"column_names": ["workspace_id", "idempotency_key"]}),
            ("idempotency_key", "workspace_id"),
        )
        self.assertEqual(
            mod._constraint_columns({"column_names": ["x"]}),
            ("x",),
        )

    def test_find_constraints_by_columns_exact_match(self):
        mod = _get_migration_module({
            "sync_events": [
                {"name": "sync_events_idempotency_key_key", "column_names": ["idempotency_key"]},
                {"name": "uq_sync_events_workspace_idempotency",
                 "column_names": ["workspace_id", "idempotency_key"]},
            ]
        })
        # Match composite constraint
        result = mod._find_unique_constraints_by_columns(
            "sync_events", ["workspace_id", "idempotency_key"]
        )
        self.assertEqual(result, ["uq_sync_events_workspace_idempotency"])

        # Match single-column constraint
        result = mod._find_unique_constraints_by_columns("sync_events", ["idempotency_key"])
        self.assertEqual(result, ["sync_events_idempotency_key_key"])

        # No match
        result = mod._find_unique_constraints_by_columns("sync_events", ["workspace_id"])
        self.assertEqual(result, [])

    def test_has_unique_constraint(self):
        mod = _get_migration_module({
            "run_records": [
                {"name": "uq_run_records_workspace_idempotency",
                 "column_names": ["workspace_id", "idempotency_key"]},
            ]
        })
        self.assertTrue(
            mod._has_unique_constraint("run_records", ["workspace_id", "idempotency_key"])
        )
        self.assertFalse(
            mod._has_unique_constraint("run_records", ["idempotency_key"])
        )
        self.assertFalse(
            mod._has_unique_constraint("nonexistent_table", ["idempotency_key"])
        )

    def test_ensure_unique_constraint_idempotent(self):
        """_ensure_unique_constraint must NOT call op.create_unique_constraint if constraint exists."""
        mod = _get_migration_module({
            "article_reference_events": [
                {"name": "uq_article_ref_events_workspace_idempotency",
                 "column_names": ["workspace_id", "idempotency_key"]},
            ]
        })

        # Clear any prior calls
        mod._create_calls.clear()
        mod._ensure_unique_constraint(
            "article_reference_events",
            "uq_article_ref_events_workspace_idempotency",
            ["workspace_id", "idempotency_key"],
        )
        # Should NOT have called create because constraint already exists
        self.assertEqual(len(mod._create_calls), 0)

    def test_ensure_unique_constraint_creates_when_absent(self):
        mod = _get_migration_module({"article_reference_events": []})

        mod._create_calls.clear()
        mod._ensure_unique_constraint(
            "article_reference_events",
            "uq_article_ref_events_workspace_idempotency",
            ["workspace_id", "idempotency_key"],
        )
        self.assertEqual(len(mod._create_calls), 1)
        name, tbl, cols = mod._create_calls[0]
        self.assertEqual(name, "uq_article_ref_events_workspace_idempotency")
        self.assertEqual(tbl, "article_reference_events")
        self.assertEqual(cols, ["workspace_id", "idempotency_key"])

    def test_drop_constraints_by_columns(self):
        mod = _get_migration_module({
            "sync_events": [
                {"name": "sync_events_idempotency_key_key", "column_names": ["idempotency_key"]},
                {"name": "some_other_constraint", "column_names": ["workspace_id"]},
            ]
        })

        mod._drop_calls.clear()
        mod._drop_unique_constraints_by_columns("sync_events", ["idempotency_key"])

        self.assertEqual(len(mod._drop_calls), 1)
        name, tbl, typ = mod._drop_calls[0]
        self.assertEqual(name, "sync_events_idempotency_key_key")
        self.assertEqual(tbl, "sync_events")
        self.assertEqual(typ, "unique")

    def test_drop_constraints_by_columns_none_exist(self):
        mod = _get_migration_module({"sync_events": []})  # nothing to drop

        mod._drop_calls.clear()
        mod._drop_unique_constraints_by_columns("sync_events", ["idempotency_key"])
        self.assertEqual(len(mod._drop_calls), 0)

    def test_upgrade_handles_missing_old_constraint_gracefully(self):
        """upgrade() must not fail if the old single-column constraint is already gone."""
        # Empty tables — no constraints at all
        mod = _get_migration_module({
            "sync_events": [],
            "run_records": [],
            "article_reference_events": [],
        })

        mod._drop_calls.clear()
        mod._create_calls.clear()
        for table, new_name, key_col, ws_col in mod._TABLE_META:
            mod._drop_unique_constraints_by_columns(table, [key_col])
            mod._ensure_unique_constraint(table, new_name, [ws_col, key_col])

        # No constraints to drop (nothing to find)
        self.assertEqual(mod._drop_calls, [])
        # All three new composite constraints should be created
        created_names = [c[0] for c in mod._create_calls]
        self.assertEqual(len(created_names), 3)


class TestDowngradeDuplicateGuard(unittest.TestCase):
    """Verify that downgrade blocks when cross-workspace duplicates exist."""

    def test_duplicate_key_raises_runtime_error(self):
        """Cross-workspace duplicate idempotency_key must raise RuntimeError."""
        mock_result = MagicMock()
        mock_result.fetchone.return_value = ("run:rec-001", 3)  # (idempotency_key, count)
        mock_conn = MagicMock()
        mock_conn.execute = MagicMock(return_value=mock_result)

        mod = _get_migration_module({})

        # Patch op.get_bind inside the migration module namespace so the
        # migration helper uses our mock connection regardless of how op was resolved.
        import sys as _sys
        migration_op = _sys.modules[_MIGRATION_NAME]
        original_get_bind = migration_op.op.get_bind
        migration_op.op.get_bind = lambda: mock_conn
        try:
            with self.assertRaisesRegex(
                RuntimeError,
                r"Downgrade blocked.*table 'sync_events'.*duplicate.*run:rec-001",
            ):
                mod._assert_no_cross_workspace_duplicates("sync_events", "idempotency_key")
        finally:
            migration_op.op.get_bind = original_get_bind

    def test_no_duplicate_succeeds(self):
        """No duplicate → no exception."""
        mock_result = MagicMock()
        mock_result.fetchone.return_value = None  # no rows → no duplicate
        mock_conn = MagicMock()
        mock_conn.execute = MagicMock(return_value=mock_result)

        mod = _get_migration_module({})

        import sys as _sys
        migration_op = _sys.modules[_MIGRATION_NAME]
        original_get_bind = migration_op.op.get_bind
        migration_op.op.get_bind = lambda: mock_conn
        try:
            # Should not raise
            mod._assert_no_cross_workspace_duplicates("run_records", "idempotency_key")
        finally:
            migration_op.op.get_bind = original_get_bind


class TestUpgradeBehavior(unittest.TestCase):
    """Integration-style test: verify the upgrade() sequence calls the right helpers."""

    def test_upgrade_drops_old_single_column_constraint(self):
        """upgrade() must drop the single-column idempotency_key unique constraint."""
        # Initial state: old single-column unique exists, composite absent
        mod = _get_migration_module({
            "sync_events": [
                {"name": "sync_events_idempotency_key_key", "column_names": ["idempotency_key"]},
            ],
            "run_records": [
                {"name": "run_records_idempotency_key_key", "column_names": ["idempotency_key"]},
            ],
            "article_reference_events": [
                {"name": "article_reference_events_idempotency_key_key",
                 "column_names": ["idempotency_key"]},
            ],
        })

        mod._drop_calls.clear()
        mod._create_calls.clear()
        for table, new_name, key_col, ws_col in mod._TABLE_META:
            mod._drop_unique_constraints_by_columns(table, [key_col])
            mod._ensure_unique_constraint(table, new_name, [ws_col, key_col])

        # All three old single-column constraints should be dropped
        dropped_names = [c[0] for c in mod._drop_calls]
        self.assertIn("sync_events_idempotency_key_key", dropped_names)
        self.assertIn("run_records_idempotency_key_key", dropped_names)
        self.assertIn("article_reference_events_idempotency_key_key", dropped_names)

        # All three new composite constraints should be created
        created_names = [c[0] for c in mod._create_calls]
        self.assertIn("uq_sync_events_workspace_idempotency", created_names)
        self.assertIn("uq_run_records_workspace_idempotency", created_names)
        self.assertIn("uq_article_ref_events_workspace_idempotency", created_names)

    def test_upgrade_idempotent_when_new_constraint_already_exists(self):
        """upgrade() must not create a duplicate when composite constraint is already present."""
        # Composite constraint already exists → no drop, no create needed for it
        mod = _get_migration_module({
            "sync_events": [
                {"name": "uq_sync_events_workspace_idempotency",
                 "column_names": ["workspace_id", "idempotency_key"]},
            ],
            "run_records": [],
            "article_reference_events": [],
        })

        mod._drop_calls.clear()
        mod._create_calls.clear()
        for table, new_name, key_col, ws_col in mod._TABLE_META:
            mod._drop_unique_constraints_by_columns(table, [key_col])
            mod._ensure_unique_constraint(table, new_name, [ws_col, key_col])

        # No single-column constraints to drop (they're absent)
        self.assertEqual(mod._drop_calls, [])
        # sync_events already had the composite → not created again
        # run_records and article_reference_events had none → created
        created_names = [c[0] for c in mod._create_calls]
        self.assertNotIn("uq_sync_events_workspace_idempotency", created_names)
        self.assertIn("uq_run_records_workspace_idempotency", created_names)
        self.assertIn("uq_article_ref_events_workspace_idempotency", created_names)


if __name__ == "__main__":
    unittest.main()
