from __future__ import annotations

from pathlib import Path
from typing import Any

from .app_paths import resolve_app_path
from .cloud_session_store import cloud_session_identity_key
from .local_account_space import account_scoped_path
from .sqlite_json_store import MISSING, SQLiteJsonDocumentStore
from .time_utils import local_now


DEFAULT_CLOUD_AGENT_STATUS_DB_PATH = resolve_app_path("user_data/cloud_agent_status.sqlite3")
MAX_AGENT_STATUS_COMMANDS = 500
MAX_AGENT_STATUS_CHUNKS_PER_COMMAND = 100


class CloudAgentStatusStore:
    """Read-only local cache for cloud Agent command status.

    This store intentionally does not execute Agent commands. It keeps the
    status/result side of state-delta durable so diagnostics and future Agent
    reconnect logic can inspect what the cloud has already reported.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._explicit_path = Path(db_path) if db_path is not None else None

    @property
    def db_path(self) -> Path:
        if self._explicit_path is not None:
            return self._explicit_path
        return account_scoped_path(
            "user_data/cloud_agent_status.sqlite3",
            fallback=DEFAULT_CLOUD_AGENT_STATUS_DB_PATH,
        )

    def apply_status(self, identity_key: str, entity: dict[str, Any]) -> dict[str, Any]:
        safe_identity = str(identity_key or "").strip()
        normalized = _normalize_agent_status_entity(entity)
        if not safe_identity:
            return {"ok": False, "message": "missing identity_key", "updated": 0}
        if not normalized:
            return {"ok": False, "message": "invalid agent status entity", "updated": 0}

        state = self._load_state(safe_identity)
        commands = state.setdefault("commands", {})
        command_id = str(normalized.get("id") or "").strip()
        previous = commands.get(command_id) if isinstance(commands.get(command_id), dict) else {}
        merged = dict(previous or {})
        merged.update(normalized)
        merged["result_chunks"] = _merge_result_chunks(
            previous.get("result_chunks") if isinstance(previous, dict) else [],
            normalized.get("result_chunks"),
        )
        commands[command_id] = merged
        state["commands"] = _trim_commands(commands)
        state["updated_at"] = local_now().isoformat(timespec="seconds")
        self._save_state(safe_identity, state)
        return {
            "ok": True,
            "updated": 1,
            "command_id": command_id,
            "status": str(merged.get("status") or ""),
            "result_chunks": len(merged.get("result_chunks") or []),
        }

    def diagnostics(self, session: dict[str, Any] | None = None) -> dict[str, Any]:
        identity_key = cloud_session_identity_key(session) if session is not None else ""
        state = self._load_state(identity_key) if identity_key else {}
        commands = state.get("commands") if isinstance(state.get("commands"), dict) else {}
        by_status: dict[str, int] = {}
        newest = []
        for command_id, command in commands.items():
            if not isinstance(command, dict):
                continue
            status = str(command.get("status") or "unknown")
            by_status[status] = by_status.get(status, 0) + 1
            newest.append(
                {
                    "id": str(command_id),
                    "status": status,
                    "target_device_id": str(command.get("target_device_id") or ""),
                    "target_role": str(command.get("target_role") or ""),
                    "chunk_count": len(command.get("result_chunks") or []),
                    "created_at": str(command.get("created_at") or ""),
                    "updated_at": str(command.get("updated_at") or command.get("created_at") or ""),
                }
            )
        newest.sort(key=lambda item: (item["updated_at"], item["id"]), reverse=True)
        return {
            "path": str(self.db_path),
            "identity_key": identity_key,
            "total": len(commands),
            "by_status": by_status,
            "newest": newest[:10],
            "updated_at": str(state.get("updated_at") or ""),
        }

    def _load_state(self, identity_key: str) -> dict[str, Any]:
        if not identity_key:
            return {"identity_key": "", "commands": {}, "updated_at": ""}
        loaded = SQLiteJsonDocumentStore(self.db_path).load(_state_key(identity_key), default=MISSING)
        if not isinstance(loaded, dict):
            return {"identity_key": identity_key, "commands": {}, "updated_at": ""}
        if str(loaded.get("identity_key") or "") != identity_key:
            return {"identity_key": identity_key, "commands": {}, "updated_at": ""}
        commands = loaded.get("commands") if isinstance(loaded.get("commands"), dict) else {}
        return {
            "identity_key": identity_key,
            "commands": commands,
            "updated_at": str(loaded.get("updated_at") or ""),
        }

    def _save_state(self, identity_key: str, state: dict[str, Any]) -> None:
        if not identity_key:
            return
        payload = {
            "identity_key": identity_key,
            "commands": state.get("commands") if isinstance(state.get("commands"), dict) else {},
            "updated_at": str(state.get("updated_at") or ""),
        }
        SQLiteJsonDocumentStore(self.db_path).save(_state_key(identity_key), payload)


def _state_key(identity_key: str) -> str:
    return f"agent_status:{identity_key}"


def _normalize_agent_status_entity(entity: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(entity, dict) or str(entity.get("type") or "") != "agent_command_status":
        return {}
    command_id = str(entity.get("id") or "").strip()
    if not command_id:
        return {}
    chunks = entity.get("result_chunks") if isinstance(entity.get("result_chunks"), list) else []
    return {
        "type": "agent_command_status",
        "id": command_id,
        "workspace_id": _safe_int(entity.get("workspace_id") or entity.get("workspaceId")),
        "target_device_id": str(entity.get("target_device_id") or entity.get("targetDeviceId") or "").strip(),
        "target_role": str(entity.get("target_role") or entity.get("targetRole") or "").strip(),
        "status": str(entity.get("status") or "").strip(),
        "visibility_until": str(entity.get("visibility_until") or entity.get("visibilityUntil") or "").strip(),
        "idempotency_key": str(entity.get("idempotency_key") or entity.get("idempotencyKey") or "").strip(),
        "cancel_requested_at": str(
            entity.get("cancel_requested_at") or entity.get("cancelRequestedAt") or ""
        ).strip(),
        "expires_at": str(entity.get("expires_at") or entity.get("expiresAt") or "").strip(),
        "created_at": str(entity.get("created_at") or entity.get("createdAt") or "").strip(),
        "updated_at": local_now().isoformat(timespec="seconds"),
        "result_chunks": [_normalize_result_chunk(chunk, command_id=command_id) for chunk in chunks],
    }


def _normalize_result_chunk(chunk: Any, *, command_id: str) -> dict[str, Any]:
    payload = chunk if isinstance(chunk, dict) else {}
    return {
        "command_id": str(payload.get("command_id") or payload.get("commandId") or command_id).strip(),
        "seq": _safe_int(payload.get("seq"), default=0),
        "payload_json": payload.get("payload_json") if isinstance(payload.get("payload_json"), dict) else {},
        "is_final": bool(payload.get("is_final") or payload.get("isFinal")),
        "created_at": str(payload.get("created_at") or payload.get("createdAt") or "").strip(),
    }


def _merge_result_chunks(existing: Any, incoming: Any) -> list[dict[str, Any]]:
    by_seq: dict[int, dict[str, Any]] = {}
    for source in (existing, incoming):
        if not isinstance(source, list):
            continue
        for chunk in source:
            if not isinstance(chunk, dict):
                continue
            seq = _safe_int(chunk.get("seq"), default=0)
            by_seq[seq] = dict(chunk)
    return [by_seq[seq] for seq in sorted(by_seq)][:MAX_AGENT_STATUS_CHUNKS_PER_COMMAND]


def _trim_commands(commands: dict[str, Any]) -> dict[str, dict[str, Any]]:
    items = [(key, value) for key, value in commands.items() if isinstance(value, dict)]
    items.sort(
        key=lambda item: (
            str(item[1].get("updated_at") or item[1].get("created_at") or ""),
            str(item[0]),
        ),
        reverse=True,
    )
    return {str(key): dict(value) for key, value in items[:MAX_AGENT_STATUS_COMMANDS]}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except Exception:
        return int(default)
