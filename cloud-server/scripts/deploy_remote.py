from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_REMOTE_DIR = "/opt/surfaced/cloud-server"
DEFAULT_BACKUP_DIR = "/opt/surfaced/backups"
DEFAULT_WAL_ARCHIVE_DIR = "/opt/surfaced/postgres-wal"
DEFAULT_HEALTH_URL = "http://127.0.0.1:8080"
RSYNC_EXCLUDES = (
    ".env",
    ".venv",
    "__pycache__/",
    ".pytest_cache/",
    ".DS_Store",
    "._*",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy the cloud-server directory to a remote host over SSH.")
    parser.add_argument(
        "--ssh-host",
        default=os.environ.get("SURFACED_CLOUD_DEPLOY_SSH_HOST", "").strip(),
        help="SSH host or config alias, e.g. tencent.",
    )
    parser.add_argument(
        "--remote-dir",
        default=os.environ.get("SURFACED_CLOUD_DEPLOY_REMOTE_DIR", DEFAULT_REMOTE_DIR),
        help="Remote cloud-server directory.",
    )
    parser.add_argument(
        "--backup-dir",
        default=os.environ.get("SURFACED_CLOUD_DEPLOY_BACKUP_DIR", DEFAULT_BACKUP_DIR),
        help="Remote backup directory.",
    )
    parser.add_argument(
        "--wal-archive-dir",
        default=os.environ.get("SURFACED_CLOUD_DEPLOY_WAL_ARCHIVE_DIR", DEFAULT_WAL_ARCHIVE_DIR),
        help="Remote PostgreSQL WAL archive directory.",
    )
    parser.add_argument(
        "--health-url",
        default=os.environ.get("SURFACED_CLOUD_DEPLOY_HEALTH_URL", DEFAULT_HEALTH_URL),
        help="Remote health-check base URL.",
    )
    parser.add_argument("--skip-db-backup", action="store_true", help="Skip pg_dump backup before deploy.")
    parser.add_argument("--skip-code-backup", action="store_true", help="Skip remote code tarball backup before deploy.")
    parser.add_argument("--skip-rebuild", action="store_true", help="Skip docker compose up -d --build.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands instead of executing them.")
    args = parser.parse_args(argv)

    if not str(args.ssh_host or "").strip():
        parser.error("--ssh-host is required (or set SURFACED_CLOUD_DEPLOY_SSH_HOST)")

    rsync_command = build_rsync_command(ssh_host=args.ssh_host, remote_dir=args.remote_dir)
    remote_script = render_remote_script(
        remote_dir=args.remote_dir,
        backup_dir=args.backup_dir,
        wal_archive_dir=args.wal_archive_dir,
        health_url=args.health_url,
        db_backup=not bool(args.skip_db_backup),
        code_backup=not bool(args.skip_code_backup),
        rebuild=not bool(args.skip_rebuild),
    )
    ssh_command = ["ssh", args.ssh_host, "bash", "-s"]

    if args.dry_run:
        print("rsync:", shell_join(rsync_command))
        print("ssh:", shell_join(ssh_command))
        print(remote_script)
        return 0

    run(rsync_command)
    run(ssh_command, input_text=remote_script)
    return 0


def build_rsync_command(*, ssh_host: str, remote_dir: str) -> list[str]:
    command = ["rsync", "-az", "--delete"]
    for pattern in RSYNC_EXCLUDES:
        command.extend(["--exclude", pattern])
    command.extend([f"{ROOT}/", f"{ssh_host}:{remote_dir}/"])
    return command


def render_remote_script(
    *,
    remote_dir: str,
    backup_dir: str,
    wal_archive_dir: str = DEFAULT_WAL_ARCHIVE_DIR,
    health_url: str,
    db_backup: bool,
    code_backup: bool,
    rebuild: bool,
) -> str:
    remote_q = shlex.quote(str(remote_dir))
    backup_q = shlex.quote(str(backup_dir))
    wal_archive_q = shlex.quote(str(wal_archive_dir))
    health_q = shlex.quote(str(health_url))
    lines = [
        "set -euo pipefail",
        f"REMOTE_DIR={remote_q}",
        f"BACKUP_DIR={backup_q}",
        f"WAL_ARCHIVE_DIR={wal_archive_q}",
        f"HEALTH_URL={health_q}",
        "TS=$(date +%Y%m%d-%H%M%S)",
        'echo "remote_dir=$REMOTE_DIR"',
        'echo "backup_dir=$BACKUP_DIR"',
        'sudo mkdir -p "$BACKUP_DIR"',
        'sudo install -d -m 700 -o 70 -g 70 "$WAL_ARCHIVE_DIR"',
        'find "$REMOTE_DIR" \\( -name "._*" -o -name ".DS_Store" \\) -delete',
    ]
    if db_backup:
        lines.extend(
            [
                'echo "step=db_backup"',
                'cd "$REMOTE_DIR"',
                'sudo docker compose exec -T postgres sh -lc \'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"\' '
                '| gzip | sudo tee "$BACKUP_DIR/surfaced_cloud-${TS}.sql.gz" >/dev/null',
                'ls -lh "$BACKUP_DIR/surfaced_cloud-${TS}.sql.gz"',
            ]
        )
    if code_backup:
        lines.extend(
            [
                'echo "step=code_backup"',
                'PARENT_DIR=$(dirname "$REMOTE_DIR")',
                'BASENAME=$(basename "$REMOTE_DIR")',
                'sudo tar -czf "$BACKUP_DIR/cloud-server-code-${TS}.tar.gz" -C "$PARENT_DIR" "$BASENAME"',
                'ls -lh "$BACKUP_DIR/cloud-server-code-${TS}.tar.gz"',
            ]
        )
    if rebuild:
        lines.extend(
            [
                'echo "step=rebuild"',
                'cd "$REMOTE_DIR"',
                "sudo docker compose up -d --build",
            ]
        )
    lines.extend(
        [
            'echo "step=deploy_check"',
            'cd "$REMOTE_DIR"',
            'python3 -m scripts.deploy_check --base-url "$HEALTH_URL"',
        ]
    )
    return "\n".join(lines) + "\n"


def run(command: list[str], *, input_text: str | None = None) -> None:
    subprocess.run(
        command,
        input=input_text,
        text=input_text is not None,
        check=True,
    )


def shell_join(command: list[str]) -> str:
    return " ".join(shlex.quote(str(part)) for part in command)


if __name__ == "__main__":
    raise SystemExit(main())
