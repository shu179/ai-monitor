"""
截图增强工具

- 为命中截图添加顶部摘要
- 保持通知图为单张主截图，避免额外预览造成视觉割裂
- 支持从 config.yaml 读取自定义装饰主题
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from core.app_paths import resolve_app_path
from core.config_watcher import load_config
from core.time_utils import local_now


PLATFORM_LABELS = {
    "local_model": "本地模型",
    "doubao": "豆包",
    "deepseek": "DeepSeek",
    "ark_deepseek": "方舟 DeepSeek",
    "kimi": "Kimi",
    "yuanbao": "元宝",
    "tongyi": "通义",
    "wenxin": "文心一言",
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "gemini": "Gemini",
    "perplexity": "Perplexity",
}

PLATFORM_ID_ALIASES = {
    "doubao": "doubao",
    "豆包": "doubao",
    "deepseek": "deepseek",
    "DeepSeek": "deepseek",
    "ark_deepseek": "ark_deepseek",
    "ark deepseek": "ark_deepseek",
    "方舟 DeepSeek": "ark_deepseek",
    "kimi": "kimi",
    "Kimi": "kimi",
    "yuanbao": "yuanbao",
    "元宝": "yuanbao",
    "tongyi": "tongyi",
    "通义": "tongyi",
    "通义千问": "tongyi",
    "qwen": "tongyi",
    "wenxin": "wenxin",
    "文心": "wenxin",
    "文心一言": "wenxin",
    "ernie": "wenxin",
    "chatgpt": "chatgpt",
    "ChatGPT": "chatgpt",
    "claude": "claude",
    "Claude": "claude",
    "gemini": "gemini",
    "Gemini": "gemini",
    "perplexity": "perplexity",
    "Perplexity": "perplexity",
}

PLATFORM_LOGO_STYLES = {
    "doubao": {"fill": "#FFF5EB", "label": "豆"},
    "deepseek": {"fill": "#EEF4FF", "label": "DS"},
    "ark_deepseek": {"fill": "#F3F4F6", "label": "A"},
    "kimi": {"fill": "#F3F4F6", "label": "K"},
    "yuanbao": {"fill": "#ECFEF9", "label": "元"},
    "tongyi": {"fill": "#F6F1FF", "label": "通"},
    "wenxin": {"fill": "#EEF4FF", "label": "文"},
    "chatgpt": {"fill": "#ECFDF5", "label": "GPT"},
    "claude": {"fill": "#FFF7ED", "label": "C"},
    "gemini": {"fill": "#EEF4FF", "label": "G"},
    "perplexity": {"fill": "#F3F4F6", "label": "P"},
}

PLATFORM_LOGO_ASSETS = {
    "doubao": "assets/logos/doubao.png",
    "deepseek": "assets/logos/deepseek.png",
    "ark_deepseek": "assets/logos/ark_deepseek.png",
    "kimi": "assets/logos/kimi.png",
    "yuanbao": "assets/logos/yuanbao.png",
    "tongyi": "assets/logos/tongyi.png",
    "wenxin": "assets/logos/wenxin.png",
    "chatgpt": "assets/logos/chatgpt.png",
    "claude": "assets/logos/claude.png",
    "gemini": "assets/logos/gemini.png",
    "perplexity": "assets/logos/perplexity.png",
}

DEFAULT_THEME = {
    "enabled": True,
    "title": "{platform}",
    "subtitle": "{brand}",
    "footer": "MONITOR SNAPSHOT",
    "show_timestamp": True,
    "show_footer": True,
    "draw_highlight_boxes": True,
    "accent_color": "#14C7F3",
    "background": {
        "start": "#FCFDFF",
        "end": "#F7FAFF",
    },
    "card": {
        "surface": "#FFFFFF",
        "border": "#D7E3F4",
        "shadow": "#10213A26",
    },
    "header": {
        "start": "#173A43",
        "end": "#14C7F3",
    },
    "text": {
        "header_primary": "#F8FBFF",
        "header_secondary": "#D8E4FF",
        "body_primary": "#0F172A",
        "body_secondary": "#5B6B84",
        "chip_text": "#0F172A",
        "footer": "#6B7A90",
    },
    "chip": {
        "fill": "#FFFFFF",
        "border": "#D6E0F5",
    },
    "layout": {
        "outer_padding": 28,
        "inner_padding": 18,
        "header_height": 152,
        "radius": 28,
        "image_radius": 22,
        "shadow_blur": 18,
        "shadow_offset_y": 10,
    },
}


def get_default_decoration_theme() -> dict:
    """返回截图装饰默认主题。"""
    return _deep_merge(DEFAULT_THEME, {})


def get_decoration_theme(config: dict | None = None) -> dict:
    """从配置中读取截图装饰主题，并与默认值合并。"""
    theme = get_default_decoration_theme()
    user_theme = ((config or {}).get("screenshot") or {}).get("decoration") or {}
    if isinstance(user_theme, dict):
        theme = _deep_merge(theme, user_theme)
    return theme


def decorate_screenshot(
    image_path: str,
    *,
    platform_name: str,
    brand: str,
    keyword: str = "",
    preview_png: bytes | None = None,
    preview_boxes: list[dict] | None = None,
    preview_scale: tuple[float, float] = (1.0, 1.0),
    draw_boxes_on_main: bool = True,
) -> dict:
    """为截图添加顶部标题，保持通知图为单张截图。"""
    theme = _load_theme()

    with Image.open(image_path) as main_img:
        if main_img.mode != "RGB":
            main_img = main_img.convert("RGB")
        main_img = main_img.copy()

    boxes = list(preview_boxes or [])
    image_changed = False
    if draw_boxes_on_main and boxes and theme.get("draw_highlight_boxes", True):
        _draw_highlight_boxes(
            main_img,
            boxes,
            scale_x=preview_scale[0],
            scale_y=preview_scale[1],
            accent_color=str(theme.get("accent_color", DEFAULT_THEME["accent_color"])),
        )
        image_changed = True

    if not theme.get("enabled", True):
        if image_changed:
            _save_jpeg(main_img, image_path)
        return {
            "preview_available": False,
            "highlight_count": len(boxes),
            "generated_at": local_now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    layout = theme.get("layout", {})
    outer_padding = _clamp_int(layout.get("outer_padding"), 28, minimum=12, maximum=80)
    inner_padding = _clamp_int(layout.get("inner_padding"), 18, minimum=8, maximum=48)
    header_h = _clamp_int(layout.get("header_height"), 136, minimum=88, maximum=240)
    radius = _clamp_int(layout.get("radius"), 28, minimum=12, maximum=48)
    image_radius = _clamp_int(layout.get("image_radius"), 22, minimum=8, maximum=36)
    shadow_blur = _clamp_int(layout.get("shadow_blur"), 18, minimum=0, maximum=32)
    shadow_offset_y = _clamp_int(layout.get("shadow_offset_y"), 10, minimum=0, maximum=24)
    footer_h = 34 if theme.get("show_footer", True) else 0
    has_keyword_chip = bool(keyword)
    if has_keyword_chip:
        header_h = max(header_h, 162)
    card_w = max(main_img.width + inner_padding * 2, 880)
    card_h = header_h + inner_padding + main_img.height + inner_padding + footer_h
    canvas_w = card_w + outer_padding * 2
    canvas_h = card_h + outer_padding * 2

    background = theme.get("background", {})
    card_colors = theme.get("card", {})
    header_colors = theme.get("header", {})
    text_colors = theme.get("text", {})
    chip_colors = theme.get("chip", {})
    accent = str(theme.get("accent_color", DEFAULT_THEME["accent_color"]))

    canvas = _create_linear_gradient(
        (canvas_w, canvas_h),
        str(background.get("start", DEFAULT_THEME["background"]["start"])),
        str(background.get("end", DEFAULT_THEME["background"]["end"])),
        horizontal=False,
    )
    canvas = canvas.convert("RGBA")
    _add_soft_glow(canvas, accent, center=(canvas_w - outer_padding * 2, outer_padding))

    card_x = outer_padding
    card_y = outer_padding
    card_bbox = (card_x, card_y, card_x + card_w, card_y + card_h)

    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    _draw_shadow(
        overlay,
        card_bbox,
        radius=radius,
        color=str(card_colors.get("shadow", DEFAULT_THEME["card"]["shadow"])),
        blur=shadow_blur,
        offset_y=shadow_offset_y,
    )
    card_layer = _build_supersampled_roundrect(
        (card_w, card_h),
        radius=radius,
        fill=str(card_colors.get("surface", DEFAULT_THEME["card"]["surface"])),
        outline=str(card_colors.get("border", DEFAULT_THEME["card"]["border"])),
        outline_width=1,
    )
    overlay.paste(card_layer, (card_x, card_y), card_layer)

    header_img = _create_linear_gradient(
        (card_w, header_h),
        str(header_colors.get("start", DEFAULT_THEME["header"]["start"])),
        str(header_colors.get("end", DEFAULT_THEME["header"]["end"])),
        horizontal=True,
    ).convert("RGBA")
    header_mask = _build_header_mask(card_w, header_h, radius)
    overlay.paste(header_img, (card_x, card_y), header_mask)
    canvas.alpha_composite(overlay)

    draw = ImageDraw.Draw(canvas)
    title_font = _font(38, bold=True)
    chip_font = _font(13, bold=True)
    footer_font = _font(13, bold=False)

    badge_size = 68
    title_gap = 18
    time_badge_w = 178
    left_group_shift = 8
    right_group_shift = 10
    badge_x = card_x + inner_padding + left_group_shift
    badge_y = card_y + max(18, (header_h - badge_size) // 2)
    time_badge_x = card_x + card_w - inner_padding - time_badge_w - right_group_shift
    _draw_platform_logo(
        canvas,
        platform_name=platform_name,
        box=(badge_x, badge_y, badge_x + badge_size, badge_y + badge_size),
    )
    draw = ImageDraw.Draw(canvas)

    title_x = badge_x + badge_size + title_gap
    normalized_platform = _normalize_platform_name(platform_name)
    context = {
        "platform": PLATFORM_LABELS.get(normalized_platform, platform_name or "未知平台"),
        "platform_raw": normalized_platform or platform_name or "",
        "brand": brand or "未命名品牌",
        "keyword": keyword or "未填写关键词",
        "time": local_now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    title = _render_template(str(theme.get("title", DEFAULT_THEME["title"])), context)
    subtitle = _render_template(str(theme.get("subtitle", DEFAULT_THEME["subtitle"])), context)
    title_line = f"{title}  ·  {subtitle}" if subtitle else title
    title_box = draw.textbbox((0, 0), title_line, font=title_font)
    title_top_offset = title_box[1]
    title_height = title_box[3] - title_box[1]
    title_text_y = badge_y + (badge_size - title_height) / 2 - title_top_offset - 2
    _draw_text_crisp(
        draw,
        (title_x, title_text_y),
        title_line,
        fill=str(text_colors.get("header_primary", DEFAULT_THEME["text"]["header_primary"])),
        font=title_font,
    )

    if theme.get("show_timestamp", True):
        _draw_time_badge(
            canvas,
            box=(time_badge_x, badge_y, time_badge_x + time_badge_w, badge_y + badge_size),
            time_text=context["time"],
            label_font=_font(11, bold=True),
            value_font=_font(15, bold=True),
        )

    if keyword:
        chip_y = title_text_y + title_height + 12
        chip_x = title_x
        platform_chip = _trim_text(context["platform"], 10)
        chip_x = _draw_chip(
            draw,
            x=chip_x,
            y=chip_y,
            text=platform_chip,
            font=chip_font,
            fill="#2F8FFF",
            border="#2F8FFF",
            text_fill="#FFFFFF",
            padding_x=14,
            padding_y=7,
            radius=18,
        ) + 10

        keyword_chip = _trim_text(keyword, 22)
        keyword_box = draw.textbbox((0, 0), keyword_chip, font=chip_font)
        keyword_chip_w = (keyword_box[2] - keyword_box[0]) + 28
        max_keyword_right = time_badge_x - 18
        if chip_x + keyword_chip_w > max_keyword_right:
            available_w = max(80, max_keyword_right - chip_x)
            approx_chars = max(6, int((available_w - 28) / max(7, chip_font.size * 0.9)))
            keyword_chip = _trim_text(keyword, approx_chars)
        _draw_chip(
            draw,
            x=chip_x,
            y=chip_y,
            text=keyword_chip,
            font=chip_font,
            fill="#ECF5FF",
            border="#8FC2FF",
            text_fill="#1E63D7",
            padding_x=14,
            padding_y=7,
            radius=18,
        )

    image_x = card_x + max(inner_padding, (card_w - main_img.width) // 2)
    image_y = card_y + header_h + inner_padding
    image_bbox = (image_x, image_y, image_x + main_img.width, image_y + main_img.height)
    _draw_shadow(
        canvas,
        image_bbox,
        radius=image_radius,
        color=_with_alpha(accent, 20),
        blur=max(6, shadow_blur - 8),
        offset_y=6,
    )
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        image_bbox,
        radius=image_radius,
        fill="#FFFFFF",
        outline=str(card_colors.get("border", DEFAULT_THEME["card"]["border"])),
        width=1,
    )
    rounded_main = _apply_rounded_corners(main_img, image_radius)
    canvas.paste(rounded_main, (image_x, image_y), rounded_main)

    if theme.get("show_footer", True):
        footer = _render_template(str(theme.get("footer", DEFAULT_THEME["footer"])), context)
        _draw_text_crisp(
            draw,
            (card_x + inner_padding, card_y + card_h - footer_h + 6),
            footer,
            fill=str(text_colors.get("footer", DEFAULT_THEME["text"]["footer"])),
            font=footer_font,
        )

    _save_jpeg(canvas.convert("RGB"), image_path)

    return {
        "preview_available": False,
        "highlight_count": len(boxes),
        "generated_at": local_now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _draw_highlight_boxes(
    image: Image.Image,
    boxes: list[dict],
    *,
    scale_x: float,
    scale_y: float,
    accent_color: str,
) -> None:
    draw = ImageDraw.Draw(image, "RGBA")
    for box in boxes[:8]:
        x = max(0, int(float(box.get("x", 0)) * scale_x))
        y = max(0, int(float(box.get("y", 0)) * scale_y))
        w = max(10, int(float(box.get("width", 0)) * scale_x))
        h = max(8, int(float(box.get("height", 0)) * scale_y))
        draw.rounded_rectangle(
            (x, y, x + w, y + h),
            radius=8,
            outline=accent_color,
            width=4,
            fill=_with_alpha(accent_color, 40),
        )


def _load_theme() -> dict:
    try:
        config = load_config("config.yaml")
        return get_decoration_theme(config)
    except Exception:
        return get_default_decoration_theme()


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base or {})
    for key, value in (override or {}).items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _clamp_int(value, default: int, *, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except Exception:
        return default


def _render_template(template: str, context: dict) -> str:
    try:
        return template.format(**context)
    except Exception:
        return template


def _trim_text(text: str, limit: int) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _normalize_platform_name(platform_name: str) -> str:
    raw = str(platform_name or "").strip()
    if not raw:
        return ""
    return PLATFORM_ID_ALIASES.get(raw, PLATFORM_ID_ALIASES.get(raw.lower(), raw.lower()))


def _draw_platform_logo(
    canvas: Image.Image,
    *,
    platform_name: str,
    box: tuple[int, int, int, int],
) -> None:
    normalized_platform = _normalize_platform_name(platform_name)
    style = PLATFORM_LOGO_STYLES.get(normalized_platform, {"fill": "#F3F4F6", "label": "AI"})
    left, top, right, bottom = box
    width = right - left
    height = bottom - top
    container = _build_supersampled_badge(
        width,
        height,
        outer_radius=20,
        fill="#FFFFFF",
        outline="#E7ECF5",
        outline_width=1,
        inner_inset=6,
        inner_radius=16,
        inner_fill=str(style.get("fill", "#F3F4F6")),
    )
    cdraw = ImageDraw.Draw(container)
    logo_asset = _load_platform_logo_asset(normalized_platform, size=(width - 20, height - 20))
    if logo_asset is not None:
        container.paste(logo_asset, ((width - logo_asset.width) // 2, (height - logo_asset.height) // 2), logo_asset)
    else:
        label_font = _font(24, bold=True)
        text = str(style.get("label", "AI"))
        text_box = cdraw.textbbox((0, 0), text, font=label_font)
        text_w = text_box[2] - text_box[0]
        text_h = text_box[3] - text_box[1]
        _draw_text_crisp(
            cdraw,
            ((width - text_w) / 2, (height - text_h) / 2 - 3),
            text,
            fill="#111827",
            font=label_font,
        )
    canvas.paste(container, (left, top), container)


def _draw_time_badge(
    canvas: Image.Image,
    *,
    box: tuple[int, int, int, int],
    time_text: str,
    label_font,
    value_font,
) -> None:
    left, top, right, bottom = box
    width = right - left
    height = bottom - top
    badge = _build_supersampled_badge(
        width,
        height,
        outer_radius=20,
        fill="#FFFFFF",
        outline="#E7ECF5",
        outline_width=1,
    )
    bdraw = ImageDraw.Draw(badge)
    _draw_text_crisp(bdraw, (14, 14), "TIME", fill="#37506E", font=label_font)
    _draw_text_crisp(bdraw, (14, 34), time_text, fill="#162033", font=value_font)
    canvas.paste(badge, (left, top), badge)


def _load_platform_logo_asset(platform_name: str, *, size: tuple[int, int]) -> Image.Image | None:
    normalized_platform = _normalize_platform_name(platform_name)
    rel_path = PLATFORM_LOGO_ASSETS.get(normalized_platform)
    if not rel_path:
        return None
    asset_path = resolve_app_path(rel_path)
    if not asset_path.exists():
        return None
    try:
        with Image.open(asset_path) as img:
            logo = img.convert("RGBA")
    except Exception:
        return None

    target_w, target_h = size
    logo.thumbnail((target_w, target_h), Image.LANCZOS)
    return logo


def _create_linear_gradient(size: tuple[int, int], start: str, end: str, *, horizontal: bool) -> Image.Image:
    width, height = size
    start_rgba = _parse_color(start)
    end_rgba = _parse_color(end)
    base = Image.new("RGBA", size, start_rgba)
    draw = ImageDraw.Draw(base)
    steps = max(width if horizontal else height, 1)
    for idx in range(steps):
        ratio = idx / max(steps - 1, 1)
        color = tuple(
            int(start_rgba[i] + (end_rgba[i] - start_rgba[i]) * ratio)
            for i in range(4)
        )
        if horizontal:
            draw.line((idx, 0, idx, height), fill=color)
        else:
            draw.line((0, idx, width, idx), fill=color)
    return base


def _build_supersampled_badge(
    width: int,
    height: int,
    *,
    outer_radius: int,
    fill: str,
    outline: str,
    outline_width: int = 1,
    inner_inset: int | None = None,
    inner_radius: int | None = None,
    inner_fill: str | None = None,
    scale: int = 4,
) -> Image.Image:
    badge = _build_supersampled_roundrect(
        (width, height),
        radius=outer_radius,
        fill=fill,
        outline=outline,
        outline_width=outline_width,
        scale=scale,
    )
    if inner_inset is None or inner_fill is None:
        return badge
    scaled_w = max(1, width * scale)
    scaled_h = max(1, height * scale)
    inner = Image.new("RGBA", (scaled_w, scaled_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(inner)
    inset = max(1, inner_inset * scale)
    draw.rounded_rectangle(
        (inset, inset, scaled_w - inset - 1, scaled_h - inset - 1),
        radius=max(1, (inner_radius if inner_radius is not None else outer_radius) * scale),
        fill=inner_fill,
    )
    resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
    inner_resized = inner.resize((width, height), resampling)
    badge.alpha_composite(inner_resized)
    return badge


def _build_supersampled_roundrect(
    size: tuple[int, int],
    *,
    radius: int,
    fill: str,
    outline: str | None = None,
    outline_width: int = 0,
    scale: int = 4,
) -> Image.Image:
    width, height = size
    scaled_w = max(1, width * scale)
    scaled_h = max(1, height * scale)
    layer = Image.new("RGBA", (scaled_w, scaled_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle(
        (0, 0, scaled_w - 1, scaled_h - 1),
        radius=max(1, radius * scale),
        fill=fill,
        outline=outline,
        width=max(1, outline_width * scale),
    )
    resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
    return layer.resize((width, height), resampling)


def _add_soft_glow(canvas: Image.Image, color: str, *, center: tuple[int, int]) -> None:
    glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    cx, cy = center
    gdraw.ellipse(
        (cx - 180, cy - 120, cx + 180, cy + 120),
        fill=_with_alpha(color, 22),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(40))
    canvas.alpha_composite(glow)


def _draw_shadow(
    image: Image.Image,
    bbox: tuple[int, int, int, int],
    *,
    radius: int,
    color: str,
    blur: int,
    offset_y: int,
) -> None:
    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(shadow)
    left, top, right, bottom = bbox
    draw.rounded_rectangle(
        (left, top + offset_y, right, bottom + offset_y),
        radius=radius,
        fill=_parse_color(color),
    )
    if blur > 0:
        shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    image.alpha_composite(shadow)


def _build_header_mask(width: int, height: int, radius: int) -> Image.Image:
    scale = 4
    scaled_w = max(1, width * scale)
    scaled_h = max(1, height * scale)
    scaled_radius = max(1, radius * scale)
    mask = Image.new("L", (scaled_w, scaled_h), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, scaled_w - 1, scaled_h - 1 + scaled_radius), radius=scaled_radius, fill=255)
    draw.rectangle((0, scaled_radius, scaled_w, scaled_h), fill=255)
    resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
    return mask.resize((width, height), resampling)


def _draw_chip(
    draw: ImageDraw.ImageDraw,
    *,
    x: int,
    y: int,
    text: str,
    font,
    fill: str,
    border: str,
    text_fill: str,
    padding_x: int,
    padding_y: int,
    radius: int,
    align: str = "left",
) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    text_w = box[2] - box[0]
    text_h = box[3] - box[1]
    width = text_w + padding_x * 2
    height = text_h + padding_y * 2
    left = x if align == "left" else x - width
    top = y
    draw.rounded_rectangle(
        (left, top, left + width, top + height),
        radius=radius,
        fill=_parse_color(fill),
        outline=_parse_color(border),
        width=1,
    )
    _draw_text_crisp(draw, (left + padding_x, top + padding_y - 1), text, fill=text_fill, font=font)
    return left + width


def _apply_rounded_corners(image: Image.Image, radius: int) -> Image.Image:
    rgba = image.convert("RGBA")
    mask = Image.new("L", rgba.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, rgba.width, rgba.height), radius=radius, fill=255)
    rounded = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
    rounded.paste(rgba, (0, 0), mask)
    return rounded


def _parse_color(value: str) -> tuple[int, int, int, int]:
    text = str(value or "").strip()
    if text.startswith("#"):
        text = text[1:]
        if len(text) == 6:
            return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4)) + (255,)
        if len(text) == 8:
            return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4, 6))
    return (255, 255, 255, 255)


def _with_alpha(color: str, alpha: int) -> str:
    rgba = _parse_color(color)
    return "#{:02X}{:02X}{:02X}{:02X}".format(rgba[0], rgba[1], rgba[2], max(0, min(255, alpha)))


def _draw_text_crisp(draw: ImageDraw.ImageDraw, position, text: str, *, fill: str, font) -> None:
    x, y = position
    draw.text((x, y), text, fill=fill, font=font)


def _save_jpeg(image: Image.Image, image_path: str) -> None:
    image.save(image_path, format="JPEG", quality=95, optimize=True, subsampling=0)


def _font(size: int, *, bold: bool = False):
    candidates = [
        "/System/Library/Fonts/STHeiti Medium.ttc" if bold else "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
    ]
    for path in candidates:
        if not path:
            continue
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()
