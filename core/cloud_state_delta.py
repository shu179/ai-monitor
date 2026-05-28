from __future__ import annotations

import json
import os
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .app_paths import resolve_app_path
from .cloud_client import CloudClientError, SurfacedCloudClient, new_trace_id
from .cloud_session_store import (
    CloudSessionChangedError,
    CloudSessionStore,
    cloud_session_identity_key,
)
from .cloud_state_delta_inbox import CloudStateDeltaInbox
from .cloud_task_sync import _cloud_request_with_refresh
from .file_lock import CrossProcessRLock
from .local_account_space import account_scoped_path
from .time_utils import local_now


DEFAULT_CLOUD_STATE_DELTA_PATH = resolve_app_path("user_data/cloud_state_delta.json")


class CloudStateDeltaStore:
    """Persist v2 state-delta cursors for the current cloud account.

    This is a cursor/diagnostics store only. Business appliers for articles,
    answers, assets, and Agent results can attach later without changing the
    account-scoped state file format.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._explicit_path = Path(path) if path is not None else None
        self._lock = CrossProcessRLock(lambda: self._lock_path())

    @property
    def path(self) -> Path:
        return self._state_path()

    def load(self, session: dict[str, Any] | None = None) -> dict[str, Any]:
        identity_key = cloud_session_identity_key(session) if session is not None else ""
        with self._lock:
            state = self._load_unlocked()
            if identity_key and str(state.get("identity_key") or "") not in {"", identity_key}:
                return self._default_state(identity_key)
            if identity_key and not str(state.get("identity_key") or ""):
                state["identity_key"] = identity_key
            return state

    def save(self, state: dict[str, Any]) -> dict[str, Any]:
        payload = self._normalize_state(state)
        payload["updated_at"] = local_now().isoformat(timespec="seconds")
        with self._lock:
            self._save_unlocked(payload)
        return payload

    def reset_for_identity(self, identity_key: str) -> dict[str, Any]:
        state = self._default_state(str(identity_key or "").strip())
        state["updated_at"] = local_now().isoformat(timespec="seconds")
        with self._lock:
            self._save_unlocked(state)
        return state

    def diagnostics(self, session: dict[str, Any] | None = None) -> dict[str, Any]:
        state = self.load(session)
        return {
            "path": str(self.path),
            "identity_key": str(state.get("identity_key") or ""),
            "cursors": dict(state.get("cursors") if isinstance(state.get("cursors"), dict) else {}),
            "reset_token_present": bool(str(state.get("reset_token") or "")),
            "bootstrap_cursor_present": bool(str(state.get("bootstrap_cursor") or "")),
            "last_pulled_at": str(state.get("last_pulled_at") or ""),
            "last_error": str(state.get("last_error") or ""),
            "last_summary": state.get("last_summary") if isinstance(state.get("last_summary"), dict) else {},
            "updated_at": str(state.get("updated_at") or ""),
        }

    def _state_path(self) -> Path:
        if self._explicit_path is not None:
            return self._explicit_path
        return account_scoped_path("user_data/cloud_state_delta.json", fallback=DEFAULT_CLOUD_STATE_DELTA_PATH)

    def _lock_path(self) -> Path:
        path = self._state_path()
        return path.with_name(f"{path.name}.lock")

    def _load_unlocked(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                state = self._default_state(str(data.get("identity_key") or ""))
                state.update(data)
                return self._normalize_state(state)
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f"[CloudStateDelta] 读取状态失败: {exc}")
        return self._default_state("")

    def _save_unlocked(self, payload: dict[str, Any]) -> None:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".cloud_state_delta_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    @staticmethod
    def _default_state(identity_key: str) -> dict[str, Any]:
        return {
            "identity_key": str(identity_key or "").strip(),
            "cursors": {},
            "reset_token": "",
            "bootstrap_cursor": "",
            "last_pulled_at": "",
            "last_error": "",
            "last_summary": {},
            "updated_at": "",
        }

    @staticmethod
    def _normalize_state(state: dict[str, Any]) -> dict[str, Any]:
        payload = dict(state or {})
        cursors: dict[str, int] = {}
        raw_cursors = payload.get("cursors") if isinstance(payload.get("cursors"), dict) else {}
        for key, value in raw_cursors.items():
            stream = str(key or "").strip()
            if not stream:
                continue
            try:
                cursors[stream] = max(0, int(value or 0))
            except Exception:
                cursors[stream] = 0
        payload["identity_key"] = str(payload.get("identity_key") or "").strip()
        payload["cursors"] = cursors
        payload["reset_token"] = str(payload.get("reset_token") or "").strip()
        payload["bootstrap_cursor"] = str(payload.get("bootstrap_cursor") or "").strip()
        payload["last_pulled_at"] = str(payload.get("last_pulled_at") or "").strip()
        payload["last_error"] = str(payload.get("last_error") or "").strip()
        if not isinstance(payload.get("last_summary"), dict):
            payload["last_summary"] = {}
        payload["updated_at"] = str(payload.get("updated_at") or "").strip()
        return payload


def pull_cloud_state_delta(
    *,
    client: SurfacedCloudClient | None = None,
    session_store: CloudSessionStore | None = None,
    state_store: CloudStateDeltaStore | None = None,
    inbox: CloudStateDeltaInbox | None = None,
    limit: int = 500,
    max_pages: int = 5,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    store = session_store or CloudSessionStore()
    session = store.load()
    identity_key = cloud_session_identity_key(session)
    base_url = str(session.get("base_url") or "").strip()
    access_token = str(session.get("access_token") or "").strip()
    refresh_token = str(session.get("refresh_token") or "").strip()
    delta_store = state_store or CloudStateDeltaStore()
    if not base_url or not access_token:
        summary = _summary(
            ok=False,
            mode="idle",
            message="未登录云端",
            started_at=started_at,
        )
        _save_error(delta_store, identity_key, summary)
        return summary

    state = delta_store.load(session)
    if str(state.get("identity_key") or "") != identity_key:
        state = delta_store.reset_for_identity(identity_key)

    target_client = client or SurfacedCloudClient(base_url)
    safe_limit = max(1, min(int(limit or 500), 1000))
    pages_limit = max(1, int(max_pages or 1))
    pages = 0
    total_changes = 0
    total_object_refs = 0
    streams: Counter[str] = Counter()
    mode = "reset" if str(state.get("reset_token") or "") else "delta"
    has_more = False
    reset_required = False
    retry_after_seconds = 0.0
    queue_depth_hint = 0
    throttle_bucket = ""
    state_updated = False
    inbox_store = inbox or CloudStateDeltaInbox()
    inbox_created = 0
    inbox_duplicates = 0

    try:
        while pages < pages_limit:
            request_state = dict(state)
            response, session = _cloud_request_with_refresh(
                target_client,
                store,
                session,
                base_url=base_url,
                access_token=str(session.get("access_token") or access_token).strip(),
                refresh_token=str(session.get("refresh_token") or refresh_token).strip(),
                operation=lambda token, request_state=request_state: target_client.state_delta(
                    token,
                    cursors=request_state.get("cursors") if isinstance(request_state.get("cursors"), dict) else {},
                    limit=safe_limit,
                    reset_token=str(request_state.get("reset_token") or "") or None,
                    bootstrap_cursor=str(request_state.get("bootstrap_cursor") or "") or None,
                    trace_id=new_trace_id("state-delta"),
                ),
            )
            if cloud_session_identity_key(session) != identity_key:
                raise CloudSessionChangedError("云端账号已切换，本次 state-delta 已中止")
            pages += 1
            changes = response.get("changes") if isinstance(response.get("changes"), list) else []
            object_refs = response.get("object_refs") if isinstance(response.get("object_refs"), list) else []
            total_changes += len(changes)
            total_object_refs += len(object_refs)
            for change in changes:
                if isinstance(change, dict):
                    stream = str(change.get("stream") or "").strip() or "unknown"
                    streams[stream] += 1

            retry_after_seconds = max(retry_after_seconds, _safe_float(response.get("retry_after_seconds"), 0.0))
            queue_depth_hint = max(queue_depth_hint, _safe_int(response.get("queue_depth_hint"), 0))
            response_throttle_bucket = str(response.get("throttle_bucket") or "").strip()
            if response_throttle_bucket:
                throttle_bucket = response_throttle_bucket
            if bool(response.get("reset_required")):
                reset_required = True
                state["reset_token"] = str(response.get("reset_token") or "")
                state["bootstrap_cursor"] = str(response.get("bootstrap_cursor") or "")
                has_more = True
                mode = "reset"
                state_updated = True
                if not state["reset_token"] or pages >= pages_limit:
                    break
                continue

            inbox_result = inbox_store.record_changes(
                identity_key=identity_key,
                changes=changes,
                object_refs=object_refs,
            )
            inbox_created += int(inbox_result.get("created") or 0)
            inbox_duplicates += int(inbox_result.get("duplicates") or 0)

            next_cursors = response.get("next_cursors") if isinstance(response.get("next_cursors"), dict) else {}
            if next_cursors:
                state["cursors"] = _merge_cursors(state.get("cursors"), next_cursors)
                state_updated = True
            state["reset_token"] = str(response.get("reset_token") or "") if response.get("reset_token") else ""
            state["bootstrap_cursor"] = (
                str(response.get("bootstrap_cursor") or "") if response.get("bootstrap_cursor") else ""
            )
            state_updated = True
            has_more = bool(response.get("has_more"))
            if not has_more:
                break
            if not state["reset_token"] and mode == "reset":
                break

        summary = _summary(
            ok=True,
            mode=mode,
            message="",
            started_at=started_at,
            pages=pages,
            changes=total_changes,
            streams=dict(streams),
            object_refs=total_object_refs,
            reset_required=reset_required,
            has_more=has_more,
            next_retry_after_seconds=retry_after_seconds,
            queue_depth_hint=queue_depth_hint,
            throttle_bucket=throttle_bucket,
            state_updated=state_updated,
            inbox_created=inbox_created,
            inbox_duplicates=inbox_duplicates,
        )
        state["identity_key"] = identity_key
        state["last_pulled_at"] = local_now().isoformat(timespec="seconds")
        state["last_error"] = ""
        state["last_summary"] = summary
        delta_store.save(state)
        return summary
    except (CloudClientError, CloudSessionChangedError, Exception) as exc:
        if isinstance(exc, CloudClientError):
            retry_after_seconds = max(retry_after_seconds, _safe_float(exc.retry_after_seconds, 0.0))
            queue_depth_hint = max(queue_depth_hint, _safe_int(exc.queue_depth_hint, 0))
            if str(exc.throttle_bucket or "").strip():
                throttle_bucket = str(exc.throttle_bucket or "").strip()
        summary = _summary(
            ok=False,
            mode=mode,
            message=str(exc),
            started_at=started_at,
            pages=pages,
            changes=total_changes,
            streams=dict(streams),
            object_refs=total_object_refs,
            reset_required=reset_required,
            has_more=has_more,
            next_retry_after_seconds=retry_after_seconds,
            queue_depth_hint=queue_depth_hint,
            throttle_bucket=throttle_bucket,
            state_updated=state_updated,
            inbox_created=inbox_created,
            inbox_duplicates=inbox_duplicates,
        )
        _save_error(delta_store, identity_key, summary)
        return summary


def _merge_cursors(current: Any, incoming: dict[str, Any]) -> dict[str, int]:
    merged: dict[str, int] = {}
    if isinstance(current, dict):
        for key, value in current.items():
            try:
                merged[str(key)] = max(0, int(value or 0))
            except Exception:
                merged[str(key)] = 0
    for key, value in incoming.items():
        stream = str(key or "").strip()
        if not stream:
            continue
        try:
            merged[stream] = max(int(merged.get(stream) or 0), int(value or 0))
        except Exception:
            merged[stream] = int(merged.get(stream) or 0)
    return merged


def _save_error(store: CloudStateDeltaStore, identity_key: str, summary: dict[str, Any]) -> None:
    try:
        state = store.load()
        state["identity_key"] = str(identity_key or state.get("identity_key") or "").strip()
        state["last_error"] = str(summary.get("message") or "")
        state["last_summary"] = dict(summary)
        store.save(state)
    except Exception:
        return


def _summary(
    *,
    ok: bool,
    mode: str,
    message: str,
    started_at: float,
    pages: int = 0,
    changes: int = 0,
    streams: dict[str, int] | None = None,
    object_refs: int = 0,
    reset_required: bool = False,
    has_more: bool = False,
    next_retry_after_seconds: int = 0,
    queue_depth_hint: int = 0,
    throttle_bucket: str = "",
    state_updated: bool = False,
    inbox_created: int = 0,
    inbox_duplicates: int = 0,
) -> dict[str, Any]:
    return {
        "ok": bool(ok),
        "mode": str(mode or "delta"),
        "message": str(message or ""),
        "pages": max(0, int(pages or 0)),
        "changes": max(0, int(changes or 0)),
        "streams": dict(streams or {}),
        "object_refs": max(0, int(object_refs or 0)),
        "reset_required": bool(reset_required),
        "has_more": bool(has_more),
        "next_retry_after_seconds": max(0.0, float(next_retry_after_seconds or 0.0)),
        "retry_after_seconds": max(0.0, float(next_retry_after_seconds or 0.0)),
        "queue_depth_hint": max(0, int(queue_depth_hint or 0)),
        "throttle_bucket": str(throttle_bucket or "").strip(),
        "duration_ms": int(round((time.perf_counter() - started_at) * 1000)),
        "state_updated": bool(state_updated),
        "inbox_created": max(0, int(inbox_created or 0)),
        "inbox_duplicates": max(0, int(inbox_duplicates or 0)),
    }


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)
