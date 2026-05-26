from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import select

from app.api.deps import CurrentUser
from app.db.session import SessionLocal
from app.models import User
from app.services.change_log_service import (
    compact_change_snapshot,
    diff_change_streams,
    event_id_for_change_snapshot,
    wait_for_workspace_change_notifications,
)
from app.services.event_service import (
    build_workspace_event_snapshot,
    diff_workspace_event_names,
    event_id_for_snapshot,
)
from app.sync_event_types import (
    EVENT_ARTICLE_CHANGED,
    EVENT_REFERENCE_CHANGED,
    EVENT_RUN_RECORD_CHANGED,
    EVENT_TASK_CHANGED,
    EVENT_WORKSPACE_CHANGED,
)

router = APIRouter()

STREAM_POLL_SECONDS = 2.0
CHANGE_STREAM_POLL_SECONDS = 30.0
STREAM_HEARTBEAT_SECONDS = 20.0
STREAM_MAX_SECONDS = 120.0
STREAM_EVENT_BY_CHANGE_STREAM = {
    "tasks": EVENT_TASK_CHANGED,
    "runs": EVENT_RUN_RECORD_CHANGED,
    "articles": EVENT_ARTICLE_CHANGED,
    "references": EVENT_REFERENCE_CHANGED,
    "profile": EVENT_WORKSPACE_CHANGED,
    "agent_status": EVENT_WORKSPACE_CHANGED,
}


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
    yield from _change_event_generator(user_context)


def _change_event_generator(user_context: dict[str, int]) -> Iterator[str]:
    previous_changes = _load_change_snapshot_or_none(user_context)
    if previous_changes is None:
        yield from _legacy_event_generator(user_context)
        return

    started_at = time.monotonic()
    last_heartbeat_at = time.monotonic()
    yield _format_sse("hello", {"changes": previous_changes}, event_id=event_id_for_change_snapshot(previous_changes))

    while time.monotonic() - started_at < STREAM_MAX_SECONDS:
        current_user = _load_current_user(user_context)
        if current_user is None:
            yield _format_sse("session_revoked", {"reason": "session_revoked"})
            break

        saw_notify = False
        for payload in wait_for_workspace_change_notifications(
            workspace_id=user_context["workspace_id"],
            timeout_seconds=min(CHANGE_STREAM_POLL_SECONDS, STREAM_MAX_SECONDS),
        ):
            saw_notify = True
            stream = str(payload.get("stream") or "").strip()
            seq = int(payload.get("seq") or 0)
            if stream:
                previous_changes[stream] = max(int(previous_changes.get(stream) or 0), seq)
                yield _format_sse(
                    STREAM_EVENT_BY_CHANGE_STREAM.get(stream, EVENT_WORKSPACE_CHANGED),
                    {"stream": stream, "seq": seq},
                    event_id=event_id_for_change_snapshot(previous_changes),
                )
                last_heartbeat_at = time.monotonic()
        current_changes = _load_change_snapshot_or_none(user_context)
        if current_changes is None:
            yield from _legacy_event_generator(user_context)
            return
        changed_streams = diff_change_streams(previous_changes, current_changes)
        if changed_streams:
            event_id = event_id_for_change_snapshot(current_changes)
            for stream in changed_streams:
                yield _format_sse(
                    STREAM_EVENT_BY_CHANGE_STREAM.get(stream, EVENT_WORKSPACE_CHANGED),
                    {"stream": stream, "seq": int(current_changes.get(stream) or 0)},
                    event_id=event_id,
                )
            previous_changes = current_changes
            last_heartbeat_at = time.monotonic()
            continue

        if not saw_notify and time.monotonic() - last_heartbeat_at >= STREAM_HEARTBEAT_SECONDS:
            yield _format_sse("heartbeat", {"changes": current_changes}, event_id=event_id_for_change_snapshot(current_changes))
            last_heartbeat_at = time.monotonic()


def _legacy_event_generator(user_context: dict[str, int]) -> Iterator[str]:
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


def _load_change_snapshot_or_none(user_context: dict[str, int]) -> dict[str, int] | None:
    user = _load_current_user(user_context)
    if user is None:
        return None
    with SessionLocal() as db:
        try:
            return compact_change_snapshot(db, user.workspace_id)
        except SQLAlchemyError:
            return None


def _format_sse(event: str, data: dict[str, Any], *, event_id: str = "") -> str:
    lines = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}")
    return "\n".join(lines) + "\n\n"
