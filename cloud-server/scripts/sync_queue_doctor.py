from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect Cloud Sync v2 queue health.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero on warning.")
    args = parser.parse_args()
    from app.db.session import SessionLocal
    from app.services.sync_queue_diagnostics import build_sync_queue_report, format_sync_queue_report

    with SessionLocal() as db:
        report = build_sync_queue_report(db)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_sync_queue_report(report))
    if args.strict and report["status"] != "ok":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
