#!/usr/bin/env python3
"""准备本地模型随包资源。

作用：
1. 收集本机 Ollama 可执行文件到 third_party/ollama/
2. 收集指定 Ollama 模型到 third_party/ollama-models/

默认只收集 gemma4:e2b，适合当前项目的打包场景。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path


DEFAULT_MODEL = "gemma4:e2b"
DEFAULT_REGISTRY = "registry.ollama.ai"
DEFAULT_NAMESPACE = "library"


@dataclass
class ModelRef:
    raw: str
    registry: str
    namespace: str
    name: str
    tag: str

    @property
    def manifest_relative_path(self) -> Path:
        return Path(self.registry) / self.namespace / self.name / self.tag


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
    parser = argparse.ArgumentParser(description="准备 Ollama 随包资源")
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        default=[],
        help="要随包的模型，可传多次；默认 gemma4:e2b",
    )
    parser.add_argument(
        "--all-local-models",
        action="store_true",
        help="直接复制本机整个 Ollama 模型仓库，而不是只复制指定模型",
    )
    parser.add_argument(
        "--ollama-bin",
        default="",
        help="手动指定 Ollama 可执行文件路径",
    )
    parser.add_argument(
        "--models-dir",
        default="",
        help="手动指定 Ollama 模型仓库目录（应包含 manifests/ 和 blobs/）",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="输出根目录，默认项目根目录下的 third_party/",
    )
    parser.add_argument(
        "--bundle-platform",
        default=current_bundle_platform(),
        help="要准备到哪个平台目录，默认当前平台，例如 darwin-arm64、windows-amd64",
    )
    parser.add_argument(
        "--skip-binary",
        action="store_true",
        help="只准备模型，不复制 Ollama 可执行文件",
    )
    parser.add_argument(
        "--skip-models",
        action="store_true",
        help="只准备 Ollama 可执行文件，不复制模型",
    )
    parser.add_argument(
        "--force-clean",
        action="store_true",
        help="复制前先清空目标 third_party/ollama 和 third_party/ollama-models",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印计划，不实际复制文件",
    )
    return parser.parse_args()


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def detect_ollama_binary(explicit: str) -> Path | None:
    candidates: list[str] = []
    if explicit.strip():
        candidates.append(explicit.strip())
    env_bin = os.environ.get("AIBRANDMONITOR_OLLAMA_BIN", "").strip()
    if env_bin:
        candidates.append(env_bin)

    if os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        if local_appdata:
            candidates.append(str(Path(local_appdata) / "Programs" / "Ollama" / "ollama.exe"))
    elif sys_platform() == "darwin":
        candidates.append("/Applications/Ollama.app/Contents/Resources/ollama")

    discovered = shutil.which("ollama")
    if discovered:
        candidates.append(discovered)

    for raw in candidates:
        path = Path(raw).expanduser()
        if path.exists() and path.is_file():
            return path
    return None


def detect_models_dir(explicit: str) -> Path | None:
    candidates: list[str] = []
    if explicit.strip():
        candidates.append(explicit.strip())
    env_models = os.environ.get("OLLAMA_MODELS", "").strip()
    if env_models:
        candidates.append(env_models)
    app_models = os.environ.get("AIBRANDMONITOR_OLLAMA_MODELS_DIR", "").strip()
    if app_models:
        candidates.append(app_models)
    candidates.append(str(Path.home() / ".ollama" / "models"))

    for raw in candidates:
        path = Path(raw).expanduser()
        if path.exists() and (path / "manifests").exists() and (path / "blobs").exists():
            return path
    return None


def sys_platform() -> str:
    return os.sys.platform


def parse_model_ref(raw: str) -> ModelRef:
    text = raw.strip()
    if not text:
        raise ValueError("模型名不能为空")

    tag = "latest"
    ref_text = text
    slash_index = text.rfind("/")
    colon_index = text.rfind(":")
    if colon_index > slash_index:
        ref_text = text[:colon_index]
        tag = text[colon_index + 1 :].strip() or "latest"

    parts = [part for part in ref_text.split("/") if part]
    if not parts:
        raise ValueError(f"无法识别模型名：{raw}")

    registry = DEFAULT_REGISTRY
    namespace = DEFAULT_NAMESPACE
    name_parts: list[str]

    if len(parts) == 1:
        name_parts = parts
    elif len(parts) == 2:
        namespace = parts[0]
        name_parts = [parts[1]]
    else:
        registry = parts[0]
        namespace = parts[1]
        name_parts = parts[2:]

    name = "/".join(name_parts)
    if not name:
        raise ValueError(f"无法识别模型名：{raw}")
    return ModelRef(raw=text, registry=registry, namespace=namespace, name=name, tag=tag)


def digest_to_blob_filename(digest: str) -> str:
    return digest.replace(":", "-")


def ensure_parent(path: Path, dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)


def copy_file(src: Path, dst: Path, dry_run: bool) -> None:
    ensure_parent(dst, dry_run)
    if dry_run:
        print(f"[计划] 复制文件: {src} -> {dst}")
        return
    shutil.copy2(src, dst)


def copy_tree(src: Path, dst: Path, dry_run: bool) -> None:
    if dry_run:
        print(f"[计划] 复制目录: {src} -> {dst}")
        return
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def ensure_executable(path: Path, dry_run: bool) -> None:
    if dry_run or os.name == "nt":
        return
    mode = path.stat().st_mode
    os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def binary_name_for_platform(bundle_platform: str) -> str:
    return "ollama.exe" if bundle_platform.startswith("windows-") else "ollama"


def collect_binary(ollama_bin: Path, output_root: Path, bundle_platform: str, dry_run: bool) -> Path:
    target = output_root / "ollama" / bundle_platform / binary_name_for_platform(bundle_platform)
    copy_file(ollama_bin, target, dry_run)
    ensure_executable(target, dry_run)
    return target


def collect_all_models(models_dir: Path, output_root: Path, dry_run: bool) -> tuple[Path, Path]:
    manifests_src = models_dir / "manifests"
    blobs_src = models_dir / "blobs"
    manifests_dst = output_root / "ollama-models" / "manifests"
    blobs_dst = output_root / "ollama-models" / "blobs"
    copy_tree(manifests_src, manifests_dst, dry_run)
    copy_tree(blobs_src, blobs_dst, dry_run)
    return manifests_dst, blobs_dst


def load_manifest(manifest_path: Path) -> dict:
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def collect_selected_models(models_dir: Path, output_root: Path, models: list[ModelRef], dry_run: bool) -> tuple[int, int]:
    copied_manifests = 0
    copied_blobs: set[str] = set()
    manifests_root = models_dir / "manifests"
    blobs_root = models_dir / "blobs"
    out_root = output_root / "ollama-models"

    for model in models:
        manifest_src = manifests_root / model.manifest_relative_path
        if not manifest_src.exists():
            raise FileNotFoundError(f"未找到模型清单：{model.raw} -> {manifest_src}")

        manifest_dst = out_root / "manifests" / model.manifest_relative_path
        copy_file(manifest_src, manifest_dst, dry_run)
        copied_manifests += 1

        manifest_data = load_manifest(manifest_src)
        digests: list[str] = []
        config = manifest_data.get("config") or {}
        config_digest = str(config.get("digest") or "").strip()
        if config_digest:
            digests.append(config_digest)
        for layer in manifest_data.get("layers") or []:
            if not isinstance(layer, dict):
                continue
            digest = str(layer.get("digest") or "").strip()
            if digest:
                digests.append(digest)

        for digest in digests:
            blob_name = digest_to_blob_filename(digest)
            if blob_name in copied_blobs:
                continue
            blob_src = blobs_root / blob_name
            if not blob_src.exists():
                raise FileNotFoundError(f"未找到模型 blob：{digest} -> {blob_src}")
            blob_dst = out_root / "blobs" / blob_name
            copy_file(blob_src, blob_dst, dry_run)
            copied_blobs.add(blob_name)

    return copied_manifests, len(copied_blobs)


def maybe_clean_output(output_root: Path, dry_run: bool) -> None:
    for relative in ("ollama", "ollama-models"):
        target = output_root / relative
        if not target.exists():
            continue
        if dry_run:
            print(f"[计划] 清空目录: {target}")
            continue
        shutil.rmtree(target)


def main() -> int:
    args = parse_args()
    root = project_root()
    output_root = Path(args.output_dir).expanduser() if args.output_dir else (root / "third_party")
    output_root = output_root.resolve()
    models = [parse_model_ref(item) for item in (args.models or [DEFAULT_MODEL])]
    bundle_platform = str(args.bundle_platform or "").strip() or current_bundle_platform()

    print(f"[信息] 项目根目录: {root}")
    print(f"[信息] 资源输出目录: {output_root}")
    print(f"[信息] 目标平台目录: {bundle_platform}")

    if args.force_clean:
        maybe_clean_output(output_root, args.dry_run)

    if not args.skip_binary:
        ollama_bin = detect_ollama_binary(args.ollama_bin)
        if ollama_bin is None:
            print("[错误] 未找到本机 Ollama 可执行文件，请先安装或通过 --ollama-bin 指定")
            return 1
        binary_target = collect_binary(ollama_bin, output_root, bundle_platform, args.dry_run)
        print(f"[完成] Ollama 引擎已准备: {binary_target}")

    if not args.skip_models:
        models_dir = detect_models_dir(args.models_dir)
        if models_dir is None:
            print("[错误] 未找到本机 Ollama 模型仓库，请先拉取模型或通过 --models-dir 指定")
            return 1

        if args.all_local_models:
            manifests_dst, blobs_dst = collect_all_models(models_dir, output_root, args.dry_run)
            print(f"[完成] 已复制整个模型仓库: {manifests_dst.parent}")
            print(f"[完成] manifests: {manifests_dst}")
            print(f"[完成] blobs: {blobs_dst}")
        else:
            manifest_count, blob_count = collect_selected_models(models_dir, output_root, models, args.dry_run)
            print(f"[完成] 已复制模型清单数: {manifest_count}")
            print(f"[完成] 已复制 blob 数: {blob_count}")
            print("[完成] 已准备模型:")
            for model in models:
                print(f"  - {model.raw}")

    print("[完成] 本地模型随包资源准备结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
