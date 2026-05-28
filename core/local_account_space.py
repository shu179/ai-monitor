from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .app_paths import get_data_root, resolve_app_path
from .cloud_session_store import CloudSessionStore, normalize_cloud_base_url
from .file_lock import CrossProcessRLock


ACCOUNT_PROFILE_ROOT = get_data_root() / "user_data" / "cloud_profiles"

ACCOUNT_SCOPED_RELATIVE_PATHS = (
    "config.yaml",
    "config.local.yaml",
    "logs/local_store.sqlite3",
    "logs/history",
    "logs/articles.json",
    "logs/article_import_batches.json",
    "logs/article_reference_index",
    "logs/domain_overrides.json",
    "logs/domain_media_names.json",
    "logs/excluded_article_urls.json",
    "logs/account_crawl_state.json",
    "logs/diagnostics.json",
    "user_data/cloud_outbox.json",
    "user_data/cloud_outbox.sqlite3",
    "user_data/daily_task_status.json",
    "user_data/runtime_state.json",
    "user_data/scheduler_state.json",
    "user_data/scheduler_cycle_state.json",
)


def current_account_profile_dir(session_store: CloudSessionStore | None = None) -> Path | None:
    session = (session_store or CloudSessionStore()).load()
    return account_profile_dir_from_session(session)


def account_profile_dir_from_session(session: dict[str, Any] | None) -> Path | None:
    profile_key = account_profile_key(session)
    if not profile_key:
        return None
    return ACCOUNT_PROFILE_ROOT / profile_key


def account_profile_key(session: dict[str, Any] | None) -> str:
    payload = session if isinstance(session, dict) else {}
    base_url = normalize_cloud_base_url(payload.get("base_url"))
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    workspace_id = str(user.get("workspace_id") or "").strip()
    user_id = str(user.get("id") or "").strip()
    if not base_url or not workspace_id or not user_id:
        return ""

    host = urlparse(base_url).netloc or base_url.replace("https://", "").replace("http://", "")
    host_slug = _slug(host) or "cloud"
    raw = f"{base_url}|{workspace_id}|{user_id}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"{host_slug}_workspace{workspace_id}_user{user_id}_{digest}"


def account_scoped_path(relative_path: str | Path, *, fallback: Path | None = None) -> Path:
    target = Path(relative_path)
    if target.is_absolute():
        return target
    profile_dir = current_account_profile_dir()
    if profile_dir is None:
        return fallback or resolve_app_path(target)
    return profile_dir / target


def current_account_config_path(session_store: CloudSessionStore | None = None) -> Path:
    profile_dir = current_account_profile_dir(session_store)
    if profile_dir is None:
        return get_data_root() / "user_data" / "anonymous" / "config.yaml"
    return profile_dir / "config.yaml"


def ensure_current_account_space(session_store: CloudSessionStore | None = None, *, copy_legacy: bool = True) -> Path | None:
    store = session_store or CloudSessionStore()
    session = store.load()
    return ensure_account_space(session, copy_legacy=copy_legacy)


def ensure_account_space(session: dict[str, Any] | None, *, copy_legacy: bool = True) -> Path | None:
    profile_dir = account_profile_dir_from_session(session)
    if profile_dir is None:
        return None

    profile_dir.mkdir(parents=True, exist_ok=True)
    marker_path = profile_dir / "profile_meta.json"
    with CrossProcessRLock(_profile_meta_lock_file(marker_path)):
        existing_meta = _read_json_object(marker_path)
        legacy_copied = False
        if copy_legacy and not marker_path.exists():
            _copy_legacy_account_files(profile_dir)
            legacy_copied = True

        meta = _build_profile_meta(session)
        meta["initialized_at"] = existing_meta.get("initialized_at") or _utc_now_text()
        meta["legacy_copied"] = existing_meta.get("legacy_copied", legacy_copied)
        _write_json_if_changed(marker_path, meta)
    return profile_dir


def _copy_legacy_account_files(profile_dir: Path) -> None:
    for relative in ACCOUNT_SCOPED_RELATIVE_PATHS:
        src = get_data_root() / relative
        dst = profile_dir / relative
        _copy_missing(src, dst)


def _copy_missing(src: Path, dst: Path) -> None:
    if not src.exists() or dst.exists():
        return
    try:
        if src.is_dir():
            shutil.copytree(src, dst)
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    except Exception:
        # Migration should never block login. Cloud pull can rebuild account state.
        pass


def _build_profile_meta(session: dict[str, Any] | None) -> dict[str, Any]:
    payload = session if isinstance(session, dict) else {}
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    return {
        "base_url": normalize_cloud_base_url(payload.get("base_url")),
        "workspace_id": user.get("workspace_id"),
        "user_id": user.get("id"),
        "username": user.get("username"),
        "role": user.get("role"),
        "updated_at": _utc_now_text(),
    }


def merge_profile_meta(profile_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Merge account profile metadata with a path-scoped lock and atomic write."""
    return _write_json_if_changed(Path(profile_dir) / "profile_meta.json", payload)


def _write_json_if_changed(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    with CrossProcessRLock(_profile_meta_lock_file(path)):
        existing = _read_json_object(path)
        merged = dict(existing)
        merged.update(payload)
        if merged == existing:
            return merged
        _atomic_write_json(path, merged)
        return merged


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _profile_meta_lock_file(path: Path) -> Path:
    return path.with_name(f"{path.name}.lock")


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        existing_data = json.loads(path.read_text(encoding="utf-8"))
        return existing_data if isinstance(existing_data, dict) else {}
    except Exception:
        return {}


def _slug(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return text[:48]


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
