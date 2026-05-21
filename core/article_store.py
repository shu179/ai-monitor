"""
文章录入存储模块

负责：
- 管理录入文章列表（logs/articles.json）
- 管理域名→媒体类型记忆（logs/domain_overrides.json）
- 管理域名→媒体名记忆（logs/domain_media_names.json）
- 提供关键词→任务组匹配、文章数统计等查询接口
"""

import copy
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from .app_paths import resolve_app_path
from .file_lock import CrossProcessRLock
from .history import normalize_platform_id
from .local_account_space import account_scoped_path
from .sqlite_json_store import MISSING, SQLiteJsonDocumentStore
from .time_utils import local_now

DEFAULT_ARTICLES_FILE = resolve_app_path("logs/articles.json")
DEFAULT_DOMAIN_OVERRIDES_FILE = resolve_app_path("logs/domain_overrides.json")
DEFAULT_DOMAIN_MEDIA_NAMES_FILE = resolve_app_path("logs/domain_media_names.json")
DEFAULT_EXCLUDED_ARTICLE_URLS_FILE = resolve_app_path("logs/excluded_article_urls.json")
DEFAULT_LOCAL_STORE_DB_FILE = resolve_app_path("logs/local_store.sqlite3")
DEFAULT_ARTICLE_SHADOW_DB_FILE = resolve_app_path("logs/article_history_shadow.sqlite3")
DEFAULT_ARTICLE_STORE_DB_FILE = resolve_app_path("logs/article_store.sqlite3")

ARTICLES_FILE = DEFAULT_ARTICLES_FILE
DOMAIN_OVERRIDES_FILE = DEFAULT_DOMAIN_OVERRIDES_FILE
DOMAIN_MEDIA_NAMES_FILE = DEFAULT_DOMAIN_MEDIA_NAMES_FILE
EXCLUDED_ARTICLE_URLS_FILE = DEFAULT_EXCLUDED_ARTICLE_URLS_FILE
LOCAL_STORE_DB_FILE = DEFAULT_LOCAL_STORE_DB_FILE
ARTICLE_SHADOW_DB_FILE = DEFAULT_ARTICLE_SHADOW_DB_FILE
ARTICLE_STORE_DB_FILE = DEFAULT_ARTICLE_STORE_DB_FILE

MAX_REFERENCE_EVENTS_PER_TASK = 500
MAX_EXCLUDED_ARTICLE_URLS = 5000
STORAGE_BACKEND_ENV = "AIBRANDMONITOR_STORAGE_BACKEND"
ARTICLE_STORE_BACKEND_ENV = "AIBRANDMONITOR_ARTICLE_STORE_BACKEND"
ARTICLE_SHADOW_WRITE_ENV = "AIBRANDMONITOR_ARTICLE_SQLITE_SHADOW_WRITES"
SMALL_DOCUMENT_CACHE_TTL_SECONDS = 1.0

ARTICLE_STORE_BACKEND_JSON_VALUES = {"json", "off", "disabled", "file", "files"}
ARTICLE_STORE_BACKEND_SQLITE_VALUES = {"sqlite", "sqlite3", "db", "database"}
ARTICLE_STORE_BACKEND_AUTO_VALUES = {"", "auto", "sqlite_auto", "sqlite_primary_auto", "auto_sqlite"}
ARTICLE_STORE_SQLITE_MIGRATION_MIN_INTERVAL_SECONDS = 30.0
ARTICLE_STORE_SQLITE_ERROR_LIMIT = 3
ARTICLE_STORE_SQLITE_COOLDOWN_SECONDS = 60.0

ARTICLE_MATCH_REFRESH_BATCH_SIZE_ENV = "AIBRANDMONITOR_ARTICLE_MATCH_REFRESH_BATCH_SIZE"
ARTICLE_MATCH_REFRESH_SLEEP_SECONDS_ENV = "AIBRANDMONITOR_ARTICLE_MATCH_REFRESH_SLEEP_SECONDS"
DEFAULT_MATCH_REFRESH_BATCH_SIZE = 200
DEFAULT_MATCH_REFRESH_SLEEP_SECONDS = 0.02

_small_document_cache: dict[str, tuple[float, object]] = {}
_article_store_backend_health_lock = threading.RLock()
_article_store_backend_health: dict[str, object] = {}
_article_store_migration_lock = threading.RLock()
_article_store_migration_state: dict[str, object] = {}
_article_store_migration_threads: list[threading.Thread] = []
_match_refresh_worker_lock = threading.RLock()
_match_refresh_worker_thread: threading.Thread | None = None

_BUILTIN_MEDIA_SITE_PROFILES: dict[str, dict[str, str]] = {
    "redsh.com": {"name": "红商网", "media_type": "authority"},
    "gc-zb.com": {"name": "招标与采购网", "media_type": "authority"},
    "xnnews.com.cn": {"name": "咸宁新闻网", "media_type": "authority"},
    "lfnews.cn": {"name": "廊坊新闻网", "media_type": "authority"},
    "bozhou.cn": {"name": "亳州新闻网", "media_type": "authority"},
    "redhongan.com": {"name": "红安网", "media_type": "authority"},
}

# 权威媒体域名白名单（内置初始值，可通过手动切换覆盖）
AUTHORITY_DOMAINS: set = {
    "news.cn",
    "xinhua.net", "xinhuanet.com",
    "people.com.cn", "peopledaily.com.cn",
    "cctv.com", "央视网",
    "thepaper.cn",
    "36kr.com",
    "jiemian.com",
    "caixin.com",
    "chinadaily.com.cn",
    "bjnews.com.cn",
    "21jingji.com",
    "yicai.com",
    "sina.com.cn", "sina.cn",
    "163.com",
    "sohu.com",
    "ifeng.com",
    "qq.com",
    "tencent.com",
    "huanqiu.com",
    "guancha.cn",
    "huxiu.com",
    "latepost.com",
    "cls.cn",
    "stcn.com",
    "cnstock.com",
    "nbd.com.cn",
    "eeo.com.cn",
    "cb.com.cn",
    "cyzone.cn",
    "tmtpost.com",
    "donews.com",
    "leiphone.com",
    "geekpark.net",
    "itjuzi.com",
    "jfdaily.com",
    "nandu.com",
    "infzm.com",
    "yangtse.com",
    "xinmin.cn",
    "southcn.com",
    "ynet.com",
    "ce.cn",
    "china.com.cn",
    "stdaily.com",
    "workercn.cn",
    "cankaoxiaoxi.com",
    "qianlong.com",
    "dingzhoudaily.com",
    "dzxww.cn",
    "techcrunch.com",
    "reuters.com",
    "bloomberg.com",
    "ft.com",
    "wsj.com",
    "nytimes.com",
    "zaobao.com",
    "chinanews.com.cn",
    "gmw.cn",
    "cnr.cn",
    "gov.cn",
    "cnjiayu.com.cn",
}
AUTHORITY_DOMAINS.update(
    domain
    for domain, profile in _BUILTIN_MEDIA_SITE_PROFILES.items()
    if profile.get("media_type") == "authority"
)

SELF_MEDIA_DOMAIN_SUFFIXES: set = {
    "mp.weixin.qq.com",
    "weixin.qq.com",
    "baijiahao.baidu.com",
    "author.baidu.com",
    "mbd.baidu.com",
    "toutiaohao.com",
    "mp.toutiao.com",
    "zhuanlan.zhihu.com",
    "zhihu.com",
    "weibo.com",
    "k.sina.com.cn",
    "douyin.com",
    "iesdouyin.com",
    "ixigua.com",
    "xiaohongshu.com",
    "xiaohongshu.cn",
    "bilibili.com",
    "kuaishou.com",
    "mp.sohu.com",
    "dy.163.com",
    "om.qq.com",
    "kuaibao.qq.com",
    "mp.dayu.com",
    "a.mp.uc.cn",
    "mparticle.uc.cn",
    "mp.yidianzixun.com",
    "yidianzixun.com",
    "cnblogs.com",
    "csdn.net",
    "juejin.cn",
    "jianshu.com",
    "douban.com",
    "toutiao.io",
    "toutiao.com",
}

SELF_MEDIA_PATH_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("sohu.com", ("/a/", "/mp/")),
    ("163.com", ("/dy/article/", "/article/", "/news/article/")),
    ("qq.com", ("/rain/a/", "/omn/")),
    ("inews.qq.com", ("/a/",)),
    ("uc.cn", ("/article.html", "/article/")),
    ("sina.com.cn", ("/article_",)),
    ("zhihu.com", ("/p/", "/question/")),
    ("douban.com", ("/note/", "/people/")),
    ("jianshu.com", ("/p/",)),
)

def _article_store_lock_file() -> Path:
    articles_path = _articles_file()
    return articles_path.with_name("article_store.lock")


_lock = CrossProcessRLock(_article_store_lock_file)


def _article_now_minute_text() -> str:
    return local_now().strftime("%Y-%m-%d %H:%M")


def _article_now_second_text() -> str:
    return local_now().strftime("%Y-%m-%d %H:%M:%S")

_MEDIA_NAME_BY_DOMAIN_SUFFIX: dict[str, str] = {
    "mp.weixin.qq.com": "微信公众号",
    "weixin.qq.com": "微信公众号",
    "baijiahao.baidu.com": "百家号",
    "toutiaohao.com": "头条号",
    "news.cn": "新华网",
    "xinhua.net": "新华网",
    "xinhuanet.com": "新华网",
    "people.com.cn": "人民网",
    "peopledaily.com.cn": "人民日报",
    "cnjiayu.com.cn": "嘉鱼网",
    "cctv.com": "央视网",
    "thepaper.cn": "澎湃新闻",
    "36kr.com": "36氪",
    "jiemian.com": "界面新闻",
    "caixin.com": "财新网",
    "chinadaily.com.cn": "中国日报",
    "bjnews.com.cn": "新京报",
    "21jingji.com": "21世纪经济报道",
    "yicai.com": "第一财经",
    "sina.com.cn": "新浪",
    "sina.cn": "新浪",
    "163.com": "网易",
    "sohu.com": "搜狐",
    "ifeng.com": "凤凰网",
    "qq.com": "腾讯新闻",
    "tencent.com": "腾讯新闻",
    "huanqiu.com": "环球网",
    "guancha.cn": "观察者网",
    "huxiu.com": "虎嗅",
    "latepost.com": "晚点LatePost",
    "zhihu.com": "知乎",
    "toutiao.com": "今日头条",
    "baidu.com": "百度",
    "weibo.com": "微博",
    "douyin.com": "抖音",
    "iesdouyin.com": "抖音",
    "ixigua.com": "西瓜视频",
    "xiaohongshu.com": "小红书",
    "xiaohongshu.cn": "小红书",
    "bilibili.com": "哔哩哔哩",
    "kuaishou.com": "快手",
    "acfun.cn": "AcFun",
    "yidianzixun.com": "一点资讯",
    "smzdm.com": "什么值得买",
    "sspai.com": "少数派",
    "juejin.cn": "掘金",
    "cnblogs.com": "博客园",
    "csdn.net": "CSDN",
    "cls.cn": "财联社",
    "stcn.com": "证券时报",
    "cnstock.com": "上海证券报",
    "nbd.com.cn": "每日经济新闻",
    "eeo.com.cn": "经济观察网",
    "cb.com.cn": "中国经营网",
    "cyzone.cn": "创业邦",
    "tmtpost.com": "钛媒体",
    "donews.com": "DoNews",
    "leiphone.com": "雷锋网",
    "geekpark.net": "极客公园",
    "itjuzi.com": "IT桔子",
    "jfdaily.com": "上观新闻",
    "nandu.com": "南方都市报",
    "infzm.com": "南方周末",
    "yangtse.com": "扬子晚报",
    "xinmin.cn": "新民晚报",
    "southcn.com": "南方网",
    "ynet.com": "北青网",
    "ce.cn": "中国经济网",
    "china.com.cn": "中国网",
    "stdaily.com": "科技日报",
    "workercn.cn": "中工网",
    "cankaoxiaoxi.com": "参考消息",
    "qianlong.com": "千龙网",
    "dingzhoudaily.com": "定州日报",
    "dzxww.cn": "定州新闻网",
    "rednet.cn": "红网",
    "thecover.cn": "封面新闻",
    "cqnews.net": "华龙网",
    "gzdaily.cn": "广州日报新花城",
    "dzwww.com": "大众网",
    "dzrb.dzng.com": "大众日报",
    "jiqizhixin.com": "机器之心",
    "qbitai.com": "量子位",
    "pingwest.com": "品玩",
    "krj.com.cn": "金融界",
    "jrj.com.cn": "金融界",
    "wallstreetcn.com": "华尔街见闻",
    "techcrunch.com": "TechCrunch",
    "theverge.com": "The Verge",
    "verge.com": "The Verge",
    "wired.com": "WIRED",
    "medium.com": "Medium",
    "uxdesign.cc": "UX Collective",
    "smashingmagazine.com": "Smashing Magazine",
    "reuters.com": "Reuters",
    "bloomberg.com": "Bloomberg",
    "ft.com": "Financial Times",
    "wsj.com": "The Wall Street Journal",
    "nytimes.com": "The New York Times",
    "zaobao.com": "联合早报",
    "chinanews.com.cn": "中国新闻网",
    "gmw.cn": "光明网",
    "cnr.cn": "央广网",
    "gov.cn": "中国政府网",
}
_MEDIA_NAME_BY_DOMAIN_SUFFIX.update({
    domain: str(profile.get("name") or "").strip()
    for domain, profile in _BUILTIN_MEDIA_SITE_PROFILES.items()
    if str(profile.get("name") or "").strip()
})


def _articles_file() -> Path:
    if ARTICLES_FILE != DEFAULT_ARTICLES_FILE:
        return ARTICLES_FILE
    return account_scoped_path("logs/articles.json", fallback=DEFAULT_ARTICLES_FILE)


def _domain_overrides_file() -> Path:
    if DOMAIN_OVERRIDES_FILE != DEFAULT_DOMAIN_OVERRIDES_FILE:
        return DOMAIN_OVERRIDES_FILE
    return account_scoped_path("logs/domain_overrides.json", fallback=DEFAULT_DOMAIN_OVERRIDES_FILE)


def _domain_media_names_file() -> Path:
    if DOMAIN_MEDIA_NAMES_FILE != DEFAULT_DOMAIN_MEDIA_NAMES_FILE:
        return DOMAIN_MEDIA_NAMES_FILE
    return account_scoped_path("logs/domain_media_names.json", fallback=DEFAULT_DOMAIN_MEDIA_NAMES_FILE)


def _excluded_article_urls_file() -> Path:
    if EXCLUDED_ARTICLE_URLS_FILE != DEFAULT_EXCLUDED_ARTICLE_URLS_FILE:
        return EXCLUDED_ARTICLE_URLS_FILE
    return account_scoped_path("logs/excluded_article_urls.json", fallback=DEFAULT_EXCLUDED_ARTICLE_URLS_FILE)


def get_articles_file_path() -> Path:
    return _articles_file()


def get_domain_overrides_file_path() -> Path:
    return _domain_overrides_file()


def get_domain_media_names_file_path() -> Path:
    return _domain_media_names_file()


def get_excluded_article_urls_file_path() -> Path:
    return _excluded_article_urls_file()


def _local_store_db_file() -> Path:
    if LOCAL_STORE_DB_FILE != DEFAULT_LOCAL_STORE_DB_FILE:
        return LOCAL_STORE_DB_FILE
    return account_scoped_path("logs/local_store.sqlite3", fallback=DEFAULT_LOCAL_STORE_DB_FILE)


def _article_store_db_file() -> Path:
    if ARTICLE_STORE_DB_FILE != DEFAULT_ARTICLE_STORE_DB_FILE:
        return ARTICLE_STORE_DB_FILE
    if ARTICLES_FILE != DEFAULT_ARTICLES_FILE:
        return ARTICLES_FILE.parent / "article_store.sqlite3"
    return account_scoped_path("logs/article_store.sqlite3", fallback=DEFAULT_ARTICLE_STORE_DB_FILE)


def get_article_store_db_path() -> Path:
    return _article_store_db_file()


def _sqlite_storage_enabled() -> bool:
    backend = os.environ.get(STORAGE_BACKEND_ENV, "").strip().lower()
    return backend in {"sqlite", "sqlite3", "db", "database"}


def _sqlite_store() -> SQLiteJsonDocumentStore:
    return SQLiteJsonDocumentStore(_local_store_db_file())


def _article_store_backend_request() -> dict[str, str]:
    raw_value = os.environ.get(ARTICLE_STORE_BACKEND_ENV)
    normalized = str(raw_value or "").strip().lower()
    if normalized in ARTICLE_STORE_BACKEND_JSON_VALUES:
        requested = "json"
    elif normalized in ARTICLE_STORE_BACKEND_SQLITE_VALUES:
        requested = "sqlite"
    elif normalized in ARTICLE_STORE_BACKEND_AUTO_VALUES:
        requested = "auto"
    else:
        requested = "auto"
    return {
        "requested_backend": requested,
        "raw_backend": "" if raw_value is None else str(raw_value),
        "backend_source": "default_auto" if raw_value is None or not str(raw_value).strip() else "env",
    }


def _article_store_backend_name() -> str:
    return _article_store_backend_request()["requested_backend"]


def _article_store_sqlite_enabled() -> bool:
    return _resolve_article_store_backend(schedule_migration=True, record=True)["effective_backend"] == "sqlite"


def _article_json_file_signature() -> tuple[str, int, int]:
    return _json_document_signature(
        "article_store/articles",
        _articles_file(),
        use_sqlite=False,
    )


def _article_json_source_signature() -> str:
    payload = {
        "version": 1,
        "articles": _article_json_file_signature(),
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _article_json_count_from_file(*, max_bytes: int = 5_000_000) -> int | None:
    path = _articles_file()
    try:
        if not path.exists():
            return 0
        if path.stat().st_size > max_bytes:
            return None
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return len(parsed) if isinstance(parsed, list) else 0


def _article_sqlite_error_limit() -> int:
    try:
        value = int(os.environ.get("AIBRANDMONITOR_ARTICLE_STORE_SQLITE_ERROR_LIMIT", ARTICLE_STORE_SQLITE_ERROR_LIMIT))
    except Exception:
        value = ARTICLE_STORE_SQLITE_ERROR_LIMIT
    return max(1, value)


def _article_sqlite_cooldown_seconds() -> float:
    try:
        value = float(os.environ.get("AIBRANDMONITOR_ARTICLE_STORE_SQLITE_COOLDOWN_SECONDS", ARTICLE_STORE_SQLITE_COOLDOWN_SECONDS))
    except Exception:
        value = ARTICLE_STORE_SQLITE_COOLDOWN_SECONDS
    return max(0.0, value)


def _article_sqlite_migration_min_interval_seconds() -> float:
    try:
        value = float(
            os.environ.get(
                "AIBRANDMONITOR_ARTICLE_STORE_SQLITE_MIGRATION_MIN_INTERVAL_SECONDS",
                ARTICLE_STORE_SQLITE_MIGRATION_MIN_INTERVAL_SECONDS,
            )
        )
    except Exception:
        value = ARTICLE_STORE_SQLITE_MIGRATION_MIN_INTERVAL_SECONDS
    return max(0.0, value)


def _article_store_sqlite_cooldown_remaining() -> float:
    now_ts = time.monotonic()
    with _article_store_backend_health_lock:
        disabled_until = _safe_float(
            _article_store_backend_health.get("disabled_until_monotonic"),
            _safe_float(_article_store_backend_health.get("disabled_until"), 0.0),
        )
    return max(0.0, disabled_until - now_ts)


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _record_article_store_backend_success(mode: str, *, effective_backend: str, db_path: Path | None = None) -> None:
    with _article_store_backend_health_lock:
        _article_store_backend_health.update({
            "last_mode": str(mode or ""),
            "last_effective_backend": str(effective_backend or ""),
            "last_success_at": local_now().isoformat(timespec="seconds"),
            "consecutive_errors": 0,
            "disabled_until": 0.0,
            "disabled_until_monotonic": 0.0,
            "disabled_until_iso": "",
        })
        if db_path is not None:
            _article_store_backend_health["last_db_path"] = str(db_path)


def _record_article_store_backend_fallback(mode: str, reason: str, *, db_path: Path | None = None) -> None:
    with _article_store_backend_health_lock:
        _article_store_backend_health.update({
            "last_mode": str(mode or ""),
            "last_effective_backend": "json",
            "last_fallback_reason": str(reason or ""),
            "last_fallback_at": local_now().isoformat(timespec="seconds"),
            "fallback_count": _coerce_non_negative_int(_article_store_backend_health.get("fallback_count", 0)) + 1,
        })
        if db_path is not None:
            _article_store_backend_health["last_db_path"] = str(db_path)


def _record_article_store_backend_error(mode: str, reason: str, *, detail: str = "", db_path: Path | None = None) -> None:
    now_ts = time.monotonic()
    now_wall = local_now()
    with _article_store_backend_health_lock:
        consecutive = _coerce_non_negative_int(_article_store_backend_health.get("consecutive_errors", 0)) + 1
        cooldown_seconds = _article_sqlite_cooldown_seconds()
        disabled_until = (
            now_ts + cooldown_seconds
            if consecutive >= _article_sqlite_error_limit()
            else 0.0
        )
        _article_store_backend_health.update({
            "last_mode": str(mode or ""),
            "last_effective_backend": "json",
            "last_fallback_reason": str(reason or ""),
            "last_fallback_at": local_now().isoformat(timespec="seconds"),
            "last_error": str(detail or reason or ""),
            "last_error_at": local_now().isoformat(timespec="seconds"),
            "consecutive_errors": consecutive,
            "fallback_count": _coerce_non_negative_int(_article_store_backend_health.get("fallback_count", 0)) + 1,
            "disabled_until": disabled_until,
            "disabled_until_monotonic": disabled_until,
            "disabled_until_iso": (
                (now_wall + timedelta(seconds=cooldown_seconds)).isoformat(timespec="seconds")
                if disabled_until > 0
                else ""
            ),
        })
        if disabled_until > 0:
            _article_store_backend_health["tripped_count"] = (
                _coerce_non_negative_int(_article_store_backend_health.get("tripped_count", 0)) + 1
            )
        if db_path is not None:
            _article_store_backend_health["last_db_path"] = str(db_path)


def _load_articles_json_for_sqlite_import() -> list[dict]:
    data = _read_json_path(_articles_file(), [])
    normalized_articles = []
    for article in data if isinstance(data, list) else []:
        if not isinstance(article, dict):
            continue
        normalized, _ = _normalize_article_entry(article)
        normalized_articles.append(normalized)
    return normalized_articles


def _build_article_sqlite_store():
    from .article_sqlite_store import ArticleSQLiteStore

    return ArticleSQLiteStore(
        _article_store_db_file(),
        normalize_article_url=normalize_article_url,
        normalize_article_entry=lambda article: _normalize_article_entry(article)[0],
        now_text=_article_now_minute_text,
    )


def _write_article_sqlite_import_meta(
    store,
    *,
    imported_articles: list[dict],
    result: dict,
    source_signature: str,
    source_file_signature: tuple[str, int, int],
    reason: str,
    source_path: Path | None = None,
) -> None:
    imported_at = local_now().isoformat(timespec="seconds")
    source_path = source_path or _articles_file()
    store.set_meta("article_store_backend", "sqlite_authoritative")
    store.set_meta("article_store_import_source", "articles.json")
    store.set_meta("article_store_import_reason", str(reason or ""))
    store.set_meta("article_store_import_source_path", str(source_path))
    store.set_meta(
        "article_store_import_source_signature",
        json.dumps(source_file_signature, ensure_ascii=False, default=str, separators=(",", ":")),
    )
    store.set_meta("article_store_json_source_signature", source_signature)
    store.set_meta("article_store_import_source_count", str(len(imported_articles)))
    try:
        store.set_meta("article_store_sqlite_article_count", str(store.count_articles()))
    except Exception:
        store.set_meta("article_store_sqlite_article_count", "")
    store.set_meta(
        "article_store_import_result",
        json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
    store.set_meta("article_store_imported_at", imported_at)
    store.set_meta("article_store_last_ok_at", imported_at)
    store.set_meta("article_store_last_error", "")


def _article_import_payload_digest(articles: list[dict]) -> str:
    text = json.dumps(articles or [], ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _initialize_article_sqlite_store_from_json_sync(*, reason: str) -> dict:
    store = _build_article_sqlite_store()
    imported_articles = _load_articles_json_for_sqlite_import()
    imported_digest = _article_import_payload_digest(imported_articles)
    source_file_signature = _article_json_file_signature()
    source_signature = _article_json_source_signature()
    result = store.import_from_articles(imported_articles, replace=True)
    imported_sqlite_count = store.count_articles()
    if imported_articles and imported_sqlite_count <= 0:
        store.set_meta("article_store_json_source_signature", "")
        store.set_meta("article_store_last_error", "count_mismatch")
        raise RuntimeError("article sqlite import count mismatch")
    final_source_signature = _article_json_source_signature()
    if final_source_signature != source_signature:
        final_articles = _load_articles_json_for_sqlite_import()
        if _article_import_payload_digest(final_articles) != imported_digest:
            store.set_meta("article_store_json_source_signature", "")
            store.set_meta("article_store_last_error", "source_changed_during_import")
            raise RuntimeError("article json source changed during sqlite import")
        source_signature = final_source_signature
        source_file_signature = _article_json_file_signature()
    _write_article_sqlite_import_meta(
        store,
        imported_articles=imported_articles,
        result=result,
        source_signature=source_signature,
        source_file_signature=source_file_signature,
        reason=reason,
        source_path=_articles_file(),
    )
    return {
        "result": result,
        "source_signature": source_signature,
        "source_count": len(imported_articles),
        "article_count": imported_sqlite_count,
    }


def _article_sqlite_store():
    db_path = _article_store_db_file()
    db_missing = not db_path.exists()
    store = _build_article_sqlite_store()
    if db_missing:
        _initialize_article_sqlite_store_from_json_sync(reason="forced_sqlite_first_use")
    return store


def _article_sqlite_meta_snapshot(store, keys: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in keys:
        try:
            result[key] = str(store.get_meta(key) or "").strip()
        except Exception:
            result[key] = ""
    return result


def _article_store_sqlite_freshness(store) -> dict[str, object]:
    current_json_signature = _article_json_source_signature()
    current_file_signature = _article_json_file_signature()
    current_file_signature_text = json.dumps(
        current_file_signature,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )
    meta = _article_sqlite_meta_snapshot(
        store,
        [
            "article_store_json_source_signature",
            "article_store_import_source_signature",
            "article_store_import_source_count",
            "article_store_imported_at",
            "article_store_last_ok_at",
            "article_store_last_error",
        ],
    )
    sqlite_count = store.count_articles()
    imported_count_text = str(meta.get("article_store_import_source_count") or "").strip()
    try:
        imported_count = int(imported_count_text) if imported_count_text else None
    except Exception:
        imported_count = None
    json_count = _article_json_count_from_file()
    stored_json_signature = str(meta.get("article_store_json_source_signature") or "").strip()
    stored_file_signature = str(meta.get("article_store_import_source_signature") or "").strip()
    signature_fresh = (
        stored_json_signature == current_json_signature
        or stored_file_signature == current_file_signature_text
    )
    has_import_meta = bool(stored_json_signature or stored_file_signature)
    count_fresh = True if has_import_meta else json_count is not None and sqlite_count == json_count
    fresh = bool(signature_fresh and count_fresh)
    return {
        "fresh": fresh,
        "signature_fresh": bool(signature_fresh),
        "count_fresh": bool(count_fresh),
        "json_source_signature": current_json_signature,
        "json_file_signature": current_file_signature,
        "stored_json_source_signature": stored_json_signature,
        "stored_json_file_signature": stored_file_signature,
        "sqlite_count": sqlite_count,
        "json_count": json_count,
        "imported_count": imported_count,
        "meta": meta,
    }


def _resolve_article_store_backend(*, schedule_migration: bool, record: bool) -> dict[str, object]:
    request = _article_store_backend_request()
    requested = request["requested_backend"]
    db_path = _article_store_db_file()
    result: dict[str, object] = {
        **request,
        "effective_backend": "json",
        "fallback_reason": "",
        "db_path": str(db_path),
        "db_ready": False,
        "readiness": {},
        "freshness": {},
    }

    if requested == "json":
        result["fallback_reason"] = "forced_json"
        if record:
            _record_article_store_backend_fallback(requested, "forced_json", db_path=db_path)
        return result

    cooldown_remaining = _article_store_sqlite_cooldown_remaining()
    if requested == "auto" and cooldown_remaining > 0:
        result["fallback_reason"] = "health_cooldown"
        result["cooldown_remaining_seconds"] = round(cooldown_remaining, 3)
        if record:
            _record_article_store_backend_fallback(requested, "health_cooldown", db_path=db_path)
        return result

    try:
        if requested == "sqlite":
            if not db_path.exists():
                _initialize_article_sqlite_store_from_json_sync(reason="forced_sqlite_missing_db")
            readiness = _article_sqlite_store_class().validate_readiness(db_path)
            result["readiness"] = readiness
            result["db_ready"] = bool(readiness.get("ready"))
            if bool(readiness.get("ready")):
                result["effective_backend"] = "sqlite"
                if record:
                    _record_article_store_backend_success(requested, effective_backend="sqlite", db_path=db_path)
                return result
            reason = (
                "article_store_db_missing"
                if not readiness.get("available")
                else f"article_store_db_{readiness.get('reason') or 'not_ready'}"
            )
            result["fallback_reason"] = reason
            if record:
                _record_article_store_backend_error(requested, reason, detail=json.dumps(readiness, ensure_ascii=False), db_path=db_path)
            return result

        readiness = _article_sqlite_store_class().validate_readiness(db_path)
        result["readiness"] = readiness
        result["db_ready"] = bool(readiness.get("ready"))
        if not bool(readiness.get("ready")):
            reason = (
                "article_store_db_missing"
                if not readiness.get("available")
                else f"article_store_db_{readiness.get('reason') or 'not_ready'}"
            )
            result["fallback_reason"] = reason
            if schedule_migration:
                result["migration_scheduled"] = _schedule_article_store_sqlite_migration(reason=reason)
            if record:
                _record_article_store_backend_fallback(requested, reason, db_path=db_path)
            return result

        store = _build_article_sqlite_store()
        freshness = _article_store_sqlite_freshness(store)
        result["freshness"] = freshness
        if bool(freshness.get("fresh")):
            result["effective_backend"] = "sqlite"
            if record:
                _record_article_store_backend_success(requested, effective_backend="sqlite", db_path=db_path)
            return result

        reason = "article_store_source_stale" if not freshness.get("signature_fresh") else "article_store_count_stale"
        result["fallback_reason"] = reason
        if schedule_migration:
            result["migration_scheduled"] = _schedule_article_store_sqlite_migration(reason=reason)
        if record:
            _record_article_store_backend_fallback(requested, reason, db_path=db_path)
        return result
    except Exception as exc:
        reason = "article_store_sqlite_error"
        detail = f"{exc.__class__.__name__}: {exc}"
        result["fallback_reason"] = reason
        result["error"] = detail
        if record:
            _record_article_store_backend_error(requested, reason, detail=detail, db_path=db_path)
        return result


def _article_sqlite_store_class():
    from .article_sqlite_store import ArticleSQLiteStore

    return ArticleSQLiteStore


def _schedule_article_store_sqlite_migration(*, reason: str) -> bool:
    now_ts = time.monotonic()
    db_path = _article_store_db_file()
    with _article_store_migration_lock:
        _article_store_migration_threads[:] = [
            thread for thread in _article_store_migration_threads if thread.is_alive()
        ]
        if bool(_article_store_migration_state.get("running")):
            return False
        next_allowed_at = _safe_float(
            _article_store_migration_state.get("next_allowed_at_monotonic"),
            _safe_float(_article_store_migration_state.get("next_allowed_at"), 0.0),
        )
        if next_allowed_at > now_ts:
            return False
        next_allowed = now_ts + _article_sqlite_migration_min_interval_seconds()
        _article_store_migration_state.update({
            "running": True,
            "last_reason": str(reason or ""),
            "last_started_at": now_ts,
            "last_started_at_monotonic": now_ts,
            "last_started_at_iso": local_now().isoformat(timespec="seconds"),
            "last_finished_at": 0.0,
            "last_finished_at_monotonic": 0.0,
            "last_finished_at_iso": "",
            "last_error": "",
            "db_path": str(db_path),
            "next_allowed_at": next_allowed,
            "next_allowed_at_monotonic": next_allowed,
        })

    thread = threading.Thread(
        target=_run_article_store_sqlite_migration,
        args=(str(reason or ""),),
        name="article-store-sqlite-primary-migration",
        daemon=True,
    )
    with _article_store_migration_lock:
        _article_store_migration_threads.append(thread)
    thread.start()
    return True


def _run_article_store_sqlite_migration(reason: str) -> None:
    ok = False
    detail = ""
    summary: dict[str, object] = {}
    try:
        summary = _initialize_article_sqlite_store_from_json_sync(reason=f"auto:{reason}")
        ok = True
    except Exception as exc:
        detail = f"{exc.__class__.__name__}: {exc}"
        try:
            store = _build_article_sqlite_store()
            store.set_meta("article_store_json_source_signature", "")
            store.set_meta("article_store_last_error", detail)
            store.set_meta("article_store_last_error_at", local_now().isoformat(timespec="seconds"))
        except Exception:
            pass
        _record_article_store_backend_error("auto", "article_store_migration_failed", detail=detail, db_path=_article_store_db_file())
    finally:
        finished_at = time.monotonic()
        with _article_store_migration_lock:
            _article_store_migration_state.update({
                "running": False,
                "last_ok": ok,
                "last_error": detail,
                "last_finished_at": finished_at,
                "last_finished_at_monotonic": finished_at,
                "last_finished_at_iso": local_now().isoformat(timespec="seconds"),
                "last_summary": summary,
            })


def wait_for_article_store_backend_migration(timeout: float = 5.0) -> dict[str, object]:
    deadline = time.monotonic() + max(0.0, float(timeout or 0.0))
    while True:
        with _article_store_migration_lock:
            threads = [thread for thread in _article_store_migration_threads if thread.is_alive()]
        if not threads:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        for thread in threads:
            thread.join(timeout=min(0.05, max(0.0, remaining)))
    with _article_store_migration_lock:
        _article_store_migration_threads[:] = [
            thread for thread in _article_store_migration_threads if thread.is_alive()
        ]
        return dict(_article_store_migration_state)


def reset_article_store_backend_health_for_tests() -> None:
    wait_for_article_store_backend_migration(timeout=2.0)
    with _article_store_backend_health_lock:
        _article_store_backend_health.clear()
    with _article_store_migration_lock:
        _article_store_migration_state.clear()
        _article_store_migration_threads.clear()
    _reset_article_match_refresh_state_for_tests()


def _match_refresh_job_health() -> dict[str, object]:
    try:
        return get_article_match_refresh_status()
    except Exception as exc:
        return {"error": f"{exc.__class__.__name__}: {exc}"}


def get_article_store_backend_health() -> dict[str, object]:
    request = _article_store_backend_request()
    db_path = _article_store_db_file()
    resolution = _resolve_article_store_backend(schedule_migration=False, record=False)
    readiness = resolution.get("readiness") if isinstance(resolution.get("readiness"), dict) else {}
    freshness = resolution.get("freshness") if isinstance(resolution.get("freshness"), dict) else {}
    sqlite_signature = ""
    sqlite_count = None
    if bool(readiness.get("ready")):
        try:
            store = _build_article_sqlite_store()
            sqlite_signature = store.source_signature()
            sqlite_count = store.count_articles()
        except Exception:
            sqlite_signature = ""
            sqlite_count = None
    with _article_store_backend_health_lock:
        health = dict(_article_store_backend_health)
    cooldown_remaining = _article_store_sqlite_cooldown_remaining()
    health["blocked"] = cooldown_remaining > 0
    health["cooldown_remaining_seconds"] = round(cooldown_remaining, 3)
    health["error_limit"] = _article_sqlite_error_limit()
    health["cooldown_seconds"] = _article_sqlite_cooldown_seconds()
    with _article_store_migration_lock:
        migration_state = dict(_article_store_migration_state)
        migration_state["thread_count"] = len([thread for thread in _article_store_migration_threads if thread.is_alive()])
    json_signature = _article_json_source_signature()
    json_file_signature = _article_json_file_signature()
    json_count = _article_json_count_from_file()
    return {
        "ok": True,
        "requested_backend": request["requested_backend"],
        "effective_backend": resolution.get("effective_backend", "json"),
        "effectiveBackend": resolution.get("effective_backend", "json"),
        "backend_source": request["backend_source"],
        "raw_backend": request["raw_backend"],
        "fallback_reason": resolution.get("fallback_reason", ""),
        "fallbackReason": resolution.get("fallback_reason", ""),
        "db_path": str(db_path),
        "db_ready": bool(readiness.get("ready")),
        "readiness": readiness,
        "json_signature": json_signature,
        "json_file_signature": json_file_signature,
        "sqlite_signature": sqlite_signature,
        "article_counts": {
            "json": json_count,
            "sqlite": sqlite_count if sqlite_count is not None else freshness.get("sqlite_count"),
            "imported": freshness.get("imported_count"),
        },
        "freshness": freshness,
        "migration_state": migration_state,
        "health": health,
        "match_refresh_job": _match_refresh_job_health(),
    }


def _article_shadow_db_file() -> Path:
    if ARTICLE_SHADOW_DB_FILE != DEFAULT_ARTICLE_SHADOW_DB_FILE:
        return ARTICLE_SHADOW_DB_FILE
    if ARTICLES_FILE != DEFAULT_ARTICLES_FILE:
        return ARTICLES_FILE.parent / "article_history_shadow.sqlite3"
    return account_scoped_path("logs/article_history_shadow.sqlite3", fallback=DEFAULT_ARTICLE_SHADOW_DB_FILE)


def _article_shadow_store():
    from .article_history_sqlite_store import ArticleHistorySQLiteStore

    return ArticleHistorySQLiteStore(_article_shadow_db_file(), normalize_article_url=normalize_article_url)


def _article_shadow_writes_enabled() -> bool:
    value = os.environ.get(ARTICLE_SHADOW_WRITE_ENV)
    if value is None:
        return True
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "sqlite", "shadow"}


def _article_shadow_stored_source_signature() -> str:
    if not _article_shadow_db_file().exists():
        return ""
    try:
        return str(_article_shadow_store().get_meta("article_source_signature") or "").strip()
    except Exception:
        return ""


def _begin_article_shadow_runtime_write() -> dict[str, object]:
    return {
        "enabled": _article_shadow_writes_enabled(),
        "source_signature_before": get_article_source_signature(),
        "stored_signature_before": _article_shadow_stored_source_signature(),
    }


def _mark_article_shadow_dirty(reason: str = "") -> None:
    try:
        _article_shadow_store().set_meta("article_source_signature", "")
        _article_shadow_store().set_meta("article_last_dirty_reason", str(reason or "runtime_write"))
    except Exception as exc:
        print(f"[ArticleStore] 标记 SQLite 文章影子索引脏失败: {exc}")


def _finish_article_shadow_runtime_write(context: dict[str, object], *, ok: bool, reason: str = "") -> None:
    source_signature_before = str(context.get("source_signature_before") or "").strip()
    stored_signature_before = str(context.get("stored_signature_before") or "").strip()
    if not ok or not source_signature_before or stored_signature_before != source_signature_before:
        if not stored_signature_before and not _article_shadow_db_file().exists():
            return
        _mark_article_shadow_dirty(reason or "runtime_write_needs_rebuild")
        return
    try:
        store = _article_shadow_store()
        store.set_meta("article_source_signature", get_article_source_signature())
        store.set_meta("article_last_incremental_sync_at", local_now().isoformat(timespec="seconds"))
    except Exception as exc:
        print(f"[ArticleStore] 更新 SQLite 文章影子索引签名失败: {exc}")
        _mark_article_shadow_dirty(reason or "runtime_signature_update_failed")


def _sync_article_shadow_articles(
    articles: list[dict] | tuple[dict, ...],
    *,
    reason: str,
    context: dict[str, object] | None = None,
) -> None:
    payload = [dict(article) for article in (articles or []) if isinstance(article, dict)]
    if not payload:
        return
    write_context = context if context is not None else _begin_article_shadow_runtime_write()
    if not bool(write_context.get("enabled")):
        _finish_article_shadow_runtime_write(write_context, ok=False, reason="shadow_writes_disabled")
        return
    ok = False
    try:
        _article_shadow_store().upsert_articles(payload)
        ok = True
    except Exception as exc:
        print(f"[ArticleStore] SQLite 文章影子索引增量写入失败: {exc}")
    _finish_article_shadow_runtime_write(write_context, ok=ok, reason=reason)


def _sync_article_shadow_delete(
    article_ids: list[str] | tuple[str, ...] | set[str],
    *,
    reason: str,
    context: dict[str, object] | None = None,
) -> None:
    ids = [
        str(article_id or "").strip()
        for article_id in (article_ids or [])
        if str(article_id or "").strip()
    ]
    if not ids:
        return
    write_context = context if context is not None else _begin_article_shadow_runtime_write()
    if not bool(write_context.get("enabled")):
        _finish_article_shadow_runtime_write(write_context, ok=False, reason="shadow_writes_disabled")
        return
    ok = False
    try:
        _article_shadow_store().delete_articles_by_ids(ids)
        ok = True
    except Exception as exc:
        print(f"[ArticleStore] SQLite 文章影子索引删除失败: {exc}")
    _finish_article_shadow_runtime_write(write_context, ok=ok, reason=reason)


def _sync_article_shadow_mutation(
    *,
    upsert_articles: list[dict] | tuple[dict, ...] = (),
    delete_article_ids: list[str] | tuple[str, ...] | set[str] = (),
    reason: str,
    context: dict[str, object] | None = None,
) -> None:
    payload = [dict(article) for article in (upsert_articles or []) if isinstance(article, dict)]
    ids = [
        str(article_id or "").strip()
        for article_id in (delete_article_ids or [])
        if str(article_id or "").strip()
    ]
    if not payload and not ids:
        return
    write_context = context if context is not None else _begin_article_shadow_runtime_write()
    if not bool(write_context.get("enabled")):
        _finish_article_shadow_runtime_write(write_context, ok=False, reason="shadow_writes_disabled")
        return
    ok = False
    try:
        store = _article_shadow_store()
        if ids:
            store.delete_articles_by_ids(ids)
        if payload:
            store.upsert_articles(payload)
        ok = True
    except Exception as exc:
        print(f"[ArticleStore] SQLite 文章影子索引增量变更失败: {exc}")
    _finish_article_shadow_runtime_write(write_context, ok=ok, reason=reason)


def _read_json_path(path: Path, default):
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(default, list):
                return data if isinstance(data, list) else []
            if isinstance(default, dict):
                return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return [] if isinstance(default, list) else {}


def _write_json_path(path: Path, payload, label: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), suffix=".tmp"
        )
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
    except Exception as e:
        print(f"[ArticleStore] 写入 {label} 失败: {e}")


def _load_json_document(doc_key: str, path: Path, default, *, use_sqlite: bool):
    if use_sqlite and _sqlite_storage_enabled():
        try:
            store = _sqlite_store()
            data = store.load(doc_key, MISSING)
            if data is MISSING:
                data = _read_json_path(path, default)
                if data:
                    store.save(doc_key, data)
        except Exception as e:
            print(f"[ArticleStore] 读取 {doc_key} SQLite 失败，回退 JSON: {e}")
            data = _read_json_path(path, default)
        if isinstance(default, list):
            return data if isinstance(data, list) else []
        if isinstance(default, dict):
            return data if isinstance(data, dict) else {}
        return data
    return _read_json_path(path, default)


def _load_small_json_document(doc_key: str, path: Path, default, *, use_sqlite: bool):
    now = time.monotonic()
    cached = _small_document_cache.get(doc_key)
    if cached is not None and now - cached[0] <= SMALL_DOCUMENT_CACHE_TTL_SECONDS:
        return copy.deepcopy(cached[1])
    data = _load_json_document(doc_key, path, default, use_sqlite=use_sqlite)
    _small_document_cache[doc_key] = (now, copy.deepcopy(data))
    return data


def _invalidate_json_document_cache(doc_key: str) -> None:
    _small_document_cache.pop(doc_key, None)


def _save_json_document(doc_key: str, path: Path, payload, *, use_sqlite: bool, label: str) -> None:
    if use_sqlite and _sqlite_storage_enabled():
        try:
            _sqlite_store().save(doc_key, payload)
            _invalidate_json_document_cache(doc_key)
            return
        except Exception as e:
            print(f"[ArticleStore] 写入 {label} SQLite 失败，回退 JSON: {e}")
    _write_json_path(path, payload, label)
    _invalidate_json_document_cache(doc_key)


def _json_document_signature(doc_key: str, path: Path, *, use_sqlite: bool) -> tuple[str, int, int]:
    if use_sqlite and _sqlite_storage_enabled():
        try:
            store = _sqlite_store()
            if store.exists(doc_key):
                return store.signature(doc_key)
        except Exception as e:
            print(f"[ArticleStore] 读取 {doc_key} SQLite 签名失败，回退 JSON: {e}")
    try:
        stat = path.stat()
        return (str(path), int(stat.st_mtime_ns), int(stat.st_size))
    except FileNotFoundError:
        return (str(path), 0, 0)
    except Exception:
        return (str(path), -1, -1)

_MEDIA_NAME_ALIASES: dict[str, str] = {
    "头条": "今日头条",
    "嘉鱼门户 权威发布": "嘉鱼网",
    "嘉鱼门户": "嘉鱼网",
    "千龙网·中国首都网": "千龙网",
    "千龙新闻网": "千龙网",
}

_MEDIA_NAME_TRAILING_NOISE = (
    "权威发布",
    "官方发布",
    "官方网站",
    "官方平台",
    "官方媒体",
    "官方账号",
    "权威媒体",
    "权威资讯",
    "资讯发布",
    "新闻发布",
    "政务发布",
)

_ARTICLE_MEDIA_PLATFORM_LABEL_ALIASES: dict[str, str] = {
    "头条": "今日头条",
    "今日头条": "今日头条",
    "头条号": "今日头条",
    "搜狐": "搜狐",
    "搜狐号": "搜狐",
    "腾讯": "腾讯新闻",
    "腾讯新闻": "腾讯新闻",
    "腾讯号": "腾讯新闻",
    "官方腾讯号": "腾讯新闻",
    "腾讯网新闻": "腾讯新闻",
    "知乎": "知乎",
    "博客园": "博客园",
    "公众号": "微信公众号",
    "微信": "微信公众号",
    "微信公众号": "微信公众号",
    "百家号": "百家号",
    "微博": "微博",
    "小红书": "小红书",
    "抖音": "抖音",
    "快手": "快手",
    "哔哩哔哩": "哔哩哔哩",
    "b站": "哔哩哔哩",
}

_CHANNEL_MEDIA_NAME_PATTERN = re.compile(
    r"^(?:产经|财经|产业|要闻|时政|政务|民生|社会|本地|国内|国际|科技|教育|旅游|文化|体育|娱乐|健康|汽车|房产|专题|公告|视频|图片)?(?:新闻|资讯|频道)$"
)
_LIKELY_MEDIA_NAME_PATTERN = re.compile(
    r"(日报|晚报|晨报|时报|周报|周刊|新闻网|新闻客户端|客户端|融媒体中心|电视台|广播电台|报业集团|网|台|报|刊|号)$"
)


def _compact_article_media_label(value: str) -> str:
    return re.sub(r"[\s_:\-—–|｜/\\（）()【】\[\]\"'“”‘’]+", "", str(value or "").strip().lower())


def _resolve_article_media_platform_label(value: str) -> str:
    key = _compact_article_media_label(value)
    if not key:
        return ""
    normalized_aliases = {
        _compact_article_media_label(alias): label
        for alias, label in _ARTICLE_MEDIA_PLATFORM_LABEL_ALIASES.items()
    }
    return normalized_aliases.get(key, "")


def _is_article_media_parenthetical_noise(value: str) -> bool:
    text = str(value or "").strip()
    if not text or _resolve_article_media_platform_label(text):
        return False
    normalized = _compact_article_media_label(text)
    if normalized in {"官方", "官媒", "news", "geo", "排名", "可发", "好出稿", "出稿", "可发排名"}:
        return True
    return bool(
        "geo" in normalized
        or "可发" in normalized
        or "排名" in normalized
        or "好出稿" in normalized
        or normalized == "官方"
        or normalized.endswith("news")
    )


def _strip_article_media_qualifier(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for _ in range(3):
        match = re.fullmatch(r"(.+?)[（(【\[]([^（）()【】\[\]]+)[）)】\]]", text)
        if not match or not _is_article_media_parenthetical_noise(match.group(2)):
            break
        text = match.group(1).strip()
    return text


def _split_legacy_article_media_platform_account(value: str) -> tuple[str, str]:
    text = str(value or "").strip()
    if not text:
        return "", ""
    match = re.fullmatch(r"(.+?)[（(【\[]([^（）()【】\[\]]+)[）)】\]]", text)
    if not match:
        return "", ""
    left = match.group(1).strip()
    right = match.group(2).strip()
    left_platform = _resolve_article_media_platform_label(left)
    right_platform = _resolve_article_media_platform_label(right)
    if right_platform and left and not left_platform:
        return right_platform, left
    if left_platform and right and not _is_article_media_parenthetical_noise(right):
        return left_platform, right
    return "", ""


# ---------------------------------------------------------------------------
# 域名工具
# ---------------------------------------------------------------------------

def extract_domain(url: str) -> str:
    """从 URL 提取二级域名（去掉 www.）"""
    try:
        parsed = urlparse(url)
        host = parsed.netloc or parsed.path
        host = host.lower().strip()
        # 去掉端口
        if ":" in host:
            host = host.split(":")[0]
        # 去掉 www.
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "igshid",
    "mkt_tok",
    "mc_cid",
    "mc_eid",
    "spm",
}
_SITE_TRACKING_QUERY_KEYS: dict[str, set[str]] = {
    "redhongan.com": {"timestamp"},
}
_SITE_TRACKING_QUERY_KEY_ITEMS = tuple(
    sorted(_SITE_TRACKING_QUERY_KEYS.items(), key=lambda item: len(item[0]), reverse=True)
)


def _is_site_tracking_query_key(host: str, key: str) -> bool:
    normalized_host = str(host or "").strip().lower()
    normalized_key = str(key or "").strip().lower()
    if not normalized_host or not normalized_key:
        return False
    for suffix, keys in _SITE_TRACKING_QUERY_KEY_ITEMS:
        if normalized_key in keys and (normalized_host == suffix or normalized_host.endswith("." + suffix)):
            return True
    return False


def normalize_article_url(url: str) -> str:
    """归一化文章 URL，便于做去重判断。"""
    raw = str(url or "").strip()
    if not raw:
        return ""

    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        raw = f"https://{raw}"

    try:
        parsed = urlsplit(raw)
        scheme = (parsed.scheme or "https").lower()
        host = (parsed.netloc or "").strip().lower()
        if not host and parsed.path:
            reparsed = urlsplit(f"{scheme}://{parsed.path}")
            parsed = reparsed
            host = (parsed.netloc or "").strip().lower()
        if ":" in host:
            hostname, port = host.split(":", 1)
            if (scheme == "http" and port == "80") or (scheme == "https" and port == "443"):
                host = hostname
        if host.startswith("www."):
            host = host[4:]

        path = parsed.path or ""
        if path != "/":
            path = path.rstrip("/")

        filtered_query = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            normalized_key = str(key or "").strip().lower()
            if not normalized_key:
                continue
            if (
                normalized_key.startswith("utm_")
                or normalized_key in _TRACKING_QUERY_KEYS
                or _is_site_tracking_query_key(host, normalized_key)
            ):
                continue
            filtered_query.append((key, value))
        filtered_query.sort()
        query = urlencode(filtered_query, doseq=True)
        return urlunsplit((scheme, host, path, query, ""))
    except Exception:
        return raw


def resolve_article_display_url(article: dict) -> str:
    """Return the user-facing URL while keeping normalized URLs for matching."""
    if not isinstance(article, dict):
        return ""
    stored_url = str(article.get("url") or "").strip()
    normalized_stored_url = normalize_article_url(stored_url)
    raw_url = str(article.get("raw_url") or "").strip()
    if raw_url and re.match(r"(?i)^(?:https?://|www\.)", raw_url):
        normalized_raw_url = normalize_article_url(raw_url)
        if normalized_raw_url and (not normalized_stored_url or normalized_raw_url == normalized_stored_url):
            return raw_url
    return stored_url


def _clean_media_name_text(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""

    text = re.sub(r"\s+", " ", raw).strip(" \t\r\n-—–_|｜,，;；/·")
    text = _MEDIA_NAME_ALIASES.get(text, text)
    if not re.search(r"[\u4e00-\u9fff]", text):
        return text

    edge_chars = " \t\r\n-—–_|｜,，;；/·"
    for _ in range(3):
        previous = text
        for noise in _MEDIA_NAME_TRAILING_NOISE:
            text = re.sub(
                rf"(?:\s*(?:-|—|–|_|｜|\||,|，|;|；|/|·)?\s*){re.escape(noise)}$",
                "",
                text,
            ).strip(edge_chars)
        text = _MEDIA_NAME_ALIASES.get(text, text)
        if text == previous:
            break
    alias = _MEDIA_NAME_ALIASES.get(text)
    if alias:
        return alias
    if "·" in text:
        for part in [item.strip() for item in text.split("·") if item.strip()]:
            if part in _MEDIA_NAME_ALIASES:
                return _MEDIA_NAME_ALIASES[part]
            if _LIKELY_MEDIA_NAME_PATTERN.search(part) and not _CHANNEL_MEDIA_NAME_PATTERN.match(part):
                return part
    return text or raw


def _looks_like_channel_media_name(value: str) -> bool:
    text = _clean_media_name_text(value)
    if not text or "." in text or not re.search(r"[\u4e00-\u9fff]", text):
        return False
    if len(text) > 10:
        return False
    return bool(_CHANNEL_MEDIA_NAME_PATTERN.match(text))


def _looks_like_likely_media_name(value: str) -> bool:
    text = _clean_media_name_text(value)
    if not text or "." in text or not re.search(r"[\u4e00-\u9fff]", text):
        return False
    if _looks_like_channel_media_name(text):
        return False
    if len(text) > 18:
        return False
    return bool(_LIKELY_MEDIA_NAME_PATTERN.search(text))


def _looks_like_domainish_value(value: str) -> bool:
    text = str(value or "").strip()
    if not text or re.search(r"[\u4e00-\u9fff]", text):
        return False
    return bool(re.search(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text) or re.search(r"(?:^|[./])[\w-]+\.[a-zA-Z]{2,}", text))


def _mapped_media_name_for_domain(domain: str) -> str:
    candidate = extract_domain(domain) or str(domain or "").strip().lower()
    if not candidate:
        return ""
    for suffix, media_name in sorted(
        _MEDIA_NAME_BY_DOMAIN_SUFFIX.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if candidate == suffix or candidate.endswith("." + suffix):
            return media_name
    return ""


def _canonical_selfmedia_platform_media_name(value: str) -> str:
    domain = extract_domain(value)
    if not domain:
        return ""
    if _get_platform_selfmedia_type(value or domain) != "selfmedia":
        return ""
    return _canonical_selfmedia_platform_site_media_name(domain)


def _canonical_selfmedia_platform_site_media_name(domain: str) -> str:
    candidate = extract_domain(domain) or str(domain or "").strip().lower()
    if not candidate:
        return ""
    locked_suffixes = set(SELF_MEDIA_DOMAIN_SUFFIXES)
    locked_suffixes.update(suffix for suffix, _prefixes in SELF_MEDIA_PATH_RULES)
    for suffix in sorted(locked_suffixes, key=len, reverse=True):
        if _match_domain_suffix(candidate, suffix):
            return _mapped_media_name_for_domain(candidate)
    return ""


def _learned_media_name_for_domain(domain: str, mapped_media_name: str = "") -> str:
    candidate = extract_domain(domain) or str(domain or "").strip().lower()
    if not candidate:
        return ""
    learned_names = _load_domain_media_names()
    for learned_domain, media_name in sorted(
        learned_names.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if candidate == learned_domain or candidate.endswith("." + learned_domain):
            learned_name = _clean_media_name_text(str(media_name or "").strip())
            if mapped_media_name and _looks_like_channel_media_name(learned_name):
                return mapped_media_name
            return learned_name
    return ""


def resolve_media_name_detail(
    value: str,
    *,
    page_url: str = "",
    source: str = "unknown",
) -> dict[str, object]:
    """解析媒体名并返回置信度；低置信结果不应写入域名记忆。"""
    raw = _clean_media_name_text(str(value or "").strip())
    value_domain = extract_domain(raw) if _looks_like_domainish_value(raw) else ""
    page_domain = extract_domain(page_url)
    lookup_domain = value_domain or page_domain

    mapped_media_name = _mapped_media_name_for_domain(lookup_domain)
    if mapped_media_name:
        return {
            "name": mapped_media_name,
            "confidence": 1.0,
            "source": "domain_map",
            "can_learn": False,
        }

    learned_media_name = _learned_media_name_for_domain(lookup_domain)
    if learned_media_name and not _looks_like_channel_media_name(learned_media_name):
        return {
            "name": learned_media_name,
            "confidence": 0.95,
            "source": "learned",
            "can_learn": False,
        }

    if not raw:
        fallback_domain = lookup_domain
        return {
            "name": fallback_domain,
            "confidence": 0.2 if fallback_domain else 0.0,
            "source": "domain_fallback" if fallback_domain else "empty",
            "can_learn": False,
        }

    if value_domain:
        return {
            "name": value_domain,
            "confidence": 0.2,
            "source": "domain_fallback",
            "can_learn": False,
        }

    if _looks_like_channel_media_name(raw):
        fallback_domain = page_domain
        return {
            "name": fallback_domain,
            "confidence": 0.15 if fallback_domain else 0.0,
            "source": "channel_rejected",
            "can_learn": False,
        }

    confidence_by_source = {
        "schema": 0.86,
        "meta": 0.78,
        "homepage": 0.82,
        "ai": 0.72,
        "title_suffix": 0.45,
        "unknown": 0.5,
    }
    confidence = confidence_by_source.get(source, 0.5)
    if source == "title_suffix" and not _looks_like_likely_media_name(raw):
        return {
            "name": page_domain or "",
            "confidence": 0.15 if page_domain else 0.0,
            "source": "title_suffix_rejected",
            "can_learn": False,
        }

    can_learn = (
        confidence >= 0.78
        and source in {"schema", "meta", "homepage"}
        and _looks_like_likely_media_name(raw)
    )
    return {
        "name": raw,
        "confidence": confidence,
        "source": source,
        "can_learn": can_learn,
    }


def resolve_media_name(value: str) -> str:
    """将域名/站点标识解析为具体媒体名。"""
    return str(resolve_media_name_detail(value).get("name") or "")


def resolve_article_source(article: dict) -> str:
    """解析文章记录应展示给前端的媒体名。"""
    if not isinstance(article, dict):
        return ""

    canonical_platform_media_name = _canonical_selfmedia_platform_media_name(article.get("url", ""))
    if canonical_platform_media_name:
        return canonical_platform_media_name

    url_media_name = resolve_media_name(article.get("url", ""))
    for key in ("media_name", "source", "platform"):
        media_name = resolve_media_name(article.get(key, ""))
        if media_name:
            if (
                url_media_name
                and not _looks_like_channel_media_name(url_media_name)
                and _looks_like_channel_media_name(media_name)
            ):
                return url_media_name
            return media_name

    if url_media_name:
        return url_media_name

    return str(article.get("title", "文章") or "文章").strip()


def resolve_article_export_source(article: dict, *, show_selfmedia_account: bool = True) -> str:
    """导出时展示媒体名，可选择在账号抓取文章后补充账号名。"""
    if not isinstance(article, dict):
        return ""
    source = resolve_article_source(article)
    account_name = str(article.get("account_name", "") or "").strip()
    if (
        show_selfmedia_account
        and account_name
        and source
        and account_name != source
        and f"（{account_name}）" not in source
    ):
        return f"{source}（{account_name}）"
    return source


def _match_domain_suffix(domain: str, suffix: str) -> bool:
    normalized_domain = str(domain or "").strip().lower()
    normalized_suffix = str(suffix or "").strip().lower()
    if not normalized_domain or not normalized_suffix:
        return False
    return (
        normalized_domain == normalized_suffix
        or normalized_domain.endswith("." + normalized_suffix)
    )


_MULTI_PART_PUBLIC_SUFFIXES = {
    "com.cn",
    "net.cn",
    "org.cn",
    "gov.cn",
    "edu.cn",
    "ac.cn",
    "co.uk",
}


def get_domain_site_key(domain: str) -> str:
    normalized = str(domain or "").strip().lower()
    if not normalized:
        return ""

    parts = [part for part in normalized.split(".") if part]
    if len(parts) <= 2:
        return normalized

    tail = ".".join(parts[-2:])
    if tail in _MULTI_PART_PUBLIC_SUFFIXES and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _domains_share_site_key(left: str, right: str) -> bool:
    left_key = get_domain_site_key(left)
    right_key = get_domain_site_key(right)
    return bool(left_key and right_key and left_key == right_key)


def _get_platform_selfmedia_type(url_or_domain: str) -> str:
    raw = str(url_or_domain or "").strip()
    domain = extract_domain(raw)
    if not domain:
        return ""
    for forced_domain in SELF_MEDIA_DOMAIN_SUFFIXES:
        if _match_domain_suffix(domain, forced_domain):
            return "selfmedia"
    path = ""
    if _looks_like_domainish_value(raw):
        try:
            path = urlparse(raw if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw) else f"https://{raw}").path.lower()
        except Exception:
            path = ""
    if path:
        for suffix, prefixes in SELF_MEDIA_PATH_RULES:
            if _match_domain_suffix(domain, suffix) and any(path.startswith(prefix) for prefix in prefixes):
                return "selfmedia"
    return ""


def _classify_media_name_hint(media_name: str) -> str:
    name = str(media_name or "").strip()
    if not name:
        return ""

    lowered = name.lower()
    selfmedia_keywords = (
        "微信公众号",
        "百家号",
        "头条号",
        "知乎",
        "微博",
        "抖音",
        "快手",
        "小红书",
        "哔哩哔哩",
        "bilibili",
        "视频号",
        "豆瓣",
        "虎扑",
        "什么值得买",
        "少数派",
        "掘金",
        "csdn",
        "博客园",
        "网易号",
        "搜狐号",
    )
    if any(keyword.lower() in lowered for keyword in selfmedia_keywords):
        return "selfmedia"

    authority_suffixes = (
        "日报",
        "晚报",
        "晨报",
        "时报",
        "周报",
        "周刊",
        "新闻网",
        "新闻客户端",
        "电视台",
        "广播电台",
        "融媒体中心",
        "报业集团",
    )
    if any(name.endswith(suffix) for suffix in authority_suffixes):
        return "authority"

    authority_keywords = (
        "人民网",
        "新华网",
        "央视网",
        "央广网",
        "中国新闻网",
        "光明网",
        "中国日报",
        "中国网",
        "中工网",
        "参考消息",
        "澎湃新闻",
        "界面新闻",
        "财新",
        "第一财经",
        "36氪",
        "虎嗅",
        "钛媒体",
        "创业邦",
        "凤凰网",
        "腾讯新闻",
        "网易新闻",
        "搜狐",
        "新浪",
        "千龙网",
        "定州新闻网",
        "嘉鱼网",
        "红安网",
        "云上红安",
    )
    if any(keyword in name for keyword in authority_keywords):
        return "authority"

    return ""


def _coerce_non_negative_int(value, default: int = 0) -> int:
    try:
        parsed = int(value or 0)
    except Exception:
        parsed = default
    return max(0, parsed)


def _safe_capacity(value, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(0, parsed)


def _trim_reference_events_for_storage(events: list[dict]) -> tuple[list[dict], bool]:
    capacity = _safe_capacity(MAX_REFERENCE_EVENTS_PER_TASK, 500)
    if capacity <= 0 or len(events) <= capacity:
        return events, False
    indexed_events = list(enumerate(events))
    indexed_events.sort(
        key=lambda item: (
            str((item[1] or {}).get("referenced_at") or ""),
            item[0],
        )
    )
    kept = [event for _, event in indexed_events[-capacity:]]
    return kept, True


def _max_timestamp_text(left: str, right: str) -> str:
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if not left_text:
        return right_text
    if not right_text:
        return left_text
    return right_text if right_text > left_text else left_text


def _make_reference_event_id(
    *,
    explicit_event_id: str = "",
    record_id: str = "",
    task_name: str = "",
    article_url: str = "",
    platform: str = "",
    referenced_at: str = "",
    source: str = "",
) -> str:
    event_id = str(explicit_event_id or "").strip()
    if event_id:
        return event_id

    record_text = str(record_id or "").strip()
    if record_text:
        payload = {
            "record_id": record_text,
            "task_name": str(task_name or "").strip(),
            "article_url": normalize_article_url(article_url),
            "platform": normalize_platform_id(platform),
            "referenced_at": str(referenced_at or "").strip(),
            "source": str(source or "").strip(),
        }
        digest = hashlib.sha1(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:20]
        return f"reference:{digest}"

    return uuid.uuid4().hex


def _normalize_reference_event(
    event: dict,
    *,
    task_name: str = "",
    article_url: str = "",
    default_source: str = "",
    default_platform: str = "",
    default_referenced_at: str = "",
) -> dict | None:
    if not isinstance(event, dict):
        return None

    normalized: dict = {}
    event_id = str(event.get("event_id") or event.get("id") or "").strip()
    if event_id:
        normalized["event_id"] = event_id

    record_id = str(event.get("record_id") or event.get("history_record_id") or "").strip()
    if record_id:
        normalized["record_id"] = record_id

    referenced_at = str(
        event.get("referenced_at")
        or event.get("last_referenced_at")
        or default_referenced_at
        or ""
    ).strip()
    if referenced_at:
        normalized["referenced_at"] = referenced_at

    platform = normalize_platform_id(str(event.get("platform") or default_platform or "").strip())
    if not platform:
        raw_platforms = event.get("platforms")
        if isinstance(raw_platforms, list):
            for raw_platform in raw_platforms:
                platform = normalize_platform_id(str(raw_platform or "").strip())
                if platform:
                    break
    if platform:
        normalized["platform"] = platform

    source = str(event.get("source") or default_source or "").strip()
    if source:
        normalized["source"] = source

    event_task_name = str(event.get("task_name") or task_name or "").strip()
    if event_task_name:
        normalized["task_name"] = event_task_name

    normalized_url = normalize_article_url(str(event.get("article_url") or event.get("url") or article_url or "").strip())
    if normalized_url:
        normalized["article_url"] = normalized_url

    return normalized or None


def _normalize_reference_hit(
    hit: dict,
    *,
    task_name: str = "",
    article_url: str = "",
) -> tuple[dict, bool]:
    if not isinstance(hit, dict):
        return {}, True

    normalized = dict(hit)
    before = json.dumps(normalized, ensure_ascii=False, sort_keys=True)

    count = _coerce_non_negative_int(normalized.get("count", 0))
    normalized["count"] = count

    source = str(normalized.get("source") or "").strip()
    if source:
        normalized["source"] = source
    else:
        normalized.pop("source", None)

    last_referenced_at = str(normalized.get("last_referenced_at") or "").strip()
    if last_referenced_at:
        normalized["last_referenced_at"] = last_referenced_at
    else:
        normalized.pop("last_referenced_at", None)

    raw_events = normalized.get("events")
    if isinstance(raw_events, list):
        cleaned_events = []
        seen_event_ids: set[str] = set()
        latest_time = last_referenced_at
        for raw_event in raw_events:
            event = _normalize_reference_event(
                raw_event,
                task_name=task_name,
                article_url=article_url,
                default_source=source,
            )
            if not event:
                continue
            event_id = str(event.get("event_id") or "").strip()
            if event_id and event_id in seen_event_ids:
                continue
            if event_id:
                seen_event_ids.add(event_id)
            latest_time = _max_timestamp_text(latest_time, str(event.get("referenced_at") or ""))
            cleaned_events.append(event)

        if cleaned_events:
            total_event_count = len(cleaned_events)
            cleaned_events, trimmed = _trim_reference_events_for_storage(cleaned_events)
            normalized["events"] = cleaned_events
            normalized["count"] = max(count, total_event_count)
            if latest_time:
                normalized["last_referenced_at"] = latest_time
            if trimmed:
                normalized["events_compacted"] = True
        else:
            normalized.pop("events", None)
    else:
        normalized.pop("events", None)

    after = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    return normalized, after != before


def _normalize_article_entry(entry: dict) -> tuple[dict, bool]:
    """为旧记录补齐媒体名，避免前端继续显示域名。"""
    normalized = dict(entry)
    changed = False

    normalized_url = normalize_article_url(normalized.get("url", ""))
    if normalized_url != str(normalized.get("url", "") or "").strip():
        normalized["url"] = normalized_url
        changed = True

    for key in ("account_id", "account_name", "account_url", "account_platform", "account_platform_label"):
        value = str(normalized.get(key, "") or "").strip()
        if value != normalized.get(key, ""):
            normalized[key] = value
            changed = True

    media_name = str(normalized.get("media_name", "") or "").strip()
    account_name = str(normalized.get("account_name", "") or "").strip()
    account_platform_label = str(normalized.get("account_platform_label", "") or "").strip()
    split_platform, split_account = _split_legacy_article_media_platform_account(media_name)
    if split_platform:
        normalized["media_name"] = split_platform
        media_name = split_platform
        changed = True
        if split_account and not account_name:
            normalized["account_name"] = split_account
            account_name = split_account
            changed = True
    else:
        stripped_media_name = _strip_article_media_qualifier(media_name)
        if stripped_media_name and stripped_media_name != media_name:
            normalized["media_name"] = stripped_media_name
            media_name = stripped_media_name
            changed = True

    media_platform_label = _resolve_article_media_platform_label(media_name)
    if media_platform_label and media_platform_label != media_name:
        normalized["media_name"] = media_platform_label
        media_name = media_platform_label
        changed = True

    account_platform_from_name = _resolve_article_media_platform_label(account_name)
    if account_platform_from_name and media_name and media_name != account_platform_from_name:
        normalized["media_name"] = account_platform_from_name
        normalized["account_name"] = _strip_article_media_qualifier(media_name) or media_name
        media_name = account_platform_from_name
        account_name = str(normalized.get("account_name", "") or "").strip()
        changed = True
    elif account_platform_from_name and media_name == account_platform_from_name:
        normalized["account_name"] = ""
        account_name = ""
        changed = True

    if account_name and media_name == account_name and account_platform_label:
        normalized["media_name"] = account_platform_label
        media_name = account_platform_label
        changed = True
    canonical_platform_media_name = _canonical_selfmedia_platform_media_name(normalized_url or normalized.get("url", ""))
    if canonical_platform_media_name:
        candidate_account_name = _strip_article_media_qualifier(media_name)
        candidate_is_platform = bool(_resolve_article_media_platform_label(candidate_account_name))
        if (
            not account_name
            and candidate_account_name
            and candidate_account_name != canonical_platform_media_name
            and not candidate_is_platform
            and not _looks_like_channel_media_name(candidate_account_name)
        ):
            normalized["account_name"] = candidate_account_name
            account_name = candidate_account_name
            changed = True
        if media_name != canonical_platform_media_name:
            normalized["media_name"] = canonical_platform_media_name
            media_name = canonical_platform_media_name
            changed = True
    resolved = resolve_article_source(normalized)
    if resolved and media_name != resolved:
        normalized["media_name"] = resolved
        changed = True

    domain = extract_domain(normalized_url or normalized.get("url", ""))
    media_type = str(normalized.get("media_type", "") or "").strip().lower()
    forced_media_type = _get_platform_selfmedia_type(normalized_url or domain)
    if forced_media_type:
        if media_type != forced_media_type:
            normalized["media_type"] = forced_media_type
            changed = True
    else:
        source_hint = (
            str(normalized.get("media_name", "") or "").strip()
            or str(normalized.get("source", "") or "").strip()
            or str(normalized.get("platform", "") or "").strip()
        )
        inferred_media_type = classify_article_media_type(normalized_url or domain, source_hint) if domain or source_hint else "selfmedia"
        resolved_media_type = media_type if media_type in {"authority", "selfmedia"} else inferred_media_type
        if media_type == "selfmedia" and inferred_media_type == "authority":
            resolved_media_type = inferred_media_type
        if resolved_media_type != media_type:
            normalized["media_type"] = resolved_media_type
            changed = True

    excerpt = str(normalized.get("excerpt", "") or "").strip()
    if excerpt != normalized.get("excerpt", ""):
        normalized["excerpt"] = excerpt
        changed = True

    imported_at = str(normalized.get("imported_at") or normalized.get("created_at") or "").strip()
    if not imported_at:
        imported_at = str(normalized.get("ts", "") or "").strip() or _article_now_minute_text()
    if imported_at != normalized.get("imported_at", ""):
        normalized["imported_at"] = imported_at
        changed = True

    excluded_tasks = normalized.get("excluded_tasks", [])
    if not isinstance(excluded_tasks, list):
        normalized["excluded_tasks"] = []
        changed = True
    else:
        cleaned_excluded_tasks = []
        for task_name in excluded_tasks:
            task_text = str(task_name or "").strip()
            if task_text and task_text not in cleaned_excluded_tasks:
                cleaned_excluded_tasks.append(task_text)
        if cleaned_excluded_tasks != excluded_tasks:
            normalized["excluded_tasks"] = cleaned_excluded_tasks
            changed = True
    excluded_task_set = set(normalized.get("excluded_tasks") or [])

    matched_tasks = normalized.get("matched_tasks", [])
    if not isinstance(matched_tasks, list):
        normalized["matched_tasks"] = []
        changed = True
    else:
        cleaned_matched_tasks = []
        for task_name in matched_tasks:
            task_text = str(task_name or "").strip()
            if task_text and task_text not in excluded_task_set and task_text not in cleaned_matched_tasks:
                cleaned_matched_tasks.append(task_text)
        if cleaned_matched_tasks != matched_tasks:
            normalized["matched_tasks"] = cleaned_matched_tasks
            changed = True

    match_reasons = normalized.get("match_reasons", {})
    if not isinstance(match_reasons, dict):
        normalized["match_reasons"] = {}
        changed = True
    elif excluded_task_set:
        cleaned_match_reasons = {
            str(task_name or "").strip(): reasons
            for task_name, reasons in match_reasons.items()
            if str(task_name or "").strip() and str(task_name or "").strip() not in excluded_task_set
        }
        if cleaned_match_reasons != match_reasons:
            normalized["match_reasons"] = cleaned_match_reasons
            changed = True

    unmatched_reason = str(normalized.get("unmatched_reason", "") or "").strip()
    if unmatched_reason != normalized.get("unmatched_reason", ""):
        normalized["unmatched_reason"] = unmatched_reason
        changed = True

    referenced_tasks = normalized.get("referenced_tasks", [])
    if not isinstance(referenced_tasks, list):
        normalized["referenced_tasks"] = []
        changed = True
    else:
        cleaned_referenced_tasks = []
        for task_name in referenced_tasks:
            task_text = str(task_name or "").strip()
            if task_text and task_text not in excluded_task_set and task_text not in cleaned_referenced_tasks:
                cleaned_referenced_tasks.append(task_text)
        if cleaned_referenced_tasks != referenced_tasks:
            normalized["referenced_tasks"] = cleaned_referenced_tasks
            changed = True

    reference_hits = normalized.get("reference_hits", {})
    if not isinstance(reference_hits, dict):
        normalized["reference_hits"] = {}
        changed = True
    else:
        cleaned_reference_hits = {}
        for task_name, hit in reference_hits.items():
            task_text = str(task_name or "").strip()
            if not task_text or task_text in excluded_task_set:
                continue
            normalized_hit, hit_changed = _normalize_reference_hit(
                hit,
                task_name=task_text,
                article_url=normalized.get("url", ""),
            )
            if normalized_hit:
                cleaned_reference_hits[task_text] = normalized_hit
            changed = changed or hit_changed
        if cleaned_reference_hits != reference_hits:
            normalized["reference_hits"] = cleaned_reference_hits
            changed = True

    export_keyword_categories = normalized.get("export_keyword_categories", [])
    if not isinstance(export_keyword_categories, list):
        normalized["export_keyword_categories"] = []
        changed = True
    else:
        cleaned_categories = _dedupe_texts([
            str(label or "").strip()
            for label in export_keyword_categories
            if str(label or "").strip()
        ])
        if cleaned_categories != export_keyword_categories:
            normalized["export_keyword_categories"] = cleaned_categories
            changed = True

    export_keyword_categories_by_task = normalized.get("export_keyword_categories_by_task", {})
    if not isinstance(export_keyword_categories_by_task, dict):
        normalized["export_keyword_categories_by_task"] = {}
        changed = True
    else:
        cleaned_by_task: dict[str, list[str]] = {}
        for task_name, labels in export_keyword_categories_by_task.items():
            task_text = str(task_name or "").strip()
            if not task_text:
                continue
            cleaned_by_task[task_text] = _dedupe_texts([
                str(label or "").strip()
                for label in (labels or [])
                if str(label or "").strip()
            ])
        if cleaned_by_task != export_keyword_categories_by_task:
            normalized["export_keyword_categories_by_task"] = cleaned_by_task
            changed = True

    for key in ("_export_keyword_config_signature", "_export_keyword_article_signature"):
        value = str(normalized.get(key, "") or "").strip()
        if value != normalized.get(key, ""):
            normalized[key] = value
            changed = True

    return normalized, changed


def _is_authority_domain(domain: str) -> bool:
    for auth_domain in AUTHORITY_DOMAINS:
        if domain == auth_domain or domain.endswith("." + auth_domain):
            return True
    return False


def _domain_override_media_type(domain: str, hinted_media_type: str = "") -> str:
    overrides = _load_domain_overrides()
    override_candidates = []
    for candidate in (domain, get_domain_site_key(domain)):
        normalized = str(candidate or "").strip().lower()
        if normalized and normalized not in override_candidates:
            override_candidates.append(normalized)
    for candidate in override_candidates:
        if candidate not in overrides:
            continue
        override_media_type = str(overrides[candidate] or "").strip().lower()
        if override_media_type in {"authority", "selfmedia"}:
            return override_media_type
    return ""


def classify_article_media_type(url_or_domain: str, media_name: str = "") -> str:
    """
    判断媒体类型。
    优先级：自媒体名称/平台 URL > 手动记忆 > 权威域名 > 媒体名特征 > 默认媒体。
    返回 "authority" 或 "selfmedia"
    """
    raw = str(url_or_domain or "").strip()
    domain = extract_domain(raw) or raw.lower()
    media_name = str(media_name or "").strip()

    if not domain and not media_name:
        return "selfmedia"
    hinted_media_type = _classify_media_name_hint(media_name)

    if hinted_media_type == "selfmedia":
        return "selfmedia"

    platform_media_type = _get_platform_selfmedia_type(raw or domain)
    if platform_media_type:
        return platform_media_type

    override_media_type = _domain_override_media_type(domain, hinted_media_type)
    if override_media_type:
        return override_media_type

    if _is_authority_domain(domain):
        return "authority"
    if hinted_media_type:
        return hinted_media_type
    return "authority"


def classify_domain(domain: str, media_name: str = "") -> str:
    return classify_article_media_type(domain, media_name)


def should_auto_save_media_type(url_or_domain: str, media_type: str, media_name: str = "") -> bool:
    media_type = str(media_type or "").strip().lower()
    if media_type not in {"authority", "selfmedia"}:
        return False
    raw = str(url_or_domain or "").strip()
    domain = extract_domain(raw) or raw.lower()
    hinted_media_type = _classify_media_name_hint(media_name)
    if media_type == "selfmedia":
        return bool(_get_platform_selfmedia_type(raw or domain) or hinted_media_type == "selfmedia")
    return bool(_is_authority_domain(domain) or hinted_media_type == "authority")


# ---------------------------------------------------------------------------
# domain_overrides / domain_media_names CRUD
# ---------------------------------------------------------------------------

def _load_domain_overrides() -> dict:
    with _lock:
        return _load_small_json_document(
            "article_store/domain_overrides",
            _domain_overrides_file(),
            {},
            use_sqlite=DOMAIN_OVERRIDES_FILE == DEFAULT_DOMAIN_OVERRIDES_FILE,
        )


def _save_domain_overrides(overrides: dict) -> None:
    _save_json_document(
        "article_store/domain_overrides",
        _domain_overrides_file(),
        overrides,
        use_sqlite=DOMAIN_OVERRIDES_FILE == DEFAULT_DOMAIN_OVERRIDES_FILE,
        label="domain_overrides",
    )


def _load_domain_media_names() -> dict:
    with _lock:
        return _load_small_json_document(
            "article_store/domain_media_names",
            _domain_media_names_file(),
            {},
            use_sqlite=DOMAIN_MEDIA_NAMES_FILE == DEFAULT_DOMAIN_MEDIA_NAMES_FILE,
        )


def _save_domain_media_names(media_names: dict) -> None:
    _save_json_document(
        "article_store/domain_media_names",
        _domain_media_names_file(),
        media_names,
        use_sqlite=DOMAIN_MEDIA_NAMES_FILE == DEFAULT_DOMAIN_MEDIA_NAMES_FILE,
        label="domain_media_names",
    )


def save_domain_media_name(domain: str, media_name: str, force: bool = False) -> None:
    """
    记忆某域名对应的媒体名。
    force=False 时仅在该域名尚无记录时写入；
    force=True 时允许覆盖旧值。
    同时尽量回填已有文章记录中的 media_name。
    """
    domain = str(domain or "").strip().lower()
    media_name = _clean_media_name_text(str(media_name or "").strip())
    if not domain or not media_name:
        return
    locked_media_name = _canonical_selfmedia_platform_site_media_name(domain)
    if locked_media_name:
        media_name = locked_media_name
        force = True
    if _looks_like_channel_media_name(media_name) and not force:
        return

    changed_articles: list[dict] = []
    shadow_context: dict[str, object] | None = None
    with _lock:
        media_names = _load_domain_media_names()
        site_key = get_domain_site_key(domain)
        target_keys = []
        for candidate in (domain, site_key):
            normalized = str(candidate or "").strip().lower()
            if normalized and normalized not in target_keys:
                target_keys.append(normalized)

        remembered_name = ""
        if not force:
            for key in target_keys:
                remembered_name = str(media_names.get(key, "") or "").strip()
                if remembered_name:
                    break
        effective_media_name = remembered_name or media_name

        for key in target_keys:
            old_name = str(media_names.get(key, "") or "").strip()
            if old_name and not force:
                continue
            media_names[key] = effective_media_name
        _save_domain_media_names(media_names)

        articles = _article_sqlite_store().list_articles() if _article_store_sqlite_enabled() else _load_articles()
        changed = False
        for article in articles:
            article_domain = extract_domain(article.get("url", ""))
            if not _domains_share_site_key(article_domain, domain):
                continue
            current_name = str(article.get("media_name", "") or "").strip()
            if locked_media_name or force or not current_name or current_name == article_domain:
                article["media_name"] = effective_media_name
                changed = True
                changed_articles.append(dict(article))
        if changed:
            if _article_store_sqlite_enabled():
                _article_sqlite_store().bulk_upsert_articles(changed_articles)
            else:
                shadow_context = _begin_article_shadow_runtime_write()
                _save_articles(articles)
    if changed_articles and shadow_context is not None:
        _sync_article_shadow_articles(
            changed_articles,
            reason="domain_media_name_backfill",
            context=shadow_context,
        )


def save_domain_override(domain: str, media_type: str, force: bool = False) -> None:
    """
    记忆某域名的媒体类型。
    force=True 时强制覆盖（手动切换场景）；
    force=False 时仅在该域名尚无记录时写入（自动识别场景）。
    同时批量更新 articles.json 中同域名的已有记录（仅 force=True 时）。
    """
    domain = str(domain or "").strip().lower()
    media_type = str(media_type or "").strip().lower()
    if not domain:
        return
    forced_media_type = _get_platform_selfmedia_type(domain)
    if forced_media_type:
        media_type = forced_media_type
    elif media_type not in {"authority", "selfmedia"}:
        media_type = "selfmedia"
    changed_articles: list[dict] = []
    shadow_context: dict[str, object] | None = None
    with _lock:
        overrides = _load_domain_overrides()
        site_key = get_domain_site_key(domain)
        override_keys = []
        for candidate in (domain, site_key):
            normalized = str(candidate or "").strip().lower()
            if normalized and normalized not in override_keys:
                override_keys.append(normalized)

        remembered_media_type = ""
        if not force:
            for key in override_keys:
                remembered_media_type = str(overrides.get(key, "") or "").strip().lower()
                if remembered_media_type in {"authority", "selfmedia"}:
                    break
        effective_media_type = remembered_media_type or media_type
        if force:
            for saved_domain in list(overrides.keys()):
                if _domains_share_site_key(saved_domain, domain):
                    overrides[saved_domain] = effective_media_type
        for key in override_keys:
            old_type = str(overrides.get(key, "") or "").strip().lower()
            if old_type and not force:
                continue
            overrides[key] = effective_media_type
        _save_domain_overrides(overrides)

        # 站点级记忆后，同站点已有记录也一并回填，避免不同入口重复识别后状态不一致。
        if force or site_key:
            articles = _article_sqlite_store().list_articles() if _article_store_sqlite_enabled() else _load_articles()
            changed = False
            for article in articles:
                article_domain = extract_domain(article.get("url", ""))
                if _domains_share_site_key(article_domain, domain):
                    article["media_type"] = effective_media_type
                    changed = True
                    changed_articles.append(dict(article))
            if changed:
                if _article_store_sqlite_enabled():
                    _article_sqlite_store().bulk_upsert_articles(changed_articles)
                else:
                    shadow_context = _begin_article_shadow_runtime_write()
                    _save_articles(articles)
    if changed_articles and shadow_context is not None:
        _sync_article_shadow_articles(
            changed_articles,
            reason="domain_media_type_backfill",
            context=shadow_context,
        )


# ---------------------------------------------------------------------------
# articles CRUD
# ---------------------------------------------------------------------------

def _load_articles() -> list:
    with _lock:
        data = _load_json_document(
            "article_store/articles",
            _articles_file(),
            [],
            use_sqlite=ARTICLES_FILE == DEFAULT_ARTICLES_FILE,
        )
        normalized_articles = []
        changed = False
        for article in data:
            if not isinstance(article, dict):
                changed = True
                continue
            normalized, item_changed = _normalize_article_entry(article)
            normalized_articles.append(normalized)
            changed = changed or item_changed
        if changed:
            _save_articles(normalized_articles)
        return normalized_articles


def _save_articles(articles: list) -> None:
    _save_json_document(
        "article_store/articles",
        _articles_file(),
        articles,
        use_sqlite=ARTICLES_FILE == DEFAULT_ARTICLES_FILE,
        label="articles.json",
    )


def _article_sort_timestamp(value) -> int:
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


def _first_article_sort_timestamp(article: dict, keys: tuple[str, ...]) -> int:
    for key in keys:
        value = article.get(key)
        timestamp = _article_sort_timestamp(value)
        if timestamp > 0:
            return timestamp
    return 0


def _sort_articles_for_display(articles: list) -> list:
    def sort_key(article: dict) -> tuple[int, int, str]:
        published_timestamp = _first_article_sort_timestamp(
            article,
            ("published_ts", "published_at", "published", "ts"),
        )
        imported_timestamp = _first_article_sort_timestamp(
            article,
            ("imported_at", "created_at", "ts"),
        )
        return (published_timestamp, imported_timestamp, str(article.get("id") or ""))

    return sorted(articles, key=sort_key, reverse=True)


def _load_excluded_article_urls() -> dict:
    with _lock:
        return _load_small_json_document(
            "article_store/excluded_article_urls",
            _excluded_article_urls_file(),
            {},
            use_sqlite=EXCLUDED_ARTICLE_URLS_FILE == DEFAULT_EXCLUDED_ARTICLE_URLS_FILE,
        )


def _trim_excluded_article_urls_for_storage(excluded_urls: dict) -> tuple[dict, bool]:
    if not isinstance(excluded_urls, dict):
        return {}, True
    capacity = _safe_capacity(MAX_EXCLUDED_ARTICLE_URLS, 5000)
    if capacity <= 0:
        return {}, bool(excluded_urls)
    if len(excluded_urls) <= capacity:
        return excluded_urls, False
    indexed_items = [
        (index, key, value)
        for index, (key, value) in enumerate(excluded_urls.items())
        if str(key or "").strip()
    ]
    indexed_items.sort(
        key=lambda item: (
            _excluded_article_url_sort_text(item[2]),
            item[0],
        )
    )
    kept_items = indexed_items[-capacity:]
    return {key: value for _, key, value in kept_items}, True


def _excluded_article_url_sort_text(value) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("updated_at") or value.get("excluded_at") or "")


def _save_excluded_article_urls(excluded_urls: dict) -> None:
    _save_json_document(
        "article_store/excluded_article_urls",
        _excluded_article_urls_file(),
        excluded_urls,
        use_sqlite=EXCLUDED_ARTICLE_URLS_FILE == DEFAULT_EXCLUDED_ARTICLE_URLS_FILE,
        label="excluded_article_urls",
    )


def exclude_article_url(url: str, *, title: str = "", source: str = "manual_delete") -> dict | None:
    """记录手动删除过的文章 URL，供账号自动抓取时排除。"""
    normalized_url = normalize_article_url(url)
    if not normalized_url:
        return None

    now_text = _article_now_second_text()
    with _lock:
        excluded_urls = _load_excluded_article_urls()
        existing = dict(excluded_urls.get(normalized_url) or {})
        existing.update({
            "url": normalized_url,
            "title": str(title or existing.get("title") or "").strip(),
            "source": str(source or existing.get("source") or "manual_delete").strip() or "manual_delete",
            "excluded_at": str(existing.get("excluded_at") or now_text),
            "updated_at": now_text,
        })
        try:
            existing["count"] = int(existing.get("count", 0) or 0) + 1
        except Exception:
            existing["count"] = 1
        excluded_urls[normalized_url] = existing
        excluded_urls, _ = _trim_excluded_article_urls_for_storage(excluded_urls)
        _save_excluded_article_urls(excluded_urls)
        return dict(existing)


def is_article_url_excluded(url: str) -> bool:
    normalized_url = normalize_article_url(url)
    if not normalized_url:
        return False
    with _lock:
        return normalized_url in _load_excluded_article_urls()


def get_excluded_article_urls(limit: int = 200) -> list[dict]:
    with _lock:
        excluded_urls = _load_excluded_article_urls()
    items = [
        dict(value)
        for value in excluded_urls.values()
        if isinstance(value, dict) and str(value.get("url") or "").strip()
    ]
    items.sort(key=lambda item: str(item.get("updated_at") or item.get("excluded_at") or ""), reverse=True)
    return items[:max(0, int(limit or 0))]


def restore_excluded_article_urls(urls: list[str] | tuple[str, ...] | set[str]) -> dict:
    normalized_urls = []
    for url in urls or []:
        normalized_url = normalize_article_url(str(url or "").strip())
        if normalized_url and normalized_url not in normalized_urls:
            normalized_urls.append(normalized_url)
    if not normalized_urls:
        return {
            "ok": False,
            "message": "没有可恢复的链接",
            "removed_count": 0,
            "urls": [],
        }

    removed = []
    with _lock:
        excluded_urls = _load_excluded_article_urls()
        for normalized_url in normalized_urls:
            if normalized_url in excluded_urls:
                removed.append(normalized_url)
                excluded_urls.pop(normalized_url, None)
        if removed:
            _save_excluded_article_urls(excluded_urls)

    return {
        "ok": True,
        "message": f"已移除 {len(removed)} 条排除链接" if removed else "这些链接当前不在排除列表中",
        "removed_count": len(removed),
        "urls": removed,
    }


def _clear_excluded_article_urls_for_normalized_urls(normalized_urls: set[str]) -> None:
    urls = {
        normalize_article_url(url)
        for url in (normalized_urls or set())
        if normalize_article_url(url)
    }
    if not urls:
        return
    excluded_urls = _load_excluded_article_urls()
    changed = False
    for normalized_url in urls:
        if normalized_url in excluded_urls:
            excluded_urls.pop(normalized_url, None)
            changed = True
    if changed:
        _save_excluded_article_urls(excluded_urls)


def _add_article_sqlite(entry: dict) -> dict:
    store = _article_sqlite_store()
    with _lock:
        normalized_url = normalize_article_url((entry or {}).get("url", ""))
        existing = store.get_article_by_url(normalized_url) if normalized_url else None
        article = store.add_article(dict(entry or {}))
        if existing is None and normalized_url:
            _clear_excluded_article_urls_for_normalized_urls({normalized_url})
        return article


def _bulk_upsert_articles_sqlite(entries: list[dict] | tuple[dict, ...]) -> list[dict]:
    store = _article_sqlite_store()
    with _lock:
        results = store.bulk_upsert_articles(entries or [])
        restored_urls = {
            normalize_article_url(article.get("url", ""))
            for article in results
            if isinstance(article, dict) and normalize_article_url(article.get("url", ""))
        }
        _clear_excluded_article_urls_for_normalized_urls(restored_urls)
        return results


def _update_article_sqlite(article_id: str, patch: dict) -> dict | None:
    with _lock:
        return _article_sqlite_store().update_article(article_id, patch or {})


def _delete_article_sqlite(article_id: str, *, exclude_url: bool = True) -> dict | None:
    with _lock:
        removed = _article_sqlite_store().delete_article(article_id)
        if removed is not None and exclude_url:
            exclude_article_url(
                str(removed.get("url") or "").strip(),
                title=str(removed.get("title") or "").strip(),
                source="manual_delete",
            )
        return removed


def _confirm_article_import_batch_sqlite(
    article_ids: list[str] | tuple[str, ...] | set[str],
    updated_article_ids: list[str] | tuple[str, ...] | set[str],
    *,
    import_id: str,
    confirmed_at: str,
) -> int:
    target_ids = {
        str(article_id or "").strip()
        for article_id in list(article_ids or []) + list(updated_article_ids or [])
        if str(article_id or "").strip()
    }
    if not target_ids:
        return 0
    store = _article_sqlite_store()
    with _lock:
        updated_articles = store.bulk_confirm_import(
            target_ids,
            import_id=str(import_id or "").strip(),
            confirmed_at=str(confirmed_at or "").strip(),
        )
    return len(updated_articles)


def _undo_article_import_batch_sqlite(
    article_ids: list[str] | tuple[str, ...] | set[str],
    updated_articles: list[dict] | tuple[dict, ...] = (),
) -> dict[str, int]:
    remove_ids = {
        str(article_id or "").strip()
        for article_id in (article_ids or [])
        if str(article_id or "").strip()
    }
    restore_clear_fields = {
        "account_name": "",
        "published_at": "",
        "ts": "",
        "excerpt": "",
        "import_batch_id": "",
        "import_status": "",
        "import_file_name": "",
        "imported_from_sheet": "",
        "imported_from_row": 0,
        "last_table_import_at": "",
        "last_table_import_file": "",
        "import_confirmed_at": "",
        "matched_tasks": [],
        "match_reasons": {},
        "unmatched_reason": "",
    }
    restore_by_id: dict[str, dict] = {}
    for item in updated_articles or []:
        if not isinstance(item, dict):
            continue
        article_id = str(item.get("id") or "").strip()
        before = item.get("before") if isinstance(item.get("before"), dict) else None
        if not article_id or not before:
            continue
        restore_patch = dict(restore_clear_fields)
        restore_patch.update(before)
        restore_by_id[article_id] = restore_patch
    if not remove_ids and not restore_by_id:
        return {"removed_count": 0, "restored_count": 0}
    removed_count = 0
    restored_count = 0
    store = _article_sqlite_store()
    with _lock:
        for article_id in remove_ids:
            if store.delete_article(article_id) is not None:
                removed_count += 1
        for article_id, restore_patch in restore_by_id.items():
            if store.update_article(article_id, restore_patch) is not None:
                restored_count += 1
    return {"removed_count": removed_count, "restored_count": restored_count}


def _remove_article_from_task_sqlite(article_id: str, task_name: str) -> dict | None:
    article_id = str(article_id or "").strip()
    task_name = str(task_name or "").strip()
    if not article_id or not task_name:
        return None
    store = _article_sqlite_store()
    with _lock:
        article = store.get_article_by_id(article_id)
        if article is None:
            return None
        updated = dict(article)
        excluded_tasks = [
            str(name or "").strip()
            for name in (updated.get("excluded_tasks") or [])
            if str(name or "").strip()
        ]
        if task_name not in excluded_tasks:
            excluded_tasks.append(task_name)
        updated["excluded_tasks"] = excluded_tasks
        updated["matched_tasks"] = [
            str(name or "").strip()
            for name in (updated.get("matched_tasks") or [])
            if str(name or "").strip() and str(name or "").strip() != task_name
        ]
        match_reasons = updated.get("match_reasons") if isinstance(updated.get("match_reasons"), dict) else {}
        if task_name in match_reasons:
            match_reasons = dict(match_reasons)
            match_reasons.pop(task_name, None)
        updated["match_reasons"] = match_reasons
        updated["referenced_tasks"] = [
            str(name or "").strip()
            for name in (updated.get("referenced_tasks") or [])
            if str(name or "").strip() and str(name or "").strip() != task_name
        ]
        reference_hits = updated.get("reference_hits") if isinstance(updated.get("reference_hits"), dict) else {}
        if task_name in reference_hits:
            reference_hits = dict(reference_hits)
            reference_hits.pop(task_name, None)
        updated["reference_hits"] = reference_hits
        if not updated["matched_tasks"]:
            updated["unmatched_reason"] = f"已从品牌“{task_name}”文章汇总移除"
        normalized, _ = _normalize_article_entry(updated)
        return store.update_article(article_id, normalized)


def _update_media_type_sqlite(article_id: str, media_type: str) -> bool:
    article_id = str(article_id or "").strip()
    media_type = str(media_type or "").strip()
    if not article_id:
        return False
    store = _article_sqlite_store()
    with _lock:
        article = store.get_article_by_id(article_id)
        if article is None:
            return False
        domain = extract_domain(article.get("url", ""))
        updated = store.update_article(article_id, {"media_type": media_type})
    if updated is not None and domain:
        save_domain_override(domain, media_type, force=True)
    return updated is not None


def _prune_cloud_articles_by_visible_task_ids_sqlite(
    visible_cloud_task_ids: list[int] | set[int] | tuple[int, ...],
    *,
    visible_cloud_article_ids: list[int] | set[int] | tuple[int, ...] | None = None,
    visible_cloud_url_hashes: list[str] | set[str] | tuple[str, ...] | None = None,
) -> dict:
    visible_ids = {
        int(task_id)
        for task_id in visible_cloud_task_ids or []
        if _safe_positive_int(task_id) is not None
    }
    article_scope_enabled = visible_cloud_article_ids is not None or visible_cloud_url_hashes is not None
    visible_article_ids = {
        int(article_id)
        for article_id in visible_cloud_article_ids or []
        if _safe_positive_int(article_id) is not None
    }
    visible_url_hashes = {
        str(url_hash or "").strip()
        for url_hash in visible_cloud_url_hashes or []
        if str(url_hash or "").strip()
    }
    updated_articles: list[dict] = []
    articles = _article_sqlite_store().list_articles()
    pruned_links = 0
    for article in articles:
        if not _is_cloud_downloaded_article(article):
            continue
        if article_scope_enabled:
            cloud_article_id = _safe_positive_int(article.get("cloud_article_id"))
            cloud_url_hash = str(article.get("cloud_url_hash") or "").strip()
            in_visible_article_scope = (
                (cloud_article_id is not None and cloud_article_id in visible_article_ids)
                or (cloud_url_hash and cloud_url_hash in visible_url_hashes)
            )
            if not in_visible_article_scope:
                next_article = dict(article)
                original_task_ids = [
                    task_id
                    for task_id in (_safe_positive_int(item) for item in article.get("cloud_task_ids") or [])
                    if task_id is not None
                ]
                hidden_task_ids = [task_id for task_id in original_task_ids if task_id not in visible_ids]
                if hidden_task_ids != original_task_ids or article.get("matched_tasks") or article.get("match_reasons"):
                    next_article["cloud_task_ids"] = hidden_task_ids
                    next_article["matched_tasks"] = []
                    next_article["match_reasons"] = {}
                    next_article["unmatched_reason"] = "云端文章当前不在当前账号可见范围"
                    pruned_links += 1
                    updated_articles.append(dict(next_article))
                continue
        cloud_task_ids = [
            task_id
            for task_id in (_safe_positive_int(item) for item in article.get("cloud_task_ids") or [])
            if task_id is not None
        ]
        visible_article_task_ids = [task_id for task_id in cloud_task_ids if task_id in visible_ids]
        if not visible_article_task_ids:
            continue
        if visible_article_task_ids != cloud_task_ids:
            next_article = dict(article)
            next_article["cloud_task_ids"] = visible_article_task_ids
            pruned_links += 1
            updated_articles.append(dict(next_article))
    if updated_articles:
        _article_sqlite_store().bulk_upsert_articles(updated_articles)
    return {
        "removed": 0,
        "pruned_links": pruned_links,
        "remaining": len(articles),
    }


def _refresh_article_matches_sqlite(config: dict) -> list:
    compiled_matcher = compile_article_matcher(config)
    config_signature = compiled_matcher.config_signature
    export_keyword_plan = build_article_export_keyword_plan(config)
    export_keyword_config_signature = build_article_export_keyword_signature(config)
    store = _article_sqlite_store()
    stats = store.get_match_refresh_stats(config_signature)
    if int(stats.get("needs_refresh_count") or 0) <= 0:
        # API compatibility still requires returning the full display list; the
        # expensive match analysis/write phase is skipped on the warm path.
        return store.list_articles()

    for batch in store.iter_articles_needing_match(config_signature, batch_size=500):
        updates: list[dict] = []
        for article in batch:
            excluded_task_names = {
                str(name or "").strip()
                for name in (article.get("excluded_tasks") or [])
                if str(name or "").strip()
            }
            raw_matched = [
                str(name or "").strip()
                for name in (article.get("matched_tasks") or [])
                if str(name or "").strip()
            ]
            stored = [
                str(name or "").strip()
                for name in (article.get("matched_tasks") or [])
                if str(name or "").strip()
                and str(name or "").strip() not in excluded_task_names
            ]
            existing_reasons = article.get("match_reasons") if isinstance(article.get("match_reasons"), dict) else {}
            fields = _build_match_fields(str(article.get("title", "") or ""), article)
            current_signature = _article_match_signature(article, config_signature, fields=fields)
            metadata_changed = (
                str(article.get("_match_signature") or "") != current_signature
                or str(article.get("_match_config_signature") or "") != config_signature
            )
            if str(article.get("_match_signature") or "") == current_signature:
                match_reasons = {
                    task_name: existing_reasons.get(task_name) or ["保留历史归类"]
                    for task_name in stored
                }
                unmatched_reason = "" if stored else str(article.get("unmatched_reason", "") or "").strip()
                update_payload = {
                    "id": article.get("id"),
                    "matched_tasks": stored,
                    "match_reasons": match_reasons,
                    "unmatched_reason": unmatched_reason,
                    "_match_signature": current_signature,
                    "_match_config_signature": config_signature,
                }
                cache_article = dict(article)
                cache_article.update(update_payload)
                cache_changed = update_article_export_keyword_cache(
                    cache_article,
                    config,
                    keyword_plan=export_keyword_plan,
                    config_signature=export_keyword_config_signature,
                )
                if (
                    stored != raw_matched
                    or match_reasons != existing_reasons
                    or unmatched_reason != str(article.get("unmatched_reason", "") or "").strip()
                    or metadata_changed
                    or cache_changed
                ):
                    update_payload.update(article_export_keyword_cache_fields(cache_article))
                    updates.append(update_payload)
                continue

            analyzed = analyze_article_matches(
                article.get("title", ""),
                config,
                article=article,
                compiled_matcher=compiled_matcher,
                fields=fields,
            )
            inferred = [
                str(name or "").strip()
                for name in (analyzed.get("matched_tasks") or [])
                if str(name or "").strip()
                and str(name or "").strip() not in excluded_task_names
            ]
            inferred_reasons = {
                str(name or "").strip(): [
                    str(reason or "").strip()
                    for reason in reasons
                    if str(reason or "").strip()
                ]
                for name, reasons in (analyzed.get("match_reasons") or {}).items()
                if str(name or "").strip()
                and str(name or "").strip() not in excluded_task_names
            }
            merged = []
            for task_name in stored + inferred:
                if task_name and task_name not in merged:
                    merged.append(task_name)
            match_reasons = {
                task_name: inferred_reasons.get(task_name) or ["保留历史归类"]
                for task_name in merged
            }
            unmatched_reason = "" if merged else str(analyzed.get("unmatched_reason", "") or "").strip()
            update_payload = {
                "id": article.get("id"),
                "matched_tasks": merged,
                "match_reasons": match_reasons,
                "unmatched_reason": unmatched_reason,
                "_match_signature": current_signature,
                "_match_config_signature": config_signature,
            }
            cache_article = dict(article)
            cache_article.update(update_payload)
            update_article_export_keyword_cache(
                cache_article,
                config,
                keyword_plan=export_keyword_plan,
                config_signature=export_keyword_config_signature,
            )
            update_payload.update(article_export_keyword_cache_fields(cache_article))
            updates.append(update_payload)
        if updates:
            store.bulk_update_match_fields(updates)

    # Returning the full list is intentionally retained for caller compatibility;
    # SQLite avoids the full analyze/write pass even though this read can still be large.
    return store.list_articles()


def _mark_articles_referenced_by_urls_sqlite(
    task_names: list[str] | tuple[str, ...] | set[str],
    urls: list[str] | tuple[str, ...] | set[str],
    *,
    source: str = "recognition",
    referenced_at: str = "",
    platform: str = "",
    event_id: str = "",
    record_id: str = "",
) -> dict:
    target_task_names = []
    for task_name in task_names or []:
        task_text = str(task_name or "").strip()
        if task_text and task_text not in target_task_names:
            target_task_names.append(task_text)

    normalized_urls = []
    for url in urls or []:
        normalized_url = normalize_article_url(str(url or "").strip())
        if normalized_url and normalized_url not in normalized_urls:
            normalized_urls.append(normalized_url)

    if not normalized_urls:
        return {
            "matched_count": 0,
            "updated_count": 0,
            "task_names": target_task_names,
            "urls": [],
            "articles": [],
        }

    url_set = set(normalized_urls)
    reference_source = str(source or "recognition").strip() or "recognition"
    reference_time = str(referenced_at or "").strip() or _article_now_second_text()
    normalized_platform = normalize_platform_id(platform)

    matched_articles = []
    updated_count = 0
    updated_articles: list[dict] = []
    # 走 normalized_url UNIQUE 索引精确查询，O(M log N)；避免 list_articles() 全表
    # 加载 + Python O(N) 扫（在 articles 表上万条时差异显著）。下面仍保留
    # article_url not in url_set 的 Python 端二次校验，防止 normalize 版本漂移导致误命中。
    articles = _article_sqlite_store().list_articles_by_normalized_urls(list(url_set))
    for article in articles:
        article_url = normalize_article_url(article.get("url", ""))
        if not article_url or article_url not in url_set:
            continue

        article_tasks = [
            str(name or "").strip()
            for name in (article.get("matched_tasks") or [])
            if str(name or "").strip()
        ]
        if target_task_names:
            matched_task_names = [
                task_name
                for task_name in target_task_names
                if task_name in article_tasks
            ]
            if not matched_task_names:
                continue
        else:
            matched_task_names = article_tasks

        if not matched_task_names:
            continue

        referenced_tasks = [
            str(name or "").strip()
            for name in (article.get("referenced_tasks") or [])
            if str(name or "").strip()
        ]
        reference_hits = article.get("reference_hits") if isinstance(article.get("reference_hits"), dict) else {}
        before_tasks = list(referenced_tasks)
        before_hits = json.dumps(reference_hits, ensure_ascii=False, sort_keys=True)

        for task_name in matched_task_names:
            if task_name not in referenced_tasks:
                referenced_tasks.append(task_name)
            hit = dict(reference_hits.get(task_name) or {})
            normalized_hit, _ = _normalize_reference_hit(
                hit,
                task_name=task_name,
                article_url=article_url,
            )
            hit = normalized_hit or {}
            events = list(hit.get("events") or [])
            explicit_event_id = _make_reference_event_id(
                explicit_event_id=event_id,
                record_id=record_id,
                task_name=task_name,
                article_url=article_url,
                platform=normalized_platform,
                referenced_at=reference_time,
                source=reference_source,
            )
            added_event = False
            if not any(str(item.get("event_id") or "").strip() == explicit_event_id for item in events if isinstance(item, dict)):
                event_payload = {
                    "event_id": explicit_event_id,
                    "referenced_at": reference_time,
                    "source": reference_source,
                    "article_url": article_url,
                    "task_name": task_name,
                }
                if normalized_platform:
                    event_payload["platform"] = normalized_platform
                if record_id:
                    event_payload["record_id"] = str(record_id or "").strip()
                events.append(event_payload)
                added_event = True

            try:
                count = int(hit.get("count", 0) or 0)
            except Exception:
                count = 0
            if added_event:
                count += 1
            events, trimmed_events = _trim_reference_events_for_storage(events)
            hit.update({
                "last_referenced_at": _max_timestamp_text(str(hit.get("last_referenced_at") or ""), reference_time) if added_event else str(hit.get("last_referenced_at") or "").strip(),
                "source": reference_source,
                "count": max(count, 0),
                "events": events,
            })
            if trimmed_events:
                hit["events_compacted"] = True
            reference_hits[task_name] = hit

        article["referenced_tasks"] = referenced_tasks
        article["reference_hits"] = reference_hits
        after_hits = json.dumps(reference_hits, ensure_ascii=False, sort_keys=True)
        item_changed = referenced_tasks != before_tasks or after_hits != before_hits
        if item_changed:
            updated_count += 1
            updated_articles.append(dict(article))
        matched_articles.append({
            "id": article.get("id", ""),
            "title": article.get("title", ""),
            "url": article.get("url", ""),
            "tasks": list(matched_task_names),
        })

    if updated_articles:
        _article_sqlite_store().bulk_upsert_articles(updated_articles)

    return {
        "matched_count": len(matched_articles),
        "updated_count": updated_count,
        "task_names": target_task_names,
        "urls": normalized_urls,
        "articles": matched_articles,
    }


def _import_article_store_bundle_sqlite(
    *,
    normalized_mode: str,
    normalized_articles: list[dict],
    normalized_overrides: dict,
    normalized_media_names: dict,
    normalized_excluded_urls: dict,
) -> dict:
    store = _article_sqlite_store()
    with _lock:
        existing_articles = store.list_articles()
        created_articles = 0
        updated_articles = 0

        if normalized_mode == "replace":
            next_articles = list(normalized_articles)
            created_articles = len(next_articles)
        else:
            next_articles = list(existing_articles)
            index_by_id = {
                str(article.get("id", "")).strip(): index
                for index, article in enumerate(next_articles)
                if str(article.get("id", "")).strip()
            }
            index_by_url = {
                normalize_article_url(article.get("url", "")): index
                for index, article in enumerate(next_articles)
                if normalize_article_url(article.get("url", ""))
            }

            for article in normalized_articles:
                article_id = str(article.get("id", "")).strip()
                normalized_url = normalize_article_url(article.get("url", ""))
                target_index = None
                if normalized_url:
                    target_index = index_by_url.get(normalized_url)
                if target_index is None and article_id:
                    target_index = index_by_id.get(article_id)

                if target_index is None:
                    next_articles.append(article)
                    created_articles += 1
                    target_index = len(next_articles) - 1
                else:
                    merged = dict(next_articles[target_index])
                    merged.update(article)
                    if article_id:
                        merged["id"] = article_id
                    next_articles[target_index] = merged
                    updated_articles += 1

                final_item = next_articles[target_index]
                final_id = str(final_item.get("id", "")).strip()
                final_url = normalize_article_url(final_item.get("url", ""))
                if final_id:
                    index_by_id[final_id] = target_index
                if final_url:
                    index_by_url[final_url] = target_index

        if normalized_mode == "replace":
            next_overrides = dict(normalized_overrides)
            next_media_names = dict(normalized_media_names)
            next_excluded_urls = dict(normalized_excluded_urls)
        else:
            next_overrides = dict(_load_domain_overrides())
            next_overrides.update(normalized_overrides)
            next_media_names = dict(_load_domain_media_names())
            next_media_names.update(normalized_media_names)
            next_excluded_urls = dict(_load_excluded_article_urls())
            next_excluded_urls.update(normalized_excluded_urls)

        next_excluded_urls, _ = _trim_excluded_article_urls_for_storage(next_excluded_urls)
        store.import_from_articles(next_articles, replace=True)
        _save_domain_overrides(next_overrides)
        _save_domain_media_names(next_media_names)
        _save_excluded_article_urls(next_excluded_urls)

    return {
        "mode": normalized_mode,
        "articles_total": len(next_articles),
        "articles_created": created_articles,
        "articles_updated": updated_articles,
        "domain_overrides_total": len(next_overrides),
        "domain_media_names_total": len(next_media_names),
        "excluded_article_urls_total": len(next_excluded_urls),
    }


def add_article(entry: dict) -> dict:
    """
    新增一条文章记录，自动补全 id 和 ts。
    返回补全后的完整 entry。
    """
    if _article_store_sqlite_enabled():
        return _add_article_sqlite(entry)

    entry = dict(entry)
    entry.setdefault("id", uuid.uuid4().hex)
    now_text = _article_now_minute_text()
    entry.setdefault("ts", now_text)
    entry.setdefault("url", "")
    entry.setdefault("title", "")
    entry.setdefault("platform", "")
    entry.setdefault("media_name", "")
    entry.setdefault("media_type", "selfmedia")
    entry.setdefault("excerpt", "")
    entry.setdefault("imported_at", now_text)
    entry.setdefault("matched_tasks", [])
    entry.setdefault("match_reasons", {})
    entry.setdefault("unmatched_reason", "")
    entry.setdefault("fetch_method", "html")
    entry, _ = _normalize_article_entry(entry)
    normalized_url = normalize_article_url(entry.get("url", ""))

    added_article: dict | None = None
    shadow_context: dict[str, object] | None = None
    with _lock:
        articles = _load_articles()
        if normalized_url:
            for article in articles:
                if normalize_article_url(article.get("url", "")) == normalized_url:
                    return article
        articles.append(entry)
        shadow_context = _begin_article_shadow_runtime_write()
        _save_articles(articles)
        added_article = dict(entry)
        if normalized_url:
            excluded_urls = _load_excluded_article_urls()
            if normalized_url in excluded_urls:
                excluded_urls.pop(normalized_url, None)
                _save_excluded_article_urls(excluded_urls)
    if added_article is not None and shadow_context is not None:
        _sync_article_shadow_articles([added_article], reason="add_article", context=shadow_context)
    return entry


def bulk_upsert_articles(entries: list[dict] | tuple[dict, ...]) -> list[dict]:
    """Create or update multiple article records with a single articles.json write."""
    if _article_store_sqlite_enabled():
        return _bulk_upsert_articles_sqlite(entries)

    prepared_entries = [dict(entry) for entry in (entries or []) if isinstance(entry, dict)]
    if not prepared_entries:
        return []

    now_text = _article_now_minute_text()
    results: list[dict] = []
    shadow_context: dict[str, object] | None = None
    with _lock:
        articles = _load_articles()
        index_by_id = {
            str(article.get("id", "")).strip(): index
            for index, article in enumerate(articles)
            if str(article.get("id", "")).strip()
        }
        index_by_url = {
            normalize_article_url(article.get("url", "")): index
            for index, article in enumerate(articles)
            if normalize_article_url(article.get("url", ""))
        }
        restored_urls: set[str] = set()

        for raw_entry in prepared_entries:
            entry = dict(raw_entry)
            article_id = str(entry.get("id", "") or "").strip()
            normalized_url = normalize_article_url(entry.get("url", ""))
            target_index = index_by_id.get(article_id) if article_id else None
            if target_index is None and normalized_url:
                target_index = index_by_url.get(normalized_url)

            if target_index is None:
                entry.setdefault("id", uuid.uuid4().hex)
                entry.setdefault("ts", now_text)
                entry.setdefault("url", "")
                entry.setdefault("title", "")
                entry.setdefault("platform", "")
                entry.setdefault("media_name", "")
                entry.setdefault("media_type", "selfmedia")
                entry.setdefault("excerpt", "")
                entry.setdefault("imported_at", now_text)
                entry.setdefault("matched_tasks", [])
                entry.setdefault("match_reasons", {})
                entry.setdefault("unmatched_reason", "")
                entry.setdefault("fetch_method", "html")
                normalized, _ = _normalize_article_entry(entry)
                articles.append(normalized)
                target_index = len(articles) - 1
            else:
                merged = dict(articles[target_index])
                merged.update(entry)
                merged["id"] = articles[target_index].get("id", merged.get("id", "")) or article_id or uuid.uuid4().hex
                if "ts" not in entry or not str(entry.get("ts") or "").strip():
                    merged["ts"] = articles[target_index].get("ts", merged.get("ts", ""))
                merged.setdefault("url", "")
                merged.setdefault("title", "")
                merged.setdefault("platform", "")
                merged.setdefault("media_name", "")
                merged.setdefault("media_type", "selfmedia")
                merged.setdefault("excerpt", "")
                merged.setdefault(
                    "imported_at",
                    articles[target_index].get("imported_at", "")
                    or articles[target_index].get("created_at", "")
                    or articles[target_index].get("ts", "")
                    or now_text,
                )
                merged.setdefault("matched_tasks", [])
                merged.setdefault("match_reasons", {})
                merged.setdefault("unmatched_reason", "")
                merged.setdefault("fetch_method", "html")
                normalized, _ = _normalize_article_entry(merged)
                articles[target_index] = normalized

            final_item = articles[target_index]
            final_id = str(final_item.get("id", "") or "").strip()
            final_url = normalize_article_url(final_item.get("url", ""))
            if final_id:
                index_by_id[final_id] = target_index
            if final_url:
                index_by_url[final_url] = target_index
                restored_urls.add(final_url)
            results.append(dict(final_item))

        shadow_context = _begin_article_shadow_runtime_write()
        _save_articles(articles)
        if restored_urls:
            excluded_urls = _load_excluded_article_urls()
            changed_exclusions = False
            for normalized_url in restored_urls:
                if normalized_url in excluded_urls:
                    excluded_urls.pop(normalized_url, None)
                    changed_exclusions = True
            if changed_exclusions:
                _save_excluded_article_urls(excluded_urls)

    if results and shadow_context is not None:
        _sync_article_shadow_articles(results, reason="bulk_upsert_articles", context=shadow_context)
    return results


def update_article(article_id: str, patch: dict) -> dict | None:
    """按文章 ID 更新记录并返回最新内容。"""
    if _article_store_sqlite_enabled():
        return _update_article_sqlite(article_id, patch)

    article_id = str(article_id or "").strip()
    if not article_id:
        return None

    updates = dict(patch or {})
    updated_article: dict | None = None
    shadow_context: dict[str, object] | None = None
    with _lock:
        articles = _load_articles()
        for index, article in enumerate(articles):
            if str(article.get("id", "")).strip() != article_id:
                continue
            merged = dict(article)
            merged.update(updates)
            merged["id"] = article.get("id", merged.get("id", ""))
            if "ts" not in updates or not str(updates.get("ts") or "").strip():
                merged["ts"] = article.get("ts", merged.get("ts", ""))
            merged.setdefault("url", "")
            merged.setdefault("title", "")
            merged.setdefault("platform", "")
            merged.setdefault("media_name", "")
            merged.setdefault("media_type", "selfmedia")
            merged.setdefault("excerpt", "")
            merged.setdefault(
                "imported_at",
                article.get("imported_at", "")
                or article.get("created_at", "")
                or article.get("ts", "")
                or _article_now_minute_text(),
            )
            merged.setdefault("matched_tasks", [])
            merged.setdefault("match_reasons", {})
            merged.setdefault("unmatched_reason", "")
            merged.setdefault("fetch_method", "html")
            merged, _ = _normalize_article_entry(merged)
            articles[index] = merged
            shadow_context = _begin_article_shadow_runtime_write()
            _save_articles(articles)
            updated_article = dict(merged)
            break
    if updated_article is not None and shadow_context is not None:
        _sync_article_shadow_articles([updated_article], reason="update_article", context=shadow_context)
        return updated_article
    return None


def confirm_article_import_batch(
    article_ids: list[str] | tuple[str, ...] | set[str],
    updated_article_ids: list[str] | tuple[str, ...] | set[str] = (),
    *,
    import_id: str,
    confirmed_at: str,
) -> int:
    """批量确认表格导入，避免逐篇重复读写 articles.json。"""
    if _article_store_sqlite_enabled():
        return _confirm_article_import_batch_sqlite(
            article_ids,
            updated_article_ids,
            import_id=import_id,
            confirmed_at=confirmed_at,
        )

    target_ids = {
        str(article_id or "").strip()
        for article_id in list(article_ids or []) + list(updated_article_ids or [])
        if str(article_id or "").strip()
    }
    if not target_ids:
        return 0

    updated_count = 0
    updated_articles: list[dict] = []
    shadow_context: dict[str, object] | None = None
    with _lock:
        articles = _load_articles()
        for index, article in enumerate(articles):
            article_id = str(article.get("id", "") or "").strip()
            if article_id not in target_ids:
                continue
            merged = dict(article)
            merged.update({
                "import_status": "confirmed",
                "import_batch_id": str(import_id or "").strip(),
                "import_confirmed_at": str(confirmed_at or "").strip(),
            })
            normalized, _ = _normalize_article_entry(merged)
            articles[index] = normalized
            updated_articles.append(dict(normalized))
            updated_count += 1
        if updated_count:
            shadow_context = _begin_article_shadow_runtime_write()
            _save_articles(articles)
    if updated_articles and shadow_context is not None:
        _sync_article_shadow_articles(
            updated_articles,
            reason="confirm_article_import_batch",
            context=shadow_context,
        )
    return updated_count


def undo_article_import_batch(
    article_ids: list[str] | tuple[str, ...] | set[str],
    updated_articles: list[dict] | tuple[dict, ...] = (),
) -> dict[str, int]:
    """批量撤销表格导入，新增文章移除，已更新文章恢复导入前快照。"""
    if _article_store_sqlite_enabled():
        return _undo_article_import_batch_sqlite(article_ids, updated_articles)

    remove_ids = {
        str(article_id or "").strip()
        for article_id in (article_ids or [])
        if str(article_id or "").strip()
    }
    restore_clear_fields = {
        "account_name": "",
        "published_at": "",
        "ts": "",
        "excerpt": "",
        "import_batch_id": "",
        "import_status": "",
        "import_file_name": "",
        "imported_from_sheet": "",
        "imported_from_row": 0,
        "last_table_import_at": "",
        "last_table_import_file": "",
        "import_confirmed_at": "",
        "matched_tasks": [],
        "match_reasons": {},
        "unmatched_reason": "",
    }
    restore_by_id: dict[str, dict] = {}
    for item in updated_articles or []:
        if not isinstance(item, dict):
            continue
        article_id = str(item.get("id") or "").strip()
        before = item.get("before") if isinstance(item.get("before"), dict) else None
        if not article_id or not before:
            continue
        restore_patch = dict(restore_clear_fields)
        restore_patch.update(before)
        restore_by_id[article_id] = restore_patch

    if not remove_ids and not restore_by_id:
        return {"removed_count": 0, "restored_count": 0}

    removed_count = 0
    restored_count = 0
    removed_ids: list[str] = []
    restored_articles: list[dict] = []
    shadow_context: dict[str, object] | None = None
    with _lock:
        articles = _load_articles()
        next_articles = []
        for article in articles:
            article_id = str(article.get("id", "") or "").strip()
            if article_id and article_id in remove_ids:
                removed_count += 1
                removed_ids.append(article_id)
                continue
            restore_patch = restore_by_id.get(article_id)
            if restore_patch is not None:
                merged = dict(article)
                merged.update(restore_patch)
                merged["id"] = article.get("id", merged.get("id", ""))
                normalized, _ = _normalize_article_entry(merged)
                next_articles.append(normalized)
                restored_articles.append(dict(normalized))
                restored_count += 1
                continue
            next_articles.append(article)
        if removed_count or restored_count:
            shadow_context = _begin_article_shadow_runtime_write()
            _save_articles(next_articles)
    if shadow_context is not None:
        _sync_article_shadow_mutation(
            upsert_articles=restored_articles,
            delete_article_ids=removed_ids,
            reason="undo_article_import_batch",
            context=shadow_context,
        )

    return {"removed_count": removed_count, "restored_count": restored_count}


def find_article_by_url(url: str) -> dict | None:
    if _article_store_sqlite_enabled():
        return _article_sqlite_store().get_article_by_url(url)

    normalized_url = normalize_article_url(url)
    if not normalized_url:
        return None
    with _lock:
        articles = _load_articles()
        for article in articles:
            if normalize_article_url(article.get("url", "")) == normalized_url:
                return article
    return None


def get_articles() -> list:
    """返回全部文章，按发布时间降序，录入时间兜底。"""
    if _article_store_sqlite_enabled():
        return _article_sqlite_store().list_articles()

    with _lock:
        articles = _load_articles()
    return _sort_articles_for_display(articles)


def get_articles_file_signature() -> tuple[str, int, int]:
    """Return a cheap source signature for the article store file."""
    if _article_store_sqlite_enabled():
        return _article_sqlite_store().file_signature()

    return _json_document_signature(
        "article_store/articles",
        _articles_file(),
        use_sqlite=ARTICLES_FILE == DEFAULT_ARTICLES_FILE,
    )


def get_article_source_signature() -> str:
    payload = {
        "version": 1,
        "articles": get_articles_file_signature(),
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _safe_positive_int(value) -> int | None:
    try:
        number = int(str(value or "").strip())
    except Exception:
        return None
    return number if number > 0 else None


def _is_cloud_downloaded_article(article: dict) -> bool:
    if not isinstance(article, dict):
        return False
    return (
        str(article.get("fetch_method") or "").strip() == "cloud"
        or _safe_positive_int(article.get("cloud_article_id")) is not None
        or bool(str(article.get("cloud_url_hash") or "").strip())
    )


def prune_cloud_articles_by_visible_task_ids(
    visible_cloud_task_ids: list[int] | set[int] | tuple[int, ...],
    *,
    visible_cloud_article_ids: list[int] | set[int] | tuple[int, ...] | None = None,
    visible_cloud_url_hashes: list[str] | set[str] | tuple[str, ...] | None = None,
) -> dict:
    """Keep cloud-downloaded articles on disk; UI visibility is filtered at read time."""
    if _article_store_sqlite_enabled():
        return _prune_cloud_articles_by_visible_task_ids_sqlite(
            visible_cloud_task_ids,
            visible_cloud_article_ids=visible_cloud_article_ids,
            visible_cloud_url_hashes=visible_cloud_url_hashes,
        )

    visible_ids = {
        int(task_id)
        for task_id in visible_cloud_task_ids or []
        if _safe_positive_int(task_id) is not None
    }
    article_scope_enabled = visible_cloud_article_ids is not None or visible_cloud_url_hashes is not None
    visible_article_ids = {
        int(article_id)
        for article_id in visible_cloud_article_ids or []
        if _safe_positive_int(article_id) is not None
    }
    visible_url_hashes = {
        str(url_hash or "").strip()
        for url_hash in visible_cloud_url_hashes or []
        if str(url_hash or "").strip()
    }
    updated_articles: list[dict] = []
    shadow_context: dict[str, object] | None = None
    with _lock:
        articles = _load_articles()
        kept: list[dict] = []
        removed = 0
        pruned_links = 0
        for article in articles:
            if not _is_cloud_downloaded_article(article):
                kept.append(article)
                continue
            if article_scope_enabled:
                cloud_article_id = _safe_positive_int(article.get("cloud_article_id"))
                cloud_url_hash = str(article.get("cloud_url_hash") or "").strip()
                in_visible_article_scope = (
                    (cloud_article_id is not None and cloud_article_id in visible_article_ids)
                    or (cloud_url_hash and cloud_url_hash in visible_url_hashes)
                )
                if not in_visible_article_scope:
                    next_article = dict(article)
                    original_task_ids = [
                        task_id
                        for task_id in (_safe_positive_int(item) for item in article.get("cloud_task_ids") or [])
                        if task_id is not None
                    ]
                    hidden_task_ids = [task_id for task_id in original_task_ids if task_id not in visible_ids]
                    if hidden_task_ids != original_task_ids or article.get("matched_tasks") or article.get("match_reasons"):
                        next_article["cloud_task_ids"] = hidden_task_ids
                        next_article["matched_tasks"] = []
                        next_article["match_reasons"] = {}
                        next_article["unmatched_reason"] = "云端文章当前不在当前账号可见范围"
                        pruned_links += 1
                        updated_articles.append(dict(next_article))
                    kept.append(next_article)
                    continue
            cloud_task_ids = [
                task_id
                for task_id in (_safe_positive_int(item) for item in article.get("cloud_task_ids") or [])
                if task_id is not None
            ]
            visible_article_task_ids = [task_id for task_id in cloud_task_ids if task_id in visible_ids]
            if not visible_article_task_ids:
                kept.append(article)
                continue
            if visible_article_task_ids != cloud_task_ids:
                next_article = dict(article)
                next_article["cloud_task_ids"] = visible_article_task_ids
                kept.append(next_article)
                pruned_links += 1
                updated_articles.append(dict(next_article))
                continue
            kept.append(article)
        if removed or pruned_links:
            shadow_context = _begin_article_shadow_runtime_write()
            _save_articles(kept)
    if updated_articles and shadow_context is not None:
        _sync_article_shadow_articles(
            updated_articles,
            reason="prune_cloud_articles_by_visible_task_ids",
            context=shadow_context,
        )
    return {
        "removed": removed,
        "pruned_links": pruned_links,
        "remaining": len(kept),
    }


def export_article_store_bundle() -> dict:
    """导出文章存储及站点记忆数据。"""
    with _lock:
        return {
            "articles": list(_article_sqlite_store().list_articles() if _article_store_sqlite_enabled() else _load_articles()),
            "domain_overrides": dict(_load_domain_overrides()),
            "domain_media_names": dict(_load_domain_media_names()),
            "excluded_article_urls": dict(_load_excluded_article_urls()),
        }


def import_article_store_bundle(bundle: dict | None, *, mode: str = "merge") -> dict:
    """导入文章存储及站点记忆数据。"""
    payload = bundle if isinstance(bundle, dict) else {}
    normalized_mode = str(mode or "merge").strip().lower()
    if normalized_mode not in {"merge", "replace"}:
        normalized_mode = "merge"

    raw_articles = payload.get("articles", [])
    raw_overrides = payload.get("domain_overrides", {})
    raw_media_names = payload.get("domain_media_names", {})
    raw_excluded_urls = payload.get("excluded_article_urls", {})

    def _normalize_incoming_article(raw_item: dict) -> dict | None:
        if not isinstance(raw_item, dict):
            return None
        item = dict(raw_item)
        item.setdefault("id", uuid.uuid4().hex)
        now_text = _article_now_minute_text()
        item.setdefault("ts", now_text)
        item.setdefault("url", "")
        item.setdefault("title", "")
        item.setdefault("platform", "")
        item.setdefault("media_name", "")
        item.setdefault("media_type", "selfmedia")
        item.setdefault("excerpt", "")
        item.setdefault("imported_at", now_text)
        item.setdefault("matched_tasks", [])
        item.setdefault("match_reasons", {})
        item.setdefault("unmatched_reason", "")
        item.setdefault("fetch_method", "html")
        normalized, _ = _normalize_article_entry(item)
        return normalized

    normalized_articles = []
    seen_article_keys: set[str] = set()
    for raw_item in raw_articles if isinstance(raw_articles, list) else []:
        normalized = _normalize_incoming_article(raw_item)
        if normalized is None:
            continue
        normalized_url = normalize_article_url(normalized.get("url", ""))
        article_id = str(normalized.get("id", "")).strip()
        dedupe_key = normalized_url or f"id:{article_id}" if article_id else ""
        if dedupe_key and dedupe_key in seen_article_keys:
            continue
        if dedupe_key:
            seen_article_keys.add(dedupe_key)
        normalized_articles.append(normalized)

    normalized_overrides = {}
    if isinstance(raw_overrides, dict):
        for raw_domain, raw_media_type in raw_overrides.items():
            domain = str(raw_domain or "").strip().lower()
            media_type = str(raw_media_type or "").strip().lower()
            if not domain or media_type not in {"authority", "selfmedia"}:
                continue
            normalized_overrides[domain] = media_type

    normalized_media_names = {}
    if isinstance(raw_media_names, dict):
        for raw_domain, raw_media_name in raw_media_names.items():
            domain = str(raw_domain or "").strip().lower()
            media_name = str(raw_media_name or "").strip()
            if not domain or not media_name:
                continue
            normalized_media_names[domain] = media_name

    normalized_excluded_urls = {}
    if isinstance(raw_excluded_urls, dict):
        for raw_url, raw_payload in raw_excluded_urls.items():
            normalized_url = normalize_article_url(str(raw_url or "").strip())
            if not normalized_url:
                continue
            payload_item = dict(raw_payload) if isinstance(raw_payload, dict) else {}
            payload_item["url"] = normalized_url
            normalized_excluded_urls[normalized_url] = payload_item

    if _article_store_sqlite_enabled():
        return _import_article_store_bundle_sqlite(
            normalized_mode=normalized_mode,
            normalized_articles=normalized_articles,
            normalized_overrides=normalized_overrides,
            normalized_media_names=normalized_media_names,
            normalized_excluded_urls=normalized_excluded_urls,
        )

    shadow_context: dict[str, object] | None = None
    shadow_upserts: list[dict] = []
    shadow_deletes: list[str] = []
    with _lock:
        existing_articles = _load_articles()
        created_articles = 0
        updated_articles = 0

        if normalized_mode == "replace":
            next_articles = list(normalized_articles)
            created_articles = len(next_articles)
        else:
            next_articles = list(existing_articles)
            index_by_id = {
                str(article.get("id", "")).strip(): index
                for index, article in enumerate(next_articles)
                if str(article.get("id", "")).strip()
            }
            index_by_url = {
                normalize_article_url(article.get("url", "")): index
                for index, article in enumerate(next_articles)
                if normalize_article_url(article.get("url", ""))
            }

            for article in normalized_articles:
                article_id = str(article.get("id", "")).strip()
                normalized_url = normalize_article_url(article.get("url", ""))
                target_index = None
                if normalized_url:
                    target_index = index_by_url.get(normalized_url)
                if target_index is None and article_id:
                    target_index = index_by_id.get(article_id)

                if target_index is None:
                    next_articles.append(article)
                    created_articles += 1
                    target_index = len(next_articles) - 1
                else:
                    merged = dict(next_articles[target_index])
                    merged.update(article)
                    if article_id:
                        merged["id"] = article_id
                    next_articles[target_index] = merged
                    updated_articles += 1

                final_item = next_articles[target_index]
                final_id = str(final_item.get("id", "")).strip()
                final_url = normalize_article_url(final_item.get("url", ""))
                if final_id:
                    index_by_id[final_id] = target_index
                if final_url:
                    index_by_url[final_url] = target_index

        if normalized_mode == "replace":
            next_overrides = dict(normalized_overrides)
            next_media_names = dict(normalized_media_names)
            next_excluded_urls = dict(normalized_excluded_urls)
        else:
            next_overrides = dict(_load_domain_overrides())
            next_overrides.update(normalized_overrides)
            next_media_names = dict(_load_domain_media_names())
            next_media_names.update(normalized_media_names)
            next_excluded_urls = dict(_load_excluded_article_urls())
            next_excluded_urls.update(normalized_excluded_urls)

        next_excluded_urls, _ = _trim_excluded_article_urls_for_storage(next_excluded_urls)
        if normalized_mode == "replace":
            next_ids = {
                str(article.get("id", "") or "").strip()
                for article in next_articles
                if str(article.get("id", "") or "").strip()
            }
            shadow_deletes = [
                str(article.get("id", "") or "").strip()
                for article in existing_articles
                if str(article.get("id", "") or "").strip()
                and str(article.get("id", "") or "").strip() not in next_ids
            ]
            shadow_upserts = [dict(article) for article in next_articles if isinstance(article, dict)]
        else:
            shadow_upserts = [dict(article) for article in normalized_articles if isinstance(article, dict)]
        shadow_context = _begin_article_shadow_runtime_write()
        _save_articles(next_articles)
        _save_domain_overrides(next_overrides)
        _save_domain_media_names(next_media_names)
        _save_excluded_article_urls(next_excluded_urls)
    if shadow_context is not None:
        _sync_article_shadow_mutation(
            upsert_articles=shadow_upserts,
            delete_article_ids=shadow_deletes,
            reason="import_article_store_bundle",
            context=shadow_context,
        )

    return {
        "mode": normalized_mode,
        "articles_total": len(next_articles),
        "articles_created": created_articles,
        "articles_updated": updated_articles,
        "domain_overrides_total": len(next_overrides),
        "domain_media_names_total": len(next_media_names),
        "excluded_article_urls_total": len(next_excluded_urls),
    }


def delete_article(article_id: str, *, exclude_url: bool = True) -> dict | None:
    """按 ID 删除文章，返回被删除的文章。"""
    if _article_store_sqlite_enabled():
        return _delete_article_sqlite(article_id, exclude_url=exclude_url)

    article_id = str(article_id or "").strip()
    if not article_id:
        return None

    shadow_context: dict[str, object] | None = None
    with _lock:
        articles = _load_articles()
        removed = None
        remaining = []
        for article in articles:
            if removed is None and str(article.get("id", "")).strip() == article_id:
                removed = article
                continue
            remaining.append(article)
        if removed is None:
            return None
        shadow_context = _begin_article_shadow_runtime_write()
        _save_articles(remaining)
        if exclude_url:
            exclude_article_url(
                str(removed.get("url") or "").strip(),
                title=str(removed.get("title") or "").strip(),
                source="manual_delete",
            )
    if shadow_context is not None:
        _sync_article_shadow_delete([article_id], reason="delete_article", context=shadow_context)
    return removed


def remove_article_from_task(article_id: str, task_name: str) -> dict | None:
    """仅将文章从指定任务/品牌汇总中移除，不删除文章本体。"""
    if _article_store_sqlite_enabled():
        return _remove_article_from_task_sqlite(article_id, task_name)

    article_id = str(article_id or "").strip()
    task_name = str(task_name or "").strip()
    if not article_id or not task_name:
        return None

    updated_article: dict | None = None
    shadow_context: dict[str, object] | None = None
    with _lock:
        articles = _load_articles()
        for index, article in enumerate(articles):
            if str(article.get("id", "")).strip() != article_id:
                continue

            updated = dict(article)
            excluded_tasks = [
                str(name or "").strip()
                for name in (updated.get("excluded_tasks") or [])
                if str(name or "").strip()
            ]
            if task_name not in excluded_tasks:
                excluded_tasks.append(task_name)
            updated["excluded_tasks"] = excluded_tasks

            updated["matched_tasks"] = [
                str(name or "").strip()
                for name in (updated.get("matched_tasks") or [])
                if str(name or "").strip() and str(name or "").strip() != task_name
            ]
            match_reasons = updated.get("match_reasons") if isinstance(updated.get("match_reasons"), dict) else {}
            if task_name in match_reasons:
                match_reasons = dict(match_reasons)
                match_reasons.pop(task_name, None)
            updated["match_reasons"] = match_reasons

            updated["referenced_tasks"] = [
                str(name or "").strip()
                for name in (updated.get("referenced_tasks") or [])
                if str(name or "").strip() and str(name or "").strip() != task_name
            ]
            reference_hits = updated.get("reference_hits") if isinstance(updated.get("reference_hits"), dict) else {}
            if task_name in reference_hits:
                reference_hits = dict(reference_hits)
                reference_hits.pop(task_name, None)
            updated["reference_hits"] = reference_hits

            if not updated["matched_tasks"]:
                updated["unmatched_reason"] = f"已从品牌“{task_name}”文章汇总移除"

            updated, _ = _normalize_article_entry(updated)
            articles[index] = updated
            shadow_context = _begin_article_shadow_runtime_write()
            _save_articles(articles)
            updated_article = dict(updated)
            break
    if updated_article is not None and shadow_context is not None:
        _sync_article_shadow_articles(
            [updated_article],
            reason="remove_article_from_task",
            context=shadow_context,
        )
        return updated_article
    return None


def update_media_type(article_id: str, media_type: str) -> bool:
    """
    修改单条文章的媒体类型，并同步记忆该域名（force=True）。
    """
    if _article_store_sqlite_enabled():
        return _update_media_type_sqlite(article_id, media_type)

    updated_article: dict | None = None
    shadow_context: dict[str, object] | None = None
    with _lock:
        articles = _load_articles()
        changed = False
        domain = ""
        for article in articles:
            if article.get("id") == article_id:
                article["media_type"] = media_type
                domain = extract_domain(article.get("url", ""))
                updated_article = dict(article)
                changed = True
                break
        if changed:
            shadow_context = _begin_article_shadow_runtime_write()
            _save_articles(articles)

    if updated_article is not None and shadow_context is not None:
        _sync_article_shadow_articles(
            [updated_article],
            reason="update_media_type",
            context=shadow_context,
        )
    if changed and domain:
        save_domain_override(domain, media_type, force=True)
    return changed


def get_task_article_counts(task_names: list) -> dict:
    """
    返回各任务组的文章数统计。
    格式：{"任务组名": count, ...}
    """
    articles = get_articles()
    counts: dict = {name: 0 for name in task_names}
    for article in articles:
        for task_name in article.get("matched_tasks", []):
            if task_name in counts:
                counts[task_name] += 1
    return counts


def get_articles_by_task(task_name: str) -> list:
    """返回绑定到某任务组的全部文章，时间降序"""
    articles = get_articles()
    return [a for a in articles if task_name in a.get("matched_tasks", [])]


def mark_articles_referenced_by_urls(
    task_names: list[str] | tuple[str, ...] | set[str],
    urls: list[str] | tuple[str, ...] | set[str],
    *,
    source: str = "recognition",
    referenced_at: str = "",
    platform: str = "",
    event_id: str = "",
    record_id: str = "",
) -> dict:
    """
    将已录入文章标记为“已引用”。

    仅做本地归一化 URL 精确匹配，不访问网络；若传入 task_names，则只标记
    已归类到对应任务组的文章，避免跨品牌误标。
    """
    if _article_store_sqlite_enabled():
        return _mark_articles_referenced_by_urls_sqlite(
            task_names,
            urls,
            source=source,
            referenced_at=referenced_at,
            platform=platform,
            event_id=event_id,
            record_id=record_id,
        )

    target_task_names = []
    for task_name in task_names or []:
        task_text = str(task_name or "").strip()
        if task_text and task_text not in target_task_names:
            target_task_names.append(task_text)

    normalized_urls = []
    for url in urls or []:
        normalized_url = normalize_article_url(str(url or "").strip())
        if normalized_url and normalized_url not in normalized_urls:
            normalized_urls.append(normalized_url)

    if not normalized_urls:
        return {
            "matched_count": 0,
            "updated_count": 0,
            "task_names": target_task_names,
            "urls": [],
            "articles": [],
        }

    url_set = set(normalized_urls)
    reference_source = str(source or "recognition").strip() or "recognition"
    reference_time = str(referenced_at or "").strip() or _article_now_second_text()
    normalized_platform = normalize_platform_id(platform)

    matched_articles = []
    updated_count = 0
    updated_articles: list[dict] = []
    shadow_context: dict[str, object] | None = None
    with _lock:
        articles = _load_articles()
        changed = False
        for article in articles:
            article_url = normalize_article_url(article.get("url", ""))
            if not article_url or article_url not in url_set:
                continue

            article_tasks = [
                str(name or "").strip()
                for name in (article.get("matched_tasks") or [])
                if str(name or "").strip()
            ]
            if target_task_names:
                matched_task_names = [
                    task_name
                    for task_name in target_task_names
                    if task_name in article_tasks
                ]
                if not matched_task_names:
                    continue
            else:
                matched_task_names = article_tasks

            if not matched_task_names:
                continue

            referenced_tasks = [
                str(name or "").strip()
                for name in (article.get("referenced_tasks") or [])
                if str(name or "").strip()
            ]
            reference_hits = article.get("reference_hits") if isinstance(article.get("reference_hits"), dict) else {}
            before_tasks = list(referenced_tasks)
            before_hits = json.dumps(reference_hits, ensure_ascii=False, sort_keys=True)

            for task_name in matched_task_names:
                if task_name not in referenced_tasks:
                    referenced_tasks.append(task_name)
                hit = dict(reference_hits.get(task_name) or {})
                normalized_hit, _ = _normalize_reference_hit(
                    hit,
                    task_name=task_name,
                    article_url=article_url,
                )
                hit = normalized_hit or {}
                events = list(hit.get("events") or [])
                explicit_event_id = _make_reference_event_id(
                    explicit_event_id=event_id,
                    record_id=record_id,
                    task_name=task_name,
                    article_url=article_url,
                    platform=normalized_platform,
                    referenced_at=reference_time,
                    source=reference_source,
                )
                added_event = False
                if not any(str(item.get("event_id") or "").strip() == explicit_event_id for item in events if isinstance(item, dict)):
                    event_payload = {
                        "event_id": explicit_event_id,
                        "referenced_at": reference_time,
                        "source": reference_source,
                        "article_url": article_url,
                        "task_name": task_name,
                    }
                    if normalized_platform:
                        event_payload["platform"] = normalized_platform
                    if record_id:
                        event_payload["record_id"] = str(record_id or "").strip()
                    events.append(event_payload)
                    added_event = True

                try:
                    count = int(hit.get("count", 0) or 0)
                except Exception:
                    count = 0
                if added_event:
                    count += 1
                events, trimmed_events = _trim_reference_events_for_storage(events)
                hit.update({
                    "last_referenced_at": _max_timestamp_text(str(hit.get("last_referenced_at") or ""), reference_time) if added_event else str(hit.get("last_referenced_at") or "").strip(),
                    "source": reference_source,
                    "count": max(count, 0),
                    "events": events,
                })
                if trimmed_events:
                    hit["events_compacted"] = True
                reference_hits[task_name] = hit

            article["referenced_tasks"] = referenced_tasks
            article["reference_hits"] = reference_hits
            after_hits = json.dumps(reference_hits, ensure_ascii=False, sort_keys=True)
            item_changed = referenced_tasks != before_tasks or after_hits != before_hits
            changed = changed or item_changed
            if item_changed:
                updated_count += 1
                updated_articles.append(dict(article))
            matched_articles.append({
                "id": article.get("id", ""),
                "title": article.get("title", ""),
                "url": article.get("url", ""),
                "tasks": list(matched_task_names),
            })

        if changed:
            shadow_context = _begin_article_shadow_runtime_write()
            _save_articles(articles)
    if updated_articles and shadow_context is not None:
        _sync_article_shadow_articles(
            updated_articles,
            reason="mark_articles_referenced_by_urls",
            context=shadow_context,
        )

    return {
        "matched_count": len(matched_articles),
        "updated_count": updated_count,
        "task_names": target_task_names,
        "urls": normalized_urls,
        "articles": matched_articles,
    }


# ---------------------------------------------------------------------------
# 关键词匹配
# ---------------------------------------------------------------------------

_MATCH_NORMALIZE_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+", re.IGNORECASE)
_INTENT_TERMS = (
    "品牌推荐", "品牌", "牌子", "推荐", "排行", "榜单", "测评", "评测",
    "对比", "横评", "盘点", "怎么选", "选购", "口碑", "值得买", "合集",
)
_NEGATIVE_TERMS = (
    "做法", "食谱", "菜谱", "热量", "营养", "卡路里", "炖", "炒", "煎", "烤",
    "卤", "教程", "步骤", "价格", "行情", "批发",
)
_THEME_NOISE_TERMS = tuple(sorted(set(_INTENT_TERMS + ("品牌", "推荐", "品牌词")), key=len, reverse=True))
_CORE_KEYWORD_HINT_TERMS = (
    "geo", "seo", "ai",
    "优化", "公司", "机构", "服务", "品牌", "平台", "厂家", "商家",
    "加盟", "代理", "软件", "系统", "课程", "培训", "医院", "学校",
    "教育", "职教", "院校", "技工", "技校", "装修", "设计", "律师",
    "留学", "旅游", "租车",
)
_TITLE_INSERTION_NOISE_TERMS = (
    "优质", "专业", "精选", "优选", "高端", "实力", "靠谱", "正规",
    "本地", "附近", "官方", "知名", "热门", "口碑", "优先", "高效",
)
_PROVIDER_EQUIVALENT_TERMS = (
    "供应商", "服务商", "制造商", "生产商", "厂家", "厂商", "企业", "公司", "商家",
)


def _normalize_match_text(text: str) -> str:
    return _MATCH_NORMALIZE_RE.sub("", str(text or "").strip().lower())


_TITLE_INSERTION_NOISE_NORMALIZED = {
    _normalize_match_text(term)
    for term in _TITLE_INSERTION_NOISE_TERMS
    if _normalize_match_text(term)
}
_PROVIDER_EQUIVALENT_NORMALIZED = {
    _normalize_match_text(term)
    for term in _PROVIDER_EQUIVALENT_TERMS
    if _normalize_match_text(term)
}


def _build_match_segments(text: str) -> list[str]:
    normalized = _normalize_match_text(text)
    if not normalized:
        return []
    return re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", normalized, flags=re.IGNORECASE)


def _ordered_segments_match(haystack: str, segments: list[str]) -> bool:
    if not haystack or not segments:
        return False
    position = 0
    for segment in segments:
        idx = haystack.find(segment, position)
        if idx < 0:
            return False
        position = idx + len(segment)
    return True


def _bounded_edit_distance(left: str, right: str, max_distance: int) -> int:
    if left == right:
        return 0
    if max_distance < 0:
        return max(len(left), len(right))
    if abs(len(left) - len(right)) > max_distance:
        return max_distance + 1

    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i]
        row_min = current[0]
        for j, right_char in enumerate(right, start=1):
            insert_cost = current[j - 1] + 1
            delete_cost = previous[j] + 1
            replace_cost = previous[j - 1] + (0 if left_char == right_char else 1)
            cost = min(insert_cost, delete_cost, replace_cost)
            current.append(cost)
            if cost < row_min:
                row_min = cost
        if row_min > max_distance:
            return max_distance + 1
        previous = current
    return previous[-1]


def _has_strong_boundary_overlap(left: str, right: str) -> bool:
    if not left or not right:
        return False
    prefix = 0
    for a, b in zip(left, right):
        if a != b:
            break
        prefix += 1

    suffix = 0
    for a, b in zip(reversed(left), reversed(right)):
        if a != b:
            break
        suffix += 1

    return max(prefix, suffix) >= 2


def _near_substring_match_normalized(normalized_haystack: str, normalized_candidate: str) -> bool:
    if len(normalized_candidate) < 4 or len(normalized_haystack) < 3:
        return False

    max_distance = 1 if len(normalized_candidate) <= 6 else 2
    min_len = max(3, len(normalized_candidate) - max_distance)
    max_len = min(len(normalized_haystack), len(normalized_candidate) + max_distance)

    for window_len in range(min_len, max_len + 1):
        for start in range(0, len(normalized_haystack) - window_len + 1):
            window = normalized_haystack[start:start + window_len]
            if not _has_strong_boundary_overlap(window, normalized_candidate):
                continue
            if _bounded_edit_distance(window, normalized_candidate, max_distance) <= max_distance:
                return True
    return False


def _near_substring_match(haystack: str, candidate: str) -> bool:
    return _near_substring_match_normalized(
        _normalize_match_text(haystack),
        _normalize_match_text(candidate),
    )


def _candidate_matches_article(haystack: str, candidate: str) -> bool:
    normalized_candidate = _normalize_match_text(candidate)
    if not normalized_candidate:
        return False
    if normalized_candidate in haystack:
        return True
    segments = [seg for seg in _build_match_segments(candidate) if len(seg) >= 2 or re.search(r"[a-z]", seg)]
    if len(segments) >= 2 and _ordered_segments_match(haystack, segments):
        return True
    return _near_substring_match_normalized(haystack, normalized_candidate)


def _build_char_ngrams(text: str, size: int) -> list[str]:
    normalized = _normalize_match_text(text)
    if len(normalized) < size or size <= 0:
        return []
    return [normalized[idx:idx + size] for idx in range(0, len(normalized) - size + 1)]


def _strip_title_insertion_noise(text: str) -> str:
    normalized = _normalize_match_text(text)
    if not normalized:
        return ""
    stripped = normalized
    for term in _TITLE_INSERTION_NOISE_TERMS:
        normalized_term = _normalize_match_text(term)
        if normalized_term:
            stripped = stripped.replace(normalized_term, "")
    return stripped


def _strip_equivalent_provider_terms(text: str) -> str:
    normalized = _normalize_match_text(text)
    if not normalized:
        return ""
    stripped = normalized
    for term in sorted(_PROVIDER_EQUIVALENT_NORMALIZED, key=len, reverse=True):
        stripped = stripped.replace(term, "")
    for term in _THEME_NOISE_TERMS:
        normalized_term = _normalize_match_text(term)
        if normalized_term:
            stripped = stripped.replace(normalized_term, "")
    return stripped


def _extract_provider_subject_terms(keyword: str) -> list[str]:
    normalized_keyword = _normalize_match_text(keyword)
    if not normalized_keyword:
        return []
    if not any(term in normalized_keyword for term in _PROVIDER_EQUIVALENT_NORMALIZED):
        return []

    subject = _strip_equivalent_provider_terms(normalized_keyword)
    if len(subject) < 3:
        return []
    return _dedupe_texts([subject])


def _provider_equivalent_theme_matches(title: str, keyword: str) -> bool:
    normalized_title = _normalize_match_text(title)
    normalized_keyword = _normalize_match_text(keyword)
    if not normalized_title or not normalized_keyword:
        return False
    if not any(term in normalized_title for term in _PROVIDER_EQUIVALENT_NORMALIZED):
        return False
    if not any(term in normalized_keyword for term in _PROVIDER_EQUIVALENT_NORMALIZED):
        return False

    title_subject = _strip_equivalent_provider_terms(normalized_title)
    keyword_subject = _strip_equivalent_provider_terms(normalized_keyword)
    if len(keyword_subject) < 3 or len(title_subject) < 3:
        return False
    return _candidate_matches_article(title_subject, keyword_subject)


def _keyword_theme_matches_title(title: str, keyword: str) -> bool:
    """用于文章归类的标题-关键词相似匹配，允许插入少量分析词或修饰词。"""
    normalized_title = _normalize_match_text(title)
    normalized_keyword = _normalize_match_text(keyword)
    if not normalized_title or not normalized_keyword:
        return False
    if _candidate_matches_article(normalized_title, normalized_keyword):
        return True
    if _provider_equivalent_theme_matches(normalized_title, normalized_keyword):
        return True

    stripped_title = _strip_title_insertion_noise(normalized_title)
    stripped_keyword = _strip_title_insertion_noise(normalized_keyword)
    if (
        stripped_title
        and stripped_keyword
        and (stripped_title != normalized_title or stripped_keyword != normalized_keyword)
        and _candidate_matches_article(stripped_title, stripped_keyword)
    ):
        return True

    if len(normalized_keyword) >= 4:
        for split_idx in range(2, len(normalized_keyword) - 1):
            left = normalized_keyword[:split_idx]
            right = normalized_keyword[split_idx:]
            if len(left) < 2 or len(right) < 2:
                continue
            left_idx = normalized_title.find(left)
            if left_idx < 0:
                continue
            search_start = left_idx + len(left)
            right_idx = normalized_title.find(right, search_start)
            if right_idx < 0:
                continue
            gap = normalized_title[search_start:right_idx]
            if not gap or len(gap) > 4:
                continue
            if gap in _TITLE_INSERTION_NOISE_NORMALIZED:
                return True

    if len(normalized_title) < 8 or len(normalized_keyword) < 6:
        return False

    matcher = SequenceMatcher(None, normalized_keyword, normalized_title)
    matching_blocks = matcher.get_matching_blocks()
    max_block = max((block.size for block in matching_blocks), default=0)
    ratio = matcher.ratio()
    keyword_len = len(normalized_keyword)

    # 长关键词允许标题中夹杂“现状/分析/推荐”等修饰片段，只要主体连续块足够长即可。
    if ratio >= 0.72 and max_block >= max(6, min(12, keyword_len - 1)):
        return True

    ngram_size = 4 if keyword_len >= 10 else 3
    keyword_ngrams = _build_char_ngrams(normalized_keyword, ngram_size)
    if len(keyword_ngrams) < 2:
        return False
    hit_count = sum(1 for gram in keyword_ngrams if gram in normalized_title)
    hit_ratio = hit_count / len(keyword_ngrams)
    if max_block >= max(6, int(keyword_len * 0.45)) and hit_ratio >= 0.45:
        return True
    return False


def _dedupe_texts(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        key = _normalize_match_text(text)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _extract_theme_terms(text: str) -> list[str]:
    normalized = _normalize_match_text(text)
    if not normalized:
        return []

    lowered = normalized
    for noise in _THEME_NOISE_TERMS:
        lowered = lowered.replace(_normalize_match_text(noise), " ")

    terms = re.findall(r"[a-z0-9]{2,}|[\u4e00-\u9fff]{2,}", lowered)
    return _dedupe_texts(terms)


def _build_match_fields(title: str, article: dict | None = None) -> dict[str, str]:
    texts = [str(title or "").strip()]
    if isinstance(article, dict):
        texts.extend([
            str(article.get("media_name", "") or "").strip(),
            str(article.get("excerpt", "") or "").strip(),
            str(article.get("source", "") or "").strip(),
            str(article.get("platform", "") or "").strip(),
            str(article.get("url", "") or "").strip(),
        ])
    return {
        "title": _normalize_match_text(title),
        "excerpt": _normalize_match_text(article.get("excerpt", "") if isinstance(article, dict) else ""),
        "source": _normalize_match_text(article.get("source", "") if isinstance(article, dict) else ""),
        "platform": _normalize_match_text(article.get("platform", "") if isinstance(article, dict) else ""),
        "url": _normalize_match_text(article.get("url", "") if isinstance(article, dict) else ""),
        "full": _normalize_match_text(" ".join(t for t in texts if t)),
    }


def _format_term_list(terms: list[str], limit: int = 2) -> str:
    unique_terms = _dedupe_texts(terms)
    return "、".join(unique_terms[:limit])


def _looks_like_core_keyword(text: str) -> bool:
    normalized = _normalize_match_text(text)
    if len(normalized) < 4:
        return False
    return any(hint in normalized for hint in _CORE_KEYWORD_HINT_TERMS)


def _extract_core_keyword_texts(keyword: str, task: dict | None = None) -> list[str]:
    raw = str(keyword or "").strip()
    if not raw:
        return []

    simplified = raw
    for noise in _THEME_NOISE_TERMS:
        simplified = simplified.replace(noise, " ")

    simplified = re.sub(r"\s+", " ", simplified).strip()
    candidates: list[str] = []

    normalized = _normalize_match_text(simplified)
    if normalized:
        candidates.append(simplified)
    else:
        candidates.append(raw)

    compact = re.sub(r"\s+", "", simplified or raw)
    compact_lower = compact.lower()
    if compact and _looks_like_core_keyword(compact_lower):
        start_indexes = [
            compact_lower.find(hint)
            for hint in _CORE_KEYWORD_HINT_TERMS
            if compact_lower.find(hint) > 0
        ]
        start_indexes = [idx for idx in start_indexes if idx > 0]
        if start_indexes:
            relaxed = compact[min(start_indexes):].strip()
            if relaxed and _looks_like_core_keyword(relaxed):
                candidates.append(relaxed)

    return _dedupe_texts(candidates)


def _remove_brand_terms_from_keyword(keyword: str, brand_terms: list[str]) -> list[str]:
    normalized_keyword = _normalize_match_text(keyword)
    if not normalized_keyword:
        return []

    candidates: list[str] = []
    for brand in brand_terms:
        normalized_brand = _normalize_match_text(brand)
        if len(normalized_brand) < 2 or normalized_brand not in normalized_keyword:
            continue
        stripped = normalized_keyword.replace(normalized_brand, "")
        if stripped and stripped != normalized_keyword:
            candidates.append(stripped)

    return [
        candidate
        for candidate in _dedupe_texts(candidates)
        if len(_normalize_match_text(candidate)) >= 4 and _looks_like_core_keyword(candidate)
    ]


def _collect_title_tag_hits(fields: dict[str, str], task: dict) -> dict[str, list[str]]:
    industry_terms = _dedupe_texts([
        str(term or "").strip()
        for term in (task.get("industry_tags") or [])
        if str(term or "").strip()
    ])
    region_terms = _dedupe_texts([
        str(term or "").strip()
        for term in (task.get("region_tags") or [])
        if str(term or "").strip()
    ])
    industry_hits = [
        term for term in industry_terms
        if _candidate_matches_article(fields["title"], term) or _keyword_theme_matches_title(fields["title"], term)
    ]
    region_hits = [
        term for term in region_terms
        if _candidate_matches_article(fields["title"], term) or _keyword_theme_matches_title(fields["title"], term)
    ]
    if not industry_hits or not region_hits:
        return {"industry": [], "region": []}
    return {
        "industry": industry_hits,
        "region": region_hits,
    }


class _CompiledMatchTerm:
    __slots__ = (
        "raw",
        "normalized",
        "segments",
        "stripped_insertion",
        "stripped_insertion_segments",
        "provider_subject",
        "provider_subject_segments",
        "ngrams",
    )

    def __init__(self, raw: str) -> None:
        self.raw = str(raw or "").strip()
        self.normalized = _normalize_match_text(self.raw)
        self.segments = [
            segment
            for segment in _build_match_segments(self.normalized)
            if len(segment) >= 2 or re.search(r"[a-z]", segment)
        ]
        self.stripped_insertion = _strip_title_insertion_noise(self.normalized)
        self.stripped_insertion_segments = [
            segment
            for segment in _build_match_segments(self.stripped_insertion)
            if len(segment) >= 2 or re.search(r"[a-z]", segment)
        ]
        provider_subjects = _extract_provider_subject_terms(self.normalized)
        self.provider_subject = provider_subjects[0] if provider_subjects else ""
        self.provider_subject_segments = [
            segment
            for segment in _build_match_segments(self.provider_subject)
            if len(segment) >= 2 or re.search(r"[a-z]", segment)
        ]
        if len(self.normalized) >= 6:
            ngram_size = 4 if len(self.normalized) >= 10 else 3
            self.ngrams = _build_char_ngrams(self.normalized, ngram_size)
        else:
            self.ngrams = []


class _CompiledArticleTask:
    __slots__ = (
        "task_name",
        "brand_terms",
        "normalized_brand_terms",
        "brand_match_terms",
        "keyword_terms",
        "keyword_match_terms",
        "keyword_core_texts",
        "core_match_terms",
        "core_candidate_match_terms",
        "provider_subject_terms",
        "provider_subject_match_terms",
        "brand_removed_context_terms",
        "brand_removed_context_match_terms",
        "industry_tags",
        "region_tags",
        "industry_match_terms",
        "region_match_terms",
    )

    def __init__(self, task: dict) -> None:
        self.task_name = str(task.get("name", "") or "").strip()
        task_brand = str(task.get("brand", "") or "").strip()
        keywords = task.get("keywords", [])
        if not keywords and task.get("keyword"):
            keywords = [{"keyword": task["keyword"]}]

        self.keyword_terms = _dedupe_texts([
            str((entry or {}).get("keyword", "") or "").strip()
            for entry in keywords
            if isinstance(entry, dict)
        ])
        self.brand_terms = _dedupe_texts(
            [task_brand]
            + [
                str((entry or {}).get("brand", "") or "").strip()
                for entry in keywords
                if isinstance(entry, dict)
            ]
        )
        self.normalized_brand_terms = [
            _normalize_match_text(term)
            for term in self.brand_terms
            if _normalize_match_text(term)
        ]
        self.brand_match_terms = [_CompiledMatchTerm(term) for term in self.brand_terms]
        self.keyword_match_terms = [_CompiledMatchTerm(term) for term in self.keyword_terms]

        self.keyword_core_texts: dict[str, list[str]] = {}
        core_terms: list[str] = []
        provider_subject_terms: list[str] = []
        context_terms: list[str] = []
        core_candidate_terms: list[str] = []
        for keyword in self.keyword_terms:
            keyword_core_terms = [
                core_text
                for core_text in _extract_core_keyword_texts(keyword, task=task)
                if core_text and core_text != keyword
            ]
            self.keyword_core_texts[keyword] = keyword_core_terms
            core_terms.extend(keyword_core_terms)
            core_candidate_terms.extend(keyword_core_terms)
            current_provider_subject_terms = _extract_provider_subject_terms(keyword)
            provider_subject_terms.extend(current_provider_subject_terms)
            core_candidate_terms.extend(current_provider_subject_terms)
            current_context_terms = _remove_brand_terms_from_keyword(keyword, self.brand_terms)
            context_terms.extend(current_context_terms)
            core_candidate_terms.extend(current_context_terms)

        self.core_match_terms = [_CompiledMatchTerm(term) for term in core_terms]
        self.core_candidate_match_terms = [
            _CompiledMatchTerm(term)
            for term in core_candidate_terms
        ]
        self.provider_subject_terms = _dedupe_texts(provider_subject_terms)
        self.provider_subject_match_terms = [
            _CompiledMatchTerm(term)
            for term in self.provider_subject_terms
        ]
        self.brand_removed_context_terms = _dedupe_texts(context_terms)
        self.brand_removed_context_match_terms = [
            _CompiledMatchTerm(term)
            for term in self.brand_removed_context_terms
        ]
        self.industry_tags = _dedupe_texts([
            str(term or "").strip()
            for term in (task.get("industry_tags") or [])
            if str(term or "").strip()
        ])
        self.region_tags = _dedupe_texts([
            str(term or "").strip()
            for term in (task.get("region_tags") or [])
            if str(term or "").strip()
        ])
        self.industry_match_terms = [_CompiledMatchTerm(term) for term in self.industry_tags]
        self.region_match_terms = [_CompiledMatchTerm(term) for term in self.region_tags]


class CompiledArticleMatcher:
    """Preprocessed article matcher for refresh batches sharing one config."""

    __slots__ = ("config_signature", "tasks", "valid_task_names")

    def __init__(self, config: dict | None) -> None:
        self.config_signature = _article_match_config_signature(config)
        self.tasks = [
            compiled_task
            for compiled_task in (
                _CompiledArticleTask(task)
                for task in ((config or {}).get("tasks", []) or [])
                if isinstance(task, dict)
            )
            if compiled_task.task_name
        ]
        self.valid_task_names = {task.task_name for task in self.tasks}

    def analyze(
        self,
        title: str,
        article: dict | None = None,
        *,
        fields: dict[str, str] | None = None,
    ) -> dict[str, object]:
        match_fields = fields if fields is not None else _build_match_fields(title, article)
        return _analyze_article_matches_compiled(match_fields, self)


def compile_article_matcher(config: dict | None) -> CompiledArticleMatcher:
    return CompiledArticleMatcher(config)


def _compiled_candidate_matches(
    normalized_haystack: str,
    normalized_candidate: str,
    segments: list[str],
) -> bool:
    if not normalized_candidate:
        return False
    if normalized_candidate in normalized_haystack:
        return True
    if len(segments) >= 2 and _ordered_segments_match(normalized_haystack, segments):
        return True
    return _near_substring_match_normalized(normalized_haystack, normalized_candidate)


def _compiled_candidate_term_matches(normalized_haystack: str, term: _CompiledMatchTerm) -> bool:
    return _compiled_candidate_matches(normalized_haystack, term.normalized, term.segments)


def _build_compiled_title_context(fields: dict[str, str]) -> dict[str, str]:
    title = fields["title"]
    provider_subject = ""
    if any(term in title for term in _PROVIDER_EQUIVALENT_NORMALIZED):
        provider_subject = _strip_equivalent_provider_terms(title)
    return {
        "title": title,
        "provider_subject": provider_subject,
        "stripped_insertion": _strip_title_insertion_noise(title),
    }


def _compiled_keyword_theme_matches_title(
    title_context: dict[str, str],
    term: _CompiledMatchTerm,
) -> bool:
    normalized_title = title_context["title"]
    normalized_keyword = term.normalized
    if not normalized_title or not normalized_keyword:
        return False
    if _compiled_candidate_term_matches(normalized_title, term):
        return True
    title_provider_subject = title_context.get("provider_subject", "")
    if (
        title_provider_subject
        and term.provider_subject
        and len(title_provider_subject) >= 3
        and len(term.provider_subject) >= 3
        and _compiled_candidate_matches(
            title_provider_subject,
            term.provider_subject,
            term.provider_subject_segments,
        )
    ):
        return True

    stripped_title = title_context.get("stripped_insertion", "")
    stripped_keyword = term.stripped_insertion
    if (
        stripped_title
        and stripped_keyword
        and (stripped_title != normalized_title or stripped_keyword != normalized_keyword)
        and _compiled_candidate_matches(
            stripped_title,
            stripped_keyword,
            term.stripped_insertion_segments,
        )
    ):
        return True

    if len(normalized_keyword) >= 4:
        for split_idx in range(2, len(normalized_keyword) - 1):
            left = normalized_keyword[:split_idx]
            right = normalized_keyword[split_idx:]
            if len(left) < 2 or len(right) < 2:
                continue
            left_idx = normalized_title.find(left)
            if left_idx < 0:
                continue
            search_start = left_idx + len(left)
            right_idx = normalized_title.find(right, search_start)
            if right_idx < 0:
                continue
            gap = normalized_title[search_start:right_idx]
            if not gap or len(gap) > 4:
                continue
            if gap in _TITLE_INSERTION_NOISE_NORMALIZED:
                return True

    if len(normalized_title) < 8 or len(normalized_keyword) < 6:
        return False

    matcher = SequenceMatcher(None, normalized_keyword, normalized_title)
    matching_blocks = matcher.get_matching_blocks()
    max_block = max((block.size for block in matching_blocks), default=0)
    ratio = matcher.ratio()
    keyword_len = len(normalized_keyword)

    if ratio >= 0.72 and max_block >= max(6, min(12, keyword_len - 1)):
        return True

    if len(term.ngrams) < 2:
        return False
    hit_count = sum(1 for gram in term.ngrams if gram in normalized_title)
    hit_ratio = hit_count / len(term.ngrams)
    if max_block >= max(6, int(keyword_len * 0.45)) and hit_ratio >= 0.45:
        return True
    return False


def _collect_compiled_title_tag_hits(
    title_context: dict[str, str],
    task: _CompiledArticleTask,
) -> dict[str, list[str]]:
    industry_hits = [
        term.raw
        for term in task.industry_match_terms
        if _compiled_candidate_term_matches(title_context["title"], term)
        or _compiled_keyword_theme_matches_title(title_context, term)
    ]
    region_hits = [
        term.raw
        for term in task.region_match_terms
        if _compiled_candidate_term_matches(title_context["title"], term)
        or _compiled_keyword_theme_matches_title(title_context, term)
    ]
    if not industry_hits or not region_hits:
        return {"industry": [], "region": []}
    return {
        "industry": industry_hits,
        "region": region_hits,
    }


def _collect_compiled_task_candidates(
    task: _CompiledArticleTask,
    fields: dict[str, str],
    title_context: dict[str, str],
) -> dict[str, object]:
    title_brand_hits = [
        term.raw
        for term in task.brand_match_terms
        if _compiled_candidate_term_matches(fields["title"], term)
    ]
    title_keyword_hits = [
        term.raw
        for term in task.keyword_match_terms
        if _compiled_keyword_theme_matches_title(title_context, term)
    ]

    core_hits: list[str] = []
    for term in task.core_candidate_match_terms:
        if _compiled_keyword_theme_matches_title(title_context, term):
            core_hits.append(term.raw)

    excerpt_brand_hits = [
        term.raw
        for term in task.brand_match_terms
        if _compiled_candidate_term_matches(fields["excerpt"], term)
    ]
    meta_brand_hits = [
        term.raw
        for term in task.brand_match_terms
        if any(_compiled_candidate_term_matches(fields[key], term) for key in ("source", "platform", "url"))
    ]

    title_reasons: list[str] = []
    if title_brand_hits:
        title_reasons.append(f"标题命中品牌名“{_format_term_list(title_brand_hits, 1)}”")
    if title_keyword_hits:
        title_reasons.append(f"标题命中关键词“{_format_term_list(title_keyword_hits, 1)}”")
    if core_hits and not title_keyword_hits:
        title_reasons.append(f"标题命中核心词“{_format_term_list(core_hits, 1)}”")

    title_tag_hits = _collect_compiled_title_tag_hits(title_context, task)
    if title_tag_hits.get("industry") and title_tag_hits.get("region"):
        title_reasons.append(
            "标题命中行业/地区标签"
            f"“{_format_term_list(title_tag_hits['industry'], 1)} / {_format_term_list(title_tag_hits['region'], 1)}”"
        )

    return {
        "candidate": bool(
            title_brand_hits
            or title_keyword_hits
            or core_hits
            or (title_tag_hits.get("industry") and title_tag_hits.get("region"))
        ),
        "title_brand_hits": title_brand_hits,
        "title_keyword_hits": title_keyword_hits,
        "core_hits": core_hits,
        "title_tag_hits": title_tag_hits,
        "excerpt_brand_hits": excerpt_brand_hits,
        "meta_brand_hits": meta_brand_hits,
        "title_reasons": title_reasons[:2],
    }


def _collect_task_candidates(task: dict, fields: dict[str, str]) -> dict[str, object]:
    task_brand = str(task.get("brand", "") or "").strip()
    keywords = task.get("keywords", [])
    if not keywords and task.get("keyword"):
        keywords = [{"keyword": task["keyword"]}]

    keyword_terms = _dedupe_texts([str((entry or {}).get("keyword", "") or "").strip() for entry in keywords if isinstance(entry, dict)])
    brand_terms = _dedupe_texts(
        [task_brand]
        + [str((entry or {}).get("brand", "") or "").strip() for entry in keywords if isinstance(entry, dict)]
    )

    title_brand_hits = [brand for brand in brand_terms if _candidate_matches_article(fields["title"], brand)]
    title_keyword_hits = [keyword for keyword in keyword_terms if _keyword_theme_matches_title(fields["title"], keyword)]

    core_hits: list[str] = []
    for keyword in keyword_terms:
        for core_text in _extract_core_keyword_texts(keyword, task=task):
            if not core_text or core_text == keyword:
                continue
            if _keyword_theme_matches_title(fields["title"], core_text):
                core_hits.append(core_text)
        for subject_text in _extract_provider_subject_terms(keyword):
            if _keyword_theme_matches_title(fields["title"], subject_text):
                core_hits.append(subject_text)
        for context_text in _remove_brand_terms_from_keyword(keyword, brand_terms):
            if _keyword_theme_matches_title(fields["title"], context_text):
                core_hits.append(context_text)

    excerpt_brand_hits = [
        brand for brand in brand_terms
        if _candidate_matches_article(fields["excerpt"], brand)
    ]
    meta_brand_hits = [
        brand for brand in brand_terms
        if any(_candidate_matches_article(fields[key], brand) for key in ("source", "platform", "url"))
    ]

    title_reasons: list[str] = []
    if title_brand_hits:
        title_reasons.append(f"标题命中品牌名“{_format_term_list(title_brand_hits, 1)}”")
    if title_keyword_hits:
        title_reasons.append(f"标题命中关键词“{_format_term_list(title_keyword_hits, 1)}”")
    if core_hits and not title_keyword_hits:
        title_reasons.append(f"标题命中核心词“{_format_term_list(core_hits, 1)}”")

    title_tag_hits = _collect_title_tag_hits(fields, task)
    if title_tag_hits.get("industry") and title_tag_hits.get("region"):
        title_reasons.append(
            "标题命中行业/地区标签"
            f"“{_format_term_list(title_tag_hits['industry'], 1)} / {_format_term_list(title_tag_hits['region'], 1)}”"
        )

    return {
        "candidate": bool(
            title_brand_hits
            or title_keyword_hits
            or core_hits
            or (title_tag_hits.get("industry") and title_tag_hits.get("region"))
        ),
        "title_brand_hits": title_brand_hits,
        "title_keyword_hits": title_keyword_hits,
        "core_hits": core_hits,
        "title_tag_hits": title_tag_hits,
        "excerpt_brand_hits": excerpt_brand_hits,
        "meta_brand_hits": meta_brand_hits,
        "title_reasons": title_reasons[:2],
    }


def _finalize_article_match_candidates(
    fields: dict[str, str],
    candidates: list[dict[str, object]],
) -> dict[str, object]:
    if not fields["full"]:
        return {
            "matched_tasks": [],
            "match_reasons": {},
            "unmatched_reason": "缺少可用于归类的文章信息",
        }

    matched: list[str] = []
    match_reasons: dict[str, list[str]] = {}
    unmatched_reason = ""
    if not candidates:
        unmatched_reason = "标题未命中任何关键词核心词，暂未归类"
    elif len(candidates) == 1:
        item = candidates[0]
        task_name = str(item.get("task_name") or "").strip()
        reasons = [str(reason or "").strip() for reason in (item.get("title_reasons") or []) if str(reason or "").strip()]
        excerpt_brand_hits = [str(term or "").strip() for term in (item.get("excerpt_brand_hits") or []) if str(term or "").strip()]
        meta_brand_hits = [str(term or "").strip() for term in (item.get("meta_brand_hits") or []) if str(term or "").strip()]
        if excerpt_brand_hits:
            reasons.append(f"正文摘录提到品牌“{_format_term_list(excerpt_brand_hits, 1)}”")
        elif meta_brand_hits:
            reasons.append(f"来源信息提到品牌“{_format_term_list(meta_brand_hits, 1)}”")
        matched = [task_name]
        match_reasons[task_name] = reasons[:3] or ["标题命中任务关键词"]
    else:
        brand_signal_candidates = [
            item for item in candidates
            if (
                item.get("title_brand_hits")
                or item.get("excerpt_brand_hits")
                or item.get("meta_brand_hits")
            )
        ]
        selected = brand_signal_candidates
        if not selected:
            keyword_hit_counts: dict[str, int] = {}
            for item in candidates:
                for keyword in (item.get("title_keyword_hits") or []):
                    normalized_keyword = _normalize_match_text(str(keyword or ""))
                    if normalized_keyword:
                        keyword_hit_counts[normalized_keyword] = keyword_hit_counts.get(normalized_keyword, 0) + 1
            selected = [
                item for item in candidates
                if any(
                    keyword_hit_counts.get(_normalize_match_text(str(keyword or "")), 0) == 1
                    for keyword in (item.get("title_keyword_hits") or [])
                )
            ]
        if not selected:
            candidate_names = [
                str(item.get("task_name") or "").strip()
                for item in candidates
                if str(item.get("task_name") or "").strip()
            ]
            unmatched_reason = f"标题命中了多个候选品牌线索，但缺少明确品牌信号：{'、'.join(candidate_names[:3])}"
        for item in selected:
            task_name = str(item.get("task_name") or "").strip()
            reasons = [str(reason or "").strip() for reason in (item.get("title_reasons") or []) if str(reason or "").strip()]
            excerpt_brand_hits = [str(term or "").strip() for term in (item.get("excerpt_brand_hits") or []) if str(term or "").strip()]
            meta_brand_hits = [str(term or "").strip() for term in (item.get("meta_brand_hits") or []) if str(term or "").strip()]
            if excerpt_brand_hits:
                reasons.append(f"正文摘录提到品牌“{_format_term_list(excerpt_brand_hits, 1)}”")
            elif meta_brand_hits:
                reasons.append(f"来源信息提到品牌“{_format_term_list(meta_brand_hits, 1)}”")
            matched.append(task_name)
            match_reasons[task_name] = reasons[:3] or ["标题命中任务关键词"]

    if not matched:
        if not unmatched_reason:
            if candidates:
                core_candidates = [
                    str(item.get("task_name") or "").strip()
                    for item in candidates
                    if str(item.get("task_name") or "").strip()
                ]
                if core_candidates:
                    unmatched_reason = f"标题命中了候选品牌线索，但未定位到明确品牌：{'、'.join(core_candidates[:3])}"
            else:
                unmatched_reason = "未命中品牌名或关键词，暂未归类"

    return {
        "matched_tasks": matched,
        "match_reasons": match_reasons,
        "unmatched_reason": unmatched_reason,
    }


def _analyze_article_matches_compiled(
    fields: dict[str, str],
    compiled_matcher: CompiledArticleMatcher,
) -> dict[str, object]:
    if not fields["full"]:
        return _finalize_article_match_candidates(fields, [])

    title_context = _build_compiled_title_context(fields)
    candidates: list[dict[str, object]] = []
    for task in compiled_matcher.tasks:
        candidate_info = _collect_compiled_task_candidates(task, fields, title_context)
        if not bool(candidate_info.get("candidate")):
            continue
        candidates.append({
            "task_name": task.task_name,
            **candidate_info,
        })
    return _finalize_article_match_candidates(fields, candidates)


def analyze_article_matches(
    title: str,
    config: dict,
    article: dict | None = None,
    *,
    compiled_matcher: CompiledArticleMatcher | None = None,
    fields: dict[str, str] | None = None,
) -> dict[str, object]:
    """返回文章命中的任务、命中原因及未命中提示。"""
    match_fields = fields if fields is not None else _build_match_fields(title, article)
    if compiled_matcher is not None:
        return compiled_matcher.analyze(title, article=article, fields=match_fields)

    if not match_fields["full"]:
        return _finalize_article_match_candidates(match_fields, [])

    candidates: list[dict[str, object]] = []
    for task in config.get("tasks", []):
        task_name = task.get("name", "").strip()
        if not task_name:
            continue
        candidate_info = _collect_task_candidates(task, match_fields)
        if not bool(candidate_info.get("candidate")):
            continue
        candidates.append({
            "task_name": task_name,
            **candidate_info,
        })

    return _finalize_article_match_candidates(match_fields, candidates)


def match_tasks(title: str, config: dict, article: dict | None = None) -> list:
    """根据文章信息匹配任务组关键词，返回命中的任务组名列表。"""
    analyzed = analyze_article_matches(title, config, article=article)
    return list(analyzed.get("matched_tasks") or [])


def _task_keyword_terms(task: dict) -> list[str]:
    keywords = task.get("keywords", [])
    if not keywords and task.get("keyword"):
        keywords = [{"keyword": task["keyword"]}]
    return _dedupe_texts([
        str((entry or {}).get("keyword", "") or "").strip()
        for entry in keywords
        if isinstance(entry, dict) and str((entry or {}).get("keyword", "") or "").strip()
    ])


ARTICLE_EXPORT_KEYWORD_CACHE_VERSION = 2
ARTICLE_EXPORT_KEYWORD_CACHE_FIELDS = (
    "export_keyword_categories",
    "export_keyword_categories_by_task",
    "_export_keyword_config_signature",
    "_export_keyword_article_signature",
    "_export_keyword_cache_version",
)


def build_article_export_keyword_order(config: dict | None, task_name: str = "") -> dict[str, int]:
    """Return keyword display order for article exports, preserving task config order."""
    order: dict[str, int] = {}
    scoped_task_name = str(task_name or "").strip()
    for task in (config or {}).get("tasks", []) or []:
        if not isinstance(task, dict):
            continue
        current_task_name = str(task.get("name", "") or "").strip()
        if scoped_task_name and current_task_name != scoped_task_name:
            continue
        for term in _task_keyword_terms(task):
            if term and term not in order:
                order[term] = len(order)
    return order


def _is_ascii_alnum_char(value: str) -> bool:
    return ("a" <= value <= "z") or ("0" <= value <= "9")


def _contains_ascii_token(haystack: str, needle: str) -> bool:
    if not haystack or not needle:
        return False
    position = 0
    while True:
        index = haystack.find(needle, position)
        if index < 0:
            return False
        before_ok = index == 0 or not _is_ascii_alnum_char(haystack[index - 1])
        after_index = index + len(needle)
        after_ok = after_index >= len(haystack) or not _is_ascii_alnum_char(haystack[after_index])
        if before_ok and after_ok:
            return True
        position = index + 1


def _export_keyword_matches_title(title: str, keyword: str) -> bool:
    raw_title = str(title or "")
    normalized_title = _normalize_match_text(title)
    normalized_keyword = _normalize_match_text(keyword)
    return _export_keyword_matches_normalized_title(
        raw_title.lower(),
        normalized_title,
        normalized_keyword,
    )


def _export_keyword_matches_normalized_title(
    raw_title_lower: str,
    normalized_title: str,
    normalized_keyword: str,
) -> bool:
    if not normalized_title or not normalized_keyword:
        return False
    if re.fullmatch(r"[a-z0-9]{1,3}", normalized_keyword):
        return (
            _contains_ascii_token(raw_title_lower, normalized_keyword)
            or _contains_ascii_token(normalized_title, normalized_keyword)
        )
    return normalized_keyword in normalized_title


def build_article_export_keyword_plan(config: dict | None, task_name: str = "") -> list[dict[str, str]]:
    """Precompute user-configured keyword labels for repeated article export matching."""
    scoped_task_name = str(task_name or "").strip()
    plan: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for task in (config or {}).get("tasks", []) or []:
        if not isinstance(task, dict):
            continue
        current_task_name = str(task.get("name", "") or "").strip()
        if scoped_task_name and current_task_name != scoped_task_name:
            continue
        for keyword in _task_keyword_terms(task):
            normalized_keyword = _normalize_match_text(keyword)
            if not normalized_keyword:
                continue
            key = (current_task_name, keyword)
            if key in seen:
                continue
            seen.add(key)
            plan.append({
                "task_name": current_task_name,
                "label": keyword,
                "normalized_keyword": normalized_keyword,
            })
    return plan


def build_article_export_keyword_signature(config: dict | None) -> str:
    """Return a stable signature for configured export keyword labels."""
    tasks: list[dict[str, object]] = []
    for task in (config or {}).get("tasks", []) or []:
        if not isinstance(task, dict):
            continue
        tasks.append({
            "name": str(task.get("name", "") or "").strip(),
            "keywords": _task_keyword_terms(task),
        })
    payload = json.dumps(
        {
            "version": ARTICLE_EXPORT_KEYWORD_CACHE_VERSION,
            "tasks": tasks,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()


def _article_export_keyword_article_signature(article: dict | None) -> str:
    item = article or {}
    matched_tasks = [
        str(name or "").strip()
        for name in (item.get("matched_tasks") or [])
        if str(name or "").strip()
    ]
    payload = json.dumps(
        {
            "version": ARTICLE_EXPORT_KEYWORD_CACHE_VERSION,
            "title": str(item.get("title", "") or ""),
            "matched_tasks": matched_tasks,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()


def _valid_article_export_keyword_cache(
    article: dict | None,
    config_signature: str,
    article_signature: str,
) -> tuple[list[str], dict[str, list[str]]] | None:
    if not isinstance(article, dict):
        return None
    if str(article.get("_export_keyword_cache_version") or "") != str(ARTICLE_EXPORT_KEYWORD_CACHE_VERSION):
        return None
    if str(article.get("_export_keyword_config_signature") or "") != str(config_signature or ""):
        return None
    if str(article.get("_export_keyword_article_signature") or "") != str(article_signature or ""):
        return None
    labels = [
        str(label or "").strip()
        for label in (article.get("export_keyword_categories") or [])
        if str(label or "").strip()
    ]
    raw_by_task = article.get("export_keyword_categories_by_task")
    if not isinstance(raw_by_task, dict):
        return None
    by_task: dict[str, list[str]] = {}
    for raw_task_name, raw_labels in raw_by_task.items():
        task_name = str(raw_task_name or "").strip()
        if not task_name:
            continue
        by_task[task_name] = [
            str(label or "").strip()
            for label in (raw_labels or [])
            if str(label or "").strip()
        ]
    return labels, by_task


def build_article_export_keyword_cache(
    article: dict,
    config: dict | None,
    *,
    keyword_plan: list[dict[str, str]] | None = None,
    config_signature: str = "",
) -> dict[str, object]:
    plan = keyword_plan if keyword_plan is not None else build_article_export_keyword_plan(config)
    resolved_config_signature = config_signature or build_article_export_keyword_signature(config)
    article_signature = _article_export_keyword_article_signature(article)
    article_title = str((article or {}).get("title", "") or "")
    raw_title_lower = article_title.lower()
    normalized_title = _normalize_match_text(article_title)
    matched_task_set = {
        str(name or "").strip()
        for name in ((article or {}).get("matched_tasks") or [])
        if str(name or "").strip()
    }
    labels: list[str] = []
    by_task: dict[str, list[str]] = {}
    for entry in plan:
        task_name = str(entry.get("task_name", "") or "").strip()
        label = str(entry.get("label", "") or "").strip()
        normalized_keyword = str(entry.get("normalized_keyword", "") or "").strip()
        if (
            not task_name
            or not label
            or not _export_keyword_matches_normalized_title(raw_title_lower, normalized_title, normalized_keyword)
        ):
            continue
        task_labels = by_task.setdefault(task_name, [])
        if label not in task_labels:
            task_labels.append(label)
        if (not matched_task_set or task_name in matched_task_set) and label not in labels:
            labels.append(label)
    return {
        "export_keyword_categories": labels,
        "export_keyword_categories_by_task": by_task,
        "_export_keyword_config_signature": resolved_config_signature,
        "_export_keyword_article_signature": article_signature,
        "_export_keyword_cache_version": ARTICLE_EXPORT_KEYWORD_CACHE_VERSION,
    }


def article_export_keyword_cache_fields(article: dict) -> dict[str, object]:
    return {
        field: article.get(field)
        for field in ARTICLE_EXPORT_KEYWORD_CACHE_FIELDS
        if field in article
    }


def update_article_export_keyword_cache(
    article: dict,
    config: dict | None,
    *,
    keyword_plan: list[dict[str, str]] | None = None,
    config_signature: str = "",
) -> bool:
    cache = build_article_export_keyword_cache(
        article,
        config,
        keyword_plan=keyword_plan,
        config_signature=config_signature,
    )
    changed = any(article.get(key) != value for key, value in cache.items())
    article.update(cache)
    return changed


def resolve_article_export_keywords(
    article: dict,
    config: dict | None,
    task_name: str = "",
    *,
    keyword_plan: list[dict[str, str]] | None = None,
    keyword_config_signature: str = "",
) -> list[str]:
    """Resolve export keyword labels from user-configured task keywords only."""
    article_title = str((article or {}).get("title", "") or "")
    raw_title_lower = article_title.lower()
    normalized_title = _normalize_match_text(article_title)
    matched_tasks = [
        str(name or "").strip()
        for name in ((article or {}).get("matched_tasks") or [])
        if str(name or "").strip()
    ]
    matched_task_set = set(matched_tasks)
    scoped_task_name = str(task_name or "").strip()
    config_signature = keyword_config_signature or build_article_export_keyword_signature(config)
    article_signature = _article_export_keyword_article_signature(article)
    cached = _valid_article_export_keyword_cache(article, config_signature, article_signature)
    if cached is not None:
        cached_labels, cached_by_task = cached
        if scoped_task_name:
            return list(cached_by_task.get(scoped_task_name) or [])
        return list(cached_labels)

    labels: list[str] = []
    plan = keyword_plan if keyword_plan is not None else build_article_export_keyword_plan(config, scoped_task_name)

    for entry in plan:
        current_task_name = str(entry.get("task_name", "") or "").strip()
        if not scoped_task_name and matched_task_set and current_task_name not in matched_task_set:
            continue
        label = str(entry.get("label", "") or "").strip()
        normalized_keyword = str(entry.get("normalized_keyword", "") or "").strip()
        if (
            label
            and label not in labels
            and _export_keyword_matches_normalized_title(raw_title_lower, normalized_title, normalized_keyword)
        ):
            labels.append(label)

    return labels


def _article_match_config_signature(config: dict | None) -> str:
    tasks: list[dict[str, object]] = []
    for task in (config or {}).get("tasks", []) or []:
        if not isinstance(task, dict):
            continue
        keywords: list[dict[str, str]] = []
        raw_keywords = task.get("keywords", [])
        if not raw_keywords and task.get("keyword"):
            raw_keywords = [{"keyword": task.get("keyword")}]
        for entry in raw_keywords or []:
            if not isinstance(entry, dict):
                continue
            keywords.append({
                "keyword": str(entry.get("keyword", "") or "").strip(),
                "brand": str(entry.get("brand", "") or "").strip(),
            })
        tasks.append({
            "name": str(task.get("name", "") or "").strip(),
            "brand": str(task.get("brand", "") or "").strip(),
            "keywords": keywords,
            "industry_tags": [
                str(term or "").strip()
                for term in (task.get("industry_tags") or [])
                if str(term or "").strip()
            ],
            "region_tags": [
                str(term or "").strip()
                for term in (task.get("region_tags") or [])
                if str(term or "").strip()
            ],
        })
    payload = json.dumps(tasks, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()


def _article_match_signature(
    article: dict,
    config_signature: str,
    *,
    fields: dict[str, str] | None = None,
) -> str:
    match_fields = (
        fields
        if fields is not None
        else _build_match_fields(str((article or {}).get("title", "") or ""), article)
    )
    payload = {
        "version": 2,
        "config": config_signature,
        "fields": match_fields,
        "excluded_tasks": sorted(
            str(name or "").strip()
            for name in ((article or {}).get("excluded_tasks") or [])
            if str(name or "").strip()
        ),
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def refresh_article_matches(config: dict) -> list:
    """
    根据当前配置重建文章与任务的关联关系，返回最新文章列表。
    保留历史归类任务名，并补齐新匹配到的任务。
    """
    if _article_store_sqlite_enabled():
        return _refresh_article_matches_sqlite(config)

    compiled_matcher = compile_article_matcher(config)
    config_signature = compiled_matcher.config_signature
    export_keyword_plan = build_article_export_keyword_plan(config)
    export_keyword_config_signature = build_article_export_keyword_signature(config)

    updated_articles: list[dict] = []
    shadow_context: dict[str, object] | None = None
    with _lock:
        articles = _load_articles()
        changed = False
        for article in articles:
            excluded_task_names = {
                str(name or "").strip()
                for name in (article.get("excluded_tasks") or [])
                if str(name or "").strip()
            }
            raw_matched = [
                str(name or "").strip()
                for name in (article.get("matched_tasks") or [])
                if str(name or "").strip()
            ]
            stored = [
                str(name or "").strip()
                for name in (article.get("matched_tasks") or [])
                if str(name or "").strip()
                and str(name or "").strip() not in excluded_task_names
            ]
            existing_reasons = article.get("match_reasons") if isinstance(article.get("match_reasons"), dict) else {}
            fields = _build_match_fields(str(article.get("title", "") or ""), article)
            current_signature = _article_match_signature(article, config_signature, fields=fields)
            if str(article.get("_match_signature") or "") == current_signature:
                match_reasons = {
                    task_name: existing_reasons.get(task_name) or ["保留历史归类"]
                    for task_name in stored
                }
                unmatched_reason = "" if stored else str(article.get("unmatched_reason", "") or "").strip()
                match_fields_changed = (
                    stored != raw_matched
                    or match_reasons != existing_reasons
                    or unmatched_reason != str(article.get("unmatched_reason", "") or "").strip()
                )
                if match_fields_changed:
                    article["matched_tasks"] = stored
                    article["match_reasons"] = match_reasons
                    article["unmatched_reason"] = unmatched_reason
                cache_changed = update_article_export_keyword_cache(
                    article,
                    config,
                    keyword_plan=export_keyword_plan,
                    config_signature=export_keyword_config_signature,
                )
                if match_fields_changed or cache_changed:
                    updated_articles.append(dict(article))
                    changed = True
                continue

            analyzed = analyze_article_matches(
                article.get("title", ""),
                config,
                article=article,
                compiled_matcher=compiled_matcher,
                fields=fields,
            )
            inferred = [
                str(name or "").strip()
                for name in (analyzed.get("matched_tasks") or [])
                if str(name or "").strip()
                and str(name or "").strip() not in excluded_task_names
            ]
            inferred_reasons = {
                str(name or "").strip(): [
                    str(reason or "").strip()
                    for reason in reasons
                    if str(reason or "").strip()
                ]
                for name, reasons in (analyzed.get("match_reasons") or {}).items()
                if str(name or "").strip()
                and str(name or "").strip() not in excluded_task_names
            }
            merged = []
            for task_name in stored + inferred:
                if task_name and task_name not in merged:
                    merged.append(task_name)
            match_reasons = {
                task_name: inferred_reasons.get(task_name) or ["保留历史归类"]
                for task_name in merged
            }
            unmatched_reason = "" if merged else str(analyzed.get("unmatched_reason", "") or "").strip()
            if (
                merged != list(article.get("matched_tasks") or [])
                or match_reasons != dict(article.get("match_reasons") or {})
                or unmatched_reason != str(article.get("unmatched_reason", "") or "").strip()
                or str(article.get("_match_signature") or "") != current_signature
            ):
                article["matched_tasks"] = merged
                article["match_reasons"] = match_reasons
                article["unmatched_reason"] = unmatched_reason
                article["_match_signature"] = current_signature
                update_article_export_keyword_cache(
                    article,
                    config,
                    keyword_plan=export_keyword_plan,
                    config_signature=export_keyword_config_signature,
                )
                updated_articles.append(dict(article))
                changed = True
        if changed:
            shadow_context = _begin_article_shadow_runtime_write()
            _save_articles(articles)
    if updated_articles and shadow_context is not None:
        _sync_article_shadow_articles(
            updated_articles,
            reason="refresh_article_matches",
            context=shadow_context,
        )

    return _sort_articles_for_display(articles)


def _match_refresh_batch_size() -> int:
    try:
        value = int(os.environ.get(ARTICLE_MATCH_REFRESH_BATCH_SIZE_ENV, DEFAULT_MATCH_REFRESH_BATCH_SIZE))
    except Exception:
        value = DEFAULT_MATCH_REFRESH_BATCH_SIZE
    return max(10, min(2000, value))


def _match_refresh_sleep_seconds() -> float:
    try:
        value = float(os.environ.get(ARTICLE_MATCH_REFRESH_SLEEP_SECONDS_ENV, DEFAULT_MATCH_REFRESH_SLEEP_SECONDS))
    except Exception:
        value = DEFAULT_MATCH_REFRESH_SLEEP_SECONDS
    return max(0.0, min(0.5, value))


def _write_match_refresh_meta(store, **kwargs) -> None:
    for key, value in kwargs.items():
        store.set_meta(f"match_refresh_{key}", str(value or ""))


def get_article_match_refresh_status() -> dict[str, object]:
    if not _article_store_sqlite_enabled():
        return {"backend": "json", "message": "match_refresh_job_only_available_for_sqlite_backend"}
    try:
        store = _article_sqlite_store()
    except Exception:
        return {"backend": "sqlite", "error": "store_unavailable"}
    meta_keys = [
        "match_refresh_running",
        "match_refresh_config_signature",
        "match_refresh_total",
        "match_refresh_needs_refresh_count",
        "match_refresh_processed_count",
        "match_refresh_updated_count",
        "match_refresh_analyzed_count",
        "match_refresh_batch_size",
        "match_refresh_started_at",
        "match_refresh_updated_at",
        "match_refresh_finished_at",
        "match_refresh_last_error",
        "match_refresh_reason",
    ]
    meta_snapshot: dict[str, str] = {}
    for key in meta_keys:
        try:
            meta_snapshot[key] = store.get_meta(key)
        except Exception:
            meta_snapshot[key] = ""
    running = meta_snapshot.get("match_refresh_running") == "1"
    with _match_refresh_worker_lock:
        worker_alive = _match_refresh_worker_thread is not None and _match_refresh_worker_thread.is_alive()
    total = int(meta_snapshot.get("match_refresh_total") or 0)
    processed = int(meta_snapshot.get("match_refresh_processed_count") or 0)
    finished_at = meta_snapshot.get("match_refresh_finished_at", "")
    last_error = meta_snapshot.get("match_refresh_last_error", "")
    if running and not worker_alive:
        status = "interrupted"
    elif running:
        status = "running"
    elif last_error:
        status = "error"
    elif finished_at:
        status = "finished"
    elif not running and total > 0:
        status = "finished"
    else:
        status = "idle"
    return {
        "backend": "sqlite",
        "status": status,
        "running": running,
        "worker_alive": worker_alive,
        "config_signature": meta_snapshot.get("match_refresh_config_signature", ""),
        "total": total,
        "needs_refresh_count": int(meta_snapshot.get("match_refresh_needs_refresh_count") or 0),
        "processed_count": processed,
        "updated_count": int(meta_snapshot.get("match_refresh_updated_count") or 0),
        "analyzed_count": int(meta_snapshot.get("match_refresh_analyzed_count") or 0),
        "batch_size": int(meta_snapshot.get("match_refresh_batch_size") or 0),
        "started_at": meta_snapshot.get("match_refresh_started_at", ""),
        "updated_at": meta_snapshot.get("match_refresh_updated_at", ""),
        "finished_at": finished_at,
        "last_error": last_error,
        "reason": meta_snapshot.get("match_refresh_reason", ""),
    }


def _run_article_match_refresh_worker(config: dict, reason: str) -> None:
    store = None
    try:
        compiled = compile_article_matcher(config)
        config_signature = compiled.config_signature
        export_keyword_plan = build_article_export_keyword_plan(config)
        export_keyword_config_signature = build_article_export_keyword_signature(config)
        store = _article_sqlite_store()
        batch_size = _match_refresh_batch_size()
        sleep_seconds = _match_refresh_sleep_seconds()
        stats = store.get_match_refresh_stats(config_signature)
        needs_refresh = int(stats.get("needs_refresh_count") or 0)
        total_articles = int(stats.get("total") or 0)
        _write_match_refresh_meta(
            store,
            running="1",
            config_signature=config_signature,
            total=str(total_articles),
            needs_refresh_count=str(needs_refresh),
            processed_count="0",
            updated_count="0",
            analyzed_count="0",
            batch_size=str(batch_size),
            started_at=local_now().isoformat(timespec="seconds"),
            updated_at="",
            finished_at="",
            last_error="",
            reason=str(reason or ""),
        )
        processed = 0
        updated = 0
        analyzed = 0
        for batch in store.iter_articles_needing_match(config_signature, batch_size=batch_size):
            updates: list[dict] = []
            for article in batch:
                excluded_task_names = {
                    str(name or "").strip()
                    for name in (article.get("excluded_tasks") or [])
                    if str(name or "").strip()
                }
                raw_matched = [
                    str(name or "").strip()
                    for name in (article.get("matched_tasks") or [])
                    if str(name or "").strip()
                ]
                stored = [
                    str(name or "").strip()
                    for name in (article.get("matched_tasks") or [])
                    if str(name or "").strip()
                    and str(name or "").strip() not in excluded_task_names
                ]
                existing_reasons = article.get("match_reasons") if isinstance(article.get("match_reasons"), dict) else {}
                fields = _build_match_fields(str(article.get("title", "") or ""), article)
                current_signature = _article_match_signature(article, config_signature, fields=fields)
                metadata_changed = (
                    str(article.get("_match_signature") or "") != current_signature
                    or str(article.get("_match_config_signature") or "") != config_signature
                )
                if str(article.get("_match_signature") or "") == current_signature:
                    match_reasons = {
                        task_name: existing_reasons.get(task_name) or ["保留历史归类"]
                        for task_name in stored
                    }
                    unmatched_reason = "" if stored else str(article.get("unmatched_reason", "") or "").strip()
                    update_payload = {
                        "id": article.get("id"),
                        "matched_tasks": stored,
                        "match_reasons": match_reasons,
                        "unmatched_reason": unmatched_reason,
                        "_match_signature": current_signature,
                        "_match_config_signature": config_signature,
                    }
                    cache_article = dict(article)
                    cache_article.update(update_payload)
                    cache_changed = update_article_export_keyword_cache(
                        cache_article,
                        config,
                        keyword_plan=export_keyword_plan,
                        config_signature=export_keyword_config_signature,
                    )
                    if (
                        stored != raw_matched
                        or match_reasons != existing_reasons
                        or unmatched_reason != str(article.get("unmatched_reason", "") or "").strip()
                        or metadata_changed
                        or cache_changed
                    ):
                        update_payload.update(article_export_keyword_cache_fields(cache_article))
                        updates.append(update_payload)
                    processed += 1
                    continue
                analyzed_result = analyze_article_matches(
                    article.get("title", ""),
                    config,
                    article=article,
                    compiled_matcher=compiled,
                    fields=fields,
                )
                analyzed += 1
                inferred = [
                    str(name or "").strip()
                    for name in (analyzed_result.get("matched_tasks") or [])
                    if str(name or "").strip()
                    and str(name or "").strip() not in excluded_task_names
                ]
                inferred_reasons = {
                    str(name or "").strip(): [
                        str(r or "").strip()
                        for r in reasons
                        if str(r or "").strip()
                    ]
                    for name, reasons in (analyzed_result.get("match_reasons") or {}).items()
                    if str(name or "").strip()
                    and str(name or "").strip() not in excluded_task_names
                }
                merged = []
                for task_name in stored + inferred:
                    if task_name and task_name not in merged:
                        merged.append(task_name)
                match_reasons_map = {
                    task_name: inferred_reasons.get(task_name) or ["保留历史归类"]
                    for task_name in merged
                }
                unmatched_reason = "" if merged else str(analyzed_result.get("unmatched_reason", "") or "").strip()
                update_payload = {
                    "id": article.get("id"),
                    "matched_tasks": merged,
                    "match_reasons": match_reasons_map,
                    "unmatched_reason": unmatched_reason,
                    "_match_signature": current_signature,
                    "_match_config_signature": config_signature,
                }
                cache_article = dict(article)
                cache_article.update(update_payload)
                update_article_export_keyword_cache(
                    cache_article,
                    config,
                    keyword_plan=export_keyword_plan,
                    config_signature=export_keyword_config_signature,
                )
                update_payload.update(article_export_keyword_cache_fields(cache_article))
                updates.append(update_payload)
                processed += 1
            if updates:
                results = store.bulk_update_match_fields(updates)
                updated += len(results)
            _write_match_refresh_meta(
                store,
                processed_count=str(processed),
                updated_count=str(updated),
                analyzed_count=str(analyzed),
                updated_at=local_now().isoformat(timespec="seconds"),
            )
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
        _write_match_refresh_meta(
            store,
            running="0",
            finished_at=local_now().isoformat(timespec="seconds"),
            updated_at=local_now().isoformat(timespec="seconds"),
        )
    except Exception as exc:
        try:
            if store is None:
                store = _article_sqlite_store()
            _write_match_refresh_meta(
                store,
                running="0",
                last_error=f"{exc.__class__.__name__}: {exc}",
                finished_at=local_now().isoformat(timespec="seconds"),
                updated_at=local_now().isoformat(timespec="seconds"),
            )
        except Exception:
            pass
    finally:
        _match_refresh_worker_lock.acquire()
        try:
            global _match_refresh_worker_thread
            _match_refresh_worker_thread = None
        finally:
            _match_refresh_worker_lock.release()


def schedule_article_match_refresh(config: dict, *, reason: str = "", force: bool = False) -> dict[str, object]:
    if not _article_store_sqlite_enabled():
        return {"scheduled": False, "reason": "not_sqlite_backend"}
    compiled = compile_article_matcher(config)
    config_signature = compiled.config_signature
    store = _article_sqlite_store()
    stats = store.get_match_refresh_stats(config_signature)
    needs_refresh = int(stats.get("needs_refresh_count") or 0)
    if needs_refresh <= 0 and not force:
        return {
            "scheduled": False,
            "reason": "nothing_to_refresh",
            "needs_refresh_count": 0,
            "total": int(stats.get("total") or 0),
        }
    _match_refresh_worker_lock.acquire()
    try:
        global _match_refresh_worker_thread
        if _match_refresh_worker_thread is not None and _match_refresh_worker_thread.is_alive():
            return {
                "scheduled": False,
                "reason": "already_running",
                "needs_refresh_count": needs_refresh,
                "total": int(stats.get("total") or 0),
                "status": get_article_match_refresh_status(),
            }
        batch_size = _match_refresh_batch_size()
        _write_match_refresh_meta(
            store,
            running="1",
            config_signature=config_signature,
            total=str(int(stats.get("total") or 0)),
            needs_refresh_count=str(needs_refresh),
            processed_count="0",
            updated_count="0",
            analyzed_count="0",
            batch_size=str(batch_size),
            started_at=local_now().isoformat(timespec="seconds"),
            updated_at="",
            finished_at="",
            last_error="",
            reason=str(reason or ""),
        )
        thread = threading.Thread(
            target=_run_article_match_refresh_worker,
            args=(config, str(reason or "")),
            name="article-match-refresh-worker",
            daemon=True,
        )
        _match_refresh_worker_thread = thread
        thread.start()
    finally:
        _match_refresh_worker_lock.release()
    return {
        "scheduled": True,
        "reason": str(reason or ""),
        "needs_refresh_count": needs_refresh,
        "total": int(stats.get("total") or 0),
        "batch_size": batch_size,
        "sleep_seconds": _match_refresh_sleep_seconds(),
    }


def _reset_article_match_refresh_state_for_tests(timeout: float = 5.0) -> None:
    global _match_refresh_worker_thread
    deadline = time.monotonic() + max(0.0, float(timeout or 0.0))
    while True:
        with _match_refresh_worker_lock:
            thread = _match_refresh_worker_thread
        if thread is None or not thread.is_alive() or thread is threading.current_thread():
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(timeout=min(0.05, max(0.0, remaining)))

    with _match_refresh_worker_lock:
        if _match_refresh_worker_thread is not None and not _match_refresh_worker_thread.is_alive():
            _match_refresh_worker_thread = None
