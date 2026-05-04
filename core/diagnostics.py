"""
失败诊断中心

- 记录任务执行、通知、同步等异常事件
- 支持在 UI 中查看和手动标记为已处理
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import datetime

from .app_paths import resolve_app_path
from .local_account_space import account_scoped_path


DEFAULT_DIAGNOSTICS_PATH = resolve_app_path("logs/diagnostics.json")
DIAGNOSTICS_PATH = DEFAULT_DIAGNOSTICS_PATH
MAX_EVENTS = 500

_LOCK = threading.Lock()


def record_event(
    category: str,
    message: str,
    *,
    level: str = "error",
    task_name: str = "",
    platform: str = "",
    keyword: str = "",
    brand: str = "",
    details: dict | None = None,
    suggestion: str = "",
) -> dict:
    """记录一条诊断事件。"""
    diagnostics_path = _diagnostics_path()
    diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "id": uuid.uuid4().hex,
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "level": str(level or "error").strip() or "error",
        "category": str(category or "general").strip() or "general",
        "message": str(message or "").strip(),
        "task_name": str(task_name or "").strip(),
        "platform": str(platform or "").strip(),
        "keyword": str(keyword or "").strip(),
        "brand": str(brand or "").strip(),
        "details": details or {},
        "suggestion": str(suggestion or _default_suggestion(category, message)).strip(),
        "resolved": False,
        "resolved_at": "",
        "resolved_note": "",
    }

    with _LOCK:
        items = _load_events()
        items.append(event)
        if len(items) > MAX_EVENTS:
            items = items[-MAX_EVENTS:]
        _save_events(items)

    return event


def get_events(limit: int = 200, include_resolved: bool = True) -> list[dict]:
    """读取诊断事件，按时间倒序。"""
    with _LOCK:
        items = list(reversed(_load_events()))
    if not include_resolved:
        items = [item for item in items if not item.get("resolved")]
    return items[:max(1, limit)]


def resolve_event(event_id: str, note: str = "") -> bool:
    """标记诊断事件为已处理。"""
    event_id = str(event_id or "").strip()
    if not event_id:
        return False

    with _LOCK:
        items = _load_events()
        changed = False
        for item in items:
            if str(item.get("id", "")).strip() != event_id:
                continue
            item["resolved"] = True
            item["resolved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            item["resolved_note"] = str(note or "").strip()
            changed = True
            break
        if changed:
            _save_events(items)
        return changed


def clear_resolved_events() -> int:
    """清理已处理事件，返回删除数量。"""
    with _LOCK:
        items = _load_events()
        kept = [item for item in items if not item.get("resolved")]
        removed = len(items) - len(kept)
        if removed:
            _save_events(kept)
        return removed


def _default_suggestion(category: str, message: str) -> str:
    text = f"{category} {message}".lower()
    if "selector_miss" in text or "selector" in text or "选择器" in text:
        return "检查平台页面是否改版；可在抓取模式设置中重新检测对应 selector，确认候选后再应用。"
    if "webhook" in text or "企业微信" in text:
        return "检查企业微信 webhook 是否可用，以及当前任务是否配置了有效 webhook。"
    if "登录" in text or "captcha" in text or "验证" in text:
        return "检查平台登录态是否失效，必要时重新登录并完成一次人工验证。"
    if "timeout" in text or "超时" in text:
        return "检查网络与页面响应速度，必要时适当提高超时时间或减少同时运行的任务。"
    if "api" in text or "token" in text or "401" in text:
        return "检查 API Key / token 是否过期，必要时重新填写凭证。"
    if "截图" in text:
        return "检查页面结构是否变化，以及截图目录是否可写。"
    return "根据错误消息检查对应配置或运行环境，必要时查看日志定位上下文。"


def _load_events() -> list[dict]:
    diagnostics_path = _diagnostics_path()
    if not diagnostics_path.exists():
        return []
    try:
        with open(diagnostics_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_events(items: list[dict]) -> None:
    diagnostics_path = _diagnostics_path()
    diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(diagnostics_path.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, diagnostics_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _diagnostics_path():
    resolved_default = resolve_app_path("logs/diagnostics.json")
    if DIAGNOSTICS_PATH != resolved_default:
        return DIAGNOSTICS_PATH
    return account_scoped_path("logs/diagnostics.json", fallback=resolved_default)


def get_diagnostics_path():
    return _diagnostics_path()
