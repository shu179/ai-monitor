from pathlib import Path

from core.cloud_object_transfer_store import CloudObjectTransferStore


def test_cloud_object_transfer_store_records_success_and_diagnostics(tmp_path: Path):
    store = CloudObjectTransferStore(tmp_path / "transfers.sqlite3")

    started = store.start_transfer(
        transfer_id="upload-1",
        direction="upload",
        object_id="",
        sha256="a" * 64,
        size_bytes=123,
        storage_size_bytes=99,
        path="/tmp/file.txt",
        content_type="text/plain",
        compression="zstd",
        trace_id="trace-1",
    )
    finished = store.finish_transfer("upload-1", object_id="object-1", status="active")
    diagnostics = store.diagnostics()

    assert started["ok"] is True
    assert finished["ok"] is True
    assert diagnostics["total"] == 1
    assert diagnostics["by_status"] == {"active": 1}
    assert diagnostics["by_direction"] == {"upload": {"active": 1}}
    assert diagnostics["bytes_by_direction"] == {"upload": 123}
    assert diagnostics["newest"][0]["object_id"] == "object-1"
    assert diagnostics["newest"][0]["attempts"] == 1
    assert diagnostics["newest"][0]["content_type"] == "text/plain"
    assert diagnostics["newest"][0]["storage_size_bytes"] == 99
    assert diagnostics["newest"][0]["compression"] == "zstd"


def test_cloud_object_transfer_store_retries_increment_attempts(tmp_path: Path):
    store = CloudObjectTransferStore(tmp_path / "transfers.sqlite3")

    store.start_transfer(transfer_id="upload-1", direction="upload", sha256="a" * 64)
    store.fail_transfer("upload-1", "network down")
    store.start_transfer(transfer_id="upload-1", direction="upload", sha256="a" * 64)
    diagnostics = store.diagnostics()

    assert diagnostics["by_status"] == {"running": 1}
    assert diagnostics["newest"][0]["attempts"] == 2
    assert diagnostics["newest"][0]["last_error"] == ""


def test_cloud_object_transfer_store_records_failure(tmp_path: Path):
    store = CloudObjectTransferStore(tmp_path / "transfers.sqlite3")

    store.start_transfer(transfer_id="download-1", direction="download", object_id="object-1")
    failed = store.fail_transfer("download-1", "sha mismatch")
    diagnostics = store.diagnostics()

    assert failed["ok"] is True
    assert diagnostics["by_status"] == {"failed": 1}
    assert diagnostics["by_direction"] == {"download": {"failed": 1}}
    assert diagnostics["failed"][0]["last_error"] == "sha mismatch"
    assert diagnostics["failed"][0]["next_attempt_at"]
    assert diagnostics["failed"][0]["next_attempt_after_seconds"] >= 1


def test_cloud_object_transfer_store_updates_transfer_metadata(tmp_path: Path):
    store = CloudObjectTransferStore(tmp_path / "transfers.sqlite3")

    store.start_transfer(transfer_id="download-1", direction="download", object_id="object-1")
    updated = store.update_transfer_metadata(
        "download-1",
        sha256="b" * 64,
        size_bytes=123,
        storage_size_bytes=45,
        content_type="text/plain",
        compression="zstd",
    )
    store.fail_transfer("download-1", "temporary", retry_after_seconds=0)
    retryable = store.retryable_transfers()

    assert updated["ok"] is True
    assert retryable[0]["sha256"] == "b" * 64
    assert retryable[0]["size_bytes"] == 123
    assert retryable[0]["storage_size_bytes"] == 45
    assert retryable[0]["content_type"] == "text/plain"
    assert retryable[0]["compression"] == "zstd"


def test_cloud_object_transfer_store_lists_retryable_failed_transfers(tmp_path: Path):
    store = CloudObjectTransferStore(tmp_path / "transfers.sqlite3")

    store.start_transfer(transfer_id="download-1", direction="download", object_id="object-1", sha256="a" * 64)
    store.fail_transfer("download-1", "temporary", retry_after_seconds=0)
    store.start_transfer(transfer_id="upload-1", direction="upload", sha256="b" * 64)
    store.fail_transfer("upload-1", "temporary", retry_after_seconds=0)

    downloads = store.retryable_transfers(direction="download")
    diagnostics = store.diagnostics()

    assert [item["transfer_id"] for item in downloads] == ["download-1"]
    assert diagnostics["retryable_count"] == 2


def test_cloud_object_transfer_store_excludes_transfers_over_attempt_budget(tmp_path: Path):
    store = CloudObjectTransferStore(tmp_path / "transfers.sqlite3")

    for _index in range(3):
        store.start_transfer(transfer_id="download-1", direction="download", object_id="object-1")
        store.fail_transfer("download-1", "temporary", retry_after_seconds=0)

    retryable = store.retryable_transfers(max_attempts=3)

    assert retryable == []


def test_cloud_object_transfer_store_lists_stale_running_transfers(tmp_path: Path):
    store = CloudObjectTransferStore(tmp_path / "transfers.sqlite3")
    store.start_transfer(transfer_id="download-1", direction="download", object_id="object-1")
    with store._connection() as conn:
        conn.execute(
            "UPDATE object_transfers SET updated_at = ? WHERE transfer_id = ?",
            ("2000-01-01T00:00:00+00:00", "download-1"),
        )

    retryable = store.retryable_transfers(stale_running_seconds=60)

    assert [item["transfer_id"] for item in retryable] == ["download-1"]


def test_cloud_object_transfer_store_honors_retry_after_and_reports_retry_status(tmp_path: Path):
    store = CloudObjectTransferStore(tmp_path / "transfers.sqlite3")

    store.start_transfer(transfer_id="upload-1", direction="upload", sha256="a" * 64)
    store.fail_transfer("upload-1", "busy", retry_after_seconds=7.5)
    retryable = store.retryable_transfers(direction="upload")
    status = store.retry_status(direction="upload")
    diagnostics = store.diagnostics()

    assert retryable == []
    assert status["retry_ready_count"] == 0
    assert status["retry_waiting_count"] == 1
    assert status["wait_reason"] == "waiting_retry_backoff"
    assert status["next_retry_after_seconds"] >= 7
    assert diagnostics["retry_status_by_direction"]["upload"]["wait_reason"] == "waiting_retry_backoff"


def test_cloud_object_transfer_store_migrates_existing_db_with_transfer_metadata(tmp_path: Path):
    db_path = tmp_path / "legacy.sqlite3"
    store = CloudObjectTransferStore(db_path)
    store.start_transfer(transfer_id="download-1", direction="download", object_id="object-1")
    with store._connection() as conn:
        conn.execute("ALTER TABLE object_transfers RENAME TO object_transfers_new")
        conn.execute(
            """
            CREATE TABLE object_transfers (
                transfer_id TEXT PRIMARY KEY,
                direction TEXT NOT NULL,
                object_id TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                path TEXT NOT NULL,
                content_type TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                trace_id TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            INSERT INTO object_transfers(
                transfer_id, direction, object_id, sha256, size_bytes, path,
                content_type, status, attempts, trace_id, last_error,
                started_at, updated_at, completed_at
            )
            SELECT transfer_id, direction, object_id, sha256, size_bytes, path,
                   content_type, status, attempts, trace_id, last_error,
                   started_at, updated_at, completed_at
            FROM object_transfers_new
            """
        )
        conn.execute("DROP TABLE object_transfers_new")

    restarted = CloudObjectTransferStore(db_path)
    restarted.start_transfer(
        transfer_id="download-1",
        direction="download",
        object_id="object-1",
        sha256="a" * 64,
        size_bytes=123,
        storage_size_bytes=45,
        content_type="text/plain",
        compression="zstd",
    )
    diagnostics = restarted.diagnostics()

    assert diagnostics["newest"][0]["storage_size_bytes"] == 45
    assert diagnostics["newest"][0]["compression"] == "zstd"
