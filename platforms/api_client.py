"""
各 AI 平台官方 API 调用。

设计目标：
1. 平台模型调用尽量贴近各平台网页端的“联网搜索”表现。
2. 原生联网搜索优先；没有公开搜索参数时，退化为“搜索摘要桥接 + 模型总结”。
3. 保持现有调用入口不变，供搜搜、草稿生成和 AI 助手复用。
"""

from __future__ import annotations

import html
import json
import os
import re
import threading
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests
import time
import yaml

from core.app_paths import resolve_app_path
from core.local_model_manager import get_local_model_manager
from core.time_utils import local_today


REQUEST_TIMEOUT = 60
SEARCH_TIMEOUT = 15
SEARCH_RESULT_LIMIT = 6
SEARCH_FETCH_LIMIT = 8
SEARCH_QUERY_VARIANT_LIMIT = 3
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# HTTP Session 按线程复用连接池，避免跨线程共享 Session 内部状态。
_http_session: requests.Session | None = None
_direct_http_session: requests.Session | None = None
_SESSION_LOCAL = threading.local()
_PLATFORM_LAST_ERRORS: dict[str, str] = {}
_PLATFORM_LAST_ERRORS_LOCK = threading.Lock()
_OLLAMA_THINK_DISABLED_MODELS: set[str] = set()
_OLLAMA_THINK_DISABLED_MODELS_LOCK = threading.Lock()
_PLATFORM_ID_ALIASES = {
    "local_qwen": "local_model",
}


def _normalize_platform_id(platform: str) -> str:
    return _PLATFORM_ID_ALIASES.get(str(platform or "").strip().lower(), str(platform or "").strip().lower())


def _get_platform_config_entry(config: dict[str, Any] | None, platform: str) -> dict[str, Any]:
    platforms_cfg = (config or {}).get("platforms", {}) or {}
    normalized = _normalize_platform_id(platform)
    entry = platforms_cfg.get(normalized)
    if isinstance(entry, dict):
        return entry
    if normalized == "local_model":
        legacy_entry = platforms_cfg.get("local_qwen")
        if isinstance(legacy_entry, dict):
            return legacy_entry
    return {}


def _get_session() -> requests.Session:
    session = getattr(_SESSION_LOCAL, "http_session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(COMMON_HEADERS)
        _SESSION_LOCAL.http_session = session
    return session


def _get_direct_session() -> requests.Session:
    session = getattr(_SESSION_LOCAL, "direct_http_session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(COMMON_HEADERS)
        session.trust_env = False
        _SESSION_LOCAL.direct_http_session = session
    return session


def _is_loopback_url(url: str) -> bool:
    parsed = urlparse(url if "://" in url else f"http://{url}")
    host = str(parsed.hostname or "").strip().lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def _set_platform_last_error(platform: str, message: str = "") -> None:
    key = _normalize_platform_id(platform)
    if not key:
        return
    text = str(message or "").strip()
    with _PLATFORM_LAST_ERRORS_LOCK:
        if text:
            _PLATFORM_LAST_ERRORS[key] = text
        else:
            _PLATFORM_LAST_ERRORS.pop(key, None)


def get_platform_last_error(platform: str) -> str:
    with _PLATFORM_LAST_ERRORS_LOCK:
        return str(_PLATFORM_LAST_ERRORS.get(_normalize_platform_id(platform), "") or "").strip()


def _preview_text(value: Any, limit: int = 120) -> str:
    text = str(value or "").replace("\r", "\\r").replace("\n", "\\n").strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _describe_message_roles(messages: list[dict[str, Any]]) -> str:
    roles: list[str] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "") or "").strip()
        if role:
            roles.append(role)
    return " -> ".join(roles) if roles else "(empty)"


def _extract_ollama_thinking(data: dict[str, Any]) -> str:
    message = data.get("message") if isinstance(data.get("message"), dict) else {}
    return str(
        (message or {}).get("thinking", "")
        or (message or {}).get("reasoning_content", "")
        or (message or {}).get("reasoning", "")
        or data.get("thinking", "")
        or data.get("reasoning_content", "")
        or data.get("reasoning", "")
        or ""
    )


def _request_with_retry(method: str, url: str, *, max_retries: int = 2, retry_delay: float = 1.0, **kwargs) -> requests.Response:
    """带简单重试的 HTTP 请求，仅对网络层/5xx 错误重试。"""
    session = _get_direct_session() if _is_loopback_url(url) else _get_session()
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            resp = session.request(method, url, **kwargs)
            if resp.status_code < 500 or attempt >= max_retries:
                return resp
            # 5xx 重试
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            if attempt >= max_retries:
                raise
        time.sleep(retry_delay * (attempt + 1))
    raise last_exc  # type: ignore[misc]

PLATFORM_API_CONFIG = {
    "local_model": {
        "base_url": "http://127.0.0.1:11434",
        "default_model": "gemma4:e2b",
        "search_mode": "bridge",
        "api_kind": "ollama",
        "api_key_optional": True,
    },
    "doubao": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "default_model": "doubao-1-5-pro-32k-250115",
        "search_mode": "native_responses",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-chat",
        "search_mode": "bridge",
    },
    "ark_deepseek": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "default_model": "",
        "search_mode": "native_responses",
    },
    "kimi": {
        "base_url": "https://api.moonshot.cn/v1",
        "default_model": "moonshot-v1-8k",
        "search_mode": "native_builtin_tool",
    },
    "tongyi": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
        "search_mode": "native_chat",
    },
    "wenxin": {
        "base_url": "https://qianfan.baidubce.com/v2",
        "default_model": "ernie-4.0-8k",
        "search_mode": "native_ai_search",
    },
    "yuanbao": {
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "default_model": "hunyuan-turbo",
        "search_mode": "native_chat",
    },
    "chatgpt": {
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "search_mode": "bridge",
        "api_kind": "openai_compat",
    },
    "claude": {
        "base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-3-5-sonnet-latest",
        "search_mode": "bridge",
        "api_kind": "anthropic",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-2.0-flash",
        "search_mode": "bridge",
        "api_kind": "openai_compat",
    },
    "perplexity": {
        "base_url": "https://api.perplexity.ai",
        "default_model": "sonar-pro",
        "search_mode": "bridge",
        "api_kind": "openai_compat",
    },
}

PLATFORM_API_KEY_ALIASES = {
    "ark_deepseek": "doubao",
}

VISION_MODEL_HINTS = {
    "deepseek": ("vision", "vl", "janus", "image", "multi"),
    "local_model": ("vision", "vl", "omni", "multi"),
}

SEARCH_STYLE_PROMPTS = {
    "local_model": "请像可靠的本地模型助手一样，优先基于给定文本材料做稳健判断和结构化回答；不要臆测，不要跑题，结论尽量直接。",
    "doubao": "请像豆包网页端的联网回答一样，优先整合最新网页信息后给出结论。",
    "deepseek": "请像 DeepSeek 网页端的搜索回答一样，先消化搜索信息，再输出清晰结论。",
    "ark_deepseek": "请像火山方舟里的 DeepSeek 联网回答一样，先整合搜索信息，再给出清晰结论。",
    "kimi": "请像 Kimi 网页端的搜索对话一样，先做资料归纳，再给出简洁但完整的分析。",
    "tongyi": "请像通义网页端的联网回答一样，基于检索结果整合信息并输出结论。",
    "wenxin": "请像文心一言网页端的联网回答一样，优先使用最新资料总结结论。",
    "yuanbao": "请像腾讯元宝网页端的联网回答一样，优先整合最新网页信息后给出结论。",
    "chatgpt": "请像 ChatGPT 的网页端回答一样，先整合信息，再给出清晰、分步骤的结论。",
    "claude": "请像 Claude 的网页端回答一样，先结构化整理信息，再给出稳健结论。",
    "gemini": "请像 Gemini 的网页端回答一样，基于最新信息组织简洁而准确的结论。",
    "perplexity": "请像 Perplexity 的搜索问答一样，优先整合最新公开资料后给出结论。",
}


def platform_requires_api_key(platform: str) -> bool:
    cfg = PLATFORM_API_CONFIG.get(_normalize_platform_id(platform)) or {}
    return not bool(cfg.get("api_key_optional", False))


def model_supports_image_input(platform: str, model: str | None) -> bool:
    """
    粗粒度判断模型是否支持图片输入。

    当前主要用于在发起多模态请求前做前置校验，避免已知文本模型直接返回 400。
    """
    normalized_platform = _normalize_platform_id(platform)
    normalized_model = (model or "").strip().lower()
    if not normalized_model:
        normalized_model = str(
            PLATFORM_API_CONFIG.get(normalized_platform, {}).get("default_model") or ""
        ).strip().lower()

    hints = VISION_MODEL_HINTS.get(normalized_platform)
    if not hints:
        return True
    return any(hint in normalized_model for hint in hints)


def get_platform_api_key(config: dict, platform: str) -> str:
    """返回平台可用的 API Key；部分平台可回退到共享配置。"""
    config = config or {}
    platforms_cfg = config.get("platforms", {}) or {}
    direct_key = str(_get_platform_config_entry(config, platform).get("api_key", "") or "").strip()
    if direct_key:
        return direct_key

    alias = PLATFORM_API_KEY_ALIASES.get(platform)
    if alias:
        alias_key = str(platforms_cfg.get(alias, {}).get("api_key", "") or "").strip()
        if alias_key:
            return alias_key

    return ""


def platform_has_configured_access(config: dict, platform: str) -> bool:
    if not platform_requires_api_key(platform):
        return True
    return bool(get_platform_api_key(config, platform))


def query_platform_api(
    platform: str,
    keyword: str,
    api_key: str,
    model: str | None = None,
    enable_search: bool = True,
    deep_think: bool = False,
):
    """
    调用指定平台 API，返回回答文本。失败返回 None。

    搜索策略：
    - 豆包：优先使用 Responses API + web_search
    - 通义：优先使用 OpenAI 兼容接口 + enable_search
    - 文心：优先使用 AI Search / 搜索增强；旧 AK|SK 凭证退化为桥接搜索
    - DeepSeek：当前公开 API 未统一暴露网页端搜索参数，使用搜索桥接兜底
    - Kimi：优先使用官方 $web_search；失败时回退到搜索桥接
    """
    platform = _normalize_platform_id(platform)
    cfg = PLATFORM_API_CONFIG.get(platform)
    if not cfg:
        print(f"[API] 不支持的平台: {platform}")
        _set_platform_last_error(platform, f"不支持的平台: {platform}")
        return None
    if platform_requires_api_key(platform) and not api_key:
        print(f"[API] {platform} 未配置 api_key")
        _set_platform_last_error(platform, "未配置 api_key")
        return None
    resolved_model = model if model is not None else cfg["default_model"]
    if not str(resolved_model or "").strip():
        print(f"[API] {platform} 未配置 api_model / Endpoint ID")
        _set_platform_last_error(platform, "未配置 api_model / Endpoint ID")
        return None

    try:
        if platform == "local_model":
            return _query_local_model(keyword, api_key, resolved_model, enable_search, deep_think)
        if platform == "doubao":
            return _query_doubao(keyword, api_key, resolved_model, enable_search, deep_think)
        if platform == "tongyi":
            return _query_tongyi(keyword, api_key, resolved_model, enable_search, deep_think)
        if platform == "wenxin":
            return _query_wenxin(keyword, api_key, resolved_model, enable_search, deep_think)
        if platform == "ark_deepseek":
            return _query_ark_deepseek(keyword, api_key, resolved_model, enable_search, deep_think)
        if platform == "deepseek":
            return _query_deepseek(keyword, api_key, resolved_model, enable_search, deep_think)
        if platform == "kimi":
            return _query_kimi(keyword, api_key, resolved_model, enable_search, deep_think)
        if platform == "yuanbao":
            return _query_yuanbao(keyword, api_key, resolved_model, enable_search, deep_think)
        if platform == "chatgpt":
            return _query_chatgpt(keyword, api_key, resolved_model, enable_search, deep_think)
        if platform == "claude":
            return _query_claude(keyword, api_key, resolved_model, enable_search, deep_think)
        if platform == "gemini":
            return _query_gemini(keyword, api_key, resolved_model, enable_search, deep_think)
        if platform == "perplexity":
            return _query_perplexity(keyword, api_key, resolved_model, enable_search, deep_think)
    except Exception as e:
        print(f"[API] {platform} 调用失败: {e}")
        return None

    print(f"[API] 未实现的平台: {platform}")
    return None


def _openai_content_to_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                text = str(item.get("text", "")).strip()
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()
    return str(content or "").strip()


def _openai_content_to_anthropic_blocks(content) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if isinstance(content, str):
        text = content.strip()
        if text:
            blocks.append({"type": "text", "text": text})
        return blocks

    if not isinstance(content, list):
        text = str(content or "").strip()
        if text:
            blocks.append({"type": "text", "text": text})
        return blocks

    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text":
            text = str(item.get("text", "")).strip()
            if text:
                blocks.append({"type": "text", "text": text})
            continue
        if item_type != "image_url":
            continue
        image_url = item.get("image_url", {}) or {}
        url = str(image_url.get("url", "") or "").strip()
        if not url.startswith("data:") or "," not in url:
            continue
        header, data = url.split(",", 1)
        media_type = "image/png"
        if ";" in header:
            prefix = header[5:]
            if prefix:
                media_type = prefix.split(";", 1)[0] or media_type
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": data,
            },
        })
    return blocks


def _openai_content_to_ollama_message(content) -> dict[str, Any]:
    text = _openai_content_to_text(content)
    message: dict[str, Any] = {"content": text}

    if not isinstance(content, list):
        return message

    images: list[str] = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "image_url":
            continue
        image_url = item.get("image_url", {}) or {}
        url = str(image_url.get("url", "") or "").strip()
        if not url.startswith("data:") or "," not in url:
            continue
        _, data = url.split(",", 1)
        data = data.strip()
        if data:
            images.append(data)
    if images:
        message["images"] = images
    return message


def _extract_ollama_text(data: dict[str, Any]) -> str:
    message = data.get("message")
    if isinstance(message, dict):
        text = str(message.get("content", "") or "").strip()
        if text:
            return text
    return str(data.get("response", "") or "").strip()


def _extract_ollama_stream_piece(data: dict[str, Any]) -> dict[str, Any]:
    message = data.get("message") if isinstance(data.get("message"), dict) else {}
    content = str(
        (message or {}).get("content", "")
        or data.get("response", "")
        or ""
    )
    thinking = _extract_ollama_thinking(data)
    return {
        "content": content,
        "thinking": thinking,
        "done": bool(data.get("done")),
    }


def _build_ollama_extra_body(
    platform: str,
    *,
    deep_think: bool = False,
    extra_body: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    统一补齐 Ollama 专有请求参数。

    对本地模型场景，深度思考不能只靠 system prompt 暗示，需要显式下发
    think 开关给 Ollama，模型才会进入 thinking 模式。
    """
    merged = dict(extra_body or {})
    if platform == "local_model" and deep_think and "think" not in merged:
        merged["think"] = True
    return merged or None


def _format_http_error_with_body(exc: Exception) -> str:
    text = str(exc).strip()
    response = getattr(exc, "response", None)
    if response is None:
        return text
    try:
        body = response.text.strip()
    except Exception:
        body = ""
    if body:
        return f"{text}; body={body[:500]}"
    return text


def _request_ollama_chat(
    *,
    platform: str,
    base_url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    deep_think: bool,
    stream: bool,
):
    """
    向 Ollama 发起聊天请求。

    某些本地运行时或模型在开启 think 后会直接返回 500，这里自动回退一次，
    避免用户在程序里完全不可用。
    """
    url = base_url.rstrip("/") + "/api/chat"
    model_name = str(payload.get("model", "") or "").strip()
    effective_payload = payload
    effective_deep_think = deep_think
    with _OLLAMA_THINK_DISABLED_MODELS_LOCK:
        remembered_disabled = bool(model_name and model_name in _OLLAMA_THINK_DISABLED_MODELS)
    print(
        f"[OLLAMA DEBUG] request model={model_name or '(empty)'} stream={stream} "
        f"deep_think={deep_think} payload_think={bool(payload.get('think'))} "
        f"remembered_disabled={remembered_disabled} messages={len(payload.get('messages') or [])}"
    )
    if (
        platform == "local_model"
        and deep_think
        and model_name
        and remembered_disabled
        and payload.get("think")
    ):
        effective_payload = dict(payload)
        effective_payload.pop("think", None)
        effective_deep_think = False
        print(
            f"[API] {platform} 已记住模型 {model_name} 的 think 不稳定，本次直接回退普通模式 "
            f"(payload_think=False)"
        )
    try:
        return _request_with_retry(
            "POST",
            url,
            headers=headers,
            json=effective_payload,
            timeout=REQUEST_TIMEOUT,
            stream=stream,
        )
    except requests.HTTPError as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if effective_deep_think and status_code == 500:
            fallback_payload = dict(effective_payload)
            fallback_payload.pop("think", None)
            if platform == "local_model" and model_name:
                with _OLLAMA_THINK_DISABLED_MODELS_LOCK:
                    _OLLAMA_THINK_DISABLED_MODELS.add(model_name)
                print(f"[API] {platform} 已将模型 {model_name} 标记为 think 不稳定，后续自动禁用")
            print(f"[API] {platform} think 模式触发 500，自动回退普通模式")
            return _request_with_retry(
                "POST",
                url,
                headers=headers,
                json=fallback_payload,
                timeout=REQUEST_TIMEOUT,
                stream=stream,
            )
        raise


def _extract_anthropic_text(data: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in data.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            text = str(item.get("text", "") or "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def send_platform_chat_messages(
    platform: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    *,
    deep_think: bool = False,
    extra_body: dict[str, Any] | None = None,
) -> str | None:
    """发送通用聊天消息，兼容 OpenAI 风格与 Anthropic 风格接口。"""
    platform = _normalize_platform_id(platform)
    cfg = PLATFORM_API_CONFIG.get(platform) or {}
    api_kind = str(cfg.get("api_kind") or "openai_compat").strip()
    base_url = str(cfg.get("base_url") or "").strip()
    if (platform_requires_api_key(platform) and not api_key) or not model or not base_url:
        if platform_requires_api_key(platform) and not api_key:
            _set_platform_last_error(platform, "未配置 API Key")
        elif not model:
            _set_platform_last_error(platform, "未配置模型名")
        elif not base_url:
            _set_platform_last_error(platform, "未配置接口地址")
        return None

    try:
        print(
            f"[API] {platform} 调用中，模型: {model}"
            f"{'，深度思考: 开' if deep_think else ''}"
        )
        if api_kind == "anthropic":
            headers = {
                **COMMON_HEADERS,
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            }
            system_texts: list[str] = []
            anthropic_messages: list[dict[str, Any]] = []
            for message in messages:
                if not isinstance(message, dict):
                    continue
                role = str(message.get("role", "")).strip()
                content = message.get("content", "")
                if role == "system":
                    text = _openai_content_to_text(content)
                    if text:
                        system_texts.append(text)
                    continue
                if role not in {"user", "assistant"}:
                    continue
                blocks = _openai_content_to_anthropic_blocks(content)
                if not blocks:
                    continue
                anthropic_messages.append({"role": role, "content": blocks})

            payload: dict[str, Any] = {
                "model": model,
                "max_tokens": 2048,
                "messages": anthropic_messages,
            }
            if system_texts:
                payload["system"] = "\n\n".join(system_texts)
            if extra_body:
                payload.update(extra_body)
            resp = _request_with_retry(
                "POST",
                base_url.rstrip("/") + "/messages",
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            text = _extract_anthropic_text(resp.json())
            if text:
                print(f"[API] {platform} 返回 {len(text)} 字符")
            _set_platform_last_error(platform, "")
            return text

        if api_kind == "ollama":
            manager = get_local_model_manager()
            ok, prepare_message = manager.ensure_ready(model, reason="chat")
            if not ok:
                _set_platform_last_error(platform, prepare_message)
                print(f"[API] {platform} 本地模型未就绪: {prepare_message}")
                return None

            ollama_messages: list[dict[str, Any]] = []
            for message in messages:
                if not isinstance(message, dict):
                    continue
                role = str(message.get("role", "")).strip()
                if role not in {"system", "user", "assistant"}:
                    continue
                payload = _openai_content_to_ollama_message(message.get("content", ""))
                payload["role"] = role
                ollama_messages.append(payload)

            headers = {
                **COMMON_HEADERS,
                "Content-Type": "application/json",
            }
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            payload: dict[str, Any] = {
                "model": model,
                "messages": ollama_messages,
                "stream": False,
            }
            ollama_extra_body = _build_ollama_extra_body(
                platform,
                deep_think=deep_think,
                extra_body=extra_body,
            )
            if ollama_extra_body:
                payload.update(ollama_extra_body)
            resp = _request_ollama_chat(
                platform=platform,
                base_url=base_url,
                headers=headers,
                payload=payload,
                deep_think=deep_think,
                stream=False,
            )
            resp.raise_for_status()
            data = resp.json()
            thinking = _extract_ollama_thinking(data).strip()
            text = _extract_ollama_text(data)
            print(
                f"[OLLAMA DEBUG] nonstream response model={model} "
                f"thinking_len={len(thinking)} content_len={len(text)} "
                f"thinking_preview={_preview_text(thinking)} content_preview={_preview_text(text)}"
            )
            if text:
                print(f"[API] {platform} 返回 {len(text)} 字符")
            _set_platform_last_error(platform, "")
            return text

        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "timeout": REQUEST_TIMEOUT,
        }
        if extra_body:
            kwargs["extra_body"] = extra_body
        resp = client.chat.completions.create(**kwargs)
        text = _extract_chat_text(resp)
        if text:
            print(f"[API] {platform} 返回 {len(text)} 字符")
        _set_platform_last_error(platform, "")
        return text
    except ImportError:
        print("[API] 缺少 openai 库，请运行: pip install openai")
        _set_platform_last_error(platform, "缺少 openai 依赖，请先安装 openai")
        return None
    except Exception as e:
        error_message = _format_http_error_with_body(e)
        print(f"[API] {platform} 调用失败: {error_message}")
        _set_platform_last_error(platform, error_message)
        return None


def send_platform_chat_messages_stream(
    platform: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    *,
    deep_think: bool = False,
    extra_body: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """发送通用聊天消息流；Ollama 走真流式，其它平台回退为单次结果。"""
    platform = _normalize_platform_id(platform)
    cfg = PLATFORM_API_CONFIG.get(platform) or {}
    api_kind = str(cfg.get("api_kind") or "openai_compat").strip()
    base_url = str(cfg.get("base_url") or "").strip()
    if (platform_requires_api_key(platform) and not api_key) or not model or not base_url:
        if platform_requires_api_key(platform) and not api_key:
            _set_platform_last_error(platform, "未配置 API Key")
        elif not model:
            _set_platform_last_error(platform, "未配置模型名")
        elif not base_url:
            _set_platform_last_error(platform, "未配置接口地址")
        yield {"type": "error", "message": get_platform_last_error(platform) or "聊天请求配置不完整"}
        return

    if api_kind != "ollama":
        text = send_platform_chat_messages(
            platform,
            api_key,
            model,
            messages,
            deep_think=deep_think,
            extra_body=extra_body,
        )
        if text is None:
            yield {"type": "error", "message": get_platform_last_error(platform) or f"平台 {platform} 接口没有返回内容"}
            return
        if text:
            yield {"type": "delta", "delta": text}
        yield {"type": "done"}
        return

    try:
        manager = get_local_model_manager()
        ok, prepare_message = manager.ensure_ready(model, reason="chat_stream")
        if not ok:
            _set_platform_last_error(platform, prepare_message)
            yield {"type": "error", "message": prepare_message}
            return

        ollama_messages: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role", "")).strip()
            if role not in {"system", "user", "assistant"}:
                continue
            payload = _openai_content_to_ollama_message(message.get("content", ""))
            payload["role"] = role
            ollama_messages.append(payload)

        headers = {
            **COMMON_HEADERS,
            "Content-Type": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload: dict[str, Any] = {
            "model": model,
            "messages": ollama_messages,
            "stream": True,
        }
        ollama_extra_body = _build_ollama_extra_body(
            platform,
            deep_think=deep_think,
            extra_body=extra_body,
        )
        if ollama_extra_body:
            payload.update(ollama_extra_body)
        print(
            f"[OLLAMA DEBUG] stream setup model={model} deep_think={deep_think} "
            f"payload_think={bool(payload.get('think'))} roles={_describe_message_roles(ollama_messages)}"
        )

        session = _get_direct_session() if _is_loopback_url(base_url) else _get_session()
        with _request_ollama_chat(
            platform=platform,
            base_url=base_url,
            headers=headers,
            payload=payload,
            deep_think=deep_think,
            stream=True,
        ) as resp:
            resp.raise_for_status()
            seen_thinking = False
            seen_content = False
            for raw_line in resp.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue
                try:
                    data = json.loads(raw_line)
                except Exception:
                    continue
                if data.get("error"):
                    message = str(data.get("error") or "").strip() or f"平台 {platform} 流式返回错误"
                    _set_platform_last_error(platform, message)
                    yield {"type": "error", "message": message}
                    return
                piece = _extract_ollama_stream_piece(data)
                if piece["thinking"]:
                    if not seen_thinking:
                        seen_thinking = True
                        print(
                            f"[OLLAMA DEBUG] first thinking chunk model={model} "
                            f"len={len(piece['thinking'])} preview={_preview_text(piece['thinking'])}"
                        )
                    yield {"type": "thinking_delta", "delta": piece["thinking"]}
                if piece["content"]:
                    if not seen_content:
                        seen_content = True
                        print(
                            f"[OLLAMA DEBUG] first content chunk model={model} "
                            f"len={len(piece['content'])} preview={_preview_text(piece['content'])}"
                        )
                    yield {"type": "delta", "delta": piece["content"]}
                if piece["done"]:
                    if not seen_thinking:
                        print(f"[OLLAMA DEBUG] stream done model={model} without thinking chunk")
                    if not seen_content:
                        print(f"[OLLAMA DEBUG] stream done model={model} without content chunk")
                    _set_platform_last_error(platform, "")
                    yield {"type": "done"}
                    return
    except Exception as e:
        error_message = _format_http_error_with_body(e)
        print(f"[API] {platform} 流式调用失败: {error_message}")
        _set_platform_last_error(platform, error_message)
        yield {"type": "error", "message": error_message}


def _query_doubao(keyword: str, api_key: str, model: str, enable_search: bool, deep_think: bool) -> str | None:
    if enable_search:
        text = _query_openai_responses_with_web_search(
            platform="doubao",
            api_key=api_key,
            base_url=PLATFORM_API_CONFIG["doubao"]["base_url"],
            model=model,
            keyword=keyword,
            deep_think=deep_think,
        )
        if text:
            print("[API] doubao 原生联网搜索成功")
            return text
        print("[API] doubao 原生联网搜索不可用，回退到搜索桥接")
    return _query_openai_compat(
        platform="doubao",
        keyword=keyword,
        api_key=api_key,
        base_url=PLATFORM_API_CONFIG["doubao"]["base_url"],
        model=model,
        search_context=_build_search_context(keyword, "doubao") if enable_search else "",
        extra_body=_build_extra_body("doubao", enable_search=False, deep_think=deep_think),
        deep_think=deep_think,
    )


def _query_tongyi(keyword: str, api_key: str, model: str, enable_search: bool, deep_think: bool) -> str | None:
    if enable_search:
        text = _query_openai_compat(
            platform="tongyi",
            keyword=keyword,
            api_key=api_key,
            base_url=PLATFORM_API_CONFIG["tongyi"]["base_url"],
            model=model,
            extra_body=_build_extra_body("tongyi", enable_search=True, deep_think=deep_think),
            deep_think=deep_think,
        )
        if text:
            print("[API] tongyi 原生联网搜索成功")
            return text
        print("[API] tongyi 原生联网搜索不可用，回退到搜索桥接")
    return _query_openai_compat(
        platform="tongyi",
        keyword=keyword,
        api_key=api_key,
        base_url=PLATFORM_API_CONFIG["tongyi"]["base_url"],
        model=model,
        search_context=_build_search_context(keyword, "tongyi") if enable_search else "",
        extra_body=_build_extra_body("tongyi", enable_search=False, deep_think=deep_think),
        deep_think=deep_think,
    )


def _query_deepseek(keyword: str, api_key: str, model: str, enable_search: bool, deep_think: bool) -> str | None:
    resolved_model = _resolve_runtime_model("deepseek", model, deep_think)
    return _query_openai_compat(
        platform="deepseek",
        keyword=keyword,
        api_key=api_key,
        base_url=PLATFORM_API_CONFIG["deepseek"]["base_url"],
        model=resolved_model,
        search_context=_build_search_context(keyword, "deepseek") if enable_search else "",
        extra_body=_build_extra_body("deepseek", enable_search=False, deep_think=deep_think),
        deep_think=deep_think,
    )


def _query_yuanbao(keyword: str, api_key: str, model: str, enable_search: bool, deep_think: bool) -> str | None:
    resolved_model = _resolve_runtime_model("yuanbao", model, deep_think)
    # 混元原生支持联网增强（enable_enhancement）和深度思考（enable_thinking）
    return _query_openai_compat(
        platform="yuanbao",
        keyword=keyword,
        api_key=api_key,
        base_url=PLATFORM_API_CONFIG["yuanbao"]["base_url"],
        model=resolved_model,
        search_context="",
        extra_body=_build_extra_body("yuanbao", enable_search=enable_search, deep_think=deep_think),
        deep_think=deep_think,
    )


def _query_chatgpt(keyword: str, api_key: str, model: str, enable_search: bool, deep_think: bool) -> str | None:
    search_context = _build_search_context(keyword, "chatgpt") if enable_search else ""
    return send_platform_chat_messages(
        "chatgpt",
        api_key,
        model,
        _build_messages("chatgpt", keyword, search_context, deep_think=deep_think),
        deep_think=deep_think,
    )


def _query_local_model(keyword: str, api_key: str, model: str, enable_search: bool, deep_think: bool) -> str | None:
    return send_platform_chat_messages(
        "local_model",
        api_key,
        model,
        _build_chat_messages_for_keyword(
            "local_model",
            keyword,
            search_context=_build_search_context(keyword, "local_model") if enable_search else "",
            deep_think=deep_think,
        ),
        deep_think=deep_think,
    )


def _query_claude(keyword: str, api_key: str, model: str, enable_search: bool, deep_think: bool) -> str | None:
    search_context = _build_search_context(keyword, "claude") if enable_search else ""
    return send_platform_chat_messages(
        "claude",
        api_key,
        model,
        _build_messages("claude", keyword, search_context, deep_think=deep_think),
        deep_think=deep_think,
    )


def _query_gemini(keyword: str, api_key: str, model: str, enable_search: bool, deep_think: bool) -> str | None:
    search_context = _build_search_context(keyword, "gemini") if enable_search else ""
    return send_platform_chat_messages(
        "gemini",
        api_key,
        model,
        _build_messages("gemini", keyword, search_context, deep_think=deep_think),
        deep_think=deep_think,
    )


def _query_perplexity(keyword: str, api_key: str, model: str, enable_search: bool, deep_think: bool) -> str | None:
    search_context = _build_search_context(keyword, "perplexity") if enable_search else ""
    return send_platform_chat_messages(
        "perplexity",
        api_key,
        model,
        _build_messages("perplexity", keyword, search_context, deep_think=deep_think),
        deep_think=deep_think,
    )


def _query_ark_deepseek(keyword: str, api_key: str, model: str, enable_search: bool, deep_think: bool) -> str | None:
    if not model:
        print("[API] ark_deepseek 未配置推理接入点 ID 或模型标识")
        return None

    if enable_search:
        text = _query_openai_responses_with_web_search(
            platform="ark_deepseek",
            api_key=api_key,
            base_url=PLATFORM_API_CONFIG["ark_deepseek"]["base_url"],
            model=model,
            keyword=keyword,
            deep_think=deep_think,
        )
        if text:
            print("[API] ark_deepseek 原生联网搜索成功")
            return text
        print("[API] ark_deepseek 原生联网搜索不可用，回退到搜索桥接")

    return _query_openai_compat(
        platform="ark_deepseek",
        keyword=keyword,
        api_key=api_key,
        base_url=PLATFORM_API_CONFIG["ark_deepseek"]["base_url"],
        model=model,
        search_context=_build_search_context(keyword, "ark_deepseek") if enable_search else "",
        extra_body=_build_extra_body("ark_deepseek", enable_search=False, deep_think=deep_think),
        deep_think=deep_think,
    )


def _query_kimi(keyword: str, api_key: str, model: str, enable_search: bool, deep_think: bool) -> str | None:
    if enable_search:
        text = _query_kimi_with_official_web_search(keyword, api_key, model, deep_think)
        if text:
            print("[API] kimi 原生联网搜索成功")
            return text
        print("[API] kimi 原生联网搜索不可用，回退到搜索桥接")

    return _query_openai_compat(
        platform="kimi",
        keyword=keyword,
        api_key=api_key,
        base_url=PLATFORM_API_CONFIG["kimi"]["base_url"],
        model=model,
        search_context=_build_search_context(keyword, "kimi") if enable_search else "",
        deep_think=deep_think,
    )


def _query_kimi_with_official_web_search(keyword: str, api_key: str, model: str, deep_think: bool) -> str | None:
    """
    Kimi 官方联网搜索（$web_search）。

    Moonshot 官方文档要求以 builtin_function 工具调用方式接入，并在工具轮次
    中把工具返回结果以 role=tool 的消息回传。该搜索工具当前与 kimi-k2.5 的
    thinking 模式不兼容，因此在用户开启深度思考时会先关闭这一参数；若失败，
    再由上层回退到现有的搜索桥接逻辑。
    """
    url = PLATFORM_API_CONFIG["kimi"]["base_url"].rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _build_system_prompt("kimi", native_search=True, deep_think=False)},
            {"role": "user", "content": keyword},
        ],
        "tools": [
            {
                "type": "builtin_function",
                "function": {"name": "$web_search"},
            }
        ],
    }
    if deep_think:
        # 官方文档说明 $web_search 与 thinking 模式暂不兼容，这里显式关闭，
        # 失败后再交给桥接搜索路径继续处理。
        payload["thinking"] = {"type": "disabled"}

    try:
        print(
            f"[API] kimi 调用中，模型: {model}，模式: 官方联网搜索"
            f"{'，thinking: 自动关闭' if deep_think else ''}"
        )
        for _ in range(4):
            resp = _request_with_retry(
                "POST",
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                return None

            message = ((choices[0] or {}).get("message") or {})
            tool_calls = message.get("tool_calls") or []
            text = _flatten_content(message.get("content"))
            if text and not tool_calls:
                print(f"[API] kimi 返回 {len(text)} 字符")
                return text
            if not tool_calls:
                return text

            payload["messages"].append(_build_kimi_assistant_tool_message(message))
            for tool_call in tool_calls:
                tool_id = str(tool_call.get("id", "") or "").strip()
                function = tool_call.get("function") or {}
                tool_name = str(function.get("name", "") or "").strip()
                raw_arguments = function.get("arguments") or "{}"

                if not tool_id or not tool_name:
                    continue

                tool_content = _build_kimi_tool_result_content(tool_name, raw_arguments)
                payload["messages"].append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "name": tool_name,
                        "content": tool_content,
                    }
                )

        print("[API] kimi 官方联网搜索超过最大工具轮次")
        return None
    except Exception as e:
        print(f"[API] kimi 官方联网搜索调用失败: {e}")
        return None


def _build_kimi_tool_result_content(tool_name: str, raw_arguments: Any) -> str:
    if tool_name != "$web_search":
        return json.dumps(
            {"error": f"unsupported tool: {tool_name}"},
            ensure_ascii=False,
        )

    if isinstance(raw_arguments, str):
        try:
            parsed_arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            parsed_arguments = {"query": raw_arguments}
    elif isinstance(raw_arguments, dict):
        parsed_arguments = raw_arguments
    else:
        parsed_arguments = {"arguments": raw_arguments}

    if not isinstance(parsed_arguments, dict):
        parsed_arguments = {"arguments": parsed_arguments}

    return json.dumps(parsed_arguments, ensure_ascii=False)


def _build_kimi_assistant_tool_message(message: dict[str, Any]) -> dict[str, Any]:
    assistant_message: dict[str, Any] = {
        "role": str(message.get("role", "assistant") or "assistant"),
    }
    if "content" in message:
        assistant_message["content"] = message.get("content")
    if message.get("tool_calls"):
        assistant_message["tool_calls"] = message["tool_calls"]
    if message.get("reasoning_content"):
        assistant_message["reasoning_content"] = message["reasoning_content"]
    return assistant_message


def _query_wenxin(keyword: str, api_key: str, model: str, enable_search: bool, deep_think: bool) -> str | None:
    if enable_search and "|" not in api_key:
        text = _query_wenxin_ai_search(keyword, api_key, model, deep_think)
        if text:
            print("[API] wenxin 原生联网搜索成功（AI Search）")
            return text
        print("[API] wenxin AI Search 不可用，回退到搜索增强/桥接")

        text = _query_wenxin_openai_compat(
            keyword=keyword,
            api_key=api_key,
            model=model,
            enable_search=True,
            deep_think=deep_think,
        )
        if text:
            print("[API] wenxin 原生联网搜索成功（搜索增强）")
            return text
        print("[API] wenxin 搜索增强不可用，回退到搜索桥接")

    if "|" in api_key:
        return _query_wenxin_legacy(
            keyword=keyword,
            api_key=api_key,
            model=model,
            search_context=_build_search_context(keyword, "wenxin") if enable_search else "",
            deep_think=deep_think,
        )

    return _query_wenxin_openai_compat(
        keyword=keyword,
        api_key=api_key,
        model=model,
        enable_search=False,
        search_context=_build_search_context(keyword, "wenxin") if enable_search else "",
        deep_think=deep_think,
    )


def _query_openai_responses_with_web_search(
    platform: str,
    api_key: str,
    base_url: str,
    model: str,
    keyword: str,
    deep_think: bool = False,
) -> str | None:
    try:
        print(f"[API] {platform} 调用中，模型: {model}，模式: 原生联网搜索")
        payload: dict[str, Any] = {
            "model": model,
            "input": [
                {"role": "system", "content": _build_system_prompt(platform, native_search=True, deep_think=deep_think)},
                {"role": "user", "content": keyword},
            ],
            "tools": [{"type": "web_search"}],
        }
        extra_body = _build_extra_body(platform, enable_search=True, deep_think=deep_think)
        if extra_body:
            payload.update(extra_body)
        url = base_url.rstrip("/") + "/responses"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        resp = _request_with_retry("POST", url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        text = _extract_responses_json_text(data)
        if text:
            print(f"[API] {platform} 返回 {len(text)} 字符")
        return text
    except Exception as e:
        print(f"[API] {platform} Responses API 调用失败: {e}")
        return None


def _query_openai_compat(
    platform: str,
    keyword: str,
    api_key: str,
    base_url: str,
    model: str,
    search_context: str = "",
    extra_body: dict[str, Any] | None = None,
    deep_think: bool = False,
) -> str | None:
    """调用 OpenAI 兼容接口。"""
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url)
        print(
            f"[API] {platform} 调用中，模型: {model}"
            f"{'，模式: 搜索桥接' if search_context else ''}"
            f"{'，模式: 原生联网/扩展参数' if extra_body else ''}"
            f"{'，深度思考: 开' if deep_think else ''}"
        )
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": _build_messages(platform, keyword, search_context, deep_think=deep_think),
            "timeout": REQUEST_TIMEOUT,
        }
        if extra_body:
            kwargs["extra_body"] = extra_body
        resp = client.chat.completions.create(**kwargs)
        text = _extract_chat_text(resp)
        if text:
            print(f"[API] {platform} 返回 {len(text)} 字符")
        return text
    except ImportError:
        print("[API] 缺少 openai 库，请运行: pip install openai")
        return None
    except Exception as e:
        print(f"[API] {platform} 调用失败: {e}")
        return None


def _query_wenxin_ai_search(keyword: str, api_key: str, model: str, deep_think: bool) -> str | None:
    """
    文心 AI Search 接口。
    适用于直接希望获取“联网搜索 + 总结”风格回答的场景。
    """
    url = "https://qianfan.baidubce.com/v2/ai_search/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _build_system_prompt("wenxin", native_search=True, deep_think=deep_think)},
            {"role": "user", "content": keyword},
        ],
        "stream": False,
    }
    extra_body = _build_extra_body("wenxin", enable_search=False, deep_think=deep_think)
    payload.update(extra_body)
    try:
        print(f"[API] wenxin 调用中，模型: {model}，模式: AI Search")
        resp = _request_with_retry(
            "POST",
            url,
            headers={
                **COMMON_HEADERS,
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        text = _extract_openai_json_text(data)
        if text:
            print(f"[API] wenxin 返回 {len(text)} 字符")
        return text
    except Exception as e:
        print(f"[API] wenxin AI Search 调用失败: {e}")
        return None


def _query_wenxin_openai_compat(
    keyword: str,
    api_key: str,
    model: str,
    enable_search: bool,
    search_context: str = "",
    deep_think: bool = False,
) -> str | None:
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            base_url=PLATFORM_API_CONFIG["wenxin"]["base_url"],
        )
        extra_body = _build_extra_body("wenxin", enable_search=enable_search, deep_think=deep_think)
        print(
            f"[API] wenxin 调用中，模型: {model}"
            f"{'，模式: 搜索增强' if enable_search else ''}"
            f"{'，模式: 搜索桥接' if search_context else ''}"
        )
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": _build_messages("wenxin", keyword, search_context, deep_think=deep_think),
            "timeout": REQUEST_TIMEOUT,
        }
        if extra_body:
            kwargs["extra_body"] = extra_body
        resp = client.chat.completions.create(**kwargs)
        text = _extract_chat_text(resp)
        if text:
            print(f"[API] wenxin 返回 {len(text)} 字符")
        return text
    except Exception as e:
        print(f"[API] wenxin OpenAI兼容接口调用失败: {e}")
        return None


def _query_wenxin_legacy(
    keyword: str,
    api_key: str,
    model: str,
    search_context: str = "",
    deep_think: bool = False,
) -> str | None:
    """旧版 qianfan SDK，凭证格式为 ACCESS_KEY|SECRET_KEY。"""
    try:
        import qianfan

        parts = api_key.split("|", 1)
        if len(parts) != 2:
            print("[API] wenxin api_key 格式应为 ACCESS_KEY|SECRET_KEY 或 Bearer Token")
            return None

        chat = qianfan.ChatCompletion(ak=parts[0].strip(), sk=parts[1].strip())
        print(
            f"[API] wenxin 调用中，模型: {model}"
            f"{'，模式: 搜索桥接' if search_context else ''}"
        )
        payload: dict[str, Any] = {
            "model": model,
            "messages": _build_messages("wenxin", keyword, search_context, deep_think=deep_think),
        }
        payload.update(_build_extra_body("wenxin", enable_search=False, deep_think=deep_think))
        resp = chat.do(**payload)
        text = str(resp.get("result", "")).strip()
        if text:
            print(f"[API] wenxin 返回 {len(text)} 字符")
        return text or None
    except ImportError:
        print("[API] 缺少 qianfan 库，请运行: pip install qianfan")
        return None
    except Exception as e:
        print(f"[API] wenxin 旧版 SDK 调用失败: {e}")
        return None


def _build_messages(
    platform: str,
    keyword: str,
    search_context: str = "",
    deep_think: bool = False,
) -> list[dict[str, str]]:
    messages = [{"role": "system", "content": _build_system_prompt(platform, native_search=False, deep_think=deep_think)}]
    if search_context:
        user_prompt = (
            f"用户问题：{keyword}\n\n"
            f"{search_context}\n\n"
            "请严格遵守：\n"
            "1. 优先依据上面的联网检索结果回答，不要忽略检索摘要。\n"
            "2. 如不同来源有冲突，优先较新日期和更权威来源。\n"
            "3. 如检索摘要仍不足以支持确定结论，请明确说明不确定。\n"
            "4. 回答风格尽量贴近该平台网页端搜索回答：先结论，后要点，可自然引用 [1][2]。\n"
            "5. 用中文回答。"
        )
    else:
        user_prompt = keyword
    messages.append({"role": "user", "content": user_prompt})
    return messages


def _build_system_prompt(platform: str, native_search: bool, deep_think: bool = False) -> str:
    base = SEARCH_STYLE_PROMPTS.get(platform, "请像该平台网页端回答一样输出。")
    freshness = local_today().isoformat()
    deep_think_hint = (
        " 请先充分思考、拆解问题，再给出结论与依据。"
        if deep_think else
        " 请直接给出清晰结论，避免冗长铺垫。"
    )
    if native_search:
        return (
            f"{base} 今天是 {freshness}。"
            "请优先使用联网检索到的最新公开资料，整合、去重后给出结论。"
            "如果资料不足，请明确说明，不要编造。"
            f"{deep_think_hint}"
        )
    return (
        f"{base} 今天是 {freshness}。"
        "如果消息中已经附带联网检索摘要，请优先依据这些摘要回答；"
        "如果没有检索摘要，就基于你现有能力回答，但不要伪造实时来源。"
        f"{deep_think_hint}"
    )


def _resolve_runtime_model(platform: str, model: str, deep_think: bool) -> str:
    if platform == "deepseek":
        if not model or model in {"deepseek-chat", "deepseek-reasoner"}:
            return "deepseek-reasoner" if deep_think else "deepseek-chat"
        return model
    if platform == "yuanbao":
        # 深度思考时切换到元宝托管的 DeepSeek-R1 模型
        if deep_think and (not model or model == "hunyuan-turbo"):
            return "hunyuan-deepseek-r1"
        return model or "hunyuan-turbo"
    return model


def _build_extra_body(platform: str, enable_search: bool, deep_think: bool) -> dict[str, Any] | None:
    extra: dict[str, Any] = {}

    if platform == "tongyi" and enable_search:
        extra["enable_search"] = True
    elif platform == "wenxin" and enable_search:
        extra["web_search"] = {"enable": True}
    elif platform == "yuanbao" and enable_search:
        extra["enable_enhancement"] = True

    if deep_think:
        if platform in {"doubao", "deepseek", "ark_deepseek"}:
            # 基于官方文档描述推断使用 thinking 对象控制深度思考。
            extra["thinking"] = {"type": "enabled"}
        elif platform in {"tongyi", "wenxin", "yuanbao"}:
            extra["enable_thinking"] = True

    return extra or None


def _build_search_context(keyword: str, platform: str) -> str:
    results = _fetch_search_results(keyword, limit=SEARCH_RESULT_LIMIT)
    if not results:
        print(f"[API] {platform} 搜索桥接未获取到结果，将退化为普通回答")
        return ""

    lines = [
        "以下是系统刚刚联网检索到的网页摘要，请将它们当作最新参考资料：",
    ]
    for idx, item in enumerate(results, start=1):
        title = item.get("title", "").strip()
        snippet = item.get("snippet", "").strip()
        url = item.get("url", "").strip()
        if title:
            lines.append(f"[{idx}] 标题：{title}")
        if snippet:
            lines.append(f"[{idx}] 摘要：{snippet}")
        if url:
            lines.append(f"[{idx}] 链接：{url}")
    return "\n".join(lines)


def _fetch_search_results(query: str, limit: int = 6) -> list[dict[str, str]]:
    normalized_query = _normalize_query_text(query)
    tavily_api_key = _get_tavily_api_key()
    if tavily_api_key:
        try:
            tavily_results = _search_tavily(normalized_query, max(limit, SEARCH_FETCH_LIMIT), tavily_api_key)
            if tavily_results:
                print(f"[API] Tavily 搜索命中 {len(tavily_results)} 条结果")
                return tavily_results[:limit]
        except Exception as e:
            print(f"[API] Tavily 搜索失败: {e}")

    variants = _build_search_query_variants(normalized_query)
    fetchers = (_search_bing_rss, _search_bing_html, _search_duckduckgo_html)
    ranked: dict[str, dict[str, Any]] = {}
    failures: list[str] = []

    for variant in variants:
        for fetcher in fetchers:
            try:
                results = fetcher(variant, max(limit, SEARCH_FETCH_LIMIT))
            except Exception as e:
                failures.append(f"{fetcher.__name__} / {variant}: {e}")
                continue

            for item in results:
                normalized = _normalize_search_item(item)
                if not normalized:
                    continue

                unique_key = normalized["url"] or f'{normalized["title"]}|{normalized["snippet"]}'
                score = _score_search_result(normalized_query, normalized)
                if not unique_key:
                    continue

                existing = ranked.get(unique_key)
                if not existing or score > existing["_score"]:
                    ranked[unique_key] = {
                        **normalized,
                        "_score": score,
                        "_variant": variant,
                        "_fetcher": fetcher.__name__,
                    }

            # 已经有足够高相关候选时，不再继续访问后续兜底搜索源。
            if _has_enough_ranked_results(ranked, limit):
                break
        if _has_enough_ranked_results(ranked, limit):
            break

    if not ranked:
        if failures:
            preview = " | ".join(failures[:3])
            print(f"[API] 搜索桥接未获取到结果，失败摘要: {preview}")
        return []

    ordered = sorted(
        ranked.values(),
        key=lambda item: (
            item.get("_score", 0.0),
            len(item.get("snippet", "")),
            len(item.get("title", "")),
        ),
        reverse=True,
    )

    filtered = [
        {
            "title": item["title"],
            "snippet": item["snippet"],
            "url": item["url"],
        }
        for item in ordered
        if item.get("_score", 0.0) > 0
    ]
    selected = filtered[:limit] or [
        {
            "title": item["title"],
            "snippet": item["snippet"],
            "url": item["url"],
        }
        for item in ordered[:limit]
    ]
    print(
        f"[API] 搜索桥接聚合命中 {len(ranked)} 条候选，"
        f"返回 {len(selected)} 条高相关结果"
    )
    return selected


def _has_enough_ranked_results(ranked: dict[str, dict[str, Any]], limit: int) -> bool:
    good_results = [
        item for item in ranked.values()
        if item.get("_score", 0.0) >= 0.6
    ]
    return len(good_results) >= max(3, min(limit, 6))


def _search_tavily(query: str, limit: int, api_key: str) -> list[dict[str, str]]:
    payload: dict[str, Any] = {
        "query": query,
        "topic": "news" if _looks_time_sensitive_query(query) else "general",
        "search_depth": "advanced" if _should_use_advanced_search(query) else "basic",
        "max_results": max(1, limit),
        "include_answer": False,
        "include_raw_content": False,
        "auto_parameters": True,
    }
    if '"' in query:
        payload["exact_match"] = True

    resp = _request_with_retry(
        "POST",
        TAVILY_SEARCH_URL,
        headers={
            **COMMON_HEADERS,
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    items: list[dict[str, str]] = []
    for result in data.get("results") or []:
        title = _clean_html(str(result.get("title", "") or ""))
        snippet = _clean_html(str(result.get("content", "") or result.get("snippet", "") or ""))
        url = _normalize_result_url(str(result.get("url", "") or ""))
        if title or snippet:
            items.append({"title": title, "snippet": snippet, "url": url})
        if len(items) >= limit:
            break
    return items


def _search_bing_rss(query: str, limit: int) -> list[dict[str, str]]:
    resp = _request_with_retry(
        "GET",
        "https://www.bing.com/search",
        params={"q": query, "format": "rss", "mkt": "zh-CN"},
        headers=COMMON_HEADERS,
        timeout=SEARCH_TIMEOUT,
    )
    resp.raise_for_status()

    root = ET.fromstring(resp.text)
    items = []
    for node in root.findall(".//item"):
        title = _clean_html(node.findtext("title") or "")
        snippet = _clean_html(node.findtext("description") or "")
        url = _normalize_result_url(node.findtext("link") or "")
        if title or snippet:
            items.append({"title": title, "snippet": snippet, "url": url})
        if len(items) >= limit:
            break
    return items


def _search_bing_html(query: str, limit: int) -> list[dict[str, str]]:
    resp = _request_with_retry(
        "GET",
        "https://www.bing.com/search",
        params={"q": query, "mkt": "zh-CN"},
        headers=COMMON_HEADERS,
        timeout=SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    html_text = resp.text

    block_pattern = re.compile(
        r'<li class="b_algo".*?</li>',
        re.S,
    )
    title_pattern = re.compile(r"<h2><a[^>]*href=\"(?P<href>[^\"]+)\"[^>]*>(?P<title>.*?)</a></h2>", re.S)
    snippet_pattern = re.compile(r'<div class="b_caption"><p>(?P<snippet>.*?)</p>', re.S)

    items: list[dict[str, str]] = []
    for block in block_pattern.findall(html_text):
        title_match = title_pattern.search(block)
        if not title_match:
            continue
        snippet_match = snippet_pattern.search(block)
        items.append({
            "title": _clean_html(title_match.group("title") or ""),
            "snippet": _clean_html((snippet_match.group("snippet") if snippet_match else "") or ""),
            "url": _normalize_result_url(title_match.group("href") or ""),
        })
        if len(items) >= limit:
            break
    return items


def _search_duckduckgo_html(query: str, limit: int) -> list[dict[str, str]]:
    resp = _request_with_retry(
        "GET",
        "https://html.duckduckgo.com/html/",
        params={"q": query, "kl": "cn-zh"},
        headers=COMMON_HEADERS,
        timeout=SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    html_text = resp.text

    block_pattern = re.compile(
        r'<a[^>]*class="result__a"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>'
        r'(?:(?:(?!class="result__a").)*?<a[^>]*class="result__snippet"[^>]*>(?P<snippet>.*?)</a>)?',
        re.S,
    )

    items: list[dict[str, str]] = []
    for match in block_pattern.finditer(html_text):
        title = _clean_html(match.group("title") or "")
        snippet = _clean_html(match.group("snippet") or "")
        url = _normalize_result_url(match.group("href") or "")
        if title or snippet:
            items.append({"title": title, "snippet": snippet, "url": url})
        if len(items) >= limit:
            break
    return items


def _normalize_query_text(query: str) -> str:
    text = html.unescape(query or "")
    text = text.replace("\u3000", " ")
    text = re.sub(r"[“”\"'`]+", " ", text)
    text = re.sub(r"[，。！？、；：()\[\]{}<>]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _build_search_query_variants(query: str) -> list[str]:
    variants: list[str] = []

    def add(value: str):
        value = _normalize_query_text(value)
        if value and value not in variants:
            variants.append(value)

    add(query)

    compact = re.sub(r"\s+", "", query or "")
    if compact and compact != query:
        add(compact)

    if re.search(r"[\u4e00-\u9fff]", query or "") and len(compact) <= 24:
        add(f"\"{compact}\"")

    if _looks_time_sensitive_query(query):
        add(f"{query} 最新")

    return variants[:SEARCH_QUERY_VARIANT_LIMIT]


def _looks_time_sensitive_query(query: str) -> bool:
    text = (query or "").lower()
    return any(
        keyword in text
        for keyword in ("最新", "今天", "今日", "现在", "刚刚", "近期", "新闻", "价格", "股价", "汇率", "天气", "比分")
    )


def _should_use_advanced_search(query: str) -> bool:
    text = _normalize_query_text(query)
    if _looks_time_sensitive_query(text):
        return True
    if len(text) >= 12:
        return True
    return any(keyword in text.lower() for keyword in ("公司", "官网", "评测", "对比", "怎么样", "推荐"))


def _get_tavily_api_key() -> str:
    env_key = os.getenv("TAVILY_API_KEY", "").strip()
    if env_key:
        return env_key

    config = _load_runtime_search_config()
    search_cfg = config.get("search", {}) or {}
    return str(search_cfg.get("tavily_api_key", "") or "").strip()


_runtime_search_config_cache: dict[str, Any] | None = None
_runtime_search_config_ts: float = 0.0
_RUNTIME_SEARCH_CONFIG_TTL = 30.0  # 缓存 30 秒


def _load_runtime_search_config() -> dict[str, Any]:
    global _runtime_search_config_cache, _runtime_search_config_ts
    import time as _time
    now = _time.monotonic()
    if _runtime_search_config_cache is not None and (now - _runtime_search_config_ts) < _RUNTIME_SEARCH_CONFIG_TTL:
        return _runtime_search_config_cache

    config_path = resolve_app_path("config.yaml")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except Exception:
        config = {}

    local_path = config_path.with_name("config.local.yaml")
    if local_path.exists():
        try:
            with open(local_path, "r", encoding="utf-8") as f:
                local_config = yaml.safe_load(f) or {}
            config = _deep_merge_dict(config, local_config)
        except Exception:
            pass

    _runtime_search_config_cache = config
    _runtime_search_config_ts = now
    return config


def _deep_merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base or {})
    for key, value in (override or {}).items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _deep_merge_dict(result[key], value)
        else:
            result[key] = value
    return result


def _normalize_search_item(item: dict[str, str]) -> dict[str, str] | None:
    title = _clean_html(item.get("title", "") or "")
    snippet = _clean_html(item.get("snippet", "") or "")
    url = _normalize_result_url(item.get("url", "") or "")
    if not title and not snippet:
        return None
    return {
        "title": title,
        "snippet": snippet,
        "url": url,
    }


def _score_search_result(query: str, item: dict[str, str]) -> float:
    text = f'{item.get("title", "")} {item.get("snippet", "")}'.lower()
    normalized_query = _normalize_query_text(query).lower()
    if not text or not normalized_query:
        return 0.0

    terms = _extract_query_terms(normalized_query)
    if not terms:
        return 0.0

    matched_weight = 0.0
    total_weight = 0.0
    title_text = str(item.get("title", "")).lower()

    for term in terms:
        weight = _term_weight(term)
        total_weight += weight
        if term in text:
            matched_weight += weight
            if term in title_text:
                matched_weight += weight * 0.35

    score = matched_weight / max(total_weight, 1.0)
    if normalized_query in text:
        score += 0.8
    if normalized_query in title_text:
        score += 0.5
    return round(score, 4)


def _extract_query_terms(query: str) -> list[str]:
    terms: list[str] = []

    def add(term: str):
        term = term.strip().lower()
        if len(term) >= 2 and term not in terms:
            terms.append(term)

    groups = re.findall(r"[\u4e00-\u9fff]+|[a-z0-9][a-z0-9._+-]*", query.lower())
    for group in groups:
        add(group)
        if re.fullmatch(r"[\u4e00-\u9fff]+", group):
            if len(group) >= 4:
                for idx in range(len(group) - 1):
                    add(group[idx:idx + 2])
            if len(group) >= 6:
                for idx in range(len(group) - 2):
                    add(group[idx:idx + 3])

    return terms


def _term_weight(term: str) -> float:
    if re.fullmatch(r"[a-z0-9._+-]+", term):
        return 2.2 if len(term) >= 3 else 1.2
    return min(3.6, 1.0 + len(term) * 0.3)


def _clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _normalize_result_url(url: str) -> str:
    url = html.unescape(url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = f"https:{url}"

    parsed = urlparse(url)
    if "duckduckgo.com" in parsed.netloc:
        redirected = parse_qs(parsed.query).get("uddg")
        if redirected:
            return unquote(redirected[0])
    return url


def _extract_chat_text(resp: Any) -> str | None:
    try:
        content = resp.choices[0].message.content
    except Exception:
        return None

    return _flatten_content(content)


def _extract_responses_json_text(data: dict[str, Any]) -> str | None:
    """从 responses API 的 JSON 响应中提取文本。"""
    # output_text 快捷字段
    output_text = data.get("output_text")
    if output_text:
        return str(output_text).strip()
    # output 列表
    chunks: list[str] = []
    for item in data.get("output") or []:
        for part in item.get("content") or []:
            if part.get("type") in {"output_text", "text"}:
                t = part.get("text") or ""
                if t:
                    chunks.append(str(t).strip())
    text = "\n".join(x for x in chunks if x)
    return text.strip() or None


def _extract_responses_text(resp: Any) -> str | None:
    output_text = getattr(resp, "output_text", None)
    if output_text:
        return str(output_text).strip()

    output = getattr(resp, "output", None) or []
    chunks: list[str] = []
    for item in output:
        content = getattr(item, "content", None) or []
        for part in content:
            part_type = getattr(part, "type", "")
            if part_type in {"output_text", "text"}:
                text = getattr(part, "text", "") or ""
                if text:
                    chunks.append(str(text).strip())
    text = "\n".join(x for x in chunks if x)
    return text.strip() or None


def _extract_openai_json_text(data: dict[str, Any]) -> str | None:
    choices = data.get("choices") or []
    if not choices:
        return None
    content = ((choices[0] or {}).get("message") or {}).get("content")
    return _flatten_content(content)


def _flatten_content(content: Any) -> str | None:
    if isinstance(content, str):
        text = content.strip()
        return text or None

    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item.strip())
                continue
            if not isinstance(item, dict):
                continue
            item_type = item.get("type", "")
            if item_type in {"text", "output_text"}:
                text = item.get("text") or item.get("content") or ""
                if text:
                    chunks.append(str(text).strip())
                continue
            if "text" in item and item["text"]:
                chunks.append(str(item["text"]).strip())
        text = "\n".join(x for x in chunks if x)
        return text.strip() or None

    return None
