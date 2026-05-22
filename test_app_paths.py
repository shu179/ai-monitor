from __future__ import annotations

from pathlib import Path

from core import app_paths as app_paths_module


def test_get_data_root_migrates_legacy_config_local(monkeypatch, tmp_path):
    app_root = tmp_path / "legacy-app"
    data_root = tmp_path / "data-root"
    app_root.mkdir()
    data_root.mkdir()

    legacy_local = app_root / "config.local.yaml"
    legacy_local.write_text(
        "task_secrets:\n  legacy_task:\n    webhook_url: https://example.com/webhook\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("AIBRANDMONITOR_DATA_DIR", str(data_root))
    monkeypatch.setattr(app_paths_module, "get_app_root", lambda: app_root)
    monkeypatch.setattr(app_paths_module, "_migration_done", False)

    resolved_root = app_paths_module.get_data_root()

    assert resolved_root == data_root
    assert (data_root / "config.local.yaml").read_text(encoding="utf-8") == legacy_local.read_text(
        encoding="utf-8"
    )
