from __future__ import annotations

from pathlib import Path
from typing import Any

from .app_paths import resolve_app_path
from .cloud_session_store import cloud_session_identity_key
from .local_account_space import account_scoped_path
from .sqlite_json_store import MISSING, SQLiteJsonDocumentStore
from .time_utils import local_now


DEFAULT_CLOUD_CONTENT_STATE_DB_PATH = resolve_app_path("user_data/cloud_content_state.sqlite3")
MAX_CACHED_ANSWERS = 1000
MAX_CACHED_ASSETS = 2000
MAX_INLINE_TEXT_CHARS = 128_000


class CloudContentStateStore:
    """Read-only local cache for content-oriented state-delta streams.

    This store is deliberately passive: it persists cloud answers/assets and
    their object references for diagnostics and future download plumbing, but it
    does not download objects, mutate visible UI state, or execute commands.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._explicit_path = Path(db_path) if db_path is not None else None

    @property
    def db_path(self) -> Path:
        if self._explicit_path is not None:
            return self._explicit_path
        return account_scoped_path(
            "user_data/cloud_content_state.sqlite3",
            fallback=DEFAULT_CLOUD_CONTENT_STATE_DB_PATH,
        )

    def apply_answer(
        self,
        identity_key: str,
        entity: dict[str, Any],
        *,
        object_refs: list[dict[str, Any]] | None = None,
        ref_id: str = "",
    ) -> dict[str, Any]:
        safe_identity = str(identity_key or "").strip()
        normalized = _normalize_answer_entity(entity, object_refs=object_refs, ref_id=ref_id)
        if not safe_identity:
            return {"ok": False, "message": "missing identity_key", "updated": 0}
        if not normalized:
            return {"ok": False, "message": "invalid answer entity", "updated": 0}

        state = self._load_state(safe_identity)
        answers = state.setdefault("answers", {})
        answer_id = str(normalized.get("id") or "").strip()
        previous = answers.get(answer_id) if isinstance(answers.get(answer_id), dict) else {}
        merged = dict(previous or {})
        merged.update(normalized)
        answers[answer_id] = merged
        state["answers"] = _trim_items(answers, limit=MAX_CACHED_ANSWERS)
        state["updated_at"] = local_now().isoformat(timespec="seconds")
        self._save_state(safe_identity, state)
        return {"ok": True, "updated": 1, "id": answer_id, "kind": "answer"}

    def apply_asset(
        self,
        identity_key: str,
        entity: dict[str, Any],
        *,
        object_refs: list[dict[str, Any]] | None = None,
        ref_id: str = "",
    ) -> dict[str, Any]:
        safe_identity = str(identity_key or "").strip()
        normalized = _normalize_asset_entity(entity, object_refs=object_refs, ref_id=ref_id)
        if not safe_identity:
            return {"ok": False, "message": "missing identity_key", "updated": 0}
        if not normalized:
            return {"ok": False, "message": "invalid asset entity", "updated": 0}

        state = self._load_state(safe_identity)
        assets = state.setdefault("assets", {})
        asset_id = str(normalized.get("id") or "").strip()
        previous = assets.get(asset_id) if isinstance(assets.get(asset_id), dict) else {}
        merged = dict(previous or {})
        merged.update(normalized)
        assets[asset_id] = merged
        state["assets"] = _trim_items(assets, limit=MAX_CACHED_ASSETS)
        state["updated_at"] = local_now().isoformat(timespec="seconds")
        self._save_state(safe_identity, state)
        return {"ok": True, "updated": 1, "id": asset_id, "kind": "asset"}

    def diagnostics(self, session: dict[str, Any] | None = None) -> dict[str, Any]:
        identity_key = cloud_session_identity_key(session) if session is not None else ""
        state = self._load_state(identity_key) if identity_key else {}
        answers = state.get("answers") if isinstance(state.get("answers"), dict) else {}
        assets = state.get("assets") if isinstance(state.get("assets"), dict) else {}
        answer_by_type: dict[str, int] = {}
        asset_by_status: dict[str, int] = {}
        newest_answers = []
        newest_assets = []
        for answer_id, answer in answers.items():
            if not isinstance(answer, dict):
                continue
            answer_type = str(answer.get("type") or "answer")
            answer_by_type[answer_type] = answer_by_type.get(answer_type, 0) + 1
            newest_answers.append(
                {
                    "id": str(answer_id),
                    "type": answer_type,
                    "run_record_id": str(answer.get("run_record_id") or ""),
                    "content_kind": str(answer.get("content_kind") or ""),
                    "object_ref_count": len(answer.get("object_refs") or []),
                    "updated_at": str(answer.get("updated_at") or answer.get("created_at") or ""),
                }
            )
        for asset_id, asset in assets.items():
            if not isinstance(asset, dict):
                continue
            status = str(asset.get("status") or "unknown")
            asset_by_status[status] = asset_by_status.get(status, 0) + 1
            newest_assets.append(
                {
                    "id": str(asset_id),
                    "object_id": str(asset.get("object_id") or ""),
                    "sha256": str(asset.get("sha256") or ""),
                    "content_type": str(asset.get("content_type") or ""),
                    "size_bytes": int(asset.get("size_bytes") or 0),
                    "storage_size_bytes": int(asset.get("storage_size_bytes") or 0),
                    "updated_at": str(asset.get("updated_at") or asset.get("created_at") or ""),
                }
            )
        newest_answers.sort(key=lambda item: (item["updated_at"], item["id"]), reverse=True)
        newest_assets.sort(key=lambda item: (item["updated_at"], item["id"]), reverse=True)
        return {
            "path": str(self.db_path),
            "identity_key": identity_key,
            "answers_total": len(answers),
            "assets_total": len(assets),
            "answer_by_type": answer_by_type,
            "asset_by_status": asset_by_status,
            "newest_answers": newest_answers[:10],
            "newest_assets": newest_assets[:10],
            "updated_at": str(state.get("updated_at") or ""),
        }

    def _load_state(self, identity_key: str) -> dict[str, Any]:
        if not identity_key:
            return {"identity_key": "", "answers": {}, "assets": {}, "updated_at": ""}
        loaded = SQLiteJsonDocumentStore(self.db_path).load(_state_key(identity_key), default=MISSING)
        if not isinstance(loaded, dict):
            return {"identity_key": identity_key, "answers": {}, "assets": {}, "updated_at": ""}
        if str(loaded.get("identity_key") or "") != identity_key:
            return {"identity_key": identity_key, "answers": {}, "assets": {}, "updated_at": ""}
        answers = loaded.get("answers") if isinstance(loaded.get("answers"), dict) else {}
        assets = loaded.get("assets") if isinstance(loaded.get("assets"), dict) else {}
        return {
            "identity_key": identity_key,
            "answers": answers,
            "assets": assets,
            "updated_at": str(loaded.get("updated_at") or ""),
        }

    def _save_state(self, identity_key: str, state: dict[str, Any]) -> None:
        if not identity_key:
            return
        payload = {
            "identity_key": identity_key,
            "answers": state.get("answers") if isinstance(state.get("answers"), dict) else {},
            "assets": state.get("assets") if isinstance(state.get("assets"), dict) else {},
            "updated_at": str(state.get("updated_at") or ""),
        }
        SQLiteJsonDocumentStore(self.db_path).save(_state_key(identity_key), payload)


def _state_key(identity_key: str) -> str:
    return f"content_state:{identity_key}"


def _normalize_answer_entity(
    entity: dict[str, Any],
    *,
    object_refs: list[dict[str, Any]] | None,
    ref_id: str,
) -> dict[str, Any]:
    if not isinstance(entity, dict):
        return {}
    answer_id = str(
        entity.get("id")
        or entity.get("answer_id")
        or entity.get("answerId")
        or entity.get("run_record_id")
        or entity.get("runRecordId")
        or ref_id
        or ""
    ).strip()
    if not answer_id:
        return {}
    inline_text = str(entity.get("answer_text") or entity.get("answerText") or entity.get("inline_text") or "")
    if len(inline_text) > MAX_INLINE_TEXT_CHARS:
        inline_text = inline_text[:MAX_INLINE_TEXT_CHARS]
    content_ref = entity.get("content_ref") if isinstance(entity.get("content_ref"), dict) else {}
    return {
        "type": str(entity.get("type") or "answer").strip() or "answer",
        "id": answer_id,
        "workspace_id": _safe_int(entity.get("workspace_id") or entity.get("workspaceId")),
        "task_id": _safe_int(entity.get("task_id") or entity.get("taskId")),
        "run_record_id": str(entity.get("run_record_id") or entity.get("runRecordId") or "").strip(),
        "platform": str(entity.get("platform") or "").strip(),
        "keyword": str(entity.get("keyword") or "").strip(),
        "brand": str(entity.get("brand") or "").strip(),
        "content_kind": str(content_ref.get("kind") or ("inline_text" if inline_text else "")).strip(),
        "answer_text": inline_text,
        "content_ref": dict(content_ref),
        "object_refs": _normalize_object_refs(object_refs),
        "created_at": str(entity.get("created_at") or entity.get("createdAt") or "").strip(),
        "updated_at": local_now().isoformat(timespec="seconds"),
    }


def _normalize_asset_entity(
    entity: dict[str, Any],
    *,
    object_refs: list[dict[str, Any]] | None,
    ref_id: str,
) -> dict[str, Any]:
    payload = entity if isinstance(entity, dict) else {}
    refs = _normalize_object_refs(object_refs)
    if not payload and refs:
        payload = refs[0]
    object_id = str(payload.get("object_id") or payload.get("objectId") or payload.get("id") or "").strip()
    sha256 = str(payload.get("sha256") or "").strip()
    asset_id = str(payload.get("asset_id") or payload.get("assetId") or object_id or sha256 or ref_id or "").strip()
    if not asset_id:
        return {}
    return {
        "type": str(payload.get("type") or "asset").strip() or "asset",
        "id": asset_id,
        "workspace_id": _safe_int(payload.get("workspace_id") or payload.get("workspaceId")),
        "object_id": object_id,
        "sha256": sha256,
        "size_bytes": _safe_int(payload.get("size_bytes") or payload.get("sizeBytes")),
        "storage_size_bytes": _safe_int(payload.get("storage_size_bytes") or payload.get("storageSizeBytes")),
        "content_type": str(payload.get("content_type") or payload.get("contentType") or "").strip(),
        "compression": str(payload.get("compression") or "").strip(),
        "storage_key": str(payload.get("storage_key") or payload.get("storageKey") or "").strip(),
        "status": str(payload.get("status") or "active").strip() or "active",
        "url": str(payload.get("url") or payload.get("download_url") or payload.get("downloadUrl") or "").strip(),
        "object_refs": refs,
        "created_at": str(payload.get("created_at") or payload.get("createdAt") or "").strip(),
        "updated_at": local_now().isoformat(timespec="seconds"),
    }


def _normalize_object_refs(object_refs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in object_refs or []:
        if not isinstance(ref, dict):
            continue
        object_id = str(ref.get("object_id") or ref.get("objectId") or ref.get("id") or "").strip()
        sha256 = str(ref.get("sha256") or "").strip()
        key = object_id or sha256
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(
            {
                "object_id": object_id,
                "sha256": sha256,
                "size_bytes": _safe_int(ref.get("size_bytes") or ref.get("sizeBytes")),
                "storage_size_bytes": _safe_int(ref.get("storage_size_bytes") or ref.get("storageSizeBytes")),
                "content_type": str(ref.get("content_type") or ref.get("contentType") or "").strip(),
                "compression": str(ref.get("compression") or "").strip(),
                "storage_key": str(ref.get("storage_key") or ref.get("storageKey") or "").strip(),
            }
        )
    return normalized


def _trim_items(items: dict[str, Any], *, limit: int) -> dict[str, dict[str, Any]]:
    rows = [(key, value) for key, value in items.items() if isinstance(value, dict)]
    rows.sort(
        key=lambda item: (
            str(item[1].get("updated_at") or item[1].get("created_at") or ""),
            str(item[0]),
        ),
        reverse=True,
    )
    return {str(key): dict(value) for key, value in rows[: max(1, int(limit or 1))]}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except Exception:
        return int(default)
