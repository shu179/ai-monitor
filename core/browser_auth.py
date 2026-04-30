"""浏览器平台账号资料夹管理。"""

from __future__ import annotations

import json
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from core.app_paths import resolve_app_dir, resolve_app_path
from core.browser_processes import is_browser_profile_in_use

BROWSER_AUTH_PLATFORM_IDS: tuple[str, ...] = (
    "doubao",
    "deepseek",
    "kimi",
    "yuanbao",
    "tongyi",
    "wenxin",
)

_METADATA_PATH = resolve_app_path("user_data/browser_auth_profiles.json")
_PROFILE_ROOT = resolve_app_dir("user_data/browser_auth_profiles")
_LOCK = threading.RLock()

_SAFE_BROWSER_CACHE_RELATIVE_PATHS: tuple[str, ...] = (
    "BrowserMetrics",
    "BrowserMetrics-spare.pma",
    "Crashpad",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "GraphiteDawnCache",
    "GrShaderCache",
    "GPUCache",
    "ShaderCache",
    "SingletonCookie",
    "SingletonLock",
    "SingletonSocket",
    "Default/Cache",
    "Default/Code Cache",
    "Default/DawnGraphiteCache",
    "Default/DawnWebGPUCache",
    "Default/GPUCache",
    "Default/ShaderCache",
)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _normalize_platform_name(platform_name: str) -> str:
    text = str(platform_name or "").strip().lower()
    return text


def _load_metadata() -> dict[str, Any]:
    if not _METADATA_PATH.exists():
        return {"platforms": {}}
    try:
        payload = json.loads(_METADATA_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"platforms": {}}
    if not isinstance(payload, dict):
        return {"platforms": {}}
    platforms = payload.get("platforms")
    if not isinstance(platforms, dict):
        payload["platforms"] = {}
    return payload


def _save_metadata(metadata: dict[str, Any]) -> None:
    _METADATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    _METADATA_PATH.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _platform_bucket(metadata: dict[str, Any], platform_name: str) -> dict[str, Any]:
    platforms = metadata.setdefault("platforms", {})
    bucket = platforms.setdefault(platform_name, {})
    if not isinstance(bucket, dict):
        bucket = {}
        platforms[platform_name] = bucket
    profiles = bucket.setdefault("profiles", {})
    if not isinstance(profiles, dict):
        profiles = {}
        bucket["profiles"] = profiles
    return bucket


def _legacy_profile_relative_path(platform_name: str) -> str:
    return f"auth/{platform_name}"


def _fresh_profile_relative_path(platform_name: str, profile_id: str) -> str:
    return f"user_data/browser_auth_profiles/{platform_name}/{profile_id}"


def _resolve_profile_path(relative_path: str) -> Path:
    return resolve_app_path(relative_path)


def _has_any_profile_files(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        return any(path.iterdir())
    except Exception:
        return False


def _delete_path_tree(path: Path) -> None:
    try:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
            return
    except Exception:
        pass
    if not path.exists():
        return
    shutil.rmtree(path, ignore_errors=True)


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _is_safe_browser_profile_path(path: Path) -> bool:
    resolved = path.resolve()
    safe_roots = (
        resolve_app_path("user_data").resolve(),
        resolve_app_path("auth").resolve(),
    )
    return any(_is_relative_to(resolved, root) for root in safe_roots)


def cleanup_browser_runtime_cache(profile_path: str | Path) -> dict[str, Any]:
    """只清理浏览器可再生缓存/锁文件，保留 Cookie、Local Storage 等登录数据。"""
    path = Path(profile_path).expanduser()
    if not path.exists():
        return {"ok": True, "removed": 0, "skipped": True, "reason": "profile_not_found"}
    if not path.is_dir() or not _is_safe_browser_profile_path(path):
        return {"ok": False, "removed": 0, "skipped": True, "reason": "unsafe_profile_path"}
    if is_browser_profile_in_use(path):
        return {"ok": True, "removed": 0, "skipped": True, "reason": "profile_in_use"}

    removed = 0
    for relative_path in _SAFE_BROWSER_CACHE_RELATIVE_PATHS:
        target = (path / relative_path).resolve()
        if not _is_relative_to(target, path.resolve()) or not target.exists():
            continue
        try:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
            removed += 1
        except Exception:
            continue
    return {"ok": True, "removed": removed, "skipped": False, "reason": ""}


def _ensure_legacy_profile_entry(metadata: dict[str, Any], platform_name: str) -> bool:
    bucket = _platform_bucket(metadata, platform_name)
    profiles = bucket["profiles"]
    changed = False
    legacy_path = resolve_app_path(_legacy_profile_relative_path(platform_name))
    if not _has_any_profile_files(legacy_path):
        if legacy_path.exists():
            _delete_path_tree(legacy_path)
        if "legacy" in profiles:
            profiles.pop("legacy", None)
            changed = True
        if str(bucket.get("active_profile_id") or "").strip() == "legacy":
            bucket["active_profile_id"] = ""
            changed = True
        return changed
    if "legacy" not in profiles:
        profiles["legacy"] = {
            "id": "legacy",
            "label": "旧环境",
            "relative_path": _legacy_profile_relative_path(platform_name),
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "kind": "legacy",
        }
        changed = True
    if not str(bucket.get("active_profile_id") or "").strip():
        bucket["active_profile_id"] = "legacy"
        changed = True
    return changed


def _ensure_active_profile(metadata: dict[str, Any], platform_name: str) -> tuple[dict[str, Any], bool]:
    bucket = _platform_bucket(metadata, platform_name)
    changed = _ensure_legacy_profile_entry(metadata, platform_name)
    profiles = bucket["profiles"]
    active_profile_id = str(bucket.get("active_profile_id") or "").strip()
    if active_profile_id and isinstance(profiles.get(active_profile_id), dict):
        return profiles[active_profile_id], changed

    if profiles:
        first_profile_id = next(iter(profiles.keys()))
        bucket["active_profile_id"] = first_profile_id
        return profiles[first_profile_id], True

    profile_id = "default"
    relative_path = _fresh_profile_relative_path(platform_name, profile_id)
    _resolve_profile_path(relative_path).mkdir(parents=True, exist_ok=True)
    profiles[profile_id] = {
        "id": profile_id,
        "label": "默认环境",
        "relative_path": relative_path,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "kind": "fresh",
        "authenticated": False,
    }
    bucket["active_profile_id"] = profile_id
    return profiles[profile_id], True


def _build_profile_snapshot(profile: dict[str, Any], active_profile_id: str) -> dict[str, Any]:
    profile_id = str(profile.get("id") or "").strip()
    relative_path = str(profile.get("relative_path") or "").strip()
    path = _resolve_profile_path(relative_path)
    has_files = _has_any_profile_files(path)
    authenticated_raw = profile.get("authenticated")
    if authenticated_raw is True:
        auth_state = "authenticated"
    elif authenticated_raw is False:
        auth_state = "logged_out"
    elif has_files:
        auth_state = "needs_confirmation"
    else:
        auth_state = "logged_out"
    return {
        "id": profile_id,
        "label": str(profile.get("label") or profile_id or "未命名环境").strip(),
        "relative_path": relative_path,
        "absolute_path": str(path),
        "created_at": str(profile.get("created_at") or ""),
        "updated_at": str(profile.get("updated_at") or ""),
        "last_used_at": str(profile.get("last_used_at") or ""),
        "kind": str(profile.get("kind") or "fresh"),
        "has_files": has_files,
        "authenticated": authenticated_raw is True,
        "auth_state": auth_state,
        "is_active": profile_id == active_profile_id,
    }


def _build_platform_snapshot(metadata: dict[str, Any], platform_name: str) -> dict[str, Any]:
    bucket = _platform_bucket(metadata, platform_name)
    active_profile_id = str(bucket.get("active_profile_id") or "").strip()
    profiles = []
    for profile in bucket.get("profiles", {}).values():
        if isinstance(profile, dict):
            profiles.append(_build_profile_snapshot(profile, active_profile_id))
    profiles.sort(
        key=lambda item: (
            not bool(item.get("is_active")),
            item.get("last_used_at") or "",
            item.get("created_at") or "",
            item.get("id") or "",
        ),
        reverse=False,
    )
    active_profile = next((item for item in profiles if item.get("is_active")), None)
    return {
        "platform": platform_name,
        "active_profile_id": active_profile_id,
        "active_profile": active_profile,
        "profiles": profiles,
    }


def get_browser_auth_snapshot(
    platform_names: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    with _LOCK:
        metadata = _load_metadata()
        changed = False
        names = [
            _normalize_platform_name(name)
            for name in (platform_names or BROWSER_AUTH_PLATFORM_IDS)
            if _normalize_platform_name(name)
        ]
        snapshots: dict[str, dict[str, Any]] = {}
        for platform_name in names:
            _, bucket_changed = _ensure_active_profile(metadata, platform_name)
            changed = changed or bucket_changed
            snapshots[platform_name] = _build_platform_snapshot(metadata, platform_name)
        if changed:
            _save_metadata(metadata)
        return snapshots


def get_active_browser_auth_dir(platform_name: str) -> Path:
    normalized = _normalize_platform_name(platform_name)
    if not normalized:
        raise ValueError("platform_name 不能为空")
    with _LOCK:
        metadata = _load_metadata()
        profile, changed = _ensure_active_profile(metadata, normalized)
        if changed:
            _save_metadata(metadata)
        relative_path = str(profile.get("relative_path") or "").strip()
        path = _resolve_profile_path(relative_path)
        path.mkdir(parents=True, exist_ok=True)
        return path


def mark_browser_auth_profile_used(platform_name: str) -> dict[str, Any]:
    normalized = _normalize_platform_name(platform_name)
    if not normalized:
        raise ValueError("platform_name 不能为空")
    with _LOCK:
        metadata = _load_metadata()
        profile, _ = _ensure_active_profile(metadata, normalized)
        profile["last_used_at"] = _now_iso()
        profile["updated_at"] = _now_iso()
        _save_metadata(metadata)
        return _build_platform_snapshot(metadata, normalized)


def mark_browser_auth_profile_authenticated(
    platform_name: str,
    authenticated: bool,
) -> dict[str, Any]:
    normalized = _normalize_platform_name(platform_name)
    if not normalized:
        raise ValueError("platform_name 不能为空")
    with _LOCK:
        metadata = _load_metadata()
        profile, _ = _ensure_active_profile(metadata, normalized)
        profile["authenticated"] = bool(authenticated)
        profile["updated_at"] = _now_iso()
        if authenticated:
            profile["last_authenticated_at"] = _now_iso()
        _save_metadata(metadata)
        return _build_platform_snapshot(metadata, normalized)


def activate_browser_auth_profile(platform_name: str, profile_id: str) -> dict[str, Any]:
    normalized = _normalize_platform_name(platform_name)
    target_profile_id = str(profile_id or "").strip()
    if not normalized or not target_profile_id:
        raise ValueError("platform_name 和 profile_id 不能为空")
    with _LOCK:
        metadata = _load_metadata()
        _ensure_active_profile(metadata, normalized)
        bucket = _platform_bucket(metadata, normalized)
        profiles = bucket["profiles"]
        profile = profiles.get(target_profile_id)
        if not isinstance(profile, dict):
            raise KeyError(target_profile_id)
        bucket["active_profile_id"] = target_profile_id
        profile["last_used_at"] = _now_iso()
        profile["updated_at"] = _now_iso()
        path = _resolve_profile_path(str(profile.get("relative_path") or ""))
        path.mkdir(parents=True, exist_ok=True)
        _save_metadata(metadata)
        return _build_platform_snapshot(metadata, normalized)


def create_fresh_browser_auth_profile(
    platform_name: str,
    *,
    label: str = "",
) -> dict[str, Any]:
    normalized = _normalize_platform_name(platform_name)
    if not normalized:
        raise ValueError("platform_name 不能为空")
    with _LOCK:
        metadata = _load_metadata()
        _ensure_active_profile(metadata, normalized)
        bucket = _platform_bucket(metadata, normalized)
        profiles = bucket["profiles"]
        profile_id = f"profile-{uuid4().hex[:8]}"
        relative_path = _fresh_profile_relative_path(normalized, profile_id)
        path = _resolve_profile_path(relative_path)
        path.mkdir(parents=True, exist_ok=True)
        now = _now_iso()
        profiles[profile_id] = {
            "id": profile_id,
            "label": str(label or f"新环境 {datetime.now().strftime('%m-%d %H:%M')}").strip(),
            "relative_path": relative_path,
            "created_at": now,
            "updated_at": now,
            "last_used_at": now,
            "kind": "fresh",
            "authenticated": False,
        }
        bucket["active_profile_id"] = profile_id
        _save_metadata(metadata)
        return _build_platform_snapshot(metadata, normalized)


def clear_browser_auth_profile_data(
    platform_name: str,
    *,
    profile_id: str | None = None,
) -> dict[str, Any]:
    normalized = _normalize_platform_name(platform_name)
    if not normalized:
        raise ValueError("platform_name 不能为空")
    with _LOCK:
        metadata = _load_metadata()
        active_profile, _ = _ensure_active_profile(metadata, normalized)
        bucket = _platform_bucket(metadata, normalized)
        target_id = str(profile_id or bucket.get("active_profile_id") or active_profile.get("id") or "").strip()
        profile = bucket["profiles"].get(target_id)
        if not isinstance(profile, dict):
            raise KeyError(target_id)
        path = _resolve_profile_path(str(profile.get("relative_path") or ""))
        if path.exists():
            for child in list(path.iterdir()):
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    try:
                        child.unlink()
                    except FileNotFoundError:
                        pass
        path.mkdir(parents=True, exist_ok=True)
        profile["updated_at"] = _now_iso()
        profile["last_cleared_at"] = _now_iso()
        profile["authenticated"] = False
        _save_metadata(metadata)
        return _build_platform_snapshot(metadata, normalized)


def reset_browser_auth_environment(platform_name: str) -> dict[str, Any]:
    """
    完全重置指定平台的浏览器环境，清除所有旧数据。

    清理范围：
    1. 所有已注册的 profile 目录（legacy + fresh）
    2. 新版 profile 根目录下的平台子目录
    3. 旧版 auth/{platform} 目录
    4. 共享的 user_data/ 目录（如果存在且被该平台使用）

    清理后会创建一个全新的 profile 环境。
    """
    normalized = _normalize_platform_name(platform_name)
    if not normalized:
        raise ValueError("platform_name 不能为空")

    with _LOCK:
        metadata = _load_metadata()
        bucket = _platform_bucket(metadata, normalized)
        profiles = bucket.get("profiles", {}) or {}

        paths_to_delete: list[Path] = []

        # 1. 收集所有已注册的 profile 路径
        for profile in list(profiles.values()):
            if not isinstance(profile, dict):
                continue
            relative_path = str(profile.get("relative_path") or "").strip()
            if relative_path:
                paths_to_delete.append(_resolve_profile_path(relative_path))

        # 2. 兜底清理：新版 profile 根目录下的平台子目录
        paths_to_delete.append(_PROFILE_ROOT / normalized)

        # 3. 兜底清理：旧版 auth/{platform} 目录
        paths_to_delete.append(resolve_app_path(_legacy_profile_relative_path(normalized)))

        # 4. 特殊处理：清理共享的 user_data/ 目录（豆包等平台可能在用）
        # 只有当该平台的 profile 指向 user_data/ 或没有独立目录时才清理
        shared_user_data = resolve_app_path("user_data")
        should_clean_shared = False

        # 检查是否有 profile 使用了 user_data 或其子目录
        for profile in profiles.values():
            if not isinstance(profile, dict):
                continue
            rel_path = str(profile.get("relative_path") or "").strip()
            if rel_path.startswith("user_data/") or rel_path == "user_data":
                should_clean_shared = True
                break

        # 如果没有明确的 profile 配置，且 auth/{platform} 不存在，说明可能在用 user_data
        if not profiles and not resolve_app_path(_legacy_profile_relative_path(normalized)).exists():
            should_clean_shared = True

        if should_clean_shared and shared_user_data.exists():
            paths_to_delete.append(shared_user_data)

        # 去重后删除所有目录
        deduped_paths: list[Path] = []
        seen = set()
        for path in paths_to_delete:
            key = str(path.resolve()) if path.exists() else str(path)
            if key in seen:
                continue
            seen.add(key)
            deduped_paths.append(path)

        deleted_paths = []
        for path in deduped_paths:
            if path.exists():
                deleted_paths.append(str(path))
                _delete_path_tree(path)

        # 创建全新的 profile 环境
        now = _now_iso()
        profile_id = f"profile-{uuid4().hex[:8]}"
        relative_path = _fresh_profile_relative_path(normalized, profile_id)
        fresh_path = _resolve_profile_path(relative_path)
        fresh_path.mkdir(parents=True, exist_ok=True)

        bucket["profiles"] = {
            profile_id: {
                "id": profile_id,
                "label": "默认环境",
                "relative_path": relative_path,
                "created_at": now,
                "updated_at": now,
                "last_used_at": "",
                "kind": "fresh",
                "authenticated": False,
                "last_reset_at": now,
                "reset_deleted_paths": deleted_paths,
            }
        }
        bucket["active_profile_id"] = profile_id
        _save_metadata(metadata)

        snapshot = _build_platform_snapshot(metadata, normalized)
        snapshot["reset_info"] = {
            "deleted_paths": deleted_paths,
            "new_profile_path": str(fresh_path),
        }
        return snapshot


def reset_all_browser_auth_environments() -> dict[str, Any]:
    """
    一键重置所有平台的浏览器环境。

    清理范围：
    1. 所有平台的 profile 目录
    2. 整个 user_data/ 目录（包括共享数据）
    3. 整个 auth/ 目录（旧版）
    4. metadata 配置文件

    清理后为每个平台创建全新的 profile 环境。
    """
    with _LOCK:
        # 收集所有需要删除的路径
        paths_to_delete: list[Path] = [
            resolve_app_path("user_data"),  # 共享的 user_data 目录
            resolve_app_path("auth"),       # 旧版 auth 目录
            _METADATA_PATH,                 # metadata 配置文件
        ]

        deleted_paths = []
        for path in paths_to_delete:
            if path.exists():
                deleted_paths.append(str(path))
                _delete_path_tree(path)

        # 为每个平台创建全新的环境
        metadata = {"platforms": {}}
        now = _now_iso()
        results = {}

        for platform_name in BROWSER_AUTH_PLATFORM_IDS:
            bucket = _platform_bucket(metadata, platform_name)
            profile_id = f"profile-{uuid4().hex[:8]}"
            relative_path = _fresh_profile_relative_path(platform_name, profile_id)
            fresh_path = _resolve_profile_path(relative_path)
            fresh_path.mkdir(parents=True, exist_ok=True)

            bucket["profiles"] = {
                profile_id: {
                    "id": profile_id,
                    "label": "默认环境",
                    "relative_path": relative_path,
                    "created_at": now,
                    "updated_at": now,
                    "last_used_at": "",
                    "kind": "fresh",
                    "authenticated": False,
                    "last_reset_at": now,
                }
            }
            bucket["active_profile_id"] = profile_id
            results[platform_name] = {
                "profile_path": str(fresh_path),
                "profile_id": profile_id,
            }

        _save_metadata(metadata)

        return {
            "deleted_paths": deleted_paths,
            "platforms": results,
        }
