from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .time_utils import local_now, local_today, parse_local_date


SCHEMA_VERSION = 1

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
        "match_signature",
        "match_config_signature",
        "raw_json",
        "updated_at_ns",
    },
    "article_task_links": {"article_id", "task_name", "relation"},
}


class ArticleSQLiteStore:
    """Authoritative SQLite storage for ArticleStore article records.

    The schema intentionally mirrors the structured article part of the
    history shadow database, but this class owns only ArticleStore semantics:
    article CRUD, normalized-url upsert behavior, task links, metadata, and
    migration imports.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        normalize_article_url: Callable[[str], str] | None = None,
        normalize_article_entry: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        now_text: Callable[[], str] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self._normalize_article_url = normalize_article_url
        self._normalize_article_entry = normalize_article_entry
        self._now_text = now_text or (lambda: local_now().strftime("%Y-%m-%d %H:%M"))

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
                    match_signature TEXT NOT NULL DEFAULT '',
                    match_config_signature TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL,
                    updated_at_ns INTEGER NOT NULL
                )
                """
            )
            self._ensure_column(conn, "articles", "sort_published_ts", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "articles", "sort_imported_ts", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "articles", "match_signature", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "articles", "match_config_signature", "TEXT NOT NULL DEFAULT ''")
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
                "CREATE INDEX IF NOT EXISTS idx_article_store_sort_time "
                "ON articles(sort_published_ts DESC, sort_imported_ts DESC, id DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_article_store_media_type "
                "ON articles(media_type)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_article_store_task_links "
                "ON article_task_links(task_name, relation, article_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_article_store_match_refresh "
                "ON articles(match_config_signature, match_signature, sort_published_ts DESC, sort_imported_ts DESC, id DESC)"
            )
            conn.execute(
                """
                INSERT INTO store_meta(key, value) VALUES('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(SCHEMA_VERSION),),
            )

    def import_from_json(self, articles_path: str | Path, *, replace: bool = True) -> dict[str, int]:
        path = Path(articles_path)
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            parsed = []
        if not isinstance(parsed, list):
            parsed = []
        return self.import_from_articles(parsed, replace=replace)

    def import_from_articles(
        self,
        articles: Iterable[dict[str, Any]],
        *,
        replace: bool = True,
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
                article["id"] = article_id
                article = self._clear_match_metadata(article)
                article = self._normalize_article(article)
                normalized_url = self._normalized_url(article)
                existing = self._find_article_row(
                    conn,
                    article_id=article_id,
                    normalized_url=normalized_url,
                    prefer_id=True,
                )
                if existing is not None:
                    article["id"] = existing[0]
                existed = existing is not None
                self._upsert_article_row(conn, article, updated_at_ns=updated_at_ns)
                if existed:
                    updated += 1
                else:
                    created += 1
        return {"created": created, "updated": updated, "skipped": skipped}

    def list_articles(self) -> list[dict[str, Any]]:
        self.initialize()
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT raw_json
                FROM articles
                ORDER BY sort_published_ts DESC, sort_imported_ts DESC, id DESC
                """
            ).fetchall()
        return [self._json_loads(row[0]) for row in rows]

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
    ) -> dict[str, Any]:
        self.initialize()
        where_sql, params = self._article_query_filters(
            task_name=task_name,
            relation=relation,
            media_type=media_type,
            search=search,
        )
        capped_limit = max(1, min(500, int(limit or 100)))
        capped_offset = max(0, int(offset or 0))
        today_text = self._date_text(today if today is not None else local_today().isoformat())[:10]
        with self._connection() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM articles a {where_sql}",
                params,
            ).fetchone()[0]
            today_total = self._article_today_count_with_conn(conn, where_sql, params, today_text)
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
                [*params, capped_limit, capped_offset],
            ).fetchall()
        return {
            "total": int(total or 0),
            "today_total": int(today_total),
            "limit": capped_limit,
            "offset": capped_offset,
            "items": [self._json_loads(row[0]) for row in rows],
        }

    def bulk_upsert_articles(self, entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        prepared_entries = [dict(entry) for entry in (entries or []) if isinstance(entry, dict)]
        if not prepared_entries:
            return []
        self.initialize()
        now_text = self._now_text()
        updated_at_ns = time.time_ns()
        results: list[dict[str, Any]] = []
        with self._connection() as conn:
            for raw_entry in prepared_entries:
                entry = dict(raw_entry)
                article_id = self._text(entry.get("id"))
                normalized_url = self._normalized_url(entry)
                existing = self._find_article_row(
                    conn,
                    article_id=article_id,
                    normalized_url=normalized_url,
                    prefer_id=True,
                )
                if existing is None:
                    entry = self._apply_article_defaults(entry, now_text=now_text)
                else:
                    current = dict(existing[1])
                    merged = dict(current)
                    merged.update(entry)
                    merged["id"] = current.get("id", "") or article_id or uuid.uuid4().hex
                    if "ts" not in entry or not self._text(entry.get("ts")):
                        merged["ts"] = current.get("ts", merged.get("ts", ""))
                    entry = self._apply_article_defaults(
                        merged,
                        now_text=now_text,
                        existing=current,
                    )
                entry = self._clear_match_metadata(entry)
                article = self._normalize_article(entry)
                self._upsert_article_row(conn, article, updated_at_ns=updated_at_ns)
                results.append(dict(article))
        return results

    def add_article(self, entry: dict[str, Any]) -> dict[str, Any]:
        self.initialize()
        now_text = self._now_text()
        raw_entry = dict(entry or {})
        article = self._apply_article_defaults(raw_entry, now_text=now_text)
        article = self._clear_match_metadata(article)
        article = self._normalize_article(article)
        normalized_url = self._normalized_url(article)
        updated_at_ns = time.time_ns()
        with self._connection() as conn:
            if normalized_url:
                existing = self._find_article_row(
                    conn,
                    article_id=self._text(article.get("id")),
                    normalized_url=normalized_url,
                    prefer_id=False,
                )
                if existing is not None:
                    return dict(existing[1])
            self._upsert_article_row(conn, article, updated_at_ns=updated_at_ns)
        return dict(article)

    def update_article(self, article_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        normalized_id = self._text(article_id)
        if not normalized_id:
            return None
        self.initialize()
        updated_at_ns = time.time_ns()
        with self._connection() as conn:
            existing = self._get_article_by_id_conn(conn, normalized_id)
            if existing is None:
                return None
            merged = dict(existing)
            updates = dict(patch or {})
            merged.update(updates)
            merged["id"] = existing.get("id", merged.get("id", ""))
            if "ts" not in updates or not self._text(updates.get("ts")):
                merged["ts"] = existing.get("ts", merged.get("ts", ""))
            merged = self._clear_match_metadata(merged)
            merged = self._apply_article_defaults(
                merged,
                now_text=self._now_text(),
                existing=existing,
            )
            article = self._normalize_article(merged)
            self._upsert_article_row(conn, article, updated_at_ns=updated_at_ns)
        return dict(article)

    def delete_article(self, article_id: str) -> dict[str, Any] | None:
        normalized_id = self._text(article_id)
        if not normalized_id:
            return None
        self.initialize()
        with self._connection() as conn:
            article = self._get_article_by_id_conn(conn, normalized_id)
            if article is None:
                return None
            conn.execute("DELETE FROM article_task_links WHERE article_id = ?", (normalized_id,))
            conn.execute("DELETE FROM articles WHERE id = ?", (normalized_id,))
        return dict(article)

    def get_article_by_url(self, url: str) -> dict[str, Any] | None:
        normalized_url = self._normalize_url_text(url)
        if not normalized_url:
            return None
        self.initialize()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT raw_json FROM articles WHERE normalized_url = ?",
                (normalized_url,),
            ).fetchone()
        return self._json_loads(row[0]) if row else None

    def get_article_by_id(self, article_id: str) -> dict[str, Any] | None:
        normalized_id = self._text(article_id)
        if not normalized_id:
            return None
        self.initialize()
        with self._connection() as conn:
            return self._get_article_by_id_conn(conn, normalized_id)

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

    def count_articles(self) -> int:
        self.initialize()
        with self._connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM articles").fetchone()
        return int((row or [0])[0] or 0)

    def get_match_refresh_stats(self, config_signature: str) -> dict[str, int]:
        self.initialize()
        signature = self._text(config_signature)
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*),
                    SUM(CASE WHEN match_config_signature = ? AND match_signature != '' THEN 1 ELSE 0 END)
                FROM articles
                """,
                (signature,),
            ).fetchone()
        total = int((row or [0, 0])[0] or 0)
        matching = int((row or [0, 0])[1] or 0)
        return {
            "total": total,
            "matching_signature_count": matching,
            "needs_refresh_count": max(0, total - matching),
        }

    def iter_articles_needing_match(
        self,
        config_signature: str,
        *,
        batch_size: int = 500,
        limit: int | None = None,
    ):
        self.initialize()
        signature = self._text(config_signature)
        capped_batch_size = max(1, min(5000, int(batch_size or 500)))
        remaining = None if limit is None else max(0, int(limit or 0))
        last_key: tuple[int, int, str] | None = None
        while remaining is None or remaining > 0:
            current_limit = capped_batch_size if remaining is None else min(capped_batch_size, remaining)
            if current_limit <= 0:
                break
            params: list[Any] = [signature]
            keyset_sql = ""
            if last_key is not None:
                keyset_sql = """
                    AND (
                        sort_published_ts < ?
                        OR (sort_published_ts = ? AND sort_imported_ts < ?)
                        OR (sort_published_ts = ? AND sort_imported_ts = ? AND id < ?)
                    )
                """
                params.extend([
                    last_key[0],
                    last_key[0],
                    last_key[1],
                    last_key[0],
                    last_key[1],
                    last_key[2],
                ])
            params.append(current_limit)
            with self._connection() as conn:
                rows = conn.execute(
                    f"""
                    SELECT raw_json, sort_published_ts, sort_imported_ts, id
                    FROM articles
                    WHERE NOT (match_config_signature = ? AND match_signature != '')
                    {keyset_sql}
                    ORDER BY sort_published_ts DESC, sort_imported_ts DESC, id DESC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
            if not rows:
                break
            batch = [self._json_loads(row[0]) for row in rows]
            yield batch
            last_row = rows[-1]
            last_key = (int(last_row[1] or 0), int(last_row[2] or 0), self._text(last_row[3]))
            if remaining is not None:
                remaining -= len(rows)
            if len(rows) < current_limit:
                break

    def bulk_update_match_fields(self, updates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        prepared_updates = [dict(update) for update in (updates or []) if isinstance(update, dict)]
        if not prepared_updates:
            return []
        self.initialize()
        updated_at_ns = time.time_ns()
        results: list[dict[str, Any]] = []
        with self._connection() as conn:
            for update in prepared_updates:
                article_id = self._text(update.get("id"))
                if not article_id:
                    continue
                existing = self._get_article_by_id_conn(conn, article_id)
                if existing is None:
                    continue
                article = dict(existing)
                article["matched_tasks"] = self._unique_texts(update.get("matched_tasks"))
                article["match_reasons"] = self._normalize_match_reasons(update.get("match_reasons"))
                article["unmatched_reason"] = self._text(update.get("unmatched_reason"))
                article["_match_signature"] = self._text(update.get("_match_signature"))
                article["_match_config_signature"] = self._text(
                    update.get("_match_config_signature") or update.get("match_config_signature")
                )
                article = self._normalize_article(article)
                self._upsert_article_row(conn, article, updated_at_ns=updated_at_ns)
                results.append(dict(article))
        return results

    def source_signature(self) -> str:
        self.initialize()
        digest = hashlib.sha256()
        count = 0
        max_updated_at_ns = 0
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT id, updated_at_ns, raw_json
                FROM articles
                ORDER BY id
                """
            ).fetchall()
        for article_id, updated_at_ns, raw_json in rows:
            count += 1
            try:
                max_updated_at_ns = max(max_updated_at_ns, int(updated_at_ns or 0))
            except Exception:
                pass
            digest.update(self._text(article_id).encode("utf-8", errors="ignore"))
            digest.update(b"\0")
            digest.update(str(raw_json or "").encode("utf-8", errors="ignore"))
            digest.update(b"\0")
        return f"{self.db_path}::articles:{count}:{max_updated_at_ns}:{digest.hexdigest()}"

    def file_signature(self) -> tuple[str, int, int]:
        self.initialize()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(updated_at_ns), 0) FROM articles"
            ).fetchone()
        count = int((row or [0, 0])[0] or 0)
        max_updated_at_ns = int((row or [0, 0])[1] or 0)
        return (self.source_signature(), max_updated_at_ns, count)

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

    @classmethod
    def validate_readiness(cls, db_path: str | Path) -> dict[str, Any]:
        path = Path(db_path)
        status: dict[str, Any] = {
            "available": path.exists(),
            "ready": False,
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
            status["ready"] = True
            return status
        except Exception as exc:
            status["reason"] = "exception"
            status["error"] = f"{exc.__class__.__name__}: {exc}"
            return status
        finally:
            if conn is not None:
                conn.close()

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
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA foreign_keys = ON")
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
        prefer_id: bool,
    ) -> tuple[str, dict[str, Any], str] | None:
        normalized_id = self._text(article_id)
        normalized_url = self._text(normalized_url)
        if prefer_id and normalized_id:
            row = conn.execute(
                "SELECT id, raw_json FROM articles WHERE id = ?",
                (normalized_id,),
            ).fetchone()
            if row is not None:
                return str(row[0]), self._json_loads(row[1]), "id"
        if normalized_url:
            row = conn.execute(
                "SELECT id, raw_json FROM articles WHERE normalized_url = ?",
                (normalized_url,),
            ).fetchone()
            if row is not None:
                matched_by = "id" if str(row[0]) == normalized_id else "url"
                return str(row[0]), self._json_loads(row[1]), matched_by
        if not prefer_id and normalized_id:
            row = conn.execute(
                "SELECT id, raw_json FROM articles WHERE id = ?",
                (normalized_id,),
            ).fetchone()
            if row is not None:
                return str(row[0]), self._json_loads(row[1]), "id"
        return None

    def _get_article_by_id_conn(
        self,
        conn: sqlite3.Connection,
        article_id: str,
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT raw_json FROM articles WHERE id = ?",
            (article_id,),
        ).fetchone()
        return self._json_loads(row[0]) if row else None

    def _upsert_article_row(
        self,
        conn: sqlite3.Connection,
        article: dict[str, Any],
        *,
        updated_at_ns: int,
    ) -> None:
        article_id = self._article_id(article)
        if not article_id:
            return
        article["id"] = article_id
        normalized_url = self._normalized_url(article)
        sort_published_ts, sort_imported_ts = self._article_sort_key(article)
        conn.execute(
            """
            INSERT INTO articles(
                id, normalized_url, title, media_name, media_type,
                published_at, imported_at, ts, fetch_method,
                sort_published_ts, sort_imported_ts,
                match_signature, match_config_signature,
                raw_json, updated_at_ns
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                match_signature = excluded.match_signature,
                match_config_signature = excluded.match_config_signature,
                raw_json = excluded.raw_json,
                updated_at_ns = excluded.updated_at_ns
            """,
            (
                article_id,
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
                self._text(article.get("_match_signature")),
                self._text(article.get("_match_config_signature")),
                self._json_dumps(article),
                int(updated_at_ns),
            ),
        )
        self._replace_article_task_links(conn, article_id, article)

    def _replace_article_task_links(
        self,
        conn: sqlite3.Connection,
        article_id: str,
        article: dict[str, Any],
    ) -> None:
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

    def _apply_article_defaults(
        self,
        article: dict[str, Any],
        *,
        now_text: str,
        existing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        item = dict(article)
        item.setdefault("id", uuid.uuid4().hex)
        item.setdefault("ts", now_text)
        item.setdefault("url", "")
        item.setdefault("title", "")
        item.setdefault("platform", "")
        item.setdefault("media_name", "")
        item.setdefault("media_type", "selfmedia")
        item.setdefault("excerpt", "")
        if existing is None:
            item.setdefault("imported_at", now_text)
        else:
            item.setdefault(
                "imported_at",
                existing.get("imported_at", "")
                or existing.get("created_at", "")
                or existing.get("ts", "")
                or now_text,
            )
        item.setdefault("matched_tasks", [])
        item.setdefault("match_reasons", {})
        item.setdefault("unmatched_reason", "")
        item.setdefault("fetch_method", "html")
        return item

    def _normalize_article(self, article: dict[str, Any]) -> dict[str, Any]:
        item = dict(article)
        if self._normalize_article_entry is not None:
            normalized = self._normalize_article_entry(item)
            if isinstance(normalized, dict):
                return dict(normalized)
        normalized_url = self._normalized_url(item)
        if normalized_url:
            item["url"] = normalized_url
        return item

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

    def _normalized_url(self, article: dict[str, Any]) -> str:
        return self._normalize_url_text(article.get("url"))

    def _normalize_url_text(self, value: Any) -> str:
        raw_url = self._text(value)
        if not raw_url:
            return ""
        if self._normalize_article_url is None:
            return raw_url
        return self._text(self._normalize_article_url(raw_url))

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

    @staticmethod
    def _unique_texts(values: Any) -> list[str]:
        result: list[str] = []
        if not isinstance(values, list):
            return result
        for value in values:
            text = ArticleSQLiteStore._text(value)
            if text and text not in result:
                result.append(text)
        return result

    @classmethod
    def _normalize_match_reasons(cls, value: Any) -> dict[str, list[str]]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, list[str]] = {}
        for raw_task_name, raw_reasons in value.items():
            task_name = cls._text(raw_task_name)
            if not task_name:
                continue
            reasons: list[str] = []
            if isinstance(raw_reasons, list):
                for raw_reason in raw_reasons:
                    reason = cls._text(raw_reason)
                    if reason:
                        reasons.append(reason)
            elif raw_reasons is not None:
                reason = cls._text(raw_reasons)
                if reason:
                    reasons.append(reason)
            result[task_name] = reasons
        return result

    @staticmethod
    def _clear_match_metadata(article: dict[str, Any]) -> dict[str, Any]:
        item = dict(article)
        item.pop("_match_signature", None)
        item.pop("_match_config_signature", None)
        return item

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
