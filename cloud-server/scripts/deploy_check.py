from __future__ import annotations

import argparse
import json
import os
import shutil
import stat as stat_module
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_TOTAL_OBJECT_QUOTA_BYTES = 10 * 1024 * 1024 * 1024
DEFAULT_WORKSPACE_OBJECT_QUOTA_BYTES = 5 * 1024 * 1024 * 1024
DEFAULT_MAX_OBJECT_FILE_BYTES = 512 * 1024 * 1024
DEFAULT_MIN_OBJECT_FREE_BYTES = 8 * 1024 * 1024 * 1024


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run preflight checks for a Surfaced Cloud deployment.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero on warnings as well as errors.")
    args = parser.parse_args(argv)
    report = build_deploy_check_report(base_url=args.base_url)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_deploy_check_report(report))
    if report["status"] == "error":
        return 2
    if args.strict and report["status"] == "warn":
        return 1
    return 0


def build_deploy_check_report(*, base_url: str = "http://127.0.0.1:8080") -> dict:
    env_path = ROOT / ".env"
    env_values = parse_env_file(env_path)
    required_env = [
        "SURFACED_CLOUD_SECRET_KEY",
        "SURFACED_CLOUD_DATABASE_URL",
        "POSTGRES_PASSWORD",
    ]
    env_missing = [key for key in required_env if not env_values.get(key)]
    weak_secrets = [
        key
        for key in ("SURFACED_CLOUD_SECRET_KEY", "POSTGRES_PASSWORD")
        if str(env_values.get(key) or "").strip() in {"", "change-this-before-deploy", "surfaced-password"}
    ]
    object_dir = Path(env_values.get("SURFACED_CLOUD_OBJECT_STORAGE_LOCAL_DIR") or "/opt/surfaced/object-data")
    backup_dir = Path(env_values.get("SURFACED_CLOUD_BACKUP_DIR") or "/opt/surfaced/backups")
    wal_archive_dir = Path(env_values.get("SURFACED_CLOUD_PITR_WAL_ARCHIVE_DIR") or "/opt/surfaced/postgres-wal")
    object_limits = {
        "total_quota_bytes": _safe_int(
            env_values.get("SURFACED_CLOUD_OBJECT_STORAGE_TOTAL_QUOTA_BYTES"),
            default=DEFAULT_TOTAL_OBJECT_QUOTA_BYTES,
        ),
        "workspace_quota_bytes": _safe_int(
            env_values.get("SURFACED_CLOUD_OBJECT_STORAGE_WORKSPACE_QUOTA_BYTES"),
            default=DEFAULT_WORKSPACE_OBJECT_QUOTA_BYTES,
        ),
        "max_file_bytes": _safe_int(
            env_values.get("SURFACED_CLOUD_OBJECT_STORAGE_MAX_FILE_BYTES"),
            default=DEFAULT_MAX_OBJECT_FILE_BYTES,
        ),
        "min_free_bytes": _safe_int(
            env_values.get("SURFACED_CLOUD_OBJECT_STORAGE_MIN_FREE_BYTES"),
            default=DEFAULT_MIN_OBJECT_FREE_BYTES,
        ),
    }
    dirs = {
        "object_storage": _object_storage_dir_report(object_dir, limits=object_limits),
        "backups": _dir_report(backup_dir, min_free_bytes=0),
        "wal_archive": _wal_archive_dir_report(wal_archive_dir),
    }
    compose = _compose_report()
    postgres_observability = _postgres_observability_report(env_values)
    health = _health_report(base_url)
    checks = {
        "env_file_exists": env_path.exists(),
        "env_missing": env_missing,
        "weak_secrets": weak_secrets,
        "directories": dirs,
        "compose": compose,
        "postgres_observability": postgres_observability,
        "health": health,
    }
    status = _status(checks)
    return {"status": status, **checks}


def format_deploy_check_report(report: dict) -> str:
    lines = [f"Deploy check: status={report['status']}"]
    lines.append(f"env_file_exists={report['env_file_exists']}")
    lines.append(f"env_missing={','.join(report['env_missing']) or 'none'}")
    lines.append(f"weak_secrets={','.join(report['weak_secrets']) or 'none'}")
    for name, item in report["directories"].items():
        lines.append(
            f"{name}_dir path={item['path']} exists={item['exists']} "
            f"writable={item.get('writable', False)} total_bytes={item.get('total_bytes', 0)} "
            f"free_bytes={item['free_bytes']} min_free_bytes={item['min_free_bytes']} status={item['status']}"
        )
        if name == "object_storage":
            limits = item.get("limits", {})
            lines.append(
                "object_storage_limits "
                f"total_quota_bytes={limits.get('total_quota_bytes', 0)} "
                f"workspace_quota_bytes={limits.get('workspace_quota_bytes', 0)} "
                f"max_file_bytes={limits.get('max_file_bytes', 0)} "
                f"min_free_bytes={limits.get('min_free_bytes', item['min_free_bytes'])}"
            )
            capacity_errors = item.get("capacity_errors") or []
            lines.append(f"object_storage_capacity_errors={','.join(capacity_errors) or 'none'}")
    compose = report["compose"]
    lines.append(
        "compose="
        f"available={compose['available']} "
        f"config_ok={compose['config_ok']} "
        f"services={','.join(compose['services']) or 'unknown'}"
    )
    pg_obs = report.get("postgres_observability") or {}
    lines.append(
        "postgres_observability="
        f"checked={pg_obs.get('checked', False)} "
        f"pg_stat_statements={pg_obs.get('pg_stat_statements', False)} "
        f"slow_query_ms={pg_obs.get('slow_query_ms', 0)} "
        f"status={pg_obs.get('status', 'unknown')} "
        f"error={pg_obs.get('error') or 'none'}"
    )
    health = report["health"]
    lines.append(f"health url={health['url']} ok={health['ok']} status_code={health['status_code']} error={health['error'] or 'none'}")
    return "\n".join(lines)


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _dir_report(path: Path, *, min_free_bytes: int) -> dict:
    exists = path.exists() and path.is_dir()
    probe_path = path if exists else path.parent
    try:
        usage = shutil.disk_usage(probe_path)
        total_bytes = int(usage.total)
        used_bytes = int(usage.used)
        free_bytes = int(usage.free)
    except Exception:
        total_bytes = 0
        used_bytes = 0
        free_bytes = 0
    writable = _path_is_writable(path) if exists else False
    status = "ok" if exists and writable and free_bytes >= int(min_free_bytes or 0) else "error"
    return {
        "path": str(path),
        "exists": exists,
        "writable": writable,
        "total_bytes": total_bytes,
        "used_bytes": used_bytes,
        "free_bytes": free_bytes,
        "min_free_bytes": int(min_free_bytes or 0),
        "status": status,
    }


def _object_storage_dir_report(path: Path, *, limits: dict[str, int]) -> dict:
    report = _dir_report(path, min_free_bytes=int(limits["min_free_bytes"]))
    capacity_errors = _object_storage_capacity_errors(
        total_bytes=int(report["total_bytes"]),
        free_bytes=int(report["free_bytes"]),
        limits=limits,
    )
    report["limits"] = {key: int(value or 0) for key, value in limits.items()}
    report["capacity_errors"] = capacity_errors
    if capacity_errors:
        report["status"] = "error"
    return report


def _wal_archive_dir_report(path: Path) -> dict:
    report = _dir_report(path, min_free_bytes=0)
    postgres_owner_writable = False
    try:
        info = path.stat()
        postgres_owner_writable = (
            path.exists()
            and path.is_dir()
            and int(info.st_uid) == 70
            and int(info.st_gid) == 70
            and bool(int(info.st_mode) & stat_module.S_IWUSR)
        )
    except Exception:
        postgres_owner_writable = False
    report["postgres_owner_writable"] = postgres_owner_writable
    if report["exists"] and (report.get("writable") or postgres_owner_writable):
        report["status"] = "ok"
    return report


def _object_storage_capacity_errors(*, total_bytes: int, free_bytes: int, limits: dict[str, int]) -> list[str]:
    total_quota = int(limits.get("total_quota_bytes") or 0)
    workspace_quota = int(limits.get("workspace_quota_bytes") or 0)
    max_file = int(limits.get("max_file_bytes") or 0)
    min_free = int(limits.get("min_free_bytes") or 0)
    errors: list[str] = []
    if total_quota > 0 and workspace_quota > total_quota:
        errors.append("workspace_quota_exceeds_total_quota")
    if workspace_quota > 0 and max_file > workspace_quota:
        errors.append("max_file_exceeds_workspace_quota")
    if total_quota > 0 and max_file > total_quota:
        errors.append("max_file_exceeds_total_quota")
    if total_bytes > 0 and total_quota > 0 and total_quota + min_free > total_bytes:
        errors.append("total_quota_exceeds_disk_capacity_after_min_free")
    if free_bytes > 0 and max_file > 0 and free_bytes - max_file < min_free:
        errors.append("not_enough_free_space_for_max_file_upload")
    return errors


def _path_is_writable(path: Path) -> bool:
    try:
        with tempfile.NamedTemporaryFile(prefix=".deploy-check-", dir=str(path), delete=True) as handle:
            handle.write(b"ok")
            handle.flush()
        return True
    except Exception:
        return False


def _compose_report() -> dict:
    available = shutil.which("docker") is not None
    services: list[str] = []
    config_ok = False
    error = ""
    if available:
        try:
            result = subprocess.run(
                ["docker", "compose", "config", "--services"],
                cwd=str(ROOT),
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            )
            services = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            config_ok = {"api", "worker", "postgres", "migrate"}.issubset(set(services))
        except Exception as exc:
            error = str(exc)
    return {"available": available, "config_ok": config_ok, "services": services, "error": error}


def _postgres_observability_report(env_values: dict[str, str] | None = None) -> dict:
    if shutil.which("docker") is None:
        return {"checked": False, "status": "warn", "pg_stat_statements": False, "slow_query_ms": 0, "error": "docker unavailable"}
    try:
        env_values = env_values or {}
        db_user = str(env_values.get("POSTGRES_USER") or "surfaced")
        db_name = str(env_values.get("POSTGRES_DB") or "surfaced_cloud")
        result = _run_docker_compose_exec(
            [
                "postgres",
                "psql",
                "-U",
                db_user,
                "-d",
                db_name,
                "-tAc",
                (
                    "SELECT current_setting('shared_preload_libraries', true), "
                    "current_setting('log_min_duration_statement', true), "
                    "EXISTS(SELECT 1 FROM pg_extension WHERE extname='pg_stat_statements')"
                ),
            ]
        )
        line = (result.stdout or "").strip().splitlines()[0]
        libraries, slow_query, extension = (line.split("|") + ["", "", ""])[:3]
        slow_query_ms = _parse_duration_ms(slow_query)
        pg_stat_ready = "pg_stat_statements" in str(libraries or "") and str(extension).strip().lower() == "t"
        status = "ok" if pg_stat_ready and slow_query_ms > 0 and slow_query_ms <= 200 else "warn"
        return {
            "checked": True,
            "status": status,
            "pg_stat_statements": pg_stat_ready,
            "slow_query_ms": slow_query_ms,
            "error": "",
        }
    except Exception as exc:
        return {
            "checked": False,
            "status": "warn",
            "pg_stat_statements": False,
            "slow_query_ms": 0,
            "error": str(exc),
        }


def _run_docker_compose_exec(args: list[str]) -> subprocess.CompletedProcess[str]:
    last_error: Exception | None = None
    prefixes = [["docker"], ["sudo", "-n", "docker"]]
    for prefix in prefixes:
        if shutil.which(prefix[0]) is None:
            continue
        command = [*prefix, "compose", "exec", "-T", *args]
        try:
            return subprocess.run(
                command,
                cwd=str(ROOT),
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError("docker unavailable")


def _parse_duration_ms(value: str) -> int:
    text = str(value or "").strip().lower()
    if not text:
        return 0
    try:
        if text.endswith("ms"):
            return int(float(text[:-2]))
        if text.endswith("s"):
            return int(float(text[:-1]) * 1000)
        return int(float(text))
    except Exception:
        return 0


def _health_report(base_url: str) -> dict:
    url = str(base_url or "").rstrip("/") + "/health"
    try:
        with urlopen(url, timeout=2) as response:
            status_code = int(getattr(response, "status", 0) or 0)
            ok = 200 <= status_code < 300
    except Exception as exc:
        return {"url": url, "ok": False, "status_code": 0, "error": str(exc)[:200]}
    return {"url": url, "ok": ok, "status_code": status_code, "error": ""}


def _safe_int(value: str | None, *, default: int) -> int:
    try:
        return int(str(value or "").strip())
    except Exception:
        return default


def _status(checks: dict) -> str:
    if not checks["env_file_exists"] or checks["env_missing"] or checks["weak_secrets"]:
        return "error"
    if any(item["status"] == "error" for item in checks["directories"].values()):
        return "error"
    if not checks["compose"]["config_ok"]:
        return "warn"
    if checks.get("postgres_observability", {}).get("status") == "warn":
        return "warn"
    if not checks["health"]["ok"]:
        return "warn"
    return "ok"


if __name__ == "__main__":
    raise SystemExit(main())
