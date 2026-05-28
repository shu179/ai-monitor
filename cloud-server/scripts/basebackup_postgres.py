from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backup_postgres import DEFAULT_BACKUP_DIR, parse_database_url  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a PostgreSQL base backup for PITR.")
    parser.add_argument("--output-dir", default=os.environ.get("SURFACED_CLOUD_BACKUP_DIR", str(DEFAULT_BACKUP_DIR)))
    parser.add_argument(
        "--retain-days",
        type=int,
        default=int(os.environ.get("SURFACED_CLOUD_PITR_BASEBACKUP_RETAIN_DAYS", "7") or 7),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    marker = os.environ.get("SURFACED_CLOUD_ALLOW_BASEBACKUP", "").strip().lower()
    if marker not in {"1", "true", "yes"} and not args.dry_run:
        print("Refusing to run: set SURFACED_CLOUD_ALLOW_BASEBACKUP=1 or use --dry-run.")
        return 2

    from app.core.config import get_settings

    settings = get_settings()
    spec = parse_database_url(settings.database_url)
    output_dir = Path(args.output_dir).expanduser().resolve()
    plan = build_basebackup_plan(output_dir=output_dir, database=spec["database"])
    if args.dry_run:
        print(f"basebackup_plan output={plan['output_path']} retain_days={args.retain_days}")
        return 0

    target = Path(plan["output_path"])
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise RuntimeError(f"basebackup target is not empty: {target}")
    target.mkdir(parents=True, exist_ok=True)
    run_pg_basebackup(spec, target)
    removed = prune_basebackups(output_dir, retain_days=max(1, int(args.retain_days or 7)))
    print(f"basebackup_complete path={target} pruned={removed}")
    return 0


def build_basebackup_plan(*, output_dir: Path, database: str, now: datetime | None = None) -> dict[str, str]:
    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    target = output_dir / f"basebackup-{database}-{timestamp}"
    return {"output_path": str(target)}


def run_pg_basebackup(spec: dict[str, str], output_dir: Path) -> None:
    env = os.environ.copy()
    if spec.get("password"):
        env["PGPASSWORD"] = spec["password"]
    command = [
        "pg_basebackup",
        "--host",
        spec["host"],
        "--port",
        spec["port"],
        "--username",
        spec["user"],
        "--pgdata",
        str(output_dir),
        "--format=tar",
        "--gzip",
        "--wal-method=stream",
        "--checkpoint=fast",
        "--progress",
    ]
    subprocess.run(command, check=True, env=env)


def prune_basebackups(output_dir: Path, *, retain_days: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(retain_days or 1)))
    removed = 0
    for path in output_dir.glob("basebackup-*"):
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except FileNotFoundError:
            continue
        if mtime >= cutoff:
            continue
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        removed += 1
    return removed


if __name__ == "__main__":
    raise SystemExit(main())
