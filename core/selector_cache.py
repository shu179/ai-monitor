"""Persistent learned selector cache keyed by browser profile."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


def _cache_path(profile_dir: str | os.PathLike[str]) -> Path:
    base = Path(profile_dir).expanduser()
    return base / "selectors_learned.yaml"


def load_selector_cache(profile_dir: str | os.PathLike[str]) -> dict[str, dict[str, Any]]:
    path = _cache_path(profile_dir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def get_learned_selector(profile_dir: str | os.PathLike[str], field_name: str) -> str:
    field = str(field_name or "").strip()
    if not field:
        return ""
    entry = load_selector_cache(profile_dir).get(field, {})
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("selector") or "").strip()


def set_learned_selector(
    profile_dir: str | os.PathLike[str],
    field_name: str,
    selector: str,
    *,
    source: str = "vision",
    note: str = "",
) -> str:
    field = str(field_name or "").strip()
    selector_text = str(selector or "").strip()
    if not field or not selector_text:
        return ""

    path = _cache_path(profile_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_selector_cache(profile_dir)
    previous = str((existing.get(field) or {}).get("selector") or "").strip()
    current = dict(existing.get(field) or {})
    current["selector"] = selector_text
    current["source"] = str(source or "").strip() or "vision"
    current["note"] = str(note or "").strip()
    current["updated_at"] = datetime.now().isoformat(timespec="seconds")
    current["hit_count"] = int(current.get("hit_count") or 0) + 1
    existing[field] = current

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(existing, handle, allow_unicode=True, sort_keys=False)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    return previous
