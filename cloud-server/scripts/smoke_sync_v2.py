from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.core.security import hash_password
from app.db.session import SessionLocal
from app.models import (
    BrandTask,
    RunRecord,
    SyncBatchItem,
    SyncEvent,
    TaskAccessLevel,
    TaskMember,
    User,
    UserRole,
    Workspace,
    WorkspaceChangeLog,
)
from app.schemas import SyncEventIn
from app.services.change_log_service import compact_change_snapshot
from app.services.sync_service import accept_sync_events
from app.services.sync_v2_service import _virtual_shard, build_state_delta
from app.services.sync_v2_worker import process_sync_batch_items_once
from app.sync_event_types import EVENT_RUN_RECORD


def main() -> int:
    marker = os.environ.get("SURFACED_CLOUD_ALLOW_SMOKE", "").strip().lower()
    if marker not in {"1", "true", "yes"}:
        print("Refusing to run: set SURFACED_CLOUD_ALLOW_SMOKE=1 for a disposable database.")
        return 2

    with SessionLocal() as db:
        suffix = f"{os.getpid()}-{uuid4().hex[:10]}"
        workspace, user, task = _seed_workspace(db, suffix=suffix)
        event = SyncEventIn(
            event_type=EVENT_RUN_RECORD,
            idempotency_key=f"smoke-run-{suffix}",
            payload={
                "task_id": int(task.id),
                "platform": "smoke",
                "keyword": "cloud-sync-v2",
                "brand": "SmokeBrand",
                "mode": "smoke",
                "result": {"success": True, "rank": 1},
            },
        )

        accepted, duplicates = accept_sync_events(db, user, [event])
        if accepted != 1 or duplicates != 0:
            raise AssertionError(f"unexpected accept result: accepted={accepted} duplicates={duplicates}")

        item = db.scalar(
            select(SyncBatchItem)
            .where(SyncBatchItem.workspace_id == workspace.id, SyncBatchItem.idempotency_key == event.idempotency_key)
            .order_by(SyncBatchItem.created_at.desc(), SyncBatchItem.id.desc())
        )
        if item is None:
            raise AssertionError("sync batch item was not created")
        shard = int(item.virtual_shard)

        stats = process_sync_batch_items_once(db, worker_id="smoke-worker", shard_ids=[shard], limit=10)
        if stats.get("done") != 1:
            raise AssertionError(f"worker did not complete item: {stats}")

        mirrored = db.scalar(
            select(SyncEvent).where(
                SyncEvent.workspace_id == workspace.id,
                SyncEvent.idempotency_key == event.idempotency_key,
            )
        )
        if mirrored is None:
            raise AssertionError("legacy sync_events mirror was not written")

        record = db.scalar(
            select(RunRecord).where(
                RunRecord.workspace_id == workspace.id,
                RunRecord.idempotency_key == event.idempotency_key,
            )
        )
        if record is None:
            raise AssertionError("run record was not materialized")

        change = db.scalar(
            select(WorkspaceChangeLog).where(
                WorkspaceChangeLog.workspace_id == workspace.id,
                WorkspaceChangeLog.stream == "runs",
                WorkspaceChangeLog.ref_id == event.idempotency_key,
            )
        )
        if change is None:
            raise AssertionError("workspace change log was not written")

        snapshot = compact_change_snapshot(db, int(workspace.id))
        delta = build_state_delta(db, user, cursors={"runs": 0}, limit=10)
        if not any(item.get("ref_id") == event.idempotency_key for item in delta.get("changes", [])):
            raise AssertionError(f"state-delta did not include smoke change: {delta}")

        print(
            "Cloud Sync v2 smoke OK: "
            f"workspace={workspace.id} task={task.id} shard={shard} "
            f"snapshot={snapshot} delta_changes={len(delta.get('changes', []))}"
        )
    return 0


def _seed_workspace(db, *, suffix: str):
    workspace = Workspace(name=f"Smoke Workspace {suffix}")
    db.add(workspace)
    db.flush()
    user = User(
        workspace_id=workspace.id,
        username=f"smoke-{suffix}@example.com",
        email=f"smoke-{suffix}@example.com",
        email_verified=True,
        password_hash=hash_password("smoke-password-123"),
        role=UserRole.admin,
        display_name="Smoke Admin",
        enabled=True,
    )
    db.add(user)
    db.flush()
    task = BrandTask(
        workspace_id=workspace.id,
        task_key=f"smoke-task-{suffix}",
        name="Smoke Task",
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
    db.refresh(user)
    db.refresh(task)
    return workspace, user, task


if __name__ == "__main__":
    raise SystemExit(main())
