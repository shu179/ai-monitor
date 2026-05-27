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
