from __future__ import annotations

import os

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.sync_v2_service import LIMITS
from app.services.sync_v2_worker import run_sync_v2_worker


def main() -> None:
    settings = get_settings()
    worker_id = os.environ.get("SURFACED_CLOUD_WORKER_ID", f"worker-{os.getpid()}")
    total_shards = int(os.environ.get("SURFACED_CLOUD_WORKER_TOTAL_SHARDS", str(LIMITS["virtual_shards"])))
    shard_offset = int(os.environ.get("SURFACED_CLOUD_WORKER_SHARD_OFFSET", "0"))
    shard_step = int(os.environ.get("SURFACED_CLOUD_WORKER_SHARD_STEP", "1"))
    shard_ids = list(range(shard_offset, max(0, total_shards), max(1, shard_step)))
    run_sync_v2_worker(
        session_factory=SessionLocal,
        worker_id=worker_id,
        shard_ids=shard_ids,
        poll_seconds=float(os.environ.get("SURFACED_CLOUD_WORKER_POLL_SECONDS", "1")),
        batch_limit=int(os.environ.get("SURFACED_CLOUD_WORKER_BATCH_LIMIT", "100")),
        statement_timeout_ms=int(settings.worker_db_statement_timeout_ms),
    )


if __name__ == "__main__":
    main()
