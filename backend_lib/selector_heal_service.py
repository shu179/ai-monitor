"""Conservative selector diagnosis and manual apply service."""

from __future__ import annotations

import base64
from collections.abc import Callable
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from core.app_paths import resolve_app_path
from core.browser_platform_factory import normalize_browser_platform_name
from platforms.api_client import (
    get_platform_api_key,
    get_platform_last_error,
    model_supports_image_input,
    platform_has_configured_access,
    send_platform_chat_messages,
)
from core.selector_heal.repair import diagnose_selector_field
from core.selector_heal.registry import get_field_intent
from core.selector_heal.verifier import verify_selector_candidates


class SelectorHealService:
    """Orchestrates conservative selector diagnostics.

    The first API slice is conservative: it creates a temporary headed
    platform, inspects the requested field, optionally verifies supported
    candidates by clicking in that temporary browser, and always closes the
    platform. It never writes config.
    """

    def __init__(
        self,
        *,
        config_loader: Callable[[], dict[str, Any]],
        platform_factory: Callable[..., Any],
        config_saver: Callable[[dict[str, Any]], Any] | None = None,
        selector_config_writer: Callable[[str, str, str], str] | None = None,
        platform_session_closer: Callable[[str, str], None] | None = None,
        runtime_safety_checker: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._config_loader = config_loader
        self._platform_factory = platform_factory
        self._config_saver = config_saver
        self._selector_config_writer = selector_config_writer
        self._platform_session_closer = platform_session_closer
        self._runtime_safety_checker = runtime_safety_checker

    def diagnose(self, payload: dict[str, Any] | None) -> dict[str, Any]:
        request = payload or {}
        platform = normalize_browser_platform_name(str(request.get("platform") or "").strip())
        raw_fields = request.get("fields")
        fields = [str(item or "").strip() for item in raw_fields] if isinstance(raw_fields, list) else []
        fields = [item for item in fields if item]
        verify = bool(request.get("verify", False))
        vision = bool(request.get("vision", False))
        auto_apply = bool(request.get("auto_apply", False))

        if not platform:
            return {"ok": False, "message": "platform 不能为空", "results": []}
        if vision:
            return {"ok": False, "message": "当前阶段尚未开放视觉兜底，请使用 vision=false", "results": []}
        if not fields:
            fields = ["new_chat_selector"]

        unsupported = [field for field in fields if get_field_intent(platform, field) is None]
        if unsupported:
            return {
                "ok": False,
                "platform": platform,
                "message": f"字段未纳入 selector 诊断白名单: {', '.join(unsupported)}",
                "results": [],
            }

        runtime_safety = self._runtime_safety()
        if not bool(runtime_safety.get("runtime_safe", True)):
            return {
                "ok": False,
                "platform": platform,
                "verify": verify,
                "vision": False,
                "runtime_safe": False,
                "blocking_reason": str(runtime_safety.get("blocking_reason") or "当前有抓取或测试任务运行中"),
                "runtime_safety": runtime_safety,
                "message": str(runtime_safety.get("blocking_reason") or "当前有抓取或测试任务运行中，请先暂停后再诊断 selector"),
                "results": [],
            }

        config = self._config_loader()
        platform_instance = None
        try:
            platform_instance = self._platform_factory(
                platform,
                config=config,
                inspect=True,
                stop_checker=None,
            )
            platform_instance.prefer_headed_runtime = True
            platform_instance.inspect = True
            if getattr(platform_instance, "page", None) is None:
                platform_instance = platform_instance.start()
            page = getattr(platform_instance, "page", None)
            if page is None:
                return {"ok": False, "platform": platform, "message": "诊断浏览器页面未就绪", "results": []}

            results = []
            for field in fields:
                current_selector = str(getattr(platform_instance, field, "") or "").strip()
                diagnosis = diagnose_selector_field(
                    page,
                    platform=platform,
                    field_name=field,
                    current_selector=current_selector,
                )
                diagnosis = self._maybe_enrich_with_selector_agent(
                    platform_instance,
                    diagnosis,
                    config=config,
                )
                if verify:
                    diagnosis = verify_selector_candidates(platform_instance, diagnosis)
                diagnosis = self._annotate_field_policy(diagnosis, platform=platform, field=field)
                if auto_apply and verify:
                    diagnosis = self._auto_apply_verified_diagnosis(diagnosis, platform=platform, field=field)
                results.append(diagnosis)
            return {
                "ok": True,
                "platform": platform,
                "verify": verify,
                "vision": False,
                "auto_apply": auto_apply,
                "runtime_safe": True,
                "results": results,
            }
        except Exception as exc:
            return {
                "ok": False,
                "platform": platform,
                "message": str(exc) or "selector 诊断失败",
                "results": [],
            }
        finally:
            if platform_instance is not None:
                try:
                    platform_instance.close()
                except Exception:
                    pass

    def diagnose_pause_state(self, payload: dict[str, Any] | None) -> dict[str, Any]:
        """Probe an in-generation pause/stop state in a temporary headed browser."""
        request = payload or {}
        platform = normalize_browser_platform_name(str(request.get("platform") or "deepseek").strip())
        if platform != "deepseek":
            return {"ok": False, "platform": platform, "message": "当前仅支持 DeepSeek 暂停态诊断"}

        runtime_safety = self._runtime_safety()
        if not bool(runtime_safety.get("runtime_safe", True)):
            return {
                "ok": False,
                "platform": platform,
                "runtime_safe": False,
                "blocking_reason": str(runtime_safety.get("blocking_reason") or "当前有抓取或测试任务运行中"),
                "runtime_safety": runtime_safety,
                "message": str(runtime_safety.get("blocking_reason") or "当前有抓取或测试任务运行中，请先暂停后再诊断暂停态"),
            }

        prompt = str(
            request.get("prompt")
            or "请用中文详细说明生成式 AI 搜索优化的完整流程，至少分 12 点展开，每一点给出实践建议和风险提示。"
        ).strip()
        timeout_seconds = _bounded_float(request.get("timeout"), default=45.0, minimum=5.0, maximum=120.0)
        interval_seconds = _bounded_float(request.get("interval"), default=0.35, minimum=0.1, maximum=2.0)

        config = self._config_loader()
        platform_instance = None
        samples: list[dict[str, Any]] = []
        submit_error = ""
        try:
            platform_instance = self._platform_factory(
                platform,
                config=config,
                inspect=True,
                stop_checker=None,
            )
            platform_instance.prefer_headed_runtime = True
            platform_instance.inspect = True
            if getattr(platform_instance, "page", None) is None:
                platform_instance = platform_instance.start()
            platform_instance.ensure_logged_in(timeout=20)
            platform_instance.start_new_chat()
            platform_instance.deep_think = False
            platform_instance.type_like_human(prompt)
            try:
                platform_instance.submit_prompt()
            except Exception as exc:
                submit_error = str(exc) or "未确认问题已发送"
                try:
                    platform_instance.page.keyboard.press("Enter")
                except Exception as fallback_exc:
                    submit_error = f"{submit_error}; Enter 兜底失败: {fallback_exc}"

            deadline = time.time() + timeout_seconds
            saw_generating = False
            while time.time() < deadline:
                state = platform_instance._get_generation_signal_state() or {}
                state = dict(state)
                state["ts"] = time.time()
                samples.append(state)
                if bool(state.get("is_generating")):
                    saw_generating = True
                elif saw_generating:
                    break
                time.sleep(interval_seconds)

            summary = _summarize_pause_state_samples(samples)
            verified_selector = str(summary.get("suggested_selector") or "").strip()
            ok = bool(summary.get("saw_pause_state") and verified_selector)
            previous_selector = ""
            saved = False
            save_error = ""
            if ok and self._selector_config_writer is not None:
                try:
                    previous_selector = self._selector_config_writer(platform, "generation_pause_selector", verified_selector)
                    saved = True
                    if self._platform_session_closer is not None:
                        self._platform_session_closer(platform, "selector 配置已更新")
                except Exception as exc:
                    save_error = str(exc) or "自动保存 DeepSeek 暂停态 selector 失败"
            log_path = _write_pause_state_probe_log(
                platform=platform,
                ok=ok,
                prompt=prompt,
                summary=summary,
                samples=samples,
                error=submit_error or None,
            )
            return {
                "ok": ok,
                "platform": platform,
                "runtime_safe": True,
                "field": "generation_pause_selector",
                "prompt": prompt,
                "verified_selector": verified_selector,
                "selector": verified_selector,
                "previous_selector": previous_selector,
                "saved": saved,
                "save_error": save_error,
                "summary": summary,
                "log_path": log_path,
                "submit_error": submit_error,
                "message": "已抓到并保存 DeepSeek 生成中的暂停态 selector"
                if saved
                else (save_error or ("已抓到 DeepSeek 生成中的暂停态表达" if verified_selector else "未抓到可确认的 DeepSeek 暂停态表达")),
            }
        except Exception as exc:
            summary = _summarize_pause_state_samples(samples)
            return {
                "ok": False,
                "platform": platform,
                "field": "generation_pause_state",
                "message": str(exc) or "暂停态诊断失败",
                "summary": summary,
                "log_path": _write_pause_state_probe_log(
                    platform=platform,
                    ok=False,
                    prompt=prompt,
                    summary=summary,
                    samples=samples,
                    error=str(exc) or "暂停态诊断失败",
                ),
            }
        finally:
            if platform_instance is not None:
                try:
                    platform_instance.close()
                except Exception:
                    pass

    def _maybe_enrich_with_selector_agent(
        self,
        platform_instance: Any,
        diagnosis: dict[str, Any],
        *,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        updated = dict(diagnosis or {})
        selector_agent_cfg = self._resolve_selector_agent_config(config)
        if not selector_agent_cfg.get("enabled"):
            updated.setdefault("selector_agent_used", False)
            updated.setdefault("selector_agent_reason", "selector_agent 未启用")
            return updated

        if str(updated.get("current_status") or "").strip() == "healthy":
            updated.setdefault("selector_agent_used", False)
            updated.setdefault("selector_agent_reason", "当前 selector 仍可用，跳过模型辅助")
            return updated

        platform = str(selector_agent_cfg.get("platform") or "").strip()
        model = str(selector_agent_cfg.get("model") or "").strip()
        api_key = str(selector_agent_cfg.get("api_key") or "").strip()
        if not platform or not model:
            updated["selector_agent_used"] = False
            updated["selector_agent_error"] = "selector_agent 未配置平台或模型"
            return updated
        if not selector_agent_cfg.get("available", False):
            updated["selector_agent_used"] = False
            updated["selector_agent_error"] = str(selector_agent_cfg.get("error") or "selector_agent 不可用")
            return updated

        page = getattr(platform_instance, "page", None)
        if page is None:
            updated["selector_agent_used"] = False
            updated["selector_agent_error"] = "诊断浏览器页面未就绪"
            return updated

        screenshot_b64 = ""
        screenshot_mime = ""
        if bool(selector_agent_cfg.get("supports_image_input", False)):
            screenshot_b64, screenshot_mime = self._capture_selector_agent_screenshot(page)

        candidates = list(updated.get("candidates") or [])
        prompt_payload = {
            "platform": str(updated.get("platform") or "").strip(),
            "field": str(updated.get("field") or "").strip(),
            "intent": str(updated.get("intent") or "").strip(),
            "current_selector": str(updated.get("current_selector") or "").strip(),
            "current_status": str(updated.get("current_status") or "").strip(),
            "page_url": self._safe_page_value(page, "url"),
            "page_title": self._safe_page_title(page),
            "candidates": candidates[:8],
            "instructions": [
                "优先从候选里挑最稳定的 selector。",
                "如果候选都不合适，可以给出一个新的更稳定 selector。",
                "只返回 JSON，不要输出多余解释。",
            ],
        }
        target_label = str(updated.get("intent") or updated.get("field") or "目标").strip() or "目标"
        system_prompt = (
            "你是 selector 诊断助手。你的任务是基于页面截图和 DOM 候选，"
            f"找出最可能稳定的 {target_label} selector。"
            "请只输出 JSON，格式必须是 "
            "{\"selected_selector\":\"...\",\"selected_index\":0,\"confidence\":0.0,"
            "\"reason\":\"...\",\"candidate_order\":[\"...\"],\"needs_verification\":true}. "
            "selected_selector 可以是候选里的 selector，也可以是你根据页面证据推断出的新 selector。"
            "不要编造无法验证的内容。"
        )
        user_text = json.dumps(prompt_payload, ensure_ascii=False, indent=2)
        user_content: Any = user_text
        if screenshot_b64:
            user_content = [
                {"type": "text", "text": user_text},
                {
                    "type": "image_url",
                    "image_url": {"url": f"{screenshot_mime};base64,{screenshot_b64}"},
                },
            ]

        try:
            content = send_platform_chat_messages(
                platform,
                api_key,
                model,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            )
            if content is None:
                updated["selector_agent_used"] = False
                updated["selector_agent_error"] = get_platform_last_error(platform) or "selector_agent 调用失败"
                return updated
            agent_result = _extract_json_object(content)
        except Exception as exc:
            updated["selector_agent_used"] = False
            updated["selector_agent_error"] = str(exc) or "selector_agent 调用失败"
            return updated

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

        updated["selector_agent_used"] = True
        updated["selector_agent_platform"] = platform
        updated["selector_agent_model"] = model
        updated["selector_agent_confidence"] = confidence
        updated["selector_agent_reason"] = reason
        updated["selector_agent_error"] = ""
        updated["selector_agent_image_used"] = bool(screenshot_b64)
        updated["selector_agent_image_supported"] = bool(selector_agent_cfg.get("supports_image_input", False))
        updated["selector_agent_raw"] = agent_result

        if not selected_selector and 0 <= selected_index < len(candidates):
            selected_selector = str((candidates[selected_index] or {}).get("selector") or "").strip()

        if candidate_order:
            ordered_candidates = self._reorder_candidates_by_selector_order(candidates, candidate_order)
        else:
            ordered_candidates = list(candidates)

        if selected_selector:
            ordered_candidates = self._promote_selector_candidate(
                ordered_candidates,
                selected_selector,
                reason=reason or "selector_agent 建议",
                confidence=max(confidence, 0.65),
            )
            updated["selector_agent_selected_selector"] = selected_selector

        if ordered_candidates:
            updated["candidates"] = ordered_candidates
        return updated

    def _resolve_selector_agent_config(self, config: dict[str, Any]) -> dict[str, Any]:
        selector_agent_cfg = dict(config.get("selector_agent", {}) or {})
        if not bool(selector_agent_cfg.get("enabled", False)):
            return {"enabled": False}

        assistant_cfg = dict(config.get("ai_assistant", {}) or {})
        platform = str(selector_agent_cfg.get("platform") or assistant_cfg.get("platform") or "").strip()
        model = str(selector_agent_cfg.get("model") or assistant_cfg.get("model") or "").strip()
        if not platform or not model:
            return {"enabled": True, "available": False, "error": "selector_agent 未配置平台或模型"}
        if not platform_has_configured_access(config, platform):
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
            "api_key": get_platform_api_key(config, platform),
            "supports_image_input": model_supports_image_input(platform, model),
        }

    @staticmethod
    def _safe_page_value(page: Any, attr: str) -> str:
        try:
            return str(getattr(page, attr, "") or "").strip()
        except Exception:
            return ""

    def _safe_page_title(self, page: Any) -> str:
        try:
            title = page.title()
        except Exception:
            title = ""
        return str(title or "").strip()

    @staticmethod
    def _capture_selector_agent_screenshot(page: Any) -> tuple[str, str]:
        try:
            image_bytes = page.screenshot(type="jpeg", quality=82, full_page=False)
        except Exception:
            return "", ""
        if not image_bytes:
            return "", ""
        return base64.b64encode(image_bytes).decode("utf-8"), "data:image/jpeg"

    @staticmethod
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

    @staticmethod
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
        promoted: list[dict[str, Any]] = []
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
            match["selector_agent_confidence"] = confidence
            promoted.append(match)
            for index, candidate in enumerate(candidates):
                if index == match_index:
                    continue
                promoted.append(candidate)
            return promoted
        promoted.append(
            {
                "selector": selector,
                "score": max(float(confidence or 0.0), 0.65),
                "reason": reason or "selector_agent 建议",
                "verified": False,
                "selector_agent_selected": True,
                "selector_agent_confidence": confidence,
            }
        )
        promoted.extend(candidates)
        return promoted

    def apply(self, payload: dict[str, Any] | None) -> dict[str, Any]:
        """Apply a selector only after a fresh headed-browser verification."""
        request = payload or {}
        platform = normalize_browser_platform_name(str(request.get("platform") or "").strip())
        field = str(request.get("field") or "").strip()
        candidate_selector = str(request.get("candidate_selector") or request.get("selector") or "").strip()

        if not platform:
            return {"ok": False, "message": "platform 不能为空"}
        if not field:
            return {"ok": False, "platform": platform, "message": "field 不能为空"}
        if not candidate_selector:
            return {"ok": False, "platform": platform, "field": field, "message": "candidate_selector 不能为空"}
        if self._config_saver is None and self._selector_config_writer is None:
            return {"ok": False, "platform": platform, "field": field, "message": "当前运行环境未配置写入器，无法应用 selector"}

        intent = get_field_intent(platform, field)
        if intent is None:
            return {
                "ok": False,
                "platform": platform,
                "field": field,
                "message": "字段未纳入 selector 诊断白名单",
            }
        apply_allowed = self._field_apply_allowed(intent)
        if not apply_allowed:
            return {
                "ok": False,
                "platform": platform,
                "field": field,
                "field_risk_level": intent.risk_level,
                "apply_allowed": False,
                "message": "当前字段不允许通过 selector-heal 应用",
            }

        runtime_safety = self._runtime_safety()
        if not bool(runtime_safety.get("runtime_safe", True)):
            return {
                "ok": False,
                "platform": platform,
                "field": field,
                "field_risk_level": intent.risk_level,
                "apply_allowed": True,
                "runtime_safe": False,
                "blocking_reason": str(runtime_safety.get("blocking_reason") or "当前有抓取或测试任务运行中"),
                "runtime_safety": runtime_safety,
                "message": str(runtime_safety.get("blocking_reason") or "当前有抓取或测试任务运行中，请先暂停后再应用 selector"),
            }

        verification = self._verify_candidate_for_apply(
            platform=platform,
            field=field,
            candidate_selector=candidate_selector,
        )
        if not verification.get("ok"):
            return verification

        previous_selector = self._write_selector_config(platform, field, candidate_selector)

        if self._platform_session_closer is not None:
            try:
                self._platform_session_closer(platform, "selector 配置已更新")
            except Exception:
                pass

        return {
            "ok": True,
            "platform": platform,
            "field": field,
            "selector": candidate_selector,
            "previous_selector": previous_selector,
            "field_risk_level": intent.risk_level,
            "apply_allowed": True,
            "runtime_safe": True,
            "message": "selector 已验证并应用",
            "verification": verification.get("verification"),
        }

    def _runtime_safety(self) -> dict[str, Any]:
        if self._runtime_safety_checker is None:
            return {"runtime_safe": True, "blocking_reason": "", "checks": {}}
        try:
            result = self._runtime_safety_checker() or {}
        except Exception as exc:
            return {
                "runtime_safe": False,
                "blocking_reason": f"运行态安全检查失败: {exc}",
                "checks": {},
            }
        if not isinstance(result, dict):
            return {"runtime_safe": False, "blocking_reason": "运行态安全检查返回异常", "checks": {}}
        result.setdefault("runtime_safe", True)
        result.setdefault("blocking_reason", "")
        result.setdefault("checks", {})
        return result

    def _write_selector_config(self, platform: str, field: str, candidate_selector: str) -> str:
        if self._selector_config_writer is not None:
            return str(self._selector_config_writer(platform, field, candidate_selector) or "")

        config = self._config_loader()
        browser_cfg = config.get("browser_automation")
        if not isinstance(browser_cfg, dict):
            browser_cfg = {}
            config["browser_automation"] = browser_cfg
        platform_cfg = browser_cfg.get(platform)
        if not isinstance(platform_cfg, dict):
            platform_cfg = {}
            browser_cfg[platform] = platform_cfg

        previous_selector = str(platform_cfg.get(field) or "").strip()
        platform_cfg[field] = candidate_selector
        if self._config_saver is None:
            raise RuntimeError("当前运行环境未配置写入器，无法应用 selector")
        self._config_saver(config)
        return previous_selector

    @staticmethod
    def _field_apply_allowed(intent: Any) -> bool:
        return bool(
            getattr(intent, "risk_level", "") == "low"
            and getattr(intent, "success_check", "") in {"click_then_input_empty", "click_then_state_active"}
        )

    def _annotate_field_policy(self, diagnosis: dict[str, Any], *, platform: str, field: str) -> dict[str, Any]:
        intent = get_field_intent(platform, field)
        if intent is None:
            diagnosis.setdefault("field_risk_level", "unsupported")
            diagnosis.setdefault("apply_allowed", False)
            return diagnosis
        diagnosis["field_risk_level"] = intent.risk_level
        diagnosis["apply_allowed"] = self._field_apply_allowed(intent)
        return diagnosis

    def _auto_apply_verified_diagnosis(self, diagnosis: dict[str, Any], *, platform: str, field: str) -> dict[str, Any]:
        updated = dict(diagnosis or {})
        if not updated.get("apply_allowed"):
            updated.setdefault("saved", False)
            return updated
        verified_selector = str(updated.get("verified_selector") or "").strip()
        verify_status = str(updated.get("verify_status") or "").strip()
        if verify_status != "passed" or not verified_selector:
            updated.setdefault("saved", False)
            return updated
        try:
            previous_selector = self._write_selector_config(platform, field, verified_selector)
            updated["selector"] = verified_selector
            updated["previous_selector"] = previous_selector
            updated["current_selector"] = verified_selector
            updated["current_status"] = "healthy"
            updated["saved"] = True
            updated["message"] = "selector 已验证并自动保存"
            if self._platform_session_closer is not None:
                self._platform_session_closer(platform, "selector 配置已更新")
        except Exception as exc:
            updated["saved"] = False
            updated["save_error"] = str(exc) or "自动保存 selector 失败"
            updated["message"] = updated["save_error"]
        return updated

    def _verify_candidate_for_apply(
        self,
        *,
        platform: str,
        field: str,
        candidate_selector: str,
    ) -> dict[str, Any]:
        """Re-run diagnosis and require the exact candidate to verify."""
        result = self.diagnose({"platform": platform, "fields": [field], "verify": True, "vision": False})
        if not result.get("ok"):
            return {
                "ok": False,
                "platform": platform,
                "field": field,
                "message": result.get("message") or "应用前验证失败",
                "diagnosis": result,
            }

        diagnosis = ((result.get("results") or [{}])[0] or {}) if isinstance(result.get("results"), list) else {}
        verified_candidates = [
            item
            for item in (diagnosis.get("candidates") or [])
            if isinstance(item, dict)
            and str(item.get("selector") or "").strip() == candidate_selector
            and bool(item.get("verified"))
        ]
        if not verified_candidates:
            return {
                "ok": False,
                "platform": platform,
                "field": field,
                "selector": candidate_selector,
                "message": "候选 selector 未通过应用前验证，未写入配置",
                "diagnosis": diagnosis,
            }

        return {
            "ok": True,
            "platform": platform,
            "field": field,
            "selector": candidate_selector,
            "verification": {
                "verify_status": diagnosis.get("verify_status"),
                "verified_selector": diagnosis.get("verified_selector"),
                "candidate": verified_candidates[0],
            },
        }


def _bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except Exception:
        number = default
    return max(minimum, min(maximum, number))


def _safe_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


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
    try:
        parsed = json.loads(snippet)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    end = snippet.rfind("}")
    if end == -1:
        raise ValueError("AI 返回 JSON 解析失败")
    parsed = json.loads(snippet[: end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("AI 返回 JSON 解析失败")
    return parsed


def _summarize_pause_state_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    generating_samples = [sample for sample in samples if bool(sample.get("is_generating"))]
    selector_counts: dict[str, int] = {}
    path_prefix_counts: dict[str, int] = {}

    def add_count(bucket: dict[str, int], value: Any) -> None:
        text = str(value or "").strip()
        if not text:
            return
        bucket[text] = bucket.get(text, 0) + 1

    for sample in generating_samples:
        for hint in sample.get("stop_selector_hints") or []:
            add_count(selector_counts, hint)
        for path in sample.get("stop_paths") or []:
            if not isinstance(path, dict):
                continue
            add_count(path_prefix_counts, path.get("dPrefix"))
            for hint in path.get("selectorHints") or []:
                add_count(selector_counts, hint)
        for control in sample.get("stop_controls") or []:
            if not isinstance(control, dict):
                continue
            for hint in control.get("selectorHints") or []:
                add_count(selector_counts, hint)

    top_selectors = sorted(selector_counts.items(), key=lambda item: item[1], reverse=True)
    top_path_prefixes = sorted(path_prefix_counts.items(), key=lambda item: item[1], reverse=True)
    suggested_selector = ""
    for selector, _ in top_selectors:
        if ":has(path" in selector or selector.startswith("path[") or "aria-label" in selector or selector.startswith("text="):
            suggested_selector = selector
            break
    if suggested_selector.startswith("path["):
        path_selector = suggested_selector
        for selector, _ in top_selectors:
            if ":has(" in selector and path_selector in selector:
                suggested_selector = selector
                break

    return {
        "sample_count": len(samples),
        "generating_sample_count": len(generating_samples),
        "saw_pause_state": bool(generating_samples),
        "suggested_selector": suggested_selector,
        "top_selectors": top_selectors[:10],
        "top_path_prefixes": top_path_prefixes[:5],
        "first_generating_sample": generating_samples[0] if generating_samples else None,
        "last_sample": samples[-1] if samples else None,
    }


def _write_pause_state_probe_log(
    *,
    platform: str,
    ok: bool,
    prompt: str,
    summary: dict[str, Any],
    samples: list[dict[str, Any]],
    error: str | None = None,
) -> str:
    try:
        output_dir = resolve_app_path("logs/selector_heal")
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = output_dir / f"{platform}_pause_state_{timestamp}.json"
        payload = {
            "ok": bool(ok),
            "platform": platform,
            "prompt": prompt,
            "error": error or "",
            "summary": summary,
            "samples": samples,
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(output_path)
    except Exception:
        return ""
