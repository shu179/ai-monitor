"""Read-only selector diagnosis orchestration."""

from __future__ import annotations

from typing import Any

from .dom_probe import collect_interactive_candidates, inspect_selector
from .ranker import rank_candidates
from .registry import get_field_intent


def diagnose_selector_field(
    page: Any,
    *,
    platform: str,
    field_name: str,
    current_selector: str = "",
    candidate_limit: int = 8,
) -> dict[str, Any]:
    """Diagnose one selector field without clicking or writing config."""
    intent = get_field_intent(platform, field_name)
    if intent is None:
        return {
            "ok": False,
            "platform": str(platform or "").strip(),
            "field": str(field_name or "").strip(),
            "message": "字段未纳入 selector 诊断白名单",
            "current_status": "unsupported",
            "candidates": [],
        }

    current_status = inspect_selector(page, current_selector)
    raw_candidates = collect_interactive_candidates(page)
    ranked = rank_candidates(raw_candidates, intent, limit=candidate_limit)
    candidates = [
        {
            "selector": str(item.get("selector") or ""),
            "score": float(item.get("score") or 0.0),
            "reason": str(item.get("reason") or ""),
            "verified": False,
            "bbox": ((item.get("candidate") or {}).get("bbox") or {}),
            "text": str(((item.get("candidate") or {}).get("text") or ""))[:120],
            "aria_label": str(((item.get("candidate") or {}).get("ariaLabel") or ""))[:120],
            "test_id": str(((item.get("candidate") or {}).get("testId") or ""))[:120],
            "tag": str(((item.get("candidate") or {}).get("tag") or "")),
        }
        for item in ranked
    ]
    status_label = "healthy" if current_status.get("count", 0) and current_status.get("visible") else "missing"
    if current_status.get("error"):
        status_label = "error"
    return {
        "ok": True,
        "platform": intent.platform,
        "field": intent.field_name,
        "intent": intent.intent_zh,
        "current_selector": str(current_selector or "").strip(),
        "current_status": status_label,
        "current": current_status,
        "candidates": candidates,
    }
