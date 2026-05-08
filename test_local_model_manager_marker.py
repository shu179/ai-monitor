from pathlib import Path

import pytest

from core.local_model_manager import LocalModelManager


def test_bundle_sync_marker_uses_atomic_locked_file(tmp_path: Path):
    manager = LocalModelManager()
    manager._resolved_runtime_models = str(tmp_path / "models")
    payload = {"source_path": "/bundle", "source_root": {"mtime_ns": 1, "size": 2}}

    manager._save_bundle_sync_marker(payload)

    marker_path = tmp_path / ".bundle_sync_state.json"
    assert manager._load_bundle_sync_marker() == payload
    assert marker_path.exists()
    assert marker_path.with_name(".bundle_sync_state.json.lock").exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
