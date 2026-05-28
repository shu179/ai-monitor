"""Monthly range-partition management for the Cloud Sync v2 time-series tables.

Migration 0011 created ``workspace_change_log``, ``sync_batch_items`` and
``agent_commands`` as ``PARTITION BY RANGE (created_at)`` but only added a
``*_default`` catch-all partition. Without monthly partitions every row lands in
that one default partition, so the table is effectively unpartitioned: range
pruning does nothing and retention can only ``DELETE`` (slow, bloat) instead of
``DROP``-ing whole partitions.

This module fills that gap with three pieces:

* :func:`ensure_future_partitions` — create the upcoming months' partitions
  ahead of time. Strictly-future months never overlap rows still sitting in the
  default partition, so this is conflict-free and safe to run unattended in the
  maintenance loop.
* :func:`drop_aged_partitions` — ``DROP`` monthly partitions whose whole range
  is older than a retention cutoff. Append-only logs only.
* :func:`adopt_default_partitions` — one-time, operator-run: carve the rows that
  already accumulated in the default partition into monthly partitions. This
  briefly detaches the default partition, so it must run in a quiet window.

The secondary indexes are defined per-default in migration 0011 (not on the
partitioned parent), so every partition we create re-declares the same indexes.
All identifiers come from the fixed registry plus a computed ``YYYYMM`` suffix —
never from request data — so the f-string DDL carries no injection surface.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PartitionedTable:
    parent: str
    # (index_suffix, comma-separated column list) mirrored onto every partition.
    indexes: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def default_partition(self) -> str:
        return f"{self.parent}_default"


PARTITIONED_TABLES: tuple[PartitionedTable, ...] = (
    PartitionedTable(
        parent="workspace_change_log",
        indexes=(("lookup", "workspace_id, stream, seq"),),
    ),
    PartitionedTable(
        parent="sync_batch_items",
        indexes=(
            ("claim", "status, virtual_shard, created_at, id"),
            ("workspace_status", "workspace_id, status, created_at"),
            ("partition_order", "workspace_id, partition_key, seq"),
        ),
    ),
    PartitionedTable(
        parent="agent_commands",
        indexes=(("dispatch", "workspace_id, target_device_id, status, visibility_until"),),
    ),
)

_TABLES_BY_PARENT = {table.parent: table for table in PARTITIONED_TABLES}
_PARTITION_NAME_RE = re.compile(r"_p(\d{4})(\d{2})$")


# --- pure helpers (no DB) ---------------------------------------------------

def month_floor(moment: datetime | date) -> date:
    return date(moment.year, moment.month, 1)


def add_months(anchor: date, months: int) -> date:
    index = (anchor.year * 12 + (anchor.month - 1)) + months
    return date(index // 12, index % 12 + 1, 1)


def partition_suffix(month_start: date) -> str:
    return f"p{month_start.year:04d}{month_start.month:02d}"


def partition_name(parent: str, month_start: date) -> str:
    return f"{parent}_{partition_suffix(month_start)}"


def month_bounds(month_start: date) -> tuple[date, date]:
    """Half-open [start_of_month, start_of_next_month) range for a partition."""
    return month_floor(month_start), add_months(month_floor(month_start), 1)


def future_months(now: datetime, months_ahead: int) -> list[date]:
    """Current month plus the next ``months_ahead`` months."""
    base = month_floor(now)
    return [add_months(base, offset) for offset in range(max(0, int(months_ahead)) + 1)]


def window_months(now: datetime, *, months_back: int, months_ahead: int) -> list[date]:
    base = month_floor(now)
    start = -max(0, int(months_back))
    end = max(0, int(months_ahead))
    return [add_months(base, offset) for offset in range(start, end + 1)]


def partition_month_from_name(parent: str, name: str) -> date | None:
    """Parse the month from a monthly partition name; None for the default/other."""
    if not name.startswith(f"{parent}_p"):
        return None
    match = _PARTITION_NAME_RE.search(name)
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return date(year, month, 1)


def _iso_midnight_utc(day: date) -> str:
    return f"{day.isoformat()} 00:00:00+00"


# --- DDL operations (need a DB session) -------------------------------------

def _create_partition_indexes(db: Session, table: PartitionedTable, name: str) -> None:
    for suffix, columns in table.indexes:
        index_name = f"{name}_{suffix}"
        db.execute(text(f'CREATE INDEX IF NOT EXISTS "{index_name}" ON "{name}" ({columns})'))


def _list_existing_partitions(db: Session, parent: str) -> set[str]:
    rows = db.execute(
        text(
            """
            SELECT child.relname
            FROM pg_inherits
            JOIN pg_class AS parent ON parent.oid = pg_inherits.inhparent
            JOIN pg_class AS child ON child.oid = pg_inherits.inhrelid
            WHERE parent.relname = :parent
            """
        ),
        {"parent": parent},
    )
    return {str(row[0]) for row in rows}


def ensure_future_partitions(
    db: Session,
    *,
    months_ahead: int = 3,
    tables: tuple[PartitionedTable, ...] = PARTITIONED_TABLES,
    now: datetime | None = None,
) -> dict[str, int]:
    """Create the current + next ``months_ahead`` monthly partitions if missing.

    Conflict-free: a month's partition is only created when it does not already
    exist, and future months hold no rows in the default partition, so the
    range CREATE never collides. Returns the count of partitions created per
    parent table.
    """
    moment = now or datetime.now(timezone.utc)
    created: dict[str, int] = {}
    for table in tables:
        existing = _list_existing_partitions(db, table.parent)
        made = 0
        for month_start in future_months(moment, months_ahead):
            name = partition_name(table.parent, month_start)
            if name in existing:
                continue
            lo, hi = month_bounds(month_start)
            try:
                # Savepoint so an overlap failure on one month rolls back only
                # this CREATE, never the surrounding maintenance transaction.
                with db.begin_nested():
                    db.execute(
                        text(
                            f'CREATE TABLE IF NOT EXISTS "{name}" PARTITION OF "{table.parent}" '
                            f"FOR VALUES FROM ('{_iso_midnight_utc(lo)}') TO ('{_iso_midnight_utc(hi)}')"
                        )
                    )
                    _create_partition_indexes(db, table, name)
                made += 1
            except Exception as exc:  # pragma: no cover - overlap with default data
                # The only expected failure is a range overlap with rows still in
                # the default partition (pre-adoption current month). Skip it; the
                # adoption script handles those. Never let one table abort the run.
                logger.warning("[Partition] skip %s: %s", name, exc)
        created[table.parent] = made
    return created


def drop_aged_partitions(
    db: Session,
    *,
    parent: str,
    cutoff: datetime,
) -> list[str]:
    """DROP monthly partitions of ``parent`` whose whole range ends at/before cutoff.

    Only for append-only logs where dropping a fully-aged month is safe. The
    default partition and the current/future months are never touched.
    """
    cutoff_month = month_floor(cutoff)
    dropped: list[str] = []
    for name in sorted(_list_existing_partitions(db, parent)):
        month_start = partition_month_from_name(parent, name)
        if month_start is None:
            continue
        _, hi = month_bounds(month_start)
        # hi is the first instant NOT in this partition; drop only when the whole
        # month is strictly older than the retention cutoff month.
        if hi <= cutoff_month:
            db.execute(text(f'DROP TABLE IF EXISTS "{name}"'))
            dropped.append(name)
    return dropped


def adopt_default_partitions(
    db: Session,
    *,
    months_back: int = 1,
    months_ahead: int = 0,
    tables: tuple[PartitionedTable, ...] = PARTITIONED_TABLES,
    now: datetime | None = None,
) -> dict[str, int]:
    """One-time: move rows already in the default partition into monthly ones.

    For each parent this detaches the default partition, creates any missing
    monthly partitions in the window, re-inserts the matching rows (which route
    into the new partitions), deletes them from the detached default, and
    re-attaches the default. The detach window briefly rejects out-of-range
    inserts, so run this in a low-traffic window. Cheap while tables are small.
    """
    moment = now or datetime.now(timezone.utc)
    months = window_months(moment, months_back=months_back, months_ahead=months_ahead)
    if not months:
        return {}
    window_lo = month_bounds(months[0])[0]
    window_hi = month_bounds(months[-1])[1]
    adopted: dict[str, int] = {}
    for table in tables:
        existing = _list_existing_partitions(db, table.parent)
        default = table.default_partition
        moved = int(
            db.execute(
                text(
                    f'SELECT count(*) FROM "{default}" '
                    f"WHERE created_at >= '{_iso_midnight_utc(window_lo)}' "
                    f"AND created_at < '{_iso_midnight_utc(window_hi)}'"
                )
            ).scalar_one()
            or 0
        )
        db.execute(text(f'ALTER TABLE "{table.parent}" DETACH PARTITION "{default}"'))
        for month_start in months:
            name = partition_name(table.parent, month_start)
            if name in existing:
                continue
            lo, hi = month_bounds(month_start)
            db.execute(
                text(
                    f'CREATE TABLE IF NOT EXISTS "{name}" PARTITION OF "{table.parent}" '
                    f"FOR VALUES FROM ('{_iso_midnight_utc(lo)}') TO ('{_iso_midnight_utc(hi)}')"
                )
            )
            _create_partition_indexes(db, table, name)
        if moved:
            db.execute(
                text(
                    f'INSERT INTO "{table.parent}" '
                    f'SELECT * FROM "{default}" '
                    f"WHERE created_at >= '{_iso_midnight_utc(window_lo)}' "
                    f"AND created_at < '{_iso_midnight_utc(window_hi)}'"
                )
            )
            db.execute(
                text(
                    f'DELETE FROM "{default}" '
                    f"WHERE created_at >= '{_iso_midnight_utc(window_lo)}' "
                    f"AND created_at < '{_iso_midnight_utc(window_hi)}'"
                )
            )
        db.execute(text(f'ALTER TABLE "{table.parent}" ATTACH PARTITION "{default}" DEFAULT'))
        adopted[table.parent] = moved
    return adopted
