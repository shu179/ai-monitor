#!/usr/bin/env python3
"""下载并提取 Ollama 官方发布资产到随包目录。"""

from __future__ import annotations

import argparse
import os
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import requests


DEFAULT_VERSION = "v0.13.5"

ASSET_MAP = {
    "darwin-arm64": {
        "asset_name": "ollama-darwin.tgz",
        "binary_name": "ollama",
        "archive_kind": "tar.gz",
    },
    "darwin-amd64": {
        "asset_name": "ollama-darwin.tgz",
        "binary_name": "ollama",
        "archive_kind": "tar.gz",
    },
    "windows-amd64": {
        "asset_name": "ollama-windows-amd64.zip",
        "binary_name": "ollama.exe",
        "archive_kind": "zip",
    },
    "windows-arm64": {
        "asset_name": "ollama-windows-arm64.zip",
        "binary_name": "ollama.exe",
        "archive_kind": "zip",
    },
}


def current_bundle_platform() -> str:
    machine = os.uname().machine.lower() if hasattr(os, "uname") else ""
    if os.name == "nt":
        if machine in {"amd64", "x86_64", "x64"}:
            arch = "amd64"
        elif machine in {"arm64", "aarch64"}:
            arch = "arm64"
        else:
            arch = machine or "unknown"
        return f"windows-{arch}"
    if os.sys.platform == "darwin":
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="下载 Ollama 官方发布资产并整理到 third_party")
    parser.add_argument(
        "--bundle-platform",
        default=current_bundle_platform(),
        help="目标平台，例如 darwin-arm64、windows-amd64",
    )
    parser.add_argument(
        "--version",
        default=DEFAULT_VERSION,
        help="Ollama release 版本号，例如 v0.13.5",
    )
    parser.add_argument(
        "--archive",
        default="",
        help="本地已下载的归档文件路径；传入后跳过在线下载",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="输出根目录，默认项目根目录下的 third_party/",
    )
    parser.add_argument(
        "--base-url",
        default="https://github.com/ollama/ollama/releases/download",
        help="发布资产基础地址，默认官方 GitHub release",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印计划，不实际下载和写入",
    )
    return parser.parse_args()


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def resolve_asset_info(bundle_platform: str) -> dict[str, str]:
    info = ASSET_MAP.get(bundle_platform)
    if info is None:
        supported = ", ".join(sorted(ASSET_MAP))
        raise ValueError(f"暂不支持的平台: {bundle_platform}；当前支持: {supported}")
    return info


def build_download_url(base_url: str, version: str, asset_name: str) -> str:
    return f"{base_url.rstrip('/')}/{version}/{asset_name}"


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def download_file(url: str, target: Path) -> None:
    ensure_parent(target)
    with requests.get(url, stream=True, timeout=30) as response:
        response.raise_for_status()
        with target.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)


def find_member_path(names: list[str], binary_name: str) -> str:
    normalized = [name for name in names if name and not name.endswith("/")]
    for name in normalized:
        if Path(name).name == binary_name:
            return name
    raise FileNotFoundError(f"归档内未找到目标文件: {binary_name}")


def extract_binary_from_zip(archive: Path, binary_name: str, target: Path) -> None:
    with zipfile.ZipFile(archive, "r") as zf:
        member = find_member_path(zf.namelist(), binary_name)
        ensure_parent(target)
        with zf.open(member, "r") as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst)


def extract_binary_from_targz(archive: Path, binary_name: str, target: Path) -> None:
    with tarfile.open(archive, "r:gz") as tf:
        names = tf.getnames()
        member_name = find_member_path(names, binary_name)
        member = tf.getmember(member_name)
        extracted = tf.extractfile(member)
        if extracted is None:
            raise FileNotFoundError(f"无法从归档中读取目标文件: {binary_name}")
        ensure_parent(target)
        with extracted, target.open("wb") as dst:
            shutil.copyfileobj(extracted, dst)


def make_executable(path: Path) -> None:
    if path.suffix.lower() == ".exe":
        return
    mode = path.stat().st_mode
    path.chmod(mode | 0o111)


def main() -> int:
    args = parse_args()
    bundle_platform = str(args.bundle_platform or "").strip() or current_bundle_platform()
    info = resolve_asset_info(bundle_platform)
    asset_name = info["asset_name"]
    binary_name = info["binary_name"]
    archive_kind = info["archive_kind"]

    root = project_root()
    output_root = Path(args.output_dir).expanduser() if args.output_dir else (root / "third_party")
    output_root = output_root.resolve()
    target_binary = output_root / "ollama" / bundle_platform / binary_name

    if args.archive:
        archive_path = Path(args.archive).expanduser().resolve()
        url = ""
    else:
        archive_path = Path(tempfile.gettempdir()) / asset_name
        url = build_download_url(str(args.base_url), str(args.version), asset_name)

    print(f"[信息] 目标平台: {bundle_platform}")
    print(f"[信息] 目标文件: {target_binary}")
    if url:
        print(f"[信息] 下载地址: {url}")
    else:
        print(f"[信息] 使用本地归档: {archive_path}")

    if args.dry_run:
        print("[计划] 下载并提取官方 Ollama 发布资产")
        return 0

    if url:
        print(f"[开始] 下载 {asset_name}")
        download_file(url, archive_path)
        print(f"[完成] 下载到: {archive_path}")
    elif not archive_path.exists():
        print(f"[错误] 本地归档不存在: {archive_path}")
        return 1

    if archive_kind == "zip":
        extract_binary_from_zip(archive_path, binary_name, target_binary)
    elif archive_kind == "tar.gz":
        extract_binary_from_targz(archive_path, binary_name, target_binary)
    else:
        print(f"[错误] 不支持的归档类型: {archive_kind}")
        return 1

    make_executable(target_binary)
    print(f"[完成] 已提取内置引擎: {target_binary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
