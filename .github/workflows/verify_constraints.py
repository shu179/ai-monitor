#!/usr/bin/env python3
"""Verify unique constraints for the workspace-scoped idempotency migration."""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence

from sqlalchemy import create_engine, inspect

TABLES = ("sync_events", "run_records", "article_reference_events")
WORKSPACE_SCOPED_COLUMNS = frozenset(("workspace_id", "idempotency_key"))
GLOBAL_IDEMPOTENCY_COLUMNS = frozenset(("idempotency_key",))


def _constraint_columns(constraint: dict) -> frozenset[str]:
    return frozenset(constraint.get("column_names") or ())


def _has_unique_constraint(
    constraints: Iterable[dict],
    expected_columns: frozenset[str],
) -> bool:
    return any(_constraint_columns(constraint) == expected_columns for constraint in constraints)


def verify_constraints(db_inspector, tables: Sequence[str] = TABLES) -> bool:
    """Return True only when every table has the new constraint and not the old one."""
    all_passed = True

    for table in tables:
        constraints = db_inspector.get_unique_constraints(table)
        print(f"Table {table}:")
        for constraint in constraints:
            print(f'  {constraint["name"]}: {constraint["column_names"]}')

        has_new = _has_unique_constraint(constraints, WORKSPACE_SCOPED_COLUMNS)
        has_old = _has_unique_constraint(constraints, GLOBAL_IDEMPOTENCY_COLUMNS)

        if not has_new:
            print("  ERROR: Missing workspace-scoped constraint (workspace_id, idempotency_key)")
            all_passed = False
        if has_old:
            print("  ERROR: Old global constraint (idempotency_key) still exists")
            all_passed = False
        print()

    return all_passed


def main() -> int:
    database_url = os.environ["SURFACED_CLOUD_DATABASE_URL"]
    engine = create_engine(database_url)
    db_inspector = inspect(engine)

    if not verify_constraints(db_inspector):
        raise RuntimeError("Constraint verification failed")

    print("All constraints verified!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
