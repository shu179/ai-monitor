from pathlib import Path

from core.browser_auth import cleanup_browser_runtime_cache


def test_cleanup_browser_runtime_cache_keeps_login_state(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    monkeypatch.setenv("AIBRANDMONITOR_DATA_DIR", str(data_root))
    profile = data_root / "user_data" / "browser_auth_profiles" / "doubao" / "default"
    (profile / "Default" / "Cache").mkdir(parents=True)
    (profile / "Default" / "Code Cache" / "js").mkdir(parents=True)
    (profile / "Default" / "Local Storage" / "leveldb").mkdir(parents=True)
    (profile / "ShaderCache").mkdir(parents=True)
    (profile / "Default" / "Cookies").write_text("cookie", encoding="utf-8")
    (profile / "Default" / "Local Storage" / "leveldb" / "000003.log").write_text("state", encoding="utf-8")
    (profile / "Default" / "Cache" / "entry").write_text("cache", encoding="utf-8")
    (profile / "Default" / "Code Cache" / "js" / "entry").write_text("code", encoding="utf-8")
    (profile / "ShaderCache" / "data_0").write_text("shader", encoding="utf-8")
    (profile / "SingletonLock").write_text("lock", encoding="utf-8")

    result = cleanup_browser_runtime_cache(profile)

    assert result["ok"] is True
    assert result["removed"] >= 3
    assert not (profile / "Default" / "Cache").exists()
    assert not (profile / "Default" / "Code Cache").exists()
    assert not (profile / "ShaderCache").exists()
    assert not (profile / "SingletonLock").exists()
    assert (profile / "Default" / "Cookies").read_text(encoding="utf-8") == "cookie"
    assert (profile / "Default" / "Local Storage" / "leveldb" / "000003.log").read_text(encoding="utf-8") == "state"


def test_cleanup_browser_runtime_cache_rejects_paths_outside_app_data(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    monkeypatch.setenv("AIBRANDMONITOR_DATA_DIR", str(data_root))
    outside_profile = tmp_path / "outside-profile"
    (outside_profile / "Default" / "Cache").mkdir(parents=True)
    cache_file = outside_profile / "Default" / "Cache" / "entry"
    cache_file.write_text("cache", encoding="utf-8")

    result = cleanup_browser_runtime_cache(Path(outside_profile))

    assert result["ok"] is False
    assert result["reason"] == "unsafe_profile_path"
    assert cache_file.read_text(encoding="utf-8") == "cache"
