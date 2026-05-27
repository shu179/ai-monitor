from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run read-only cloud sync shadow reconciliation checks.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero on warning.")
    parser.add_argument("--sample-limit", type=int, default=20)
    args = parser.parse_args()
    from app.db.session import SessionLocal
    from app.services.shadow_reconcile_service import build_shadow_reconcile_report, format_shadow_reconcile_report

    with SessionLocal() as db:
        report = build_shadow_reconcile_report(db, sample_limit=args.sample_limit)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_shadow_reconcile_report(report))
    if args.strict and report["status"] != "ok":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
