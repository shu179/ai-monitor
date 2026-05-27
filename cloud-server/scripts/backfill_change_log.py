from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill v2 workspace change log rows for existing cloud data.")
    parser.add_argument("--workspace-id", type=int, default=0, help="Limit to one workspace; default is all workspaces.")
    parser.add_argument("--limit", type=int, default=500, help="Max rows per entity type and workspace per run.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)
    marker = os.environ.get("SURFACED_CLOUD_ALLOW_CHANGE_LOG_BACKFILL", "").strip().lower()
    if marker not in {"1", "true", "yes"}:
        print("Refusing to run: set SURFACED_CLOUD_ALLOW_CHANGE_LOG_BACKFILL=1.")
        return 2

    from app.db.session import SessionLocal
    from app.models import Workspace
    from app.services.change_log_backfill_service import backfill_workspace_change_logs

    totals = {"tasks": 0, "runs": 0, "articles": 0, "references": 0}
    workspace_reports: list[dict[str, int]] = []
    with SessionLocal() as db:
        workspace_ids = _workspace_ids(db, workspace_id=int(args.workspace_id or 0))
        for workspace_id in workspace_ids:
            stats = backfill_workspace_change_logs(db, workspace_id=workspace_id, limit=int(args.limit or 500))
            db.commit()
            workspace_reports.append({"workspace_id": workspace_id, **stats})
            for key in totals:
                totals[key] += int(stats.get(key) or 0)
    output = {"workspaces": workspace_reports, "totals": totals}
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"Change log backfill totals={totals} workspaces={workspace_reports}")
    return 0


def _workspace_ids(db, *, workspace_id: int) -> list[int]:
    from app.models import Workspace

    if workspace_id:
        return [int(workspace_id)]
    rows = db.execute(select(Workspace.id).order_by(Workspace.id.asc())).scalars()
    return [int(row) for row in rows]


if __name__ == "__main__":
    raise SystemExit(main())
