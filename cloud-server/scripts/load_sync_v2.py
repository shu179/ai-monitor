from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import delete

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


PROFILE_DEFAULTS = {
    "dev": {"tenants": 1, "events_per_tenant": 100, "batch_size": 50, "concurrency": 2, "objects": 0, "object_bytes": 0},
    "l1": {"tenants": 10, "events_per_tenant": 6000, "batch_size": 500, "concurrency": 10, "objects": 0, "object_bytes": 0},
    "l2": {"tenants": 1, "events_per_tenant": 50000, "batch_size": 500, "concurrency": 8, "objects": 0, "object_bytes": 0},
    "l3": {"tenants": 10, "events_per_tenant": 6000, "batch_size": 500, "concurrency": 10, "objects": 5, "object_bytes": 1024 * 1024 * 1024},
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Cloud Sync v2 HTTP load profiles.")
    parser.add_argument("--base-url", default=os.environ.get("SURFACED_CLOUD_SMOKE_BASE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--profile", choices=sorted(PROFILE_DEFAULTS), default="dev")
    parser.add_argument("--tenants", type=int)
    parser.add_argument("--events-per-tenant", type=int)
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--objects", type=int)
    parser.add_argument("--object-bytes", type=int)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--keep", action="store_true", help="Keep load-test workspaces for inspection.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)
    marker = os.environ.get("SURFACED_CLOUD_ALLOW_LOAD", "").strip().lower()
    if marker not in {"1", "true", "yes"}:
        print("Refusing to run: set SURFACED_CLOUD_ALLOW_LOAD=1 for load tests.")
        return 2

    cfg = _profile_config(args)
    started_at = time.monotonic()
    workspace_ids: list[int] = []
    result: dict = {}
    try:
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            tenants = [_seed_workspace(db, suffix=f"load-{os.getpid()}-{idx}-{uuid4().hex[:8]}") for idx in range(cfg["tenants"])]
            workspace_ids = [item["workspace_id"] for item in tenants]

        base_url = str(args.base_url).rstrip("/")
        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            for tenant in tenants:
                tenant["token"] = _login(client, email=tenant["email"], password=tenant["password"])

        batch_metrics = _send_sync_batches(base_url=base_url, tenants=tenants, cfg=cfg)
        object_metrics = _send_objects(base_url=base_url, tenants=tenants, cfg=cfg) if cfg["objects"] else []
        delta_metrics = _wait_for_materialization(base_url=base_url, tenants=tenants, cfg=cfg, timeout_seconds=args.timeout_seconds)
        queue_report = _load_queue_report()

        result = {
            "profile": args.profile,
            "config": cfg,
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
            "batches": _summarize_metrics(batch_metrics),
            "objects": _summarize_metrics(object_metrics),
            "delta": _summarize_metrics(delta_metrics),
            "queue": _summarize_queue(queue_report),
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(_format_result(result))
        return 0
    finally:
        if workspace_ids and not args.keep:
            _cleanup_workspaces(workspace_ids)


def _profile_config(args: argparse.Namespace) -> dict[str, int]:
    cfg = dict(PROFILE_DEFAULTS[args.profile])
    for key, arg_name in [
        ("tenants", "tenants"),
        ("events_per_tenant", "events_per_tenant"),
        ("batch_size", "batch_size"),
        ("concurrency", "concurrency"),
        ("objects", "objects"),
        ("object_bytes", "object_bytes"),
    ]:
        value = getattr(args, arg_name)
        if value is not None:
            cfg[key] = int(value)
    cfg["tenants"] = max(1, int(cfg["tenants"]))
    cfg["events_per_tenant"] = max(1, int(cfg["events_per_tenant"]))
    cfg["batch_size"] = max(1, min(int(cfg["batch_size"]), 500))
    cfg["concurrency"] = max(1, int(cfg["concurrency"]))
    cfg["objects"] = max(0, int(cfg["objects"]))
    cfg["object_bytes"] = max(0, int(cfg["object_bytes"]))
    return cfg


def _seed_workspace(db, *, suffix: str) -> dict:
    from app.core.security import hash_password
    from app.models import BrandTask, TaskAccessLevel, TaskMember, User, UserRole, Workspace

    email = f"{suffix}@example.com"
    password = f"Load-{uuid4().hex[:16]}"
    workspace = Workspace(name=f"Load Workspace {suffix}")
    db.add(workspace)
    db.flush()
    user = User(
        workspace_id=workspace.id,
        username=email,
        email=email,
        email_verified=True,
        password_hash=hash_password(password),
        role=UserRole.admin,
        display_name="Load Admin",
        enabled=True,
    )
    db.add(user)
    db.flush()
    task = BrandTask(
        workspace_id=workspace.id,
        task_key=f"load-task-{suffix}"[:128],
        name="Load Task",
        brand="LoadBrand",
        config_json={},
        enabled=True,
    )
    db.add(task)
    db.flush()
    db.add(TaskMember(workspace_id=workspace.id, task_id=task.id, user_id=user.id, access_level=TaskAccessLevel.operate))
    db.commit()
    return {"workspace_id": int(workspace.id), "task_id": int(task.id), "email": email, "password": password}


def _login(client: httpx.Client, *, email: str, password: str) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": email, "password": password, "device_id": "sync-v2-load", "app_version": "load"},
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


def _send_sync_batches(*, base_url: str, tenants: list[dict], cfg: dict[str, int]) -> list[dict]:
    jobs = []
    for tenant in tenants:
        total = int(cfg["events_per_tenant"])
        batch_size = int(cfg["batch_size"])
        for offset in range(0, total, batch_size):
            jobs.append((tenant, offset, min(batch_size, total - offset)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=int(cfg["concurrency"])) as executor:
        return list(executor.map(lambda job: _send_one_batch(base_url, *job), jobs))


def _send_one_batch(base_url: str, tenant: dict, offset: int, count: int) -> dict:
    events = []
    now = datetime.now(timezone.utc).isoformat()
    for idx in range(offset, offset + count):
        key = f"load-run-{tenant['workspace_id']}-{idx}"
        events.append(
            {
                "event_type": "run_record",
                "idempotency_key": key,
                "seq": idx,
                "partition_key": f"{tenant['workspace_id']}:run:{idx}",
                "payload": {
                    "task_id": int(tenant["task_id"]),
                    "platform": "load",
                    "keyword": f"kw-{idx % 100}",
                    "brand": "LoadBrand",
                    "mode": "load",
                    "executed_at": now,
                    "result": {"success": True, "rank": idx % 10},
                },
            }
        )
    started_at = time.monotonic()
    status_code = 0
    retry_after = ""
    queue_depth = ""
    try:
        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            response = client.post(
                "/api/v2/sync/batches",
                json={"idempotency_key": f"load-batch-{tenant['workspace_id']}-{offset}", "events": events},
                headers={"Authorization": f"Bearer {tenant['token']}"},
            )
            status_code = int(response.status_code)
            retry_after = response.headers.get("Retry-After", "")
            queue_depth = response.headers.get("X-Queue-Depth-Hint", "")
            response.raise_for_status()
            payload = response.json()
            accepted = int(payload.get("accepted") or 0)
    except Exception as exc:
        return _metric(started_at, status_code=status_code, error=str(exc), retry_after=retry_after, queue_depth=queue_depth)
    return _metric(started_at, status_code=status_code, accepted=accepted, retry_after=retry_after, queue_depth=queue_depth)


def _wait_for_materialization(*, base_url: str, tenants: list[dict], cfg: dict[str, int], timeout_seconds: float) -> list[dict]:
    metrics: list[dict] = []
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    for tenant in tenants:
        started_at = time.monotonic()
        expected = f"load-run-{tenant['workspace_id']}-{int(cfg['events_per_tenant']) - 1}"
        seen = False
        last_payload: dict = {}
        while time.monotonic() < deadline:
            with httpx.Client(base_url=base_url, timeout=30.0) as client:
                response = client.post(
                    "/api/v2/sync/state-delta",
                    json={"cursors": {"runs": 0}, "limit": 1000},
                    headers={"Authorization": f"Bearer {tenant['token']}"},
                )
                status_code = int(response.status_code)
                response.raise_for_status()
                last_payload = response.json()
            if any(item.get("ref_id") == expected for item in last_payload.get("changes", [])):
                seen = True
                break
            time.sleep(0.5)
        metrics.append(_metric(started_at, status_code=200 if seen else 408, error="" if seen else f"missing {expected}"))
    return metrics


def _send_objects(*, base_url: str, tenants: list[dict], cfg: dict[str, int]) -> list[dict]:
    if not tenants or int(cfg["object_bytes"]) <= 0:
        return []
    jobs = [(tenants[idx % len(tenants)], int(cfg["object_bytes"]), idx) for idx in range(int(cfg["objects"]))]
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(jobs), int(cfg["concurrency"]))) as executor:
        return list(executor.map(lambda job: _send_one_object(base_url, *job), jobs))


def _send_one_object(base_url: str, tenant: dict, object_bytes: int, index: int) -> dict:
    import hashlib

    size_bytes = max(1, int(object_bytes))
    sha256 = _deterministic_payload_sha256(size_bytes=size_bytes, index=index)
    started_at = time.monotonic()
    status_code = 0
    try:
        with httpx.Client(base_url=base_url, timeout=120.0) as client:
            headers = {"Authorization": f"Bearer {tenant['token']}"}
            create = client.post(
                "/api/v2/objects/uploads",
                json={
                    "sha256": sha256,
                    "size_bytes": size_bytes,
                    "storage_size_bytes": size_bytes,
                    "content_type": "application/octet-stream",
                    "compression": "none",
                },
                headers=headers,
            )
            status_code = int(create.status_code)
            create.raise_for_status()
            upload = create.json()
            if upload["strategy"] == "inline":
                return _metric(started_at, status_code=status_code, accepted=1)
            upload_url = str(upload["upload"]["url"])
            if upload_url.startswith("/"):
                upload_url = f"{base_url}{upload_url}"
            put = client.put(upload_url, content=_iter_deterministic_payload(size_bytes=size_bytes, index=index), headers=headers)
            status_code = int(put.status_code)
            put.raise_for_status()
    except Exception as exc:
        return _metric(started_at, status_code=status_code, error=str(exc))
    return _metric(started_at, status_code=status_code, accepted=1)


def _deterministic_payload_sha256(*, size_bytes: int, index: int) -> str:
    import hashlib

    digest = hashlib.sha256()
    for chunk in _iter_deterministic_payload(size_bytes=size_bytes, index=index):
        digest.update(chunk)
    return digest.hexdigest()


def _iter_deterministic_payload(*, size_bytes: int, index: int):
    chunk = (f"Surfaced load object payload {int(index)}\n".encode("utf-8") * 4096)
    remaining = max(0, int(size_bytes))
    while remaining > 0:
        current = chunk[: min(len(chunk), remaining)]
        remaining -= len(current)
        yield current


def _load_queue_report() -> dict:
    from app.db.session import SessionLocal
    from app.services.sync_queue_diagnostics import build_sync_queue_report

    with SessionLocal() as db:
        return build_sync_queue_report(db)


def _cleanup_workspaces(workspace_ids: list[int]) -> None:
    from app.db.session import SessionLocal
    from app.models import Workspace

    with SessionLocal() as db:
        db.execute(delete(Workspace).where(Workspace.id.in_([int(item) for item in workspace_ids])))
        db.commit()


def _metric(started_at: float, *, status_code: int, accepted: int = 0, error: str = "", retry_after: str = "", queue_depth: str = "") -> dict:
    return {
        "elapsed_ms": int(round((time.monotonic() - started_at) * 1000)),
        "status_code": int(status_code or 0),
        "accepted": int(accepted or 0),
        "error": str(error or ""),
        "retry_after": str(retry_after or ""),
        "queue_depth": str(queue_depth or ""),
    }


def _summarize_metrics(metrics: list[dict]) -> dict:
    if not metrics:
        return {"count": 0, "accepted": 0, "errors": 0, "status_codes": {}, "p50_ms": 0, "p95_ms": 0, "p99_ms": 0}
    values = sorted(int(item.get("elapsed_ms") or 0) for item in metrics)
    status_codes: dict[str, int] = {}
    for item in metrics:
        status_codes[str(item.get("status_code") or 0)] = status_codes.get(str(item.get("status_code") or 0), 0) + 1
    return {
        "count": len(metrics),
        "accepted": sum(int(item.get("accepted") or 0) for item in metrics),
        "errors": sum(1 for item in metrics if item.get("error")),
        "status_codes": status_codes,
        "p50_ms": _percentile(values, 50),
        "p95_ms": _percentile(values, 95),
        "p99_ms": _percentile(values, 99),
        "max_ms": max(values),
    }


def _percentile(values: list[int], percentile: int) -> int:
    if not values:
        return 0
    if len(values) == 1:
        return values[0]
    return int(round(statistics.quantiles(values, n=100, method="inclusive")[percentile - 1]))


def _summarize_queue(report: dict) -> dict:
    return {
        "status": report.get("status"),
        "worker_state": report.get("worker_state"),
        "counts": report.get("counts", {}),
        "expired_in_progress": report.get("expired_in_progress", 0),
        "recent_done_latency_ms": report.get("recent_done_latency_ms", {}),
    }


def _format_result(result: dict) -> str:
    lines = [
        f"Cloud Sync v2 load profile={result['profile']} elapsed_seconds={result['elapsed_seconds']}",
        f"config={result['config']}",
        f"batches={result['batches']}",
        f"objects={result['objects']}",
        f"delta={result['delta']}",
        f"queue={result['queue']}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
