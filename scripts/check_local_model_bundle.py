#!/usr/bin/env python3
"""检查本地模型随包资源是否齐全。"""

from __future__ import annotations

import argparse
import json
import os
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
    parser = argparse.ArgumentParser(description="检查 Ollama 随包资源")
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        default=[],
        help="要检查的模型，可传多次；默认 gemma4:e2b",
    )
    parser.add_argument(
        "--bundle-dir",
        default="",
        help="资源根目录，默认项目根目录下的 third_party/",
    )
    parser.add_argument(
        "--bundle-platform",
        default=current_bundle_platform(),
        help="要检查哪个平台目录，默认当前平台，例如 darwin-arm64、windows-amd64",
    )
    return parser.parse_args()


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


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


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    args = parse_args()
    root = project_root()
    bundle_dir = Path(args.bundle_dir).expanduser() if args.bundle_dir else (root / "third_party")
    bundle_dir = bundle_dir.resolve()
    models = [parse_model_ref(item) for item in (args.models or [DEFAULT_MODEL])]
    bundle_platform = str(args.bundle_platform or "").strip() or current_bundle_platform()

    binary_name = "ollama.exe" if bundle_platform.startswith("windows-") else "ollama"
    binary_path = bundle_dir / "ollama" / bundle_platform / binary_name
    models_root = bundle_dir / "ollama-models"
    manifests_root = models_root / "manifests"
    blobs_root = models_root / "blobs"

    print(f"[信息] 检查目录: {bundle_dir}")
    print(f"[信息] 平台目录: {bundle_platform}")

    issues: list[str] = []

    if binary_path.exists() and binary_path.is_file():
        print(f"[OK] 已发现 Ollama 引擎: {binary_path}")
    else:
        issues.append(f"缺少 Ollama 引擎文件: {binary_path}")

    if manifests_root.exists() and manifests_root.is_dir():
        print(f"[OK] 已发现 manifests 目录: {manifests_root}")
    else:
        issues.append(f"缺少 manifests 目录: {manifests_root}")

    if blobs_root.exists() and blobs_root.is_dir():
        print(f"[OK] 已发现 blobs 目录: {blobs_root}")
    else:
        issues.append(f"缺少 blobs 目录: {blobs_root}")

    for model in models:
        manifest_path = manifests_root / model.manifest_relative_path
        if not manifest_path.exists():
            issues.append(f"缺少模型清单: {model.raw} -> {manifest_path}")
            continue

        print(f"[OK] 已发现模型清单: {model.raw}")
        try:
            manifest = load_json(manifest_path)
        except Exception as exc:
            issues.append(f"模型清单无法解析: {manifest_path} ({exc})")
            continue

        digests: list[str] = []
        config = manifest.get("config") or {}
        config_digest = str(config.get("digest") or "").strip()
        if config_digest:
            digests.append(config_digest)
        for layer in manifest.get("layers") or []:
            if not isinstance(layer, dict):
                continue
            digest = str(layer.get("digest") or "").strip()
            if digest:
                digests.append(digest)

        missing_blobs: list[str] = []
        for digest in digests:
            blob_path = blobs_root / digest_to_blob_filename(digest)
            if not blob_path.exists():
                missing_blobs.append(digest)

        if missing_blobs:
            issues.append(
                f"模型 {model.raw} 缺少 {len(missing_blobs)} 个 blob: "
                + ", ".join(missing_blobs[:5])
                + (" ..." if len(missing_blobs) > 5 else "")
            )
        else:
            print(f"[OK] 模型 {model.raw} 的 blob 文件完整")

    if issues:
        print("[结果] 随包资源检查失败")
        for issue in issues:
            print(f"[缺失] {issue}")
        return 1

    print("[结果] 随包资源检查通过，可以进入打包流程")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
