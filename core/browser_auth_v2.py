"""浏览器平台账号资料夹管理 - 简化版。

架构说明：
- 所有平台使用统一的 browser_profiles/{platform}/default 路径
- 不再支持多 profile，简化管理
- 自动迁移旧的 auth/ 和 user_data/ 数据
"""

from __future__ import annotations

import json
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from core.app_paths import resolve_app_dir, resolve_app_path

BROWSER_AUTH_PLATFORM_IDS: tuple[str, ...] = (
    "doubao",
    "deepseek",
    "kimi",
    "yuanbao",
    "tongyi",
    "wenxin",
)

_LOCK = threading.RLock()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _normalize_platform_name(platform_name: str) -> str:
    return str(platform_name or "").strip().lower()


def _get_platform_profile_path(platform_name: str) -> Path:
    """返回平台的浏览器环境路径（统一结构）"""
    return resolve_app_path(f"browser_profiles/{platform_name}/default")


def _get_legacy_auth_path(platform_name: str) -> Path:
    """返回旧版 auth/ 路径"""
    return resolve_app_path(f"auth/{platform_name}")


def _get_shared_user_data_path() -> Path:
    """返回共享的 user_data/ 路径（已废弃）"""
    return resolve_app_path("user_data")


def _delete_path_tree(path: Path) -> None:
    """递归删除目录或文件"""
    try:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
            return
    except Exception:
        pass
    if not path.exists():
        return
    shutil.rmtree(path, ignore_errors=True)


def _has_any_files(path: Path) -> bool:
    """检查目录是否有文件"""
    if not path.exists():
        return False
    try:
        return any(path.iterdir())
    except Exception:
        return False


def _migrate_legacy_profile(platform_name: str) -> bool:
    """迁移旧的浏览器环境到新路径"""
    new_path = _get_platform_profile_path(platform_name)

    # 如果新路径已存在且有文件，不迁移
    if _has_any_files(new_path):
        return False

    # 尝试从 auth/{platform} 迁移
    legacy_path = _get_legacy_auth_path(platform_name)
    if _has_any_files(legacy_path):
        try:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(legacy_path, new_path, dirs_exist_ok=True)
            print(f"[browser_auth] 已迁移 {platform_name} 从 auth/ 到 browser_profiles/")
            return True
        except Exception as e:
            print(f"[browser_auth] 迁移 {platform_name} 失败: {e}")

    return False


def get_active_browser_auth_dir(platform_name: str) -> Path:
    """
    获取平台的浏览器环境路径。

    逻辑：
    1. 优先使用新路径 browser_profiles/{platform}/default
    2. 如果新路径不存在，尝试从旧路径迁移
    3. 如果都不存在，创建新路径
    """
    normalized = _normalize_platform_name(platform_name)
    if not normalized:
        raise ValueError("platform_name 不能为空")

    with _LOCK:
        new_path = _get_platform_profile_path(normalized)

        # 如果新路径不存在，尝试迁移
        if not _has_any_files(new_path):
            _migrate_legacy_profile(normalized)

        # 确保目录存在
        new_path.mkdir(parents=True, exist_ok=True)
        return new_path


def get_browser_auth_snapshot(platform_names: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """获取所有平台的环境状态快照"""
    with _LOCK:
        names = platform_names or list(BROWSER_AUTH_PLATFORM_IDS)
        snapshots = {}

        for platform_name in names:
            normalized = _normalize_platform_name(platform_name)
            if not normalized:
                continue

            new_path = _get_platform_profile_path(normalized)
            legacy_path = _get_legacy_auth_path(normalized)

            # 判断当前使用的路径
            if _has_any_files(new_path):
                active_path = new_path
                path_type = "new"
            elif _has_any_files(legacy_path):
                active_path = legacy_path
                path_type = "legacy"
            else:
                active_path = new_path
                path_type = "empty"

            snapshots[normalized] = {
                "platform": normalized,
                "active_path": str(active_path),
                "path_type": path_type,
                "has_files": _has_any_files(active_path),
                "new_path": str(new_path),
                "legacy_path": str(legacy_path),
            }

        return snapshots


def reset_browser_auth_environment(platform_name: str) -> dict[str, Any]:
    """
    完全重置指定平台的浏览器环境。

    清理范围：
    1. browser_profiles/{platform}/
    2. auth/{platform}/
    3. user_data/ (如果该平台可能在用)
    """
    normalized = _normalize_platform_name(platform_name)
    if not normalized:
        raise ValueError("platform_name 不能为空")

    with _LOCK:
        paths_to_delete = []

        # 1. 新路径的整个平台目录
        new_platform_dir = resolve_app_path(f"browser_profiles/{normalized}")
        if new_platform_dir.exists():
            paths_to_delete.append(new_platform_dir)

        # 2. 旧版 auth/ 路径
        legacy_path = _get_legacy_auth_path(normalized)
        if legacy_path.exists():
            paths_to_delete.append(legacy_path)

        # 3. 共享 user_data/ (只对豆包等早期平台清理)
        # 检查是否有其他平台在用 browser_profiles，如果都没有，说明可能在用 user_data
        browser_profiles_root = resolve_app_path("browser_profiles")
        has_other_platforms = False
        if browser_profiles_root.exists():
            for other_platform in BROWSER_AUTH_PLATFORM_IDS:
                if other_platform == normalized:
                    continue
                other_path = browser_profiles_root / other_platform / "default"
                if _has_any_files(other_path):
                    has_other_platforms = True
                    break

        # 如果没有其他平台使用新路径，且该平台也没有 legacy 路径，说明可能在用 user_data
        shared_user_data = _get_shared_user_data_path()
        should_clean_shared = (
            not has_other_platforms
            and not _has_any_files(legacy_path)
            and _has_any_files(shared_user_data)
        )

        if should_clean_shared:
            # 只删除浏览器相关文件，保留应用配置
            browser_files = [
                "Default", "GrShaderCache", "GraphiteDawnCache",
                "ShaderCache", "component_crx_cache", "extensions_crx_cache",
                "segmentation_platform", "Safe Browsing", "NativeMessagingHosts",
                "ChromeFeatureState", "Last Version", "Local State", "Variations",
                "first_party_sets.db", "first_party_sets.db-journal",
                "Cookies", "Cookies-journal",
            ]
            for name in browser_files:
                browser_file = shared_user_data / name
                if browser_file.exists():
                    paths_to_delete.append(browser_file)

        # 删除所有路径
        deleted_paths = []
        for path in paths_to_delete:
            if path.exists():
                deleted_paths.append(str(path))
                _delete_path_tree(path)

        # 创建新的干净环境
        new_path = _get_platform_profile_path(normalized)
        new_path.mkdir(parents=True, exist_ok=True)

        return {
            "platform": normalized,
            "deleted_paths": deleted_paths,
            "new_path": str(new_path),
            "reset_at": _now_iso(),
        }


def reset_all_browser_auth_environments() -> dict[str, Any]:
    """
    一键重置所有平台的浏览器环境。

    清理范围：
    1. 整个 browser_profiles/ 目录
    2. 整个 auth/ 目录
    3. user_data/ 中的浏览器文件（保留应用配置）
    """
    with _LOCK:
        paths_to_delete = []

        # 1. 整个 browser_profiles/ 目录
        browser_profiles_root = resolve_app_path("browser_profiles")
        if browser_profiles_root.exists():
            paths_to_delete.append(browser_profiles_root)

        # 2. 整个 auth/ 目录
        auth_root = resolve_app_path("auth")
        if auth_root.exists():
            paths_to_delete.append(auth_root)

        # 3. user_data/ 中的浏览器文件（保留应用配置）
        shared_user_data = _get_shared_user_data_path()
        if shared_user_data.exists():
            browser_files = [
                "Default", "GrShaderCache", "GraphiteDawnCache",
                "ShaderCache", "component_crx_cache", "extensions_crx_cache",
                "segmentation_platform", "Safe Browsing", "NativeMessagingHosts",
                "ChromeFeatureState", "Last Version", "Local State", "Variations",
                "first_party_sets.db", "first_party_sets.db-journal",
                "Cookies", "Cookies-journal", "browser_auth_profiles",
            ]
            for name in browser_files:
                browser_file = shared_user_data / name
                if browser_file.exists():
                    paths_to_delete.append(browser_file)

        # 删除所有路径
        deleted_paths = []
        for path in paths_to_delete:
            if path.exists():
                deleted_paths.append(str(path))
                _delete_path_tree(path)

        # 为每个平台创建新环境
        platforms = {}
        for platform_name in BROWSER_AUTH_PLATFORM_IDS:
            new_path = _get_platform_profile_path(platform_name)
            new_path.mkdir(parents=True, exist_ok=True)
            platforms[platform_name] = str(new_path)

        return {
            "deleted_paths": deleted_paths,
            "platforms": platforms,
            "reset_at": _now_iso(),
        }


# 向后兼容的函数（保留旧接口）
def mark_browser_auth_profile_used(platform_name: str) -> dict[str, Any]:
    """标记平台已使用（兼容旧接口）"""
    normalized = _normalize_platform_name(platform_name)
    path = get_active_browser_auth_dir(normalized)
    return {
        "platform": normalized,
        "path": str(path),
        "used_at": _now_iso(),
    }


def mark_browser_auth_profile_authenticated(platform_name: str, authenticated: bool) -> dict[str, Any]:
    """标记平台认证状态（兼容旧接口）"""
    normalized = _normalize_platform_name(platform_name)
    path = get_active_browser_auth_dir(normalized)
    return {
        "platform": normalized,
        "path": str(path),
        "authenticated": authenticated,
        "updated_at": _now_iso(),
    }
