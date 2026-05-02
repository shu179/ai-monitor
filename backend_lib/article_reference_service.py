from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

from backend_lib.config_provider import RuntimeConfigProvider
from core.article_reference_index import (
    build_reference_index_source_signature,
    get_reference_index_file_signature,
    load_reference_index_snapshot,
    save_reference_index_snapshot,
)
from core.article_reference_ranking import (
    ALGORITHM_VERSION,
    MANUAL_REFERENCE_PLATFORM_ID,
    build_article_reference_bucket_snapshot,
    build_article_reference_ranking_from_snapshot,
)
from core.article_store import get_articles_file_signature, resolve_article_source
from core.daily_task_state import derive_task_id
from core.history import get_records, get_records_file_signature, normalize_platform_id
from core.reference_urls import normalize_reference_url
from core.time_utils import local_now

BODY_REFERENCES_HISTORY_SINCE = "2026-05-01"

_PLATFORM_LABELS = {
    "local_model": "本地模型",
    "doubao": "豆包",
    "deepseek": "DeepSeek",
    "ark_deepseek": "方舟 DeepSeek",
    "kimi": "Kimi",
    "tongyi": "通义千问",
    "wenxin": "文心一言",
    "yuanbao": "元宝",
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "gemini": "Gemini",
    "perplexity": "Perplexity",
}


class ArticleReferenceService:
    """Assemble task-scoped article reference ranking API payloads."""

    _MAX_SNAPSHOT_CACHE_ENTRIES = 8

    def __init__(
        self,
        *,
        config_provider: RuntimeConfigProvider,
        synced_articles_loader: Callable[[dict | None], list[dict[str, Any]]],
    ) -> None:
        self._config_provider = config_provider
        self._get_synced_articles = synced_articles_loader
        self._snapshot_cache_lock = threading.RLock()
        self._snapshot_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._snapshot_cache_order: list[tuple[Any, ...]] = []

    def invalidate_cache(self) -> None:
        with self._snapshot_cache_lock:
            self._snapshot_cache.clear()
            self._snapshot_cache_order.clear()

    def get_task_article_reference_ranking(
        self,
        task_id: str,
        *,
        platform: str = "all",
        date_from: str = "",
        date_to: str = "",
    ) -> dict[str, Any]:
        config = self._config_provider.load()
        task, resolved_id = self._resolve_task(config, task_id=task_id)
        if not task or not resolved_id:
            return self._empty_response(ok=False, message="未找到对应品牌任务")

        task_name = str(task.get("name") or resolved_id).strip()
        snapshot_entry = self._get_or_build_snapshot_entry(
            config=config,
            task=task,
            task_name=task_name,
            task_id=resolved_id,
        )
        ranking = build_article_reference_ranking_from_snapshot(
            snapshot_entry.get("snapshot") or {},
            article_urls=set((snapshot_entry.get("article_index") or {}).keys()),
            platform=platform,
            date_from=date_from,
            date_to=date_to,
        )
        available_platforms = self._merge_available_platforms(
            snapshot_entry.get("configured_platforms") or [],
            ranking.get("available_platforms") or [],
        )
        items = self._attach_articles(ranking.get("items") or [], snapshot_entry.get("article_index") or {})
        return {
            "ok": True,
            "algorithm_version": ALGORITHM_VERSION,
            "computed_at": local_now().strftime("%Y-%m-%d %H:%M:%S"),
            "data_coverage": {
                "body_references_since": BODY_REFERENCES_HISTORY_SINCE,
                "answer_text_fallback": True,
                "article_store_reference_events": True,
                "article_store_reference_fallback": True,
            },
            "available_platforms": available_platforms,
            "daily_points": ranking.get("daily_points") or [],
            "items": items,
            "total": len(items),
        }

    def _get_or_build_snapshot_entry(
        self,
        *,
        config: dict[str, Any],
        task: dict[str, Any],
        task_name: str,
        task_id: str,
    ) -> dict[str, Any]:
        index_source_signature = build_reference_index_source_signature(task_name, task_id)
        cache_key = self._snapshot_cache_key(
            config=config,
            task_name=task_name,
            task_id=task_id,
            index_file_signature=get_reference_index_file_signature(task_name, task_id),
        )
        with self._snapshot_cache_lock:
            cached = self._snapshot_cache.get(cache_key)
            if cached:
                self._touch_snapshot_cache_key(cache_key)
                return cached

        task_articles = self._load_task_articles(config, task_name)
        article_index = self._build_article_index(task_articles)
        snapshot = load_reference_index_snapshot(
            task_name,
            task_id,
            source_signature=index_source_signature,
        )
        if snapshot is None:
            history_records = get_records(task_name, task_id=task_id)
            article_reference_records = self._build_article_store_reference_records(
                task=task,
                task_name=task_name,
                task_id=task_id,
                articles=task_articles,
            )
            snapshot = build_article_reference_bucket_snapshot(
                history_records + article_reference_records,
                article_urls=set(article_index.keys()),
            )
            try:
                save_reference_index_snapshot(
                    task_name,
                    task_id,
                    snapshot,
                    source_signature=index_source_signature,
                )
            except Exception:
                pass
        index_file_signature = get_reference_index_file_signature(task_name, task_id)
        entry = {
            "snapshot": snapshot,
            "article_index": article_index,
            "configured_platforms": self._task_configured_platforms(task),
        }
        cache_key = self._snapshot_cache_key(
            config=config,
            task_name=task_name,
            task_id=task_id,
            index_file_signature=index_file_signature,
        )
        with self._snapshot_cache_lock:
            existing = self._snapshot_cache.get(cache_key)
            if existing:
                self._touch_snapshot_cache_key(cache_key)
                return existing
            self._snapshot_cache[cache_key] = entry
            self._touch_snapshot_cache_key(cache_key)
            while len(self._snapshot_cache_order) > self._MAX_SNAPSHOT_CACHE_ENTRIES:
                stale_key = self._snapshot_cache_order.pop(0)
                self._snapshot_cache.pop(stale_key, None)
        return entry

    def _snapshot_cache_key(
        self,
        *,
        config: dict[str, Any],
        task_name: str,
        task_id: str,
        index_file_signature: tuple[str, int, int] | None = None,
    ) -> tuple[Any, ...]:
        return (
            ALGORITHM_VERSION,
            str(task_id or "").strip(),
            str(task_name or "").strip(),
            self._task_match_config_signature(config),
            get_articles_file_signature(),
            get_records_file_signature(task_name, task_id=task_id),
            index_file_signature or get_reference_index_file_signature(task_name, task_id),
        )

    def _touch_snapshot_cache_key(self, cache_key: tuple[Any, ...]) -> None:
        try:
            self._snapshot_cache_order.remove(cache_key)
        except ValueError:
            pass
        self._snapshot_cache_order.append(cache_key)

    def _task_match_config_signature(self, config: dict[str, Any]) -> str:
        try:
            return json.dumps(
                (config or {}).get("tasks", []) or [],
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        except Exception:
            return ""

    def _empty_response(self, *, ok: bool = True, message: str = "") -> dict[str, Any]:
        payload = {
            "ok": ok,
            "algorithm_version": ALGORITHM_VERSION,
            "computed_at": local_now().strftime("%Y-%m-%d %H:%M:%S"),
            "data_coverage": {
                "body_references_since": BODY_REFERENCES_HISTORY_SINCE,
                "answer_text_fallback": True,
                "article_store_reference_events": True,
                "article_store_reference_fallback": True,
            },
            "available_platforms": [],
            "daily_points": [],
            "items": [],
            "total": 0,
        }
        if message:
            payload["message"] = message
        return payload

    def _resolve_task(self, config: dict[str, Any], *, task_id: str = "", task_name: str = "") -> tuple[dict[str, Any] | None, str]:
        normalized_task_id = str(task_id or "").strip()
        normalized_task_name = str(task_name or "").strip()
        for task in (config.get("tasks") or []):
            if not isinstance(task, dict):
                continue
            current_id = str(task.get("task_id") or derive_task_id(task)).strip()
            current_name = str(task.get("name") or current_id).strip()
            if normalized_task_id and current_id == normalized_task_id:
                return task, current_id
            if normalized_task_name and current_name == normalized_task_name:
                return task, current_id
        return None, ""

    def _load_task_articles(self, config: dict[str, Any], task_name: str) -> list[dict[str, Any]]:
        normalized_task_name = str(task_name or "").strip()
        if not normalized_task_name:
            return []
        articles = self._get_synced_articles(config)
        result: list[dict[str, Any]] = []
        for article in articles:
            if not isinstance(article, dict):
                continue
            if self._article_matches_task(article, normalized_task_name):
                result.append(article)
        return result

    def _article_matches_task(self, article: dict[str, Any], task_name: str) -> bool:
        normalized_task_name = str(task_name or "").strip()
        if not normalized_task_name:
            return False

        matched_tasks = {
            str(name or "").strip()
            for name in (article.get("matched_tasks") or [])
            if str(name or "").strip()
        }
        referenced_tasks = {
            str(name or "").strip()
            for name in (article.get("referenced_tasks") or [])
            if str(name or "").strip()
        }
        reference_hits = article.get("reference_hits") if isinstance(article.get("reference_hits"), dict) else {}
        referenced_task_keys = {
            str(name or "").strip()
            for name in reference_hits.keys()
            if str(name or "").strip()
        }
        return normalized_task_name in matched_tasks or normalized_task_name in referenced_tasks or normalized_task_name in referenced_task_keys

    def _build_article_index(self, articles: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        article_index: dict[str, dict[str, Any]] = {}
        for article in articles:
            normalized_url = normalize_reference_url(str(article.get("url") or "").strip())
            if not normalized_url or normalized_url in article_index:
                continue
            article_index[normalized_url] = article
        return article_index

    def _build_article_store_reference_records(
        self,
        *,
        task: dict[str, Any],
        task_name: str,
        task_id: str,
        articles: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        configured_platforms = self._task_configured_platforms(task)
        fallback_platform = configured_platforms[0] if len(configured_platforms) == 1 else MANUAL_REFERENCE_PLATFORM_ID
        fallback_brand = str(task.get("brand") or task_name or task_id).strip() or task_name or task_id
        reference_records: list[dict[str, Any]] = []

        for article in articles:
            normalized_url = normalize_reference_url(str(article.get("url") or "").strip())
            if not normalized_url:
                continue

            hit = self._get_article_reference_hit(article, task_name)
            if not hit:
                continue

            event_records = self._build_reference_event_records(
                article=article,
                normalized_url=normalized_url,
                hit=hit,
                task_name=task_name,
                task_id=task_id,
                fallback_platform=fallback_platform,
                fallback_brand=fallback_brand,
            )
            reference_records.extend(event_records)

            hit_count = max(0, int(hit.get("count") or 0))
            if event_records:
                legacy_count = max(0, hit_count - len(event_records))
            else:
                legacy_count = max(1, hit_count)
            if legacy_count:
                legacy_records = self._build_legacy_reference_records(
                    article=article,
                    normalized_url=normalized_url,
                    hit=hit,
                    task_name=task_name,
                    task_id=task_id,
                    fallback_platform=fallback_platform,
                    fallback_brand=fallback_brand,
                    count=legacy_count,
                )
                reference_records.extend(legacy_records)

        return reference_records

    def _build_reference_event_records(
        self,
        *,
        article: dict[str, Any],
        normalized_url: str,
        hit: dict[str, Any],
        task_name: str,
        task_id: str,
        fallback_platform: str,
        fallback_brand: str,
    ) -> list[dict[str, Any]]:
        raw_events = hit.get("events") if isinstance(hit.get("events"), list) else []
        if not raw_events:
            return []

        article_id = str(article.get("id") or "").strip()
        article_title = str(article.get("title") or "").strip() or normalized_url
        reference_source = str(hit.get("source") or "article_store_reference").strip() or "article_store_reference"
        default_time = str(
            hit.get("last_referenced_at")
            or article.get("last_referenced_at")
            or article.get("imported_at")
            or article.get("ts")
            or local_now().strftime("%Y-%m-%d %H:%M:%S")
        ).strip()

        records: list[dict[str, Any]] = []
        for index, raw_event in enumerate(raw_events):
            if not isinstance(raw_event, dict):
                continue
            platform_id = normalize_platform_id(str(raw_event.get("platform") or "").strip()) or fallback_platform
            referenced_at = str(
                raw_event.get("referenced_at")
                or hit.get("last_referenced_at")
                or default_time
            ).strip()
            event_source = str(raw_event.get("source") or reference_source).strip() or reference_source
            event_id = str(
                raw_event.get("event_id")
                or raw_event.get("record_id")
                or f"article-reference-event:{article_id}:{task_id}:{index}"
            ).strip()
            record = {
                "id": event_id,
                "ts": referenced_at,
                "task_id": task_id,
                "task_name": task_name,
                "platform": platform_id,
                "keyword": article_title,
                "brand": fallback_brand,
                "rank": 1,
                "success": True,
                "mode": "article_reference",
                "execution_source": event_source,
                "extra": {
                    "references": [{"url": normalized_url}],
                    "article_store_reference": True,
                    "reference_event_id": event_id,
                    "reference_source": event_source,
                    "article_id": article_id,
                    "article_url": normalized_url,
                },
            }
            if str(raw_event.get("record_id") or "").strip():
                record["extra"]["record_id"] = str(raw_event.get("record_id") or "").strip()
            records.append(record)
        return records

    def _build_legacy_reference_records(
        self,
        *,
        article: dict[str, Any],
        normalized_url: str,
        hit: dict[str, Any],
        task_name: str,
        task_id: str,
        fallback_platform: str,
        fallback_brand: str,
        count: int,
    ) -> list[dict[str, Any]]:
        legacy_count = max(0, int(count or 0))
        if legacy_count <= 0:
            return []

        referenced_at = str(hit.get("last_referenced_at") or "").strip() or str(
            article.get("last_referenced_at")
            or article.get("imported_at")
            or article.get("ts")
            or local_now().strftime("%Y-%m-%d %H:%M:%S")
        ).strip()
        reference_source = str(hit.get("source") or "article_store_reference").strip() or "article_store_reference"
        article_id = str(article.get("id") or "").strip()
        article_title = str(article.get("title") or "").strip() or normalized_url
        records: list[dict[str, Any]] = []
        for index in range(legacy_count):
            records.append({
                "id": f"article-reference:{article_id}:{task_id}:{index}",
                "ts": referenced_at,
                "task_id": task_id,
                "task_name": task_name,
                "platform": fallback_platform,
                "keyword": article_title,
                "brand": fallback_brand,
                "rank": 1,
                "success": True,
                "mode": "article_reference",
                "execution_source": reference_source,
                "extra": {
                    "references": [{"url": normalized_url}],
                    "manual_reference": True,
                    "reference_source": reference_source,
                    "reference_count": legacy_count,
                    "article_id": article_id,
                    "article_url": normalized_url,
                },
            })
        return records

    def _get_article_reference_hit(self, article: dict[str, Any], task_name: str) -> dict[str, Any] | None:
        normalized_task_name = str(task_name or "").strip()
        if not normalized_task_name:
            return None

        reference_hits = article.get("reference_hits") if isinstance(article.get("reference_hits"), dict) else {}
        hit = reference_hits.get(normalized_task_name)
        if isinstance(hit, dict):
            normalized_hit = dict(hit)
            try:
                normalized_hit["count"] = int(normalized_hit.get("count", 0) or 0)
            except Exception:
                normalized_hit["count"] = 0
            return normalized_hit

        referenced_tasks = [
            str(name or "").strip()
            for name in (article.get("referenced_tasks") or [])
            if str(name or "").strip()
        ]
        if normalized_task_name not in referenced_tasks:
            return None

        return {
            "count": 1,
            "last_referenced_at": str(
                article.get("last_referenced_at")
                or article.get("imported_at")
                or article.get("ts")
                or ""
            ).strip(),
            "source": "article_store_reference",
        }

    def _task_configured_platforms(self, task: dict[str, Any]) -> list[str]:
        platforms: list[str] = []

        def append_platform(value: Any) -> None:
            platform_id = normalize_platform_id(str(value or "").strip())
            if platform_id and platform_id not in platforms:
                platforms.append(platform_id)

        raw_platforms = task.get("platforms")
        if isinstance(raw_platforms, list):
            for platform in raw_platforms:
                append_platform(platform)
        for keyword in task.get("keywords") or []:
            if not isinstance(keyword, dict):
                continue
            keyword_platforms = keyword.get("platforms")
            if isinstance(keyword_platforms, list):
                for platform in keyword_platforms:
                    append_platform(platform)
        return platforms

    def _merge_available_platforms(
        self,
        configured_platforms: list[str],
        ranking_platforms: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        stats_by_id = {
            str(item.get("id") or "").strip(): item
            for item in ranking_platforms
            if str(item.get("id") or "").strip()
        }
        result: list[dict[str, Any]] = []
        seen: set[str] = set()

        def append(platform_id: str, *, configured: bool) -> None:
            normalized = normalize_platform_id(platform_id)
            if not normalized or normalized in seen:
                return
            stats = stats_by_id.get(normalized) or {}
            seen.add(normalized)
            result.append({
                "id": normalized,
                "label": _PLATFORM_LABELS.get(normalized, normalized),
                "configured": configured,
                "raw_count": int(stats.get("raw_count") or 0),
                "event_count": int(stats.get("event_count") or 0),
            })

        for platform_id in configured_platforms:
            append(platform_id, configured=True)
        for item in sorted(
            ranking_platforms,
            key=lambda value: (-int(value.get("event_count") or 0), -int(value.get("raw_count") or 0), str(value.get("id") or "")),
        ):
            append(str(item.get("id") or ""), configured=False)
        return result

    def _attach_articles(
        self,
        items: list[dict[str, Any]],
        article_index: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for raw_item in items:
            item = dict(raw_item)
            url = str(item.get("url") or "").strip()
            article = article_index.get(url)
            if not article:
                continue
            platforms = []
            for platform_item in item.get("platforms") or []:
                if not isinstance(platform_item, dict):
                    continue
                platform_id = normalize_platform_id(str(platform_item.get("id") or "").strip())
                if not platform_id:
                    continue
                platforms.append({
                    "id": platform_id,
                    "label": _PLATFORM_LABELS.get(platform_id, platform_id),
                    "raw_count": int(platform_item.get("raw_count") or 0),
                    "event_count": int(platform_item.get("event_count") or 0),
                })
            media_type = str(article.get("media_type") or "").strip()
            item.pop("url", None)
            item["platforms"] = platforms
            item["article"] = {
                "id": str(article.get("id") or ""),
                "title": str(article.get("title") or "") or url,
                "url": str(article.get("url") or url),
                "source": resolve_article_source(article) or str(article.get("platform") or ""),
                "type": "media" if media_type == "authority" else "self-media",
            }
            result.append(item)

        for index, item in enumerate(result, start=1):
            item["rank"] = index
        return result
