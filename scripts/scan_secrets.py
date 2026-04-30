#!/usr/bin/env python3
"""Lightweight repository secret guard for AI Monitor.

The scanner intentionally focuses on project-specific high-risk leaks:
- public config files carrying API keys or webhook URLs;
- browser profiles, cookies, auth snapshots, and login state tracked by Git;
- staged changes that introduce common API-key or webhook patterns.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

import yaml

ROOT = Path(__file__).resolve().parents[1]
SENSITIVE_CONFIG_FIELDS = {"api_key", "webhook_url", "notification_webhook_url", "tavily_api_key", "api_token"}
SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9][A-Za-z0-9_-]{8,}|"
    r"https://qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=|"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
PLACEHOLDER_RE = re.compile(r"(YOUR[_-]?KEY|YOUR[_-]?TOKEN|EXAMPLE|PLACEHOLDER)", re.IGNORECASE)
RUNTIME_PATH_RE = re.compile(
    r"(^|/)(doubao\.json|doubao_auth\.json|doubao_profile/|user_data/|user_data[^/]*/|"
    r"temp_profile[^/]*/|auth/[^/]+/|.*Cookies(?:-journal)?$|.*Login Data(?:-journal)?$|.*browser_state\.json$)"
)
SKIP_SCAN_PATH_RE = re.compile(
    r"(^|/)(\.git/|config\.local\.yaml$|\.app_data/|\.app_data_test/|logs/|screenshots/|reports/|exports/)"
)


def run_git(args: list[str], *, text: bool = True) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=text, stderr=subprocess.DEVNULL)


def iter_git_files(args: list[str]) -> Iterable[str]:
    try:
        output = run_git(args)
    except Exception:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def collect_secret_config_paths() -> list[str]:
    problems: list[str] = []
    path = ROOT / "config.yaml"
    if not path.exists():
        return problems
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return [f"config.yaml: cannot parse YAML: {exc}"]

    def walk(value, location: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                child_location = f"{location}.{key}" if location else str(key)
                if str(key) in SENSITIVE_CONFIG_FIELDS and str(item or "").strip():
                    problems.append(f"config.yaml:{child_location} must be empty; put the value in config.local.yaml")
                walk(item, child_location)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{location}[{index}]")

    walk(data, "")
    return problems


def collect_runtime_files(files: Iterable[str]) -> list[str]:
    return [path for path in files if RUNTIME_PATH_RE.search(path)]


def collect_text_secret_hits(files: Iterable[str]) -> list[str]:
    hits: list[str] = []
    for rel_path in files:
        if SKIP_SCAN_PATH_RE.search(rel_path) or RUNTIME_PATH_RE.search(rel_path):
            continue
        path = ROOT / rel_path
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        except Exception:
            continue
        scrubbed = "\n".join(line for line in text.splitlines() if not PLACEHOLDER_RE.search(line))
        if SECRET_VALUE_RE.search(scrubbed):
            hits.append(rel_path)
    return hits


def staged_files() -> list[str]:
    try:
        output = run_git(["diff", "--cached", "--name-only", "--diff-filter=ACMR"])
    except Exception:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan AI Monitor repo for accidental secret leaks.")
    parser.add_argument("--staged", action="store_true", help="Scan staged files instead of all tracked files.")
    args = parser.parse_args()

    files = staged_files() if args.staged else iter_git_files(["ls-files"])
    problems: list[str] = []
    problems.extend(collect_secret_config_paths())
    runtime_files = collect_runtime_files(files)
    if runtime_files:
        problems.append("runtime/auth files must not be tracked or staged:")
        problems.extend(f"  - {item}" for item in runtime_files[:50])
        if len(runtime_files) > 50:
            problems.append(f"  ... and {len(runtime_files) - 50} more")
    text_hits = collect_text_secret_hits(files)
    if text_hits:
        problems.append("possible secret values found in text files:")
        problems.extend(f"  - {item}" for item in text_hits[:50])
        if len(text_hits) > 50:
            problems.append(f"  ... and {len(text_hits) - 50} more")

    if problems:
        print("ERROR: secret scan failed", file=sys.stderr)
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    print("Secret scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
