from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create monthly partitions and (optionally) adopt existing default-partition rows."
    )
    parser.add_argument("--months-back", type=int, default=1, help="How many past months to adopt from the default partition.")
    parser.add_argument("--months-ahead", type=int, default=3, help="How many future months to pre-create.")
    parser.add_argument(
        "--ensure-only",
        action="store_true",
        help="Only create future partitions (conflict-free, no DETACH); skip adoption.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)

    from app.db.session import SessionLocal
    from app.services.partition_service import adopt_default_partitions, ensure_future_partitions

    if args.ensure_only:
        with SessionLocal() as db:
            created = ensure_future_partitions(db, months_ahead=args.months_ahead)
            db.commit()
        output = {"mode": "ensure_only", "created": created, "adopted": {}}
    else:
        # Adoption detaches the default partition briefly, so it must run in a
        # low-traffic window. It is gated so it can't fire by accident.
        marker = os.environ.get("SURFACED_CLOUD_ALLOW_PARTITION_ADOPTION", "").strip().lower()
        if marker not in {"1", "true", "yes"}:
            print(
                "Refusing to adopt default-partition rows: set "
                "SURFACED_CLOUD_ALLOW_PARTITION_ADOPTION=1 (run in a low-traffic window), "
                "or pass --ensure-only for the safe create-ahead path."
            )
            return 2
        with SessionLocal() as db:
            try:
                adopted = adopt_default_partitions(db, months_back=args.months_back, months_ahead=0)
                created = ensure_future_partitions(db, months_ahead=args.months_ahead)
                db.commit()
            except Exception:
                db.rollback()
                raise
        output = {"mode": "adopt", "created": created, "adopted": adopted}

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"Partition maintenance {output['mode']}: created={output['created']} adopted={output['adopted']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
