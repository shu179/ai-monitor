from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from jwt import InvalidTokenError
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.core.security import decode_token
from app.db.session import SessionLocal
from app.models import User
from app.schemas import (
    AgentCommandClaimRequest,
    AgentCommandClaimResponse,
    AgentCommandCreateRequest,
    AgentCommandPublic,
    AgentHeartbeatRequest,
    AgentResultChunkAck,
    AgentResultChunkPublic,
    AgentResultChunkRequest,
)
from app.services.agent_service import (
    AgentCommandError,
    append_agent_result_chunk,
    cancel_agent_command,
    claim_agent_command,
    create_agent_command,
    heartbeat_agent_command,
    list_agent_result_chunks,
)

router = APIRouter()


@router.post("/commands", response_model=AgentCommandPublic)
def create_command(payload: AgentCommandCreateRequest, current_user: CurrentUser, db: DbSession) -> AgentCommandPublic:
    try:
        return AgentCommandPublic(
            **create_agent_command(
                db,
                current_user,
                idempotency_key=payload.idempotency_key,
                payload_json=payload.payload_json,
                target_device_id=payload.target_device_id,
                target_role=payload.target_role,
            )
        )
    except AgentCommandError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/commands:claim", response_model=AgentCommandClaimResponse)
def claim_command(payload: AgentCommandClaimRequest, current_user: CurrentUser, db: DbSession) -> AgentCommandClaimResponse:
    try:
        command = claim_agent_command(
            db,
            current_user,
            device_id=payload.device_id,
            target_role=payload.target_role,
            last_seen_command_id=payload.last_seen_command_id,
        )
    except AgentCommandError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return AgentCommandClaimResponse(command=AgentCommandPublic(**command) if command else None)


@router.post("/commands/{command_id}:heartbeat", response_model=AgentCommandPublic)
def heartbeat_command(
    command_id: str,
    payload: AgentHeartbeatRequest,
    current_user: CurrentUser,
    db: DbSession,
) -> AgentCommandPublic:
    try:
        return AgentCommandPublic(
            **heartbeat_agent_command(
                db,
                current_user,
                command_id=command_id,
                device_id=payload.device_id,
            )
        )
    except AgentCommandError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/commands/{command_id}/chunks", response_model=AgentResultChunkAck)
def append_result_chunk(
    command_id: str,
    payload: AgentResultChunkRequest,
    current_user: CurrentUser,
    db: DbSession,
) -> AgentResultChunkAck:
    try:
        return AgentResultChunkAck(
            **append_agent_result_chunk(
                db,
                current_user,
                command_id=command_id,
                device_id=payload.device_id,
                seq=payload.seq,
                payload_json=payload.payload_json,
                is_final=payload.is_final,
                final_status=payload.final_status,
            )
        )
    except AgentCommandError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/commands/{command_id}:cancel", response_model=AgentCommandPublic)
def cancel_command(command_id: str, current_user: CurrentUser, db: DbSession) -> AgentCommandPublic:
    try:
        return AgentCommandPublic(**cancel_agent_command(db, current_user, command_id=command_id))
    except AgentCommandError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/commands/{command_id}/chunks", response_model=list[AgentResultChunkPublic])
def command_chunks(
    command_id: str,
    current_user: CurrentUser,
    db: DbSession,
    after_seq: int = Query(default=-1, ge=-1),
    limit: int = Query(default=500, ge=1, le=1000),
) -> list[AgentResultChunkPublic]:
    try:
        return [
            AgentResultChunkPublic(**item)
            for item in list_agent_result_chunks(
                db,
                current_user,
                command_id=command_id,
                after_seq=after_seq,
                limit=limit,
            )
        ]
    except AgentCommandError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.websocket("/ws")
async def agent_ws(
    websocket: WebSocket,
    token: str = Query(default=""),
    device_id: str = Query(default="", max_length=256),
    target_role: str = Query(default="", max_length=64),
) -> None:
    user_context = _decode_ws_user(token)
    if user_context is None or not device_id:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    try:
        while True:
            with SessionLocal() as db:
                user = _load_ws_user(db, user_context)
                if user is None:
                    await websocket.send_json({"type": "session_revoked"})
                    await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                    return
                command = claim_agent_command(
                    db,
                    user,
                    device_id=device_id,
                    target_role=target_role or None,
                )
            await websocket.send_json({"type": "command", "command": command})
            message = await websocket.receive_json()
            if not isinstance(message, dict):
                continue
            await _handle_ws_message(message, user_context=user_context, device_id=device_id, websocket=websocket)
    except WebSocketDisconnect:
        return


async def _handle_ws_message(message: dict, *, user_context: dict[str, int], device_id: str, websocket: WebSocket) -> None:
    message_type = str(message.get("type") or "").strip()
    command_id = str(message.get("command_id") or "").strip()
    if message_type not in {"heartbeat", "result_chunk"} or not command_id:
        await websocket.send_json({"type": "error", "message": "unsupported message"})
        return
    with SessionLocal() as db:
        user = _load_ws_user(db, user_context)
        if user is None:
            await websocket.send_json({"type": "session_revoked"})
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        try:
            if message_type == "heartbeat":
                payload = heartbeat_agent_command(db, user, command_id=command_id, device_id=device_id)
                await websocket.send_json({"type": "heartbeat_ack", "command": payload})
                return
            ack = append_agent_result_chunk(
                db,
                user,
                command_id=command_id,
                device_id=device_id,
                seq=int(message.get("seq") or 0),
                payload_json=message.get("payload_json") if isinstance(message.get("payload_json"), dict) else {},
                is_final=bool(message.get("is_final")),
                final_status=message.get("final_status"),
            )
            await websocket.send_json({"type": "result_chunk_ack", **ack})
        except AgentCommandError as exc:
            await websocket.send_json({"type": "error", "message": str(exc)})


def _decode_ws_user(token: str) -> dict[str, int] | None:
    try:
        payload = decode_token(str(token or ""))
    except InvalidTokenError:
        return None
    if payload.get("type") != "access":
        return None
    return {
        "id": int(payload.get("sub") or 0),
        "workspace_id": int(payload.get("workspace_id") or 0),
        "token_version": int(payload.get("token_version") or 0),
    }


def _load_ws_user(db, user_context: dict[str, int]) -> User | None:
    user = db.scalar(select(User).where(User.id == user_context["id"], User.deleted_at.is_(None)))
    if not user:
        return None
    if int(user.workspace_id or 0) != int(user_context["workspace_id"] or 0):
        return None
    if not user.enabled:
        return None
    if int(user.token_version or 0) != int(user_context["token_version"] or 0):
        return None
    return user
