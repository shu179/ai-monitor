"""Tests for the GitHub Actions constraint verification helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_verify_constraints_module():
    path = Path(__file__).parent / ".github" / "workflows" / "verify_constraints.py"
    spec = importlib.util.spec_from_file_location("verify_constraints", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeInspector:
    def __init__(self, constraints_by_table: dict[str, list[dict]]) -> None:
        self._constraints_by_table = constraints_by_table

    def get_unique_constraints(self, table_name: str) -> list[dict]:
        return list(self._constraints_by_table.get(table_name, []))


def _workspace_constraint(table: str, columns: list[str] | None = None) -> dict:
    return {
        "name": f"uq_{table}_workspace_idempotency",
        "column_names": columns or ["workspace_id", "idempotency_key"],
    }


def test_verify_constraints_accepts_workspace_scoped_constraint_in_any_order(capsys):
    module = _load_verify_constraints_module()
    inspector = FakeInspector(
        {
            table: [_workspace_constraint(table, ["idempotency_key", "workspace_id"])]
            for table in module.TABLES
        }
    )

    assert module.verify_constraints(inspector)
    captured = capsys.readouterr()
    assert "ERROR" not in captured.out


def test_verify_constraints_rejects_missing_workspace_scoped_constraint(capsys):
    module = _load_verify_constraints_module()
    inspector = FakeInspector(
        {
            table: []
            for table in module.TABLES
        }
    )

    assert not module.verify_constraints(inspector)
    captured = capsys.readouterr()
    assert "Missing workspace-scoped constraint" in captured.out


def test_verify_constraints_rejects_old_global_idempotency_constraint(capsys):
    module = _load_verify_constraints_module()
    inspector = FakeInspector(
        {
            table: [
                _workspace_constraint(table),
                {"name": f"{table}_idempotency_key_key", "column_names": ["idempotency_key"]},
            ]
            for table in module.TABLES
        }
    )

    assert not module.verify_constraints(inspector)
    captured = capsys.readouterr()
    assert "Old global constraint" in captured.out
