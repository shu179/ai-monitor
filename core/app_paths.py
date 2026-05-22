"""应用资源/数据路径解析工具。"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

_DATA_DIR_ENV = "AIBRANDMONITOR_DATA_DIR"
_APP_DIR_NAME = "AIBrandMonitor"
_DATA_TOP_LEVELS = {
    "auth",
    "browser_profiles",
    "exports",
    "logs",
    "reports",
    "screenshots",
    "user_data",
}
_DATA_FILES = {
    "config.yaml",
    "config.local.yaml",
}
_migration_lock = threading.Lock()
_migration_done = False


def get_app_root() -> Path:
    """返回应用资源根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _get_default_data_root() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / _APP_DIR_NAME
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "").strip()
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / _APP_DIR_NAME
    xdg_data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg_data_home) if xdg_data_home else Path.home() / ".local" / "share"
    return base / _APP_DIR_NAME


def _ensure_writable_root(candidate: Path) -> Path | None:
    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None

    probe_path = None
    try:
        fd, probe_path = tempfile.mkstemp(dir=str(candidate), prefix=".write_probe_", suffix=".tmp")
        os.close(fd)
        os.unlink(probe_path)
        return candidate
    except Exception:
        try:
            if probe_path:
                os.unlink(probe_path)
        except Exception:
            pass
        return None


def get_data_root() -> Path:
    """返回跨源码版/打包版共享的应用数据根目录。"""
    override = os.environ.get(_DATA_DIR_ENV, "").strip()
    candidates = []
    if override:
        candidates.append(Path(override).expanduser())
    else:
        candidates.extend([
            _get_default_data_root(),
            Path.home() / f".{_APP_DIR_NAME}",
            get_app_root() / ".app_data",
            Path(tempfile.gettempdir()) / _APP_DIR_NAME,
        ])

    root = None
    for candidate in candidates:
        writable_root = _ensure_writable_root(candidate)
        if writable_root is not None:
            root = writable_root
            break
    if root is None:
        raise RuntimeError("未找到可写的应用数据目录")
    _ensure_legacy_data_migrated(root)
    return root


def _is_data_relative_path(target: Path) -> bool:
    if target.is_absolute():
        return False
    if target.name in _DATA_FILES:
        return True
    if not target.parts:
        return False
    return target.parts[0] in _DATA_TOP_LEVELS


def _copy_missing(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    if src.is_file():
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        return

    dst.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        _copy_missing(child, dst / child.name)


def _ensure_legacy_data_migrated(data_root: Path) -> None:
    global _migration_done
    if _migration_done:
        return

    with _migration_lock:
        if _migration_done:
            return

        resource_root = get_app_root()
        migrate_targets = sorted(_DATA_TOP_LEVELS) + sorted(_DATA_FILES)
        for relative in migrate_targets:
            src = resource_root / relative
            dst = data_root / relative
            try:
                _copy_missing(src, dst)
            except Exception:
                # 迁移是兼容增强，失败时不阻塞主流程。
                pass

        _migration_done = True


def resolve_app_path(path: str | Path) -> Path:
    """将相对路径解析到资源目录或共享数据目录。"""
    target = Path(path)
    if target.is_absolute():
        return target
    if _is_data_relative_path(target):
        return get_data_root() / target
    return get_app_root() / target


def resolve_app_dir(path: str | Path) -> Path:
    """解析目录路径并确保目录存在。"""
    target = resolve_app_path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target
