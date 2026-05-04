from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.api.deps import CurrentUser
from app.db.session import SessionLocal
from app.models import User
from app.services.event_service import (
    build_workspace_event_snapshot,
    diff_workspace_event_names,
    event_id_for_snapshot,
)

router = APIRouter()

STREAM_POLL_SECONDS = 5.0
STREAM_HEARTBEAT_SECONDS = 20.0
STREAM_MAX_SECONDS = 120.0


@router.get("/stream")
def stream_events(
    current_user: CurrentUser,
    last_event_id: str = Query(default="", max_length=256),
) -> StreamingResponse:
    del last_event_id
    user_context = {
        "id": current_user.id,
        "workspace_id": current_user.workspace_id,
        "token_version": current_user.token_version,
    }
    return StreamingResponse(
        _event_generator(user_context),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _event_generator(user_context: dict[str, int]) -> Iterator[str]:
    started_at = time.monotonic()
    last_heartbeat_at = 0.0
    previous_snapshot = _load_snapshot_or_none(user_context)
    yield _format_sse("hello", {"snapshot": previous_snapshot or {}}, event_id=event_id_for_snapshot(previous_snapshot or {}))

    while time.monotonic() - started_at < STREAM_MAX_SECONDS:
        time.sleep(STREAM_POLL_SECONDS)
        current_user = _load_current_user(user_context)
        if current_user is None:
            yield _format_sse("session_revoked", {"reason": "session_revoked"})
            break

        current_snapshot = _load_snapshot_for_user(current_user)
        event_names = diff_workspace_event_names(previous_snapshot or {}, current_snapshot)
        if event_names:
            event_id = event_id_for_snapshot(current_snapshot)
            for event_name in event_names:
                yield _format_sse(event_name, {"snapshot": current_snapshot}, event_id=event_id)
            previous_snapshot = current_snapshot
            last_heartbeat_at = time.monotonic()
            continue

        if time.monotonic() - last_heartbeat_at >= STREAM_HEARTBEAT_SECONDS:
            yield _format_sse("heartbeat", {"snapshot": current_snapshot}, event_id=event_id_for_snapshot(current_snapshot))
            last_heartbeat_at = time.monotonic()


def _load_current_user(user_context: dict[str, int]) -> User | None:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.id == user_context["id"]))
        if not user:
            return None
        if user.workspace_id != user_context["workspace_id"]:
            return None
        if not user.enabled:
            return None
        if int(user.token_version or 0) != int(user_context["token_version"] or 0):
            return None
        db.expunge(user)
        return user


def _load_snapshot_or_none(user_context: dict[str, int]) -> dict[str, Any] | None:
    user = _load_current_user(user_context)
    if user is None:
        return None
    return _load_snapshot_for_user(user)


def _load_snapshot_for_user(user: User) -> dict[str, Any]:
    with SessionLocal() as db:
        attached_user = db.merge(user, load=False)
        return build_workspace_event_snapshot(db, attached_user)


def _format_sse(event: str, data: dict[str, Any], *, event_id: str = "") -> str:
    lines = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}")
    return "\n".join(lines) + "\n\n"
