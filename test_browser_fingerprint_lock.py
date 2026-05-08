from pathlib import Path

import pytest

from core.browser_fingerprint import BrowserFingerprint


def test_browser_fingerprint_uses_atomic_locked_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(BrowserFingerprint, "_detect_screen_resolution", lambda self: (1920, 1080))

    first = BrowserFingerprint(str(tmp_path))
    second = BrowserFingerprint(str(tmp_path))

    assert (tmp_path / ".fingerprint.json").exists()
    assert (tmp_path / ".fingerprint.json.lock").exists()
    assert second.config == first.config


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
