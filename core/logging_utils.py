"""Logging helpers for redacting secrets in diagnostic output."""

from __future__ import annotations

import logging
import re
from typing import Any


_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|api[_-]?token|access[_-]?token|refresh[_-]?token|session[_-]?token|"
    r"authorization|bearer|webhook[_-]?url|secret|password)",
    re.IGNORECASE,
)
_SECRET_TEXT_RE = re.compile(
    r"(?P<key>api[_-]?key|api[_-]?token|access[_-]?token|refresh[_-]?token|session[_-]?token|"
    r"authorization|webhook[_-]?url|secret|password)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<quote>['\"]?)"
    r"(?P<value>[^'\"\s,}]+)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)


def redact_secrets(value: Any) -> Any:
    """Return a copy-like value with common credential fields redacted."""
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            redacted[key] = "***" if _SECRET_KEY_RE.search(key_text) else redact_secrets(item)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item) for item in value)
    if isinstance(value, str):
        return redact_secret_text(value)
    return value


def redact_secret_text(text: Any) -> str:
    value = str(text)
    value = _BEARER_RE.sub("Bearer ***", value)
    return _SECRET_TEXT_RE.sub(lambda match: f"{match.group('key')}{match.group('sep')}{match.group('quote')}***", value)


class SecretRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_secret_text(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True
