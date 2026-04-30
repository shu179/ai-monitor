"""
Surfaced 更新元数据与升级清单解析。

设计目标：
1. 不影响现有运行链路，只提供未来升级所需的基础能力。
2. 既支持远程 manifest URL，也支持本地文件路径调试。
3. 明确区分“当前运行版本”和“可用更新结果”。
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import platform as runtime_platform
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from core.version import APP_NAME, get_version_payload
from core.app_paths import resolve_app_dir


DEFAULT_APP_UPDATE_SETTINGS = {
    "channel": "stable",
    "manifest_url": "",
    "download_page_url": "",
    "auto_check_enabled": False,
    "manifest_sha256": "",
    "manifest_public_key": "",
    "require_signature": False,
    "require_package_hash": False,
}

_SIGNATURE_FIELDS = {
    "signature",
    "signatures",
    "signature_algorithm",
    "signatureAlgorithm",
    "public_key_id",
    "key_id",
}
_UPDATE_PACKAGE_MAX_BYTES = 2 * 1024 * 1024 * 1024
_UPDATE_ROOT_MARKERS = (
    "main.py",
    "web_backend.py",
    "Surfaced.app",
    "Surfaced.exe",
)


@dataclass(frozen=True)
class _ManifestSource:
    payload: dict[str, Any]
    raw_bytes: bytes


def normalize_update_channel(value: Any, default: str = "stable") -> str:
    text = str(value or "").strip().lower()
    if text in {"stable", "beta"}:
        return text
    return default


def _normalize_sha256(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("sha256:"):
        text = text[len("sha256:"):].strip()
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else ""


def get_app_update_settings(config: dict[str, Any] | None) -> dict[str, Any]:
    raw = (config or {}).get("app_update", {}) or {}
    return {
        "channel": normalize_update_channel(raw.get("channel"), DEFAULT_APP_UPDATE_SETTINGS["channel"]),
        "manifest_url": str(raw.get("manifest_url", "") or "").strip(),
        "download_page_url": str(raw.get("download_page_url", "") or "").strip(),
        "auto_check_enabled": bool(raw.get("auto_check_enabled", False)),
        "manifest_sha256": _normalize_sha256(raw.get("manifest_sha256", "")),
        "manifest_public_key": str(
            raw.get("manifest_public_key", "")
            or os.environ.get("SURFACED_UPDATE_PUBLIC_KEY", "")
            or ""
        ).strip(),
        "require_signature": bool(raw.get("require_signature", False)),
        "require_package_hash": bool(raw.get("require_package_hash", False)),
    }


def get_platform_package_keys() -> list[str]:
    system_map = {
        "darwin": "darwin",
        "windows": "windows",
        "linux": "linux",
    }
    machine_map = {
        "x86_64": "x64",
        "amd64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }
    system_name = system_map.get(runtime_platform.system().lower(), runtime_platform.system().lower())
    machine_name = machine_map.get(runtime_platform.machine().lower(), runtime_platform.machine().lower())
    combos = [
        f"{system_name}-{machine_name}",
        system_name,
    ]
    seen: list[str] = []
    for item in combos:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def _version_to_tuple(value: Any) -> tuple[int, ...]:
    text = str(value or "").strip().lower()
    if text.startswith("v"):
        text = text[1:]
    parts = [int(chunk) for chunk in re.findall(r"\d+", text)]
    return tuple(parts or [0])


def compare_versions(current: Any, candidate: Any) -> int:
    left = list(_version_to_tuple(current))
    right = list(_version_to_tuple(candidate))
    max_len = max(len(left), len(right))
    left.extend([0] * (max_len - len(left)))
    right.extend([0] * (max_len - len(right)))
    if left < right:
        return -1
    if left > right:
        return 1
    return 0


def _read_manifest_source(source: str) -> _ManifestSource:
    text = str(source or "").strip()
    if not text:
        raise ValueError("未配置升级清单地址")

    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"}:
        with urlopen(text, timeout=5) as response:
            raw_bytes = response.read()
        return _ManifestSource(json.loads(raw_bytes.decode("utf-8")), raw_bytes)

    if parsed.scheme == "file":
        path = Path(parsed.path)
    else:
        path = Path(text).expanduser()
    raw_bytes = path.read_bytes()
    return _ManifestSource(json.loads(raw_bytes.decode("utf-8")), raw_bytes)


def _pick_package_info(packages: Any) -> dict[str, str]:
    if not isinstance(packages, dict):
        return {}
    for key in get_platform_package_keys():
        value = packages.get(key)
        if isinstance(value, str) and value.strip():
            return {"download_url": value.strip()}
        if isinstance(value, dict):
            info: dict[str, str] = {}
            for field in ("download_url", "url"):
                text = str(value.get(field, "") or "").strip()
                if text:
                    info["download_url"] = text
                    break
            sha256 = (
                _normalize_sha256(value.get("sha256"))
                or _normalize_sha256(value.get("checksum_sha256"))
                or _normalize_sha256(value.get("checksum"))
            )
            if sha256:
                info["sha256"] = sha256
            if info:
                return info
    return {}


def _pick_package_download_url(packages: Any) -> str:
    return _pick_package_info(packages).get("download_url", "")


def _resolve_manifest_entry(manifest: dict[str, Any], channel: str) -> dict[str, Any]:
    channels = manifest.get("channels")
    if isinstance(channels, dict):
        candidate = channels.get(channel) or channels.get("stable") or {}
        if isinstance(candidate, dict):
            return {
                **manifest,
                **candidate,
            }
    return manifest


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: str | Path) -> str:
    """Return the SHA256 hex digest for a local file."""
    digest = hashlib.sha256()
    with Path(path).expanduser().open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file_sha256(path: str | Path, expected_sha256: Any) -> bool:
    expected = _normalize_sha256(expected_sha256)
    if not expected:
        raise ValueError("缺少有效的 sha256 校验值")
    actual = file_sha256(path)
    if actual != expected:
        raise ValueError(f"文件 sha256 不匹配：expected={expected} actual={actual}")
    return True


def _safe_package_filename(download_url: str) -> str:
    parsed = urlparse(str(download_url or ""))
    name = Path(parsed.path).name
    if not name or name in {".", ".."}:
        name = "Surfaced-update.zip"
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return name or "Surfaced-update.zip"


def _looks_like_windows_drive_path(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", str(value or "")))


def _copy_stream_with_limit(source, target: Path, *, max_bytes: int) -> int:
    total = 0
    with target.open("wb") as handle:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("更新包超过允许大小")
            handle.write(chunk)
    return total


def download_update_package(
    download_url: str,
    expected_sha256: Any,
    *,
    destination_dir: str | Path | None = None,
    max_bytes: int = _UPDATE_PACKAGE_MAX_BYTES,
) -> dict[str, Any]:
    """Download or copy an update package, then verify its SHA256."""
    expected = _normalize_sha256(expected_sha256)
    if not expected:
        raise ValueError("下载更新包需要 manifest 提供 sha256")

    raw_url = str(download_url or "").strip()
    if not raw_url:
        raise ValueError("缺少更新包下载地址")
    parsed = urlparse(raw_url)
    is_windows_drive_path = _looks_like_windows_drive_path(raw_url)
    if parsed.scheme and parsed.scheme not in {"https", "file"} and not is_windows_drive_path:
        raise ValueError("更新包下载地址只允许 https:// 或本地 file://")

    dest_root = Path(destination_dir).expanduser().resolve() if destination_dir else resolve_app_dir("user_data/update_downloads")
    dest_root.mkdir(parents=True, exist_ok=True)
    target = dest_root / _safe_package_filename(raw_url)
    target_is_input = False

    try:
        if parsed.scheme == "https":
            with urlopen(raw_url, timeout=30) as response:
                final_url = str(response.geturl() or raw_url)
                if urlparse(final_url).scheme.lower() != "https":
                    raise ValueError("更新包下载地址重定向到了非 https 地址")
                size = _copy_stream_with_limit(response, target, max_bytes=max_bytes)
        else:
            source_path = Path(parsed.path if parsed.scheme == "file" else raw_url).expanduser().resolve()
            if not source_path.exists() or not source_path.is_file():
                raise FileNotFoundError(f"更新包不存在：{source_path}")
            if source_path.stat().st_size > max_bytes:
                raise ValueError("更新包超过允许大小")
            target_is_input = source_path == target
            if source_path != target:
                shutil.copy2(source_path, target)
            size = target.stat().st_size

        verify_file_sha256(target, expected)
    except Exception:
        try:
            if target.exists() and not target_is_input:
                target.unlink()
        except Exception:
            pass
        raise
    return {
        "archive_path": str(target),
        "sha256": expected,
        "size": int(size),
    }


def _is_safe_extract_target(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def extract_update_archive(archive_path: str | Path, *, destination_dir: str | Path | None = None) -> dict[str, str]:
    """Extract a verified update archive to a staging directory."""
    archive = Path(archive_path).expanduser().resolve()
    if not archive.exists() or not archive.is_file():
        raise FileNotFoundError(f"更新包不存在：{archive}")
    if archive.suffix.lower() != ".zip":
        raise ValueError("当前仅支持 .zip 更新包")

    staging_root = Path(destination_dir).expanduser().resolve() if destination_dir else resolve_app_dir("user_data/update_staging")
    staging_root.mkdir(parents=True, exist_ok=True)
    extract_root = Path(tempfile.mkdtemp(prefix="surfaced-update-", dir=str(staging_root)))

    try:
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                member_name = str(member.filename or "")
                if not member_name or member_name.startswith(("/", "\\")):
                    raise ValueError("更新包包含非法路径")
                target = extract_root / member_name
                if not _is_safe_extract_target(extract_root, target):
                    raise ValueError("更新包包含路径穿越内容")
            bundle.extractall(extract_root)

        source_dir = find_extracted_update_root(extract_root)
        return {
            "extract_dir": str(extract_root),
            "source_dir": str(source_dir),
        }
    except Exception:
        shutil.rmtree(extract_root, ignore_errors=True)
        raise


def find_extracted_update_root(extract_root: str | Path) -> Path:
    root = Path(extract_root).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"解压目录不存在：{root}")
    if _has_update_root_marker(root):
        return root
    children = [item for item in root.iterdir() if item.is_dir()]
    if len(children) == 1 and _has_update_root_marker(children[0]):
        return children[0].resolve()
    for child in children:
        if _has_update_root_marker(child):
            return child.resolve()
    raise ValueError("更新包解压后缺少可识别的程序入口")


def _has_update_root_marker(path: Path) -> bool:
    return any((path / marker).exists() for marker in _UPDATE_ROOT_MARKERS)


def prepare_update_package(
    *,
    download_url: str,
    expected_sha256: Any,
    download_dir: str | Path | None = None,
    staging_dir: str | Path | None = None,
) -> dict[str, Any]:
    downloaded = download_update_package(
        download_url,
        expected_sha256,
        destination_dir=download_dir,
    )
    extracted = extract_update_archive(
        downloaded["archive_path"],
        destination_dir=staging_dir,
    )
    return {
        **downloaded,
        **extracted,
    }


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _manifest_signature_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    if isinstance(manifest.get("signed"), dict):
        return copy.deepcopy(manifest["signed"])
    payload = copy.deepcopy(manifest)
    for field in _SIGNATURE_FIELDS:
        payload.pop(field, None)
    return payload


def _extract_signature_info(manifest: dict[str, Any]) -> dict[str, str]:
    signature = manifest.get("signature")
    algorithm = str(manifest.get("signature_algorithm") or manifest.get("signatureAlgorithm") or "").strip()
    key_id = str(manifest.get("public_key_id") or manifest.get("key_id") or "").strip()
    value = ""
    if isinstance(signature, dict):
        algorithm = str(signature.get("algorithm") or algorithm or "").strip()
        value = str(signature.get("value") or signature.get("signature") or signature.get("sig") or "").strip()
        key_id = str(signature.get("key_id") or signature.get("public_key_id") or key_id or "").strip()
    elif isinstance(signature, str):
        value = signature.strip()
    elif isinstance(manifest.get("signatures"), list):
        for item in manifest.get("signatures") or []:
            if not isinstance(item, dict):
                continue
            value = str(item.get("value") or item.get("signature") or item.get("sig") or "").strip()
            if value:
                algorithm = str(item.get("algorithm") or algorithm or "").strip()
                key_id = str(item.get("key_id") or item.get("public_key_id") or key_id or "").strip()
                break
    return {
        "algorithm": (algorithm or "ed25519").lower(),
        "value": value,
        "key_id": key_id,
    }


def _decode_signature_or_key(value: str) -> bytes:
    text = str(value or "").strip()
    if not text:
        return b""
    compact = "".join(text.split())
    if re.fullmatch(r"[0-9a-fA-F]+", compact) and len(compact) % 2 == 0:
        return bytes.fromhex(compact)
    padded = compact + ("=" * ((4 - len(compact) % 4) % 4))
    try:
        return base64.b64decode(padded, validate=True)
    except Exception:
        return base64.urlsafe_b64decode(padded)


def _load_manifest_public_key(public_key_text: str, algorithm: str):
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except Exception as exc:
        raise ValueError("签名校验需要安装 cryptography 依赖") from exc

    text = str(public_key_text or "").strip()
    if not text:
        raise ValueError("缺少升级清单签名公钥")
    if "-----BEGIN" in text:
        return serialization.load_pem_public_key(text.encode("utf-8"))
    raw = _decode_signature_or_key(text)
    if algorithm == "ed25519":
        return ed25519.Ed25519PublicKey.from_public_bytes(raw)
    return serialization.load_der_public_key(raw)


def _verify_signature_bytes(
    *,
    canonical_payload: bytes,
    signature: bytes,
    algorithm: str,
    public_key_text: str,
) -> None:
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
    except Exception as exc:
        raise ValueError("签名校验需要安装 cryptography 依赖") from exc

    normalized_algorithm = str(algorithm or "ed25519").strip().lower()
    public_key = _load_manifest_public_key(public_key_text, normalized_algorithm)
    if normalized_algorithm == "ed25519":
        public_key.verify(signature, canonical_payload)
        return
    if normalized_algorithm in {"rsa-pss-sha256", "rsa-pss"}:
        public_key.verify(
            signature,
            canonical_payload,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )
        return
    if normalized_algorithm in {"rsa-pkcs1-sha256", "rsa-sha256", "rs256"}:
        public_key.verify(signature, canonical_payload, padding.PKCS1v15(), hashes.SHA256())
        return
    raise ValueError(f"不支持的升级清单签名算法：{algorithm}")


def _verify_manifest_signature(manifest: dict[str, Any], public_key_text: str) -> dict[str, str]:
    signature_info = _extract_signature_info(manifest)
    if not signature_info["value"]:
        raise ValueError("升级清单缺少 signature 字段")
    payload = _manifest_signature_payload(manifest)
    try:
        _verify_signature_bytes(
            canonical_payload=_canonical_json_bytes(payload),
            signature=_decode_signature_or_key(signature_info["value"]),
            algorithm=signature_info["algorithm"],
            public_key_text=public_key_text,
        )
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("升级清单签名校验失败") from exc
    return signature_info


def _validate_manifest_security(source: _ManifestSource, settings: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    security = {
        "manifest_sha256": _sha256_bytes(source.raw_bytes),
        "manifest_hash_checked": False,
        "manifest_signature_checked": False,
        "manifest_signature_required": bool(settings.get("require_signature") or settings.get("manifest_public_key")),
        "package_hash_required": bool(settings.get("require_package_hash")),
        "signature_key_id": "",
    }
    expected_manifest_hash = _normalize_sha256(settings.get("manifest_sha256"))
    if expected_manifest_hash:
        security["manifest_hash_checked"] = True
        if security["manifest_sha256"] != expected_manifest_hash:
            raise ValueError("升级清单 sha256 不匹配")

    manifest = source.payload
    if not isinstance(manifest, dict):
        raise ValueError("升级清单必须是 JSON object")

    if security["manifest_signature_required"]:
        signature_info = _verify_manifest_signature(manifest, str(settings.get("manifest_public_key") or "").strip())
        security["manifest_signature_checked"] = True
        security["signature_key_id"] = signature_info.get("key_id", "")
        if isinstance(manifest.get("signed"), dict):
            manifest = copy.deepcopy(manifest["signed"])

    return manifest, security


def build_update_status(config: dict[str, Any] | None, *, include_check: bool = False) -> dict[str, Any]:
    version_info = get_version_payload()
    settings = get_app_update_settings(config)
    result = {
        "ok": True,
        "configured": bool(settings["manifest_url"]),
        "checked": False,
        "update_available": False,
        "message": "未配置升级清单地址",
        "current": version_info,
        "settings": settings,
        "latest": None,
        "download_url": settings["download_page_url"],
        "download_page_url": settings["download_page_url"],
        "manifest_source": settings["manifest_url"],
        "platform_keys": get_platform_package_keys(),
        "package_sha256": "",
        "security": {
            "manifest_sha256": "",
            "manifest_hash_checked": False,
            "manifest_signature_checked": False,
            "manifest_signature_required": bool(settings.get("require_signature") or settings.get("manifest_public_key")),
            "package_hash_required": bool(settings.get("require_package_hash")),
            "package_hash_present": False,
        },
    }
    if not include_check:
        if result["configured"]:
            result["message"] = "已配置升级清单，可手动检查更新"
        return result

    if not settings["manifest_url"]:
        return result

    result["checked"] = True
    try:
        manifest_source = _read_manifest_source(settings["manifest_url"])
        manifest, security = _validate_manifest_security(manifest_source, settings)
        result["security"] = {
            **result["security"],
            **security,
        }
        entry = _resolve_manifest_entry(manifest, settings["channel"])
        latest_version = str(entry.get("version", "") or "").strip()
        if not latest_version:
            raise ValueError("升级清单缺少 version 字段")
        latest_channel = normalize_update_channel(entry.get("channel"), settings["channel"])
        latest_label = f"v{latest_version}" + ("" if latest_channel == "stable" else f"-{latest_channel}")
        package_info = _pick_package_info(entry.get("packages"))
        download_url = (
            package_info.get("download_url", "")
            or str(entry.get("download_url", "") or "").strip()
            or settings["download_page_url"]
        )
        package_sha256 = (
            package_info.get("sha256", "")
            or _normalize_sha256(entry.get("sha256"))
            or _normalize_sha256(entry.get("package_sha256"))
        )
        result["security"]["package_hash_present"] = bool(package_sha256)
        if settings.get("require_package_hash") and download_url and not package_sha256:
            raise ValueError("升级清单缺少当前平台安装包 sha256")
        download_page_url = (
            str(entry.get("download_page_url", "") or "").strip()
            or settings["download_page_url"]
            or download_url
        )
        published_at = str(entry.get("published_at", "") or "").strip()
        notes = str(entry.get("notes", "") or "").strip()
        update_available = compare_versions(version_info["version"], latest_version) < 0
        result.update(
            {
                "update_available": update_available,
                "message": f"检测到新版本 {latest_label}" if update_available else "当前已经是最新版本",
                "latest": {
                    "version": latest_version,
                    "channel": latest_channel,
                    "label": latest_label,
                    "published_at": published_at,
                    "notes": notes,
                },
                "download_url": download_url,
                "download_page_url": download_page_url,
                "package_sha256": package_sha256,
            }
        )
        return result
    except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
        result.update(
            {
                "ok": False,
                "message": f"检查更新失败：{exc}",
            }
        )
        return result
