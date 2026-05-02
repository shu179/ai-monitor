"""Runtime selector self-healing for browser scraping flows."""

from __future__ import annotations

import base64
import json
from typing import Any

from core.browser_platform_factory import normalize_browser_platform_name
from core.config_watcher import load_config, save_config
from core.selector_cache import set_learned_selector
from platforms.api_client import (
    get_platform_api_key,
    get_platform_last_error,
    model_supports_image_input,
    platform_has_configured_access,
    send_platform_chat_messages,
)

from .repair import diagnose_selector_field
from .registry import get_field_intent
from .verifier import verify_selector_candidates


def attempt_runtime_selector_heal(
    platform_instance: Any,
    field_name: str,
    *,
    label: str = "",
    config: dict[str, Any] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Let the configured selector agent fix one selector in the live browser.

    This is intentionally a fallback for the scraping runtime, not the normal
    path. It runs only when platform code calls it after its deterministic
    selector flow failed.
    """
    platform = normalize_browser_platform_name(str(getattr(platform_instance, "name", "") or "").strip())
    field = str(field_name or "").strip()
    if not platform or not field:
        return {"ok": False, "selector_agent_used": False, "message": "缺少平台或字段"}

    intent = get_field_intent(platform, field)
    if intent is None:
        return {
            "ok": False,
            "platform": platform,
            "field": field,
            "selector_agent_used": False,
            "message": "字段未纳入 selector 自愈白名单",
        }
    if str(getattr(intent, "risk_level", "") or "").strip() != "low" or str(
        getattr(intent, "success_check", "") or ""
    ).strip() not in {"click_then_input_empty", "click_then_state_active"}:
        return {
            "ok": False,
            "platform": platform,
            "field": field,
            "selector_agent_used": False,
            "skipped": True,
            "message": "该字段暂不允许运行时自动写入",
        }

    page = getattr(platform_instance, "page", None)
    if page is None:
        return {
            "ok": False,
            "platform": platform,
            "field": field,
            "selector_agent_used": False,
            "message": "浏览器页面未就绪",
        }

    current_selector = str(getattr(platform_instance, field, "") or "").strip()
    runtime_config = config if isinstance(config, dict) else load_config()
    agent_cfg = _resolve_selector_agent_config(runtime_config)
    if not bool(agent_cfg.get("enabled", False)):
        return {
            "ok": False,
            "platform": platform,
            "field": field,
            "selector_agent_used": False,
            "skipped": True,
            "message": "selector_agent 未启用",
        }
    if not bool(agent_cfg.get("available", False)):
        return {
            "ok": False,
            "platform": platform,
            "field": field,
            "selector_agent_used": False,
            "message": str(agent_cfg.get("error") or "selector_agent 不可用"),
        }

    diagnosis = diagnose_selector_field(
        page,
        platform=platform,
        field_name=field,
        current_selector=current_selector,
    )
    if str(diagnosis.get("current_status") or "").strip() == "healthy":
        return {
            "ok": False,
            "platform": platform,
            "field": field,
            "selector_agent_used": False,
            "skipped": True,
            "message": "当前 selector 仍可用，未触发自愈",
            "diagnosis": diagnosis,
        }

    screenshot_b64 = ""
    screenshot_mime = ""
    if bool(agent_cfg.get("supports_image_input", False)):
        screenshot_b64, screenshot_mime = _capture_selector_agent_screenshot(page)

    candidates = list(diagnosis.get("candidates") or [])
    target_label = str(label or diagnosis.get("intent") or field).strip() or field
    prompt_payload = {
        "runtime": "browser_scraping",
        "platform": platform,
        "field": field,
        "intent": target_label,
        "current_selector": current_selector,
        "current_status": str(diagnosis.get("current_status") or "").strip(),
        "page_url": _safe_page_value(page, "url"),
        "page_title": _safe_page_title(page),
        "candidates": candidates[:8],
        "goal": (
            "从当前页面中找到能够点击并完成该流程动作的稳定 selector。"
            "程序会真实点击并验证，通过后才保存配置。"
        ),
    }
    system_prompt = (
        "你是浏览器抓取流程里的 selector 自愈代理。"
        f"当前要修复的是 {platform} 的 {target_label}。"
        "请根据页面截图和 DOM 候选判断程序应该点击哪里。"
        "只返回 JSON，格式必须是 "
        "{\"selected_selector\":\"...\",\"selected_index\":0,\"confidence\":0.0,"
        "\"reason\":\"...\",\"candidate_order\":[\"...\"],\"needs_verification\":true}。"
        "优先选择候选中的稳定 selector；如果候选都不合适，可以给出新的 CSS/XPath/Playwright selector。"
        "不要输出解释性正文。"
    )
    user_text = json.dumps(prompt_payload, ensure_ascii=False, indent=2)
    user_content: Any = user_text
    if screenshot_b64:
        user_content = [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": f"{screenshot_mime};base64,{screenshot_b64}"}},
        ]

    agent_platform = str(agent_cfg.get("platform") or "").strip()
    agent_model = str(agent_cfg.get("model") or "").strip()
    content = send_platform_chat_messages(
        agent_platform,
        str(agent_cfg.get("api_key") or ""),
        agent_model,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    if content is None:
        return {
            "ok": False,
            "platform": platform,
            "field": field,
            "selector_agent_used": True,
            "selector_agent_platform": agent_platform,
            "selector_agent_model": agent_model,
            "message": get_platform_last_error(agent_platform) or "selector_agent 调用失败",
            "diagnosis": diagnosis,
        }

    try:
        agent_result = _extract_json_object(content)
    except Exception as exc:
        return {
            "ok": False,
            "platform": platform,
            "field": field,
            "selector_agent_used": True,
            "selector_agent_platform": agent_platform,
            "selector_agent_model": agent_model,
            "message": str(exc) or "selector_agent 返回无法解析",
            "diagnosis": diagnosis,
        }

    selected_selector = str(
        agent_result.get("selected_selector")
        or agent_result.get("selector")
        or ""
    ).strip()
    selected_index = _safe_int(agent_result.get("selected_index"), default=-1)
    confidence = _safe_float(agent_result.get("confidence"), default=0.0)
    reason = str(agent_result.get("reason") or agent_result.get("summary") or "").strip()
    candidate_order = [
        str(item or "").strip()
        for item in (agent_result.get("candidate_order") or [])
        if str(item or "").strip()
    ]
    if not selected_selector and 0 <= selected_index < len(candidates):
        selected_selector = str((candidates[selected_index] or {}).get("selector") or "").strip()

    ordered_candidates = _reorder_candidates_by_selector_order(candidates, candidate_order) if candidate_order else list(candidates)
    if selected_selector:
        ordered_candidates = _promote_selector_candidate(
            ordered_candidates,
            selected_selector,
            reason=reason or "selector_agent 建议",
            confidence=max(confidence, 0.65),
        )
    diagnosis["candidates"] = ordered_candidates
    diagnosis["selector_agent_used"] = True
    diagnosis["selector_agent_platform"] = agent_platform
    diagnosis["selector_agent_model"] = agent_model
    diagnosis["selector_agent_confidence"] = confidence
    diagnosis["selector_agent_reason"] = reason
    diagnosis["selector_agent_selected_selector"] = selected_selector
    diagnosis["selector_agent_image_used"] = bool(screenshot_b64)
    diagnosis["selector_agent_image_supported"] = bool(agent_cfg.get("supports_image_input", False))

    verified = verify_selector_candidates(platform_instance, diagnosis)
    verified_selector = str(verified.get("verified_selector") or "").strip()
    if not verified_selector:
        return {
            "ok": False,
            "platform": platform,
            "field": field,
            "selector_agent_used": True,
            "selector_agent_platform": agent_platform,
            "selector_agent_model": agent_model,
            "selector_agent_confidence": confidence,
            "selector_agent_reason": reason,
            "selector_agent_selected_selector": selected_selector,
            "message": str(verified.get("verify_reason") or "模型建议未通过真实点击验证"),
            "diagnosis": verified,
        }

    saved = False
    save_error = ""
    previous_selector = current_selector
    if persist:
        try:
            next_config = load_config()
            browser_cfg = next_config.get("browser_automation")
            if not isinstance(browser_cfg, dict):
                browser_cfg = {}
                next_config["browser_automation"] = browser_cfg
            platform_cfg = browser_cfg.get(platform)
            if not isinstance(platform_cfg, dict):
                platform_cfg = {}
                browser_cfg[platform] = platform_cfg
            previous_selector = str(platform_cfg.get(field) or current_selector or "").strip()
            platform_cfg[field] = verified_selector
            save_config(next_config)
            try:
                set_learned_selector(
                    getattr(platform_instance, "user_data_dir", "") or "",
                    field,
                    verified_selector,
                    source="selector_agent",
                    note=f"{platform}:{target_label}",
                )
            except Exception:
                pass
            saved = True
        except Exception as exc:
            save_error = str(exc) or "保存 selector 配置失败"

    try:
        setattr(platform_instance, field, verified_selector)
    except Exception:
        pass

    return {
        "ok": True,
        "platform": platform,
        "field": field,
        "selector": verified_selector,
        "previous_selector": previous_selector,
        "saved": saved,
        "save_error": save_error,
        "selector_agent_used": True,
        "selector_agent_platform": agent_platform,
        "selector_agent_model": agent_model,
        "selector_agent_confidence": confidence,
        "selector_agent_reason": reason,
        "selector_agent_selected_selector": selected_selector,
        "selector_agent_image_used": bool(screenshot_b64),
        "selector_agent_image_supported": bool(agent_cfg.get("supports_image_input", False)),
        "message": "selector_agent 已真实点击验证并保存 selector" if saved else "selector_agent 已真实点击验证 selector",
        "diagnosis": verified,
    }


def _resolve_selector_agent_config(config: dict[str, Any]) -> dict[str, Any]:
    selector_agent_cfg = dict((config or {}).get("selector_agent", {}) or {})
    if not bool(selector_agent_cfg.get("enabled", False)):
        return {"enabled": False, "available": False}

    assistant_cfg = dict((config or {}).get("ai_assistant", {}) or {})
    platform = str(selector_agent_cfg.get("platform") or assistant_cfg.get("platform") or "").strip()
    model = str(selector_agent_cfg.get("model") or assistant_cfg.get("model") or "").strip()
    if not platform or not model:
        return {"enabled": True, "available": False, "error": "selector_agent 未配置平台或模型"}
    if not platform_has_configured_access(config or {}, platform):
        return {
            "enabled": True,
            "available": False,
            "platform": platform,
            "model": model,
            "error": f"{platform} 未配置可用 API Key",
        }
    return {
        "enabled": True,
        "available": True,
        "platform": platform,
        "model": model,
        "api_key": get_platform_api_key(config or {}, platform),
        "supports_image_input": model_supports_image_input(platform, model),
    }


def _capture_selector_agent_screenshot(page: Any) -> tuple[str, str]:
    try:
        image_bytes = page.screenshot(type="jpeg", quality=82, full_page=False)
    except Exception:
        return "", ""
    if not image_bytes:
        return "", ""
    return base64.b64encode(image_bytes).decode("utf-8"), "data:image/jpeg"


def _safe_page_value(page: Any, attr: str) -> str:
    try:
        return str(getattr(page, attr, "") or "").strip()
    except Exception:
        return ""


def _safe_page_title(page: Any) -> str:
    try:
        return str(page.title() or "").strip()
    except Exception:
        return ""


def _reorder_candidates_by_selector_order(
    candidates: list[dict[str, Any]],
    selector_order: list[str],
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    normalized_order = [str(item or "").strip() for item in selector_order if str(item or "").strip()]
    if not normalized_order:
        return list(candidates)
    ordered: list[dict[str, Any]] = []
    remaining = list(candidates)
    for selector in normalized_order:
        for candidate in list(remaining):
            if str(candidate.get("selector") or "").strip() != selector:
                continue
            ordered.append(candidate)
            remaining.remove(candidate)
            break
    ordered.extend(remaining)
    return ordered


def _promote_selector_candidate(
    candidates: list[dict[str, Any]],
    selector: str,
    *,
    reason: str,
    confidence: float,
) -> list[dict[str, Any]]:
    selector = str(selector or "").strip()
    if not selector:
        return list(candidates)
    match_index = -1
    for index, candidate in enumerate(candidates):
        if str(candidate.get("selector") or "").strip() == selector:
            match_index = index
            break
    if match_index >= 0:
        match = dict(candidates[match_index])
        match["score"] = max(float(match.get("score") or 0.0), float(confidence or 0.0), 0.65)
        match["reason"] = reason or str(match.get("reason") or "")
        match["selector_agent_selected"] = True
        promoted = [match]
        promoted.extend(candidate for index, candidate in enumerate(candidates) if index != match_index)
        return promoted
    return [
        {
            "selector": selector,
            "score": max(float(confidence or 0.0), 0.65),
            "reason": reason or "selector_agent 建议",
            "verified": False,
            "selector_agent_selected": True,
        },
        *candidates,
    ]


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("AI 返回为空")
    candidates = [raw]
    if raw.startswith("```"):
        lines = raw.splitlines()
        if len(lines) >= 3:
            candidates.append("\n".join(lines[1:-1]).strip())
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    start = raw.find("{")
    if start == -1:
        raise ValueError("AI 返回里没有 JSON")
    snippet = raw[start:]
    end = snippet.rfind("}")
    if end == -1:
        raise ValueError("AI 返回 JSON 解析失败")
    parsed = json.loads(snippet[: end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("AI 返回 JSON 解析失败")
    return parsed


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _safe_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)
