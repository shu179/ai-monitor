from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from .article_store import (
    get_articles,
    import_article_store_bundle,
    normalize_article_url,
    prune_cloud_articles_by_visible_task_ids,
)
from .cloud_client import CloudClientError, SurfacedCloudClient
from .cloud_session_store import CloudSessionChangedError, CloudSessionStore, cloud_session_identity_key
from .cloud_task_sync import CLOUD_PLATFORM_SYNC_STATE_KEY, _cloud_request_with_refresh
from .time_utils import local_now


ARTICLE_SYNC_STATE_KEY = "articles"


def pull_cloud_articles_into_store(
    config: dict[str, Any],
    *,
    client: SurfacedCloudClient | None = None,
    session_store: CloudSessionStore | None = None,
    force_full: bool = False,
    limit: int = 5000,
    max_pages: int = 25,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    store = session_store or CloudSessionStore()
    session = store.load()
    initial_identity_key = cloud_session_identity_key(session)
    base_url = str(session.get("base_url") or "").strip()
    access_token = str(session.get("access_token") or "").strip()
    refresh_token = str(session.get("refresh_token") or "").strip()
    if not base_url or not access_token:
        return _summary(ok=False, message="未登录云端", started_at=started_at)

    target_client = client or SurfacedCloudClient(base_url)
    state = _article_sync_state(config)
    cursor = "" if force_full else str(state.get("updated_after") or "").strip()
    cursor_id = 0 if force_full else _safe_int(state.get("updated_after_id")) or 0
    safe_limit = min(max(int(limit or 5000), 1), 10000)
    safe_max_pages = min(max(int(max_pages or 25), 1), 100)
    articles: list[dict[str, Any]] = []
    pages = 0
    cursor_stalled = False
    next_cursor = cursor
    next_cursor_id = cursor_id

    try:
        while pages < safe_max_pages:
            response, session = _cloud_request_with_refresh(
                target_client,
                store,
                session,
                base_url=base_url,
                access_token=access_token,
                refresh_token=refresh_token,
                operation=lambda token, current_cursor=next_cursor, current_cursor_id=next_cursor_id: target_client.task_articles(
                    token,
                    updated_after=current_cursor,
                    updated_after_id=current_cursor_id,
                    limit=safe_limit,
                ),
            )
            pages += 1
            batch = response.get("articles") if isinstance(response, dict) else []
            if not isinstance(batch, list):
                batch = []
            articles.extend([item for item in batch if isinstance(item, dict)])
            response_cursor = str((response or {}).get("max_updated_at") or next_cursor).strip()
            response_cursor_id = _safe_int((response or {}).get("max_article_id")) or 0
            if not batch:
                break
            if response_cursor == next_cursor and response_cursor_id == next_cursor_id:
                cursor_stalled = True
                break
            next_cursor = response_cursor
            next_cursor_id = response_cursor_id
            if len(batch) < safe_limit:
                break
    except (CloudClientError, CloudSessionChangedError) as exc:
        return _summary(ok=False, message=str(exc), started_at=started_at)

    if cloud_session_identity_key(store.load()) != initial_identity_key:
        return _summary(ok=False, message="云端账号已切换，本次文章拉取已中止", started_at=started_at)

    local_articles = cloud_articles_to_local_articles(articles, config)
    import_result = (
        import_article_store_bundle({"articles": local_articles}, mode="merge")
        if local_articles
        else {"articles_created": 0, "articles_updated": 0}
    )
    prune_kwargs = {}
    if force_full:
        prune_kwargs = {
            "visible_cloud_article_ids": [
                article_id
                for article_id in (_safe_int(article.get("id")) for article in articles)
                if article_id is not None
            ],
            "visible_cloud_url_hashes": [
                str(article.get("url_hash") or "").strip()
                for article in articles
                if str(article.get("url_hash") or "").strip()
            ],
        }
    prune_result = prune_cloud_articles_by_visible_task_ids(_visible_cloud_task_ids(config), **prune_kwargs)

    cursor_updated = False
    if (
        next_cursor
        and (
            next_cursor != str(state.get("updated_after") or "").strip()
            or next_cursor_id != (_safe_int(state.get("updated_after_id")) or 0)
        )
    ):
        state["updated_after"] = next_cursor
        state["updated_after_id"] = next_cursor_id
        cursor_updated = True
    state["last_synced_at"] = local_now().isoformat(timespec="seconds")
    state["last_mode"] = "full" if force_full else "incremental"
    if force_full:
        state["last_full_synced_at"] = state["last_synced_at"]
    _set_article_sync_state(config, state)

    created = int(import_result.get("articles_created") or 0)
    updated = int(import_result.get("articles_updated") or 0)
    return {
        "ok": True,
        "mode": "full" if force_full else "incremental",
        "fetched": len(articles),
        "imported": len(local_articles),
        "created": created,
        "updated": updated,
        "pruned": int(prune_result.get("removed") or 0),
        "pruned_links": int(prune_result.get("pruned_links") or 0),
        "cursor_updates": 1 if cursor_updated else 0,
        "updated_after": next_cursor,
        "updated_after_id": next_cursor_id,
        "pages": pages,
        "cursor_stalled": cursor_stalled,
        "state_updated": 1 if cursor_updated else 0,
        "duration_ms": _elapsed_ms(started_at),
    }


def cloud_articles_to_local_articles(cloud_articles: list[dict[str, Any]], config: dict[str, Any] | None) -> list[dict[str, Any]]:
    task_name_by_cloud_id = _task_name_by_cloud_id(config)
    existing_id_by_url = {
        normalize_article_url(str(article.get("url") or "")): str(article.get("id") or "").strip()
        for article in get_articles()
        if isinstance(article, dict) and normalize_article_url(str(article.get("url") or ""))
    }
    items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for cloud_article in cloud_articles or []:
        if not isinstance(cloud_article, dict):
            continue
        local_article = _cloud_article_to_local_article(
            cloud_article,
            task_name_by_cloud_id=task_name_by_cloud_id,
            existing_id_by_url=existing_id_by_url,
        )
        if not local_article:
            continue
        normalized_url = normalize_article_url(str(local_article.get("url") or ""))
        if normalized_url and normalized_url in seen_urls:
            continue
        if normalized_url:
            seen_urls.add(normalized_url)
        items.append(local_article)
    return items


def _cloud_article_to_local_article(
    cloud_article: dict[str, Any],
    *,
    task_name_by_cloud_id: dict[int, str],
    existing_id_by_url: dict[str, str],
) -> dict[str, Any] | None:
    url = str(cloud_article.get("canonical_url") or "").strip()
    if not url:
        return None
    normalized_url = normalize_article_url(url) or url
    cloud_id = _safe_int(cloud_article.get("id"))
    payload = cloud_article.get("payload_json") if isinstance(cloud_article.get("payload_json"), dict) else {}
    task_links = cloud_article.get("task_links") if isinstance(cloud_article.get("task_links"), list) else []
    matched_tasks: list[str] = []
    match_reasons: dict[str, list[str]] = {}
    cloud_task_ids: list[int] = []
    for link in task_links:
        if not isinstance(link, dict):
            continue
        task_id = _safe_int(link.get("task_id"))
        if task_id is None:
            continue
        task_name = task_name_by_cloud_id.get(task_id)
        if not task_name:
            continue
        cloud_task_ids.append(task_id)
        if task_name not in matched_tasks:
            matched_tasks.append(task_name)
        reasons = _link_reasons(link.get("reason_json"))
        if reasons:
            match_reasons[task_name] = reasons

    existing_id = existing_id_by_url.get(normalized_url)
    local_id = existing_id or (f"cloud_article_{cloud_id}" if cloud_id is not None else "")
    published_at = _date_text(cloud_article.get("published_at") or payload.get("published_at"))
    created_at = str(cloud_article.get("created_at") or "").strip()
    source = str(cloud_article.get("source") or payload.get("media_name") or payload.get("platform") or "").strip()
    article = {
        "id": local_id,
        "url": normalized_url,
        "title": str(cloud_article.get("title") or "").strip(),
        "platform": source,
        "media_name": source,
        "media_type": str(cloud_article.get("media_type") or "selfmedia").strip() or "selfmedia",
        "excerpt": str(payload.get("excerpt") or "").strip(),
        "published_at": published_at,
        "imported_at": _datetime_minute(created_at) or local_now().strftime("%Y-%m-%d %H:%M"),
        "ts": published_at or _date_text(created_at) or local_now().strftime("%Y-%m-%d"),
        "fetch_method": "cloud",
        "matched_tasks": matched_tasks,
        "match_reasons": match_reasons,
        "unmatched_reason": "" if matched_tasks else "云端文章当前未匹配到本地可见品牌",
        "cloud_article_id": cloud_id,
        "cloud_url_hash": str(cloud_article.get("url_hash") or "").strip(),
        "cloud_task_ids": sorted(set(cloud_task_ids)),
        "cloud_synced_at": local_now().isoformat(timespec="seconds"),
        "cloud_imported": not bool(existing_id),
    }
    for key in ("account_name", "account_url"):
        value = str(payload.get(key) or "").strip()
        if value:
            article[key] = value
    return article


def _task_name_by_cloud_id(config: dict[str, Any] | None) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for task in (config or {}).get("tasks") or []:
        if not isinstance(task, dict):
            continue
        cloud_task_id = _safe_int(task.get("cloud_task_id") or task.get("cloudTaskId"))
        task_name = str(task.get("name") or task.get("brand") or task.get("task_id") or "").strip()
        if cloud_task_id is None or not task_name:
            continue
        mapping[cloud_task_id] = task_name
    return mapping


def _visible_cloud_task_ids(config: dict[str, Any] | None) -> list[int]:
    task_ids: list[int] = []
    seen: set[int] = set()
    for task in (config or {}).get("tasks") or []:
        if not isinstance(task, dict):
            continue
        if bool(task.get("delete_pending")):
            continue
        access_level = str(task.get("cloud_access_level") or "").strip().lower()
        if access_level == "revoked":
            continue
        cloud_task_id = _safe_int(task.get("cloud_task_id") or task.get("cloudTaskId"))
        if cloud_task_id is None or cloud_task_id in seen:
            continue
        seen.add(cloud_task_id)
        task_ids.append(cloud_task_id)
    return task_ids


def _article_sync_state(config: dict[str, Any]) -> dict[str, Any]:
    platform_state = config.get(CLOUD_PLATFORM_SYNC_STATE_KEY)
    if not isinstance(platform_state, dict):
        platform_state = {}
    article_state = platform_state.get(ARTICLE_SYNC_STATE_KEY)
    return dict(article_state) if isinstance(article_state, dict) else {}


def _set_article_sync_state(config: dict[str, Any], article_state: dict[str, Any]) -> None:
    platform_state = config.get(CLOUD_PLATFORM_SYNC_STATE_KEY)
    if not isinstance(platform_state, dict):
        platform_state = {}
    platform_state[ARTICLE_SYNC_STATE_KEY] = dict(article_state or {})
    config[CLOUD_PLATFORM_SYNC_STATE_KEY] = platform_state


def _link_reasons(value: Any) -> list[str]:
    if isinstance(value, dict):
        source = value.get("reasons") if isinstance(value.get("reasons"), list) else []
        if not source:
            source = [item for item in value.values() if isinstance(item, str)]
    elif isinstance(value, list):
        source = value
    else:
        source = []
    reasons: list[str] = []
    seen: set[str] = set()
    for item in source:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        reasons.append(text[:256])
        if len(reasons) >= 5:
            break
    return reasons


def _safe_int(value: Any) -> int | None:
    try:
        number = int(str(value or "").strip())
    except Exception:
        return None
    return number if number > 0 else None


def _date_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text[:10]


def _datetime_minute(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        return parsed.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return text[:16]


def _summary(*, ok: bool, message: str, started_at: float) -> dict[str, Any]:
    return {
        "ok": ok,
        "message": message,
        "mode": "incremental",
        "fetched": 0,
        "imported": 0,
        "created": 0,
        "updated": 0,
        "cursor_updates": 0,
        "state_updated": 0,
        "duration_ms": _elapsed_ms(started_at),
    }


def _elapsed_ms(started_at: float) -> int:
    return max(0, int(round((time.perf_counter() - started_at) * 1000)))
