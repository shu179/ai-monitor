from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import Mock, patch

import web_backend
from web_backend import AppRuntime


def _runtime() -> AppRuntime:
    runtime = object.__new__(AppRuntime)
    runtime._article_sqlite_shadow_compare_lock = threading.RLock()
    runtime._article_sqlite_shadow_compare_recent = []
    runtime._article_sqlite_shadow_health_lock = threading.RLock()
    runtime._article_sqlite_shadow_health = {}
    runtime._history_sqlite_shadow_rebuild_lock = threading.RLock()
    return runtime


def test_history_storage_routes_are_registered() -> None:
    assert web_backend.GET_EXACT_RUNTIME_METHODS["/api/history-storage/status"] == "get_history_storage_status"
    assert (
        web_backend.POST_JSON_RUNTIME_METHODS["/api/history-storage/rebuild-shadow"]
        == "rebuild_history_sqlite_shadow"
    )


def test_get_history_storage_status_reports_health_and_shadow_db(tmp_path: Path) -> None:
    runtime = _runtime()
    db_path = tmp_path / "article_history_shadow.sqlite3"
    db_path.write_bytes(b"shadow")
    Path(f"{db_path}-wal").write_bytes(b"wal")

    with (
        patch("web_backend.default_shadow_db_path", return_value=db_path),
        patch("web_backend.get_structured_read_health", return_value={"enabled": False, "effectiveBackend": "json"}),
        patch("web_backend._open_sqlite_fd_count", return_value=2),
    ):
        status = runtime.get_history_storage_status()

    assert status["ok"] is True
    assert status["history"] == {"enabled": False, "effectiveBackend": "json"}
    assert status["articles"]["ok"] is True
    assert status["shadowDb"]["path"] == str(db_path)
    assert status["shadowDb"]["exists"] is True
    assert status["shadowDb"]["totalBytes"] == len(b"shadow") + len(b"wal")
    assert status["shadowDb"]["openFdCount"] == 2


def test_rebuild_history_sqlite_shadow_rebuilds_without_changing_config(tmp_path: Path) -> None:
    runtime = _runtime()
    db_path = tmp_path / "article_history_shadow.sqlite3"
    rebuild_calls = []

    def fake_rebuild(path, *, max_workers, verify_tail_limit):
        rebuild_calls.append((Path(path), max_workers, verify_tail_limit))
        db_path.write_bytes(b"rebuilt")
        return {
            "db_path": str(path),
            "history": {"records": 3},
            "verification": {"ok": True},
        }

    with (
        patch("web_backend.default_shadow_db_path", return_value=db_path),
        patch("web_backend.rebuild_shadow_store", side_effect=fake_rebuild),
        patch("web_backend.reset_structured_read_health") as reset_health,
        patch("web_backend.get_structured_read_health", return_value={"enabled": True, "effectiveBackend": "sqlite_shadow"}),
        patch("web_backend._open_sqlite_fd_count", return_value=0),
    ):
        result = runtime.rebuild_history_sqlite_shadow({"workers": 99, "tail_limit": 0})

    assert result["ok"] is True
    assert result["configChanged"] is False
    assert result["workers"] == 16
    assert result["tail_limit"] == 1
    assert result["rebuild"]["history"]["records"] == 3
    assert result["status"]["history"]["effectiveBackend"] == "sqlite_shadow"
    assert rebuild_calls == [(db_path, 16, 1)]
    reset_health.assert_called_once_with()
