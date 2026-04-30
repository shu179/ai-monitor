"""浏览器运行时探测工具。"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from core.app_paths import get_app_root


def current_browser_bundle_platform() -> str:
    machine = os.uname().machine.lower() if hasattr(os, "uname") else ""
    if os.name == "nt":
        if machine in {"amd64", "x86_64", "x64"}:
            arch = "amd64"
        elif machine in {"arm64", "aarch64"}:
            arch = "arm64"
        else:
            arch = machine or "unknown"
        return f"windows-{arch}"
    if sys.platform == "darwin":
        if machine in {"arm64", "aarch64"}:
            arch = "arm64"
        elif machine in {"x86_64", "amd64"}:
            arch = "amd64"
        else:
            arch = machine or "unknown"
        return f"darwin-{arch}"
    if machine in {"x86_64", "amd64", "x64"}:
        arch = "amd64"
    elif machine in {"arm64", "aarch64"}:
        arch = "arm64"
    else:
        arch = machine or "unknown"
    return f"linux-{arch}"


def resolve_bundled_browser_executable() -> str:
    root = get_app_root()
    candidates: list[Path] = []
    platform_dir = root / "third_party" / "browser" / current_browser_bundle_platform()
    flat_dir = root / "third_party" / "browser"
    for base_dir in (platform_dir, flat_dir):
        if not base_dir.exists():
            continue
        candidates.extend(_browser_candidates_for_dir(base_dir))
    for path in candidates:
        if path.exists():
            return str(path)
    return ""


def resolve_system_browser_executable() -> str:
    env_path = str(os.environ.get("CHROME_EXECUTABLE_PATH") or "").strip()
    candidates: list[str] = [env_path]
    root = get_app_root()
    platform_dir = root / "third_party" / "browser" / current_browser_bundle_platform()
    flat_dir = root / "third_party" / "browser"

    if sys.platform == "darwin":
        candidates.extend([
            str(platform_dir / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome"),
            str(flat_dir / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome"),
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ])
    elif sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", "").strip()
        program_files = os.environ.get("PROGRAMFILES", "").strip()
        program_files_x86 = os.environ.get("PROGRAMFILES(X86)", "").strip()
        candidates.extend([
            os.path.join(program_files, "Google", "Chrome", "Application", "chrome.exe") if program_files else "",
            os.path.join(program_files_x86, "Google", "Chrome", "Application", "chrome.exe") if program_files_x86 else "",
            os.path.join(local_app, "Google", "Chrome", "Application", "chrome.exe") if local_app else "",
        ])
    else:
        candidates.extend([
            shutil.which("google-chrome") or "",
            shutil.which("google-chrome-stable") or "",
        ])

    for path in candidates:
        if path and os.path.exists(path):
            if path != env_path:
                os.environ["CHROME_EXECUTABLE_PATH"] = path
            return path
    return ""


def _browser_candidates_for_dir(base_dir: Path) -> list[Path]:
    if sys.platform == "darwin":
        return [
            base_dir / "Google Chrome for Testing.app" / "Contents" / "MacOS" / "Google Chrome for Testing",
            base_dir / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome",
            base_dir / "Chromium.app" / "Contents" / "MacOS" / "Chromium",
            base_dir / "chrome-mac-arm64" / "Google Chrome for Testing.app" / "Contents" / "MacOS" / "Google Chrome for Testing",
            base_dir / "chrome-mac-x64" / "Google Chrome for Testing.app" / "Contents" / "MacOS" / "Google Chrome for Testing",
            base_dir / "chrome-mac" / "Google Chrome for Testing.app" / "Contents" / "MacOS" / "Google Chrome for Testing",
        ]
    if sys.platform == "win32":
        return [
            base_dir / "chrome.exe",
            base_dir / "chrome-win64" / "chrome.exe",
            base_dir / "chrome-win32" / "chrome.exe",
            base_dir / "chrome-win" / "chrome.exe",
        ]
    return [
        base_dir / "chrome",
        base_dir / "chrome-linux64" / "chrome",
        base_dir / "chrome-linux" / "chrome",
    ]
