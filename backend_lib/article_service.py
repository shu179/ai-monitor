"""Article service and import batch state for the local web backend."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
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
)
from backend_lib.config_provider import RuntimeConfigProvider
from core.app_paths import resolve_app_path
from core.article_store import (
    analyze_article_matches,
    confirm_article_import_batch,
    extract_domain,
    get_articles_file_path,
    get_articles,
    resolve_article_export_keywords,
    resolve_article_source,
    save_domain_media_name,
    undo_article_import_batch,
    update_article,
    update_media_type as update_article_media_type,
)
from core.daily_task_state import derive_task_id
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


def _article_to_api(
    article: dict[str, Any],
    config: dict[str, Any] | None = None,
    task_name: str = "",
    *,
    include_export_keywords: bool = True,
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
        "url": article.get("url", ""),
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
    if include_export_keywords:
        payload["exportKeywords"] = resolve_article_export_keywords(article, config or {}, task_name)
    return payload


def _article_import_batches_path() -> Path:
    return account_scoped_path(
        "logs/article_import_batches.json",
        fallback=resolve_app_path("logs/article_import_batches.json"),
    )


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
    return {
        "id": import_id,
        "file_name": str(batch.get("file_name") or "").strip(),
        "article_ids": article_ids,
        "updated_articles": updated_articles,
        "status": status,
        "created_at": str(batch.get("created_at") or "").strip(),
        "added_count": int(batch.get("added_count") or len(article_ids)),
        "updated_count": int(batch.get("updated_count") or len(updated_articles)),
        "duplicate_count": int(batch.get("duplicate_count") or 0),
        "skipped_count": int(batch.get("skipped_count") or 0),
    }


def _load_article_import_batches_file() -> dict[str, dict[str, Any]]:
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


def _save_article_import_batches_file(batches: dict[str, dict[str, Any]]) -> None:
    path = _article_import_batches_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": local_now().isoformat(timespec="seconds"),
        "batches": [
            batch
            for batch in batches.values()
            if isinstance(batch, dict) and str(batch.get("status") or "pending") == "pending"
        ],
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

    def save(self) -> None:
        _save_article_import_batches_file(self.get_batches())

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


def _merge_article_import_matches(
    analyzed: dict[str, object],
    config: dict[str, Any],
    raw_item: dict[str, Any],
    file_name: str,
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

    for task in config.get("tasks", []):
        if not isinstance(task, dict):
            continue
        task_name = str(task.get("name") or "").strip()
        if not task_name:
            continue
        task_terms = _article_import_task_terms(task)
        for source_label, source_value in source_texts:
            source_key = _compact_article_import_match_text(source_value)
            if len(source_key) < 2:
                continue
            for term in task_terms:
                term_key = _compact_article_import_match_text(term)
                if len(term_key) < 2:
                    continue
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
    ) -> None:
        self._config_provider = config_provider
        self._get_synced_articles = synced_articles_loader
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
        config = self._config_provider.load()
        articles = self._get_synced_articles(config)
        if task_name:
            articles = [article for article in articles if task_name in (article.get("matched_tasks") or [])]
        if media_type:
            type_map = {"media": "authority", "self-media": "selfmedia"}
            target = type_map.get(media_type, media_type)
            articles = [article for article in articles if article.get("media_type") == target]
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
                )
                for article in articles
            ],
            "total": total,
            "today_total": today_total,
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
                refreshed_article["matched_tasks"] = analyzed.get("matched_tasks") or []
                refreshed_article["match_reasons"] = analyzed.get("match_reasons") or {}
                refreshed_article["unmatched_reason"] = analyzed.get("unmatched_reason", "") or ""
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
            article = add_article({
                **draft_article,
                "matched_tasks": analyzed.get("matched_tasks") or [],
                "match_reasons": analyzed.get("match_reasons") or {},
                "unmatched_reason": analyzed.get("unmatched_reason", "") or "",
            })
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
                add_article as store_add_article,
                classify_article_media_type,
                find_article_by_url,
                normalize_article_url,
                resolve_media_name,
                update_article as store_update_article,
            )

            raw_items, details = _extract_article_import_items(original_name, data)
            if not raw_items:
                return {
                    "ok": False,
                    "message": "没有识别到可导入文章，请确认表格包含“标题”列，可选包含“链接 / 媒体 / 发布时间”列",
                    "details": details,
                }

            config = self._config_provider.load()
            now_text = local_now().strftime("%Y-%m-%d %H:%M")
            import_id = uuid4().hex
            imported_articles: list[dict[str, Any]] = []
            imported_ids: list[str] = []
            updated_articles: list[dict[str, Any]] = []
            duplicate_count = 0
            skipped_count = 0
            seen_keys: set[str] = set()

            mutable_import_fields = (
                "url",
                "title",
                "platform",
                "media_name",
                "media_type",
                "excerpt",
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
                row_key = normalized_url or "|".join([
                    title,
                    str(raw_item.get("media_name") or "").strip(),
                    str(raw_item.get("published_at") or "").strip(),
                ])
                if row_key in seen_keys:
                    duplicate_count += 1
                    continue
                seen_keys.add(row_key)

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
                bracket_match = re.fullmatch(r"(.+?)[（(]([^（）()]+)[）)]", raw_media_name)
                if bracket_match and not account_name:
                    raw_media_name = bracket_match.group(1).strip()
                    account_name = bracket_match.group(2).strip()

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

                analyzed = analyze_article_matches(title, config, article=draft_article)
                analyzed = _merge_article_import_matches(analyzed, config, raw_item, original_name)
                draft_article.update({
                    "matched_tasks": analyzed.get("matched_tasks") or [],
                    "match_reasons": analyzed.get("match_reasons") or {},
                    "unmatched_reason": analyzed.get("unmatched_reason", "") or "",
                })

                existing_article = find_article_by_url(normalized_url) if normalized_url else None
                if existing_article:
                    candidate_article = dict(existing_article)
                    candidate_article.update({
                        "url": normalized_url or raw_url or str(existing_article.get("url") or ""),
                        "title": title or str(existing_article.get("title") or ""),
                        "last_table_import_at": now_text,
                        "last_table_import_file": original_name,
                        "import_batch_id": import_id,
                        "import_status": "pending",
                    })
                    if raw_media_name or url_media_name:
                        candidate_article.update({
                            "platform": media_name,
                            "media_name": media_name,
                            "media_type": media_type,
                        })
                    if account_name:
                        candidate_article["account_name"] = account_name
                    if published_at:
                        candidate_article["published_at"] = published_at
                        candidate_article["ts"] = published_at
                    if draft_article.get("excerpt"):
                        candidate_article["excerpt"] = draft_article["excerpt"]
                    candidate_analysis = analyze_article_matches(
                        str(candidate_article.get("title") or ""),
                        config,
                        article=candidate_article,
                    )
                    candidate_analysis = _merge_article_import_matches(candidate_analysis, config, raw_item, original_name)
                    candidate_article["matched_tasks"] = candidate_analysis.get("matched_tasks") or []
                    candidate_article["match_reasons"] = candidate_analysis.get("match_reasons") or {}
                    candidate_article["unmatched_reason"] = candidate_analysis.get("unmatched_reason", "") or ""
                    if not values_changed(existing_article, candidate_article):
                        duplicate_count += 1
                        continue
                    updated_article = store_update_article(str(existing_article.get("id") or ""), candidate_article) or candidate_article
                    updated_articles.append({
                        "id": str(existing_article.get("id") or "").strip(),
                        "before": copy.deepcopy(existing_article),
                    })
                    imported_articles.append(updated_article)
                    continue

                article = store_add_article(draft_article)
                article_id = str(article.get("id") or "").strip()
                if article_id:
                    imported_ids.append(article_id)
                    imported_articles.append(article)

            if not imported_ids and not updated_articles:
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
                "status": "pending",
                "created_at": local_now().isoformat(timespec="seconds"),
                "added_count": len(imported_ids),
                "updated_count": len(updated_articles),
                "duplicate_count": duplicate_count,
                "skipped_count": skipped_count,
            }
            with self._lock:
                batches = self._import_batch_store.get_batches()
                batches[import_id] = batch
                self._import_batch_store.save()
            self._invalidate_article_cache()

            return {
                "ok": True,
                "message": (
                    f"已导入 {len(imported_ids)} 篇文章"
                    f"{f'，更新 {len(updated_articles)} 篇' if updated_articles else ''}"
                    "，完成品牌归类，等待确认"
                ),
                "import_id": import_id,
                "file_name": original_name,
                "added_count": len(imported_ids),
                "updated_count": len(updated_articles),
                "duplicate_count": duplicate_count,
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
        config = self._config_provider.load()
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
                patch["matched_tasks"] = analyzed.get("matched_tasks") or []
                patch["match_reasons"] = analyzed.get("match_reasons") or {}
                patch["unmatched_reason"] = analyzed.get("unmatched_reason", "") or ""

            if "media_name" in patch:
                domain = extract_domain(str(current_article.get("url", "") or ""))
                if domain:
                    save_domain_media_name(domain, str(patch["media_name"]), force=True)

            updated_article = update_article(article_id, patch)
            if updated_article is None:
                return {"ok": False, "message": "文章不存在"}
            self._invalidate_article_cache()
            return {"ok": True, "article": _article_to_api(updated_article)}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
