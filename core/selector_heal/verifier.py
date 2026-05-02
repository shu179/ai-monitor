"""Behavior verification for conservative selector diagnosis."""

from __future__ import annotations

import time
from typing import Any

from .registry import get_field_intent


def verify_selector_candidates(
    platform_instance: Any,
    diagnosis: dict[str, Any],
    *,
    max_candidates: int = 3,
) -> dict[str, Any]:
    """Verify ranked candidates by performing a bounded click check.

    The initial implementation is deliberately narrow: only DeepSeek's new
    chat selector is eligible, and verification stops after the first passing
    candidate. The function mutates and returns ``diagnosis`` so API callers
    keep the original read-only diagnosis shape with verification annotations.
    """
    platform = str(diagnosis.get("platform") or "").strip().lower()
    field_name = str(diagnosis.get("field") or "").strip()
    candidates = diagnosis.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        diagnosis["verify_status"] = "skipped"
        diagnosis["verify_reason"] = "没有可验证的候选 selector"
        return diagnosis

    if diagnosis.get("current_status") == "healthy":
        diagnosis["verify_status"] = "skipped"
        diagnosis["verify_reason"] = "当前 selector 健康，未执行候选点击验证"
        for candidate in candidates:
            candidate.setdefault("verified", False)
            candidate.setdefault("verify_reason", "当前 selector 健康，未验证候选")
        return diagnosis

    intent = get_field_intent(platform, field_name)
    if intent is None:
        diagnosis["verify_status"] = "unsupported"
        diagnosis["verify_reason"] = "字段未纳入 selector 诊断白名单"
        for candidate in candidates:
            candidate.setdefault("verified", False)
            candidate.setdefault("verify_reason", diagnosis["verify_reason"])
        return diagnosis

    if intent.success_check not in {"click_then_input_empty", "click_then_state_active"}:
        diagnosis["verify_status"] = "unsupported"
        diagnosis["verify_reason"] = f"暂不支持验证类型: {intent.success_check}"
        for candidate in candidates:
            candidate.setdefault("verified", False)
            candidate.setdefault("verify_reason", diagnosis["verify_reason"])
        return diagnosis

    limit = max(1, min(3, int(max_candidates or 3)))
    checked = 0
    for candidate in candidates[:limit]:
        if _candidate_score(candidate) < 0.30:
            candidate.setdefault("verified", False)
            candidate.setdefault("verify_reason", "候选语义置信度过低，跳过点击验证")
            continue
        checked += 1
        result = _verify_click_then_state_active(platform_instance, candidate, intent.success_check_params)
        candidate["verified"] = bool(result.get("verified"))
        candidate["verify_reason"] = str(result.get("reason") or "")
        if result.get("error"):
            candidate["verify_error"] = str(result.get("error") or "")
        if candidate["verified"]:
            diagnosis["verify_status"] = "passed"
            diagnosis["verified_selector"] = str(candidate.get("selector") or "")
            for remaining in candidates[checked:]:
                remaining.setdefault("verified", False)
                remaining.setdefault("verify_reason", "已有候选通过验证，未继续点击")
            return diagnosis

    diagnosis["verify_status"] = "failed"
    if checked <= 0:
        diagnosis["verify_reason"] = "没有达到语义置信度阈值的候选可验证"
    else:
        diagnosis["verify_reason"] = f"前 {checked} 个高置信候选均未通过行为验证"
    for candidate in candidates[checked:]:
        candidate.setdefault("verified", False)
        candidate.setdefault("verify_reason", "未进入验证范围")
    return diagnosis


def _candidate_score(candidate: dict[str, Any]) -> float:
    try:
        return float(candidate.get("score") or 0.0)
    except Exception:
        return 0.0


def _verify_click_then_state_active(
    platform_instance: Any,
    candidate: dict[str, Any],
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    selector = str(candidate.get("selector") or "").strip()
    if not selector:
        return {"verified": False, "reason": "候选 selector 为空"}

    page = getattr(platform_instance, "page", None)
    if page is None:
        return {"verified": False, "reason": "诊断浏览器页面未就绪"}

    input_selector = str((params or {}).get("input_selector") or getattr(platform_instance, "input_selector", "") or "").strip()
    if not input_selector:
        return {"verified": False, "reason": "缺少 input selector，无法确认新对话状态"}

    try:
        before = _call_platform_method(platform_instance, "_conversation_snapshot", default={})
        locator = page.locator(selector).first
        try:
            if locator.count() <= 0:
                return {"verified": False, "reason": "候选 selector 未命中元素"}
        except Exception as exc:
            return {"verified": False, "reason": "候选 selector 无法定位", "error": str(exc)}

        _wait_for_locator(platform_instance, locator)
        try:
            locator.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        _click_locator(platform_instance, locator)
        _sleep(platform_instance, 0.8)
        _wait_for_input(platform_instance, input_selector)

        wait_ready = getattr(platform_instance, "_wait_until_new_chat_ready", None)
        if callable(wait_ready):
            ready = bool(wait_ready(before, timeout=6.0))
        else:
            current = _call_platform_method(platform_instance, "_conversation_snapshot", default={})
            ready = _new_chat_transition_ready(before, current)
        if ready:
            return {"verified": True, "reason": "点击后输入框就绪，并确认进入新会话状态"}
        return {"verified": False, "reason": "已点击候选，但未确认进入新会话状态"}
    except Exception as exc:
        return {"verified": False, "reason": "行为验证执行失败", "error": str(exc)}


def _call_platform_method(platform_instance: Any, name: str, *, default: Any) -> Any:
    method = getattr(platform_instance, name, None)
    if not callable(method):
        return default
    try:
        return method()
    except Exception:
        return default


def _wait_for_locator(platform_instance: Any, locator: Any) -> None:
    method = getattr(platform_instance, "_wait_for_locator", None)
    if callable(method):
        method(locator, timeout_ms=2500)
        return
    locator.wait_for(timeout=2500)


def _click_locator(platform_instance: Any, locator: Any) -> None:
    method = getattr(platform_instance, "_click_locator", None)
    if callable(method):
        method(locator, timeout_ms=2500)
        return
    locator.click(timeout=2500)


def _wait_for_input(platform_instance: Any, input_selector: str) -> None:
    method = getattr(platform_instance, "_wait_for_page_selector", None)
    if callable(method):
        method(input_selector, timeout_ms=10000)
        return
    page = getattr(platform_instance, "page", None)
    if page is None:
        raise RuntimeError("诊断浏览器页面未就绪")
    page.wait_for_selector(input_selector, timeout=10000)


def _sleep(platform_instance: Any, seconds: float) -> None:
    method = getattr(platform_instance, "_cooperative_sleep", None)
    if callable(method):
        method(seconds)
        return
    time.sleep(max(0.0, float(seconds or 0.0)))


def _new_chat_transition_ready(before: dict[str, Any], current: dict[str, Any]) -> bool:
    baseline_count = int((before or {}).get("answerCount") or 0)
    baseline_length = int((before or {}).get("answerLength") or 0)
    baseline_path = str((before or {}).get("path") or "").strip()
    baseline_href = str((before or {}).get("href") or "").strip()

    current_count = int((current or {}).get("answerCount") or 0)
    current_length = int((current or {}).get("answerLength") or 0)
    current_path = str((current or {}).get("path") or "").strip()
    current_href = str((current or {}).get("href") or "").strip()

    if baseline_path and current_path and current_path != baseline_path and current_count == 0:
        return True
    if baseline_href and current_href and current_href != baseline_href and current_count == 0:
        return True
    if baseline_count <= 0 and baseline_length <= 0:
        return current_count == 0 and current_length <= 0
    if current_count == 0:
        return True
    if baseline_count > 0 and current_count < baseline_count:
        return True
    if baseline_length > 0 and current_length <= min(20, max(0, baseline_length // 5)):
        return True
    return False
