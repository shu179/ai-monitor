"""Article service and import batch state for the local web backend."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import unicodedata
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend_lib.article_import import (
    _extract_article_import_items,
    _normalize_article_import_date,
    _normalize_article_import_media_type,
    _resolve_article_import_platform_label,
    _split_article_import_platform_account,
    _strip_article_import_media_qualifier,
)
from backend_lib.config_provider import RuntimeConfigProvider
from core.app_paths import resolve_app_path
from core.article_store import (
    analyze_article_matches,
    article_export_keyword_cache_fields,
    bulk_upsert_articles,
    compile_article_matcher,
    confirm_article_import_batch,
    extract_domain,
    build_article_export_keyword_plan,
    build_article_export_keyword_signature,
    get_articles_file_path,
    get_articles,
    normalize_article_url,
    resolve_article_display_url,
    resolve_article_export_keywords,
    resolve_article_source,
    resolve_media_name,
    save_domain_media_name,
    undo_article_import_batch,
    update_article_export_keyword_cache,
    update_article,
    update_media_type as update_article_media_type,
)
from core.daily_task_state import derive_task_id
from core.file_lock import CrossProcessRLock
from core.local_account_space import account_scoped_path
from core.time_utils import local_now, local_today, parse_local_date

ARTICLE_IMPORT_EXTENSIONS = {".xlsx", ".xlsm", ".csv"}
MAX_ARTICLE_IMPORT_BYTES = 25 * 1024 * 1024


def _article_published_date(article: dict[str, Any]) -> date | None:
    published_at = article.get("published_at")
    if published_at:
        return parse_local_date(published_at)
    if str(article.get("fetch_method", "") or "").strip() == "manual_table_import":
        return None
    return parse_local_date(article.get("ts"))


def _with_article_export_keyword_cache(
    article: dict[str, Any],
    config: dict[str, Any] | None,
    *,
    keyword_plan: list[dict[str, str]] | None = None,
    config_signature: str = "",
) -> dict[str, Any]:
    cached_article = dict(article or {})
    update_article_export_keyword_cache(
        cached_article,
        config or {},
        keyword_plan=keyword_plan,
        config_signature=config_signature,
    )
    return cached_article


def _article_to_api(
    article: dict[str, Any],
    config: dict[str, Any] | None = None,
    task_name: str = "",
    *,
    include_export_keywords: bool = False,
    export_keyword_plan: list[dict[str, str]] | None = None,
    export_keyword_config_signature: str = "",
) -> dict[str, Any]:
    media_type = str(article.get("media_type", "") or "").strip()
    source = resolve_article_source(article)
    published_at = str(article.get("published_at", "") or "").strip()[:10]
    article_ts = published_at
    if not article_ts and str(article.get("fetch_method", "") or "").strip() != "manual_table_import":
        article_ts = str(article.get("ts", "") or "").strip()[:10]
    imported_at = str(article.get("imported_at") or article.get("created_at") or article.get("ts", "") or "").strip()
    matched_tasks = [
        str(task_name or "").strip()
        for task_name in (article.get("matched_tasks") or [])
        if str(task_name or "").strip()
    ]
    raw_match_reasons = article.get("match_reasons") or {}
    reason_lines: list[str] = []
    if isinstance(raw_match_reasons, dict):
        for task_name in matched_tasks:
            reasons = [
                str(reason or "").strip()
                for reason in (raw_match_reasons.get(task_name) or [])
                if str(reason or "").strip()
            ]
            if reasons:
                reason_lines.append(f"{task_name}：{'；'.join(reasons[:3])}")
    unmatched_reason = str(article.get("unmatched_reason", "") or "").strip()
    classification_status = "matched" if matched_tasks else "unmatched"
    classification_message = (
        f"已归类到 {'、'.join(matched_tasks)}"
        if matched_tasks else
        (unmatched_reason or "未命中品牌名或关键词，暂未归类")
    )
    referenced_tasks = [
        str(task_name or "").strip()
        for task_name in (article.get("referenced_tasks") or [])
        if str(task_name or "").strip()
    ]
    reference_hits = article.get("reference_hits") if isinstance(article.get("reference_hits"), dict) else {}
    referenced_at_values = []
    for task_name in referenced_tasks:
        hit = reference_hits.get(task_name) if isinstance(reference_hits, dict) else None
        if isinstance(hit, dict):
            referenced_at = str(hit.get("last_referenced_at", "") or "").strip()
            if referenced_at:
                referenced_at_values.append(referenced_at)
    last_referenced_at = max(referenced_at_values) if referenced_at_values else ""
    payload = {
        "id": article.get("id", ""),
        "source": source or article.get("title", "文章"),
        "title": article.get("title", ""),
        "type": "media" if media_type == "authority" else "self-media",
        "category": "媒体" if media_type == "authority" else "自媒体",
        "url": resolve_article_display_url(article),
        "ts": article_ts,
        "imported_at": imported_at,
        "fetch_method": article.get("fetch_method", ""),
        "platform": article.get("platform", ""),
        "media_name": source,
        "account_id": article.get("account_id", ""),
        "account_name": article.get("account_name", ""),
        "account_url": article.get("account_url", ""),
        "account_platform": article.get("account_platform", ""),
        "account_platform_label": article.get("account_platform_label", ""),
        "published_at": published_at,
        "matchedTasks": matched_tasks,
        "reasonLines": reason_lines,
        "classificationStatus": classification_status,
        "classificationMessage": classification_message,
        "unmatchedReason": unmatched_reason,
        "referenced": bool(referenced_tasks),
        "referencedTasks": referenced_tasks,
        "lastReferencedAt": last_referenced_at,
    }
    if article.get("price") is not None:
        payload["price"] = article.get("price")
    if include_export_keywords:
        payload["exportKeywords"] = resolve_article_export_keywords(
            article,
            config or {},
            task_name,
            keyword_plan=export_keyword_plan,
            keyword_config_signature=export_keyword_config_signature,
        )
    return payload


def _merge_unique_texts(left: Any, right: Any) -> list[str]:
    result: list[str] = []
    for values in (left, right):
        if not isinstance(values, list):
            continue
        for value in values:
            text = str(value or "").strip()
            if text and text not in result:
                result.append(text)
    return result


def _configured_article_task_names(config: dict[str, Any] | None) -> set[str]:
    names: set[str] = set()
    for task in (config or {}).get("tasks", []) or []:
        if not isinstance(task, dict):
            continue
        task_name = str(task.get("name") or derive_task_id(task)).strip()
        if task_name:
            names.add(task_name)
    return names


def _merge_preserved_historical_task_matches(
    analyzed: dict[str, object],
    current_article: dict[str, Any],
    config: dict[str, Any] | None,
) -> dict[str, object]:
    configured_task_names = _configured_article_task_names(config)
    excluded_task_names = {
        str(name or "").strip()
        for name in (current_article.get("excluded_tasks") or [])
        if str(name or "").strip()
    }
    preserved_tasks = [
        str(name or "").strip()
        for name in (current_article.get("matched_tasks") or [])
        if str(name or "").strip()
        and str(name or "").strip() not in configured_task_names
        and str(name or "").strip() not in excluded_task_names
    ]
    if not preserved_tasks:
        return analyzed

    matched_tasks = _merge_unique_texts(preserved_tasks, analyzed.get("matched_tasks"))
    existing_reasons = current_article.get("match_reasons") if isinstance(current_article.get("match_reasons"), dict) else {}
    analyzed_reasons = analyzed.get("match_reasons") if isinstance(analyzed.get("match_reasons"), dict) else {}
    match_reasons = dict(analyzed_reasons)
    for task_name in preserved_tasks:
        reasons = [
            str(reason or "").strip()
            for reason in (existing_reasons.get(task_name) or [])
            if str(reason or "").strip()
        ]
        match_reasons[task_name] = reasons or ["保留历史归类"]

    merged = dict(analyzed)
    merged["matched_tasks"] = matched_tasks
    merged["match_reasons"] = match_reasons
    merged["unmatched_reason"] = "" if matched_tasks else str(analyzed.get("unmatched_reason", "") or "")
    return merged


def _merge_article_for_duplicate_url(base: dict[str, Any], duplicate: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key in ("matched_tasks", "referenced_tasks", "cloud_task_ids"):
        merged[key] = _merge_unique_texts(merged.get(key), duplicate.get(key))

    base_reasons = merged.get("match_reasons") if isinstance(merged.get("match_reasons"), dict) else {}
    duplicate_reasons = duplicate.get("match_reasons") if isinstance(duplicate.get("match_reasons"), dict) else {}
    if base_reasons or duplicate_reasons:
        next_reasons: dict[str, list[str]] = {}
        for task_name in set(base_reasons.keys()) | set(duplicate_reasons.keys()):
            next_reasons[str(task_name)] = _merge_unique_texts(
                base_reasons.get(task_name),
                duplicate_reasons.get(task_name),
            )
        merged["match_reasons"] = next_reasons

    base_hits = merged.get("reference_hits") if isinstance(merged.get("reference_hits"), dict) else {}
    duplicate_hits = duplicate.get("reference_hits") if isinstance(duplicate.get("reference_hits"), dict) else {}
    if duplicate_hits:
        merged["reference_hits"] = {**base_hits, **duplicate_hits}

    for key in ("url", "raw_url", "title", "media_name", "platform", "published_at", "ts"):
        if not str(merged.get(key) or "").strip() and str(duplicate.get(key) or "").strip():
            merged[key] = duplicate.get(key)
    return merged


def _article_url_fingerprint(article: dict[str, Any]) -> str:
    title = re.sub(r"\s+", " ", str(article.get("title") or "").strip()).lower()
    source = re.sub(
        r"\s+",
        " ",
        str(article.get("media_name") or article.get("source") or article.get("platform") or "").strip(),
    ).lower()
    published = str(article.get("published_at") or article.get("published") or article.get("ts") or "").strip()[:10]
    if not title or not source:
        return ""
    return "|".join([title, source, published])


def _normalize_article_import_title(value: Any) -> str:
    raw = str(value or "")
    if not raw.strip():
        return ""
    folded = unicodedata.normalize("NFKC", raw)
    return re.sub(r"\s+", " ", folded.strip()).lower()


_ARTICLE_IMPORT_MEDIA_CANONICAL_OVERRIDES = {
    # 用户表格里同一篇腾讯系账号文章可能写成 "腾讯网（无线昆明）" 或 "无线昆明（腾讯新闻）"，
    # 经过 _split_article_import_platform_account 后 platform 部分会落在 "腾讯网" 或 "腾讯新闻"，
    # 这里统一折叠到 "腾讯新闻"，让两种写法落入同一个 match key、不再被当成两条独立文章。
    "腾讯网": "腾讯新闻",
}


def _canonicalize_article_import_media_name(value: Any) -> str:
    raw = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not raw:
        return ""
    stripped = _strip_article_import_media_qualifier(raw)
    bracket_match = re.fullmatch(r"(.+?)[（(【\[]([^（）()【】\[\]]+)[）)】\]]", stripped)
    if bracket_match:
        outer = bracket_match.group(1).strip()
        inner = bracket_match.group(2).strip()
        inner_platform = _resolve_article_import_platform_label(inner)
        outer_platform = _resolve_article_import_platform_label(outer)
        if inner_platform:
            stripped = inner_platform
        elif outer_platform:
            stripped = outer_platform
        else:
            stripped = outer
    label = _resolve_article_import_platform_label(stripped)
    if label:
        stripped = label
    stripped = _ARTICLE_IMPORT_MEDIA_CANONICAL_OVERRIDES.get(stripped, stripped)
    try:
        resolved = resolve_media_name(stripped) or stripped
    except Exception:
        resolved = stripped
    resolved = _ARTICLE_IMPORT_MEDIA_CANONICAL_OVERRIDES.get(resolved, resolved)
    return resolved.lower()


def _article_import_match_key(article: dict[str, Any]) -> tuple[str, str, str] | None:
    """Cross-form fingerprint: 同一篇文章无论写成"腾讯网（无线昆明）"还是"无线昆明（腾讯新闻）"都落到同一 key。"""
    title = _normalize_article_import_title(article.get("title"))
    if not title:
        return None
    media_canon = _canonicalize_article_import_media_name(
        article.get("media_name") or article.get("source") or article.get("platform") or ""
    )
    date_text = str(
        article.get("published_at") or article.get("published") or article.get("ts") or ""
    ).strip()[:10]
    return (title, media_canon, date_text)


def _article_import_loose_key(article: dict[str, Any]) -> tuple[str, str] | None:
    """Fallback key: only title + date. Used to attach a URL-less new row to an existing record
    when the media name is written differently (e.g., 腾讯网 vs 腾讯新闻 而又没有 URL 可以对齐)。"""
    title = _normalize_article_import_title(article.get("title"))
    if not title:
        return None
    date_text = str(
        article.get("published_at") or article.get("published") or article.get("ts") or ""
    ).strip()[:10]
    if not date_text:
        return None
    return (title, date_text)


_ARTICLE_IMPORT_URL_DIRTY_MARKERS = ("\x1eHYPERLINK:", "\x1e")


def _looks_like_dirty_url(value: Any) -> bool:
    """旧版本代码偶尔会把 cell_text 里的 HYPERLINK 标记直接塞进 url 字段，
    或者写入了一段不像 URL 的脏文本——这种 URL 实际上点不开，应该当作"没 URL"处理。"""
    text = str(value or "").strip()
    if not text:
        return True
    if any(marker in text for marker in _ARTICLE_IMPORT_URL_DIRTY_MARKERS):
        return True
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text):
        return True
    return False


def _merge_article_import_fields(base: dict[str, Any], incoming: dict[str, Any]) -> bool:
    """Fill empty fields on ``base`` from ``incoming`` (existing data 不会被覆盖). Returns True if anything changed."""
    changed = False
    _fill_keys = (
        "url",
        "raw_url",
        "title",
        "platform",
        "media_name",
        "media_type",
        "account_name",
        "excerpt",
        "published_at",
        "ts",
        "price",
        "imported_from_sheet",
        "imported_from_row",
    )
    for key in _fill_keys:
        base_value = base.get(key)
        if key in ("url", "raw_url"):
            has_base = not _looks_like_dirty_url(base_value)
        elif isinstance(base_value, str):
            has_base = bool(base_value.strip())
        else:
            has_base = base_value not in (None, "", 0)
        if has_base:
            continue
        incoming_value = incoming.get(key)
        if key in ("url", "raw_url"):
            has_incoming = not _looks_like_dirty_url(incoming_value)
        elif isinstance(incoming_value, str):
            has_incoming = bool(incoming_value.strip())
        else:
            has_incoming = incoming_value not in (None, "")
        if has_incoming:
            base[key] = incoming_value
            changed = True
    return changed


def _pick_best_existing_anchor(
    articles: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Given multiple existing articles that share a strong fingerprint (URL 或 canonical key),
    return ``(anchor, duplicates)`` — anchor is the one we keep (has URL/most fields filled),
    其它就是要合并并删除的孤儿重复。"""
    if len(articles) <= 1:
        return articles[0] if articles else None, []  # type: ignore[return-value]

    def _score(article: dict[str, Any]) -> tuple[int, int, int, int, str]:
        raw_stored = str(article.get("url") or "").strip()
        # 把脏 URL（如 \x1eHYPERLINK: marker、半截字符串）当作"没有 URL"，
        # 这样脏数据的副本会被排到后面，更新发生在干净的那条上。
        url_ok = bool(raw_stored) and not _looks_like_dirty_url(raw_stored)
        normalized = normalize_article_url(raw_stored) if url_ok else ""
        raw_url_field = str(article.get("raw_url") or "").strip()
        raw_url_ok = bool(raw_url_field) and not _looks_like_dirty_url(raw_url_field)
        title_len = len(str(article.get("title") or ""))
        excerpt_len = len(str(article.get("excerpt") or ""))
        # 越好排越前；用 article id 作为最后的 deterministic tie-breaker。
        return (
            1 if normalized else 0,
            1 if raw_url_ok else 0,
            title_len,
            excerpt_len,
            str(article.get("id") or ""),
        )

    sorted_articles = sorted(articles, key=_score, reverse=True)
    return sorted_articles[0], sorted_articles[1:]


def _hydrate_article_urls_from_fingerprints(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    url_by_fingerprint: dict[str, str] = {}
    for article in articles:
        if not isinstance(article, dict):
            continue
        display_url = resolve_article_display_url(article)
        normalized_url = normalize_article_url(display_url or str(article.get("url") or ""))
        fingerprint = _article_url_fingerprint(article)
        if normalized_url and fingerprint and fingerprint not in url_by_fingerprint:
            url_by_fingerprint[fingerprint] = display_url or str(article.get("url") or "").strip()
    if not url_by_fingerprint:
        return list(articles)
    hydrated: list[dict[str, Any]] = []
    for article in articles:
        if not isinstance(article, dict):
            continue
        item = dict(article)
        if not normalize_article_url(str(item.get("url") or "")):
            fallback_url = url_by_fingerprint.get(_article_url_fingerprint(item), "")
            if fallback_url:
                item["url"] = fallback_url
        hydrated.append(item)
    return hydrated


def _dedupe_articles_by_url(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    index_by_url: dict[str, int] = {}
    for article in _hydrate_article_urls_from_fingerprints(articles):
        if not isinstance(article, dict):
            continue
        item = dict(article)
        normalized_url = normalize_article_url(str(item.get("url") or ""))
        if not normalized_url:
            deduped.append(item)
            continue
        existing_index = index_by_url.get(normalized_url)
        if existing_index is None:
            index_by_url[normalized_url] = len(deduped)
            deduped.append(item)
            continue
        deduped[existing_index] = _merge_article_for_duplicate_url(deduped[existing_index], item)
    return deduped


def _article_import_batches_path() -> Path:
    return account_scoped_path(
        "logs/article_import_batches.json",
        fallback=resolve_app_path("logs/article_import_batches.json"),
    )


_ARTICLE_IMPORT_BATCHES_LOCK = CrossProcessRLock(lambda: _article_import_batches_lock_file())


def _article_import_batches_lock_file() -> Path:
    path = _article_import_batches_path()
    return path.with_name(f".{path.name}.lock")


def _normalize_article_import_batch(batch: Any) -> dict[str, Any] | None:
    if not isinstance(batch, dict):
        return None
    import_id = str(batch.get("id") or batch.get("import_id") or "").strip()
    if not import_id:
        return None
    status = str(batch.get("status") or "pending").strip() or "pending"
    article_ids = [
        str(article_id or "").strip()
        for article_id in (batch.get("article_ids") or [])
        if str(article_id or "").strip()
    ]
    updated_articles = []
    for item in batch.get("updated_articles") or []:
        if not isinstance(item, dict):
            continue
        article_id = str(item.get("id") or item.get("article_id") or "").strip()
        before = item.get("before") if isinstance(item.get("before"), dict) else None
        if article_id and before:
            updated_articles.append({"id": article_id, "before": before})
    merged_duplicate_articles = []
    for item in batch.get("merged_duplicate_articles") or []:
        if not isinstance(item, dict):
            continue
        article_id = str(item.get("id") or item.get("article_id") or "").strip()
        before = item.get("before") if isinstance(item.get("before"), dict) else None
        if article_id and before:
            merged_duplicate_articles.append({"id": article_id, "before": before})
    return {
        "id": import_id,
        "file_name": str(batch.get("file_name") or "").strip(),
        "article_ids": article_ids,
        "updated_articles": updated_articles,
        "merged_duplicate_articles": merged_duplicate_articles,
        "status": status,
        "created_at": str(batch.get("created_at") or "").strip(),
        "added_count": int(batch.get("added_count") or len(article_ids)),
        "updated_count": int(batch.get("updated_count") or len(updated_articles)),
        "duplicate_count": int(batch.get("duplicate_count") or 0),
        "merged_duplicate_count": int(
            batch.get("merged_duplicate_count") or len(merged_duplicate_articles)
        ),
        "skipped_count": int(batch.get("skipped_count") or 0),
    }


def _load_article_import_batches_file() -> dict[str, dict[str, Any]]:
    with _ARTICLE_IMPORT_BATCHES_LOCK:
        path = _article_import_batches_path()
        try:
            if not path.exists():
                return {}
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return {}

        raw_batches = data.get("batches") if isinstance(data, dict) else data
        if isinstance(raw_batches, dict):
            iterable = raw_batches.values()
        elif isinstance(raw_batches, list):
            iterable = raw_batches
        else:
            iterable = []
        batches: dict[str, dict[str, Any]] = {}
        for item in iterable:
            batch = _normalize_article_import_batch(item)
            if batch and batch.get("status") == "pending":
                batches[str(batch["id"])] = batch
        return batches


def _save_article_import_batches_file(
    batches: dict[str, dict[str, Any]],
    *,
    merge_existing: bool = False,
) -> None:
    with _ARTICLE_IMPORT_BATCHES_LOCK:
        path = _article_import_batches_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        pending_batches = {
            str(batch.get("id") or import_id): batch
            for import_id, batch in batches.items()
            if isinstance(batch, dict)
            and str(batch.get("id") or import_id).strip()
            and str(batch.get("status") or "pending") == "pending"
        }
        if merge_existing:
            existing = _load_article_import_batches_file()
            existing.update(pending_batches)
            pending_batches = existing
        payload = {
            "updated_at": local_now().isoformat(timespec="seconds"),
            "batches": list(pending_batches.values()),
        }
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


class ArticleImportBatchStore:
    """In-memory cache plus disk persistence for pending article imports."""

    def __init__(self, batches: dict[str, dict[str, Any]] | None = None) -> None:
        self._batches: dict[str, dict[str, Any]] = batches if batches is not None else {}
        self._loaded = batches is not None

    def get_batches(self) -> dict[str, dict[str, Any]]:
        if not self._loaded:
            existing_ids = set(self._batches)
            persisted = _load_article_import_batches_file()
            self._batches.update({
                import_id: batch
                for import_id, batch in persisted.items()
                if import_id not in existing_ids
            })
            self._loaded = True
        return self._batches

    def save(self, *, merge_existing: bool = False) -> None:
        _save_article_import_batches_file(self.get_batches(), merge_existing=merge_existing)

    def reset(self) -> None:
        self._batches = {}
        self._loaded = False


def _runtime_article_import_batches(runtime: Any) -> dict[str, dict[str, Any]]:
    if hasattr(runtime, "article_import_batch_store"):
        store = getattr(runtime, "article_import_batch_store")
        if isinstance(store, ArticleImportBatchStore):
            return store.get_batches()
    if not hasattr(runtime, "_article_import_batches"):
        runtime._article_import_batches = {}
    if not getattr(runtime, "_article_import_batches_loaded", False):
        persisted = _load_article_import_batches_file()
        runtime._article_import_batches.update({
            import_id: batch
            for import_id, batch in persisted.items()
            if import_id not in runtime._article_import_batches
        })
        runtime._article_import_batches_loaded = True
    return runtime._article_import_batches


def _article_import_batch_to_api(batch: dict[str, Any]) -> dict[str, Any]:
    return {
        "import_id": str(batch.get("id") or "").strip(),
        "file_name": str(batch.get("file_name") or "").strip(),
        "created_at": str(batch.get("created_at") or "").strip(),
        "added_count": int(batch.get("added_count") or 0),
        "updated_count": int(batch.get("updated_count") or 0),
        "duplicate_count": int(batch.get("duplicate_count") or 0),
        "skipped_count": int(batch.get("skipped_count") or 0),
        "message": (
            f"已导入 {int(batch.get('added_count') or 0)} 篇"
            f"{f'，更新 {int(batch.get('updated_count') or 0)} 篇' if int(batch.get('updated_count') or 0) else ''}"
            "，等待确认"
        ),
    }


def _compact_article_import_match_text(value: Any) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or "").strip().lower())


def _article_import_task_terms(task: dict[str, Any]) -> list[str]:
    terms: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        key = _compact_article_import_match_text(text)
        if len(key) >= 2 and key not in {"品牌", "客户", "公司", "企业", "项目", "任务"} and text not in terms:
            terms.append(text)

    for key in ("name", "brand", "brand_name", "client", "company"):
        add(task.get(key))
    for key in ("brands", "aliases", "brand_aliases"):
        values = task.get(key)
        if isinstance(values, list):
            for value in values:
                add(value)
    for keyword in task.get("keywords") or []:
        if isinstance(keyword, dict):
            add(keyword.get("brand"))
            add(keyword.get("keyword"))
        else:
            add(keyword)
    return terms


def _build_article_import_match_plan(config: dict[str, Any]) -> list[tuple[str, list[tuple[str, str]]]]:
    plan: list[tuple[str, list[tuple[str, str]]]] = []
    for task in config.get("tasks", []) or []:
        if not isinstance(task, dict):
            continue
        task_name = str(task.get("name") or "").strip()
        if not task_name:
            continue
        terms: list[tuple[str, str]] = []
        for term in _article_import_task_terms(task):
            term_key = _compact_article_import_match_text(term)
            if len(term_key) >= 2:
                terms.append((term, term_key))
        if terms:
            plan.append((task_name, terms))
    return plan


def _merge_article_import_matches(
    analyzed: dict[str, object],
    config: dict[str, Any],
    raw_item: dict[str, Any],
    file_name: str,
    *,
    import_match_plan: list[tuple[str, list[tuple[str, str]]]] | None = None,
) -> dict[str, object]:
    matched_tasks = [
        str(task_name or "").strip()
        for task_name in (analyzed.get("matched_tasks") or [])
        if str(task_name or "").strip()
    ]
    match_reasons = dict(analyzed.get("match_reasons") or {})
    source_texts = [
        ("表格任务列", raw_item.get("task_name")),
        ("表格品牌列", raw_item.get("brand_name")),
        ("导入文件名", Path(str(file_name or "")).stem),
    ]
    source_keys = [
        (source_label, _compact_article_import_match_text(source_value))
        for source_label, source_value in source_texts
    ]
    plan = import_match_plan if import_match_plan is not None else _build_article_import_match_plan(config)

    for task_name, task_terms in plan:
        for source_label, source_key in source_keys:
            if len(source_key) < 2:
                continue
            for term, term_key in task_terms:
                if term_key not in source_key and source_key not in term_key:
                    continue
                if task_name not in matched_tasks:
                    matched_tasks.append(task_name)
                reasons = [
                    str(reason or "").strip()
                    for reason in (match_reasons.get(task_name) or [])
                    if str(reason or "").strip()
                ]
                reason = f"{source_label}指向品牌“{term}”"
                if reason not in reasons:
                    reasons.append(reason)
                match_reasons[task_name] = reasons[:3]
                break
            if task_name in matched_tasks:
                break

    if matched_tasks:
        analyzed["matched_tasks"] = matched_tasks
        analyzed["match_reasons"] = match_reasons
        analyzed["unmatched_reason"] = ""
    return analyzed


class ArticleService:
    """Small service for article list/import/update/delete operations."""

    def __init__(
        self,
        *,
        config_provider: RuntimeConfigProvider,
        synced_articles_loader: Callable[[dict | None], list[dict[str, Any]]],
        invalidate_article_cache: Callable[[], None],
        lock: Any,
        import_batch_store: ArticleImportBatchStore,
        sqlite_article_page_loader: Callable[..., dict[str, Any] | None] | None = None,
        sqlite_article_compare_recorder: Callable[..., None] | None = None,
    ) -> None:
        self._config_provider = config_provider
        self._get_synced_articles = synced_articles_loader
        self._get_sqlite_article_page = sqlite_article_page_loader
        self._record_sqlite_article_compare = sqlite_article_compare_recorder
        self._invalidate_article_cache_callback = invalidate_article_cache
        self._lock = lock
        self._import_batch_store = import_batch_store

    def _invalidate_article_cache(self) -> None:
        self._invalidate_article_cache_callback()

    def _resolve_article_task_names(
        self,
        config: dict[str, Any],
        current_article: dict[str, Any],
        payload: dict[str, Any],
    ) -> tuple[list[str], str]:
        raw_values: list[Any] | None = None
        for key in ("matched_tasks", "matchedTasks", "task_names", "taskNames"):
            if key not in payload:
                continue
            raw = payload.get(key)
            if isinstance(raw, list):
                raw_values = raw
            elif isinstance(raw, str):
                raw_values = [item for item in re.split(r"[、,，/|]+", raw)]
            else:
                raw_values = []
            break
        if raw_values is None:
            for key in ("task_name", "taskName", "brand", "brand_name", "brandName"):
                if key in payload:
                    raw_values = [payload.get(key)]
                    break
        if raw_values is None:
            return [], ""

        task_lookup: dict[str, str] = {}
        for task in config.get("tasks", []) or []:
            task_id = str(task.get("task_id") or derive_task_id(task)).strip()
            task_name = str(task.get("name") or task_id).strip()
            if not task_name:
                continue
            brand_name = str(task.get("brand") or task_name).strip() or task_name
            candidates = [task_name, brand_name, task_id]
            for candidate in candidates:
                normalized = str(candidate or "").strip().lower()
                if normalized and normalized not in task_lookup:
                    task_lookup[normalized] = task_name

        existing_task_names = {
            str(name or "").strip()
            for name in (current_article.get("matched_tasks") or [])
            if str(name or "").strip()
        }
        resolved: list[str] = []
        for raw_value in raw_values:
            text = str(raw_value or "").strip()
            if not text:
                continue
            matched_task = task_lookup.get(text.lower())
            if not matched_task and text in existing_task_names:
                matched_task = text
            if not matched_task:
                return [], f"未找到品牌“{text}”，请先在品牌页创建或从候选项中选择"
            if matched_task not in resolved:
                resolved.append(matched_task)
        return resolved, ""

    def get_articles_filtered(
        self,
        media_type: str = "",
        limit: int = 50,
        task_name: str = "",
        include_export_keywords: bool = False,
    ) -> dict:
        light_config_loader = getattr(self._config_provider, "load_without_hooks", None)
        config = (
            light_config_loader()
            if callable(light_config_loader) else
            self._config_provider.load()
        )
        export_keyword_plan = (
            build_article_export_keyword_plan(config, task_name)
            if include_export_keywords else
            None
        )
        export_keyword_config_signature = (
            build_article_export_keyword_signature(config)
            if include_export_keywords else
            ""
        )
        sqlite_page = self._load_sqlite_article_page(
            config,
            media_type=media_type,
            limit=limit,
            task_name=task_name,
        )
        if sqlite_page is not None and not bool(sqlite_page.get("compare_only")):
            return {
                "articles": [
                    _article_to_api(
                        article,
                        config,
                        task_name,
                        include_export_keywords=include_export_keywords,
                        export_keyword_plan=export_keyword_plan,
                        export_keyword_config_signature=export_keyword_config_signature,
                    )
                    for article in sqlite_page["articles"]
                ],
                "total": int(sqlite_page.get("total") or 0),
                "today_total": int(sqlite_page.get("today_total") or 0),
            }

        json_result = self._load_json_article_page(
            config,
            media_type=media_type,
            limit=limit,
            task_name=task_name,
            include_export_keywords=include_export_keywords,
            export_keyword_plan=export_keyword_plan,
            export_keyword_config_signature=export_keyword_config_signature,
        )
        if sqlite_page is not None and bool(sqlite_page.get("compare_only")):
            self._compare_sqlite_article_page(
                sqlite_page,
                json_result,
                media_type=media_type,
                limit=limit,
                task_name=task_name,
            )
        return json_result

    def _load_json_article_page(
        self,
        config: dict[str, Any],
        *,
        media_type: str,
        limit: int,
        task_name: str,
        include_export_keywords: bool,
        export_keyword_plan: list[dict[str, str]] | None,
        export_keyword_config_signature: str,
    ) -> dict:
        articles = self._get_synced_articles(config)
        articles = _hydrate_article_urls_from_fingerprints(articles)
        if task_name:
            articles = [article for article in articles if task_name in (article.get("matched_tasks") or [])]
        if media_type:
            type_map = {"media": "authority", "self-media": "selfmedia"}
            target = type_map.get(media_type, media_type)
            articles = [article for article in articles if article.get("media_type") == target]
        articles = _dedupe_articles_by_url(articles)
        total = len(articles)
        today = local_today()
        today_total = sum(1 for article in articles if _article_published_date(article) == today)
        articles = articles[:limit]
        return {
            "articles": [
                _article_to_api(
                    article,
                    config,
                    task_name,
                    include_export_keywords=include_export_keywords,
                    export_keyword_plan=export_keyword_plan,
                    export_keyword_config_signature=export_keyword_config_signature,
                )
                for article in articles
            ],
            "total": total,
            "today_total": today_total,
        }

    def _compare_sqlite_article_page(
        self,
        sqlite_page: dict[str, Any],
        json_result: dict[str, Any],
        *,
        media_type: str,
        limit: int,
        task_name: str,
    ) -> None:
        recorder = self._record_sqlite_article_compare
        if not callable(recorder):
            return
        try:
            recorder(
                query={
                    "media_type": str(media_type or ""),
                    "limit": int(limit or 0),
                    "task_name": str(task_name or ""),
                },
                sqlite_page=sqlite_page,
                json_result=json_result,
            )
        except Exception as exc:
            print(f"[ArticleService] SQLite 文章页影子比对记录失败: {exc}")

    def _load_sqlite_article_page(
        self,
        config: dict[str, Any],
        *,
        media_type: str,
        limit: int,
        task_name: str,
    ) -> dict[str, Any] | None:
        if not callable(self._get_sqlite_article_page):
            return None
        try:
            page = self._get_sqlite_article_page(
                config,
                media_type=media_type,
                limit=limit,
                task_name=task_name,
            )
        except Exception as exc:
            print(f"[ArticleService] SQLite 文章页读取失败，回退 JSON: {exc}")
            return None
        if not isinstance(page, dict):
            return None
        raw_articles = page.get("articles")
        if not isinstance(raw_articles, list):
            return None
        articles = [item for item in raw_articles if isinstance(item, dict)]
        return {
            "articles": articles,
            "total": int(page.get("total") or len(articles)),
            "today_total": int(page.get("today_total") or 0),
            "compare_only": bool(page.get("compare_only")),
        }

    def import_article(self, payload: dict) -> dict:
        raw_url = str(payload.get("url", "") or "").strip()
        url = raw_url
        if not url:
            return {"ok": False, "message": "URL 不能为空"}
        try:
            from core.article_fetcher import extract_article_input_url, fetch_article_info
            from core.article_store import add_article, find_article_by_url, normalize_article_url

            extracted = extract_article_input_url(url)
            if extracted:
                url = extracted
            else:
                return {"ok": False, "message": "未识别到有效链接，请粘贴完整文章链接或分享文本"}
            config = self._config_provider.load()
            normalized_url = normalize_article_url(url)
            existing_article = find_article_by_url(normalized_url or url)
            if existing_article:
                info = fetch_article_info(url, config)
                refreshed_article = {
                    **existing_article,
                    "url": normalized_url or url,
                    "title": info.get("title", "") or existing_article.get("title", ""),
                    "platform": info.get("platform", "") or existing_article.get("platform", ""),
                    "media_name": info.get("media_name", "") or existing_article.get("media_name", ""),
                    "media_type": info.get("media_type", existing_article.get("media_type", "selfmedia")),
                    "excerpt": info.get("excerpt", "") or existing_article.get("excerpt", ""),
                    "published_at": (
                        info.get("published_at", "")
                        or existing_article.get("published_at", "")
                        or str(existing_article.get("ts", "") or "")[:10]
                    ),
                    "imported_at": (
                        existing_article.get("imported_at", "")
                        or str(existing_article.get("ts", "") or "").strip()
                        or local_now().strftime("%Y-%m-%d %H:%M")
                    ),
                    "ts": (
                        info.get("published_at", "")
                        or existing_article.get("published_at", "")
                        or str(existing_article.get("ts", "") or "")[:10]
                    ),
                    "fetch_method": info.get("fetch_method", existing_article.get("fetch_method", "html")),
                }
                analyzed = analyze_article_matches(refreshed_article["title"], config, article=refreshed_article)
                analyzed = _merge_preserved_historical_task_matches(analyzed, existing_article, config)
                refreshed_article["matched_tasks"] = analyzed.get("matched_tasks") or []
                refreshed_article["match_reasons"] = analyzed.get("match_reasons") or {}
                refreshed_article["unmatched_reason"] = analyzed.get("unmatched_reason", "") or ""
                refreshed_article = _with_article_export_keyword_cache(refreshed_article, config)
                updated_article = update_article(existing_article.get("id", ""), refreshed_article) or refreshed_article
                self._invalidate_article_cache()
                print(
                    "[WebBackend] 文章重复录入，已刷新归类",
                    {
                        "raw_url": raw_url,
                        "url": normalized_url or url,
                        "title": updated_article.get("title", ""),
                        "matched_tasks": updated_article.get("matched_tasks", []),
                        "articles_path": str(get_articles_file_path()),
                    },
                )
                return {
                    "ok": False,
                    "duplicate": True,
                    "message": "链接已录入",
                    "article": _article_to_api(updated_article),
                }
            info = fetch_article_info(url, config)
            draft_article = {
                "url": normalized_url or url,
                "title": info.get("title", ""),
                "platform": info.get("platform", ""),
                "media_name": info.get("media_name", ""),
                "media_type": info.get("media_type", "selfmedia"),
                "excerpt": info.get("excerpt", ""),
                "published_at": info.get("published_at", ""),
                "imported_at": local_now().strftime("%Y-%m-%d %H:%M"),
                "ts": info.get("published_at", "") or local_now().strftime("%Y-%m-%d"),
                "fetch_method": info.get("fetch_method", "html"),
            }
            analyzed = analyze_article_matches(draft_article["title"], config, article=draft_article)
            article = add_article(_with_article_export_keyword_cache({
                **draft_article,
                "matched_tasks": analyzed.get("matched_tasks") or [],
                "match_reasons": analyzed.get("match_reasons") or {},
                "unmatched_reason": analyzed.get("unmatched_reason", "") or "",
            }, config))
            self._invalidate_article_cache()
            print(
                "[WebBackend] 文章录入完成",
                {
                    "raw_url": raw_url,
                    "url": normalized_url or url,
                    "title": article.get("title", ""),
                    "matched_tasks": article.get("matched_tasks", []),
                    "articles_path": str(get_articles_file_path()),
                },
            )
            return {"ok": True, "article": _article_to_api(article)}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def import_articles_from_file(self, file_name: str, data: bytes) -> dict:
        original_name = Path(str(file_name or "articles.xlsx")).name
        suffix = Path(original_name).suffix.lower()
        if suffix not in ARTICLE_IMPORT_EXTENSIONS:
            return {"ok": False, "message": "目前支持导入 .xlsx/.xlsm/.csv 表格"}
        if not data:
            return {"ok": False, "message": "上传文件为空"}
        if len(data) > MAX_ARTICLE_IMPORT_BYTES:
            return {"ok": False, "message": "上传文件超过 25MB"}

        try:
            from core.article_store import (
                classify_article_media_type,
                resolve_media_name,
            )

            raw_items, details = _extract_article_import_items(original_name, data)
            if not raw_items:
                return {
                    "ok": False,
                    "message": "没有识别到可导入文章，请确认表格包含“标题”列，可选包含“链接 / 媒体 / 发布时间”列",
                    "details": details,
                }

            light_config_loader = getattr(self._config_provider, "load_without_hooks", None)
            config = (
                light_config_loader()
                if callable(light_config_loader) else
                self._config_provider.load()
            )
            compiled_matcher = compile_article_matcher(config)
            import_match_plan = _build_article_import_match_plan(config)
            export_keyword_plan = build_article_export_keyword_plan(config)
            export_keyword_config_signature = build_article_export_keyword_signature(config)
            now_text = local_now().strftime("%Y-%m-%d %H:%M")
            import_id = uuid4().hex
            imported_articles: list[dict[str, Any]] = []
            imported_ids: list[str] = []
            updated_articles: list[dict[str, Any]] = []
            duplicate_count = 0
            skipped_count = 0
            existing_articles = [
                article
                for article in get_articles()
                if isinstance(article, dict)
            ]
            existing_by_url: dict[str, list[dict[str, Any]]] = {}
            existing_by_match_key: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
            existing_by_title_date: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for article in existing_articles:
                normalized_existing_url = normalize_article_url(str(article.get("url") or ""))
                if normalized_existing_url:
                    existing_by_url.setdefault(normalized_existing_url, []).append(article)
                match_key = _article_import_match_key(article)
                if match_key:
                    existing_by_match_key.setdefault(match_key, []).append(article)
                loose_key = _article_import_loose_key(article)
                if loose_key:
                    existing_by_title_date.setdefault(loose_key, []).append(article)

            pending_upserts: list[dict[str, Any]] = []
            pending_kinds: list[str] = []
            pending_idx_by_url: dict[str, int] = {}
            pending_idx_by_match_key: dict[tuple[str, str, str], int] = {}
            pending_idx_by_loose_key: dict[tuple[str, str], list[int]] = {}
            pending_idx_by_existing_id: dict[str, int] = {}
            pending_existing_before: dict[str, dict[str, Any]] = {}
            # 导入时顺手清理那些"上一版本代码留在库里、跟当前行属于同一篇文章"的孤儿副本。
            # consumed_existing_ids 用来防止同一个旧记录被多行重复领用，
            # duplicate_articles_to_delete 保存最终要删的那些 article。
            consumed_existing_ids: set[str] = set()
            duplicate_articles_to_delete: dict[str, dict[str, Any]] = {}

            def _filter_consumed(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
                return [
                    article
                    for article in candidates
                    if str(article.get("id") or "") not in consumed_existing_ids
                ]

            def _claim_existing_match(
                seed_candidates: list[dict[str, Any]],
            ) -> dict[str, Any] | None:
                """从一组候选里挑出"主锚"，把同主体的其余条目登记成要删的重复。
                还会沿着锚的 normalized_url 和 canonical match_key 在所有索引里扩展，
                把另一个索引里那条没链接的孤儿也一起合并掉——例如 redsh.com 222352 这种情况
                库里既有"带 URL 的旧版本"也有"丢了 URL 的旧版本"，只看 URL 索引会漏掉后者。
                同时把锚的非空字段吸收一遍重复条目的内容，避免误删信息。
                """
                live = {
                    str(article.get("id") or ""): article
                    for article in _filter_consumed(seed_candidates)
                    if str(article.get("id") or "")
                }
                if not live:
                    return None

                changed = True
                while changed:
                    changed = False
                    for article in list(live.values()):
                        article_url = normalize_article_url(str(article.get("url") or ""))
                        if article_url:
                            for sibling in existing_by_url.get(article_url, []):
                                sib_id = str(sibling.get("id") or "")
                                if (
                                    sib_id
                                    and sib_id not in live
                                    and sib_id not in consumed_existing_ids
                                ):
                                    live[sib_id] = sibling
                                    changed = True
                        article_match_key = _article_import_match_key(article)
                        if article_match_key:
                            for sibling in existing_by_match_key.get(article_match_key, []):
                                sib_id = str(sibling.get("id") or "")
                                if (
                                    sib_id
                                    and sib_id not in live
                                    and sib_id not in consumed_existing_ids
                                ):
                                    live[sib_id] = sibling
                                    changed = True

                anchor, duplicates = _pick_best_existing_anchor(list(live.values()))
                if anchor is None:
                    return None
                anchor_id = str(anchor.get("id") or "")
                if anchor_id:
                    consumed_existing_ids.add(anchor_id)
                for dup in duplicates:
                    dup_id = str(dup.get("id") or "")
                    if not dup_id or dup_id == anchor_id:
                        continue
                    # 把重复条目里 anchor 还没有的字段补给 anchor，再登记删除。
                    _merge_article_import_fields(anchor, dup)
                    consumed_existing_ids.add(dup_id)
                    duplicate_articles_to_delete.setdefault(dup_id, dup)
                return anchor

            def _register_pending_keys(idx: int, article: dict[str, Any]) -> None:
                normalized_pending_url = normalize_article_url(str(article.get("url") or ""))
                if normalized_pending_url:
                    pending_idx_by_url.setdefault(normalized_pending_url, idx)
                pending_match_key = _article_import_match_key(article)
                if pending_match_key:
                    pending_idx_by_match_key.setdefault(pending_match_key, idx)
                pending_loose_key = _article_import_loose_key(article)
                if pending_loose_key:
                    bucket = pending_idx_by_loose_key.setdefault(pending_loose_key, [])
                    if idx not in bucket:
                        bucket.append(idx)

            mutable_import_fields = (
                "url",
                "raw_url",
                "title",
                "platform",
                "media_name",
                "media_type",
                "excerpt",
                "price",
                "published_at",
                "ts",
                "account_name",
                "matched_tasks",
                "match_reasons",
                "unmatched_reason",
            )

            def values_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
                for key in mutable_import_fields:
                    if json.dumps(before.get(key, ""), ensure_ascii=False, sort_keys=True) != json.dumps(after.get(key, ""), ensure_ascii=False, sort_keys=True):
                        return True
                return False

            for raw_item in raw_items:
                title = str(raw_item.get("title") or "").strip()
                if not title:
                    skipped_count += 1
                    continue

                raw_url = str(raw_item.get("url") or "").strip()
                normalized_url = normalize_article_url(raw_url) if raw_url else ""

                raw_media_name = str(raw_item.get("media_name") or "").strip()
                account_name = str(raw_item.get("account_name") or "").strip()
                platform_from_media = ""
                account_from_media = ""
                if raw_media_name:
                    platform_from_media, account_from_media = _split_article_import_platform_account(
                        raw_media_name,
                        allow_unknown_bracket_platform=True,
                    )
                if platform_from_media:
                    raw_media_name = platform_from_media
                if account_from_media and not account_name:
                    account_name = account_from_media
                platform_from_account = ""
                account_from_account = ""
                if account_name:
                    platform_from_account, account_from_account = _split_article_import_platform_account(account_name)
                if platform_from_account and not raw_media_name:
                    raw_media_name = platform_from_account
                if account_from_account:
                    account_name = account_from_account
                raw_media_name = _strip_article_import_media_qualifier(raw_media_name)
                bracket_match = re.fullmatch(r"(.+?)[（(]([^（）()]+)[）)]", raw_media_name)
                if bracket_match and not account_name:
                    raw_media_name = bracket_match.group(1).strip()
                    account_name = bracket_match.group(2).strip()
                platform_label = _resolve_article_import_platform_label(raw_media_name)
                if platform_label:
                    raw_media_name = platform_label

                url_media_name = resolve_media_name(normalized_url or raw_url)
                if (
                    raw_media_name
                    and not account_name
                    and url_media_name
                    and raw_media_name != url_media_name
                    and not _resolve_article_import_platform_label(raw_media_name)
                    and classify_article_media_type(normalized_url or raw_url, url_media_name) == "selfmedia"
                ):
                    account_name = raw_media_name
                    raw_media_name = url_media_name
                media_name = resolve_media_name(raw_media_name) or raw_media_name or url_media_name
                if account_name and media_name == account_name and url_media_name:
                    media_name = url_media_name
                if not media_name and account_name:
                    media_name = account_name
                if not media_name:
                    media_name = "未知来源"

                published_at = _normalize_article_import_date(raw_item.get("published_at"))
                media_type = _normalize_article_import_media_type(
                    raw_item.get("media_type"),
                    normalized_url or raw_url,
                    media_name,
                )
                draft_article: dict[str, Any] = {
                    "url": normalized_url or raw_url,
                    "raw_url": raw_url,
                    "title": title,
                    "platform": media_name,
                    "media_name": media_name,
                    "media_type": media_type,
                    "excerpt": str(raw_item.get("excerpt") or "").strip(),
                    "published_at": published_at,
                    "imported_at": now_text,
                    "ts": published_at,
                    "fetch_method": "manual_table_import",
                    "import_batch_id": import_id,
                    "import_status": "pending",
                    "import_file_name": original_name,
                    "imported_from_sheet": str(raw_item.get("_sheet") or "").strip(),
                    "imported_from_row": int(raw_item.get("_row") or 0),
                }
                if account_name:
                    draft_article["account_name"] = account_name
                if raw_item.get("price") is not None:
                    draft_article["price"] = raw_item["price"]

                draft_match_key = _article_import_match_key(draft_article)
                draft_loose_key = _article_import_loose_key(draft_article)

                def _pick_existing_loose() -> dict[str, Any] | None:
                    if not draft_loose_key:
                        return None
                    candidates = _filter_consumed(existing_by_title_date.get(draft_loose_key) or [])
                    if not candidates:
                        return None
                    # 当新行没 URL 时，可以让任意现存记录接收它；当新行有 URL 但现存没 URL 时，
                    # 也允许把缺失链接的旧记录补全。其他情况坚持要么命中 URL、要么命中规范 key，避免误合并。
                    if not normalized_url:
                        return candidates[0]
                    for candidate in candidates:
                        if not normalize_article_url(str(candidate.get("url") or "")):
                            return candidate
                    return None

                def _pick_pending_loose() -> int | None:
                    if not draft_loose_key:
                        return None
                    bucket = pending_idx_by_loose_key.get(draft_loose_key) or []
                    if not bucket:
                        return None
                    if not normalized_url:
                        return bucket[0]
                    for idx in bucket:
                        if not normalize_article_url(str(pending_upserts[idx].get("url") or "")):
                            return idx
                    return None

                target_kind: str | None = None
                target_value: Any = None
                if normalized_url and normalized_url in pending_idx_by_url:
                    target_kind, target_value = "pending", pending_idx_by_url[normalized_url]
                elif normalized_url and existing_by_url.get(normalized_url):
                    anchor = _claim_existing_match(existing_by_url[normalized_url])
                    if anchor is not None:
                        target_kind, target_value = "existing", anchor
                if target_kind is None and draft_match_key:
                    if draft_match_key in pending_idx_by_match_key:
                        target_kind, target_value = "pending", pending_idx_by_match_key[draft_match_key]
                    elif existing_by_match_key.get(draft_match_key):
                        anchor = _claim_existing_match(existing_by_match_key[draft_match_key])
                        if anchor is not None:
                            target_kind, target_value = "existing", anchor
                if target_kind is None:
                    pending_loose_idx = _pick_pending_loose()
                    if pending_loose_idx is not None:
                        target_kind, target_value = "pending", pending_loose_idx
                    else:
                        existing_loose = _pick_existing_loose()
                        if existing_loose is not None:
                            target_kind, target_value = "existing", existing_loose

                def _finalize_analysis(
                    candidate: dict[str, Any],
                    historical_source: dict[str, Any] | None,
                ) -> dict[str, Any]:
                    analyzed = analyze_article_matches(
                        str(candidate.get("title") or ""),
                        config,
                        article=candidate,
                        compiled_matcher=compiled_matcher,
                    )
                    analyzed = _merge_article_import_matches(
                        analyzed,
                        config,
                        raw_item,
                        original_name,
                        import_match_plan=import_match_plan,
                    )
                    if historical_source is not None:
                        analyzed = _merge_preserved_historical_task_matches(
                            analyzed,
                            historical_source,
                            config,
                        )
                    candidate["matched_tasks"] = analyzed.get("matched_tasks") or []
                    candidate["match_reasons"] = analyzed.get("match_reasons") or {}
                    candidate["unmatched_reason"] = analyzed.get("unmatched_reason", "") or ""
                    return _with_article_export_keyword_cache(
                        candidate,
                        config,
                        keyword_plan=export_keyword_plan,
                        config_signature=export_keyword_config_signature,
                    )

                if target_kind == "pending":
                    idx = target_value
                    merged = pending_upserts[idx]
                    _merge_article_import_fields(merged, draft_article)
                    # 时间信息：如果新行有更具体的发布时间，就替换原值。
                    if published_at:
                        existing_published = str(merged.get("published_at") or "").strip()
                        if not existing_published or len(published_at) > len(existing_published):
                            merged["published_at"] = published_at
                            merged["ts"] = published_at
                    merged["last_table_import_at"] = now_text
                    merged["last_table_import_file"] = original_name
                    merged["import_batch_id"] = import_id
                    merged["import_status"] = "pending"
                    historical = None
                    if pending_kinds[idx] == "update":
                        existing_id = str(merged.get("id") or "").strip()
                        if existing_id and existing_id in pending_existing_before:
                            historical = pending_existing_before[existing_id]
                    pending_upserts[idx] = _finalize_analysis(merged, historical)
                    _register_pending_keys(idx, pending_upserts[idx])
                    duplicate_count += 1
                    continue

                if target_kind == "existing":
                    existing_article = target_value
                    article_id = str(existing_article.get("id") or "").strip()
                    if article_id and article_id in pending_idx_by_existing_id:
                        idx = pending_idx_by_existing_id[article_id]
                        merged = pending_upserts[idx]
                        _merge_article_import_fields(merged, draft_article)
                        if published_at:
                            merged["published_at"] = published_at
                            merged["ts"] = published_at
                        merged["last_table_import_at"] = now_text
                        merged["last_table_import_file"] = original_name
                        merged["import_batch_id"] = import_id
                        merged["import_status"] = "pending"
                        pending_upserts[idx] = _finalize_analysis(
                            merged,
                            pending_existing_before.get(article_id),
                        )
                        _register_pending_keys(idx, pending_upserts[idx])
                        duplicate_count += 1
                        continue

                    candidate_article = dict(existing_article)
                    existing_url = str(existing_article.get("url") or "").strip()
                    candidate_article.update({
                        "title": title or str(existing_article.get("title") or ""),
                        "last_table_import_at": now_text,
                        "last_table_import_file": original_name,
                        "import_batch_id": import_id,
                        "import_status": "pending",
                    })
                    # URL：只在原记录缺链接、或新行带的是与原归一化结果一致的更完整 URL 时才覆盖，
                    # 避免出现"导入红安网把链接覆盖成空"或者无意覆盖原链接的情况。
                    if not existing_url and (normalized_url or raw_url):
                        candidate_article["url"] = normalized_url or raw_url
                    elif normalized_url and existing_url:
                        if normalize_article_url(existing_url) == normalized_url:
                            candidate_article["url"] = normalized_url
                    existing_raw_url = str(existing_article.get("raw_url") or "").strip()
                    if raw_url and (not existing_raw_url or normalize_article_url(existing_raw_url) == normalize_article_url(raw_url)):
                        candidate_article["raw_url"] = raw_url
                    if raw_media_name or url_media_name:
                        candidate_article.update({
                            "platform": media_name,
                            "media_name": media_name,
                            "media_type": media_type,
                        })
                    if account_name:
                        candidate_article["account_name"] = account_name
                    if raw_item.get("price") is not None:
                        candidate_article["price"] = raw_item["price"]
                    if published_at:
                        candidate_article["published_at"] = published_at
                        candidate_article["ts"] = published_at
                    if draft_article.get("excerpt"):
                        candidate_article["excerpt"] = draft_article["excerpt"]
                    candidate_article = _finalize_analysis(candidate_article, existing_article)
                    if not values_changed(existing_article, candidate_article):
                        duplicate_count += 1
                        continue
                    candidate_article["id"] = article_id
                    pending_upserts.append(candidate_article)
                    pending_kinds.append("update")
                    new_idx = len(pending_upserts) - 1
                    pending_idx_by_existing_id[article_id] = new_idx
                    pending_existing_before[article_id] = copy.deepcopy(existing_article)
                    _register_pending_keys(new_idx, candidate_article)
                    continue

                draft_article = _finalize_analysis(draft_article, None)
                pending_upserts.append(draft_article)
                pending_kinds.append("create")
                new_idx = len(pending_upserts) - 1
                _register_pending_keys(new_idx, draft_article)

            for existing_id, before_snapshot in pending_existing_before.items():
                updated_articles.append({"id": existing_id, "before": before_snapshot})

            if pending_upserts:
                stored_articles = bulk_upsert_articles(pending_upserts)
                for kind, article in zip(pending_kinds, stored_articles):
                    if kind == "create":
                        article_id = str(article.get("id") or "").strip()
                        if article_id:
                            imported_ids.append(article_id)
                    imported_articles.append(article)

            # 把跟主锚同主体的孤儿副本删掉。`exclude_url=False` 避免把这些 URL 放进排除列表，
            # 否则用户下次导入同一篇文章会被静默过滤。这里也记下被删的快照，方便后续审计。
            merged_duplicate_snapshots: list[dict[str, Any]] = []
            if duplicate_articles_to_delete:
                from core.article_store import delete_article as _delete_article
                anchor_ids = {
                    str(item.get("id") or "").strip()
                    for item in pending_upserts
                    if str(item.get("id") or "").strip()
                }
                for dup_id, snapshot in duplicate_articles_to_delete.items():
                    if not dup_id or dup_id in anchor_ids:
                        continue
                    try:
                        removed = _delete_article(dup_id, exclude_url=False)
                    except Exception:
                        removed = None
                    if removed is not None:
                        merged_duplicate_snapshots.append({
                            "id": dup_id,
                            "before": copy.deepcopy(removed),
                        })

            if not imported_ids and not updated_articles and not merged_duplicate_snapshots:
                return {
                    "ok": False,
                    "message": f"表格已识别 {len(raw_items)} 行，但没有新增或更新文章（重复 {duplicate_count} 行，跳过 {skipped_count} 行）",
                    "added_count": 0,
                    "updated_count": 0,
                    "duplicate_count": duplicate_count,
                    "skipped_count": skipped_count,
                    "details": details,
                }

            batch = {
                "id": import_id,
                "file_name": original_name,
                "article_ids": imported_ids,
                "updated_articles": updated_articles,
                "merged_duplicate_articles": merged_duplicate_snapshots,
                "status": "pending",
                "created_at": local_now().isoformat(timespec="seconds"),
                "added_count": len(imported_ids),
                "updated_count": len(updated_articles),
                "duplicate_count": duplicate_count,
                "merged_duplicate_count": len(merged_duplicate_snapshots),
                "skipped_count": skipped_count,
            }
            with self._lock:
                batches = self._import_batch_store.get_batches()
                batches[import_id] = batch
                self._import_batch_store.save(merge_existing=True)
            self._invalidate_article_cache()

            return {
                "ok": True,
                "message": (
                    f"已导入 {len(imported_ids)} 篇文章"
                    f"{f'，更新 {len(updated_articles)} 篇' if updated_articles else ''}"
                    f"{f'，清理重复 {len(merged_duplicate_snapshots)} 篇' if merged_duplicate_snapshots else ''}"
                    "，完成品牌归类，等待确认"
                ),
                "import_id": import_id,
                "file_name": original_name,
                "added_count": len(imported_ids),
                "updated_count": len(updated_articles),
                "duplicate_count": duplicate_count,
                "merged_duplicate_count": len(merged_duplicate_snapshots),
                "skipped_count": skipped_count,
                "articles": [_article_to_api(article) for article in imported_articles[:20]],
                "details": details,
            }
        except Exception as exc:
            return {"ok": False, "message": f"表格导入失败：{exc}"}

    def get_pending_article_imports(self) -> dict:
        with self._lock:
            batches = self._import_batch_store.get_batches()
            pending = [
                _article_import_batch_to_api(batch)
                for batch in batches.values()
                if str(batch.get("status") or "pending") == "pending"
            ]
        pending.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return {"ok": True, "batches": pending}

    def confirm_article_import(self, import_id: str) -> dict:
        import_id = str(import_id or "").strip()
        if not import_id:
            return {"ok": False, "message": "缺少导入批次 ID"}
        with self._lock:
            batches = self._import_batch_store.get_batches()
            batch = batches.get(import_id)
        if not batch:
            return {"ok": False, "message": "导入批次已处理或已失效"}
        try:
            confirmed_at = local_now().strftime("%Y-%m-%d %H:%M")
            updated_article_ids = [
                str((item or {}).get("id") or "").strip()
                for item in (batch.get("updated_articles") or [])
                if str((item or {}).get("id") or "").strip()
            ]
            confirm_article_import_batch(
                batch.get("article_ids") or [],
                updated_article_ids,
                import_id=import_id,
                confirmed_at=confirmed_at,
            )
            with self._lock:
                batches = self._import_batch_store.get_batches()
                batches.pop(import_id, None)
                self._import_batch_store.save()
            self._invalidate_article_cache()
        except Exception as exc:
            return {"ok": False, "message": f"确认失败：{exc}"}
        added_count = int(batch.get("added_count") or 0)
        updated_count = int(batch.get("updated_count") or 0)
        updated_message = f"，更新 {updated_count} 篇" if updated_count else ""
        return {
            "ok": True,
            "message": f"已确认 {added_count} 篇导入文章{updated_message}",
            "import_id": import_id,
        }

    def undo_article_import(self, import_id: str) -> dict:
        import_id = str(import_id or "").strip()
        if not import_id:
            return {"ok": False, "message": "缺少导入批次 ID"}
        with self._lock:
            batches = self._import_batch_store.get_batches()
            batch = batches.get(import_id)
        if not batch:
            return {"ok": False, "message": "导入批次已处理或已失效"}
        try:
            undo_stats = undo_article_import_batch(
                batch.get("article_ids") or [],
                batch.get("updated_articles") or [],
            )
            removed_count = int(undo_stats.get("removed_count") or 0)
            restored_count = int(undo_stats.get("restored_count") or 0)
            with self._lock:
                batches = self._import_batch_store.get_batches()
                batches.pop(import_id, None)
                self._import_batch_store.save()
            self._invalidate_article_cache()
            restored_message = f"，恢复 {restored_count} 篇已更新文章" if restored_count else ""
            return {
                "ok": True,
                "message": f"已撤销本次导入，移除 {removed_count} 篇文章{restored_message}",
                "import_id": import_id,
                "removed_count": removed_count,
                "restored_count": restored_count,
            }
        except Exception as exc:
            return {"ok": False, "message": f"撤销失败：{exc}"}

    def delete_article(self, article_id: str, task_name: str = "") -> dict:
        article_id = str(article_id or "").strip()
        task_name = str(task_name or "").strip()
        if not article_id:
            return {"ok": False, "message": "缺少文章 ID"}
        try:
            from core.article_store import delete_article, remove_article_from_task

            if task_name:
                removed = remove_article_from_task(article_id, task_name)
                if removed is None:
                    return {"ok": False, "message": "文章不存在"}
                self._invalidate_article_cache()
                return {"ok": True, "article": _article_to_api(removed), "scoped": True}

            removed = delete_article(article_id)
            if removed is None:
                return {"ok": False, "message": "文章不存在"}
            self._invalidate_article_cache()
            return {"ok": True, "article": _article_to_api(removed)}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def update_article_media_type(self, article_id: str, media_type: str) -> dict:
        article_id = str(article_id or "").strip()
        media_type = str(media_type or "").strip().lower()
        if not article_id:
            return {"ok": False, "message": "缺少文章 ID"}
        if media_type not in {"authority", "selfmedia"}:
            return {"ok": False, "message": "媒体类型无效"}
        try:
            changed = update_article_media_type(article_id, media_type)
            if not changed:
                return {"ok": False, "message": "文章不存在或未发生更新"}
            self._invalidate_article_cache()
            updated_article = next(
                (article for article in get_articles() if str(article.get("id", "")).strip() == article_id),
                None,
            )
            if updated_article is None:
                return {"ok": False, "message": "文章不存在"}
            return {"ok": True, "article": _article_to_api(updated_article)}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def update_article(self, article_id: str, payload: dict) -> dict:
        article_id = str(article_id or "").strip()
        if not article_id:
            return {"ok": False, "message": "缺少文章 ID"}
        if not isinstance(payload, dict):
            return {"ok": False, "message": "参数无效"}

        current_article = next(
            (article for article in get_articles() if str(article.get("id", "")).strip() == article_id),
            None,
        )
        if current_article is None:
            return {"ok": False, "message": "文章不存在"}

        patch: dict[str, Any] = {}
        if "title" in payload:
            title = str(payload.get("title", "") or "").strip()
            if not title:
                return {"ok": False, "message": "文章标题不能为空"}
            patch["title"] = title

        if "media_name" in payload or "mediaName" in payload or "source" in payload:
            raw_media_name = (
                payload.get("media_name")
                if "media_name" in payload
                else payload.get("mediaName")
                if "mediaName" in payload
                else payload.get("source")
            )
            media_name = str(raw_media_name or "").strip()
            if not media_name:
                return {"ok": False, "message": "媒体名不能为空"}
            patch["media_name"] = media_name

        if "published_at" in payload or "publishedAt" in payload or "ts" in payload:
            raw_published_at = (
                payload.get("published_at")
                if "published_at" in payload
                else payload.get("publishedAt")
                if "publishedAt" in payload
                else payload.get("ts")
            )
            published_at = str(raw_published_at or "").strip()[:10]
            patch["published_at"] = published_at
            if published_at:
                patch["ts"] = published_at

        if "media_type" in payload:
            media_type = str(payload.get("media_type", "") or "").strip().lower()
            if media_type not in {"authority", "selfmedia"}:
                return {"ok": False, "message": "媒体类型无效"}
            patch["media_type"] = media_type

        manual_task_assignment = any(
            key in payload
            for key in (
                "matched_tasks",
                "matchedTasks",
                "task_names",
                "taskNames",
                "task_name",
                "taskName",
                "brand",
                "brand_name",
                "brandName",
            )
        )
        light_config_loader = getattr(self._config_provider, "load_without_hooks", None)
        config = (
            light_config_loader()
            if callable(light_config_loader) else
            self._config_provider.load()
        )
        manual_task_names: list[str] = []
        if manual_task_assignment:
            manual_task_names, task_error = self._resolve_article_task_names(config, current_article, payload)
            if task_error:
                return {"ok": False, "message": task_error}
            existing_reasons = current_article.get("match_reasons") if isinstance(current_article.get("match_reasons"), dict) else {}
            patch["matched_tasks"] = manual_task_names
            patch["match_reasons"] = {
                task_name: existing_reasons.get(task_name) or ["手动设置所属品牌"]
                for task_name in manual_task_names
            }
            patch["unmatched_reason"] = "" if manual_task_names else "手动清除所属品牌，暂未归类"
            if manual_task_names:
                patch["excluded_tasks"] = [
                    str(name or "").strip()
                    for name in (current_article.get("excluded_tasks") or [])
                    if str(name or "").strip() and str(name or "").strip() not in set(manual_task_names)
                ]

        if not patch:
            return {"ok": False, "message": "没有可更新的内容"}

        try:
            merged_article = {**current_article, **patch}
            if not manual_task_assignment:
                analyzed = analyze_article_matches(
                    str(merged_article.get("title", "") or ""),
                    config,
                    article=merged_article,
                )
                analyzed = _merge_preserved_historical_task_matches(analyzed, current_article, config)
                patch["matched_tasks"] = analyzed.get("matched_tasks") or []
                patch["match_reasons"] = analyzed.get("match_reasons") or {}
                patch["unmatched_reason"] = analyzed.get("unmatched_reason", "") or ""

            if "media_name" in patch:
                domain = extract_domain(str(current_article.get("url", "") or ""))
                if domain:
                    save_domain_media_name(domain, str(patch["media_name"]), force=True)

            cache_article = {**current_article, **patch}
            update_article_export_keyword_cache(cache_article, config)
            patch.update(article_export_keyword_cache_fields(cache_article))

            updated_article = update_article(article_id, patch)
            if updated_article is None:
                return {"ok": False, "message": "文章不存在"}
            self._invalidate_article_cache()
            return {"ok": True, "article": _article_to_api(updated_article)}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
