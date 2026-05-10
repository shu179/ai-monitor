#!/usr/bin/env python3
"""Verify unique constraints for idempotency migration."""
import os
from sqlalchemy import create_engine, inspect

DATABASE_URL = os.environ["SURFACED_CLOUD_DATABASE_URL"]
engine = create_engine(DATABASE_URL)
inspector = inspect(engine)

expected_new = {("workspace_id", "idempotency_key")}
expected_old = {("idempotency_key",)}

tables = ["sync_events", "run_records", "article_reference_events"]
all_passed = True

for table in tables:
    constraints = inspector.get_unique_constraints(table)
    print(f"Table {table}:")
    for c in constraints:
        print(f'  {c["name"]}: {c["column_names"]}')

    has_new = any(
        tuple(sorted(c["column_names"])) == expected_new
        for c in constraints
    )
    has_old = any(
        tuple(sorted(c["column_names"])) == expected_old
        for c in constraints
    )

    if not has_new:
        print("  ERROR: Missing workspace-scoped constraint (workspace_id, idempotency_key)")
        all_passed = False
    if has_old:
        print("  ERROR: Old global constraint (idempotency_key) still exists")
        all_passed = False
    print()

if not all_passed:
    raise Exception("Constraint verification failed")
print("All constraints verified!")
