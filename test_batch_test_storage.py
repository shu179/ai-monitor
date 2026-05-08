from pathlib import Path

import pytest

from core.batch_test_storage import merge_batch_json, read_batch_json, write_batch_json


def test_batch_json_write_read_and_merge_use_lock_file(tmp_path: Path):
    path = tmp_path / "batch.json"

    write_batch_json(
        path,
        {
            "batch_id": "batch-1",
            "status": "running",
            "progress": {"completed_queries": 1},
        },
    )
    merged = merge_batch_json(path, {"status": "cancelled"})

    assert merged["batch_id"] == "batch-1"
    assert merged["status"] == "cancelled"
    assert merged["progress"]["completed_queries"] == 1
    assert read_batch_json(path) == merged
    assert path.with_name("batch.json.lock").exists()


def test_batch_json_merge_does_not_create_missing_file_by_default(tmp_path: Path):
    path = tmp_path / "missing.json"

    merged = merge_batch_json(path, {"status": "running"})

    assert merged == {}
    assert not path.exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
