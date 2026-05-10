"""Cross-platform helpers for browser profile process management."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .diagnostic_events import record_event_safe


def _normalize_path(path: str | Path) -> str:
    try:
        return str(Path(path).expanduser().resolve()).casefold()
    except Exception:
        return str(path or "").strip().casefold()


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    return True


def process_parent_pid(pid: int) -> int:
    target_pid = int(pid or 0)
    if target_pid <= 0:
        return 0
    try:
        import psutil  # type: ignore

        return int(psutil.Process(target_pid).ppid())
    except Exception:
        pass
    if sys.platform == "win32":
        script = (
            "$p = Get-CimInstance Win32_Process -Filter "
            f"'ProcessId = {target_pid}'; if ($p) {{ $p.ParentProcessId }}"
        )
        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
                check=False,
            )
            return int(str(completed.stdout or "").strip() or 0)
        except Exception:
            return 0
    try:
        completed = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(target_pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
            check=False,
        )
        return int(str(completed.stdout or "").strip() or 0)
    except Exception:
        return 0


def _extract_user_data_dirs(cmdline_items: list[str]) -> list[str]:
    dirs: list[str] = []
    for index, item in enumerate(cmdline_items):
        text = str(item or "").strip()
        lowered = text.casefold()
        if lowered.startswith("--user-data-dir="):
            dirs.append(text.split("=", 1)[1].strip().strip('"'))
        elif lowered == "--user-data-dir" and index + 1 < len(cmdline_items):
            dirs.append(str(cmdline_items[index + 1] or "").strip().strip('"'))
    return [path for path in dirs if path]


def _profile_owner_pids_psutil(profile_path: str) -> list[int]:
    try:
        import psutil  # type: ignore
    except Exception:
        return []
    target = _normalize_path(profile_path)
    if not target:
        return []
    pids: list[int] = []
    seen: set[int] = set()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            pid = int(proc.info.get("pid") or 0)
            if pid <= 0 or pid in seen or pid == os.getpid():
                continue
            name = str(proc.info.get("name") or "").casefold()
            cmdline_items = [str(item or "") for item in (proc.info.get("cmdline") or [])]
        except Exception:
            continue
        haystack = "\n".join(cmdline_items).casefold()
        if "chrome" not in name and "chromium" not in name and "msedge" not in name and "--user-data-dir" not in haystack:
            continue
        user_data_dirs = [_normalize_path(path) for path in _extract_user_data_dirs(cmdline_items)]
        if target in haystack or target in user_data_dirs:
            seen.add(pid)
            pids.append(pid)
    return pids


def _profile_owner_pids_lsof(profile_path: str) -> list[int]:
    try:
        completed = subprocess.run(
            ["lsof", "-t", "+D", profile_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return []
    pids: list[int] = []
    seen: set[int] = set()
    for line in (completed.stdout or "").splitlines():
        try:
            pid = int(str(line or "").strip())
        except Exception:
            continue
        if pid > 0 and pid not in seen and pid != os.getpid():
            seen.add(pid)
            pids.append(pid)
    return pids


def _profile_owner_pids_windows_powershell(profile_path: str) -> list[int]:
    target = str(profile_path or "").strip()
    if not target:
        return []
    script = rf"""
$target = [System.IO.Path]::GetFullPath($args[0]).TrimEnd('\').ToLowerInvariant()
Get-CimInstance Win32_Process |
  Where-Object {{ $_.CommandLine -and ($_.Name -match 'chrome|chromium|msedge') }} |
  ForEach-Object {{
    $cmd = $_.CommandLine.ToLowerInvariant()
    if ($cmd.Contains('--user-data-dir')) {{
      $matches = [regex]::Matches($_.CommandLine, '--user-data-dir=(""[^""]+""|\S+)')
      foreach ($match in $matches) {{
        $raw = $match.Groups[1].Value.Trim('""')
        try {{ $dir = [System.IO.Path]::GetFullPath($raw).TrimEnd('\').ToLowerInvariant() }} catch {{ $dir = $raw.ToLowerInvariant() }}
        if ($dir -eq $target) {{ $_.ProcessId; break }}
      }}
    }}
  }}
"""
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script, target],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return []
    pids: list[int] = []
    seen: set[int] = set()
    for line in (completed.stdout or "").splitlines():
        try:
            pid = int(str(line or "").strip())
        except Exception:
            continue
        if pid > 0 and pid not in seen and pid != os.getpid():
            seen.add(pid)
            pids.append(pid)
    return pids


def browser_profile_owner_pids(profile_path: str | Path) -> list[int]:
    target = str(profile_path or "").strip()
    if not target:
        return []
    pids = _profile_owner_pids_psutil(target)
    if pids:
        return pids
    if sys.platform == "win32":
        return _profile_owner_pids_windows_powershell(target)
    return _profile_owner_pids_lsof(target)


def is_browser_profile_in_use(profile_path: str | Path) -> bool:
    return bool(browser_profile_owner_pids(profile_path))


def browser_profile_process_tree_is_orphaned(profile_path: str | Path) -> bool:
    pids = set(browser_profile_owner_pids(profile_path))
    if not pids:
        return False
    for pid in pids:
        parent_pid = process_parent_pid(pid)
        if parent_pid > 1 and parent_pid not in pids:
            return False
    return True


def wait_for_pids_exit(pids: list[int], timeout_seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout_seconds or 0.0))
    remaining = [int(pid) for pid in (pids or []) if isinstance(pid, int) and pid > 0]
    if not remaining:
        return True
    while time.monotonic() < deadline:
        alive = [pid for pid in remaining if pid_is_alive(pid)]
        if not alive:
            return True
        time.sleep(0.25)
        remaining = alive
    return not any(pid_is_alive(pid) for pid in remaining)


def terminate_browser_profile_processes(
    profile_path: str | Path,
    *,
    graceful_timeout: float = 8.0,
    force: bool = True,
) -> bool:
    pids = browser_profile_owner_pids(profile_path)
    if not pids:
        return False
    terminated = False
    for pid in pids:
        try:
            if sys.platform == "win32":
                os.kill(pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            else:
                os.kill(pid, signal.SIGTERM)
            terminated = True
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
                terminated = True
            except Exception:
                record_event_safe(
                    "browser_runtime",
                    f"浏览器进程 SIGTERM 失败 (pid={pid})",
                    level="warning",
                    event_key=f"browser_cleanup_failed:{profile_path}",
                    throttle_seconds=1800,
                    details={"pid": pid, "profile_path": str(profile_path), "signal": "SIGTERM"},
                    suggestion="检查浏览器进程是否卡死",
                )
    if wait_for_pids_exit(pids, graceful_timeout):
        return terminated
    if not force:
        return terminated
    for pid in pids:
        try:
            if sys.platform == "win32":
                proc = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=4,
                    check=False,
                )
                if proc.returncode != 0:
                    record_event_safe(
                        "browser_runtime",
                        f"浏览器进程 taskkill 失败 (pid={pid})",
                        level="error",
                        event_key=f"browser_cleanup_failed:{profile_path}",
                        throttle_seconds=1800,
                        details={"pid": pid, "profile_path": str(profile_path), "signal": "taskkill", "returncode": proc.returncode},
                        suggestion="浏览器进程可能需要手动终止",
                    )
                else:
                    terminated = True
            else:
                os.kill(pid, signal.SIGKILL)
                terminated = True
        except Exception:
            record_event_safe(
                "browser_runtime",
                f"浏览器进程 SIGKILL 失败 (pid={pid})",
                level="error",
                event_key=f"browser_cleanup_failed:{profile_path}",
                throttle_seconds=1800,
                details={"pid": pid, "profile_path": str(profile_path), "signal": "SIGKILL"},
                suggestion="浏览器进程可能需要手动终止",
            )
    wait_for_pids_exit(pids, 2.0)
    return terminated
