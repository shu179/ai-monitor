from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import psycopg
from sqlalchemy import and_, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import AgentCommand, AgentResultChunk, CloudIdempotencyKey, User
from app.services.change_log_service import STREAM_AGENT_STATUS, record_workspace_change
from app.services.sync_v2_service import TTL_SECONDS

AGENT_COMMAND_NOTIFY_CHANNEL = "agent_commands"
AGENT_COMMAND_STATUS_PENDING = "pending"
AGENT_COMMAND_STATUS_DELIVERED = "delivered"
AGENT_COMMAND_STATUS_RUNNING = "running"
AGENT_COMMAND_STATUS_COMPLETED = "completed"
AGENT_COMMAND_STATUS_FAILED = "failed"
AGENT_COMMAND_STATUS_CANCELLED = "cancelled"
AGENT_COMMAND_VISIBILITY_SECONDS = 60
MAX_AGENT_PAYLOAD_BYTES = 256 * 1024
MAX_AGENT_CHUNK_BYTES = 256 * 1024


class AgentCommandError(RuntimeError):
    status_code = 400


class AgentCommandNotFound(AgentCommandError):
    status_code = 404


def create_agent_command(
    db: Session,
    user: User,
    *,
    idempotency_key: str,
    payload_json: dict[str, Any],
    target_device_id: str | None = None,
    target_role: str | None = None,
) -> dict[str, Any]:
    safe_key = _safe_text(idempotency_key, limit=128)
    if not safe_key:
        raise AgentCommandError("idempotency_key is required")
    safe_payload = _safe_payload(payload_json, max_bytes=MAX_AGENT_PAYLOAD_BYTES)
    reserved = _reserve_agent_idempotency_key(db, workspace_id=user.workspace_id, idempotency_key=safe_key)
    existing = db.scalar(
        select(AgentCommand).where(
            AgentCommand.workspace_id == user.workspace_id,
            AgentCommand.idempotency_key == safe_key,
        )
    )
    if not reserved and existing is not None:
        return _agent_command_payload(existing)
    now = datetime.now(timezone.utc)
    command = AgentCommand(
        id=str(uuid4()),
        workspace_id=user.workspace_id,
        target_device_id=_safe_optional_text(target_device_id, limit=256),
        target_role=_safe_optional_text(target_role, limit=64),
        status=AGENT_COMMAND_STATUS_PENDING,
        visibility_until=now,
        idempotency_key=safe_key,
        payload_json=safe_payload,
        expires_at=now + timedelta(seconds=TTL_SECONDS["agent_command_pending"]),
    )
    db.add(command)
    record_workspace_change(
        db,
        workspace_id=user.workspace_id,
        stream=STREAM_AGENT_STATUS,
        kind="agent.command.created",
        ref_id=command.id,
    )
    _notify_agent_command(db, workspace_id=user.workspace_id, device_id=command.target_device_id)
    db.commit()
    db.refresh(command)
    return _agent_command_payload(command)


def claim_agent_command(
    db: Session,
    user: User,
    *,
    device_id: str,
    target_role: str | None = None,
    last_seen_command_id: str | None = None,
    visibility_seconds: int = AGENT_COMMAND_VISIBILITY_SECONDS,
) -> dict[str, Any] | None:
    del last_seen_command_id
    safe_device_id = _safe_text(device_id, limit=256)
    if not safe_device_id:
        raise AgentCommandError("device_id is required")
    now = datetime.now(timezone.utc)
    command = db.scalar(
        select(AgentCommand)
        .where(
            AgentCommand.workspace_id == user.workspace_id,
            AgentCommand.status == AGENT_COMMAND_STATUS_PENDING,
            AgentCommand.expires_at > now,
            or_(AgentCommand.visibility_until.is_(None), AgentCommand.visibility_until <= now),
            or_(AgentCommand.target_device_id.is_(None), AgentCommand.target_device_id == safe_device_id),
            or_(AgentCommand.target_role.is_(None), AgentCommand.target_role == _safe_optional_text(target_role, limit=64)),
        )
        .order_by(AgentCommand.created_at.asc(), AgentCommand.id.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if command is None:
        return None
    command.status = AGENT_COMMAND_STATUS_DELIVERED
    command.target_device_id = command.target_device_id or safe_device_id
    command.visibility_until = now + timedelta(seconds=max(1, int(visibility_seconds or AGENT_COMMAND_VISIBILITY_SECONDS)))
    record_workspace_change(
        db,
        workspace_id=user.workspace_id,
        stream=STREAM_AGENT_STATUS,
        kind="agent.command.delivered",
        ref_id=command.id,
    )
    db.commit()
    db.refresh(command)
    return _agent_command_payload(command)


def heartbeat_agent_command(
    db: Session,
    user: User,
    *,
    command_id: str,
    device_id: str,
    visibility_seconds: int = AGENT_COMMAND_VISIBILITY_SECONDS,
) -> dict[str, Any]:
    command = _load_agent_command_for_device(db, user, command_id=command_id, device_id=device_id)
    command.status = AGENT_COMMAND_STATUS_RUNNING
    command.visibility_until = datetime.now(timezone.utc) + timedelta(
        seconds=max(1, int(visibility_seconds or AGENT_COMMAND_VISIBILITY_SECONDS))
    )
    record_workspace_change(
        db,
        workspace_id=user.workspace_id,
        stream=STREAM_AGENT_STATUS,
        kind="agent.command.running",
        ref_id=command.id,
    )
    db.commit()
    db.refresh(command)
    return _agent_command_payload(command)


def append_agent_result_chunk(
    db: Session,
    user: User,
    *,
    command_id: str,
    device_id: str,
    seq: int,
    payload_json: dict[str, Any],
    is_final: bool = False,
    final_status: str | None = None,
) -> dict[str, Any]:
    command = _load_agent_command_for_device(db, user, command_id=command_id, device_id=device_id)
    safe_seq = int(seq)
    if safe_seq < 0:
        raise AgentCommandError("seq must be >= 0")
    safe_payload = _safe_payload(payload_json, max_bytes=MAX_AGENT_CHUNK_BYTES)
    stmt = (
        insert(AgentResultChunk)
        .values(
            command_id=command.id,
            seq=safe_seq,
            payload_json=safe_payload,
            is_final=bool(is_final),
        )
        .on_conflict_do_update(
            index_elements=["command_id", "seq"],
            set_={
                "payload_json": safe_payload,
                "is_final": bool(is_final),
            },
        )
    )
    db.execute(stmt)
    kind = "agent.result.chunk"
    if is_final:
        command.status = _final_status(final_status)
        command.visibility_until = None
        kind = f"agent.command.{command.status}"
    else:
        command.status = AGENT_COMMAND_STATUS_RUNNING
    record_workspace_change(
        db,
        workspace_id=user.workspace_id,
        stream=STREAM_AGENT_STATUS,
        kind=kind,
        ref_id=command.id,
    )
    db.commit()
    return {
        "command_id": str(command.id),
        "seq": safe_seq,
        "is_final": bool(is_final),
        "status": str(command.status),
    }


def cancel_agent_command(db: Session, user: User, *, command_id: str) -> dict[str, Any]:
    command = db.scalar(
        select(AgentCommand).where(
            AgentCommand.workspace_id == user.workspace_id,
            AgentCommand.id == str(command_id),
        )
    )
    if command is None:
        raise AgentCommandNotFound("agent command not found")
    command.cancel_requested_at = datetime.now(timezone.utc)
    if command.status == AGENT_COMMAND_STATUS_PENDING:
        command.status = AGENT_COMMAND_STATUS_CANCELLED
    record_workspace_change(
        db,
        workspace_id=user.workspace_id,
        stream=STREAM_AGENT_STATUS,
        kind="agent.command.cancel_requested",
        ref_id=command.id,
    )
    _notify_agent_command(db, workspace_id=user.workspace_id, device_id=command.target_device_id)
    db.commit()
    db.refresh(command)
    return _agent_command_payload(command)


def list_agent_result_chunks(db: Session, user: User, *, command_id: str, after_seq: int = -1, limit: int = 500) -> list[dict[str, Any]]:
    command = db.scalar(
        select(AgentCommand.id).where(
            AgentCommand.workspace_id == user.workspace_id,
            AgentCommand.id == str(command_id),
        )
    )
    if command is None:
        raise AgentCommandNotFound("agent command not found")
    rows = db.scalars(
        select(AgentResultChunk)
        .where(
            AgentResultChunk.command_id == str(command_id),
            AgentResultChunk.seq > int(after_seq),
        )
        .order_by(AgentResultChunk.seq.asc())
        .limit(max(1, min(int(limit or 500), 1000)))
    )
    return [
        {
            "command_id": str(row.command_id),
            "seq": int(row.seq),
            "payload_json": dict(row.payload_json or {}),
            "is_final": bool(row.is_final),
            "created_at": row.created_at.isoformat() if row.created_at else "",
        }
        for row in rows
    ]


def agent_command_notify_payload(*, workspace_id: int, device_id: str | None = None) -> str:
    return json.dumps(
        {"workspace_id": int(workspace_id), "device_id": str(device_id or "")},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def wait_for_agent_command_notifications(
    *,
    workspace_id: int,
    device_id: str,
    timeout_seconds: float,
    database_url: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield agent command NOTIFY payloads routed to one local device."""
    deadline = time.monotonic() + max(0.1, float(timeout_seconds or 0.1))
    with AgentCommandNotificationListener(
        workspace_id=workspace_id,
        device_id=device_id,
        database_url=database_url,
    ) as listener:
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            payloads = listener.wait(timeout_seconds=remaining)
            if not payloads:
                break
            yield from payloads


class AgentCommandNotificationListener:
    """Hold one Postgres LISTEN connection for an agent websocket lifecycle.

    All API workers listen on one shared channel and route the tiny
    ``workspace_id/device_id`` payload in-process to their local websocket map.
    Command bodies are never sent via NOTIFY; the daemon claims them from the DB.
    """

    def __init__(self, *, workspace_id: int, device_id: str, database_url: str | None = None) -> None:
        self.workspace_id = int(workspace_id)
        self.device_id = _safe_text(device_id, limit=256)
        self.database_url = database_url
        self._conn: Any | None = None

    def __enter__(self) -> "AgentCommandNotificationListener":
        url = self.database_url or get_settings().database_url
        try:
            self._conn = psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://"), autocommit=True)
            self._conn.execute(f"LISTEN {AGENT_COMMAND_NOTIFY_CHANNEL}")
        except Exception:
            self.close()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            return

    def wait(self, *, timeout_seconds: float) -> list[dict[str, Any]]:
        safe_timeout = max(0.0, float(timeout_seconds or 0.0))
        if self._conn is None:
            if safe_timeout > 0:
                time.sleep(safe_timeout)
            return []
        deadline = time.monotonic() + safe_timeout
        while time.monotonic() <= deadline:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                for notify in self._conn.notifies(timeout=remaining, stop_after=1):
                    payload = _decode_agent_command_notify_payload(notify.payload)
                    if not self._payload_matches_device(payload):
                        break
                    return [payload]
                else:
                    return []
            except Exception:
                return []
        return []

    def _payload_matches_device(self, payload: dict[str, Any]) -> bool:
        if int(payload.get("workspace_id") or 0) != self.workspace_id:
            return False
        target_device_id = _safe_text(payload.get("device_id"), limit=256)
        return not target_device_id or target_device_id == self.device_id


def _decode_agent_command_notify_payload(payload: str) -> dict[str, Any]:
    try:
        decoded = json.loads(str(payload or "{}"))
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _load_agent_command_for_device(db: Session, user: User, *, command_id: str, device_id: str) -> AgentCommand:
    command = db.scalar(
        select(AgentCommand).where(
            AgentCommand.workspace_id == user.workspace_id,
            AgentCommand.id == str(command_id),
            or_(AgentCommand.target_device_id.is_(None), AgentCommand.target_device_id == _safe_text(device_id, limit=256)),
        )
    )
    if command is None:
        raise AgentCommandNotFound("agent command not found")
    return command


def _reserve_agent_idempotency_key(db: Session, *, workspace_id: int, idempotency_key: str) -> bool:
    stmt = (
        insert(CloudIdempotencyKey)
        .values(workspace_id=int(workspace_id), scope="agent_command", idempotency_key=str(idempotency_key))
        .on_conflict_do_nothing(index_elements=["workspace_id", "scope", "idempotency_key"])
        .returning(CloudIdempotencyKey.idempotency_key)
    )
    return db.scalar(stmt) is not None


def _notify_agent_command(db: Session, *, workspace_id: int, device_id: str | None = None) -> None:
    db.execute(
        text(f"SELECT pg_notify('{AGENT_COMMAND_NOTIFY_CHANNEL}', :payload)"),
        {"payload": agent_command_notify_payload(workspace_id=workspace_id, device_id=device_id)},
    )


def _agent_command_payload(command: AgentCommand) -> dict[str, Any]:
    return {
        "id": str(command.id),
        "workspace_id": int(command.workspace_id),
        "target_device_id": command.target_device_id,
        "target_role": command.target_role,
        "status": str(command.status),
        "visibility_until": command.visibility_until,
        "idempotency_key": str(command.idempotency_key),
        "payload_json": dict(command.payload_json or {}),
        "cancel_requested_at": command.cancel_requested_at,
        "expires_at": command.expires_at,
        "created_at": command.created_at,
    }


def _safe_text(value: str | None, *, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _safe_optional_text(value: str | None, *, limit: int) -> str | None:
    text = _safe_text(value, limit=limit)
    return text or None


def _safe_payload(value: dict[str, Any], *, max_bytes: int) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > int(max_bytes):
        raise AgentCommandError("payload is too large")
    return dict(payload)


def _final_status(value: str | None) -> str:
    text_value = str(value or AGENT_COMMAND_STATUS_COMPLETED).strip().lower()
    if text_value in {AGENT_COMMAND_STATUS_COMPLETED, AGENT_COMMAND_STATUS_FAILED, AGENT_COMMAND_STATUS_CANCELLED}:
        return text_value
    return AGENT_COMMAND_STATUS_COMPLETED
