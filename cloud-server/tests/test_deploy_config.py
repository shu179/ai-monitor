from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_docker_compose_mounts_object_and_backup_dirs() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "/opt/surfaced/object-data:/opt/surfaced/object-data" in compose
    assert "/opt/surfaced/backups:/opt/surfaced/backups" in compose
    assert "/opt/surfaced/postgres-wal:/opt/surfaced/postgres-wal" in compose
    assert "archive_mode=on" in compose
    assert "archive_command=test -d /opt/surfaced/postgres-wal" in compose
    assert "cp %p /opt/surfaced/postgres-wal/%f" in compose
    assert "max-size: \"10m\"" in compose
    assert "max-file: \"3\"" in compose


def test_dockerfile_runs_gunicorn_uvicorn_workers() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "gunicorn app.main:app" in dockerfile
    assert "uvicorn.workers.UvicornWorker" in dockerfile
    assert "SURFACED_CLOUD_API_WORKERS" in dockerfile


def test_env_example_documents_local_storage_and_backup_limits() -> None:
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "SURFACED_CLOUD_OBJECT_STORAGE_TOTAL_QUOTA_BYTES=10737418240" in env_example
    assert "SURFACED_CLOUD_OBJECT_STORAGE_WORKSPACE_QUOTA_BYTES=5368709120" in env_example
    assert "SURFACED_CLOUD_OBJECT_STORAGE_MAX_FILE_BYTES=536870912" in env_example
    assert "SURFACED_CLOUD_OBJECT_STORAGE_MIN_FREE_BYTES=8589934592" in env_example
    assert "SURFACED_CLOUD_BACKUP_DIR=/opt/surfaced/backups" in env_example
    assert "SURFACED_CLOUD_PITR_WAL_ARCHIVE_DIR=/opt/surfaced/postgres-wal" in env_example
    assert "SURFACED_CLOUD_PITR_BASEBACKUP_RETAIN_DAYS=7" in env_example


def test_pitr_dr_runbook_documents_restore_drill() -> None:
    runbook = (ROOT / "docs" / "PITR_DR.md").read_text(encoding="utf-8")

    assert "restore_command" in runbook
    assert "recovery_target_time" in runbook
    assert "RTO" in runbook
    assert "RPO" in runbook
