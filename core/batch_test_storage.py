from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .file_lock import CrossProcessRLock


def read_batch_json(path: Path) -> dict[str, Any]:
    with CrossProcessRLock(_batch_lock_file(path)):
        return _read_json_object(path)


def write_batch_json(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload or {})
    with CrossProcessRLock(_batch_lock_file(path)):
        _write_json_atomic(path, data)
    return data


def merge_batch_json(path: Path, updates: dict[str, Any], *, create: bool = False) -> dict[str, Any]:
    with CrossProcessRLock(_batch_lock_file(path)):
        existing = _read_json_object(path)
        if not existing and not path.exists() and not create:
            return {}
        merged = dict(existing)
        merged.update(dict(updates or {}))
        _write_json_atomic(path, merged)
        return merged


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _batch_lock_file(path: Path) -> Path:
    path = Path(path)
    return path.with_name(f"{path.name}.lock")
