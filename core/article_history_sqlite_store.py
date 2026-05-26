from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from .sqlite_tuning import apply_runtime_pragmas, perform_startup_maintenance, truncate_wal_if_oversized
from .time_utils import local_now, local_today, parse_local_date


SCHEMA_VERSION = 1
ARTICLE_PAGE_MAX_LIMIT = 5000

REQUIRED_TABLE_COLUMNS: dict[str, set[str]] = {
    "store_meta": {"key", "value"},
    "articles": {
        "id",
        "normalized_url",
        "title",
        "media_name",
        "media_type",
        "published_at",
        "imported_at",
        "ts",
        "fetch_method",
        "sort_published_ts",
        "sort_imported_ts",
        "raw_json",
        "updated_at_ns",
    },
    "article_task_links": {"article_id", "task_name", "relation"},
    "history_records": {
        "storage_key",
        "id",
        "task_id",
        "task_name",
        "ts",
        "platform",
        "keyword",
        "brand",
        "rank",
        "success",
        "review_status",
        "mode",
        "execution_source",
        "sort_index",
        "raw_json",
        "updated_at_ns",
    },
}


class ArticleHistorySQLiteStore:
    """Structured SQLite store for future article/history migration.

    This store is intentionally separate from the current JSON-backed runtime
    path. It provides the schema, migration import operations, and paged query
    surface needed before SQLite can become the default backend.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        normalize_article_url: Callable[[str], str] | None = None,
        sqlite_timeout: float = 30.0,
    ) -> None:
        self.db_path = Path(db_path)
        self._normalize_article_url = normalize_article_url
        self._sqlite_timeout = max(0.0, float(sqlite_timeout or 0.0))

    def initialize(self) -> None:
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS store_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS articles (
                    id TEXT PRIMARY KEY,
                    normalized_url TEXT UNIQUE,
                    title TEXT NOT NULL DEFAULT '',
                    media_name TEXT NOT NULL DEFAULT '',
                    media_type TEXT NOT NULL DEFAULT '',
                    published_at TEXT NOT NULL DEFAULT '',
                    imported_at TEXT NOT NULL DEFAULT '',
                    ts TEXT NOT NULL DEFAULT '',
                    fetch_method TEXT NOT NULL DEFAULT '',
                    sort_published_ts INTEGER NOT NULL DEFAULT 0,
                    sort_imported_ts INTEGER NOT NULL DEFAULT 0,
                    raw_json TEXT NOT NULL,
                    updated_at_ns INTEGER NOT NULL
                )
                """
            )
            self._ensure_column(conn, "articles", "sort_published_ts", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "articles", "sort_imported_ts", "INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS article_task_links (
                    article_id TEXT NOT NULL,
                    task_name TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    PRIMARY KEY(article_id, task_name, relation),
                    FOREIGN KEY(article_id) REFERENCES articles(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS history_records (
                    storage_key TEXT NOT NULL,
                    id TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    task_name TEXT NOT NULL DEFAULT '',
                    ts TEXT NOT NULL DEFAULT '',
                    platform TEXT NOT NULL DEFAULT '',
                    keyword TEXT NOT NULL DEFAULT '',
                    brand TEXT NOT NULL DEFAULT '',
                    rank INTEGER NOT NULL DEFAULT 99,
                    success INTEGER NOT NULL DEFAULT 0,
                    review_status TEXT NOT NULL DEFAULT '',
                    mode TEXT NOT NULL DEFAULT '',
                    execution_source TEXT NOT NULL DEFAULT '',
                    sort_index INTEGER NOT NULL DEFAULT 0,
                    raw_json TEXT NOT NULL,
                    updated_at_ns INTEGER NOT NULL,
                    PRIMARY KEY(storage_key, id)
                )
                """
            )
            self._ensure_column(conn, "history_records", "sort_index", "INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_articles_display_time "
                "ON articles(published_at DESC, ts DESC, imported_at DESC, id DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_articles_sort_time "
                "ON articles(sort_published_ts DESC, sort_imported_ts DESC, id DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_articles_media_type "
                "ON articles(media_type)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_article_task_links_lookup "
                "ON article_task_links(task_name, relation, article_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_history_storage_ts "
                "ON history_records(storage_key, ts, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_history_storage_order "
                "ON history_records(storage_key, ts, sort_index, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_history_storage_sort "
                "ON history_records(storage_key, sort_index, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_history_task_id_ts "
                "ON history_records(task_id, ts, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_history_task_name_ts "
                "ON history_records(task_name, ts, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_history_pending_reviews "
                "ON history_records(review_status, rank, ts DESC, task_name DESC, storage_key, sort_index, id)"
            )
            conn.execute(
                """
                INSERT INTO store_meta(key, value) VALUES('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(SCHEMA_VERSION),),
            )
            try:
                perform_startup_maintenance(conn, self.db_path)
            except Exception:
                pass

    def vacuum_wal_if_needed(self) -> bool:
        """Future daily-maintenance hook; startup maintenance is the current safety net."""
        with self._connection() as conn:
            return truncate_wal_if_oversized(conn, self.db_path)

    def import_articles(
        self,
        articles: Iterable[dict[str, Any]],
        *,
        replace: bool = False,
        merge_same_id_url: bool = True,
    ) -> dict[str, int]:
        self.initialize()
        created = 0
        updated = 0
        skipped = 0
        updated_at_ns = time.time_ns()
        with self._connection() as conn:
            if replace:
                conn.execute("DELETE FROM article_task_links")
                conn.execute("DELETE FROM articles")
            for raw_article in articles or []:
                if not isinstance(raw_article, dict):
                    skipped += 1
                    continue
                article = dict(raw_article)
                article_id = self._article_id(article)
                if not article_id:
                    skipped += 1
                    continue
                normalized_url = self._normalized_url(article)
                existing_row = self._find_article_row(
                    conn,
                    article_id=article_id,
                    normalized_url=normalized_url,
                )
                existing_id = existing_row[0] if existing_row is not None else None
                target_id = existing_id or article_id
                existed = existing_id is not None
                if existing_row is not None and (
                    existing_row[2] == "url" or (merge_same_id_url and normalized_url)
                ):
                    article = self._merge_article_for_duplicate_url(existing_row[1], article)
                article["id"] = target_id
                raw_json = self._json_dumps(article)
                sort_published_ts, sort_imported_ts = self._article_sort_key(article)
                conn.execute(
                    """
                    INSERT INTO articles(
                        id, normalized_url, title, media_name, media_type,
                        published_at, imported_at, ts, fetch_method,
                        sort_published_ts, sort_imported_ts, raw_json, updated_at_ns
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        normalized_url = excluded.normalized_url,
                        title = excluded.title,
                        media_name = excluded.media_name,
                        media_type = excluded.media_type,
                        published_at = excluded.published_at,
                        imported_at = excluded.imported_at,
                        ts = excluded.ts,
                        fetch_method = excluded.fetch_method,
                        sort_published_ts = excluded.sort_published_ts,
                        sort_imported_ts = excluded.sort_imported_ts,
                        raw_json = excluded.raw_json,
                        updated_at_ns = excluded.updated_at_ns
                    """,
                    (
                        target_id,
                        normalized_url or None,
                        self._text(article.get("title")),
                        self._text(article.get("media_name") or article.get("source") or article.get("platform")),
                        self._text(article.get("media_type")),
                        self._date_text(article.get("published_at") or article.get("published")),
                        self._date_text(article.get("imported_at") or article.get("created_at")),
                        self._date_text(article.get("ts")),
                        self._text(article.get("fetch_method")),
                        sort_published_ts,
                        sort_imported_ts,
                        raw_json,
                        updated_at_ns,
                    ),
                )
                self._replace_article_task_links(conn, target_id, article)
                if existed:
                    updated += 1
                else:
                    created += 1
        return {"created": created, "updated": updated, "skipped": skipped}

    def upsert_article(self, article: dict[str, Any]) -> dict[str, int]:
        return self.upsert_articles([article])

    def upsert_articles(self, articles: Iterable[dict[str, Any]]) -> dict[str, int]:
        return self.import_articles(articles, replace=False, merge_same_id_url=False)

    def delete_articles_by_ids(self, article_ids: Iterable[str]) -> dict[str, int]:
        self.initialize()
        normalized_ids = [
            self._text(article_id)
            for article_id in (article_ids or [])
            if self._text(article_id)
        ]
        if not normalized_ids:
            return {"deleted": 0}
        deleted = 0
        with self._connection() as conn:
            for article_id in dict.fromkeys(normalized_ids):
                row = conn.execute(
                    "SELECT 1 FROM articles WHERE id = ?",
                    (article_id,),
                ).fetchone()
                if row is None:
                    continue
                conn.execute("DELETE FROM article_task_links WHERE article_id = ?", (article_id,))
                conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
                deleted += 1
        return {"deleted": deleted}

    @classmethod
    def validate_readiness(cls, db_path: str | Path) -> dict[str, Any]:
        path = Path(db_path)
        status: dict[str, Any] = {
            "available": path.exists(),
            "ready": False,
            "stored_signature": "",
            "schema_version": "",
            "reason": "",
            "missing_tables": [],
            "missing_columns": {},
        }
        if not path.exists():
            status["reason"] = "missing"
            return status

        try:
            with path.open("rb") as fp:
                header = fp.read(16)
        except Exception as exc:
            status["reason"] = "exception"
            status["error"] = f"{exc.__class__.__name__}: {exc}"
            return status
        if header and header != b"SQLite format 3\x00":
            status["reason"] = "not_sqlite"
            return status

        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            table_names = {
                str(row[0] or "")
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            missing_tables = sorted(
                table for table in REQUIRED_TABLE_COLUMNS.keys()
                if table not in table_names
            )
            status["missing_tables"] = missing_tables
            if missing_tables:
                status["reason"] = "missing_tables"
                return status

            schema_row = conn.execute(
                "SELECT value FROM store_meta WHERE key = 'schema_version'"
            ).fetchone()
            schema_version = cls._text(schema_row[0]) if schema_row else ""
            status["schema_version"] = schema_version
            if schema_version != str(SCHEMA_VERSION):
                status["reason"] = "schema_version"
                return status

            missing_columns: dict[str, list[str]] = {}
            for table, required_columns in REQUIRED_TABLE_COLUMNS.items():
                columns = {
                    str(row[1] or "")
                    for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                missing = sorted(required_columns - columns)
                if missing:
                    missing_columns[table] = missing
            status["missing_columns"] = missing_columns
            if missing_columns:
                status["reason"] = "missing_columns"
                return status

            signature_row = conn.execute(
                "SELECT value FROM store_meta WHERE key = 'history_source_signature'"
            ).fetchone()
            status["stored_signature"] = cls._text(signature_row[0]) if signature_row else ""
            status["ready"] = True
            status["reason"] = ""
            return status
        except Exception as exc:
            status["reason"] = "exception"
            status["error"] = f"{exc.__class__.__name__}: {exc}"
            status["ready"] = False
            status["stored_signature"] = ""
            return status
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def clear_all(self) -> None:
        self.initialize()
        with self._connection() as conn:
            conn.execute("DELETE FROM article_task_links")
            conn.execute("DELETE FROM articles")
            conn.execute("DELETE FROM history_records")

    def clear_history_records(self) -> None:
        self.initialize()
        with self._connection() as conn:
            conn.execute("DELETE FROM history_records")

    def set_meta(self, key: str, value: str) -> None:
        self.initialize()
        normalized_key = self._text(key)
        if not normalized_key:
            return
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO store_meta(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (normalized_key, str(value or "")),
            )

    def get_meta(self, key: str) -> str:
        self.initialize()
        normalized_key = self._text(key)
        if not normalized_key:
            return ""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT value FROM store_meta WHERE key = ?",
                (normalized_key,),
            ).fetchone()
        return self._text(row[0]) if row else ""

    def get_article_page(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        task_name: str = "",
        relation: str = "matched",
        media_type: str = "",
        search: str = "",
        today: str | None = None,
        dedupe_scan_limit: int = 5000,
    ) -> dict[str, Any]:
        self.initialize()
        where_sql, params = self._article_query_filters(
            task_name=task_name,
            relation=relation,
            media_type=media_type,
            search=search,
        )
        limit = max(1, min(ARTICLE_PAGE_MAX_LIMIT, int(limit or 100)))
        offset = max(0, int(offset or 0))
        today_text = self._date_text(today if today is not None else local_today().isoformat())[:10]
        with self._connection() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM articles a {where_sql}",
                params,
            ).fetchone()[0]
            empty_url_count = self._article_count_with_extra_condition(
                conn,
                where_sql,
                params,
                "(a.normalized_url IS NULL OR a.normalized_url = '')",
            )
            if empty_url_count:
                return self._get_article_page_with_bounded_url_dedupe(
                    conn,
                    where_sql=where_sql,
                    params=params,
                    total=int(total or 0),
                    limit=limit,
                    offset=offset,
                    today_text=today_text,
                    dedupe_scan_limit=dedupe_scan_limit,
                )
            today_total = self._article_today_count_with_conn(
                conn,
                where_sql,
                params,
                today_text,
            )
            rows = conn.execute(
                f"""
                SELECT a.raw_json
                FROM articles a
                {where_sql}
                ORDER BY
                    a.sort_published_ts DESC,
                    a.sort_imported_ts DESC,
                    a.id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        return {
            "total": int(total or 0),
            "today_total": int(today_total),
            "limit": limit,
            "offset": offset,
            "items": [self._json_loads(row[0]) for row in rows],
        }

    def get_article_items(
        self,
        *,
        task_name: str = "",
        relation: str = "matched",
        media_type: str = "",
        search: str = "",
    ) -> list[dict[str, Any]]:
        self.initialize()
        where_sql, params = self._article_query_filters(
            task_name=task_name,
            relation=relation,
            media_type=media_type,
            search=search,
        )
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT a.raw_json
                FROM articles a
                {where_sql}
                ORDER BY
                    a.sort_published_ts DESC,
                    a.sort_imported_ts DESC,
                    a.id DESC
                """,
                params,
            ).fetchall()
        return [self._json_loads(row[0]) for row in rows]

    def get_article_today_count(
        self,
        today: str,
        *,
        task_name: str = "",
        relation: str = "matched",
        media_type: str = "",
        search: str = "",
    ) -> int:
        self.initialize()
        date_text = self._date_text(today)[:10]
        if not date_text:
            return 0
        where_sql, params = self._article_query_filters(
            task_name=task_name,
            relation=relation,
            media_type=media_type,
            search=search,
        )
        with self._connection() as conn:
            return self._article_today_count_with_conn(
                conn,
                where_sql,
                params,
                date_text,
            )

    def _get_article_page_with_bounded_url_dedupe(
        self,
        conn: sqlite3.Connection,
        *,
        where_sql: str,
        params: list[Any],
        total: int,
        limit: int,
        offset: int,
        today_text: str,
        dedupe_scan_limit: int,
    ) -> dict[str, Any]:
        scan_limit = max(0, int(dedupe_scan_limit or 0))
        if total > scan_limit:
            return {
                "total": total,
                "today_total": 0,
                "limit": limit,
                "offset": offset,
                "items": [],
                "fallback_required": True,
                "fallback_reason": "url_dedupe_scan_limit",
                "dedupe_scan_limit": scan_limit,
            }

        rows = conn.execute(
            f"""
            SELECT a.raw_json
            FROM articles a
            {where_sql}
            ORDER BY
                a.sort_published_ts DESC,
                a.sort_imported_ts DESC,
                a.id DESC
            """,
            params,
        ).fetchall()
        filtered_articles = [self._json_loads(row[0]) for row in rows]
        supplemental_articles = self._supplemental_url_articles_for_missing_fingerprints(
            conn,
            filtered_articles,
        )
        deduped = self._dedupe_articles_by_url([*filtered_articles, *supplemental_articles])
        return {
            "total": len(deduped),
            "today_total": sum(
                1 for article in deduped
                if self._article_matches_today(article, today_text)
            ),
            "limit": limit,
            "offset": offset,
            "items": deduped[offset:offset + limit],
            "dedupe_strategy": "bounded_url_scan",
        }

    def _article_count_with_extra_condition(
        self,
        conn: sqlite3.Connection,
        where_sql: str,
        params: list[Any],
        condition: str,
    ) -> int:
        row = conn.execute(
            f"SELECT COUNT(*) FROM articles a {self._append_article_condition(where_sql, condition)}",
            params,
        ).fetchone()
        return int((row or [0])[0] or 0)

    def _article_today_count_with_conn(
        self,
        conn: sqlite3.Connection,
        where_sql: str,
        params: list[Any],
        today_text: str,
    ) -> int:
        date_text = self._date_text(today_text)[:10]
        if not date_text:
            return 0
        today_condition = (
            "(substr(a.published_at, 1, 10) = ? "
            "OR (a.published_at = '' AND a.fetch_method != 'manual_table_import' "
            "AND substr(a.ts, 1, 10) = ?))"
        )
        row = conn.execute(
            f"SELECT COUNT(*) FROM articles a {self._append_article_condition(where_sql, today_condition)}",
            [*params, date_text, date_text],
        ).fetchone()
        return int((row or [0])[0] or 0)

    def import_history_records(
        self,
        storage_key: str,
        records: Iterable[dict[str, Any]],
        *,
        replace: bool = False,
    ) -> dict[str, int]:
        self.initialize()
        normalized_storage_key = self._text(storage_key)
        if not normalized_storage_key:
            return {"created": 0, "updated": 0, "skipped": 0}
        created = 0
        updated = 0
        skipped = 0
        updated_at_ns = time.time_ns()
        with self._connection() as conn:
            if replace:
                conn.execute(
                    "DELETE FROM history_records WHERE storage_key = ?",
                    (normalized_storage_key,),
                )
            start_index = 0 if replace else self._next_history_sort_index(conn, normalized_storage_key)
            for sort_index, raw_record in enumerate(records or [], start=start_index):
                outcome = self._upsert_history_record(
                    conn,
                    normalized_storage_key,
                    raw_record,
                    sort_index=sort_index,
                    updated_at_ns=updated_at_ns,
                )
                if outcome == "updated":
                    updated += 1
                elif outcome == "created":
                    created += 1
                else:
                    skipped += 1
        return {"created": created, "updated": updated, "skipped": skipped}

    def import_history_sources(
        self,
        sources: dict[str, Iterable[dict[str, Any]]],
        *,
        replace: bool = False,
    ) -> dict[str, Any]:
        self.initialize()
        details: dict[str, dict[str, int]] = {}
        totals = {"created": 0, "updated": 0, "skipped": 0}
        updated_at_ns = time.time_ns()
        with self._connection() as conn:
            if replace:
                conn.execute("DELETE FROM history_records")
            for storage_key, records in sorted((sources or {}).items()):
                normalized_storage_key = self._text(storage_key)
                if not normalized_storage_key:
                    continue
                detail = {"created": 0, "updated": 0, "skipped": 0}
                start_index = 0 if replace else self._next_history_sort_index(conn, normalized_storage_key)
                for sort_index, raw_record in enumerate(records or [], start=start_index):
                    outcome = self._upsert_history_record(
                        conn,
                        normalized_storage_key,
                        raw_record,
                        sort_index=sort_index,
                        updated_at_ns=updated_at_ns,
                    )
                    detail[outcome] += 1
                    totals[outcome] += 1
                details[normalized_storage_key] = detail
        return {
            "storage_keys": len(details),
            "created": totals["created"],
            "updated": totals["updated"],
            "skipped": totals["skipped"],
            "details": details,
        }

    def append_history_record(
        self,
        storage_key: str,
        record: dict[str, Any],
        *,
        max_records: int | None = None,
    ) -> dict[str, int]:
        """Append one runtime history record to a storage key."""
        self.initialize()
        normalized_storage_key = self._text(storage_key)
        if not normalized_storage_key:
            return {"created": 0, "updated": 0, "skipped": 1, "pruned": 0}
        updated_at_ns = time.time_ns()
        with self._connection() as conn:
            sort_index = self._next_history_sort_index(conn, normalized_storage_key)
            outcome = self._upsert_history_record(
                conn,
                normalized_storage_key,
                record,
                sort_index=sort_index,
                updated_at_ns=updated_at_ns,
            )
            pruned = 0
            if outcome != "skipped" and max_records is not None:
                pruned = self._prune_history_records(conn, normalized_storage_key, max_records)
        return {
            "created": 1 if outcome == "created" else 0,
            "updated": 1 if outcome == "updated" else 0,
            "skipped": 1 if outcome == "skipped" else 0,
            "pruned": pruned,
        }

    def apply_history_review(
        self,
        storage_keys: Iterable[str],
        record_id: str,
        status: str,
        note: str = "",
        *,
        reviewed_at: str = "",
    ) -> dict[str, Any]:
        """Update review metadata for a record across primary/legacy storage keys."""
        normalized_status = self._text(status)
        normalized_record_id = self._text(record_id)
        if normalized_status not in {"approved", "rejected"} or not normalized_record_id:
            return {"changed": 0, "storage_keys": []}
        unique_storage_keys = []
        seen_storage_keys: set[str] = set()
        for key in storage_keys or []:
            normalized_key = self._text(key)
            if normalized_key and normalized_key not in seen_storage_keys:
                unique_storage_keys.append(normalized_key)
                seen_storage_keys.add(normalized_key)
        if not unique_storage_keys:
            return {"changed": 0, "storage_keys": []}

        changed = 0
        changed_storage_keys: list[str] = []
        updated_at_ns = time.time_ns()
        normalized_note = self._text(note)
        normalized_reviewed_at = self._text(reviewed_at) or local_now().strftime("%Y-%m-%d %H:%M:%S")
        self.initialize()
        with self._connection() as conn:
            for storage_key in unique_storage_keys:
                rows = conn.execute(
                    """
                    SELECT id, raw_json
                    FROM history_records
                    WHERE storage_key = ?
                    ORDER BY sort_index ASC, id ASC
                    """,
                    (storage_key,),
                ).fetchall()
                key_changed = False
                for stored_id, raw_json in rows:
                    record = self._json_loads(raw_json)
                    if self._text(record.get("id") or stored_id) != normalized_record_id:
                        continue
                    record["review_status"] = normalized_status
                    record["review_note"] = normalized_note
                    record["reviewed_at"] = normalized_reviewed_at
                    conn.execute(
                        """
                        UPDATE history_records
                        SET review_status = ?, raw_json = ?, updated_at_ns = ?
                        WHERE storage_key = ? AND id = ?
                        """,
                        (
                            normalized_status,
                            self._json_dumps(record),
                            updated_at_ns,
                            storage_key,
                            self._text(stored_id),
                        ),
                    )
                    changed += 1
                    key_changed = True
                if key_changed:
                    changed_storage_keys.append(storage_key)
        return {"changed": changed, "storage_keys": changed_storage_keys}

    def get_history_records(
        self,
        storage_key: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self.initialize()
        normalized_storage_key = self._text(storage_key)
        if not normalized_storage_key:
            return []
        params: list[Any] = [normalized_storage_key]
        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT ? OFFSET ?"
            params.extend([max(1, int(limit or 1)), max(0, int(offset or 0))])
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT raw_json
                FROM history_records
                WHERE storage_key = ?
                ORDER BY ts ASC, sort_index ASC, id ASC
                {limit_sql}
                """,
                params,
            ).fetchall()
        return [self._json_loads(row[0]) for row in rows]

    def get_history_records_for_keys(self, storage_keys: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        self.initialize()
        unique_keys = []
        seen_keys: set[str] = set()
        for storage_key in storage_keys or []:
            normalized_storage_key = self._text(storage_key)
            if not normalized_storage_key or normalized_storage_key in seen_keys:
                continue
            seen_keys.add(normalized_storage_key)
            unique_keys.append(normalized_storage_key)
        if not unique_keys:
            return {}
        placeholders = ", ".join("?" for _ in unique_keys)
        results: dict[str, list[dict[str, Any]]] = {storage_key: [] for storage_key in unique_keys}
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT storage_key, raw_json
                FROM history_records
                WHERE storage_key IN ({placeholders})
                ORDER BY storage_key ASC, ts ASC, sort_index ASC, id ASC
                """,
                unique_keys,
            ).fetchall()
        for storage_key, raw_json in rows:
            normalized_storage_key = self._text(storage_key)
            if normalized_storage_key in results:
                results[normalized_storage_key].append(self._json_loads(raw_json))
        return results

    def list_history_storage_keys(self) -> list[str]:
        self.initialize()
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT storage_key FROM history_records ORDER BY storage_key"
            ).fetchall()
        return [str(row[0]) for row in rows if str(row[0] or "")]

    def get_history_record_count(self, storage_key: str) -> int:
        self.initialize()
        normalized_storage_key = self._text(storage_key)
        if not normalized_storage_key:
            return 0
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM history_records WHERE storage_key = ?",
                (normalized_storage_key,),
            ).fetchone()
        return int((row or [0])[0] or 0)

    def get_history_tail(self, storage_key: str, *, limit: int = 20) -> list[dict[str, Any]]:
        self.initialize()
        normalized_storage_key = self._text(storage_key)
        if not normalized_storage_key:
            return []
        capped_limit = max(1, min(500, int(limit or 20)))
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT raw_json
                FROM history_records
                WHERE storage_key = ?
                ORDER BY ts DESC, sort_index DESC, id DESC
                LIMIT ?
                """,
                (normalized_storage_key, capped_limit),
            ).fetchall()
        return list(reversed([self._json_loads(row[0]) for row in rows]))

    def get_history_task_names(self) -> list[str]:
        self.initialize()
        names: list[str] = []
        seen_names: set[str] = set()
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT storage_key, raw_json
                FROM history_records AS current
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM history_records AS earlier
                    WHERE earlier.storage_key = current.storage_key
                      AND (
                          earlier.sort_index < current.sort_index
                          OR (earlier.sort_index = current.sort_index AND earlier.id < current.id)
                      )
                )
                ORDER BY storage_key ASC
                """
            ).fetchall()
        for storage_key, raw_json in rows:
            normalized_storage_key = self._text(storage_key)
            if not normalized_storage_key:
                continue
            record = self._normalize_history_record(
                self._json_loads(raw_json),
                storage_key=normalized_storage_key,
            )
            task_name = self._text(record.get("task_name") or normalized_storage_key)
            if task_name and task_name not in seen_names:
                seen_names.add(task_name)
                names.append(task_name)
        return names

    def get_pending_reviews(self, *, limit: int = 200) -> list[dict[str, Any]]:
        self.initialize()
        capped_limit = max(1, int(limit or 200))
        items: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        with self._connection() as conn:
            batch_size = max(capped_limit * 4, 64)
            offset = 0
            while len(items) < capped_limit:
                rows = conn.execute(
                    """
                    SELECT storage_key, raw_json
                    FROM history_records
                    WHERE review_status = 'pending'
                    ORDER BY ts DESC, task_name DESC, storage_key ASC, sort_index ASC, id ASC
                    LIMIT ? OFFSET ?
                    """,
                    (batch_size, offset),
                ).fetchall()
                if not rows:
                    break
                offset += len(rows)
                for storage_key, raw_json in rows:
                    record = self._normalize_history_record(
                        self._json_loads(raw_json),
                        storage_key=self._text(storage_key),
                    )
                    record_id = self._text(record.get("id"))
                    if record_id and record_id in seen_ids:
                        continue
                    if record.get("review_status") != "pending":
                        continue
                    if record.get("rank", 99) == 99:
                        continue
                    if record_id:
                        seen_ids.add(record_id)
                    items.append(record)
                    if len(items) >= capped_limit:
                        break
                if len(rows) < batch_size:
                    break
        return items

    def get_article_task_counts(self, *, relation: str = "matched") -> dict[str, int]:
        self.initialize()
        normalized_relation = self._text(relation) or "matched"
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT task_name, COUNT(*)
                FROM article_task_links
                WHERE relation = ?
                GROUP BY task_name
                ORDER BY task_name
                """,
                (normalized_relation,),
            ).fetchall()
        return {str(row[0]): int(row[1] or 0) for row in rows if str(row[0] or "")}

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=self._sqlite_timeout)
        try:
            apply_runtime_pragmas(conn, busy_timeout_ms=int(self._sqlite_timeout * 1000))
        except BaseException:
            conn.close()
            raise
        return conn

    def _find_article_row(
        self,
        conn: sqlite3.Connection,
        *,
        article_id: str,
        normalized_url: str,
    ) -> tuple[str, dict[str, Any], str] | None:
        if normalized_url:
            row = conn.execute(
                "SELECT id, raw_json FROM articles WHERE normalized_url = ?",
                (normalized_url,),
            ).fetchone()
            if row is not None:
                existing_id = str(row[0])
                matched_by = "id" if existing_id == article_id else "url"
                return existing_id, self._json_loads(row[1]), matched_by
        row = conn.execute("SELECT id, raw_json FROM articles WHERE id = ?", (article_id,)).fetchone()
        if row is None:
            return None
        return str(row[0]), self._json_loads(row[1]), "id"

    def _replace_article_task_links(self, conn: sqlite3.Connection, article_id: str, article: dict[str, Any]) -> None:
        conn.execute("DELETE FROM article_task_links WHERE article_id = ?", (article_id,))
        for relation, field_name in (
            ("matched", "matched_tasks"),
            ("excluded", "excluded_tasks"),
            ("referenced", "referenced_tasks"),
        ):
            for task_name in self._unique_texts(article.get(field_name)):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO article_task_links(article_id, task_name, relation)
                    VALUES(?, ?, ?)
                    """,
                    (article_id, task_name, relation),
                )

    @classmethod
    def _merge_article_for_duplicate_url(
        cls,
        base: dict[str, Any],
        duplicate: dict[str, Any],
    ) -> dict[str, Any]:
        merged = dict(base)
        for key in ("matched_tasks", "referenced_tasks", "cloud_task_ids"):
            merged[key] = cls._merge_unique_texts(merged.get(key), duplicate.get(key))

        base_reasons = merged.get("match_reasons") if isinstance(merged.get("match_reasons"), dict) else {}
        duplicate_reasons = duplicate.get("match_reasons") if isinstance(duplicate.get("match_reasons"), dict) else {}
        if base_reasons or duplicate_reasons:
            next_reasons: dict[str, list[str]] = {}
            for task_name in set(base_reasons.keys()) | set(duplicate_reasons.keys()):
                next_reasons[str(task_name)] = cls._merge_unique_texts(
                    base_reasons.get(task_name),
                    duplicate_reasons.get(task_name),
                )
            merged["match_reasons"] = next_reasons

        base_hits = merged.get("reference_hits") if isinstance(merged.get("reference_hits"), dict) else {}
        duplicate_hits = duplicate.get("reference_hits") if isinstance(duplicate.get("reference_hits"), dict) else {}
        if duplicate_hits:
            merged["reference_hits"] = {**base_hits, **duplicate_hits}

        for key in ("url", "raw_url", "title", "media_name", "platform", "published_at", "ts"):
            if not cls._text(merged.get(key)) and cls._text(duplicate.get(key)):
                merged[key] = duplicate.get(key)
        return merged

    @classmethod
    def _merge_unique_texts(cls, left: Any, right: Any) -> list[str]:
        result: list[str] = []
        for values in (left, right):
            if not isinstance(values, list):
                continue
            for value in values:
                text = cls._text(value)
                if text and text not in result:
                    result.append(text)
        return result

    def _upsert_history_record(
        self,
        conn: sqlite3.Connection,
        storage_key: str,
        raw_record: Any,
        *,
        sort_index: int,
        updated_at_ns: int,
    ) -> Literal["created", "updated", "skipped"]:
        if not isinstance(raw_record, dict):
            return "skipped"
        record = dict(raw_record)
        record_for_columns = self._normalize_history_record(record, storage_key=storage_key)
        record_id = self._history_record_id(storage_key, record)
        if not record_id:
            return "skipped"
        existed = conn.execute(
            "SELECT 1 FROM history_records WHERE storage_key = ? AND id = ?",
            (storage_key, record_id),
        ).fetchone() is not None
        conn.execute(
            """
            INSERT INTO history_records(
                storage_key, id, task_id, task_name, ts, platform,
                keyword, brand, rank, success, review_status, mode,
                execution_source, sort_index, raw_json, updated_at_ns
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(storage_key, id) DO UPDATE SET
                task_id = excluded.task_id,
                task_name = excluded.task_name,
                ts = excluded.ts,
                platform = excluded.platform,
                keyword = excluded.keyword,
                brand = excluded.brand,
                rank = excluded.rank,
                success = excluded.success,
                review_status = excluded.review_status,
                mode = excluded.mode,
                execution_source = excluded.execution_source,
                sort_index = excluded.sort_index,
                raw_json = excluded.raw_json,
                updated_at_ns = excluded.updated_at_ns
            """,
            (
                storage_key,
                record_id,
                self._text(record_for_columns.get("task_id")),
                self._text(record_for_columns.get("task_name")),
                self._date_text(record_for_columns.get("ts")),
                self._text(record_for_columns.get("platform")),
                self._text(record_for_columns.get("keyword")),
                self._text(record_for_columns.get("brand")),
                self._int(record_for_columns.get("rank"), default=99),
                1 if bool(record.get("success")) else 0,
                self._text(record_for_columns.get("review_status")),
                self._text(record_for_columns.get("mode")),
                self._text(record_for_columns.get("execution_source")),
                int(sort_index),
                self._json_dumps(record),
                updated_at_ns,
            ),
        )
        return "updated" if existed else "created"

    def _next_history_sort_index(self, conn: sqlite3.Connection, storage_key: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(sort_index), -1) + 1 FROM history_records WHERE storage_key = ?",
            (storage_key,),
        ).fetchone()
        return int((row or [0])[0] or 0)

    def _prune_history_records(self, conn: sqlite3.Connection, storage_key: str, max_records: int) -> int:
        capped_max = max(1, int(max_records or 1))
        rows = conn.execute(
            """
            SELECT id
            FROM history_records
            WHERE storage_key = ?
            ORDER BY sort_index DESC, id DESC
            LIMIT -1 OFFSET ?
            """,
            (storage_key, capped_max),
        ).fetchall()
        stale_ids = [self._text(row[0]) for row in rows if self._text(row[0])]
        for stale_id in stale_ids:
            conn.execute(
                "DELETE FROM history_records WHERE storage_key = ? AND id = ?",
                (storage_key, stale_id),
            )
        return len(stale_ids)

    @classmethod
    def _normalize_history_record(cls, record: dict[str, Any], *, storage_key: str) -> dict[str, Any]:
        item = dict(record) if isinstance(record, dict) else {}
        if storage_key and not item.get("task_name"):
            item["task_name"] = storage_key
        item.setdefault("id", "")
        item.setdefault("review_status", "pending" if item.get("success") and item.get("rank", 99) != 99 else "")
        item.setdefault("review_note", "")
        item.setdefault("reviewed_at", "")
        item.setdefault("screenshot", "")
        item.setdefault("highlight_count", 0)
        item.setdefault("answer_text", "")
        item.setdefault("evidence", "")
        item.setdefault("error_message", "")
        item.setdefault("diagnostic_id", "")
        item.setdefault("mode", "")
        item.setdefault("execution_source", "")
        return item

    def _article_query_filters(
        self,
        *,
        task_name: str,
        relation: str,
        media_type: str,
        search: str,
    ) -> tuple[str, list[Any]]:
        joins = []
        conditions = []
        params: list[Any] = []
        task = self._text(task_name)
        if task:
            joins.append(
                "JOIN article_task_links atl ON atl.article_id = a.id "
                "AND atl.task_name = ? AND atl.relation = ?"
            )
            params.extend([task, self._text(relation) or "matched"])
        media = self._text(media_type)
        if media:
            conditions.append("a.media_type = ?")
            params.append(media)
        query = self._text(search)
        if query:
            like = f"%{query}%"
            conditions.append("(a.title LIKE ? OR a.media_name LIKE ? OR a.normalized_url LIKE ?)")
            params.extend([like, like, like])
        where = " ".join(joins)
        if conditions:
            where = f"{where} WHERE {' AND '.join(conditions)}"
        return where, params

    @staticmethod
    def _append_article_condition(where_sql: str, condition: str) -> str:
        normalized_condition = str(condition or "").strip()
        if not normalized_condition:
            return where_sql
        if " WHERE " in f" {where_sql} ":
            return f"{where_sql} AND {normalized_condition}"
        if where_sql:
            return f"{where_sql} WHERE {normalized_condition}"
        return f"WHERE {normalized_condition}"

    def _dedupe_articles_by_url(self, articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        index_by_url: dict[str, int] = {}
        url_by_fingerprint: dict[str, str] = {}
        for article in articles:
            if not isinstance(article, dict):
                continue
            normalized_url = self._normalized_url(article)
            fingerprint = self._article_url_fingerprint(article)
            if normalized_url and fingerprint and fingerprint not in url_by_fingerprint:
                url_by_fingerprint[fingerprint] = self._display_url(article)

        for article in articles:
            if not isinstance(article, dict):
                continue
            item = dict(article)
            normalized_url = self._normalized_url(item)
            if not normalized_url:
                fallback_url = url_by_fingerprint.get(self._article_url_fingerprint(item), "")
                if fallback_url:
                    item["url"] = fallback_url
                    normalized_url = self._normalized_url(item)
            if not normalized_url:
                deduped.append(item)
                continue
            existing_index = index_by_url.get(normalized_url)
            if existing_index is None:
                index_by_url[normalized_url] = len(deduped)
                deduped.append(item)
                continue
            deduped[existing_index] = self._merge_article_for_duplicate_url(deduped[existing_index], item)
        return deduped

    def _supplemental_url_articles_for_missing_fingerprints(
        self,
        conn: sqlite3.Connection,
        articles: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        target_fingerprints = {
            self._article_url_fingerprint(article)
            for article in articles
            if isinstance(article, dict) and not self._normalized_url(article)
        }
        target_fingerprints.discard("")
        if not target_fingerprints:
            return []

        supplemental: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        rows = conn.execute(
            """
            SELECT raw_json
            FROM articles
            WHERE normalized_url IS NOT NULL AND normalized_url != ''
            ORDER BY sort_published_ts DESC, sort_imported_ts DESC, id DESC
            """
        ).fetchall()
        for row in rows:
            article = self._json_loads(row[0])
            fingerprint = self._article_url_fingerprint(article)
            if fingerprint not in target_fingerprints:
                continue
            normalized_url = self._normalized_url(article)
            if not normalized_url or normalized_url in seen_urls:
                continue
            supplemental.append(article)
            seen_urls.add(normalized_url)
        return supplemental

    @classmethod
    def _article_url_fingerprint(cls, article: dict[str, Any]) -> str:
        title = re.sub(r"\s+", " ", cls._text(article.get("title"))).lower()
        source = re.sub(
            r"\s+",
            " ",
            cls._text(article.get("media_name") or article.get("source") or article.get("platform")),
        ).lower()
        published = cls._text(article.get("published_at") or article.get("published") or article.get("ts"))[:10]
        if not title or not source:
            return ""
        return "|".join([title, source, published])

    def _display_url(self, article: dict[str, Any]) -> str:
        stored_url = self._text(article.get("url"))
        raw_url = self._text(article.get("raw_url"))
        if raw_url:
            normalized_raw_url = self._normalize_url_text(raw_url)
            normalized_stored_url = self._normalize_url_text(stored_url)
            if normalized_raw_url and (not normalized_stored_url or normalized_raw_url == normalized_stored_url):
                return raw_url
        return stored_url

    @classmethod
    def _article_matches_today(cls, article: dict[str, Any], today_text: str) -> bool:
        date_text = cls._date_text(today_text)[:10]
        if not date_text:
            return False
        published_at = article.get("published_at")
        if published_at:
            return parse_local_date(published_at) == parse_local_date(date_text)
        if cls._text(article.get("fetch_method")) == "manual_table_import":
            return False
        return parse_local_date(article.get("ts")) == parse_local_date(date_text)

    def _article_id(self, article: dict[str, Any]) -> str:
        explicit = self._text(article.get("id"))
        if explicit:
            return explicit
        payload = {
            "url": self._normalized_url(article),
            "title": self._text(article.get("title")),
            "published_at": self._date_text(article.get("published_at") or article.get("ts")),
        }
        digest = hashlib.sha1(self._json_dumps(payload).encode("utf-8")).hexdigest()[:20]
        return f"article:{digest}"

    def _history_record_id(self, storage_key: str, record: dict[str, Any]) -> str:
        explicit = self._text(record.get("id"))
        if explicit:
            return explicit
        payload = dict(record)
        payload["storage_key"] = storage_key
        digest = hashlib.sha1(self._json_dumps(payload).encode("utf-8")).hexdigest()[:20]
        return f"record:{digest}"

    def _normalized_url(self, article: dict[str, Any]) -> str:
        raw_url = self._text(article.get("url")) or self._text(article.get("raw_url"))
        if not raw_url:
            return ""
        return self._normalize_url_text(raw_url)

    def _normalize_url_text(self, value: Any) -> str:
        raw_url = self._text(value)
        if not raw_url:
            return ""
        if self._normalize_article_url is None:
            return raw_url
        return self._text(self._normalize_article_url(raw_url))

    @staticmethod
    def _unique_texts(values: Any) -> list[str]:
        result: list[str] = []
        if not isinstance(values, list):
            return result
        for value in values:
            text = ArticleHistorySQLiteStore._text(value)
            if text and text not in result:
                result.append(text)
        return result

    @staticmethod
    def _json_dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _json_loads(value: str) -> dict[str, Any]:
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _text(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _date_text(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _int(value: Any, *, default: int = 0) -> int:
        try:
            return int(value)
        except Exception:
            return default

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, name: str, definition: str) -> None:
        columns = {
            str(row[1] or "")
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if name not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    @classmethod
    def _article_sort_key(cls, article: dict[str, Any]) -> tuple[int, int]:
        published_timestamp = cls._first_article_sort_timestamp(
            article,
            ("published_ts", "published_at", "published", "ts"),
        )
        imported_timestamp = cls._first_article_sort_timestamp(
            article,
            ("imported_at", "created_at", "ts"),
        )
        return published_timestamp, imported_timestamp

    @classmethod
    def _first_article_sort_timestamp(cls, article: dict[str, Any], keys: tuple[str, ...]) -> int:
        for key in keys:
            timestamp = cls._sort_timestamp(article.get(key))
            if timestamp > 0:
                return timestamp
        return 0

    @staticmethod
    def _sort_timestamp(value: Any) -> int:
        text = str(value or "").strip()
        if not text:
            return 0
        if re.fullmatch(r"\d{13}", text):
            try:
                return int(text) // 1000
            except Exception:
                return 0
        if re.fullmatch(r"\d{10}", text):
            try:
                return int(text)
            except Exception:
                return 0

        normalized = text.replace("T", " ").replace("Z", "+00:00")
        normalized = re.sub(
            r"([0-9]{4})\s*年\s*([0-9]{1,2})\s*月\s*([0-9]{1,2})\s*日?",
            r"\1-\2-\3",
            normalized,
        )
        normalized = re.sub(r"([0-9]{4})\.([0-9]{1,2})\.([0-9]{1,2})", r"\1-\2-\3", normalized)
        normalized = normalized.replace("/", "-")
        normalized = re.sub(r"(?<=\d{2}:\d{2}:\d{2})\.\d+", "", normalized)
        try:
            return int(datetime.fromisoformat(normalized).timestamp())
        except Exception:
            pass
        for fmt, width in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
            try:
                return int(datetime.strptime(normalized[:width], fmt).timestamp())
            except Exception:
                continue
        match = re.search(r"((?:19|20)\d{2}-\d{1,2}-\d{1,2})", normalized)
        if match:
            try:
                return int(datetime.strptime(match.group(1), "%Y-%m-%d").timestamp())
            except Exception:
                pass
        return 0
