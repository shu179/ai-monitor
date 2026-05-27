from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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
    min_free = _safe_int(env_values.get("SURFACED_CLOUD_OBJECT_STORAGE_MIN_FREE_BYTES"), default=8 * 1024 * 1024 * 1024)
    dirs = {
        "object_storage": _dir_report(object_dir, min_free_bytes=min_free),
        "backups": _dir_report(backup_dir, min_free_bytes=0),
    }
    compose = _compose_report()
    health = _health_report(base_url)
    checks = {
        "env_file_exists": env_path.exists(),
        "env_missing": env_missing,
        "weak_secrets": weak_secrets,
        "directories": dirs,
        "compose": compose,
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
            f"free_bytes={item['free_bytes']} min_free_bytes={item['min_free_bytes']} status={item['status']}"
        )
    compose = report["compose"]
    lines.append(
        "compose="
        f"available={compose['available']} "
        f"config_ok={compose['config_ok']} "
        f"services={','.join(compose['services']) or 'unknown'}"
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
        free_bytes = int(usage.free)
    except Exception:
        free_bytes = 0
    status = "ok" if exists and free_bytes >= int(min_free_bytes or 0) else "error"
    return {
        "path": str(path),
        "exists": exists,
        "free_bytes": free_bytes,
        "min_free_bytes": int(min_free_bytes or 0),
        "status": status,
    }


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
    if not checks["health"]["ok"]:
        return "warn"
    return "ok"


if __name__ == "__main__":
    raise SystemExit(main())
