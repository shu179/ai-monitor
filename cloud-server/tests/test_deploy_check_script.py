from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
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
                "writable": True,
                "total_bytes": 100,
                "free_bytes": 10,
                "min_free_bytes": 1,
                "limits": {
                    "total_quota_bytes": 50,
                    "workspace_quota_bytes": 25,
                    "max_file_bytes": 10,
                    "min_free_bytes": 1,
                },
                "capacity_errors": [],
                "status": "ok",
            },
            "backups": {
                "path": "/opt/surfaced/backups",
                "exists": True,
                "writable": True,
                "total_bytes": 100,
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
    assert "object_storage_limits" in text
    assert "object_storage_capacity_errors=none" in text
    assert "compose=" in text
    assert "health url=" in text


def test_object_storage_capacity_errors_detect_impossible_disk_budget() -> None:
    errors = deploy_check._object_storage_capacity_errors(
        total_bytes=12_000,
        free_bytes=10_000,
        limits={
            "total_quota_bytes": 10_000,
            "workspace_quota_bytes": 5_000,
            "max_file_bytes": 3_000,
            "min_free_bytes": 8_000,
        },
    )

    assert "total_quota_exceeds_disk_capacity_after_min_free" in errors
    assert "not_enough_free_space_for_max_file_upload" in errors


def test_object_storage_dir_report_marks_capacity_errors_as_error() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch("scripts.deploy_check.shutil.disk_usage") as disk_usage:
            disk_usage.return_value = SimpleNamespace(total=12_000, used=2_000, free=10_000)
            report = deploy_check._object_storage_dir_report(
                Path(tmp),
                limits={
                    "total_quota_bytes": 10_000,
                    "workspace_quota_bytes": 5_000,
                    "max_file_bytes": 3_000,
                    "min_free_bytes": 8_000,
                },
            )

    assert report["status"] == "error"
    assert report["writable"] is True
    assert "total_quota_exceeds_disk_capacity_after_min_free" in report["capacity_errors"]


def test_wal_archive_dir_accepts_postgres_owned_directory() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        fake_stat = SimpleNamespace(
            st_uid=70,
            st_gid=70,
            st_mode=0o040700,
            st_mtime=0,
        )
        with (
            patch("scripts.deploy_check._path_is_writable", return_value=False),
            patch.object(Path, "stat", return_value=fake_stat),
        ):
            report = deploy_check._wal_archive_dir_report(path)

    assert report["status"] == "ok"
    assert report["writable"] is False
    assert report["postgres_owner_writable"] is True
