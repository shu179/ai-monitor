"""
Surfaced 独立更新器。

职责：
1. 等待主程序退出
2. 将已准备好的新版本目录替换到目标目录
3. 可选重启主程序

注意：
- 这里不负责下载更新包，只负责“退出后替换”
- 建议由主程序把 updater 复制到临时目录后再启动，避免更新时覆盖到运行中的 updater 本体
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Surfaced Updater")
    parser.add_argument("--source-dir", required=True, help="已解压的新版本目录")
    parser.add_argument("--target-dir", required=True, help="当前程序目录")
    parser.add_argument("--wait-pid", type=int, default=0, help="等待退出的主程序 PID")
    parser.add_argument("--backup-dir", default="", help="备份目录，不传则自动生成")
    parser.add_argument("--log-file", default="", help="日志文件路径")
    parser.add_argument("--cleanup-source", action="store_true", help="更新完成后删除 source-dir")
    parser.add_argument("--restart-cmd-json", default="", help="重启命令 JSON 数组")
    parser.add_argument("--wait-timeout-seconds", type=int, default=120, help="等待主程序退出的超时时间")
    return parser.parse_args(argv)


def _default_log_file(target_dir: Path) -> Path:
    return target_dir.parent / "surfaced-updater.log"


def _log(message: str, *, log_file: Path | None = None) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    if log_file is None:
        return
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                check=False,
            )
            output = (result.stdout or "") + (result.stderr or "")
            return str(pid) in output
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def wait_for_process_exit(pid: int, timeout_seconds: int, *, log_file: Path | None = None) -> bool:
    if pid <= 0:
        return True
    started = time.time()
    while time.time() - started < max(1, timeout_seconds):
        if not _pid_exists(pid):
            _log(f"[Updater] 主程序已退出: pid={pid}", log_file=log_file)
            return True
        time.sleep(1)
    _log(f"[Updater] 等待主程序退出超时: pid={pid}", log_file=log_file)
    return False


def _safe_rmtree(path: Path, *, log_file: Path | None = None) -> None:
    if not path.exists():
        return
    if path.is_file():
        path.unlink(missing_ok=True)
        return
    shutil.rmtree(path, ignore_errors=False)
    _log(f"[Updater] 已删除目录: {path}", log_file=log_file)


def _build_backup_dir(target_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return target_dir.parent / f"{target_dir.name}.backup.{stamp}"


def replace_app_dir(
    source_dir: Path,
    target_dir: Path,
    backup_dir: Path,
    *,
    cleanup_source: bool = False,
    log_file: Path | None = None,
) -> None:
    if not source_dir.exists() or not source_dir.is_dir():
        raise FileNotFoundError(f"source-dir 不存在或不是目录: {source_dir}")

    backup_parent = backup_dir.parent
    backup_parent.mkdir(parents=True, exist_ok=True)

    backup_done = False
    try:
        if target_dir.exists():
            _log(f"[Updater] 备份当前版本到: {backup_dir}", log_file=log_file)
            if backup_dir.exists():
                _safe_rmtree(backup_dir, log_file=log_file)
            shutil.move(str(target_dir), str(backup_dir))
            backup_done = True

        _log(f"[Updater] 安装新版本: {source_dir} -> {target_dir}", log_file=log_file)
        shutil.copytree(source_dir, target_dir)

        if cleanup_source:
            try:
                _safe_rmtree(source_dir, log_file=log_file)
            except Exception as exc:
                _log(f"[Updater] 清理解压目录失败，已忽略: {exc}", log_file=log_file)
    except Exception:
        if not target_dir.exists() and backup_done and backup_dir.exists():
            _log("[Updater] 安装失败，回滚到旧版本", log_file=log_file)
            shutil.move(str(backup_dir), str(target_dir))
            backup_done = False
        raise


def restart_app(restart_cmd: list[str], *, log_file: Path | None = None) -> None:
    if not restart_cmd:
        return
    kwargs: dict[str, object] = {
        "cwd": str(Path(restart_cmd[0]).resolve().parent) if Path(restart_cmd[0]).exists() else None,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    kwargs = {key: value for key, value in kwargs.items() if value is not None}
    subprocess.Popen(restart_cmd, **kwargs)  # noqa: S603
    _log(f"[Updater] 已请求重启程序: {' '.join(restart_cmd)}", log_file=log_file)


def _parse_restart_cmd(raw: str) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    value = json.loads(text)
    if not isinstance(value, list):
        raise ValueError("restart-cmd-json 必须是 JSON 数组")
    return [str(item) for item in value if str(item).strip()]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source_dir = Path(args.source_dir).expanduser().resolve()
    target_dir = Path(args.target_dir).expanduser().resolve()
    backup_dir = (
        Path(args.backup_dir).expanduser().resolve()
        if str(args.backup_dir or "").strip()
        else _build_backup_dir(target_dir)
    )
    log_file = (
        Path(args.log_file).expanduser().resolve()
        if str(args.log_file or "").strip()
        else _default_log_file(target_dir)
    )

    try:
        restart_cmd = _parse_restart_cmd(args.restart_cmd_json)
    except Exception as exc:
        _log(f"[Updater] 解析重启命令失败: {exc}", log_file=log_file)
        return 2

    _log("[Updater] 启动", log_file=log_file)
    _log(f"[Updater] source={source_dir}", log_file=log_file)
    _log(f"[Updater] target={target_dir}", log_file=log_file)

    if args.wait_pid > 0:
        ok = wait_for_process_exit(args.wait_pid, args.wait_timeout_seconds, log_file=log_file)
        if not ok:
            return 3

    try:
        replace_app_dir(
            source_dir,
            target_dir,
            backup_dir,
            cleanup_source=bool(args.cleanup_source),
            log_file=log_file,
        )
        _log("[Updater] 更新完成", log_file=log_file)
        if restart_cmd:
            restart_app(restart_cmd, log_file=log_file)
        return 0
    except Exception as exc:
        _log(f"[Updater] 更新失败: {exc}", log_file=log_file)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
