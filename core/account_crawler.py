"""Account homepage article crawler.

第一版只抓账号首页 / RSS / 接口返回的最新列表，不做深度翻页。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urljoin, urlparse, urlsplit, urlunsplit
from xml.etree import ElementTree

import requests

from core.app_paths import resolve_app_path
from core.file_lock import CrossProcessRLock
from core.local_account_space import account_scoped_path
from core.article_store import (
    add_article,
    analyze_article_matches,
    find_article_by_url,
    get_excluded_article_urls,
    is_article_url_excluded,
    normalize_article_url,
    resolve_media_name,
    update_article,
)
from core.time_utils import local_now, local_today

ACCOUNT_CRAWL_STATE_FILE = resolve_app_path("logs/account_crawl_state.json")
DEFAULT_RSSHUB_BASE_URL = "https://rsshub.app"
DEFAULT_RSSHUB_FALLBACK_BASE_URLS = (
    "https://rsshub.akr.moe",
    "https://rss.neoz.cc",
    "https://rsshub.umzzz.com",
)
DEFAULT_FREQUENCY_MINUTES = 60
DEFAULT_MAX_ITEMS_PER_ACCOUNT = 100
FETCH_TIMEOUT = (2.5, 6)
MAX_ACCOUNT_WORKERS = 6
RSSHUB_HARD_FAILURE_STATUS_CODES = {403, 404, 410, 451}
RSSHUB_SOFT_FAILURES_BEFORE_COOLDOWN = 3
RSSHUB_HARD_COOLDOWN_MINUTES = 6 * 60
RSSHUB_RATE_LIMIT_COOLDOWN_MINUTES = 2 * 60
RSSHUB_SOFT_COOLDOWN_MINUTES = 60
RSSHUB_MAX_COOLDOWN_MINUTES = 24 * 60
CHINA_TIMEZONE = timezone(timedelta(hours=8))
TOUTIAO_BROWSER_MAX_PAGES = 20
TOUTIAO_BROWSER_PAGE_SIZE = 12
SOHU_BROWSER_MAX_PAGES = 10
SOHU_BROWSER_PAGE_SIZE = 20
ACCOUNT_BROWSER_TIMEOUT_MS = 30000

_lock = CrossProcessRLock(lambda: _state_lock_file())
_account_browser_lock = threading.Lock()
_rsshub_success_cache: dict[str, str] = {}


class AccountFetchError(RuntimeError):
    def __init__(self, message: str, *, rsshub_attempts: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.rsshub_attempts = rsshub_attempts or []

PLATFORM_LABELS: dict[str, str] = {
    "toutiao": "头条号",
    "sohu": "搜狐号",
    "zhihu": "知乎",
    "cnblogs": "博客园",
    "rss": "RSS",
    "unknown": "未知平台",
}
ACCOUNT_PLATFORM_MEDIA_NAMES: dict[str, str] = {
    "toutiao": "今日头条",
    "sohu": "搜狐",
    "zhihu": "知乎",
    "cnblogs": "博客园",
}
PUBLISHED_JSON_KEYS = (
    "publicTime",
    "public_time",
    "publish_time",
    "publishTime",
    "publishDate",
    "publish_date",
    "published_at",
    "publishedAt",
    "published",
    "publishedDate",
    "datePublished",
    "dateCreated",
    "dateModified",
    "releaseDate",
    "release_date",
    "releaseTime",
    "release_time",
    "pubDate",
    "pubdate",
    "create_time",
    "created_at",
    "createTime",
    "createdTime",
    "created",
    "update_time",
    "updated_at",
    "updateTime",
    "updatedTime",
    "updated",
    "modified_time",
    "modifiedAt",
    "lastmod",
    "lastModified",
    "behot_time",
)
SOHU_PUBLISHED_JSON_KEYS = (
    "publicTime",
    "public_time",
    "publishTime",
    "publish_time",
    "publishDate",
    "publish_date",
    "newsTime",
    "news_time",
    "createdTime",
    "created_time",
    "createTime",
    "create_time",
    "updateTime",
    "update_time",
    *PUBLISHED_JSON_KEYS,
)
SOHU_TITLE_JSON_KEYS = (
    "title",
    "newsTitle",
    "articleTitle",
    "article_title",
    "displayTitle",
    "display_title",
    "headline",
)
SOHU_URL_JSON_KEYS = (
    "url",
    "article_url",
    "articleUrl",
    "newsUrl",
    "news_url",
    "share_url",
    "shareUrl",
    "link",
    "target_url",
)
SOHU_ARTICLE_ID_JSON_KEYS = (
    "newsId",
    "news_id",
    "articleId",
    "article_id",
    "itemId",
    "item_id",
    "contentId",
    "content_id",
    "id",
)
SOHU_AUTHOR_ID_JSON_KEYS = (
    "authorId",
    "author_id",
    "mpId",
    "mp_id",
    "userId",
    "user_id",
    "mediaId",
    "media_id",
    "xpt",
    "xptId",
)
SOHU_FEED_CONTAINER_KEYS = (
    "data",
    "list",
    "items",
    "articles",
    "articleList",
    "article_list",
    "feed",
    "feeds",
    "news",
    "results",
    "rows",
    "records",
    "pcArticleVOS",
    "articleVOS",
)


def _now_text() -> str:
    return _local_naive_now().strftime("%Y-%m-%d %H:%M:%S")


def _article_ts_now() -> str:
    return local_today().isoformat()


def _local_naive_now() -> datetime:
    return local_now().replace(tzinfo=None)


def _read_state() -> dict[str, Any]:
    with _lock:
        state_path = _state_path()
        try:
            if state_path.exists():
                with open(state_path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                return data if isinstance(data, dict) else {}
        except Exception:
            pass
        return {}


def _write_state(state: dict[str, Any]) -> None:
    with _lock:
        state_path = _state_path()
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(state_path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(state, handle, ensure_ascii=False, indent=2)
                os.replace(tmp, state_path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as exc:
            print(f"[AccountCrawler] 写入状态失败: {exc}")


def _state_path() -> Path:
    resolved_default = resolve_app_path("logs/account_crawl_state.json")
    if ACCOUNT_CRAWL_STATE_FILE != resolved_default:
        return ACCOUNT_CRAWL_STATE_FILE
    return account_scoped_path("logs/account_crawl_state.json", fallback=resolved_default)


def _state_lock_file() -> Path:
    state_path = _state_path()
    return state_path.parent / f".{state_path.name}.lock"


def _ensure_url_scheme(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        return raw
    return f"https://{raw}"


def _make_account_id(url: str) -> str:
    return hashlib.sha1(str(url or "").strip().encode("utf-8")).hexdigest()[:16]


def infer_account_platform(url: str) -> str:
    normalized = _ensure_url_scheme(url)
    try:
        parsed = urlparse(normalized)
    except Exception:
        return "unknown"
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if "cnblogs.com" in host:
        return "cnblogs"
    if "sohu.com" in host:
        return "sohu"
    if "toutiao.com" in host or "toutiaohao.com" in host:
        return "toutiao"
    if "zhihu.com" in host:
        return "zhihu"
    if "rsshub" in host or path.endswith((".xml", ".rss")) or "/rss" in path:
        return "rss"
    return "unknown"


def normalize_account_crawling_settings(
    config: dict[str, Any] | None,
    *,
    include_state: bool = False,
) -> dict[str, Any]:
    cfg = dict((config or {}).get("account_crawling", {}) or {})
    state = _read_state() if include_state else {}
    raw_accounts = cfg.get("accounts") if isinstance(cfg.get("accounts"), list) else []
    accounts: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for index, raw_account in enumerate(raw_accounts):
        if not isinstance(raw_account, dict):
            continue
        url = _ensure_url_scheme(str(raw_account.get("url") or "").strip())
        if not url:
            continue
        account_id = str(raw_account.get("id") or "").strip() or _make_account_id(url)
        if account_id in seen_ids:
            account_id = f"{account_id}-{index}"
        seen_ids.add(account_id)
        platform = str(raw_account.get("platform") or "").strip().lower() or infer_account_platform(url)
        if platform not in PLATFORM_LABELS or platform == "unknown":
            platform = infer_account_platform(url)
        name = str(raw_account.get("name") or "").strip()
        account_state = state.get("accounts", {}).get(account_id, {}) if include_state else {}
        account_payload = {
            "id": account_id,
            "name": name,
            "url": url,
            "platform": platform,
            "platform_label": PLATFORM_LABELS.get(platform, platform or "未知平台"),
            "enabled": bool(raw_account.get("enabled", True)),
        }
        if include_state:
            account_payload.update(
                {
                    "last_crawled_at": str(account_state.get("last_crawled_at") or "").strip(),
                    "last_status": str(account_state.get("last_status") or "").strip(),
                    "last_message": str(account_state.get("last_message") or "").strip(),
                    "last_added_count": int(account_state.get("last_added_count") or 0),
                    "last_fetched_count": int(account_state.get("last_fetched_count") or 0),
                    "last_excluded_count": int(account_state.get("last_excluded_count") or 0),
                    "last_excluded_links": list(account_state.get("last_excluded_links") or [])[:10]
                    if isinstance(account_state.get("last_excluded_links"), list)
                    else [],
                }
            )
        accounts.append(account_payload)

    frequency_minutes = _safe_int(cfg.get("frequency_minutes"), DEFAULT_FREQUENCY_MINUTES)
    frequency_minutes = min(24 * 60, max(10, frequency_minutes))
    max_items = _safe_int(cfg.get("max_items_per_account"), DEFAULT_MAX_ITEMS_PER_ACCOUNT)
    max_items = min(200, max(5, max_items))
    rsshub_base_urls = _normalize_rsshub_base_urls(
        cfg.get("rsshub_base_urls"),
        cfg.get("rsshub_base_url"),
    )
    rsshub_base_url = rsshub_base_urls[0]

    payload = {
        "enabled": bool(cfg.get("enabled", False)),
        "frequency_minutes": frequency_minutes,
        "rsshub_base_url": rsshub_base_url,
        "rsshub_base_urls": rsshub_base_urls,
        "max_items_per_account": max_items,
        "accounts": accounts,
    }
    if include_state:
        payload["last_auto_run_at"] = str(state.get("last_auto_run_at") or "").strip()
        payload["last_manual_run_at"] = str(state.get("last_manual_run_at") or "").strip()
        payload["last_excluded_count"] = int(state.get("last_excluded_count") or 0)
        payload["last_excluded_links"] = list(state.get("last_excluded_links") or [])[:20] if isinstance(state.get("last_excluded_links"), list) else []
        payload["excluded_links"] = get_excluded_article_urls(limit=200)
    return payload


def should_run_scheduled_crawl(config: dict[str, Any] | None) -> bool:
    settings = normalize_account_crawling_settings(config)
    if not settings["enabled"]:
        return False
    if not any(account.get("enabled") for account in settings["accounts"]):
        return False
    state = _read_state()
    last_run = _parse_state_datetime(state.get("last_auto_run_at"))
    if last_run is None:
        return True
    return _local_naive_now() - last_run >= timedelta(minutes=int(settings["frequency_minutes"]))


def _parse_state_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _normalize_rsshub_base_urls(*values: Any) -> list[str]:
    bases: list[str] = []

    def add(raw_value: Any) -> None:
        if raw_value is None:
            return
        if isinstance(raw_value, (list, tuple, set)):
            for item in raw_value:
                add(item)
            return
        text = str(raw_value or "").strip()
        if not text:
            return
        for part in re.split(r"[\s,，;；]+", text):
            normalized = _ensure_url_scheme(part.strip()).rstrip("/")
            if normalized and normalized not in bases:
                bases.append(normalized)

    for value in values:
        add(value)
    add(DEFAULT_RSSHUB_BASE_URL)
    add(DEFAULT_RSSHUB_FALLBACK_BASE_URLS)
    return bases


def crawl_account_articles(
    config: dict[str, Any] | None,
    *,
    account_ids: list[str] | None = None,
    trigger: str = "manual",
) -> dict[str, Any]:
    settings = normalize_account_crawling_settings(config)
    requested_ids = {str(item or "").strip() for item in (account_ids or []) if str(item or "").strip()}
    accounts = [
        account
        for account in settings["accounts"]
        if account.get("enabled") and (not requested_ids or account.get("id") in requested_ids)
    ]
    if not accounts:
        return {
            "ok": False,
            "message": "暂无可抓取的账号，请先在账号页添加并启用账号主页链接",
            "fetched_count": 0,
            "added_count": 0,
            "duplicate_count": 0,
            "excluded_count": 0,
            "excluded_links": [],
            "results": [],
        }

    state = _read_state()
    settings["_rsshub_success_bases"] = dict(state.get("rsshub_success_bases", {}) or {})
    settings["_account_rsshub_success_bases"] = dict(state.get("account_rsshub_success_bases", {}) or {})
    settings["_rsshub_health"] = dict(state.get("rsshub_health", {}) or {})
    state_accounts = state.setdefault("accounts", {})
    summary = {
        "ok": True,
        "message": "",
        "fetched_count": 0,
        "added_count": 0,
        "duplicate_count": 0,
        "excluded_count": 0,
        "excluded_links": [],
        "results": [],
    }
    max_workers = max(1, min(MAX_ACCOUNT_WORKERS, len(accounts)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(_crawl_single_account, account, settings, config or {}): account
            for account in accounts
        }
        ordered_results: dict[str, dict[str, Any]] = {}
        for future in as_completed(future_map):
            account = future_map[future]
            try:
                ordered_results[str(account.get("id") or "")] = future.result()
            except Exception as exc:
                ordered_results[str(account.get("id") or "")] = {
                    "ok": False,
                    "account_id": account.get("id", ""),
                    "account_name": account.get("name", "") or account.get("platform_label", ""),
                    "platform": account.get("platform", ""),
                    "platform_label": account.get("platform_label", ""),
                    "source": "",
                    "rsshub_base_url": "",
                    "rsshub_attempts": [],
                    "message": str(exc) or "抓取失败",
                    "fetched_count": 0,
                    "added_count": 0,
                    "duplicate_count": 0,
                    "excluded_count": 0,
                    "excluded_links": [],
                    "articles": [],
                }

    for account in accounts:
        result = ordered_results.get(str(account.get("id") or "")) or {}
        summary["results"].append(result)
        summary["fetched_count"] += int(result.get("fetched_count") or 0)
        summary["added_count"] += int(result.get("added_count") or 0)
        summary["duplicate_count"] += int(result.get("duplicate_count") or 0)
        summary["excluded_count"] += int(result.get("excluded_count") or 0)
        for excluded_link in result.get("excluded_links") or []:
            if isinstance(excluded_link, dict):
                summary["excluded_links"].append(dict(excluded_link))
        state_accounts[account["id"]] = {
            "last_crawled_at": _now_text(),
            "last_status": "success" if result.get("ok") else "error",
            "last_message": str(result.get("message") or "").strip(),
            "last_added_count": int(result.get("added_count") or 0),
            "last_fetched_count": int(result.get("fetched_count") or 0),
            "last_excluded_count": int(result.get("excluded_count") or 0),
            "last_excluded_links": list(result.get("excluded_links") or [])[:10],
        }
        rsshub_base_url = str(result.get("rsshub_base_url") or "").strip()
        platform = str(result.get("platform") or account.get("platform") or "").strip().lower()
        for attempt in result.get("rsshub_attempts") or []:
            if isinstance(attempt, dict):
                _apply_rsshub_attempt_to_state(state, attempt)
        if rsshub_base_url and platform:
            state.setdefault("rsshub_success_bases", {})[platform] = rsshub_base_url
            state.setdefault("account_rsshub_success_bases", {})[str(account.get("id") or "")] = rsshub_base_url

    if trigger == "auto":
        state["last_auto_run_at"] = _now_text()
    else:
        state["last_manual_run_at"] = _now_text()
    state["last_excluded_count"] = int(summary.get("excluded_count") or 0)
    state["last_excluded_links"] = list(summary.get("excluded_links") or [])[:20]
    _merge_and_write_state_after_crawl(
        state,
        accounts=accounts,
        trigger=trigger,
        summary=summary,
    )

    failed_count = sum(1 for item in summary["results"] if not item.get("ok"))
    excluded_suffix = f"，已排除 {summary['excluded_count']} 篇" if summary["excluded_count"] else ""
    if summary["added_count"]:
        summary["message"] = f"抓取完成，新增 {summary['added_count']} 篇，重复 {summary['duplicate_count']} 篇{excluded_suffix}"
    elif failed_count == len(summary["results"]):
        summary["ok"] = False
        summary["message"] = "抓取失败，未获取到可导入文章"
    else:
        summary["message"] = f"抓取完成，暂无新增文章，重复 {summary['duplicate_count']} 篇{excluded_suffix}"
    return summary


def _crawl_single_account(account: dict[str, Any], settings: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    articles: list[dict[str, Any]] = []
    source = ""
    rsshub_base_url = ""
    rsshub_attempts: list[dict[str, Any]] = []
    error = ""
    try:
        articles, source, rsshub_base_url, rsshub_attempts = _fetch_account_latest_articles(account, settings)
    except AccountFetchError as exc:
        error = str(exc)
        rsshub_attempts = exc.rsshub_attempts
        print(f"[AccountCrawler] 抓取失败 {account.get('url')}: {exc}")
    except Exception as exc:
        error = str(exc)
        print(f"[AccountCrawler] 抓取失败 {account.get('url')}: {exc}")

    max_items = int(settings.get("max_items_per_account") or DEFAULT_MAX_ITEMS_PER_ACCOUNT)
    articles = _dedupe_article_items(articles)[:max_items]
    added_count = 0
    duplicate_count = 0
    excluded_count = 0
    excluded_links: list[dict[str, Any]] = []
    imported: list[dict[str, Any]] = []
    for item in articles:
        stored, status = _store_article_item(item, account, config, source)
        if status == "excluded":
            excluded_count += 1
            excluded_links.append({
                "url": normalize_article_url(str(item.get("url") or "").strip()),
                "title": _clean_text(str(item.get("title") or "").strip()),
                "account_id": account.get("id", ""),
                "account_name": account.get("name", "") or account.get("platform_label", ""),
            })
            continue
        if status == "duplicate":
            duplicate_count += 1
            continue
        if stored:
            added_count += 1
            imported.append(stored)

    ok = bool(articles) or not error
    message = (
        f"获取 {len(articles)} 条，新增 {added_count} 条" + (f"，排除 {excluded_count} 条" if excluded_count else "")
        if ok
        else error or "未获取到列表"
    )
    return {
        "ok": ok,
        "account_id": account.get("id", ""),
        "account_name": account.get("name", "") or account.get("platform_label", ""),
        "platform": account.get("platform", ""),
        "platform_label": account.get("platform_label", ""),
        "source": source,
        "rsshub_base_url": rsshub_base_url,
        "rsshub_attempts": rsshub_attempts,
        "message": message,
        "fetched_count": len(articles),
        "added_count": added_count,
        "duplicate_count": duplicate_count,
        "excluded_count": excluded_count,
        "excluded_links": excluded_links[:10],
        "articles": imported[:10],
    }


def _store_article_item(
    item: dict[str, Any],
    account: dict[str, Any],
    config: dict[str, Any],
    source: str,
) -> tuple[dict[str, Any] | None, str]:
    url = normalize_article_url(str(item.get("url") or "").strip())
    title = _clean_text(str(item.get("title") or "").strip())
    if not url or not title:
        return None, "skipped"
    if is_article_url_excluded(url):
        return None, "excluded"

    platform = str(account.get("platform") or "").strip().lower()
    platform_label = account.get("platform_label") or PLATFORM_LABELS.get(platform, "")
    account_name = str(account.get("name") or "").strip()
    media_name = _resolve_account_article_media_name(url, platform, str(platform_label or ""))
    published_at = _normalize_published_at(item.get("published_at") or item.get("published"))
    published_ts = _published_sort_value(
        item.get("_published_sort")
        or item.get("published_at")
        or item.get("published")
        or published_at
    )
    fetch_method = f"account_crawl:{source or account.get('platform', 'unknown')}"
    account_context = {
        "account_id": account.get("id", ""),
        "account_name": account_name,
        "account_url": account.get("url", ""),
        "account_platform": platform,
        "account_platform_label": platform_label,
    }
    existing_article = find_article_by_url(url)
    if existing_article:
        patch: dict[str, Any] = {}
        for key, value in account_context.items():
            text_value = str(value or "").strip()
            if text_value and text_value != str(existing_article.get(key, "") or "").strip():
                patch[key] = text_value
        current_media_name = str(existing_article.get("media_name", "") or "").strip()
        if media_name and (
            not current_media_name
            or (account_name and current_media_name == account_name)
            or (platform in ACCOUNT_PLATFORM_MEDIA_NAMES and current_media_name != media_name)
        ):
            patch["media_name"] = media_name
        current_platform = str(existing_article.get("platform", "") or "").strip()
        if platform_label and (not current_platform or (account_name and current_platform == account_name)):
            patch["platform"] = platform_label
        if title and title != str(existing_article.get("title") or ""):
            patch["title"] = title
        if published_at and (
            published_at != str(existing_article.get("published_at") or "")
            or published_at != str(existing_article.get("ts") or "")[:10]
        ):
            patch["published_at"] = published_at
            patch["ts"] = published_at
        if published_ts > 0 and published_ts != _safe_int(existing_article.get("published_ts"), 0):
            patch["published_ts"] = published_ts
        if fetch_method and fetch_method != str(existing_article.get("fetch_method") or ""):
            patch["fetch_method"] = fetch_method
        if patch:
            if "title" in patch:
                refreshed_article = {**existing_article, **patch, "url": url}
                analyzed = analyze_article_matches(title, config, article=refreshed_article)
                patch["matched_tasks"] = analyzed.get("matched_tasks") or []
                patch["match_reasons"] = analyzed.get("match_reasons") or {}
                patch["unmatched_reason"] = analyzed.get("unmatched_reason", "") or ""
            update_article(str(existing_article.get("id") or ""), patch)
        return None, "duplicate"

    draft_article = {
        "url": url,
        "title": title,
        "platform": platform_label,
        "media_name": media_name,
        "media_type": "selfmedia",
        "excerpt": "",
        "fetch_method": fetch_method,
        "ts": published_at or _article_ts_now(),
        "published_at": published_at,
        "published_ts": published_ts if published_ts > 0 else 0,
        **account_context,
    }
    analyzed = analyze_article_matches(title, config, article=draft_article)
    article = add_article(
        {
            **draft_article,
            "matched_tasks": analyzed.get("matched_tasks") or [],
            "match_reasons": analyzed.get("match_reasons") or {},
            "unmatched_reason": analyzed.get("unmatched_reason", "") or "",
        }
    )
    return article, "added"


def _resolve_account_article_media_name(url: str, platform: str, platform_label: str = "") -> str:
    normalized_platform = str(platform or "").strip().lower()
    if normalized_platform in ACCOUNT_PLATFORM_MEDIA_NAMES:
        return ACCOUNT_PLATFORM_MEDIA_NAMES[normalized_platform]
    return resolve_media_name(url) or str(platform_label or "").strip()


def _fetch_account_latest_articles(
    account: dict[str, Any],
    settings: dict[str, Any],
) -> tuple[list[dict[str, Any]], str, str, list[dict[str, Any]]]:
    platform = str(account.get("platform") or infer_account_platform(account.get("url", ""))).strip().lower()
    max_items = int(settings.get("max_items_per_account") or DEFAULT_MAX_ITEMS_PER_ACCOUNT)
    if platform == "toutiao":
        try:
            browser_articles = _fetch_toutiao_articles_with_browser(account, max_items=max_items)
            if browser_articles:
                return browser_articles, "browser_api", "", []
        except Exception as exc:
            print(f"[AccountCrawler] 头条浏览器接口抓取失败，改用 RSSHub 兜底 {account.get('url')}: {exc}")
    elif platform == "sohu":
        try:
            browser_articles = _fetch_sohu_articles_with_browser(account, max_items=max_items)
            if browser_articles:
                return browser_articles, "browser_api", "", []
        except Exception as exc:
            print(f"[AccountCrawler] 搜狐浏览器接口抓取失败，改用接口/RSSHub 兜底 {account.get('url')}: {exc}")

    candidates = _build_source_candidates(account, settings)
    last_error = ""
    rsshub_attempts: list[dict[str, Any]] = []
    for candidate in candidates:
        rsshub_base_url = str(candidate.get("rsshub_base") or "").strip()
        try:
            response = requests.get(
                candidate["url"],
                headers=_build_headers(candidate["url"]),
                timeout=FETCH_TIMEOUT,
                allow_redirects=True,
            )
            response.raise_for_status()
            text = _decode_response(response)
            content_type = str(response.headers.get("content-type") or "").lower()
            parser_type = candidate.get("type", "auto")
            articles: list[dict[str, Any]]
            if parser_type == "rss" or "xml" in content_type or _looks_like_xml(text):
                articles = _parse_rss_feed(text, response.url, account.get("platform", ""), account=account)
            elif "json" in content_type or _looks_like_json(text):
                articles = _extract_articles_from_json(
                    json.loads(text),
                    response.url,
                    account.get("platform", ""),
                    account=account,
                )
            else:
                articles = _extract_articles_from_html(text, response.url, account.get("platform", ""), account=account)
            if rsshub_base_url:
                rsshub_attempts.append(
                    _build_rsshub_attempt(
                        platform=platform,
                        base_url=rsshub_base_url,
                        ok=True,
                        status_code=response.status_code,
                        item_count=len(articles),
                    )
                )
            if articles:
                if rsshub_base_url:
                    _remember_runtime_rsshub_success(platform, rsshub_base_url, account_id=account.get("id"))
                if last_error:
                    print(f"[AccountCrawler] 备用源成功 {response.url}")
                return articles, str(candidate.get("type") or "auto"), rsshub_base_url, rsshub_attempts
        except Exception as exc:
            last_error = str(exc)
            if rsshub_base_url:
                rsshub_attempts.append(
                    _build_rsshub_attempt(
                        platform=platform,
                        base_url=rsshub_base_url,
                        ok=False,
                        status_code=_extract_http_status_code(exc),
                        error=last_error,
                    )
                )
            print(f"[AccountCrawler] 候选源失败，继续尝试下一个 {candidate.get('url')}: {exc}")
    if last_error:
        raise AccountFetchError(last_error, rsshub_attempts=rsshub_attempts)
    return [], "none", "", rsshub_attempts


def _build_source_candidates(account: dict[str, Any], settings: dict[str, Any]) -> list[dict[str, str]]:
    url = _ensure_url_scheme(account.get("url", ""))
    platform = str(account.get("platform") or infer_account_platform(url)).strip().lower()
    rsshub_bases = _rsshub_base_candidates(
        settings,
        platform=platform,
        account_id=str(account.get("id") or "").strip(),
    )
    max_items = int(settings.get("max_items_per_account") or DEFAULT_MAX_ITEMS_PER_ACCOUNT)
    candidates: list[dict[str, str]] = []

    def add(candidate_url: str, parser_type: str = "auto", *, rsshub_base: str = "") -> None:
        normalized = _ensure_url_scheme(candidate_url)
        if not normalized:
            return
        if all(item["url"] != normalized for item in candidates):
            candidate = {"url": normalized, "type": parser_type}
            if rsshub_base:
                candidate["rsshub_base"] = rsshub_base
            candidates.append(candidate)

    if platform == "cnblogs":
        add(_build_cnblogs_rss_url(url), "rss")
    elif platform == "sohu":
        xpt = _extract_sohu_xpt(url)
        if xpt:
            add(f"https://v2.sohu.com/public-api/feed?scene=PROFILE&sceneId={quote(xpt, safe='')}&page=1&size={max_items}", "json")
            for base_url in rsshub_bases:
                add(f"{base_url}/sohu/mp/{quote(xpt, safe='')}", "rss", rsshub_base=base_url)
    elif platform == "toutiao":
        token = _extract_toutiao_token(url)
        if token:
            for base_url in rsshub_bases:
                add(f"{base_url}/toutiao/user/token/{quote(token, safe='')}", "rss", rsshub_base=base_url)
        user_id = _extract_toutiao_user_id(url)
        if user_id:
            add(
                "https://www.toutiao.com/api/pc/list/user/feed"
                f"?category=profile_all&utm_source=toutiao&widen=1&max_behot_time=0&user_id={quote(user_id, safe='')}",
                "json",
            )
    elif platform == "zhihu":
        people_id = _extract_zhihu_people_id(url)
        if people_id:
            for base_url in rsshub_bases:
                add(f"{base_url}/zhihu/people/activities/{quote(people_id, safe='')}", "rss", rsshub_base=base_url)
                add(f"{base_url}/zhihu/xhu/people/activities/{quote(people_id, safe='')}", "rss", rsshub_base=base_url)

    if _allow_direct_account_page_fallback(account, platform, url):
        add(url, "auto")
    return candidates


def _allow_direct_account_page_fallback(account: dict[str, Any], platform: str, url: str) -> bool:
    normalized_platform = str(platform or "").strip().lower()
    if normalized_platform in {"toutiao", "zhihu"}:
        return False
    if normalized_platform != "sohu":
        return True
    return bool(_sohu_account_author_id(account, fallback_url=url))


def _fetch_toutiao_articles_with_browser(account: dict[str, Any], *, max_items: int) -> list[dict[str, Any]]:
    url = _ensure_url_scheme(account.get("url", ""))
    token = _extract_toutiao_token(url)
    if not token:
        return []

    from core.browser_runtime import resolve_system_browser_executable

    browser_executable = resolve_system_browser_executable()
    if not browser_executable:
        raise RuntimeError("未找到可用浏览器")

    with _account_browser_lock:
        from patchright.sync_api import sync_playwright

        feed_items: list[dict[str, Any]] = []
        has_more = True
        feed_user_id = _extract_toutiao_user_id(url)
        next_cursor = "0"
        seen_cursors: set[str] = set()
        max_pages = min(
            TOUTIAO_BROWSER_MAX_PAGES,
            max(1, (max_items + TOUTIAO_BROWSER_PAGE_SIZE - 1) // TOUTIAO_BROWSER_PAGE_SIZE + 1),
        )
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=browser_executable,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = None
            try:
                context = browser.new_context(
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                )
                page = context.new_page()

                def collect_feed_payload(data: Any, response_url: str = "") -> int:
                    nonlocal has_more, feed_user_id, next_cursor
                    if not isinstance(data, dict):
                        return 0
                    if str(data.get("message") or "").lower() not in {"", "success"}:
                        return 0
                    items = _extract_toutiao_feed_payload_items(data)
                    if items:
                        feed_items.extend(items)
                    payload_has_more = _extract_toutiao_feed_has_more(data)
                    if payload_has_more is not None:
                        has_more = payload_has_more
                    elif items:
                        has_more = True
                    cursor = _extract_toutiao_feed_cursor(data, items)
                    if cursor:
                        next_cursor = cursor
                    if response_url:
                        parsed_response = urlparse(response_url)
                        response_user_id = str((parse_qs(parsed_response.query).get("user_id") or [""])[0] or "").strip()
                        if response_user_id:
                            feed_user_id = response_user_id
                    return len(items)

                def on_response(response: Any) -> None:
                    if "/api/pc/list/user/feed" not in response.url:
                        return
                    try:
                        data = response.json()
                    except Exception:
                        return
                    collect_feed_payload(data, response.url)

                page.on("response", on_response)
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(6500)
                for _ in range(max_pages):
                    parsed_count = len(_parse_toutiao_feed_items(feed_items))
                    if parsed_count >= max_items:
                        break
                    if feed_user_id:
                        cursor = str(next_cursor or "0").strip() or "0"
                        if cursor in seen_cursors:
                            break
                        seen_cursors.add(cursor)
                        api_url = (
                            "https://www.toutiao.com/api/pc/list/user/feed"
                            f"?category=profile_all&utm_source=toutiao&widen=1"
                            f"&max_behot_time={quote(cursor, safe='')}"
                            f"&user_id={quote(feed_user_id, safe='')}"
                        )
                        try:
                            data = page.evaluate(
                                """async (apiUrl) => {
                                  const response = await fetch(apiUrl, {
                                    credentials: "include",
                                    headers: {
                                      "accept": "application/json, text/plain, */*",
                                      "x-requested-with": "XMLHttpRequest"
                                    }
                                  });
                                  return await response.json();
                                }""",
                                api_url,
                            )
                            added = collect_feed_payload(data, api_url)
                            if added <= 0 or not has_more:
                                break
                            page.wait_for_timeout(450)
                            continue
                        except Exception:
                            pass
                    if not has_more and feed_items:
                        break
                    page.mouse.wheel(0, 1800)
                    page.wait_for_timeout(2200)
            finally:
                _close_browser_resources(context, browser)

    return _parse_toutiao_feed_items(feed_items)[:max_items]


def _extract_toutiao_feed_payload_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    payload = data.get("data")
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "list", "articles"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _extract_toutiao_feed_has_more(data: dict[str, Any]) -> bool | None:
    for key in ("has_more", "hasMore", "has_next", "hasNext"):
        if key in data:
            return bool(data.get(key))
    payload = data.get("data")
    if isinstance(payload, dict):
        for key in ("has_more", "hasMore", "has_next", "hasNext"):
            if key in payload:
                return bool(payload.get(key))
    return None


def _extract_toutiao_feed_cursor(data: dict[str, Any], items: list[dict[str, Any]]) -> str:
    candidates: list[Any] = []
    for key in ("max_behot_time", "next_max_behot_time", "nextCursor", "maxCursor"):
        candidates.append(data.get(key))
    next_payload = data.get("next")
    if isinstance(next_payload, dict):
        for key in ("max_behot_time", "next_max_behot_time", "cursor", "maxCursor"):
            candidates.append(next_payload.get(key))
    payload = data.get("data")
    if isinstance(payload, dict):
        for key in ("max_behot_time", "next_max_behot_time", "cursor", "maxCursor"):
            candidates.append(payload.get(key))
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text and re.fullmatch(r"\d{5,13}", text):
            return text
    published_values = [
        _published_sort_value(_first_text(item, ("behot_time", "publish_time", "publishTime", "create_time", "datetime")))
        for item in items
        if isinstance(item, dict)
    ]
    positive_values = [value for value in published_values if value > 0]
    return str(min(positive_values)) if positive_values else ""


def _parse_toutiao_feed_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    articles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        article_id = _first_text(item, ("group_id", "item_id", "id", "article_id"))
        url = _first_text(item, ("article_url", "url", "share_url", "display_url"))
        if not url and article_id:
            url = f"https://www.toutiao.com/article/{article_id}/"
        title = _first_text(item, ("title", "display_title", "article_title", "abstract_title"))
        published = _first_text(item, ("behot_time", "publish_time", "publishTime", "create_time", "datetime"))
        normalized_url = normalize_article_url(urljoin("https://www.toutiao.com/", _unescape_json_url(url)))
        if not normalized_url or not title:
            continue
        dedupe_key = article_id or normalized_url
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        articles.append(
            {
                "title": title,
                "url": normalized_url,
                "published_at": published,
                "_published_sort": _published_sort_value(published),
            }
        )
    articles.sort(key=lambda item: int(item.get("_published_sort") or 0), reverse=True)
    return articles


def _fetch_sohu_articles_with_browser(account: dict[str, Any], *, max_items: int) -> list[dict[str, Any]]:
    url = _ensure_url_scheme(account.get("url", ""))
    xpt = _extract_sohu_xpt(url)
    if not xpt:
        return []

    from core.browser_runtime import resolve_system_browser_executable

    browser_executable = resolve_system_browser_executable()
    if not browser_executable:
        raise RuntimeError("未找到可用浏览器")

    with _account_browser_lock:
        from patchright.sync_api import sync_playwright

        feed_items: list[dict[str, Any]] = []
        max_pages = min(
            SOHU_BROWSER_MAX_PAGES,
            max(1, (max_items + SOHU_BROWSER_PAGE_SIZE - 1) // SOHU_BROWSER_PAGE_SIZE + 1),
        )
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=browser_executable,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = None
            try:
                context = browser.new_context(
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                )
                page = context.new_page()
                page.set_default_timeout(ACCOUNT_BROWSER_TIMEOUT_MS)

                def collect_feed_payload(data: Any) -> int:
                    items = _extract_sohu_feed_payload_items(data)
                    if items:
                        feed_items.extend(items)
                    return len(items)

                def on_response(response: Any) -> None:
                    if "/public-api/feed" not in str(response.url or ""):
                        return
                    try:
                        data = response.json()
                    except Exception:
                        return
                    collect_feed_payload(data)

                page.on("response", on_response)
                page.goto(url, wait_until="domcontentloaded", timeout=ACCOUNT_BROWSER_TIMEOUT_MS)
                page.wait_for_timeout(2500)
                for page_number in range(1, max_pages + 1):
                    parsed_count = len(_parse_sohu_feed_items(feed_items, account_xpt=xpt))
                    if parsed_count >= max_items:
                        break
                    api_url = _build_sohu_feed_api_url(xpt, page_number, min(SOHU_BROWSER_PAGE_SIZE, max_items))
                    try:
                        data = page.evaluate(
                            """async (apiUrl) => {
                              const response = await fetch(apiUrl, {
                                credentials: "include",
                                headers: {
                                  "accept": "application/json, text/plain, */*",
                                  "x-requested-with": "XMLHttpRequest"
                                }
                              });
                              return await response.json();
                            }""",
                            api_url,
                        )
                        added = collect_feed_payload(data)
                        if added <= 0 and page_number > 1:
                            break
                        page.wait_for_timeout(350)
                    except Exception:
                        break
            finally:
                _close_browser_resources(context, browser)

    return _parse_sohu_feed_items(feed_items, account_xpt=xpt)[:max_items]


def _build_sohu_feed_api_url(xpt: str, page: int, size: int) -> str:
    return (
        "https://v2.sohu.com/public-api/feed"
        f"?scene=PROFILE&sceneId={quote(str(xpt or '').strip(), safe='')}"
        f"&page={max(1, int(page or 1))}&size={max(1, int(size or SOHU_BROWSER_PAGE_SIZE))}"
    )


def _close_browser_resources(*resources: Any) -> None:
    for resource in resources:
        if resource is None:
            continue
        try:
            resource.close()
        except Exception:
            pass


def _extract_sohu_feed_payload_items(data: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen_ids: set[int] = set()

    def add_item(candidate: Any) -> None:
        if not isinstance(candidate, dict) or not _looks_like_sohu_feed_node(candidate):
            return
        object_id = id(candidate)
        if object_id in seen_ids:
            return
        seen_ids.add(object_id)
        items.append(candidate)

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(value, list):
            article_like = [item for item in value if _looks_like_sohu_feed_node(item)]
            if article_like:
                for item in article_like:
                    add_item(item)
                return
            for item in value:
                if isinstance(item, (dict, list)):
                    walk(item, depth + 1)
            return
        if not isinstance(value, dict):
            return
        if _looks_like_sohu_feed_node(value):
            add_item(value)
            return
        for key in SOHU_FEED_CONTAINER_KEYS:
            if key in value:
                walk(value.get(key), depth + 1)

    walk(data)
    return items


def _looks_like_sohu_feed_node(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    title = _first_text(node, SOHU_TITLE_JSON_KEYS)
    if not title:
        return False
    url = _first_text(node, SOHU_URL_JSON_KEYS)
    article_id = _first_text(node, SOHU_ARTICLE_ID_JSON_KEYS)
    if url and _looks_like_article_url("sohu", url):
        return True
    return bool(_normalize_sohu_article_id(article_id))


def _parse_sohu_feed_items(items: list[dict[str, Any]], *, account_xpt: str = "", base_url: str = "https://www.sohu.com/") -> list[dict[str, Any]]:
    articles: list[dict[str, Any]] = []
    seen: set[str] = set()
    account_author_id = _normalize_sohu_author_id(account_xpt)
    for item in items:
        if not isinstance(item, dict):
            continue
        title = _first_text(item, SOHU_TITLE_JSON_KEYS)
        article_id = _normalize_sohu_article_id(_first_text(item, SOHU_ARTICLE_ID_JSON_KEYS))
        item_author_id = _normalize_sohu_author_id(_first_text(item, SOHU_AUTHOR_ID_JSON_KEYS))
        if account_author_id and item_author_id and item_author_id != account_author_id:
            continue
        author_id = item_author_id or account_author_id
        raw_url = _first_text(item, SOHU_URL_JSON_KEYS)
        if article_id and (not raw_url or not _sohu_article_url_has_author(raw_url)) and author_id:
            raw_url = _build_sohu_article_url(article_id, author_id)
        elif not raw_url and article_id:
            raw_url = _build_sohu_article_url(article_id, "")
        url = normalize_article_url(urljoin(base_url, _unescape_json_url(raw_url)))
        if not title or not _looks_like_article_url("sohu", url, account_xpt=account_author_id):
            continue
        published = _first_text(item, SOHU_PUBLISHED_JSON_KEYS)
        dedupe_key = article_id or url
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        articles.append(
            {
                "title": title,
                "url": url,
                "published_at": published,
                "_published_sort": _published_sort_value(published),
            }
        )
    articles.sort(key=lambda item: int(item.get("_published_sort") or 0), reverse=True)
    return articles


def _normalize_sohu_article_id(value: Any) -> str:
    text = _clean_text(str(value or ""))
    match = re.search(r"(?<!\d)(\d{6,}(?:_\d{3,})?)(?!\d)", text)
    return match.group(1) if match else ""


def _normalize_sohu_author_id(value: Any) -> str:
    text = _clean_text(str(value or ""))
    if not text:
        return ""
    if re.fullmatch(r"\d{3,}", text):
        return text
    labelled_match = re.search(
        r"(?:authorId|author_id|mpId|mp_id|userId|user_id|mediaId|media_id|sceneId|xpt)\D{0,16}(\d{3,})",
        text,
        re.IGNORECASE,
    )
    if labelled_match:
        return labelled_match.group(1)
    article_suffix_match = re.search(r"/a/\d{6,}_(\d{3,})(?:\D|$)", text)
    if article_suffix_match:
        return article_suffix_match.group(1)
    if not re.search(r"[A-Za-z]", text):
        match = re.search(r"(?<!\d)(\d{3,})(?!\d)", text)
        return match.group(1) if match else ""
    return ""


def _build_sohu_article_url(article_id: str, author_id: str) -> str:
    normalized_article_id = _normalize_sohu_article_id(article_id)
    normalized_author_id = _normalize_sohu_author_id(author_id)
    if not normalized_article_id:
        return ""
    if "_" in normalized_article_id or not normalized_author_id:
        return f"https://www.sohu.com/a/{normalized_article_id}"
    return f"https://www.sohu.com/a/{normalized_article_id}_{normalized_author_id}"


def _sohu_article_url_ids(url: str) -> tuple[str, str]:
    parsed = urlparse(_ensure_url_scheme(url))
    match = re.search(r"/a/(\d{6,})(?:_(\d{3,}))?", parsed.path)
    if not match:
        return "", ""
    return match.group(1), match.group(2) or ""


def _sohu_article_url_has_author(url: str) -> bool:
    return bool(_sohu_article_url_ids(url)[1])


def _rsshub_base_candidates(
    settings: dict[str, Any],
    *,
    platform: str = "",
    account_id: str = "",
) -> list[str]:
    bases = _normalize_rsshub_base_urls(
        settings.get("rsshub_base_urls"),
        settings.get("rsshub_base_url"),
    )
    preferred = _preferred_rsshub_bases(settings, platform=platform, account_id=account_id)
    if not preferred:
        if platform == "toutiao" and DEFAULT_RSSHUB_BASE_URL in bases and len(bases) > 1:
            bases = [base for base in bases if base != DEFAULT_RSSHUB_BASE_URL] + [DEFAULT_RSSHUB_BASE_URL]
        return _order_rsshub_bases_by_health(bases, settings, platform=platform, preferred=[])
    ordered = [base for base in preferred if base in bases] + [base for base in bases if base not in preferred]
    return _order_rsshub_bases_by_health(ordered, settings, platform=platform, preferred=preferred)


def _order_rsshub_bases_by_health(
    bases: list[str],
    settings: dict[str, Any],
    *,
    platform: str = "",
    preferred: list[str] | None = None,
) -> list[str]:
    health_by_base = _rsshub_health_for_platform(settings, platform)
    preferred_set = set(preferred or [])
    active: list[tuple[int, str]] = []
    cooled: list[tuple[int, str]] = []
    for index, base_url in enumerate(bases):
        health = health_by_base.get(base_url, {})
        score = _rsshub_base_score(base_url, health, preferred_set=preferred_set, index=index)
        if _rsshub_base_in_cooldown(health):
            cooled.append((score, base_url))
        else:
            active.append((score, base_url))
    if active:
        return [base_url for _, base_url in sorted(active)]
    return [base_url for _, base_url in sorted(cooled)]


def _rsshub_health_for_platform(settings: dict[str, Any], platform: str) -> dict[str, dict[str, Any]]:
    health = settings.get("_rsshub_health")
    if not isinstance(health, dict):
        return {}
    platform_health = health.get(str(platform or "").strip().lower())
    return platform_health if isinstance(platform_health, dict) else {}


def _rsshub_base_score(
    base_url: str,
    health: dict[str, Any],
    *,
    preferred_set: set[str],
    index: int,
) -> int:
    preferred_bonus = -100000 if base_url in preferred_set else 0
    success_bonus = -100 * _safe_int(health.get("success_count"), 0)
    failure_penalty = 10 * _safe_int(health.get("consecutive_failures"), 0)
    return preferred_bonus + success_bonus + failure_penalty + index


def _rsshub_base_in_cooldown(health: dict[str, Any]) -> bool:
    disabled_until = _parse_state_datetime(health.get("disabled_until"))
    return bool(disabled_until and disabled_until > _local_naive_now())


def _preferred_rsshub_bases(settings: dict[str, Any], *, platform: str = "", account_id: str = "") -> list[str]:
    preferred: list[str] = []

    def add(value: Any) -> None:
        normalized = _ensure_url_scheme(str(value or "").strip()).rstrip("/")
        if normalized and normalized not in preferred:
            preferred.append(normalized)

    account_key = str(account_id or "").strip()
    platform_key = str(platform or "").strip().lower()
    account_success_bases = settings.get("_account_rsshub_success_bases")
    platform_success_bases = settings.get("_rsshub_success_bases")
    if account_key:
        add(_rsshub_success_cache.get(f"account:{account_key}"))
        if isinstance(account_success_bases, dict):
            add(account_success_bases.get(account_key))
    if platform_key:
        add(_rsshub_success_cache.get(f"platform:{platform_key}"))
        if isinstance(platform_success_bases, dict):
            add(platform_success_bases.get(platform_key))
    return preferred


def _remember_runtime_rsshub_success(platform: str, base_url: str, *, account_id: Any = "") -> None:
    normalized = _ensure_url_scheme(str(base_url or "").strip()).rstrip("/")
    if not normalized:
        return
    platform_key = str(platform or "").strip().lower()
    account_key = str(account_id or "").strip()
    with _lock:
        if account_key:
            _rsshub_success_cache[f"account:{account_key}"] = normalized
        if platform_key:
            _rsshub_success_cache[f"platform:{platform_key}"] = normalized


def _merge_and_write_state_after_crawl(
    state: dict[str, Any],
    *,
    accounts: list[dict[str, Any]],
    trigger: str,
    summary: dict[str, Any],
) -> None:
    with _lock:
        latest = _read_state()
        latest_accounts = latest.setdefault("accounts", {})
        crawled_account_ids = [
            str(account.get("id") or "").strip()
            for account in accounts
            if str(account.get("id") or "").strip()
        ]
        source_accounts = state.get("accounts") if isinstance(state.get("accounts"), dict) else {}
        for account_id in crawled_account_ids:
            account_state = source_accounts.get(account_id)
            if isinstance(account_state, dict):
                latest_accounts[account_id] = dict(account_state)

        for key in ("rsshub_success_bases", "account_rsshub_success_bases", "rsshub_health"):
            source_bucket = state.get(key) if isinstance(state.get(key), dict) else {}
            if not source_bucket:
                continue
            target_bucket = latest.setdefault(key, {})
            if isinstance(target_bucket, dict):
                target_bucket.update(source_bucket)
            else:
                latest[key] = dict(source_bucket)

        if trigger == "auto":
            latest["last_auto_run_at"] = str(state.get("last_auto_run_at") or _now_text())
        else:
            latest["last_manual_run_at"] = str(state.get("last_manual_run_at") or _now_text())
        latest["last_excluded_count"] = int(summary.get("excluded_count") or 0)
        latest["last_excluded_links"] = list(summary.get("excluded_links") or [])[:20]
        _write_state(latest)


def _build_rsshub_attempt(
    *,
    platform: str,
    base_url: str,
    ok: bool,
    status_code: int | None = None,
    error: str = "",
    item_count: int = 0,
) -> dict[str, Any]:
    return {
        "platform": str(platform or "").strip().lower(),
        "base_url": _ensure_url_scheme(str(base_url or "").strip()).rstrip("/"),
        "ok": bool(ok),
        "status_code": int(status_code) if status_code else 0,
        "error": str(error or "").strip()[:300],
        "item_count": int(item_count or 0),
        "checked_at": _now_text(),
    }


def _extract_http_status_code(exc: Exception) -> int:
    response = getattr(exc, "response", None)
    try:
        return int(getattr(response, "status_code", 0) or 0)
    except Exception:
        return 0


def _apply_rsshub_attempt_to_state(state: dict[str, Any], attempt: dict[str, Any]) -> None:
    platform = str(attempt.get("platform") or "").strip().lower() or "unknown"
    base_url = _ensure_url_scheme(str(attempt.get("base_url") or "").strip()).rstrip("/")
    if not base_url:
        return

    platform_health = state.setdefault("rsshub_health", {}).setdefault(platform, {})
    health = platform_health.setdefault(base_url, {})
    now_text = _now_text()
    if attempt.get("ok"):
        health["success_count"] = _safe_int(health.get("success_count"), 0) + 1
        health["consecutive_failures"] = 0
        health["last_success_at"] = now_text
        health["last_status_code"] = int(attempt.get("status_code") or 0)
        health["last_item_count"] = int(attempt.get("item_count") or 0)
        health["last_error"] = ""
        health["disabled_until"] = ""
        return

    consecutive_failures = _safe_int(health.get("consecutive_failures"), 0) + 1
    status_code = int(attempt.get("status_code") or 0)
    health["failure_count"] = _safe_int(health.get("failure_count"), 0) + 1
    health["consecutive_failures"] = consecutive_failures
    health["last_failure_at"] = now_text
    health["last_status_code"] = status_code
    health["last_error"] = str(attempt.get("error") or "").strip()[:300]
    cooldown_minutes = _rsshub_cooldown_minutes(status_code, consecutive_failures)
    if cooldown_minutes > 0:
        health["disabled_until"] = (
            _local_naive_now() + timedelta(minutes=cooldown_minutes)
        ).strftime("%Y-%m-%d %H:%M:%S")


def _rsshub_cooldown_minutes(status_code: int, consecutive_failures: int) -> int:
    if status_code in RSSHUB_HARD_FAILURE_STATUS_CODES:
        return RSSHUB_HARD_COOLDOWN_MINUTES
    if status_code == 429:
        return RSSHUB_RATE_LIMIT_COOLDOWN_MINUTES
    if consecutive_failures >= RSSHUB_SOFT_FAILURES_BEFORE_COOLDOWN:
        multiplier = max(1, consecutive_failures - RSSHUB_SOFT_FAILURES_BEFORE_COOLDOWN + 1)
        return min(RSSHUB_MAX_COOLDOWN_MINUTES, RSSHUB_SOFT_COOLDOWN_MINUTES * multiplier)
    return 0


def _build_cnblogs_rss_url(url: str) -> str:
    parsed = urlsplit(_ensure_url_scheme(url))
    path_parts = [part for part in parsed.path.split("/") if part]
    if path_parts and path_parts[-1].lower() == "rss":
        return urlunsplit((parsed.scheme or "https", parsed.netloc, parsed.path, "", ""))
    username = path_parts[0] if path_parts else ""
    if username:
        return urlunsplit((parsed.scheme or "https", parsed.netloc, f"/{username}/rss", "", ""))
    return url


def _extract_sohu_xpt(url: str) -> str:
    parsed = urlparse(_ensure_url_scheme(url))
    qs = parse_qs(parsed.query)
    for key in ("xpt", "id", "authorId", "author_id"):
        value = str((qs.get(key) or [""])[0] or "").strip()
        if value:
            return value
    match = re.search(r"/(?:profile|user|mp)/([^/?#]+)", parsed.path, re.IGNORECASE)
    if match:
        return str(match.group(1) or "").strip()
    _, author_id = _sohu_article_url_ids(url)
    return author_id


def _sohu_account_author_id(account: dict[str, Any] | None, *, fallback_url: str = "") -> str:
    account_url = str((account or {}).get("url") or fallback_url or "").strip()
    return _normalize_sohu_author_id(_extract_sohu_xpt(account_url))


def _extract_toutiao_token(url: str) -> str:
    parsed = urlparse(_ensure_url_scheme(url))
    qs = parse_qs(parsed.query)
    for key in ("token", "user_token"):
        value = str((qs.get(key) or [""])[0] or "").strip()
        if value:
            return value

    path_parts = [part for part in parsed.path.split("/") if part]
    lowered = [part.lower() for part in path_parts]
    for marker in ("token", "user_token"):
        if marker in lowered:
            index = lowered.index(marker)
            if index + 1 < len(path_parts):
                return str(path_parts[index + 1] or "").strip()

    match = re.search(r"/(?:c/)?user/token/([^/?#]+)", parsed.path, re.IGNORECASE)
    return str(match.group(1) or "").strip() if match else ""


def _extract_toutiao_user_id(url: str) -> str:
    parsed = urlparse(_ensure_url_scheme(url))
    qs = parse_qs(parsed.query)
    for key in ("user_id", "userId", "uid", "id"):
        value = str((qs.get(key) or [""])[0] or "").strip()
        if value and re.fullmatch(r"\d{5,}", value):
            return value
    patterns = (
        r"/c/user/(\d{5,})(?:/|$)",
        r"/user/(\d{5,})(?:/|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, parsed.path)
        if match:
            return str(match.group(1) or "").strip().strip("/")
    return ""


def _extract_zhihu_people_id(url: str) -> str:
    parsed = urlparse(_ensure_url_scheme(url))
    match = re.search(r"/people/([^/?#]+)", parsed.path)
    return str(match.group(1) or "").strip() if match else ""


def _build_headers(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
    }
    if origin:
        headers["Referer"] = f"{origin}/"
    return headers


def _decode_response(response: requests.Response) -> str:
    if response.encoding:
        return response.text
    try:
        response.encoding = response.apparent_encoding or "utf-8"
    except Exception:
        response.encoding = "utf-8"
    return response.text


def _looks_like_xml(text: str) -> bool:
    head = str(text or "").lstrip()[:200].lower()
    return head.startswith("<?xml") or "<rss" in head or "<feed" in head


def _looks_like_json(text: str) -> bool:
    head = str(text or "").lstrip()[:1]
    return head in {"{", "["}


def _parse_rss_feed(
    xml_text: str,
    base_url: str,
    platform: str = "",
    *,
    account: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    root = ElementTree.fromstring(xml_text.encode("utf-8"))
    items = [
        element
        for element in root.iter()
        if _xml_local_name(element.tag) in {"item", "entry"}
    ]
    normalized_platform = str(platform or "").strip().lower()
    account_xpt = _extract_sohu_xpt(str((account or {}).get("url") or "")) if normalized_platform == "sohu" else ""
    articles: list[dict[str, Any]] = []
    for item in items:
        title = _xml_child_text(item, {"title"})
        link = _xml_child_text(item, {"link"})
        if not link:
            for child in list(item):
                if _xml_local_name(child.tag) == "link":
                    link = str(child.attrib.get("href") or "").strip()
                    if link:
                        break
        published = _xml_child_text(item, {"pubDate", "published", "updated", "date", "created"})
        url = urljoin(base_url, link)
        if normalized_platform == "sohu" and not _looks_like_article_url("sohu", url, account_xpt=account_xpt):
            continue
        if title and link:
            articles.append(
                {
                    "title": title,
                    "url": url,
                    "published_at": published,
                }
            )
    return articles


def _xml_local_name(tag: Any) -> str:
    text = str(tag or "")
    return text.rsplit("}", 1)[-1]


def _xml_child_text(element: ElementTree.Element, names: set[str]) -> str:
    wanted = {name.lower() for name in names}
    for child in element.iter():
        if child is element:
            continue
        if _xml_local_name(child.tag).lower() in wanted:
            return _clean_text("".join(child.itertext()))
    return ""


def _extract_articles_from_json(
    data: Any,
    base_url: str,
    platform: str,
    *,
    account: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if str(platform or "").strip().lower() == "sohu":
        account_xpt = _extract_sohu_xpt(str((account or {}).get("url") or ""))
        return _parse_sohu_feed_items(
            _extract_sohu_feed_payload_items(data),
            account_xpt=account_xpt,
            base_url=base_url,
        )

    articles: list[dict[str, Any]] = []
    for node in _iter_json_dicts(data):
        title = _first_text(
            node,
            ("title", "display_title", "article_title", "open_title", "headline", "abstract_title", "name"),
        )
        url = _first_text(
            node,
            ("url", "article_url", "display_url", "source_url", "share_url", "item_url", "link", "target_url"),
        )
        article_id = _first_text(node, ("article_id", "item_id", "group_id", "id", "groupId"))
        if not url and article_id:
            url = _build_article_url_from_id(platform, article_id)
        published = _first_text(
            node,
            PUBLISHED_JSON_KEYS,
        )
        if title and url and _looks_like_article_url(platform, url):
            articles.append(
                {
                    "title": title,
                    "url": urljoin(base_url, _unescape_json_url(url)),
                    "published_at": published,
                }
            )
    return articles


def _iter_json_dicts(data: Any):
    if isinstance(data, dict):
        yield data
        for value in data.values():
            yield from _iter_json_dicts(value)
    elif isinstance(data, list):
        for item in data:
            yield from _iter_json_dicts(item)


def _first_text(node: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = node.get(key)
        if isinstance(value, (str, int, float)):
            text = _clean_text(str(value))
            if text:
                return text
        if isinstance(value, dict):
            nested = _first_text(value, ("text", "url", "title"))
            if nested:
                return nested
    return ""


def _build_article_url_from_id(platform: str, article_id: str) -> str:
    normalized = str(article_id or "").strip()
    if not normalized:
        return ""
    if platform == "toutiao":
        return f"https://www.toutiao.com/article/{normalized}/"
    if platform == "sohu":
        return f"https://www.sohu.com/a/{normalized}"
    return ""


def _unescape_json_url(url: str) -> str:
    return str(url or "").replace("\\/", "/")


class _AnchorParser(HTMLParser):
    def __init__(self, base_url: str, platform: str, account: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.base_url = base_url
        self.platform = platform
        self.account_xpt = _sohu_account_author_id(account) if platform == "sohu" else ""
        self.anchors: list[dict[str, str]] = []
        self._href_stack: list[str] = []
        self._text_stack: list[list[str]] = []
        self._ignored_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._ignored_stack.append(tag)
            return
        if tag == "a":
            attrs_dict = {key: value or "" for key, value in attrs}
            self._href_stack.append(attrs_dict.get("href", ""))
            self._text_stack.append([])

    def handle_endtag(self, tag: str) -> None:
        if self._ignored_stack and tag == self._ignored_stack[-1]:
            self._ignored_stack.pop()
            return
        if tag == "a" and self._href_stack:
            href = self._href_stack.pop()
            text_parts = self._text_stack.pop() if self._text_stack else []
            title = _clean_text(" ".join(text_parts))
            url = urljoin(self.base_url, href)
            if title and _looks_like_article_url(self.platform, url, account_xpt=self.account_xpt):
                self.anchors.append({"title": title, "url": url, "published_at": ""})

    def handle_data(self, data: str) -> None:
        if self._ignored_stack:
            return
        if self._text_stack:
            self._text_stack[-1].append(data)


def _extract_articles_from_html(
    html_text: str,
    base_url: str,
    platform: str,
    *,
    account: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if str(platform or "").strip().lower() == "sohu" and account is not None and not _sohu_account_author_id(account):
        return []
    articles = _extract_json_like_articles_from_html(html_text, base_url, platform, account=account)
    parser = _AnchorParser(base_url, platform, account=account)
    try:
        parser.feed(html_text[:500000])
    except Exception:
        pass
    articles.extend(parser.anchors)
    return articles


def _extract_json_like_articles_from_html(
    html_text: str,
    base_url: str,
    platform: str,
    *,
    account: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    articles: list[dict[str, Any]] = []
    text = str(html_text or "")
    account_xpt = _sohu_account_author_id(account) if platform == "sohu" else ""
    patterns = (
        r'"(?:title|article_title|display_title)"\s*:\s*"([^"]{4,160})"(.{0,800}?)"(?:url|article_url|source_url|share_url)"\s*:\s*"([^"]+)"',
        r'"(?:url|article_url|source_url|share_url)"\s*:\s*"([^"]+)"(.{0,800}?)"(?:title|article_title|display_title)"\s*:\s*"([^"]{4,160})"',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE | re.DOTALL):
            if pattern.startswith('"(?:title'):
                title, segment, url = match.group(1), match.group(2), match.group(3)
            else:
                url, segment, title = match.group(1), match.group(2), match.group(3)
            title = _clean_text(title)
            url = _unescape_json_url(url)
            if title and _looks_like_article_url(platform, url, account_xpt=account_xpt):
                articles.append(
                    {
                        "title": title,
                        "url": urljoin(base_url, url),
                        "published_at": _extract_published_at_from_text_segment(segment),
                    }
                )
    return articles


def _looks_like_article_url(platform: str, url: str, *, account_xpt: str = "") -> bool:
    raw = str(url or "").strip()
    if not raw or raw.startswith(("javascript:", "#")):
        return False
    lowered = raw.lower()
    if any(blocked in lowered for blocked in ("/login", "/signin", "/comment", "/tag/", "/search")):
        return False
    if platform == "cnblogs":
        return any(marker in lowered for marker in ("/p/", "/articles/", "/archive/"))
    if platform == "zhihu":
        return any(marker in lowered for marker in ("/p/", "/question/", "/answer/"))
    if platform == "toutiao":
        return (
            bool(re.search(r"/(?:article/|i)\d{8,}", lowered))
            or "group/" in lowered
            or ("toutiao.com" in lowered and bool(re.search(r"/\d{8,}", lowered)))
        )
    if platform == "sohu":
        parsed = urlparse(_ensure_url_scheme(raw))
        host = parsed.netloc.lower()
        if host and not host.endswith("sohu.com"):
            return False
        article_id, author_id = _sohu_article_url_ids(raw)
        if not article_id:
            return False
        account_author_id = _normalize_sohu_author_id(account_xpt)
        if account_author_id:
            return author_id == account_author_id
        return True
    return bool(re.search(r"/\d{4}/|/p/|/article|/post|/archives?", lowered))


def _dedupe_article_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in items:
        url = normalize_article_url(str(item.get("url") or "").strip())
        title = _clean_text(str(item.get("title") or "").strip())
        if not url or not title:
            continue
        key = url or title
        if key in seen:
            continue
        seen.add(key)
        published_at = _normalize_published_at(item.get("published_at") or item.get("published"))
        deduped.append(
            {
                **item,
                "url": url,
                "title": title,
                "published_at": published_at or _extract_published_at_from_url(url),
                "_published_sort": _published_sort_value(
                    item.get("_published_sort")
                    or item.get("published_at")
                    or item.get("published")
                    or published_at
                    or _extract_published_at_from_url(url)
                ),
            }
        )
    deduped.sort(key=lambda item: int(item.get("_published_sort") or 0), reverse=True)
    return deduped


def _extract_published_at_from_text_segment(segment: str) -> str:
    text = _clean_text(str(segment or ""))
    if not text:
        return ""
    key_pattern = "|".join(re.escape(key) for key in PUBLISHED_JSON_KEYS)
    patterns = (
        rf'(?:{key_pattern})["\']?\s*[:=]\s*["\']?([^"\',;}}\]]{{4,80}})',
        r'(?:发布时间|发布日期|发稿时间|发表时间|发布于|更新时间|更新于|时间|日期)[:：\s]*([0-9]{4}[年./-]\s*[0-9]{1,2}[月./-]\s*[0-9]{1,2}[日]?(?:\s+[0-9]{1,2}:[0-9]{1,2}(?::[0-9]{1,2})?)?)',
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        published_at = _normalize_published_at(match.group(1))
        if published_at:
            return published_at
    return ""


def _extract_published_at_from_url(url: str) -> str:
    text = str(url or "")
    patterns = (
        r"[?&](?:date|publish(?:ed)?_?date|pubdate)=([0-9]{4})[-/]?([0-9]{1,2})[-/]?([0-9]{1,2})",
        r"(?:^|[/_-])t([0-9]{4})([0-9]{2})([0-9]{2})(?:[_./-]|$)",
        r"(?:^|[/_-])([0-9]{4})([0-9]{2})([0-9]{2})(?:[_./-]|$)",
        r"/([0-9]{4})/([0-9]{1,2})/([0-9]{1,2})(?:/|$)",
        r"/([0-9]{4})([0-9]{2})(?:/|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            year = int(match.group(1))
            month = int(match.group(2))
            day = int(match.group(3)) if len(match.groups()) >= 3 and match.group(3) else 1
            if 1990 <= year <= 2100:
                return datetime(year, month, day).strftime("%Y-%m-%d")
        except Exception:
            continue
    return ""


def _clean_text(value: str) -> str:
    text = unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\\n", " ").replace("\\t", " ").replace("\\r", " ")
    text = text.replace('\\"', '"').replace("\\/", "/")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _timestamp_to_date_text(timestamp: int | float) -> str:
    try:
        return datetime.fromtimestamp(float(timestamp), tz=CHINA_TIMEZONE).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _published_sort_value(value: Any) -> int:
    text = _clean_text(str(value or ""))
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
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(CHINA_TIMEZONE)
        return int(parsed.timestamp())
    except Exception:
        pass
    normalized = _normalize_published_at(text)
    if normalized:
        try:
            return int(datetime.strptime(normalized, "%Y-%m-%d").replace(tzinfo=CHINA_TIMEZONE).timestamp())
        except Exception:
            pass
    return 0


def _normalize_published_at(value: Any) -> str:
    text = _clean_text(str(value or ""))
    if not text:
        return ""
    if re.fullmatch(r"\d{13}", text):
        return _timestamp_to_date_text(int(text) / 1000)
    if re.fullmatch(r"\d{10}", text):
        return _timestamp_to_date_text(int(text))
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(CHINA_TIMEZONE)
        return parsed.strftime("%Y-%m-%d")
    except Exception:
        pass
    normalized = text.replace("T", " ").replace("Z", " ")
    normalized = re.sub(
        r"([0-9]{4})\s*年\s*([0-9]{1,2})\s*月\s*([0-9]{1,2})\s*日?",
        r"\1-\2-\3",
        normalized,
    )
    normalized = re.sub(r"([0-9]{4})\.([0-9]{1,2})\.([0-9]{1,2})", r"\1-\2-\3", normalized)
    normalized = normalized.replace("/", "-")
    normalized = re.sub(r"(?<=\d{2}:\d{2}:\d{2})\.\d+", "", normalized)
    compact_match = re.search(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})(?!\d)", normalized)
    if compact_match:
        try:
            return datetime(
                int(compact_match.group(1)),
                int(compact_match.group(2)),
                int(compact_match.group(3)),
            ).strftime("%Y-%m-%d")
        except Exception:
            pass
    for fmt, width in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            parsed = datetime.strptime(normalized[:width], fmt)
            return parsed.strftime("%Y-%m-%d")
        except Exception:
            continue
    match = re.search(r"((?:19|20)\d{2}-\d{1,2}-\d{1,2})", normalized)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y-%m-%d").strftime("%Y-%m-%d")
        except Exception:
            pass
    return ""
