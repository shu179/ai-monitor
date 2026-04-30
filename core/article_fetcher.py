"""
文章信息识别模块

优先通过 HTTP 抓取 HTML 提取标题和平台名；
失败时调用现有 AI API（doubao）兜底识别，同时让 AI 判断媒体类型。
识别完成后将域名→媒体类型写入 domain_overrides（自动记忆，不覆盖已有手动设置）。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import unquote
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

import requests

from core.article_store import (
    classify_article_media_type,
    extract_domain,
    resolve_media_name,
    resolve_media_name_detail,
    save_domain_media_name,
    save_domain_override,
    should_auto_save_media_type,
)

# 复用已有的请求头常量
try:
    from platforms.api_client import (
        COMMON_HEADERS,
        get_platform_api_key,
        platform_requires_api_key,
        query_platform_api,
    )
except Exception:
    COMMON_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }
    get_platform_api_key = None  # type: ignore
    platform_requires_api_key = None  # type: ignore
    query_platform_api = None  # type: ignore

FETCH_TIMEOUT = (3.05, 4.5)
BROWSER_FETCH_TIMEOUT_MS = 9000
_FETCH_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,image/apng,*/*;q=0.8"
)
_PUBLISHED_META_KEYS = {
    "article:published_time",
    "article:modified_time",
    "article:published",
    "article:modified",
    "og:published_time",
    "og:updated_time",
    "datepublished",
    "datecreated",
    "datemodified",
    "date",
    "pubdate",
    "pub-date",
    "publishdate",
    "publish_date",
    "publish_time",
    "publish-time",
    "publishtime",
    "published",
    "published_at",
    "published_time",
    "publisheddate",
    "publisheddate",
    "release_date",
    "releasedate",
    "release_time",
    "releasetime",
    "created",
    "created_at",
    "created_time",
    "createtime",
    "updated",
    "updated_at",
    "updated_time",
    "updatetime",
    "modified",
    "modified_at",
    "modified_time",
    "lastmod",
    "last-modified",
    "weibo:article:create_at",
}
_PUBLISHED_JSON_KEYS = (
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
)
_PUBLISHED_KEY_PATTERN = "|".join(
    re.escape(key) for key in sorted(set(_PUBLISHED_JSON_KEYS) | _PUBLISHED_META_KEYS, key=len, reverse=True)
)
_SITE_NAME_META_KEYS = {
    "og:site_name",
    "application-name",
    "apple-mobile-web-app-title",
    "publisher",
    "source",
    "sitename",
    "site_name",
    "site",
    "media",
    "organization",
    "copyright",
}
_SHORT_CHANNEL_SEGMENTS = {
    "新闻",
    "资讯",
    "频道",
    "要闻",
    "时政",
    "政务",
    "民生",
    "社会",
    "本地",
    "国内",
    "国际",
    "财经",
    "产经",
    "产业",
    "科技",
    "教育",
    "旅游",
    "文化",
    "体育",
    "娱乐",
    "健康",
    "汽车",
    "房产",
    "专题",
    "公告",
    "视频",
    "图片",
}
_HOMEPAGE_MEDIA_CACHE: dict[str, str] = {}


# ---------------------------------------------------------------------------
# HTML 解析工具
# ---------------------------------------------------------------------------

class _MetaParser(HTMLParser):
    """轻量 HTML 解析器，提取 title / og:title / og:site_name"""

    def __init__(self):
        super().__init__()
        self.title: str = ""
        self.og_title: str = ""
        self.og_site_name: str = ""
        self.site_name_source: str = ""
        self.published_at: str = ""
        self._in_title = False
        self._title_buf: list = []
        self._ignored_stack: list[str] = []
        self._body_chunks: list[str] = []
        self._body_chars = 0
        self._ignored_tags = {"script", "style", "noscript", "svg"}

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "title":
            self._in_title = True
            self._title_buf = []
        elif tag in self._ignored_tags:
            self._ignored_stack.append(tag)
        elif tag == "meta":
            prop = attrs_dict.get("property", "").lower()
            name = attrs_dict.get("name", "").lower()
            itemprop = attrs_dict.get("itemprop", "").lower()
            content = attrs_dict.get("content", "").strip()
            if prop == "og:title" and content:
                self.og_title = content
            elif prop == "og:site_name" and content:
                self.og_site_name = content
                self.site_name_source = "meta"
            elif content and not self.og_site_name and (prop or name or itemprop) in _SITE_NAME_META_KEYS:
                self.og_site_name = content
                self.site_name_source = "meta"
            elif content and not self.published_at:
                date_key = prop or name or itemprop
                if date_key in _PUBLISHED_META_KEYS:
                    self.published_at = content
        elif tag == "time" and not self.published_at:
            attrs_dict = dict(attrs)
            datetime_value = str(attrs_dict.get("datetime") or attrs_dict.get("pubdate") or "").strip()
            if datetime_value:
                self.published_at = datetime_value

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
            self.title = "".join(self._title_buf).strip()
        elif self._ignored_stack and tag == self._ignored_stack[-1]:
            self._ignored_stack.pop()

    def handle_data(self, data):
        if self._in_title:
            self._title_buf.append(data)
            return

        if self._ignored_stack or self._body_chars >= 1200:
            return

        text = re.sub(r"\s+", " ", str(data or "")).strip()
        if len(text) < 2:
            return
        if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", text):
            return
        if text in self._body_chunks[-3:]:
            return

        remaining = max(0, 1200 - self._body_chars)
        if remaining <= 0:
            return
        clipped = text[:remaining]
        self._body_chunks.append(clipped)
        self._body_chars += len(clipped)

    @property
    def excerpt(self) -> str:
        merged = re.sub(r"\s+", " ", " ".join(self._body_chunks)).strip()
        if len(merged) <= 360:
            return merged
        return merged[:360].rstrip() + "…"


def _clean_extracted_text(value: str) -> str:
    text = unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)
    text = text.replace("\\n", " ").replace("\\t", " ").replace("\\r", " ")
    text = text.replace('\\"', '"').replace("\\/", "/")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_published_at(value: str) -> str:
    text = _clean_extracted_text(value)
    if not text:
        return ""

    if re.fullmatch(r"\d{13}", text):
        try:
            return datetime.fromtimestamp(int(text) / 1000).strftime("%Y-%m-%d")
        except Exception:
            return ""
    if re.fullmatch(r"\d{10}", text):
        try:
            return datetime.fromtimestamp(int(text)).strftime("%Y-%m-%d")
        except Exception:
            return ""

    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
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

    match = re.search(
        r"(\d{4}-\d{1,2}-\d{1,2})(?:\s+|[^\d])?(\d{1,2}:\d{1,2}(?::\d{1,2})?)?",
        normalized,
    )
    if match:
        date_part = match.group(1)
        time_part = match.group(2) or "00:00"
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            candidate = f"{date_part} {time_part}"
            try:
                return datetime.strptime(candidate, fmt).strftime("%Y-%m-%d")
            except Exception:
                continue

    try:
        return datetime.fromisoformat(normalized).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _extract_published_at_from_raw_html(html_text: str) -> str:
    raw = str(html_text or "")
    patterns = (
        rf'"(?:{_PUBLISHED_KEY_PATTERN})"\s*:\s*"?([^",}}\]]{{4,80}})"?',
        rf'"(?:{_PUBLISHED_KEY_PATTERN})"\s*:\s*(\d{{10,13}})',
        r'<time[^>]+datetime=["\']([^"\']+)["\']',
        rf'<meta[^>]+(?:property|name|itemprop)=["\'](?:{_PUBLISHED_KEY_PATTERN})["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name|itemprop)=["\'](?:{_PUBLISHED_KEY_PATTERN})["\']',
        r'(?:发布时间|发布日期|发稿时间|发表时间|发布时间为|发布于|时间|日期|编辑时间|更新于|更新时间)[:：\s]*([0-9]{4}[年./-]\s*[0-9]{1,2}[月./-]\s*[0-9]{1,2}[日]?(?:\s+[0-9]{1,2}:[0-9]{1,2}(?::[0-9]{1,2})?)?)',
        r'(?:发布时间|发布日期|发稿时间|发表时间|发布于|更新于)[^0-9]{0,20}([0-9]{4}[年./-]\s*[0-9]{1,2}[月./-]\s*[0-9]{1,2}[日]?)',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, raw[:300000], re.IGNORECASE | re.DOTALL):
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


def _looks_like_compact_site_label(text: str) -> bool:
    value = _clean_extracted_text(text)
    if len(value) < 2 or len(value) > 16:
        return False
    if re.search(r"https?://|www\.", value, re.IGNORECASE):
        return False
    if re.search(r"[，。！？；：,:;!?/]", value):
        return False
    return bool(re.search(r"[\u4e00-\u9fffA-Za-z0-9]", value))


def _looks_like_site_name_segment(text: str) -> bool:
    value = _clean_extracted_text(text)
    if not _looks_like_compact_site_label(value):
        return False
    if _looks_like_channel_segment(value):
        return False
    return bool(
        re.search(
            r"(日报|晚报|晨报|时报|周刊|新闻网|新闻客户端|客户端|官网|头条|号|网|台)$",
            value,
        )
    )


def _looks_like_channel_segment(text: str) -> bool:
    value = _clean_extracted_text(text)
    if not _looks_like_compact_site_label(value):
        return False
    if re.search(r"\d", value):
        return False
    if value in _SHORT_CHANNEL_SEGMENTS:
        return True
    if re.search(
        r"(产经|财经|产业|资讯|频道|要闻|时政|政务|民生|社会|本地|国内|国际|财经|科技|教育|旅游|文化|体育|娱乐|健康|汽车|房产|专题|公告|视频|图片|新闻)$",
        value,
    ):
        return True
    return False


def _schema_name_from_value(value) -> str:
    if isinstance(value, str):
        name = _clean_extracted_text(value)
        return name if name and not _looks_like_channel_segment(name) else ""
    if isinstance(value, dict):
        name = _clean_extracted_text(str(value.get("name") or ""))
        if name and not _looks_like_channel_segment(name):
            return name
        for key in ("publisher", "sourceOrganization", "provider", "copyrightHolder", "isPartOf"):
            nested_name = _schema_name_from_value(value.get(key))
            if nested_name:
                return nested_name
    if isinstance(value, list):
        for item in value:
            name = _schema_name_from_value(item)
            if name:
                return name
    return ""


def _schema_publisher_from_root(data) -> str:
    publisher_keys = ("publisher", "sourceOrganization", "provider", "copyrightHolder", "isPartOf")
    if isinstance(data, dict):
        for key in publisher_keys:
            name = _schema_name_from_value(data.get(key))
            if name:
                return name
        graph = data.get("@graph")
        if graph is not None:
            name = _schema_publisher_from_root(graph)
            if name:
                return name
    elif isinstance(data, list):
        for item in data:
            name = _schema_publisher_from_root(item)
            if name:
                return name
    return ""


def _iter_schema_json_blocks(html_text: str):
    raw = str(html_text or "")[:300000]
    for match in re.finditer(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        raw,
        re.IGNORECASE | re.DOTALL,
    ):
        text = unescape(str(match.group(1) or "")).strip()
        text = re.sub(r"^\s*<!--", "", text)
        text = re.sub(r"-->\s*$", "", text).strip()
        if text:
            yield text


def _extract_schema_publisher_name(html_text: str) -> str:
    for block in _iter_schema_json_blocks(html_text):
        try:
            data = json.loads(block)
        except Exception:
            data = None
        if data is not None:
            name = _schema_publisher_from_root(data)
            if name:
                return name

    raw = str(html_text or "")[:300000]
    patterns = (
        r'"publisher"\s*:\s*\{[^{}]{0,800}?"name"\s*:\s*"([^"]{2,60})"',
        r'"sourceOrganization"\s*:\s*\{[^{}]{0,800}?"name"\s*:\s*"([^"]{2,60})"',
        r'"copyrightHolder"\s*:\s*\{[^{}]{0,800}?"name"\s*:\s*"([^"]{2,60})"',
        r'"provider"\s*:\s*\{[^{}]{0,800}?"name"\s*:\s*"([^"]{2,60})"',
    )
    for pattern in patterns:
        match = re.search(pattern, raw, re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        name = _clean_extracted_text(match.group(1))
        if name and not _looks_like_channel_segment(name):
            return name
    return ""


def _refine_title_and_site_name(
    raw_title: str,
    site_name_hint: str = "",
    site_name_source: str = "",
) -> tuple[str, str, str]:
    title = _clean_extracted_text(raw_title)
    if not title:
        return "", _clean_extracted_text(site_name_hint), site_name_source

    site_name = _clean_extracted_text(site_name_hint)
    if site_name and "." in site_name and not re.search(r"[\u4e00-\u9fff]", site_name):
        site_name = ""
        site_name_source = ""

    parts = [
        _clean_extracted_text(part)
        for part in re.split(r"\s*(?:\||｜|_|—|–|-)\s*", title)
        if _clean_extracted_text(part)
    ]
    if len(parts) < 2:
        return title, site_name, site_name_source

    kept = list(parts)
    if site_name and kept[-1] == site_name:
        kept = kept[:-1]
    elif len(kept) >= 3 and _looks_like_site_name_segment(kept[-1]):
        site_name = kept[-1]
        site_name_source = "title_suffix"
        kept = kept[:-1]
    elif len(kept) == 2 and _looks_like_site_name_segment(kept[-1]) and len(kept[0]) >= max(8, len(kept[-1]) * 2):
        site_name = kept[-1]
        site_name_source = "title_suffix"
        kept = kept[:-1]

    while len(kept) >= 2 and _looks_like_channel_segment(kept[-1]) and len(kept[-2]) >= max(8, len(kept[-1]) * 2):
        kept = kept[:-1]

    refined_title = " - ".join(kept) if kept else title
    return refined_title or title, site_name, site_name_source


def _looks_like_valid_title(title: str, domain: str = "") -> bool:
    text = _clean_extracted_text(title)
    if len(text) < 4 or len(text) > 120:
        return False
    lowered = text.lower()
    normalized_domain = str(domain or "").strip().lower()
    invalid_values = {
        "",
        "今日头条",
        "头条",
        "toutiao",
        "www.toutiao.com",
        "m.toutiao.com",
        normalized_domain,
    }
    if lowered in invalid_values:
        return False
    if text.startswith("http://") or text.startswith("https://"):
        return False
    return bool(re.search(r"[\u4e00-\u9fffA-Za-z0-9]", text))


def _parse_html_info(html_text: str, url: str) -> dict:
    """从 HTML 文本提取标题、平台名和正文摘录。"""
    parser = _MetaParser()
    try:
        parser.feed(html_text[:80000])  # 只解析前80KB，避免超大页面卡住
    except Exception:
        pass

    domain = extract_domain(url)
    raw_site_name = (parser.og_site_name or "").strip()
    site_name_source = parser.site_name_source
    if not raw_site_name:
        raw_site_name = _extract_schema_publisher_name(html_text)
        if raw_site_name:
            site_name_source = "schema"
    title = (parser.og_title or parser.title or "").strip()
    if raw_site_name and _looks_like_channel_segment(raw_site_name):
        raw_site_name = ""
        site_name_source = ""
    if title and not _looks_like_valid_title(title, domain):
        title = ""
    if not title:
        title = _extract_title_from_raw_html(html_text, url)
    if title:
        title, raw_site_name, site_name_source = _refine_title_and_site_name(
            title,
            raw_site_name,
            site_name_source,
        )

    if title and not _looks_like_valid_title(title, domain):
        title = ""
    media_detail = resolve_media_name_detail(
        raw_site_name,
        page_url=url,
        source=site_name_source or "unknown",
    )
    media_name = str(media_detail.get("name") or "")
    platform = media_name or (raw_site_name or domain).strip()
    published_at = (
        _normalize_published_at(parser.published_at)
        or _extract_published_at_from_raw_html(html_text)
        or _extract_published_at_from_url(url)
    )

    return {
        "title": title,
        "platform": platform,
        "media_name": media_name,
        "media_name_confidence": float(media_detail.get("confidence") or 0),
        "media_name_source": str(media_detail.get("source") or ""),
        "media_name_can_learn": bool(media_detail.get("can_learn")),
        "excerpt": parser.excerpt,
        "published_at": published_at,
    }


def _extract_toutiao_title_from_raw_text(raw_text: str) -> str:
    patterns = (
        r'"headline"\s*:\s*"([^"]{4,220})"',
        r'"shareInfo"\s*:\s*\{.*?"title"\s*:\s*"([^"]{4,220})"',
        r'"tt_title"\s*:\s*"([^"]{4,220})"',
        r'"title"\s*:\s*"([^"]{4,220})"\s*,\s*"(?:content|abstract|group_source|source|publish_time|article_url|source_url)"',
        r'"title"\s*:\s*\{\s*"text"\s*:\s*"([^"]{4,220})"',
    )
    for pattern in patterns:
        match = re.search(pattern, raw_text, re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        title = _clean_extracted_text(match.group(1))
        if _looks_like_valid_title(title, "toutiao.com"):
            return title
    return ""


def _extract_title_from_raw_html(html_text: str, url: str = "") -> str:
    domain = extract_domain(url)
    url_published_at = _extract_published_at_from_url(url)
    patterns = (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
        r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:title["\']',
        r'<meta[^>]+name=["\']title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']title["\']',
        r'"headline"\s*:\s*"([^"]{4,160})"',
        r'"title"\s*:\s*"([^"]{4,160})"',
        r'"share_title"\s*:\s*"([^"]{4,160})"',
        r'"article_title"\s*:\s*"([^"]{4,160})"',
        r"<h1[^>]*>(.*?)</h1>",
    )
    for pattern in patterns:
        match = re.search(pattern, html_text, re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        title = _clean_extracted_text(match.group(1))
        if _looks_like_valid_title(title, domain):
            return title
    if "toutiao.com" in domain:
        return _extract_toutiao_title_from_raw_text(html_text)
    return ""


def _append_fetch_candidate(candidates: list[str], candidate: str) -> None:
    normalized = str(candidate or "").strip()
    if normalized and normalized not in candidates:
        candidates.append(normalized)


def extract_article_input_url(raw_text: str) -> str:
    text = str(raw_text or "").strip()
    if not text:
        return ""

    patterns = (
        r"(https?://[^\s<>'\"）)\]]+)",
        r"((?:www\.)?[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/[^\s<>'\"）)\]]*)?)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        candidate = str(match.group(1) or "").strip()
        candidate = candidate.rstrip("，。；：!！?？)）]】>,")
        if candidate:
            if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", candidate):
                candidate = f"https://{candidate}"
            return _unwrap_known_redirect_url(candidate)
    return ""


def _remove_query_keys(url: str, keys: set[str]) -> str:
    parsed = urlsplit(url)
    filtered = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if str(key or "").strip().lower() not in keys
    ]
    query = urlencode(filtered, doseq=True)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))


def _unwrap_known_redirect_url(url: str, max_depth: int = 3) -> str:
    current = str(url or "").strip()
    if not current:
        return ""

    redirect_keys = ("target", "url", "dest", "destination", "redirect", "redirect_url", "redirect_uri", "jump", "jump_url", "to")
    for _ in range(max_depth):
        try:
            parsed = urlsplit(current)
        except Exception:
            break
        query_map = dict(parse_qsl(parsed.query, keep_blank_values=True))
        next_url = ""
        for key in redirect_keys:
            raw = str(query_map.get(key, "") or "").strip()
            if raw:
                next_url = unquote(raw)
                break
        if not next_url:
            break
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", next_url):
            next_url = f"https://{next_url.lstrip('/')}"
        current = next_url
    return current


def _build_fetch_candidates(url: str) -> list[str]:
    candidates: list[str] = []
    raw = _unwrap_known_redirect_url(str(url or "").strip())
    if not raw:
        return candidates

    _append_fetch_candidate(candidates, raw)
    parsed = urlsplit(raw)
    host = str(parsed.netloc or "").strip().lower()
    scheme = parsed.scheme or "https"
    path = parsed.path or ""
    query = parsed.query or ""

    if host in {"toutiao.com", "www.toutiao.com", "m.toutiao.com"}:
        _append_fetch_candidate(candidates, _remove_query_keys(raw, {"source", "wid", "wid_try", "utm_source", "app", "timestamp"}))

        article_id_match = (
            re.search(r"/article/([0-9]{8,})(?:/|$)", path)
            or re.search(r"/[ai]([0-9]{8,})(?:/|$)", path)
        )
        article_id = str(article_id_match.group(1) or "").strip() if article_id_match else ""
        if article_id:
            canonical_variants = (
                f"{scheme}://www.toutiao.com/article/{article_id}",
                f"{scheme}://www.toutiao.com/article/{article_id}/",
                f"{scheme}://m.toutiao.com/article/{article_id}",
                f"{scheme}://m.toutiao.com/article/{article_id}/",
                f"{scheme}://m.toutiao.com/i{article_id}/",
                f"{scheme}://m.toutiao.com/i{article_id}/info/",
            )
            for candidate in canonical_variants:
                _append_fetch_candidate(candidates, candidate)

        if host == "toutiao.com":
            _append_fetch_candidate(candidates, urlunsplit((scheme, "www.toutiao.com", path, query, "")))
        elif host == "www.toutiao.com":
            _append_fetch_candidate(candidates, urlunsplit((scheme, "m.toutiao.com", path, query, "")))
        elif host == "m.toutiao.com":
            _append_fetch_candidate(candidates, urlunsplit((scheme, "www.toutiao.com", path, query, "")))
    elif host in {"mp.weixin.qq.com", "weixin.qq.com"}:
        _append_fetch_candidate(candidates, _remove_query_keys(raw, {"scene", "srcid", "sharer_shareinfo", "sharer_shareinfo_first", "mpshare", "from", "subscene", "clicktime", "enterid"}))
    elif host in {"link.zhihu.com", "link.csdn.net"}:
        _append_fetch_candidate(candidates, _unwrap_known_redirect_url(raw))
    elif host in {"xhslink.com", "www.xhslink.com"}:
        _append_fetch_candidate(candidates, _remove_query_keys(raw, {"xhsshare", "appuid", "apptime", "share_id", "share_from_user_hidden"}))
    elif host in {"www.xiaohongshu.com", "xiaohongshu.com", "xiaohongshu.cn"}:
        _append_fetch_candidate(candidates, _remove_query_keys(raw, {"xsec_token", "xsec_source", "appuid", "apptime", "share_id", "share_from_user_hidden"}))
    return candidates


def _build_fetch_headers(url: str) -> dict:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    headers = dict(COMMON_HEADERS)
    headers.update({
        "Accept": _FETCH_ACCEPT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    if origin:
        headers["Referer"] = f"{origin}/"
    return headers


def _decode_html_response(resp: requests.Response) -> str:
    raw_bytes = resp.content or b""
    if not raw_bytes:
        return ""

    sniffed_charset = ""
    head = raw_bytes[:4096]
    meta_match = re.search(
        br"<meta[^>]+charset\s*=\s*['\"]?\s*([A-Za-z0-9._-]+)",
        head,
        re.IGNORECASE,
    )
    if meta_match:
        try:
            sniffed_charset = meta_match.group(1).decode("ascii", errors="ignore").strip()
        except Exception:
            sniffed_charset = ""

    charset_candidates: list[str] = []
    for candidate in (
        sniffed_charset,
        getattr(resp, "apparent_encoding", "") or "",
        resp.encoding or "",
        "utf-8",
    ):
        normalized = str(candidate or "").strip()
        if normalized and normalized not in charset_candidates:
            charset_candidates.append(normalized)

    for charset in charset_candidates:
        try:
            return raw_bytes.decode(charset, errors="replace")
        except Exception:
            continue

    return raw_bytes.decode("utf-8", errors="replace")


def _fetch_html_with_fallbacks(url: str) -> tuple[dict | None, str]:
    last_error = ""
    for candidate_url in _build_fetch_candidates(url):
        try:
            resp = requests.get(
                candidate_url,
                headers=_build_fetch_headers(candidate_url),
                timeout=FETCH_TIMEOUT,
                allow_redirects=True,
            )
            resp.raise_for_status()
            return {
                "requested_url": candidate_url,
                "final_url": resp.url,
                "html_text": _decode_html_response(resp),
            }, ""
        except Exception as exc:
            last_error = str(exc)
            print(f"[ArticleFetcher] HTML 抓取失败 ({candidate_url}): {exc}")
    return None, last_error


def _should_try_browser_fetch(url: str) -> bool:
    return bool(str(url or "").strip())


def _fetch_html_via_browser(url: str) -> tuple[dict | None, str]:
    if not _should_try_browser_fetch(url):
        return None, ""

    try:
        from patchright.sync_api import sync_playwright
    except Exception as exc:
        return None, f"playwright unavailable: {exc}"
    try:
        from core.browser_runtime import resolve_system_browser_executable
    except Exception as exc:
        return None, f"browser runtime unavailable: {exc}"

    browser_executable = resolve_system_browser_executable()
    if not browser_executable:
        return None, "system chrome not found"

    last_error = ""
    for candidate_url in _build_fetch_candidates(url)[:4]:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, executable_path=browser_executable)
                page = browser.new_page()
                page.set_default_timeout(BROWSER_FETCH_TIMEOUT_MS)
                page.goto(candidate_url, wait_until="domcontentloaded", timeout=BROWSER_FETCH_TIMEOUT_MS)
                html_text = page.content()
                if not _extract_title_from_raw_html(html_text, page.url or candidate_url):
                    try:
                        page.wait_for_load_state("networkidle", timeout=1200)
                    except Exception:
                        pass
                    try:
                        page.wait_for_timeout(350)
                    except Exception:
                        pass
                    html_text = page.content()
                final_url = page.url or candidate_url
                browser.close()
                return {
                    "requested_url": candidate_url,
                    "final_url": final_url,
                    "html_text": html_text,
                }, ""
        except Exception as exc:
            last_error = str(exc)
            print(f"[ArticleFetcher] 浏览器抓取失败 ({candidate_url}): {exc}")
    return None, last_error


def _looks_unresolved_media_name(media_name: str, domain: str) -> bool:
    name = str(media_name or "").strip().lower()
    normalized_domain = str(domain or "").strip().lower()
    return not name or name == normalized_domain


def _build_site_homepage_candidates(url: str) -> list[str]:
    candidates: list[str] = []
    try:
        parsed = urlsplit(str(url or "").strip())
    except Exception:
        return candidates

    scheme = parsed.scheme or "https"
    host = str(parsed.netloc or "").strip()
    if not host:
        return candidates

    def _append(candidate_host: str) -> None:
        normalized_host = str(candidate_host or "").strip()
        if not normalized_host:
            return
        candidate_url = urlunsplit((scheme, normalized_host, "/", "", ""))
        if candidate_url not in candidates:
            candidates.append(candidate_url)

    _append(host)
    host_without_port = host.split(":", 1)[0]
    for prefix in ("m.", "wap.", "www."):
        if host_without_port.startswith(prefix):
            alternative = host_without_port[len(prefix):]
            if alternative:
                port_suffix = ""
                if ":" in host:
                    port_suffix = ":" + host.split(":", 1)[1]
                _append(alternative + port_suffix)
    return candidates


def _resolve_media_name_via_site_homepage(url: str) -> str:
    cache_key = extract_domain(url)
    if cache_key and cache_key in _HOMEPAGE_MEDIA_CACHE:
        return _HOMEPAGE_MEDIA_CACHE[cache_key]

    for homepage_url in _build_site_homepage_candidates(url):
        homepage_payload, _ = _fetch_html_with_fallbacks(homepage_url)
        homepage_result = None
        resolved_homepage_url = homepage_url
        if homepage_payload:
            resolved_homepage_url = str(
                homepage_payload.get("final_url") or homepage_payload.get("requested_url") or homepage_url
            ).strip() or homepage_url
            homepage_html = str(homepage_payload.get("html_text") or "")
            homepage_result = _parse_html_info(homepage_html, resolved_homepage_url)

        if not homepage_result and _should_try_browser_fetch(homepage_url):
            browser_payload, _ = _fetch_html_via_browser(homepage_url)
            if browser_payload:
                resolved_homepage_url = str(
                    browser_payload.get("final_url") or browser_payload.get("requested_url") or homepage_url
                ).strip() or homepage_url
                browser_html = str(browser_payload.get("html_text") or "")
                homepage_result = _parse_html_info(browser_html, resolved_homepage_url)

        if not homepage_result:
            continue

        homepage_domain = extract_domain(resolved_homepage_url)
        media_name = str(homepage_result.get("media_name") or homepage_result.get("platform") or "").strip()
        media_confidence = float(homepage_result.get("media_name_confidence") or 0)
        if media_name and media_confidence >= 0.78 and not _looks_unresolved_media_name(media_name, homepage_domain):
            if cache_key:
                _HOMEPAGE_MEDIA_CACHE[cache_key] = media_name
            return media_name
    return ""


# ---------------------------------------------------------------------------
# AI 兜底识别
# ---------------------------------------------------------------------------

def _fetch_via_ai(url: str, config: dict, known_title: str = "") -> dict | None:
    """
    调用 doubao API 识别文章标题、平台名和媒体类型。
    返回 {"title", "platform", "media_type"} 或 None（失败时）。
    """
    if query_platform_api is None:
        return None

    ai_cfg = config.get("ai_assistant", {})
    platform_code = ai_cfg.get("platform", "doubao")
    plat_cfg = config.get("platforms", {}).get(platform_code, {})
    api_key = get_platform_api_key(config, platform_code) if get_platform_api_key else plat_cfg.get("api_key", "").strip()
    api_model = plat_cfg.get("api_model", "").strip() or ai_cfg.get("model", "").strip()

    if platform_requires_api_key and platform_requires_api_key(platform_code) and not api_key:
        return None

    domain = extract_domain(url)
    default_media_name = resolve_media_name(domain) or ("今日头条" if "toutiao.com" in domain else domain)

    prompt = (
        f"请根据以下URL识别这篇文章的标题、来源媒体名称，并判断该媒体是权威媒体还是自媒体。\n"
        f"URL: {url}\n"
        f"已知标题（若有可参考）: {known_title or '无'}\n\n"
        "要求：\n"
        "1. 优先识别文章标题；如果能判断标题，就不要返回空字符串。\n"
        "2. media_name 必须返回具体媒体名，不要返回域名，不要返回网址。\n"
        "3. 若是搜狐/新浪/网易/腾讯新闻/今日头条/百家号/微信公众号这类，请直接返回该媒体或平台名。\n"
        "4. media_type 只能返回 authority 或 selfmedia。\n"
        "5. 只有在完全无法判断标题时，title 才允许为空。\n\n"
        "请严格只返回 JSON，不要加解释：\n"
        '{"title": "文章标题", "media_name": "具体媒体名", "platform": "具体媒体名", "media_type": "authority或selfmedia"}'
    )

    def _extract_json_payload(text: str) -> dict | None:
        raw = str(text or "").strip()
        if not raw:
            return None
        code_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL | re.IGNORECASE)
        if code_match:
            raw = code_match.group(1).strip()
        else:
            start = raw.find("{")
            end = raw.rfind("}")
            if start >= 0 and end > start:
                raw = raw[start:end + 1]
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _extract_plain_title(text: str) -> str:
        raw = str(text or "").strip()
        if not raw:
            return ""
        raw = re.sub(r"```(?:json)?|```", " ", raw, flags=re.IGNORECASE)
        raw = re.sub(r"^[Tt]itle\s*[:：]\s*", "", raw).strip()
        raw = raw.strip(" \t\r\n\"'“”‘’[]()（）")
        invalid_markers = (
            "无法识别", "未识别", "无法判断", "未找到", "找不到", "无法获取",
            "无法访问", "链接失效", "内容不存在", "页面不存在", "标题为空",
            "仅凭该链接", "需要打开", "抱歉", "对不起", "请提供", "{", "}",
        )
        if any(marker in raw for marker in invalid_markers):
            return ""
        if not _looks_like_valid_title(raw, domain):
            return ""
        return raw

    def _fetch_title_only() -> str:
        title_prompt = (
            f"请根据以下URL尽力识别文章标题。\nURL: {url}\n"
            f"已知标题（若有可参考）: {known_title or '无'}\n\n"
            "要求：\n"
            "1. 如果能判断标题，必须返回完整标题。\n"
            "2. 如果完全无法判断，再返回空字符串。\n"
            "3. 优先只返回标题纯文本，不要解释；如果你必须返回结构化结果，也只能返回 {\"title\":\"文章标题\"}。\n"
        )
        title_text = query_platform_api(
            platform=platform_code,
            keyword=title_prompt,
            api_key=api_key,
            model=api_model,
            enable_search=True,
            deep_think=False,
        )
        title_payload = _extract_json_payload(title_text or "")
        payload_title = str((title_payload or {}).get("title", "") or "").strip()
        if _looks_like_valid_title(payload_title, domain):
            return payload_title
        return _extract_plain_title(title_text or "")

    try:
        result_text = query_platform_api(
            platform=platform_code,
            keyword=prompt,
            api_key=api_key,
            model=api_model,
            enable_search=True,
            deep_think=False,
        )
        if not result_text:
            return None

        data = _extract_json_payload(result_text)
        if data:
            title = str(data.get("title", "")).strip() or str(known_title or "").strip()
            if not title:
                title = _fetch_title_only()
            platform = str(data.get("platform", "")).strip()
            raw_media_name = str(data.get("media_name", "")).strip()
            media_detail = resolve_media_name_detail(
                raw_media_name or platform,
                page_url=url,
                source="ai",
            )
            media_name = str(media_detail.get("name") or "") or default_media_name
            media_type = str(data.get("media_type", "selfmedia")).strip()
            if media_type not in ("authority", "selfmedia"):
                media_type = "selfmedia"
            if media_name:
                return {
                    "title": title,
                    "platform": media_name or platform,
                    "media_name": media_name or platform,
                    "media_name_confidence": float(media_detail.get("confidence") or 0),
                    "media_name_source": str(media_detail.get("source") or "ai"),
                    "media_name_can_learn": bool(media_detail.get("can_learn")),
                    "media_type": media_type,
                }
        plain_title = _extract_plain_title(result_text)
        if plain_title:
            return {
                "title": plain_title,
                "platform": default_media_name,
                "media_name": default_media_name,
                "media_type": classify_article_media_type(url or domain, default_media_name),
            }
        print(f"[ArticleFetcher] AI 原始返回未解析出标题: {str(result_text)[:240]}")
    except Exception as e:
        print(f"[ArticleFetcher] AI 识别失败: {e}")

    return None


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def fetch_article_info(url: str, config: dict) -> dict:
    """
    从 URL 识别文章标题、平台名、媒体类型。
    优先级：
    1. domain_overrides 已有记录 → 直接用记忆的媒体类型，仍需抓 HTML 获取标题
    2. 抓 HTML → 提取标题/平台 → 白名单判断媒体类型 → 写入 overrides
    3. HTML 成功但媒体名仍未识别 → AI 核实媒体名/分类并记忆
    4. HTML 失败 → AI 识别标题/平台/媒体类型 → 写入 overrides

    返回：
    {
        "title": str,
        "platform": str,
        "media_type": "authority" | "selfmedia",
        "fetch_method": "html" | "ai" | "failed",
    }
    """
    url = url.strip()
    domain = extract_domain(url)
    url_published_at = _extract_published_at_from_url(url)

    # 先尝试 HTTP 抓取 HTML
    html_result = None
    resolved_url = url
    html_payload, _ = _fetch_html_with_fallbacks(url)
    if html_payload:
        resolved_url = str(html_payload.get("final_url") or html_payload.get("requested_url") or url).strip() or url
        html_text = str(html_payload.get("html_text") or "")
        html_result = _parse_html_info(html_text, resolved_url)

    # 普通抓取失败后，统一补一次真实浏览器渲染抓取。
    if not (html_result and html_result.get("title")) and _should_try_browser_fetch(url):
        print(f"[ArticleFetcher] 普通抓取未命中标题，尝试浏览器渲染抓取...")
        browser_payload, _ = _fetch_html_via_browser(url)
        if browser_payload:
            resolved_url = str(browser_payload.get("final_url") or browser_payload.get("requested_url") or url).strip() or url
            browser_html = str(browser_payload.get("html_text") or "")
            browser_result = _parse_html_info(browser_html, resolved_url)
            if browser_result and browser_result.get("title"):
                html_result = browser_result

    resolved_domain = extract_domain(resolved_url) or domain

    if html_result and html_result.get("title"):
        html_media_name = html_result.get("media_name") or resolve_media_name(resolved_domain)
        html_media_can_learn = bool(html_result.get("media_name_can_learn"))
        if html_media_name and _looks_like_channel_segment(str(html_media_name)):
            domain_media_name = resolve_media_name(resolved_domain)
            if domain_media_name and not _looks_unresolved_media_name(domain_media_name, resolved_domain):
                html_media_name = domain_media_name
            else:
                html_media_name = domain_media_name or resolved_domain
            html_media_can_learn = False
        if _looks_unresolved_media_name(html_media_name, resolved_domain):
            homepage_media_name = _resolve_media_name_via_site_homepage(resolved_url or url)
            if homepage_media_name:
                html_media_name = homepage_media_name
                html_media_can_learn = False
        media_type = classify_article_media_type(
            resolved_url or resolved_domain,
            str(html_media_name or html_result.get("platform") or "").strip(),
        )
        if html_media_can_learn and html_media_name and not _looks_unresolved_media_name(html_media_name, resolved_domain):
            save_domain_media_name(resolved_domain, html_media_name, force=False)
        if should_auto_save_media_type(resolved_url or resolved_domain, media_type, html_media_name):
            save_domain_override(resolved_domain, media_type, force=False)
        return {
            "title": html_result["title"],
            "platform": html_media_name or html_result["platform"] or resolved_domain,
            "media_name": html_media_name,
            "media_type": media_type,
            "excerpt": html_result.get("excerpt", ""),
            "published_at": html_result.get("published_at", "") or url_published_at,
            "fetch_method": "html",
        }

    # HTML + 浏览器都未命中，再走 AI 兜底。
    print(f"[ArticleFetcher] HTML / 浏览器均未能提取标题，尝试 AI 识别...")
    ai_result = _fetch_via_ai(url, config)
    if ai_result:
        ai_media_name = ai_result.get("media_name") or resolve_media_name(domain)
        media_type = str(ai_result.get("media_type", "") or "").strip().lower()
        if media_type not in {"authority", "selfmedia"}:
            media_type = classify_article_media_type(url or domain, ai_media_name)
        # AI 返回的分类写入 overrides（不覆盖已有手动设置）
        if should_auto_save_media_type(url or domain, media_type, ai_media_name):
            save_domain_override(domain, media_type, force=False)
        if ai_media_name and not _looks_unresolved_media_name(ai_media_name, domain):
            ai_media_can_learn = bool(ai_result.get("media_name_can_learn"))
        else:
            ai_media_can_learn = False
        if ai_media_can_learn and ai_media_name and not _looks_unresolved_media_name(ai_media_name, domain):
            save_domain_media_name(domain, ai_media_name, force=False)
        return {
            "title": ai_result["title"],
            "platform": ai_result.get("platform") or domain,
            "media_name": ai_media_name,
            "media_type": media_type,
            "excerpt": "",
            "published_at": ai_result.get("published_at", "") or url_published_at,
            "fetch_method": "ai",
        }

    # 全部失败
    print(f"[ArticleFetcher] 无法识别文章信息: {url}")
    fallback_media_name = resolve_media_name(domain) or domain
    media_type = classify_article_media_type(url or domain, fallback_media_name)
    return {
        "title": "",
        "platform": fallback_media_name,
        "media_name": fallback_media_name,
        "media_type": media_type,
        "excerpt": "",
        "published_at": url_published_at,
        "fetch_method": "failed",
    }
