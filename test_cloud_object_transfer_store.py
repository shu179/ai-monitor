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
        path="/tmp/file.txt",
        content_type="text/plain",
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


def test_cloud_object_transfer_store_lists_retryable_failed_transfers(tmp_path: Path):
    store = CloudObjectTransferStore(tmp_path / "transfers.sqlite3")

    store.start_transfer(transfer_id="download-1", direction="download", object_id="object-1", sha256="a" * 64)
    store.fail_transfer("download-1", "temporary")
    store.start_transfer(transfer_id="upload-1", direction="upload", sha256="b" * 64)
    store.fail_transfer("upload-1", "temporary")

    downloads = store.retryable_transfers(direction="download")
    diagnostics = store.diagnostics()

    assert [item["transfer_id"] for item in downloads] == ["download-1"]
    assert diagnostics["retryable_count"] == 2


def test_cloud_object_transfer_store_excludes_transfers_over_attempt_budget(tmp_path: Path):
    store = CloudObjectTransferStore(tmp_path / "transfers.sqlite3")

    for _index in range(3):
        store.start_transfer(transfer_id="download-1", direction="download", object_id="object-1")
        store.fail_transfer("download-1", "temporary")

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
