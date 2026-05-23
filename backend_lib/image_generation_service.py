"""Image generation adapter for OpenAI-compatible and Gemini endpoints."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests


REQUEST_TIMEOUT_SECONDS = 180
MAX_PROMPT_CHARS = 8000
OPENAI_IMAGE_ENDPOINT = "https://api.openai.com/v1/images/generations"
GEMINI_GENERATE_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
OPENAI_DEFAULT_MODEL = "gpt-image-2"
GEMINI_DEFAULT_MODEL = "nano-banana-2"
DEFAULT_IMAGE_MODEL_ID = "gpt_image_2"

SUPPORTED_PROVIDERS = {"openai", "gemini"}
IMAGE_GENERATION_MODEL_DEFS = {
    "nano_banana_2": {
        "id": "nano_banana_2",
        "label": "Nano Banana 2",
        "provider": "gemini",
        "base_url": GEMINI_GENERATE_BASE_URL,
        "model": GEMINI_DEFAULT_MODEL,
    },
    "gpt_image_2": {
        "id": "gpt_image_2",
        "label": "GPT Image 2",
        "provider": "openai",
        "base_url": OPENAI_IMAGE_ENDPOINT,
        "model": OPENAI_DEFAULT_MODEL,
    },
}
SUPPORTED_ASPECT_RATIOS = {"1:1", "3:2", "2:3", "4:3", "3:4", "4:5", "5:4", "16:9", "9:16", "21:9"}
SUPPORTED_CANVAS_QUALITIES = {"standard", "hd", "ultra", "auto"}
SUPPORTED_GENERATION_QUALITIES = {"auto", "low", "medium", "high"}
SUPPORTED_OUTPUT_FORMATS = {"png", "jpeg", "webp"}

OPENAI_MIN_PIXELS = 655_360
OPENAI_MAX_PIXELS = 8_294_400
OPENAI_MAX_EDGE = 3840
OPENAI_SIZE_MULTIPLE = 16


class ImageGenerationError(RuntimeError):
    """Raised when an upstream image generation request fails."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _raw_model_config(raw: Any, model_id: str) -> dict[str, Any]:
    cfg = raw if isinstance(raw, dict) else {}
    if isinstance(cfg.get("models"), dict) and isinstance(cfg["models"].get(model_id), dict):
        return cfg["models"][model_id]
    if isinstance(cfg.get(model_id), dict):
        return cfg[model_id]

    # Backward compatibility for the first implementation, which stored a single
    # provider-level config instead of per-model backend configs.
    legacy_provider = _text(cfg.get("provider")).lower()
    expected_provider = _text((IMAGE_GENERATION_MODEL_DEFS.get(model_id) or {}).get("provider"))
    if legacy_provider and legacy_provider == expected_provider:
        return cfg
    return {}


def resolve_image_generation_model_config(raw: Any, model_id: str) -> dict[str, str]:
    normalized_model_id = _text(model_id) or DEFAULT_IMAGE_MODEL_ID
    defaults = IMAGE_GENERATION_MODEL_DEFS.get(normalized_model_id)
    if not defaults:
        raise ImageGenerationError("不支持的生图模型")
    cfg = _raw_model_config(raw, normalized_model_id)
    provider = _text(defaults.get("provider"))
    return {
        "id": normalized_model_id,
        "label": _text(defaults.get("label")),
        "provider": provider,
        "base_url": _text(cfg.get("base_url") or cfg.get("url")) or _text(defaults.get("base_url")),
        "api_key": _text(cfg.get("api_key")),
        "model": _text(cfg.get("model")) or _text(defaults.get("model")),
    }


def normalize_image_generation_config(raw: Any) -> dict[str, Any]:
    """Normalize backend-owned per-model image generation config."""
    models: dict[str, dict[str, str]] = {}
    for model_id in IMAGE_GENERATION_MODEL_DEFS:
        cfg = resolve_image_generation_model_config(raw, model_id)
        models[model_id] = {
            "base_url": cfg["base_url"],
            "api_key": cfg["api_key"],
            "model": cfg["model"],
        }
    return {"models": models}


def image_generation_config_to_api(raw: Any, mask_secret) -> dict[str, Any]:
    models = []
    for model_id in IMAGE_GENERATION_MODEL_DEFS:
        cfg = resolve_image_generation_model_config(raw, model_id)
        models.append({
            "id": cfg["id"],
            "label": cfg["label"],
            "provider": cfg["provider"],
            "model": cfg["model"],
            "configured": bool(cfg["api_key"] and cfg["base_url"] and cfg["model"]),
            "has_key": bool(cfg["api_key"]),
        })
    return {
        "models": models,
        "default_model_id": DEFAULT_IMAGE_MODEL_ID,
    }


def normalize_image_generation_request(raw: Any) -> dict[str, str]:
    payload = raw if isinstance(raw, dict) else {}
    prompt = _text(payload.get("prompt"))
    if not prompt:
        raise ImageGenerationError("请先填写提示词")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ImageGenerationError(f"提示词过长，请控制在 {MAX_PROMPT_CHARS} 字以内")

    aspect_ratio = _text(payload.get("aspect_ratio") or payload.get("aspectRatio")) or "1:1"
    if aspect_ratio not in SUPPORTED_ASPECT_RATIOS:
        aspect_ratio = "1:1"

    canvas_quality = _text(payload.get("canvas_quality") or payload.get("canvasQuality")).lower() or "standard"
    if canvas_quality not in SUPPORTED_CANVAS_QUALITIES:
        canvas_quality = "standard"

    generation_quality = _text(payload.get("quality") or payload.get("generation_quality") or payload.get("generationQuality")).lower() or "auto"
    if generation_quality not in SUPPORTED_GENERATION_QUALITIES:
        generation_quality = "auto"

    output_format = _text(payload.get("output_format") or payload.get("outputFormat")).lower() or "png"
    if output_format == "jpg":
        output_format = "jpeg"
    if output_format not in SUPPORTED_OUTPUT_FORMATS:
        output_format = "png"

    return {
        "model_id": _text(payload.get("model_id") or payload.get("modelId")) or DEFAULT_IMAGE_MODEL_ID,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "canvas_quality": canvas_quality,
        "quality": generation_quality,
        "output_format": output_format,
    }


def _parse_aspect_ratio(value: str) -> tuple[int, int]:
    try:
        left, right = value.split(":", 1)
        width = max(1, int(left))
        height = max(1, int(right))
        return width, height
    except Exception:
        return 1, 1


def _round_to_multiple(value: float, multiple: int = OPENAI_SIZE_MULTIPLE) -> int:
    return max(multiple, int(round(value / multiple)) * multiple)


def openai_size_from_options(aspect_ratio: str, canvas_quality: str) -> str:
    if canvas_quality == "auto":
        return "auto"
    ratio_width, ratio_height = _parse_aspect_ratio(aspect_ratio)
    ratio = ratio_width / ratio_height
    short_edge_by_quality = {
        "standard": 1024,
        "hd": 1536,
        "ultra": 2048,
    }
    short_edge = short_edge_by_quality.get(canvas_quality, 1024)
    if ratio >= 1:
        height = short_edge
        width = short_edge * ratio
    else:
        width = short_edge
        height = short_edge / ratio

    width = _round_to_multiple(width)
    height = _round_to_multiple(height)

    while (
        width > OPENAI_MAX_EDGE
        or height > OPENAI_MAX_EDGE
        or width * height > OPENAI_MAX_PIXELS
    ):
        width = _round_to_multiple(width * 0.96)
        height = _round_to_multiple(height * 0.96)

    while width * height < OPENAI_MIN_PIXELS:
        width = _round_to_multiple(width * 1.05)
        height = _round_to_multiple(height * 1.05)

    return f"{width}x{height}"


def gemini_image_size_from_canvas_quality(canvas_quality: str) -> str:
    return {
        "standard": "1K",
        "hd": "2K",
        "ultra": "4K",
    }.get(canvas_quality, "1K")


def _append_query_param(url: str, key: str, value: str) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if key not in query and value:
        query[key] = value
    return urlunparse(parsed._replace(query=urlencode(query)))


def _openai_endpoint(base_url: str) -> str:
    value = (_text(base_url) or OPENAI_IMAGE_ENDPOINT).rstrip("/")
    if value.endswith("/images/generations"):
        return value
    if value.endswith("/v1"):
        return value + "/images/generations"
    if "/images/generations" in value:
        return value
    return value + "/v1/images/generations"


def _gemini_endpoint(base_url: str, model: str, api_key: str) -> str:
    value = _text(base_url) or GEMINI_GENERATE_BASE_URL
    encoded_model = model.strip()
    endpoint = value.replace("{model}", encoded_model).replace("{api_key}", api_key)
    if ":generateContent" not in endpoint:
        endpoint = endpoint.rstrip("/") + f"/models/{encoded_model}:generateContent"
    if "{api_key}" not in value:
        endpoint = _append_query_param(endpoint, "key", api_key)
    return endpoint


def _response_error_message(response: requests.Response) -> str:
    text = response.text[:1200]
    try:
        data = response.json()
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                message = _text(error.get("message"))
                if message:
                    return message
            message = _text(data.get("message"))
            if message:
                return message
    except Exception:
        pass
    return text or f"HTTP {response.status_code}"


def _collect_openai_images(data: Any, output_format: str) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    payload = data if isinstance(data, dict) else {}
    mime_type = f"image/{output_format}"
    if output_format == "jpeg":
        mime_type = "image/jpeg"
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        b64_json = _text(item.get("b64_json"))
        url = _text(item.get("url"))
        image: dict[str, Any] = {
            "mime_type": _text(item.get("mime_type")) or mime_type,
            "revised_prompt": _text(item.get("revised_prompt")),
        }
        if b64_json:
            image["b64_json"] = b64_json
        if url:
            image["url"] = url
        if image.get("b64_json") or image.get("url"):
            images.append(image)
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "image_generation_call":
            continue
        b64_json = _text(item.get("result"))
        if b64_json:
            images.append({
                "b64_json": b64_json,
                "mime_type": mime_type,
                "revised_prompt": _text(item.get("revised_prompt")),
            })
    return images


def _collect_gemini_images(data: Any) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    payload = data if isinstance(data, dict) else {}
    for candidate in payload.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content") if isinstance(candidate.get("content"), dict) else {}
        for part in content.get("parts") or []:
            if not isinstance(part, dict):
                continue
            inline_data = part.get("inlineData") or part.get("inline_data")
            if not isinstance(inline_data, dict):
                continue
            b64_json = _text(inline_data.get("data"))
            if not b64_json:
                continue
            images.append({
                "b64_json": b64_json,
                "mime_type": _text(inline_data.get("mimeType") or inline_data.get("mime_type")) or "image/png",
                "revised_prompt": "",
            })
    return images


def _with_data_urls(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for image in images:
        item = dict(image)
        b64_json = _text(item.get("b64_json"))
        mime_type = _text(item.get("mime_type")) or "image/png"
        if b64_json:
            item["data_url"] = f"data:{mime_type};base64,{b64_json}"
        result.append(item)
    return result


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any]) -> Any:
    response = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    if response.status_code >= 400:
        raise ImageGenerationError(_response_error_message(response))
    try:
        return response.json()
    except Exception as exc:
        raise ImageGenerationError(f"接口返回了非 JSON 响应：{exc}") from exc


def generate_image_from_config(raw_config: Any, raw_payload: Any) -> dict[str, Any]:
    request = normalize_image_generation_request(raw_payload)
    cfg = resolve_image_generation_model_config(raw_config, request["model_id"])
    provider = cfg["provider"]
    api_key = cfg["api_key"]
    model = cfg["model"]
    if not api_key:
        raise ImageGenerationError("请先配置生图 API Key")
    if not model:
        raise ImageGenerationError("请先配置生图模型")

    if provider == "gemini":
        endpoint = _gemini_endpoint(cfg["base_url"], model, api_key)
        payload = {
            "contents": [{"role": "user", "parts": [{"text": request["prompt"]}]}],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {
                    "aspectRatio": request["aspect_ratio"],
                    "imageSize": gemini_image_size_from_canvas_quality(request["canvas_quality"]),
                },
            },
        }
        data = _post_json(
            endpoint,
            {
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            payload,
        )
        images = _with_data_urls(_collect_gemini_images(data))
        if not images:
            raise ImageGenerationError("Gemini 没有返回图片，请检查模型是否支持图片输出")
        return {
            "ok": True,
            "model_id": cfg["id"],
            "label": cfg["label"],
            "provider": provider,
            "model": model,
            "images": images,
            "request": {
                "aspect_ratio": request["aspect_ratio"],
                "canvas_quality": request["canvas_quality"],
                "quality": request["quality"],
                "output_format": request["output_format"],
            },
        }

    size = openai_size_from_options(request["aspect_ratio"], request["canvas_quality"])
    endpoint = _openai_endpoint(cfg["base_url"])
    payload = {
        "model": model,
        "prompt": request["prompt"],
        "n": 1,
        "size": size,
        "quality": request["quality"],
        "output_format": request["output_format"],
    }
    data = _post_json(
        endpoint,
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        payload,
    )
    images = _with_data_urls(_collect_openai_images(data, request["output_format"]))
    if not images:
        raise ImageGenerationError("接口没有返回图片数据")
    return {
        "ok": True,
        "model_id": cfg["id"],
        "label": cfg["label"],
        "provider": provider,
        "model": model,
        "images": images,
        "request": {
            "aspect_ratio": request["aspect_ratio"],
            "canvas_quality": request["canvas_quality"],
            "quality": request["quality"],
            "output_format": request["output_format"],
            "size": size,
        },
    }
