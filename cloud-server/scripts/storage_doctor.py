from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect local object storage health.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero on warn as well as error.")
    args = parser.parse_args()
    from app.db.session import SessionLocal
    from app.services.object_storage_diagnostics import build_object_storage_report, format_object_storage_report

    with SessionLocal() as db:
        report = build_object_storage_report(db)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_object_storage_report(report))
    if report["status"] == "error":
        return 2
    if args.strict and report["status"] == "warn":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
