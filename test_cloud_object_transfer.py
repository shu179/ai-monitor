import hashlib
from pathlib import Path

import pytest

from core.cloud_object_transfer import CloudObjectTransferError, upload_cloud_object_file
from core.cloud_object_transfer_store import CloudObjectTransferStore


class FakeUploadClient:
    def __init__(self, *, strategy: str = "single_put") -> None:
        self.strategy = strategy
        self.calls: list[tuple[str, dict]] = []

    def create_object_upload(self, token: str, **kwargs):
        self.calls.append(("create", {"token": token, **kwargs}))
        if self.strategy == "already_exists":
            return {
                "strategy": "already_exists",
                "object_id": "object-1",
                "sha256": kwargs["sha256"],
                "size_bytes": kwargs["size_bytes"],
                "storage_size_bytes": kwargs["storage_size_bytes"],
                "content_type": kwargs["content_type"],
                "compression": "none",
            }
        if self.strategy == "multipart":
            return {
                "strategy": "multipart",
                "session_id": "session-1",
                "sha256": kwargs["sha256"],
                "size_bytes": kwargs["size_bytes"],
                "storage_size_bytes": kwargs["storage_size_bytes"],
                "content_type": kwargs["content_type"],
                "compression": "none",
                "part_size_bytes": 4,
                "parts_total": 3,
            }
        return {
            "strategy": "single_put",
            "session_id": "session-1",
            "sha256": kwargs["sha256"],
            "size_bytes": kwargs["size_bytes"],
            "storage_size_bytes": kwargs["storage_size_bytes"],
            "content_type": kwargs["content_type"],
            "compression": "none",
            "upload": {"url": "/api/v2/objects/uploads/session-1/content", "headers": {"X-Test": "1"}},
        }

    def upload_object_content(self, token: str, upload_url: str, chunks, **kwargs):
        self.calls.append(
            (
                "upload",
                {
                    "token": token,
                    "upload_url": upload_url,
                    "body": b"".join(chunks),
                    **kwargs,
                },
            )
        )
        return {"object_id": "object-1"}

    def presign_object_upload_parts(self, token: str, session_id: str, part_numbers: list[int], **kwargs):
        self.calls.append(("presign_parts", {"token": token, "session_id": session_id, "part_numbers": part_numbers, **kwargs}))
        return {
            "session_id": session_id,
            "part_size_bytes": 4,
            "parts_total": 3,
            "upload_urls": [
                {
                    "part_number": part_number,
                    "url": f"https://storage.example.com/part-{part_number}",
                    "headers": {"X-Part": str(part_number)},
                }
                for part_number in part_numbers
            ],
        }

    def upload_object_part_content(self, token: str, upload_url: str, chunks, **kwargs):
        body = b"".join(chunks)
        part_number = int(upload_url.rsplit("-", 1)[-1])
        self.calls.append(
            (
                "upload_part",
                {
                    "token": token,
                    "upload_url": upload_url,
                    "body": body,
                    **kwargs,
                },
            )
        )
        return {"etag": f'"etag-{part_number}"'}

    def record_object_upload_part(self, token: str, session_id: str, **kwargs):
        self.calls.append(("record_part", {"token": token, "session_id": session_id, **kwargs}))
        return {
            "session_id": session_id,
            "part_number": kwargs["part_number"],
            "parts_completed": kwargs["part_number"],
            "parts_total": 3,
        }

    def complete_object_upload(self, token: str, session_id: str, **kwargs):
        self.calls.append(("complete", {"token": token, "session_id": session_id, **kwargs}))
        return {"object_id": "object-1", "status": "active", "sha256": self.calls[0][1]["sha256"]}


def test_upload_cloud_object_file_single_puts_and_completes(tmp_path: Path):
    path = tmp_path / "answer.txt"
    path.write_bytes(b"hello cloud object")
    client = FakeUploadClient()
    store = CloudObjectTransferStore(tmp_path / "transfers.sqlite3")

    result = upload_cloud_object_file(
        client,
        "access",
        path,
        content_type="text/plain",
        compression="none",
        trace_id="trace-1",
        chunk_bytes=5,
        transfer_store=store,
    )
    diagnostics = store.diagnostics()

    assert result["ok"] is True
    assert result["uploaded"] is True
    assert [name for name, _payload in client.calls] == ["create", "upload", "complete"]
    assert client.calls[0][1]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert client.calls[0][1]["size_bytes"] == len(path.read_bytes())
    assert client.calls[1][1]["upload_url"] == "/api/v2/objects/uploads/session-1/content"
    assert client.calls[1][1]["body"] == path.read_bytes()
    assert client.calls[1][1]["headers"] == {"X-Test": "1"}
    assert client.calls[2][1]["session_id"] == "session-1"
    assert client.calls[2][1]["storage_size_bytes"] == len(path.read_bytes())
    assert diagnostics["by_status"] == {"active": 1}
    assert diagnostics["newest"][0]["object_id"] == "object-1"
    assert diagnostics["newest"][0]["path"] == str(path)


def test_upload_cloud_object_file_returns_existing_manifest_without_upload(tmp_path: Path):
    path = tmp_path / "image.bin"
    path.write_bytes(b"already there")
    client = FakeUploadClient(strategy="already_exists")

    result = upload_cloud_object_file(client, "access", path)

    assert result["ok"] is True
    assert result["uploaded"] is False
    assert [name for name, _payload in client.calls] == ["create"]


def test_upload_cloud_object_file_multipart_uploads_parts_and_completes(tmp_path: Path):
    path = tmp_path / "large.bin"
    path.write_bytes(b"abcdefghij")
    client = FakeUploadClient(strategy="multipart")
    store = CloudObjectTransferStore(tmp_path / "transfers.sqlite3")

    result = upload_cloud_object_file(
        client,
        "access",
        path,
        content_type="application/octet-stream",
        compression="none",
        trace_id="trace-multipart",
        transfer_store=store,
    )

    diagnostics = store.diagnostics()
    assert result["ok"] is True
    assert result["uploaded"] is True
    assert result["strategy"] == "multipart"
    assert [name for name, _payload in client.calls] == [
        "create",
        "presign_parts",
        "upload_part",
        "record_part",
        "presign_parts",
        "upload_part",
        "record_part",
        "presign_parts",
        "upload_part",
        "record_part",
        "complete",
    ]
    assert client.calls[2][1]["body"] == b"abcd"
    assert client.calls[5][1]["body"] == b"efgh"
    assert client.calls[8][1]["body"] == b"ij"
    assert client.calls[3][1]["etag"] == '"etag-1"'
    assert client.calls[3][1]["size_bytes"] == 4
    assert client.calls[3][1]["sha256"] == hashlib.sha256(b"abcd").hexdigest()
    assert client.calls[9][1]["size_bytes"] == 2
    assert client.calls[-1][1]["session_id"] == "session-1"
    assert client.calls[-1][1]["storage_size_bytes"] == len(path.read_bytes())
    assert diagnostics["by_status"] == {"active": 1}
    assert diagnostics["newest"][0]["object_id"] == "object-1"


def test_upload_cloud_object_file_rejects_empty_file(tmp_path: Path):
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")

    with pytest.raises(CloudObjectTransferError, match="empty"):
        upload_cloud_object_file(FakeUploadClient(), "access", path)
