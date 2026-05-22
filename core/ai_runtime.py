"""
AI 运行时能力

提供给搜搜和 AI 辅助流程复用的轻量能力：
- 选择 AI 后端
- 通过模型判断品牌是否被提及
- 通过模型识别截图中的品牌
"""

import base64
import io
import json
import math
import re

from platforms.api_client import (
    PLATFORM_API_CONFIG,
    get_platform_api_key,
    get_platform_last_error,
    model_supports_image_input,
    platform_has_configured_access,
    send_platform_chat_messages,
)


AI_BACKEND_CODES = [
    code for code, cfg in PLATFORM_API_CONFIG.items()
    if cfg.get("base_url")
]


def get_ai_backend(config: dict) -> tuple[str, str, str]:
    """
    返回 (platform_code, api_key, model)。
    优先读取 ai_assistant 配置，其次回退到第一个可用平台。
    """
    assistant_cfg = config.get("ai_assistant", {})

    preferred = assistant_cfg.get("platform", "").strip()
    if preferred in AI_BACKEND_CODES and platform_has_configured_access(config, preferred):
        api_key = get_platform_api_key(config, preferred)
        model = (
            assistant_cfg.get("model", "").strip()
            or config.get("platforms", {}).get(preferred, {}).get("api_model", "").strip()
            or str((PLATFORM_API_CONFIG.get(preferred) or {}).get("default_model") or "").strip()
        )
        if model:
            return preferred, api_key, model

    for code in AI_BACKEND_CODES:
        if not platform_has_configured_access(config, code):
            continue
        api_key = get_platform_api_key(config, code)
        model = (
            config.get("platforms", {}).get(code, {}).get("api_model", "").strip()
            or str((PLATFORM_API_CONFIG.get(code) or {}).get("default_model") or "").strip()
        )
        if model:
            return code, api_key, model

    raise ValueError("未找到可用的 AI 平台，请先配置可用 Key，或启用本地模型")


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("AI 返回为空")

    candidates = [text]
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            candidates.append("\n".join(lines[1:-1]).strip())

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except Exception:
            pass

    start = text.find("{")
    if start == -1:
        raise ValueError("AI 返回里没有 JSON")

    snippet = text[start:]

    # 先整体解析（JSON后有尾部文字的常见情况）
    try:
        return json.loads(snippet)
    except Exception:
        pass

    # 用 rfind 定位最后一个 }，O(n) 而非逐位逆向扫描
    end = snippet.rfind("}")
    if end == -1:
        raise ValueError("AI 返回 JSON 解析失败")
    try:
        return json.loads(snippet[:end + 1])
    except Exception:
        raise ValueError("AI 返回 JSON 解析失败")


def judge_brand_mention(config: dict, platform_name: str, keyword: str, brand: str, answer_text: str) -> tuple[bool, str]:
    """
    让 AI 判断回答内容里是否出现品牌名。
    返回 (mentioned, evidence)
    """
    platform_code, api_key, model = get_ai_backend(config)
    backend_cfg = PLATFORM_API_CONFIG.get(platform_code, {})
    base_url = backend_cfg.get("base_url")
    if not base_url:
        raise ValueError(f"{platform_code} 暂不支持 AI 判断")

    system_prompt = (
        "你负责判断 AI 回答内容里是否明确提到了目标品牌。"
        "只返回 JSON，格式必须是 "
        "{\"mentioned\": true/false, \"evidence\": \"短证据\", \"confidence\": 0.0}。"
        "如果品牌只出现在用户问题里、不在回答里，必须返回 false。"
        "判断标准宁可保守，不要把模糊相似词当成命中。"
    )
    user_prompt = (
        f"平台: {platform_name}\n"
        f"用户关键词: {keyword}\n"
        f"目标品牌: {brand}\n"
        "下面是回答正文，请只根据回答正文判断：\n"
        f"{(answer_text or '')[:12000]}"
    )
    content = send_platform_chat_messages(
        platform_code,
        api_key,
        model,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    if content is None:
        detail = get_platform_last_error(platform_code)
        if detail:
            raise ValueError(f"{platform_code} 接口调用失败：{detail}")
        raise ValueError(f"{platform_code} 接口没有返回内容")
    result = _extract_json(content)
    mentioned = bool(result.get("mentioned", False))
    evidence = str(result.get("evidence", "")).strip()
    return mentioned, evidence


def _normalize_brand_key(value: str) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"[\s\-_/|｜()（）\[\]【】,:：.]+", "", text)


def _resize_to_fit(img, max_width: int, max_height: int):
    if img.width <= max_width and img.height <= max_height:
        return img
    ratio = min(max_width / max(1, img.width), max_height / max(1, img.height))
    size = (
        max(1, int(img.width * ratio)),
        max(1, int(img.height * ratio)),
    )
    return img.resize(size)


def _enhance_recognition_panel(img):
    from PIL import ImageEnhance, ImageFilter

    if img.mode != "RGB":
        img = img.convert("RGB")
    img = ImageEnhance.Contrast(img).enhance(1.08)
    img = ImageEnhance.Sharpness(img).enhance(1.18)
    img = img.filter(ImageFilter.SHARPEN)
    return img


def _build_cropped_panels(img):
    panels = [img.copy()]

    if img.height >= img.width * 1.45:
        overlap = 0.14
        step = img.height / 3
        crop_h = min(img.height, int(step * (1 + overlap)))
        for idx in range(3):
            top = int(max(0, min(img.height - crop_h, idx * step - crop_h * overlap / 2)))
            bottom = min(img.height, top + crop_h)
            panels.append(img.crop((0, top, img.width, bottom)))
    elif img.width >= img.height * 1.45:
        overlap = 0.14
        step = img.width / 3
        crop_w = min(img.width, int(step * (1 + overlap)))
        for idx in range(3):
            left = int(max(0, min(img.width - crop_w, idx * step - crop_w * overlap / 2)))
            right = min(img.width, left + crop_w)
            panels.append(img.crop((left, 0, right, img.height)))

    return panels


def _compose_recognition_image(image_path: str) -> bytes:
    from PIL import Image

    with Image.open(image_path) as img:
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        panels = _build_cropped_panels(img)
        if len(panels) == 1:
            panel = _enhance_recognition_panel(panels[0])
            panel = _resize_to_fit(panel, 1280, 1280)
            buffer = io.BytesIO()
            panel.save(buffer, format="JPEG", quality=84, optimize=True)
            return buffer.getvalue()

        tile_width = 880
        tile_height = 680
        padding = 20
        cols = 2
        rows = int(math.ceil(len(panels) / cols))

        canvas = Image.new(
            "RGB",
            (
                cols * tile_width + (cols + 1) * padding,
                rows * tile_height + (rows + 1) * padding,
            ),
            color=(250, 250, 250),
        )

        for idx, panel in enumerate(panels):
            enhanced = _enhance_recognition_panel(panel)
            fitted = _resize_to_fit(enhanced, tile_width, tile_height)
            x = padding + (idx % cols) * (tile_width + padding)
            y = padding + (idx // cols) * (tile_height + padding)
            offset_x = x + (tile_width - fitted.width) // 2
            offset_y = y + (tile_height - fitted.height) // 2
            canvas.paste(fitted, (offset_x, offset_y))

        buffer = io.BytesIO()
        canvas.save(buffer, format="JPEG", quality=84, optimize=True)
        return buffer.getvalue()


def detect_brands_from_image(
    config: dict,
    image_path: str,
    candidate_brands: list[str],
    *,
    hint_text: str = "",
) -> tuple[list[str], str]:
    """
    让 AI 从截图里识别品牌名。
    返回 (matched_brands, summary)
    """
    platform_code, api_key, model = get_ai_backend(config)
    backend_cfg = PLATFORM_API_CONFIG.get(platform_code, {})
    base_url = backend_cfg.get("base_url")
    if not base_url:
        raise ValueError(f"{platform_code} 暂不支持识别模式")
    if not model_supports_image_input(platform_code, model):
        raise ValueError(
            f"{platform_code} 当前模型 {model} 不支持图片输入，请切换到支持视觉的模型后重试"
        )

    if not candidate_brands:
        return [], ""

    image_bytes = _compose_recognition_image(image_path)
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    system_prompt = (
        "你负责识别截图里实际出现的品牌名。"
        "先通读图片里的文字，再做品牌判断。"
        "只允许从候选品牌列表中选择，不要自己发明品牌。"
        "禁止根据行业上下文、标题含义或常识猜测，必须以图片里能看见的文字为准。"
        "如果图片里出现的是候选品牌的别名、英文名、大小写变体，也可以判定为命中。"
        "只返回 JSON，格式必须是 "
        "{\"brands\": [\"品牌1\"], \"summary\": \"一句话总结\"}。"
        "如果一个都没出现，brands 返回空数组。"
    )
    user_parts = []
    if hint_text.strip():
        user_parts.append(f"当前识别上下文：\n{hint_text.strip()}")
    user_parts.append(
        "候选品牌列表如下，请只从里面选择：\n"
        + "\n".join(f"- {brand}" for brand in candidate_brands)
    )
    user_parts.append(
        "判断要求：\n"
        "1. 只选图片中实际可见的品牌。\n"
        "2. 不确定就不要选。\n"
        "3. summary 用一句话说明品牌出现的位置或未命中的原因。"
    )
    user_text = "\n\n".join(user_parts)
    content = send_platform_chat_messages(
        platform_code,
        api_key,
        model,
        [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            },
        ],
    )
    if content is None:
        detail = get_platform_last_error(platform_code)
        if detail:
            raise ValueError(f"{platform_code} 接口调用失败：{detail}")
        raise ValueError(f"{platform_code} 接口没有返回内容")
    result = _extract_json(content)

    brands = result.get("brands", []) or []
    normalized = []
    known = {_normalize_brand_key(brand): brand for brand in candidate_brands}
    for brand in brands:
        key = _normalize_brand_key(brand)
        if key in known and known[key] not in normalized:
            normalized.append(known[key])

    summary = str(result.get("summary", "")).strip()
    return normalized, summary
