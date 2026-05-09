from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


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
    ) -> None:
        self.db_path = Path(db_path)
        self._normalize_article_url = normalize_article_url

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
                    raw_json TEXT NOT NULL,
                    updated_at_ns INTEGER NOT NULL
                )
                """
            )
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
                    raw_json TEXT NOT NULL,
                    updated_at_ns INTEGER NOT NULL,
                    PRIMARY KEY(storage_key, id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_articles_display_time "
                "ON articles(published_at DESC, ts DESC, imported_at DESC, id DESC)"
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
                "CREATE INDEX IF NOT EXISTS idx_history_task_id_ts "
                "ON history_records(task_id, ts, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_history_task_name_ts "
                "ON history_records(task_name, ts, id)"
            )
            conn.execute(
                """
                INSERT INTO store_meta(key, value) VALUES('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(SCHEMA_VERSION),),
            )

    def import_articles(self, articles: Iterable[dict[str, Any]], *, replace: bool = False) -> dict[str, int]:
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
                existing_id = self._find_article_id(conn, article_id=article_id, normalized_url=normalized_url)
                target_id = existing_id or article_id
                existed = existing_id is not None
                article["id"] = target_id
                raw_json = self._json_dumps(article)
                conn.execute(
                    """
                    INSERT INTO articles(
                        id, normalized_url, title, media_name, media_type,
                        published_at, imported_at, ts, fetch_method, raw_json, updated_at_ns
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        normalized_url = excluded.normalized_url,
                        title = excluded.title,
                        media_name = excluded.media_name,
                        media_type = excluded.media_type,
                        published_at = excluded.published_at,
                        imported_at = excluded.imported_at,
                        ts = excluded.ts,
                        fetch_method = excluded.fetch_method,
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

    def get_article_page(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        task_name: str = "",
        relation: str = "matched",
        media_type: str = "",
        search: str = "",
    ) -> dict[str, Any]:
        self.initialize()
        where_sql, params = self._article_query_filters(
            task_name=task_name,
            relation=relation,
            media_type=media_type,
            search=search,
        )
        limit = max(1, min(500, int(limit or 100)))
        offset = max(0, int(offset or 0))
        with self._connection() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM articles a {where_sql}",
                params,
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT a.raw_json
                FROM articles a
                {where_sql}
                ORDER BY
                    COALESCE(NULLIF(a.published_at, ''), NULLIF(a.ts, ''), NULLIF(a.imported_at, '')) DESC,
                    a.id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        return {
            "total": int(total or 0),
            "limit": limit,
            "offset": offset,
            "items": [self._json_loads(row[0]) for row in rows],
        }

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
            for raw_record in records or []:
                if not isinstance(raw_record, dict):
                    skipped += 1
                    continue
                record = dict(raw_record)
                record_id = self._history_record_id(normalized_storage_key, record)
                if not record_id:
                    skipped += 1
                    continue
                existed = conn.execute(
                    "SELECT 1 FROM history_records WHERE storage_key = ? AND id = ?",
                    (normalized_storage_key, record_id),
                ).fetchone() is not None
                conn.execute(
                    """
                    INSERT INTO history_records(
                        storage_key, id, task_id, task_name, ts, platform,
                        keyword, brand, rank, success, review_status, mode,
                        execution_source, raw_json, updated_at_ns
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        raw_json = excluded.raw_json,
                        updated_at_ns = excluded.updated_at_ns
                    """,
                    (
                        normalized_storage_key,
                        record_id,
                        self._text(record.get("task_id")),
                        self._text(record.get("task_name")),
                        self._date_text(record.get("ts")),
                        self._text(record.get("platform")),
                        self._text(record.get("keyword")),
                        self._text(record.get("brand")),
                        self._int(record.get("rank"), default=99),
                        1 if bool(record.get("success")) else 0,
                        self._text(record.get("review_status")),
                        self._text(record.get("mode")),
                        self._text(record.get("execution_source")),
                        self._json_dumps(record),
                        updated_at_ns,
                    ),
                )
                if existed:
                    updated += 1
                else:
                    created += 1
        return {"created": created, "updated": updated, "skipped": skipped}

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
                ORDER BY ts ASC, id ASC
                {limit_sql}
                """,
                params,
            ).fetchall()
        return [self._json_loads(row[0]) for row in rows]

    def list_history_storage_keys(self) -> list[str]:
        self.initialize()
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT storage_key FROM history_records ORDER BY storage_key"
            ).fetchall()
        return [str(row[0]) for row in rows if str(row[0] or "")]

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

    def _find_article_id(self, conn: sqlite3.Connection, *, article_id: str, normalized_url: str) -> str | None:
        if normalized_url:
            row = conn.execute(
                "SELECT id FROM articles WHERE normalized_url = ?",
                (normalized_url,),
            ).fetchone()
            if row is not None:
                return str(row[0])
        row = conn.execute("SELECT id FROM articles WHERE id = ?", (article_id,)).fetchone()
        return str(row[0]) if row is not None else None

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
        raw_url = self._text(article.get("url"))
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
