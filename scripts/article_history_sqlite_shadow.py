#!/usr/bin/env python3
"""Rebuild or verify the article/history SQLite shadow store."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.article_history_sqlite_mirror import rebuild_shadow_store, verify_shadow_store


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Maintain the structured SQLite shadow copy of article/history JSON data.",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db-path",
        default=None,
        help="SQLite database path. Defaults to the account-scoped shadow DB under logs/.",
    )
    common.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Maximum parallel workers for reading history JSON files.",
    )
    common.add_argument(
        "--tail-limit",
        type=int,
        default=20,
        help="Number of tail records per history file to compare during verification.",
    )
    common.add_argument(
        "--compact",
        action="store_true",
        help="Print compact JSON instead of indented JSON.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "rebuild",
        parents=[common],
        help="Rebuild the shadow SQLite DB from JSON and verify it.",
    )
    subparsers.add_parser(
        "verify",
        parents=[common],
        help="Verify an existing shadow SQLite DB against JSON.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "rebuild":
        result = rebuild_shadow_store(
            args.db_path,
            max_workers=args.workers,
            verify_tail_limit=args.tail_limit,
        )
        ok = bool(result.get("verification", {}).get("ok"))
    else:
        result = verify_shadow_store(
            args.db_path,
            max_workers=args.workers,
            tail_limit=args.tail_limit,
        )
        ok = bool(result.get("ok"))

    print(_to_json(result, compact=bool(args.compact)))
    return 0 if ok else 1


def _to_json(value: dict[str, Any], *, compact: bool) -> str:
    if compact:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


if __name__ == "__main__":
    raise SystemExit(main())
