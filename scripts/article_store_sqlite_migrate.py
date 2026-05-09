from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import article_store
from core.article_sqlite_store import ArticleSQLiteStore


def _load_articles(path: Path) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _build_store(db_path: Path) -> ArticleSQLiteStore:
    return ArticleSQLiteStore(
        db_path,
        normalize_article_url=article_store.normalize_article_url,
        normalize_article_entry=lambda item: article_store._normalize_article_entry(item)[0],  # noqa: SLF001
        now_text=article_store._article_now_minute_text,  # noqa: SLF001
    )


def migrate_articles(
    *,
    articles_json: Path,
    db_path: Path | None,
    apply: bool,
    replace: bool,
) -> dict[str, Any]:
    articles = _load_articles(articles_json)
    skipped = 0
    try:
        parsed = json.loads(articles_json.read_text(encoding="utf-8"))
        if isinstance(parsed, list):
            skipped = len([item for item in parsed if not isinstance(item, dict)])
    except Exception:
        skipped = 0

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    requested_db = db_path
    if apply:
        target_db = db_path or article_store.get_article_store_db_path()
    else:
        temp_dir = tempfile.TemporaryDirectory(prefix="article-store-sqlite-dry-run-")
        target_db = Path(temp_dir.name) / "article_store.sqlite3"

    store = _build_store(target_db)
    result = store.import_from_articles(articles, replace=replace)
    summary = {
        "applied": bool(apply),
        "articles_json": str(articles_json),
        "db_path": str(target_db),
        "requested_db_path": str(requested_db) if requested_db is not None else "",
        "replace": bool(replace),
        "source_count": len(articles),
        "source_skipped_non_objects": skipped,
        "import_created": int(result.get("created") or 0),
        "import_updated": int(result.get("updated") or 0),
        "import_skipped": int(result.get("skipped") or 0),
        "signature": store.source_signature(),
    }
    if temp_dir is not None:
        summary["dry_run_note"] = "temporary sqlite database was removed before command exit"
        temp_dir.cleanup()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run or apply migration from articles.json to authoritative ArticleSQLiteStore.",
    )
    parser.add_argument(
        "--articles-json",
        type=Path,
        default=article_store.get_articles_file_path(),
        help="Source articles.json path. Defaults to the active ArticleStore JSON path.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Target SQLite DB path. Dry-run uses a temporary DB when omitted.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write to the target DB. Without this flag the migration is a dry-run.",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help="Merge into the target DB instead of replacing existing article rows.",
    )
    args = parser.parse_args()

    summary = migrate_articles(
        articles_json=args.articles_json,
        db_path=args.db,
        apply=bool(args.apply),
        replace=not bool(args.merge),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
