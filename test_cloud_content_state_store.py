from pathlib import Path

from core.cloud_content_state_store import CloudContentStateStore


def test_cloud_content_state_store_records_answer_with_object_refs(tmp_path: Path):
    store = CloudContentStateStore(tmp_path / "content.sqlite3")

    result = store.apply_answer(
        "https://api.example.com|3|2",
        {
            "type": "answer",
            "id": "answer-001",
            "workspace_id": 3,
            "task_id": 42,
            "run_record_id": "run-9",
            "platform": "doubao",
            "keyword": "即搜AI",
            "brand": "即搜AI",
            "content_ref": {"kind": "object", "object_id": "obj-1"},
            "created_at": "2026-01-01T00:00:00+00:00",
        },
        object_refs=[
            {
                "object_id": "obj-1",
                "sha256": "a" * 64,
                "size_bytes": 1234,
                "storage_size_bytes": 456,
                "content_type": "text/plain",
                "compression": "zstd",
                "storage_key": "3/aa/bb/object",
            }
        ],
    )
    diagnostics = store.diagnostics({"base_url": "https://api.example.com", "user": {"workspace_id": 3, "id": 2}})

    assert result["ok"] is True
    assert diagnostics["answers_total"] == 1
    assert diagnostics["assets_total"] == 0
    assert diagnostics["answer_by_type"] == {"answer": 1}
    assert diagnostics["newest_answers"][0]["object_ref_count"] == 1
    assert diagnostics["newest_answers"][0]["content_kind"] == "object"


def test_cloud_content_state_store_records_asset_manifest(tmp_path: Path):
    store = CloudContentStateStore(tmp_path / "content.sqlite3")

    result = store.apply_asset(
        "https://api.example.com|3|2",
        {
            "type": "asset",
            "workspace_id": 3,
            "object_id": "obj-1",
            "sha256": "a" * 64,
            "size_bytes": 1234,
            "storage_size_bytes": 456,
            "content_type": "image/png",
            "compression": "none",
            "storage_key": "3/aa/bb/object",
            "status": "active",
        },
    )
    diagnostics = store.diagnostics({"base_url": "https://api.example.com", "user": {"workspace_id": 3, "id": 2}})

    assert result["ok"] is True
    assert diagnostics["answers_total"] == 0
    assert diagnostics["assets_total"] == 1
    assert diagnostics["asset_by_status"] == {"active": 1}
    assert diagnostics["newest_assets"][0]["object_id"] == "obj-1"
    assert diagnostics["newest_assets"][0]["content_type"] == "image/png"


def test_cloud_content_state_store_rejects_invalid_entities(tmp_path: Path):
    store = CloudContentStateStore(tmp_path / "content.sqlite3")

    answer_result = store.apply_answer("https://api.example.com|3|2", {})
    asset_result = store.apply_asset("https://api.example.com|3|2", {})

    assert answer_result["ok"] is False
    assert asset_result["ok"] is False
