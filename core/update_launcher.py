"""
Surfaced 更新器启动辅助。

用途：
- 把 updater 本体复制到临时目录
- 生成独立 updater 启动命令
- 以 detached 模式启动，避免被主程序生命周期绑死
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from core.app_paths import get_app_root
from core.version import APP_NAME


def get_runtime_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return get_app_root()


def build_restart_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve())]
    return [sys.executable, str((get_app_root() / "main.py").resolve())]


def _stage_updater_binary() -> Path:
    source = get_app_root() / "updater.py"
    if not source.exists():
        raise FileNotFoundError(f"未找到 updater.py: {source}")
    temp_dir = Path(tempfile.mkdtemp(prefix="surfaced-updater-"))
    target = temp_dir / source.name
    shutil.copy2(source, target)
    return target


def build_updater_command(
    *,
    source_dir: str | Path,
    target_dir: str | Path | None = None,
    wait_pid: int | None = None,
    cleanup_source: bool = False,
    restart_after_update: bool = True,
) -> list[str]:
    staged_updater = _stage_updater_binary()
    resolved_target = Path(target_dir).expanduser().resolve() if target_dir else get_runtime_app_dir()
    command = [
        sys.executable,
        str(staged_updater),
        "--source-dir",
        str(Path(source_dir).expanduser().resolve()),
        "--target-dir",
        str(resolved_target),
        "--wait-pid",
        str(int(wait_pid or 0)),
    ]
    if cleanup_source:
        command.append("--cleanup-source")
    if restart_after_update:
        command.extend([
            "--restart-cmd-json",
            json.dumps(build_restart_command(), ensure_ascii=False),
        ])
    return command


def launch_updater(
    *,
    source_dir: str | Path,
    target_dir: str | Path | None = None,
    wait_pid: int | None = None,
    cleanup_source: bool = False,
    restart_after_update: bool = True,
) -> subprocess.Popen:
    command = build_updater_command(
        source_dir=source_dir,
        target_dir=target_dir,
        wait_pid=wait_pid,
        cleanup_source=cleanup_source,
        restart_after_update=restart_after_update,
    )
    kwargs: dict[str, object] = {
        "cwd": str(Path(command[1]).resolve().parent),
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)  # noqa: S603


def build_update_plan_payload(source_dir: str | Path) -> dict[str, object]:
    source = Path(source_dir).expanduser().resolve()
    target = get_runtime_app_dir()
    return {
        "appName": APP_NAME,
        "sourceDir": str(source),
        "targetDir": str(target),
        "waitPid": int(os.getpid()),
        "restartCommand": build_restart_command(),
        "requiresAppExit": True,
        "strategy": "external_updater",
    }
