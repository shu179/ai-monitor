"""
时间工具

统一程序内“当前本地时间/今天日期”的来源，并兼容历史上已经写入的
无时区时间字符串，避免不同模块各自取时间导致展示或跨天判断不一致。
"""

from __future__ import annotations

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo


_TIMEZONE_ENV = "AIBRANDMONITOR_TIMEZONE"


def _get_timezone():
    configured = os.environ.get(_TIMEZONE_ENV, "").strip()
    if configured:
        try:
            return ZoneInfo(configured)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo


def local_now() -> datetime:
    """返回当前本地时间；可通过环境变量覆盖时区。"""
    now = datetime.now().astimezone()
    tz = _get_timezone()
    return now if tz is None else now.astimezone(tz)


def local_today() -> date:
    """返回当前本地自然日。"""
    return local_now().date()


def parse_local_date(value) -> date | None:
    """尽量从常见时间字符串中解析出本地自然日。"""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        normalized = text.replace("Z", "+00:00")
        dt = None
        for parser in (
            lambda: datetime.fromisoformat(normalized),
            lambda: datetime.strptime(text, "%Y-%m-%d %H:%M:%S"),
            lambda: datetime.strptime(text, "%Y-%m-%d %H:%M"),
            lambda: datetime.strptime(text, "%Y-%m-%d"),
        ):
            try:
                dt = parser()
                break
            except Exception:
                continue
        if dt is None:
            return None

    if dt.tzinfo is None:
        return dt.date()
    tz = _get_timezone()
    return dt.astimezone(tz).date() if tz is not None else dt.astimezone().date()
