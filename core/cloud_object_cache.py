from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .app_paths import resolve_app_path
from .local_account_space import account_scoped_path
from .time_utils import local_now


DEFAULT_CLOUD_OBJECT_CACHE_DIR = resolve_app_path("user_data/cloud_object_cache")
MAX_CACHE_OBJECT_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_CACHE_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_PRUNE_TARGET_RATIO = 0.85
_SHA256_HEX_LENGTH = 64


class CloudObjectCacheError(RuntimeError):
    """Raised when a cloud object cannot be safely cached."""


class CloudObjectCache:
    """Filesystem cache for downloaded cloud objects.

    The cache intentionally stores bytes on disk, never in SQLite. Objects are
    addressed by their uncompressed canonical sha256, written through a temp
    file, verified, and then atomically moved into place. This is the local
    foundation for future answer/image/object downsync without exposing any UI.
    """

    def __init__(
        self,
        root_dir: str | Path | None = None,
        *,
        max_object_bytes: int = MAX_CACHE_OBJECT_BYTES,
        max_cache_bytes: int | None = None,
    ) -> None:
        self._explicit_root = Path(root_dir) if root_dir is not None else None
        self.max_object_bytes = max(1, int(max_object_bytes or MAX_CACHE_OBJECT_BYTES))
        self.max_cache_bytes = _configured_max_cache_bytes(max_cache_bytes)

    @property
    def root_dir(self) -> Path:
        if self._explicit_root is not None:
            return self._explicit_root
        return account_scoped_path(
            "user_data/cloud_object_cache",
            fallback=DEFAULT_CLOUD_OBJECT_CACHE_DIR,
        )

    def cache_bytes(
        self,
        object_ref: dict[str, Any],
        chunks: Iterable[bytes],
        *,
        trace_id: str = "",
    ) -> dict[str, Any]:
        normalized = normalize_object_ref(object_ref)
        sha256 = str(normalized.get("sha256") or "")
        expected_size = int(normalized.get("size_bytes") or 0)
        if not sha256:
            raise CloudObjectCacheError("object_ref.sha256 is required")
        if expected_size < 0:
            raise CloudObjectCacheError("object_ref.size_bytes must be non-negative")
        if expected_size > self.max_object_bytes:
            raise CloudObjectCacheError("object exceeds local cache object size limit")

        target = self.object_path(normalized)
        meta_path = self.metadata_path(normalized)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
        tmp_path = Path(tmp_name)
        digest = hashlib.sha256()
        total = 0
        try:
            with os.fdopen(fd, "wb") as handle:
                for chunk in chunks:
                    if not chunk:
                        continue
                    data = bytes(chunk)
                    total += len(data)
                    if total > self.max_object_bytes:
                        raise CloudObjectCacheError("object exceeds local cache object size limit")
                    digest.update(data)
                    handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            actual_sha = digest.hexdigest()
            if actual_sha != sha256:
                raise CloudObjectCacheError("object sha256 mismatch")
            if expected_size and total != expected_size:
                raise CloudObjectCacheError("object size mismatch")

            os.replace(tmp_path, target)
            _write_json_atomic(
                meta_path,
                {
                    "object_id": str(normalized.get("object_id") or ""),
                    "sha256": sha256,
                    "size_bytes": total,
                    "expected_size_bytes": expected_size,
                    "content_type": str(normalized.get("content_type") or ""),
                    "compression": str(normalized.get("compression") or ""),
                    "cached_at": local_now().isoformat(timespec="seconds"),
                    "trace_id": str(trace_id or "").strip(),
                },
            )
        except BaseException:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise

        return {
            "ok": True,
            "object_id": str(normalized.get("object_id") or ""),
            "sha256": sha256,
            "size_bytes": total,
            "path": str(target),
            "metadata_path": str(meta_path),
        }

    def object_path(self, object_ref: dict[str, Any]) -> Path:
        normalized = normalize_object_ref(object_ref)
        sha256 = str(normalized.get("sha256") or "")
        if not sha256:
            raise CloudObjectCacheError("object_ref.sha256 is required")
        return self.root_dir / sha256[:2] / sha256[2:4] / sha256

    def metadata_path(self, object_ref: dict[str, Any]) -> Path:
        return self.object_path(object_ref).with_suffix(".json")

    def cached_object(self, object_ref: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_object_ref(object_ref)
        target = self.object_path(normalized)
        exists = target.is_file()
        size = target.stat().st_size if exists else 0
        valid = False
        if exists:
            expected_size = int(normalized.get("size_bytes") or 0)
            valid = expected_size <= 0 or expected_size == size
            if valid:
                try:
                    os.utime(target, None)
                except OSError:
                    pass
        return {
            "exists": exists,
            "valid": bool(valid),
            "path": str(target),
            "metadata_path": str(self.metadata_path(normalized)),
            "sha256": str(normalized.get("sha256") or ""),
            "size_bytes": size,
        }

    def diagnostics(self) -> dict[str, Any]:
        entries = self._entries()
        total_bytes = sum(entry.size_bytes for entry in entries)
        return {
            "path": str(self.root_dir),
            "objects": len(entries),
            "bytes": total_bytes,
            "max_object_bytes": self.max_object_bytes,
            "max_cache_bytes": self.max_cache_bytes,
        }

    def prune(self, *, target_bytes: int | None = None) -> dict[str, Any]:
        """Prune least-recently-used cached objects until the cache is under budget."""
        entries = self._entries()
        before_bytes = sum(entry.size_bytes for entry in entries)
        max_bytes = self.max_cache_bytes
        safe_target = int(target_bytes) if target_bytes is not None else int(max_bytes * DEFAULT_PRUNE_TARGET_RATIO)
        safe_target = max(0, min(safe_target, max_bytes))
        if before_bytes <= max_bytes:
            return {
                "ok": True,
                "pruned": 0,
                "bytes_removed": 0,
                "before_bytes": before_bytes,
                "after_bytes": before_bytes,
                "target_bytes": safe_target,
            }
        after_bytes = before_bytes
        pruned = 0
        removed = 0
        for entry in sorted(entries, key=lambda item: (item.mtime, item.path.name)):
            if after_bytes <= safe_target:
                break
            try:
                entry.path.unlink()
                metadata_path = entry.path.with_suffix(".json")
                if metadata_path.exists():
                    metadata_path.unlink()
                _remove_empty_parents(entry.path.parent, stop_at=self.root_dir)
            except OSError:
                continue
            pruned += 1
            removed += entry.size_bytes
            after_bytes -= entry.size_bytes
        return {
            "ok": True,
            "pruned": pruned,
            "bytes_removed": removed,
            "before_bytes": before_bytes,
            "after_bytes": max(0, after_bytes),
            "target_bytes": safe_target,
        }

    def _entries(self) -> list["_CacheEntry"]:
        root = self.root_dir
        entries: list[_CacheEntry] = []
        if not root.exists():
            return entries
        for path in root.glob("*/*/*"):
            if not path.is_file() or path.suffix == ".json" or path.name.startswith("."):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append(_CacheEntry(path=path, size_bytes=int(stat.st_size), mtime=float(stat.st_mtime)))
        return entries


@dataclass(frozen=True)
class _CacheEntry:
    path: Path
    size_bytes: int
    mtime: float


def normalize_object_ref(object_ref: dict[str, Any]) -> dict[str, Any]:
    payload = object_ref if isinstance(object_ref, dict) else {}
    sha256 = str(payload.get("sha256") or "").strip().lower()
    if sha256 and (len(sha256) != _SHA256_HEX_LENGTH or any(ch not in "0123456789abcdef" for ch in sha256)):
        raise CloudObjectCacheError("object_ref.sha256 must be 64 hex characters")
    return {
        "object_id": str(payload.get("object_id") or payload.get("objectId") or payload.get("id") or "").strip(),
        "sha256": sha256,
        "size_bytes": _safe_int(payload.get("size_bytes") or payload.get("sizeBytes")),
        "storage_size_bytes": _safe_int(payload.get("storage_size_bytes") or payload.get("storageSizeBytes")),
        "content_type": str(payload.get("content_type") or payload.get("contentType") or "").strip(),
        "compression": str(payload.get("compression") or "").strip(),
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _configured_max_cache_bytes(value: int | None) -> int:
    if value is not None:
        return max(1, int(value))
    raw = os.environ.get("AIBRANDMONITOR_CLOUD_OBJECT_CACHE_MAX_BYTES", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except Exception:
            pass
    return DEFAULT_MAX_CACHE_BYTES


def _remove_empty_parents(path: Path, *, stop_at: Path) -> None:
    stop = stop_at.resolve()
    current = path
    while True:
        try:
            if current.resolve() == stop:
                return
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except Exception:
        return int(default)
