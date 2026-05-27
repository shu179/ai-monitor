from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import delete, select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a real HTTP Cloud Sync v2 smoke test.")
    parser.add_argument("--base-url", default=os.environ.get("SURFACED_CLOUD_SMOKE_BASE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--keep", action="store_true", help="Keep the smoke workspace for manual inspection.")
    args = parser.parse_args(argv)
    marker = os.environ.get("SURFACED_CLOUD_ALLOW_SMOKE", "").strip().lower()
    if marker not in {"1", "true", "yes"}:
        print("Refusing to run: set SURFACED_CLOUD_ALLOW_SMOKE=1 for a disposable sync smoke.")
        return 2

    from app.db.session import SessionLocal

    suffix = f"{os.getpid()}-{uuid4().hex[:10]}"
    email = f"sync-smoke-{suffix}@example.com"
    password = f"Smoke-{uuid4().hex[:16]}"
    workspace_id = 0
    event_key = f"smoke-run-{suffix}"
    base_url = str(args.base_url).rstrip("/")

    try:
        with SessionLocal() as db:
            workspace_id, task_id = _seed_workspace(db, email=email, password=password, suffix=suffix)

        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            token = _login(client, email=email, password=password)
            headers = {"Authorization": f"Bearer {token}"}
            sync_response = client.post(
                "/api/v1/sync/events",
                json={
                    "events": [
                        {
                            "event_type": "run_record",
                            "idempotency_key": event_key,
                            "payload": {
                                "task_id": task_id,
                                "platform": "smoke",
                                "keyword": "cloud-sync-v2-http",
                                "brand": "SmokeBrand",
                                "mode": "smoke",
                                "executed_at": datetime.now(timezone.utc).isoformat(),
                                "result": {"success": True, "rank": 1},
                            },
                        }
                    ]
                },
                headers=headers,
            )
            sync_response.raise_for_status()
            sync_payload = sync_response.json()
            if sync_payload.get("accepted") != 1:
                raise AssertionError(f"unexpected sync accept payload: {sync_payload}")

            delta = _wait_for_delta(
                client,
                headers=headers,
                event_key=event_key,
                timeout_seconds=float(args.timeout_seconds),
            )

        print(
            "Cloud Sync v2 HTTP smoke OK: "
            f"workspace={workspace_id} task={task_id} event_key={event_key} "
            f"delta_changes={len(delta.get('changes', []))}"
        )
        return 0
    except Exception:
        if workspace_id:
            _print_queue_debug(workspace_id=workspace_id, event_key=event_key)
        raise
    finally:
        if workspace_id and not args.keep:
            _cleanup_workspace(workspace_id=workspace_id)


def _seed_workspace(db, *, email: str, password: str, suffix: str) -> tuple[int, int]:
    from app.core.security import hash_password
    from app.models import BrandTask, TaskAccessLevel, TaskMember, User, UserRole, Workspace

    workspace = Workspace(name=f"Sync Smoke Workspace {suffix}")
    db.add(workspace)
    db.flush()
    user = User(
        workspace_id=workspace.id,
        username=email,
        email=email,
        email_verified=True,
        password_hash=hash_password(password),
        role=UserRole.admin,
        display_name="Sync Smoke Admin",
        enabled=True,
    )
    db.add(user)
    db.flush()
    task = BrandTask(
        workspace_id=workspace.id,
        task_key=f"sync-smoke-task-{suffix}",
        name="Sync Smoke Task",
        brand="SmokeBrand",
        config_json={},
        enabled=True,
    )
    db.add(task)
    db.flush()
    db.add(
        TaskMember(
            workspace_id=workspace.id,
            task_id=task.id,
            user_id=user.id,
            access_level=TaskAccessLevel.operate,
        )
    )
    db.commit()
    db.refresh(workspace)
    db.refresh(task)
    return int(workspace.id), int(task.id)


def _login(client: httpx.Client, *, email: str, password: str) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={
            "username": email,
            "password": password,
            "device_id": "sync-v2-http-smoke",
            "app_version": "smoke",
        },
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


def _wait_for_delta(
    client: httpx.Client,
    *,
    headers: dict[str, str],
    event_key: str,
    timeout_seconds: float,
) -> dict:
    deadline = time.monotonic() + max(1.0, float(timeout_seconds or 30.0))
    last_delta: dict = {}
    while time.monotonic() < deadline:
        response = client.post(
            "/api/v2/sync/state-delta",
            json={"cursors": {"runs": 0}, "limit": 20},
            headers=headers,
        )
        response.raise_for_status()
        last_delta = response.json()
        if any(item.get("ref_id") == event_key for item in last_delta.get("changes", [])):
            return last_delta
        time.sleep(0.5)
    raise AssertionError(f"state-delta did not include {event_key}: {last_delta}")


def _print_queue_debug(*, workspace_id: int, event_key: str) -> None:
    from app.db.session import SessionLocal
    from app.models import RunRecord, SyncBatchItem, SyncEvent, WorkspaceChangeLog

    with SessionLocal() as db:
        item = db.scalar(
            select(SyncBatchItem)
            .where(SyncBatchItem.workspace_id == int(workspace_id), SyncBatchItem.idempotency_key == event_key)
            .order_by(SyncBatchItem.created_at.desc(), SyncBatchItem.id.desc())
        )
        sync_event = db.scalar(
            select(SyncEvent.id).where(SyncEvent.workspace_id == int(workspace_id), SyncEvent.idempotency_key == event_key)
        )
        run_record = db.scalar(
            select(RunRecord.id).where(RunRecord.workspace_id == int(workspace_id), RunRecord.idempotency_key == event_key)
        )
        change = db.scalar(
            select(WorkspaceChangeLog.seq).where(
                WorkspaceChangeLog.workspace_id == int(workspace_id),
                WorkspaceChangeLog.stream == "runs",
                WorkspaceChangeLog.ref_id == event_key,
            )
        )
        if item is None:
            print("Sync smoke debug: batch item missing")
            return
        print(
            "Sync smoke debug: "
            f"item_status={item.status} attempts={item.attempts} "
            f"worker_id={item.worker_id} last_error={item.last_error!r} "
            f"sync_event_id={sync_event} run_record_id={run_record} change_seq={change}"
        )


def _cleanup_workspace(*, workspace_id: int) -> None:
    from app.db.session import SessionLocal
    from app.models import Workspace

    with SessionLocal() as db:
        db.execute(delete(Workspace).where(Workspace.id == int(workspace_id)))
        db.commit()


if __name__ == "__main__":
    raise SystemExit(main())
