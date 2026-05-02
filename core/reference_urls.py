from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from core.article_fetcher import _unwrap_known_redirect_url
from core.article_store import normalize_article_url

_REFERENCE_URL_PATTERNS = (
    r"(https?://[^\s<>'\"）)\]】]+)",
    r"((?:www\.)[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/[^\s<>'\"）)\]】]*)?)",
)


def normalize_reference_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""

    try:
        raw = _unwrap_known_redirect_url(raw)
    except Exception:
        pass

    if not raw:
        return ""

    try:
        parsed = urlsplit(raw)
        raw = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
    except Exception:
        raw = raw.split("#", 1)[0].strip()

    normalized = normalize_article_url(raw)
    if normalized.startswith("http://"):
        normalized = f"https://{normalized[len('http://'):]}"
        normalized = normalize_article_url(normalized)
    return normalized


def extract_reference_urls_from_text(text: str) -> list[str]:
    raw_text = str(text or "")
    if not raw_text:
        return []

    urls: list[str] = []
    seen: set[str] = set()
    for pattern_index, pattern in enumerate(_REFERENCE_URL_PATTERNS):
        for match in re.finditer(pattern, raw_text, re.IGNORECASE):
            if pattern_index == 1:
                start = match.start(1)
                if raw_text[max(0, start - 3):start].lower() == "%2f":
                    continue
            candidate = str(match.group(1) or "").strip()
            candidate = candidate.rstrip("，。；：!！?？)）]】>,\"'")
            if not candidate:
                continue
            if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", candidate):
                candidate = f"https://{candidate}"
            normalized = normalize_reference_url(candidate)
            try:
                parsed = urlsplit(normalized)
                if "%" in candidate and (parsed.path or "") in {"", "/"}:
                    continue
            except Exception:
                pass
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            urls.append(normalized)
    return urls
