from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import partition_service as ps  # noqa: E402


class PartitionHelperTests(unittest.TestCase):
    def test_month_floor(self) -> None:
        self.assertEqual(ps.month_floor(datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)), date(2026, 5, 1))

    def test_add_months_rollover(self) -> None:
        self.assertEqual(ps.add_months(date(2026, 5, 1), 1), date(2026, 6, 1))
        self.assertEqual(ps.add_months(date(2026, 12, 1), 1), date(2027, 1, 1))
        self.assertEqual(ps.add_months(date(2026, 1, 1), -1), date(2025, 12, 1))
        self.assertEqual(ps.add_months(date(2026, 3, 1), 10), date(2027, 1, 1))

    def test_partition_name_and_suffix(self) -> None:
        self.assertEqual(ps.partition_suffix(date(2026, 5, 1)), "p202605")
        self.assertEqual(
            ps.partition_name("workspace_change_log", date(2026, 5, 1)),
            "workspace_change_log_p202605",
        )

    def test_month_bounds_are_half_open(self) -> None:
        self.assertEqual(ps.month_bounds(date(2026, 5, 15)), (date(2026, 5, 1), date(2026, 6, 1)))
        self.assertEqual(ps.month_bounds(date(2026, 12, 1)), (date(2026, 12, 1), date(2027, 1, 1)))

    def test_future_months_includes_current(self) -> None:
        now = datetime(2026, 5, 10, tzinfo=timezone.utc)
        self.assertEqual(
            ps.future_months(now, 3),
            [date(2026, 5, 1), date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1)],
        )
        self.assertEqual(ps.future_months(now, 0), [date(2026, 5, 1)])

    def test_window_months_spans_back_and_ahead(self) -> None:
        now = datetime(2026, 1, 10, tzinfo=timezone.utc)
        self.assertEqual(
            ps.window_months(now, months_back=2, months_ahead=1),
            [date(2025, 11, 1), date(2025, 12, 1), date(2026, 1, 1), date(2026, 2, 1)],
        )

    def test_partition_month_from_name(self) -> None:
        self.assertEqual(
            ps.partition_month_from_name("workspace_change_log", "workspace_change_log_p202605"),
            date(2026, 5, 1),
        )
        # the default partition and other parents' names must not parse
        self.assertIsNone(ps.partition_month_from_name("workspace_change_log", "workspace_change_log_default"))
        self.assertIsNone(ps.partition_month_from_name("workspace_change_log", "sync_batch_items_p202605"))
        # an out-of-range month must be rejected, not silently dropped later
        self.assertIsNone(ps.partition_month_from_name("workspace_change_log", "workspace_change_log_p209913"))

    def test_registry_indexes_mirror_migration_defaults(self) -> None:
        # Guard: new monthly partitions re-declare these indexes, so the registry
        # must stay in sync with the *_default indexes from migration 0011 —
        # otherwise new partitions silently lose e.g. the worker claim index.
        by_parent = {table.parent: table for table in ps.PARTITIONED_TABLES}
        self.assertEqual(
            {suffix for suffix, _ in by_parent["sync_batch_items"].indexes},
            {"claim", "workspace_status", "partition_order"},
        )
        self.assertEqual({suffix for suffix, _ in by_parent["agent_commands"].indexes}, {"dispatch"})
        self.assertEqual({suffix for suffix, _ in by_parent["workspace_change_log"].indexes}, {"lookup"})


if __name__ == "__main__":
    unittest.main()
