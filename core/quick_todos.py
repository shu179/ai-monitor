"""Quick todo normalization and expiry helpers."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from .time_utils import local_now

QUICK_TODO_TTL = timedelta(hours=3)


def _parse_completed_at(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _to_local_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    local_time = local_now()
    tz = local_time.tzinfo
    if value.tzinfo is None:
        return value if tz is None else value.replace(tzinfo=tz)
    return value if tz is None else value.astimezone(tz)


def _new_todo_id() -> str:
    return f"todo-{uuid4().hex[:12]}"


def normalize_quick_todos(
    items: list[Any] | None,
    *,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Normalize todos and purge completed items that expired."""
    current_time = _to_local_datetime(now or local_now()) or local_now()
    normalized: list[dict[str, Any]] = []
    changed = False

    for raw_item in items or []:
        if isinstance(raw_item, dict):
            item = deepcopy(raw_item)
            text = str(item.get("text", "")).strip()
            done = bool(item.get("done", False))
            todo_id = str(item.get("id", "")).strip()
            completed_at = _to_local_datetime(_parse_completed_at(
                item.get("completed_at", item.get("completedAt", "")),
            ))
        else:
            text = str(raw_item or "").strip()
            done = False
            todo_id = ""
            completed_at = None
            changed = True

        if not text:
            changed = True
            continue

        if not todo_id:
            todo_id = _new_todo_id()
            changed = True

        if done:
            if completed_at is None:
                completed_at = current_time
                changed = True
            elif current_time - completed_at >= QUICK_TODO_TTL:
                changed = True
                continue
        elif completed_at is not None:
            completed_at = None
            changed = True

        normalized_item = {
            "id": todo_id,
            "text": text,
            "done": done,
        }
        if completed_at is not None:
            normalized_item["completed_at"] = completed_at.isoformat(timespec="seconds")
        normalized.append(normalized_item)

        if isinstance(raw_item, dict):
            original_comp = str(raw_item.get("completed_at", raw_item.get("completedAt", "")) or "").strip()
            normalized_comp = normalized_item.get("completed_at", "")
            if (
                str(raw_item.get("id", "")).strip() != todo_id
                or str(raw_item.get("text", "")).strip() != text
                or bool(raw_item.get("done", False)) != done
                or original_comp != normalized_comp
            ):
                changed = True

    return normalized, changed
