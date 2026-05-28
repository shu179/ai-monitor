from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate article payload text into article_versions/object storage.")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--once", action="store_true", help="Run one batch and exit.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument(
        "--max-active-txn",
        type=int,
        default=20,
        help="Pause the backfill when more than this many other DB backends are active.",
    )
    parser.add_argument(
        "--max-backoff-seconds",
        type=float,
        default=30.0,
        help="Upper bound on the adaptive throttle delay between batches.",
    )
    parser.add_argument("--no-throttle", action="store_true", help="Disable adaptive DB-load throttling.")
    args = parser.parse_args(argv)
    marker = os.environ.get("SURFACED_CLOUD_ALLOW_ARTICLE_MIGRATION", "").strip().lower()
    if marker not in {"1", "true", "yes"}:
        print("Refusing to run: set SURFACED_CLOUD_ALLOW_ARTICLE_MIGRATION=1.")
        return 2

    from app.db.session import SessionLocal
    from app.services.article_payload_migration_service import (
        build_article_payload_migration_report,
        compute_throttle_delay,
        measure_active_backends,
        migrate_article_payloads_once,
    )

    total = {"scanned": 0, "completed": 0, "skipped": 0, "failed": 0, "throttle_pauses": 0}
    while True:
        with SessionLocal() as db:
            stats = migrate_article_payloads_once(db, limit=args.limit)
        for key in ("scanned", "completed", "skipped", "failed"):
            total[key] += int(stats.get(key) or 0)
        if args.once or int(stats.get("scanned") or 0) <= 0:
            break
        if not args.no_throttle:
            with SessionLocal() as db:
                active_backends = measure_active_backends(db)
            delay = compute_throttle_delay(
                active_backends,
                max_active=args.max_active_txn,
                max_backoff_seconds=args.max_backoff_seconds,
            )
            if delay > 0:
                total["throttle_pauses"] += 1
                time.sleep(delay)
    with SessionLocal() as db:
        report = build_article_payload_migration_report(db)
    output = {"stats": total, "report": report}
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"Article payload migration stats={total} report={report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
