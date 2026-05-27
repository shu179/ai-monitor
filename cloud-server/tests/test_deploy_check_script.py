from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import deploy_check


def test_parse_env_file_reads_simple_key_values() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".env"
        path.write_text("A=1\n# ignored\nB='two'\n", encoding="utf-8")

        assert deploy_check.parse_env_file(path) == {"A": "1", "B": "two"}


def test_status_errors_on_missing_env_or_weak_secret() -> None:
    checks = {
        "env_file_exists": True,
        "env_missing": [],
        "weak_secrets": ["POSTGRES_PASSWORD"],
        "directories": {"object_storage": {"status": "ok"}, "backups": {"status": "ok"}},
        "compose": {"config_ok": True},
        "health": {"ok": True},
    }

    assert deploy_check._status(checks) == "error"


def test_status_warns_when_health_is_down_but_config_is_safe() -> None:
    checks = {
        "env_file_exists": True,
        "env_missing": [],
        "weak_secrets": [],
        "directories": {"object_storage": {"status": "ok"}, "backups": {"status": "ok"}},
        "compose": {"config_ok": True},
        "health": {"ok": False},
    }

    assert deploy_check._status(checks) == "warn"


def test_compose_report_handles_missing_docker() -> None:
    with patch("scripts.deploy_check.shutil.which", return_value=None):
        report = deploy_check._compose_report()

    assert report["available"] is False
    assert report["config_ok"] is False


def test_format_report_contains_key_sections() -> None:
    report = {
        "status": "warn",
        "env_file_exists": True,
        "env_missing": [],
        "weak_secrets": [],
        "directories": {
            "object_storage": {
                "path": "/opt/surfaced/object-data",
                "exists": True,
                "free_bytes": 10,
                "min_free_bytes": 1,
                "status": "ok",
            },
            "backups": {
                "path": "/opt/surfaced/backups",
                "exists": True,
                "free_bytes": 10,
                "min_free_bytes": 0,
                "status": "ok",
            },
        },
        "compose": {"available": True, "config_ok": True, "services": ["api", "worker"], "error": ""},
        "health": {"url": "http://127.0.0.1:8080/health", "ok": False, "status_code": 0, "error": "offline"},
    }

    text = deploy_check.format_deploy_check_report(report)

    assert "Deploy check: status=warn" in text
    assert "object_storage_dir" in text
    assert "compose=" in text
    assert "health url=" in text
