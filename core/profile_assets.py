"""Helpers for storing profile-owned media outside config.yaml."""

from __future__ import annotations

import base64
import binascii
import re
from pathlib import Path
from typing import Any

from core.app_paths import get_app_root, resolve_app_path

_PROFILE_AVATAR_PREFIX = "user_data/profile/avatar"
_PROFILE_AVATAR_URL = "/api/assets/profile/avatar"
_DATA_URL_RE = re.compile(r"^data:image/(?P<kind>[a-zA-Z0-9.+-]+);base64,(?P<data>.+)$", re.DOTALL)
_IMAGE_EXTENSIONS = {
    "jpeg": ".jpg",
    "jpg": ".jpg",
    "png": ".png",
    "webp": ".webp",
    "gif": ".gif",
}
_IMAGE_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def is_profile_avatar_url(value: Any) -> bool:
    """Return whether a frontend payload points at the served avatar endpoint."""
    return str(value or "").strip().startswith(_PROFILE_AVATAR_URL)


def public_profile_avatar_url(value: Any) -> str:
    """Return a browser-loadable URL for the configured profile avatar."""
    avatar = str(value or "").strip()
    if not avatar:
        return ""
    if avatar.startswith("data:") or avatar.startswith("http://") or avatar.startswith("https://"):
        return avatar
    if avatar.startswith("/") and not avatar.startswith(_PROFILE_AVATAR_URL):
        return avatar
    path = resolve_profile_avatar_path(avatar)
    if not path or not path.exists() or not path.is_file():
        return ""
    try:
        version = int(path.stat().st_mtime)
    except OSError:
        version = 0
    return f"{_PROFILE_AVATAR_URL}?v={version}" if version else _PROFILE_AVATAR_URL


def resolve_profile_avatar_path(value: Any) -> Path | None:
    """Resolve a stored profile avatar path without allowing arbitrary file reads."""
    avatar = str(value or "").strip()
    if not avatar or avatar.startswith("data:") or "://" in avatar:
        return None
    if avatar.startswith(_PROFILE_AVATAR_URL):
        return None
    if not avatar.startswith(_PROFILE_AVATAR_PREFIX):
        return None

    relative = Path(avatar)
    if relative.is_absolute() or ".." in relative.parts:
        return None

    app_path = (get_app_root() / relative).resolve()
    try:
        app_path.relative_to((get_app_root() / "user_data" / "profile").resolve())
    except ValueError:
        return None
    if app_path.exists():
        return app_path

    data_path = resolve_app_path(relative)
    try:
        data_path.relative_to(resolve_app_path("user_data/profile"))
    except ValueError:
        return None
    return data_path


def store_profile_avatar(value: Any) -> str:
    """Persist a data-URL avatar and return the config-relative path."""
    avatar = str(value or "").strip()
    match = _DATA_URL_RE.match(avatar)
    if not match:
        return avatar

    ext = _IMAGE_EXTENSIONS.get(match.group("kind").lower(), ".jpg")
    try:
        data = base64.b64decode(match.group("data"), validate=True)
    except (binascii.Error, ValueError):
        return ""
    if not data:
        return ""

    relative = Path(f"{_PROFILE_AVATAR_PREFIX}{ext}")
    target = get_app_root() / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return relative.as_posix()


def read_profile_avatar_asset(config: dict[str, Any]) -> tuple[bytes, str] | None:
    """Read the configured avatar asset for HTTP serving."""
    profile = config.get("profile", {}) if isinstance(config, dict) else {}
    avatar = profile.get("avatar", "") if isinstance(profile, dict) else ""
    path = resolve_profile_avatar_path(avatar)
    if not path or not path.exists() or not path.is_file():
        return None
    content_type = _IMAGE_CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return path.read_bytes(), content_type
