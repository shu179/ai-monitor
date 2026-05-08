from pathlib import Path

import pytest

from core.cloud_session_store import CloudSessionStore


def _token_pair(access: str, refresh: str, *, user_id: str = "u1", workspace_id: str = "w1") -> dict:
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "user": {
            "id": user_id,
            "workspace_id": workspace_id,
        },
    }


def test_save_and_update_user_use_path_scoped_lock(tmp_path: Path):
    store_path = tmp_path / "session.json"
    store = CloudSessionStore(store_path)

    saved = store.save_login(base_url="https://cloud.example/", token_pair=_token_pair("a1", "r1"))
    updated = store.update_user({"id": "u2", "workspace_id": "w2", "name": "updated"})

    assert saved["base_url"] == "https://cloud.example"
    assert updated["access_token"] == "a1"
    assert updated["refresh_token"] == "r1"
    assert updated["user"]["id"] == "u2"
    assert store.load()["user"]["workspace_id"] == "w2"
    assert store_path.with_name("session.json.lock").exists()


def test_clear_if_current_keeps_newer_session(tmp_path: Path):
    store = CloudSessionStore(tmp_path / "session.json")
    store.save_login(base_url="https://cloud.example", token_pair=_token_pair("new_access", "new_refresh"))

    cleared = store.clear_if_current(
        base_url="https://cloud.example",
        access_token="old_access",
        refresh_token="old_refresh",
        workspace_id="w1",
        user_id="u1",
    )

    assert cleared is False
    assert store.load()["access_token"] == "new_access"


def test_refresh_login_if_current_returns_newer_session_without_refresh(tmp_path: Path):
    store = CloudSessionStore(tmp_path / "session.json")
    store.save_login(base_url="https://cloud.example", token_pair=_token_pair("new_access", "new_refresh"))

    def fail_refresh(_token: str) -> dict:
        raise AssertionError("refresh should not run for a newer saved session")

    session = store.refresh_login_if_current(
        base_url="https://cloud.example",
        access_token="old_access",
        refresh_token="old_refresh",
        workspace_id="w1",
        user_id="u1",
        refresh=fail_refresh,
    )

    assert session["access_token"] == "new_access"
    assert session["refresh_token"] == "new_refresh"


def test_refresh_login_if_current_saves_rotated_tokens(tmp_path: Path):
    store = CloudSessionStore(tmp_path / "session.json")
    store.save_login(base_url="https://cloud.example", token_pair=_token_pair("old_access", "old_refresh"))

    def refresh(token: str) -> dict:
        assert token == "old_refresh"
        return _token_pair("new_access", "new_refresh", user_id="u2", workspace_id="w2")

    session = store.refresh_login_if_current(
        base_url="https://cloud.example",
        access_token="old_access",
        refresh_token="old_refresh",
        workspace_id="w1",
        user_id="u1",
        refresh=refresh,
    )

    assert session["access_token"] == "new_access"
    assert session["refresh_token"] == "new_refresh"
    assert store.load()["user"]["workspace_id"] == "w2"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
