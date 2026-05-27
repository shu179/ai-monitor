from __future__ import annotations

import argparse
import gzip
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_BACKUP_DIR = Path("/opt/surfaced/backups")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a local pg_dump backup for Surfaced Cloud.")
    parser.add_argument("--output-dir", default=str(DEFAULT_BACKUP_DIR))
    parser.add_argument("--retain-days", type=int, default=7)
    parser.add_argument("--no-gzip", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    marker = os.environ.get("SURFACED_CLOUD_ALLOW_BACKUP", "").strip().lower()
    if marker not in {"1", "true", "yes"} and not args.dry_run:
        print("Refusing to run: set SURFACED_CLOUD_ALLOW_BACKUP=1 or use --dry-run.")
        return 2

    from app.core.config import get_settings

    settings = get_settings()
    spec = parse_database_url(settings.database_url)
    output_dir = Path(args.output_dir).expanduser().resolve()
    plan = build_backup_plan(output_dir=output_dir, database=spec["database"], gzip_enabled=not args.no_gzip)
    if args.dry_run:
        print(f"backup_plan output={plan['output_path']} retain_days={args.retain_days} gzip={not args.no_gzip}")
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    dump_path = Path(plan["dump_path"])
    final_path = Path(plan["output_path"])
    run_pg_dump(spec, dump_path)
    if not args.no_gzip:
        gzip_file(dump_path, final_path)
        dump_path.unlink(missing_ok=True)
    removed = prune_backups(output_dir, retain_days=max(1, int(args.retain_days or 7)))
    print(f"backup_complete path={final_path} bytes={final_path.stat().st_size} pruned={removed}")
    return 0


def parse_database_url(database_url: str) -> dict[str, str]:
    normalized = str(database_url or "").replace("postgresql+psycopg://", "postgresql://", 1)
    parsed = urlparse(normalized)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("SURFACED_CLOUD_DATABASE_URL must be a PostgreSQL URL")
    database = unquote(parsed.path.lstrip("/"))
    if not database:
        raise ValueError("database name is required")
    return {
        "host": parsed.hostname or "localhost",
        "port": str(parsed.port or 5432),
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": database,
    }


def build_backup_plan(*, output_dir: Path, database: str, gzip_enabled: bool, now: datetime | None = None) -> dict[str, str]:
    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    base = f"{database}-{timestamp}.sql"
    dump_path = output_dir / base
    output_path = dump_path.with_suffix(dump_path.suffix + ".gz") if gzip_enabled else dump_path
    return {"dump_path": str(dump_path), "output_path": str(output_path)}


def run_pg_dump(spec: dict[str, str], output_path: Path) -> None:
    env = os.environ.copy()
    if spec.get("password"):
        env["PGPASSWORD"] = spec["password"]
    command = [
        "pg_dump",
        "--format=plain",
        "--no-owner",
        "--no-acl",
        "--host",
        spec["host"],
        "--port",
        spec["port"],
        "--username",
        spec["user"],
        "--file",
        str(output_path),
        spec["database"],
    ]
    subprocess.run(command, check=True, env=env)


def gzip_file(source: Path, destination: Path) -> None:
    with source.open("rb") as src, gzip.open(destination, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst)


def prune_backups(output_dir: Path, *, retain_days: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(retain_days or 1)))
    removed = 0
    for path in output_dir.glob("*.sql*"):
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except FileNotFoundError:
            continue
        if mtime >= cutoff:
            continue
        path.unlink(missing_ok=True)
        removed += 1
    return removed


if __name__ == "__main__":
    raise SystemExit(main())
