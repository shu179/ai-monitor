from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import delete, select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a real object upload/download smoke test.")
    parser.add_argument("--base-url", default=os.environ.get("SURFACED_CLOUD_SMOKE_BASE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--payload", default="Surfaced object smoke payload\n" * 1600)
    parser.add_argument("--keep", action="store_true", help="Keep the smoke workspace and object for manual inspection.")
    args = parser.parse_args()
    marker = os.environ.get("SURFACED_CLOUD_ALLOW_SMOKE", "").strip().lower()
    if marker not in {"1", "true", "yes"}:
        print("Refusing to run: set SURFACED_CLOUD_ALLOW_SMOKE=1 for a disposable smoke object.")
        return 2
    from app.db.session import SessionLocal

    body = args.payload.encode("utf-8")
    sha256 = hashlib.sha256(body).hexdigest()
    suffix = f"{os.getpid()}-{uuid4().hex[:10]}"
    email = f"object-smoke-{suffix}@example.com"
    password = f"Smoke-{uuid4().hex[:16]}"

    with SessionLocal() as db:
        workspace_id = _seed_user(db, email=email, password=password, suffix=suffix)

    base_url = str(args.base_url).rstrip("/")
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        token = _login(client, email=email, password=password)
        headers = {"Authorization": f"Bearer {token}"}
        create_response = client.post(
            "/api/v2/objects/uploads",
            json={
                "sha256": sha256,
                "size_bytes": len(body),
                "content_type": "text/plain",
                "storage_size_bytes": len(body),
                "compression": "none",
            },
            headers=headers,
        )
        create_response.raise_for_status()
        upload = create_response.json()
        if upload["strategy"] != "inline":
            upload_url = str(upload["upload"]["url"])
            if upload_url.startswith("/"):
                upload_url = f"{base_url}{upload_url}"
            put_response = client.put(upload_url, content=body, headers=headers)
            put_response.raise_for_status()
            complete_payload = put_response.json()
        else:
            raise AssertionError("smoke payload should be larger than inline threshold in server config")
        object_id = complete_payload["object_id"]
        download_response = client.post(f"/api/v2/objects/{object_id}:download", headers=headers)
        download_response.raise_for_status()
        download_url = str(download_response.json()["download_url"])
        if download_url.startswith("/"):
            download_url = f"{base_url}{download_url}"
        content_response = client.get(download_url, headers=headers)
        content_response.raise_for_status()
        downloaded = content_response.content
        if hashlib.sha256(downloaded).hexdigest() != sha256:
            raise AssertionError("downloaded object sha256 mismatch")

    if not args.keep:
        _cleanup_smoke_object(workspace_id=workspace_id, sha256=sha256)
    keep_note = "kept=true" if args.keep else "kept=false"
    print(
        "Object storage smoke OK: "
        f"workspace_email={email} object_id={object_id} bytes={len(body)} sha256={sha256} {keep_note}"
    )
    return 0


def _seed_user(db, *, email: str, password: str, suffix: str) -> int:
    from app.core.security import hash_password
    from app.models import User, UserRole, Workspace

    workspace = Workspace(name=f"Object Smoke Workspace {suffix}")
    db.add(workspace)
    db.flush()
    db.add(
        User(
            workspace_id=workspace.id,
            username=email,
            email=email,
            email_verified=True,
            password_hash=hash_password(password),
            role=UserRole.admin,
            display_name="Object Smoke Admin",
            enabled=True,
        )
    )
    db.commit()
    db.refresh(workspace)
    return int(workspace.id)


def _cleanup_smoke_object(*, workspace_id: int, sha256: str) -> None:
    from app.db.session import SessionLocal
    from app.models import ObjectManifest, Workspace
    from app.services.object_storage_service import local_object_path

    with SessionLocal() as db:
        manifest = db.scalar(
            select(ObjectManifest).where(
                ObjectManifest.workspace_id == int(workspace_id),
                ObjectManifest.sha256 == sha256,
            )
        )
        if manifest is not None:
            path = local_object_path(str(manifest.storage_key))
            path.unlink(missing_ok=True)
        db.execute(delete(Workspace).where(Workspace.id == int(workspace_id)))
        db.commit()


def _login(client: httpx.Client, *, email: str, password: str) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={
            "username": email,
            "password": password,
            "device_id": "object-storage-smoke",
            "app_version": "smoke",
        },
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


if __name__ == "__main__":
    raise SystemExit(main())
