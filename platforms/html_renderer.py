"""
将 API 返回的 markdown 文本渲染为 HTML，用 playwright 截长图。
playwright 只渲染本地 HTML 文件，不打开任何 AI 平台网页。
"""

import base64
import atexit
import html
import io
import json
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter

from core.app_paths import resolve_app_dir, resolve_app_path
from core.browser_runtime import resolve_system_browser_executable

_PLATFORM_LABELS = {
    'local_model': '本地模型',
    'doubao': '豆包', 'deepseek': 'DeepSeek', 'ark_deepseek': '方舟 DeepSeek', 'kimi': 'Kimi',
    'yuanbao': '腾讯元宝', 'tongyi': '通义千问', 'wenxin': '文心一言',
}

_FONT_FILES = {
    "inter_400": "assets/fonts/inter-400.ttf",
    "inter_500": "assets/fonts/inter-500.ttf",
    "newsreader_400": "assets/fonts/newsreader-400.ttf",
    "mono_400": "assets/fonts/jetbrains-mono-400.ttf",
    "cjk_400": "assets/fonts/source-han-sans-cn-400.otf",
    "cjk_500": "assets/fonts/source-han-sans-cn-500.otf",
}

_TEMPLATE_DEBUG_PRINTED = False
_SCREENSHOT_RENDERER = None
_SCREENSHOT_RENDERER_LOCK = threading.Lock()
_RENDERER_IDLE_TIMEOUT_SECONDS = 180
_SATORI_RENDERER = None
_SATORI_RENDERER_LOCK = threading.Lock()
_SATORI_RENDERER_UNAVAILABLE_LOGGED = False
_SATORI_RENDERER_TIMEOUT_SECONDS = 45
_SATORI_DEFAULT_SCALE = 2.0
_SATORI_JPEG_QUALITY = 92
_SATORI_TEMPLATE_WIDTH = 900
_SATORI_TEMPLATE_MIN_VIEWPORT_HEIGHT = 800
_SATORI_TEMPLATE_BOTTOM_PADDING = 28

_DEFAULT_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>{style_block}</style></head><body>
<div class="share-card">
  <header class="header">
    <div class="logo">Surfaced<span class="cyan">.</span></div>
    <div class="header-meta">THREAD SNAPSHOT</div>
  </header>
  {keyword_section}
  <div class="message ai">
    <div class="avatar ai-avatar">{logo_html}</div>
    <div class="ai-text answer-body">{html_body}</div>
  </div>
  <div class="footer"><div class="time">{timestamp}</div><div class="brand-text">AI MONITORING</div></div>
</div>
</body></html>"""

_DEFAULT_STYLE_BLOCK = """
body { margin: 0; padding: 24px; font-family: "Inter", "Source Han Sans CN", "PingFang SC", "Microsoft YaHei", sans-serif; background: #fafafa; }
.share-card { max-width: 760px; margin: 0 auto; background: #fff; border-radius: 24px; padding: 28px; }
.message { display: flex; gap: 16px; align-items: flex-start; }
.message.user { flex-direction: row-reverse; margin-bottom: 24px; }
.avatar { width: 44px; height: 44px; border-radius: 12px; display: flex; align-items: center; justify-content: center; background: #f8fafc; border: 1px solid rgba(226,232,240,0.8); flex-shrink: 0; }
.avatar img { max-width: 28px; max-height: 28px; object-fit: contain; display: block; }
.avatar-fallback { font-size: 13px; font-weight: 700; color: #3b82f6; }
.bubble { max-width: 88%; padding: 18px 24px; border-radius: 24px; background: #0f172a; color: #f8fafc; white-space: pre-wrap; word-break: break-word; line-height: 1.7; }
.ai-text { font-size: 15px; line-height: 1.75; color: #475569; word-break: break-word; }
.term-highlight { background-color: rgba(14, 165, 233, 0.1); color: #0284c7; padding: 2px 6px; border-radius: 6px; font-weight: 500; font-size: 0.95em; }
.ai-text code, .footer .time { font-family: "JetBrains Mono", monospace; }
.answer-body > :first-child { margin-top: 0 !important; }
.answer-body > :last-child { margin-bottom: 0 !important; }
.answer-body > h1:first-child,
.answer-body > h2:first-child,
.answer-body > h3:first-child,
.answer-body > h4:first-child,
.answer-body > h5:first-child,
.answer-body > h6:first-child { line-height: 1.18; margin-top: -0.12em !important; }
.answer-body img, .answer-body picture { display: none !important; }
.footer { display: flex; justify-content: space-between; gap: 12px; margin-top: 24px; padding-top: 18px; border-top: 1px solid rgba(226,232,240,0.8); color: #94a3b8; font-size: 12px; }
"""


def render_text_to_screenshot(
    text: str,
    platform: str,
    keyword: str = "",
    brand: str = "",
    output_path: str = None,
    include_badges: bool = True,
) -> str:
    """
    markdown 文本 → HTML → playwright 截长图 → JPEG
    返回截图文件路径，失败返回空字符串。
    """
    if output_path is None:
        output_path = _build_default_screenshot_path(platform)

    satori_path = _render_text_to_screenshot_with_satori(
        text=text,
        platform=platform,
        keyword=keyword,
        brand=brand,
        output_path=output_path,
        include_badges=include_badges,
    )
    if satori_path:
        return satori_path

    html_body = _md_to_html(text)
    return render_html_to_screenshot(
        html_body,
        platform,
        keyword=keyword,
        brand=brand,
        output_path=output_path,
        include_badges=include_badges,
    )


def render_html_to_screenshot(
    html_body: str,
    platform: str,
    keyword: str = "",
    brand: str = "",
    output_path: Optional[str] = None,
    include_badges: bool = True,
) -> str:
    """HTML 片段 → playwright 截长图 → JPEG"""
    _debug_log_template_sources()

    keyword_section = ""
    keyword_text = str(keyword or "").strip()
    if include_badges and keyword_text:
        keyword_section = (
            '<div class="message user">'
            '<div class="bubble user-bubble">'
            f'{html.escape(keyword_text)}'
            '</div>'
            '</div>'
        )

    rendered_html_body = _highlight_brand_mentions_in_html(html_body, brand)
    timestamp = datetime.now().strftime("%Y.%m.%d %H:%M:%S CST")
    style_block = _build_font_face_block() + "\n" + _load_style_block()
    full_html = (
        _load_html_template()
        .replace('{style_block}', style_block)
        .replace('{keyword_section}', keyword_section)
        .replace('{logo_html}', _build_platform_logo_html(platform))
        .replace('{html_body}', rendered_html_body)
        .replace('{timestamp}', timestamp)
    )

    if output_path is None:
        output_path = _build_default_screenshot_path(platform)

    try:
        _playwright_screenshot(full_html, output_path)
    except Exception as e:
        print(f"[html_renderer] 截图失败: {e}")
        return ""

    if os.path.exists(output_path):
        print(f"[html_renderer] 截图已保存: {output_path}")
        return output_path
    return ""


def _build_default_screenshot_path(platform: str) -> str:
    ts = datetime.now().strftime("%m%d_%H%M%S")
    return str(resolve_app_dir("screenshots") / f"{platform}_api_{ts}.jpg")


def _load_html_template() -> str:
    template_path = resolve_app_path("assets/templates/surfaced-share.html")
    try:
        return template_path.read_text(encoding="utf-8")
    except Exception:
        return _DEFAULT_HTML_TEMPLATE


def _load_style_block() -> str:
    style_path = resolve_app_path("assets/templates/surfaced-share.css")
    try:
        return style_path.read_text(encoding="utf-8")
    except Exception:
        return _DEFAULT_STYLE_BLOCK


def _debug_log_template_sources() -> None:
    global _TEMPLATE_DEBUG_PRINTED
    if _TEMPLATE_DEBUG_PRINTED:
        return

    html_path = resolve_app_path("assets/templates/surfaced-share.html")
    css_path = resolve_app_path("assets/templates/surfaced-share.css")
    print(f"[html_renderer] HTML模板路径: {html_path}")
    print(f"[html_renderer] CSS模板路径: {css_path}")
    try:
        print(f"[html_renderer] CSS模板修改时间: {datetime.fromtimestamp(css_path.stat().st_mtime).isoformat(timespec='seconds')}")
    except Exception:
        pass
    _TEMPLATE_DEBUG_PRINTED = True


def _build_platform_logo_html(platform: str) -> str:
    data_uri = _get_platform_logo_data_uri(platform)
    if data_uri:
        label = html.escape(_PLATFORM_LABELS.get(platform, platform))
        return f'<img src="{data_uri}" alt="{label} logo">'
    fallback = html.escape((_PLATFORM_LABELS.get(platform, platform) or "?")[:2].upper())
    return f'<span class="avatar-fallback">{fallback}</span>'


@lru_cache(maxsize=1)
def _build_font_face_block() -> str:
    font_defs: list[str] = []

    def _add_font_face(family: str, rel_path: str, *, weight: int, font_format: str, unicode_range: str = ""):
        font_path = resolve_app_path(rel_path)
        if not font_path.exists() or not font_path.is_file():
            return
        src = _path_to_file_url(font_path)
        if not src:
            return
        unicode_rule = f"\n  unicode-range: {unicode_range};" if unicode_range else ""
        font_defs.append(
            "@font-face {\n"
            f"  font-family: '{family}';\n"
            f"  src: url('{src}') format('{font_format}');\n"
            "  font-style: normal;\n"
            f"  font-weight: {weight};\n"
            "  font-display: swap;"
            f"{unicode_rule}\n"
            "}"
        )

    latin_range = "U+0000-00FF, U+0100-024F, U+1E00-1EFF, U+2000-206F"
    families = (
        ("Inter", "inter_400", 400, "truetype"),
        ("Inter", "inter_500", 500, "truetype"),
        ("Newsreader", "newsreader_400", 400, "truetype"),
        ("JetBrains Mono", "mono_400", 400, "truetype"),
    )
    for family, key, weight, font_format in families:
        _add_font_face(family, _FONT_FILES[key], weight=weight, font_format=font_format, unicode_range=latin_range)
    _add_font_face("Source Han Sans CN", _FONT_FILES["cjk_400"], weight=400, font_format="opentype")
    _add_font_face("Source Han Sans CN", _FONT_FILES["cjk_500"], weight=500, font_format="opentype")
    return "\n".join(font_defs)


@lru_cache(maxsize=32)
def _get_platform_logo_data_uri(platform: str) -> str:
    for suffix in (".png", ".ico", ".jpg", ".jpeg", ".webp", ".svg"):
        path = resolve_app_path(f"assets/logos/{platform}{suffix}")
        if not path.exists() or not path.is_file():
            continue
        return _path_to_data_uri(path)
    return ""


def _path_to_data_uri(path: Path) -> str:
    if path.suffix.lower() in {".png", ".ico", ".jpg", ".jpeg", ".webp"}:
        normalized = _normalize_logo_image_bytes(path)
        if normalized:
            payload = base64.b64encode(normalized).decode("ascii")
            return "data:image/png;base64," + payload
    mime_type, _ = mimetypes.guess_type(str(path))
    if not mime_type:
        mime_type = "application/octet-stream"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{payload}"


def _path_to_file_url(path: Path) -> str:
    try:
        return path.resolve().as_uri()
    except Exception:
        return ""


def _normalize_logo_image_bytes(path: Path) -> bytes:
    try:
        from PIL import Image, ImageChops

        with Image.open(path) as img:
            rgba = img.convert("RGBA")

        alpha_bbox = rgba.getchannel("A").getbbox()
        if alpha_bbox:
            rgba = rgba.crop(alpha_bbox)
        else:
            bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            diff = ImageChops.difference(rgba, bg)
            bbox = diff.getbbox()
            if bbox:
                rgba = rgba.crop(bbox)

        target = Image.new("RGBA", (28, 28), (0, 0, 0, 0))
        content = rgba.copy()
        content.thumbnail((24, 24), Image.LANCZOS)
        offset_x = (target.width - content.width) // 2
        offset_y = (target.height - content.height) // 2
        target.paste(content, (offset_x, offset_y), content)

        output = io.BytesIO()
        target.save(output, format="PNG")
        return output.getvalue()
    except Exception:
        return b""


def _render_text_to_screenshot_with_satori(
    *,
    text: str,
    platform: str,
    keyword: str,
    brand: str,
    output_path: str,
    include_badges: bool,
) -> str:
    """使用 Satori 快速渲染 Markdown 文本；失败时返回空字符串交给 Playwright fallback。"""
    if not _should_use_satori_renderer():
        return ""

    output = Path(str(output_path or "").strip())
    if not output:
        return ""
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp_png = output.with_name(f"{output.stem}.satori.tmp.png")

    payload = {
        "text": str(text or ""),
        "platform": str(platform or "AI"),
        "keyword": str(keyword or ""),
        "brand": str(brand or ""),
        "outputPath": str(tmp_png),
        "includeBadges": bool(include_badges),
        "timestamp": datetime.now().strftime("%Y.%m.%d %H:%M:%S CST"),
        "logoDataUri": _get_platform_logo_data_uri(platform),
        "scale": _resolve_satori_render_scale(),
    }

    try:
        _get_satori_renderer().render_text(payload)
        if not tmp_png.exists():
            raise RuntimeError("Satori renderer completed but no PNG was generated.")
        _convert_satori_png_to_jpeg(tmp_png, output)
    except Exception as exc:
        _log_satori_renderer_unavailable(f"Satori 快速渲染失败，回退 Playwright: {exc}")
        return ""
    finally:
        try:
            tmp_png.unlink(missing_ok=True)
        except Exception:
            pass

    if output.exists():
        print(f"[html_renderer] Satori截图已保存: {output}")
        return str(output)
    return ""


def _should_use_satori_renderer() -> bool:
    value = str(os.environ.get("AI_MONITOR_SATORI_RENDERER", "")).strip().lower()
    return value not in {"0", "false", "off", "no", "playwright"}


def _convert_satori_png_to_jpeg(png_path: Path, output_path: Path) -> None:
    with Image.open(png_path) as img:
        min_height = round(max(1, img.width) / _SATORI_TEMPLATE_WIDTH * _SATORI_TEMPLATE_MIN_VIEWPORT_HEIGHT)
        bottom_padding = round(max(1, img.width) / _SATORI_TEMPLATE_WIDTH * _SATORI_TEMPLATE_BOTTOM_PADDING)
        if img.mode in ("RGBA", "LA"):
            rgba = img.convert("RGBA")
            rgba = _trim_bottom_transparent(rgba, padding=bottom_padding, min_height=min_height)
            background = _build_satori_template_background(rgba.size)
            background.alpha_composite(rgba)
            rgb = background.convert("RGB")
        elif img.mode == "P":
            rgb = img.convert("RGB")
            rgb = _trim_bottom_background(rgb, background=(250, 250, 250), tolerance=4, padding=bottom_padding, min_height=min_height)
            rgb = _apply_satori_template_background_effects(rgb)
        else:
            rgb = img.convert("RGB")
            rgb = _trim_bottom_background(rgb, background=(250, 250, 250), tolerance=4, padding=bottom_padding, min_height=min_height)
            rgb = _apply_satori_template_background_effects(rgb)
        rgb.save(output_path, format="JPEG", quality=_SATORI_JPEG_QUALITY, subsampling=0, optimize=True)


def _resolve_satori_render_scale() -> float:
    raw = str(os.environ.get("AI_MONITOR_SATORI_SCALE", "")).strip()
    if raw:
        try:
            return min(3.0, max(1.0, float(raw)))
        except ValueError:
            return _SATORI_DEFAULT_SCALE
    return _SATORI_DEFAULT_SCALE


def _trim_bottom_background(
    img: Image.Image,
    *,
    background: tuple[int, int, int],
    tolerance: int,
    padding: int,
    min_height: int = 0,
) -> Image.Image:
    """裁掉 Satori 高度估算留下的底部纯背景，但保留与旧模板接近的底边距。"""
    width, height = img.size
    if width <= 0 or height <= 0:
        return img

    bg_r, bg_g, bg_b = background
    sample_step = max(1, width // 120)
    pixels = img.load()
    last_content_y = height - 1

    for y in range(height - 1, -1, -1):
        has_content = False
        for x in range(0, width, sample_step):
            r, g, b = pixels[x, y][:3]
            if (
                abs(int(r) - bg_r) > tolerance
                or abs(int(g) - bg_g) > tolerance
                or abs(int(b) - bg_b) > tolerance
            ):
                has_content = True
                break
        if has_content:
            last_content_y = y
            break

    crop_bottom = min(height, max(last_content_y + max(0, padding), int(min_height or 0)))
    if crop_bottom < height:
        return img.crop((0, 0, width, crop_bottom))
    return img


def _trim_bottom_transparent(
    img: Image.Image,
    *,
    padding: int,
    min_height: int = 0,
    alpha_tolerance: int = 2,
) -> Image.Image:
    """按透明前景裁掉预估高度，避免背景光晕影响长图底部裁剪。"""
    rgba = img.convert("RGBA")
    width, height = rgba.size
    if width <= 0 or height <= 0:
        return rgba

    sample_step = max(1, width // 180)
    alpha = rgba.getchannel("A")
    last_content_y = height - 1
    for y in range(height - 1, -1, -1):
        has_content = False
        for x in range(0, width, sample_step):
            if alpha.getpixel((x, y)) > alpha_tolerance:
                has_content = True
                break
        if has_content:
            last_content_y = y
            break

    crop_bottom = min(height, max(last_content_y + max(0, padding), int(min_height or 0)))
    if crop_bottom < height:
        return rgba.crop((0, 0, width, crop_bottom))
    return rgba


def _build_satori_template_background(size: tuple[int, int]) -> Image.Image:
    """按原 surfaced-share.css 重建 body 背景和 fixed 光晕。"""
    width, height = size
    background = Image.new("RGBA", (width, height), (250, 250, 250, 255))
    if width <= 0 or height <= 0:
        return background

    scale = max(0.1, width / _SATORI_TEMPLATE_WIDTH)
    viewport_width = width
    viewport_height = round(_SATORI_TEMPLATE_MIN_VIEWPORT_HEIGHT * scale)

    top_diameter = round(viewport_width * 0.4)
    _alpha_composite_glow(
        background,
        left=round(viewport_width * -0.1),
        top=round(viewport_height * -0.1),
        diameter=top_diameter,
        blur=round(120 * scale),
        color=(14, 165, 233, round(255 * 0.08)),
    )

    bottom_diameter = round(viewport_width * 0.5)
    _alpha_composite_glow(
        background,
        left=round(viewport_width - viewport_width * -0.05 - bottom_diameter),
        top=round(viewport_height - viewport_height * -0.1 - bottom_diameter),
        diameter=bottom_diameter,
        blur=round(150 * scale),
        color=(96, 165, 250, round(255 * 0.04)),
    )
    return background


def _apply_satori_template_background_effects(img: Image.Image) -> Image.Image:
    """兼容非透明 Satori 输出：在已扁平图片上补一点模板光晕。"""
    try:
        scale = max(0.1, img.width / _SATORI_TEMPLATE_WIDTH)
        canvas = img.convert("RGBA")
        _alpha_composite_glow(
            canvas,
            left=round(-90 * scale),
            top=round(-90 * scale),
            diameter=round(360 * scale),
            blur=round(120 * scale),
            color=(14, 165, 233, 20),
        )
        _alpha_composite_glow(
            canvas,
            left=round(img.width - 45 * scale),
            top=round(img.height - 80 * scale),
            diameter=round(450 * scale),
            blur=round(150 * scale),
            color=(96, 165, 250, 10),
        )
        return canvas.convert("RGB")
    except Exception:
        return img


def _alpha_composite_glow(
    base: Image.Image,
    *,
    left: int,
    top: int,
    diameter: int,
    blur: int,
    color: tuple[int, int, int, int],
) -> None:
    diameter = max(1, int(diameter))
    blur = max(0, int(blur))
    patch_size = diameter + blur * 2
    patch = Image.new("RGBA", (patch_size, patch_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(patch)
    draw.ellipse((blur, blur, blur + diameter, blur + diameter), fill=color)
    if blur:
        patch = patch.filter(ImageFilter.GaussianBlur(blur))

    dest_left = left - blur
    dest_top = top - blur
    crop_left = max(0, -dest_left)
    crop_top = max(0, -dest_top)
    crop_right = min(patch.width, base.width - dest_left)
    crop_bottom = min(patch.height, base.height - dest_top)
    if crop_right <= crop_left or crop_bottom <= crop_top:
        return
    cropped = patch.crop((crop_left, crop_top, crop_right, crop_bottom))
    base.alpha_composite(cropped, (max(0, dest_left), max(0, dest_top)))


def _log_satori_renderer_unavailable(message: str) -> None:
    global _SATORI_RENDERER_UNAVAILABLE_LOGGED
    if _SATORI_RENDERER_UNAVAILABLE_LOGGED:
        return
    _SATORI_RENDERER_UNAVAILABLE_LOGGED = True
    print(f"[html_renderer] {message}")


def _get_satori_renderer():
    global _SATORI_RENDERER
    with _SATORI_RENDERER_LOCK:
        if _SATORI_RENDERER is None:
            _SATORI_RENDERER = _SatoriScreenshotRenderer()
        return _SATORI_RENDERER


class _SatoriScreenshotRenderer:
    """常驻 Node worker：Markdown -> Satori SVG -> PNG。"""

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._stdout_queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._worker_mtime_ns: int | None = None

    def render_text(self, payload: dict) -> dict:
        with self._lock:
            request = dict(payload)
            request["id"] = uuid.uuid4().hex
            request["type"] = "renderText"
            self._ensure_process()
            try:
                self._send(request)
                return self._read_response(request["id"])
            except Exception:
                self.close()
                self._ensure_process()
                self._send(request)
                return self._read_response(request["id"])

    def close(self) -> None:
        process = self._process
        self._process = None
        self._worker_mtime_ns = None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except Exception:
            pass
        try:
            process.terminate()
            process.wait(timeout=2.0)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=2.0)
            except Exception:
                pass
        finally:
            for stream in (process.stdout, process.stderr):
                try:
                    if stream:
                        stream.close()
                except Exception:
                    pass

    def _ensure_process(self) -> None:
        worker_path = _satori_worker_path()
        worker_mtime_ns = worker_path.stat().st_mtime_ns if worker_path.exists() else None
        if self._process is not None and self._process.poll() is None:
            if self._worker_mtime_ns == worker_mtime_ns:
                return
            self.close()

        node_path = _resolve_node_executable()
        if not node_path:
            raise RuntimeError("未找到 node，可设置 AI_MONITOR_NODE_PATH。")
        if not worker_path.exists():
            raise RuntimeError(f"未找到 Satori worker: {worker_path}")
        if not (worker_path.parent / "node_modules" / "satori").exists():
            raise RuntimeError(f"Satori renderer 依赖未安装，请在 {worker_path.parent} 执行 npm install。")

        self._process = subprocess.Popen(
            [node_path, str(worker_path)],
            cwd=str(worker_path.parent),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        threading.Thread(target=self._read_stdout, name="satori-renderer-stdout", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="satori-renderer-stderr", daemon=True).start()
        self._worker_mtime_ns = worker_mtime_ns

    def _send(self, request: dict) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise RuntimeError("Satori renderer worker is not running.")
        process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        process.stdin.flush()

    def _read_response(self, request_id: str) -> dict:
        deadline = time.monotonic() + _SATORI_RENDERER_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Satori renderer timed out.")
            try:
                line = self._stdout_queue.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                process = self._process
                if process is not None and process.poll() is not None:
                    raise RuntimeError(f"Satori renderer exited with code {process.returncode}.")
                continue

            try:
                response = json.loads(line)
            except Exception:
                continue
            if response.get("id") != request_id:
                continue
            if not response.get("ok"):
                raise RuntimeError(str(response.get("error") or "Satori renderer failed."))
            return response.get("result") or {}

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            if line.strip():
                self._stdout_queue.put(line.strip())

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            if line.strip():
                print(f"[html_renderer:satori] {line.rstrip()}")


def _resolve_node_executable() -> str:
    configured = str(os.environ.get("AI_MONITOR_NODE_PATH", "")).strip()
    if configured and Path(configured).exists():
        return configured
    return shutil.which("node") or ""


def _satori_worker_path() -> Path:
    return resolve_app_path("renderers/satori/render_worker.mjs")


def _close_satori_renderer() -> None:
    global _SATORI_RENDERER
    if _SATORI_RENDERER is not None:
        _SATORI_RENDERER.close()
        _SATORI_RENDERER = None


atexit.register(_close_satori_renderer)


def _md_to_html(text: str) -> str:
    """markdown → HTML，优先用 markdown 库，fallback 到 <pre>"""
    try:
        import markdown
        return markdown.markdown(
            text,
            extensions=['tables', 'fenced_code', 'nl2br']
        )
    except ImportError:
        import html as html_lib
        escaped = html_lib.escape(text)
        return f"<pre style='white-space:pre-wrap;font-family:inherit'>{escaped}</pre>"


def _highlight_brand_mentions_in_html(html_body: str, brand: str) -> str:
    brand_text = str(brand or "").strip()
    if not brand_text:
        return str(html_body or "")

    pattern = _compile_brand_pattern(brand_text)
    if pattern is None:
        return str(html_body or "")

    parts = re.split(r"(<[^>]+>)", str(html_body or ""))
    highlighted: list[str] = []
    skip_tag_stack: list[str] = []
    skip_tags = {"code", "pre", "script", "style"}

    for part in parts:
        if not part:
            continue
        if part.startswith("<"):
            lower = part.lower()
            tag_match = re.match(r"<\s*/?\s*([a-z0-9:-]+)", lower)
            tag_name = tag_match.group(1) if tag_match else ""
            is_closing = bool(re.match(r"<\s*/", lower))
            is_self_closing = lower.rstrip().endswith("/>")

            if tag_name in skip_tags:
                if is_closing:
                    if skip_tag_stack and skip_tag_stack[-1] == tag_name:
                        skip_tag_stack.pop()
                elif not is_self_closing:
                    skip_tag_stack.append(tag_name)

            highlighted.append(part)
            continue

        if skip_tag_stack:
            highlighted.append(part)
            continue

        highlighted.append(pattern.sub(lambda m: f'<span class="term-highlight">{m.group(0)}</span>', part))

    return "".join(highlighted)


def _compile_brand_pattern(brand: str):
    tokens = [token for token in re.split(r"\s+", str(brand or "").strip()) if token]
    if not tokens:
        return None
    escaped_tokens = [re.escape(token) for token in tokens]
    joined = r"\s*".join(escaped_tokens)
    try:
        return re.compile(joined, flags=re.IGNORECASE)
    except re.error:
        return None


def _playwright_screenshot(html_content: str, output_jpg: str, width: int = 900):
    """通过后台常驻渲染线程执行 Playwright 截图，降低浏览器冷启动开销。"""
    _get_screenshot_renderer().render(html_content, output_jpg, width=width)


class _PlaywrightScreenshotRenderer:
    """让同一个后台线程复用浏览器进程，减少每次截图的启动成本。"""

    def __init__(self) -> None:
        self._jobs: queue.Queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._worker,
            name="html-renderer-worker",
            daemon=True,
        )
        self._thread.start()

    def render(self, html_content: str, output_jpg: str, width: int = 900) -> None:
        done = threading.Event()
        result: dict[str, BaseException | None] = {"error": None}
        self._jobs.put({
            "html_content": html_content,
            "output_jpg": output_jpg,
            "width": width,
            "done": done,
            "result": result,
        })
        done.wait()
        error = result.get("error")
        if error is not None:
            raise error

    def close(self) -> None:
        self._jobs.put(None)
        self._thread.join(timeout=2.0)

    def _worker(self) -> None:
        playwright_manager = None
        browser = None
        page = None
        try:
            from patchright.sync_api import sync_playwright

            playwright_manager = sync_playwright().start()
            while True:
                try:
                    job = self._jobs.get(timeout=_RENDERER_IDLE_TIMEOUT_SECONDS)
                except queue.Empty:
                    if page is not None:
                        try:
                            page.close()
                        except Exception:
                            pass
                        page = None
                    if browser is not None:
                        try:
                            browser.close()
                        except Exception:
                            pass
                        browser = None
                    continue
                if job is None:
                    return
                result = job["result"]
                done = job["done"]
                try:
                    browser = self._ensure_browser(playwright_manager, browser)
                    page = self._ensure_page(browser, page, width=int(job["width"] or 900))
                    self._render_once(
                        page,
                        html_content=job["html_content"],
                        output_jpg=job["output_jpg"],
                        width=int(job["width"] or 900),
                    )
                except Exception as first_error:
                    try:
                        if page is not None:
                            page.close()
                    except Exception:
                        pass
                    page = None
                    try:
                        if browser is not None:
                            browser.close()
                    except Exception:
                        pass
                    browser = None
                    try:
                        browser = self._ensure_browser(playwright_manager, browser)
                        page = self._ensure_page(browser, page, width=int(job["width"] or 900))
                        self._render_once(
                            page,
                            html_content=job["html_content"],
                            output_jpg=job["output_jpg"],
                            width=int(job["width"] or 900),
                        )
                    except Exception as second_error:
                        result["error"] = second_error
                        print(
                            "[html_renderer] 后台渲染重试失败: "
                            f"first={first_error}; second={second_error}"
                        )
                finally:
                    done.set()
        finally:
            try:
                if page is not None:
                    page.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            try:
                if playwright_manager is not None:
                    playwright_manager.stop()
            except Exception:
                pass

    def _ensure_browser(self, playwright_manager, browser):
        if browser is not None:
            return browser
        return _launch_screenshot_browser(playwright_manager)

    def _ensure_page(self, browser, page, *, width: int):
        if page is None:
            return browser.new_page(viewport={"width": width, "height": 800})
        try:
            page.set_viewport_size({"width": width, "height": 800})
        except Exception:
            try:
                page.close()
            except Exception:
                pass
            return browser.new_page(viewport={"width": width, "height": 800})
        return page

    def _render_once(self, page, *, html_content: str, output_jpg: str, width: int) -> None:
        page.set_content(html_content, wait_until="networkidle")
        try:
            page.evaluate("""
                () => {
                    if (!document.fonts || !document.fonts.ready) {
                        return true;
                    }
                    return document.fonts.ready.then(() => true);
                }
            """)
        except Exception:
            pass
        try:
            page.evaluate("""
                () => {
                    const bulletOnlyPattern = /^[\\s\\u00a0\\u200b\\u2022\\u2023\\u25E6\\u2043\\u2219\\u00b7\\u25cf\\u25cb\\u25aa\\u25ab\\-–—.]+$/;
                    const hasMeaningfulText = (value) => String(value || '').replace(/[\\s\\u00a0\\u200b]+/g, '').length > 0;
                    const unwrap = (el) => {
                        if (!el || !el.parentNode) return;
                        while (el.firstChild) {
                            el.parentNode.insertBefore(el.firstChild, el);
                        }
                        el.remove();
                    };

                    for (const li of document.querySelectorAll('.answer-body li')) {
                        let changed = true;
                        while (changed) {
                            changed = false;
                            const first = li.firstChild;
                            if (!first) break;
                            if (first.nodeType === Node.TEXT_NODE && bulletOnlyPattern.test(first.textContent || '')) {
                                first.remove();
                                changed = true;
                                continue;
                            }
                            if (first.nodeType === Node.ELEMENT_NODE) {
                                const text = String(first.textContent || '');
                                const nestedBlocks = first.querySelector('p, div, ul, ol, table, blockquote, pre, code');
                                if (!nestedBlocks && bulletOnlyPattern.test(text)) {
                                    first.remove();
                                    changed = true;
                                    continue;
                                }
                            }
                        }

                        const elementChildren = Array.from(li.children).filter((el) => {
                            if (!hasMeaningfulText(el.textContent || '')) return false;
                            return !['UL', 'OL'].includes(el.tagName);
                        });
                        if (
                            elementChildren.length === 1
                            && ['P', 'DIV'].includes(elementChildren[0].tagName)
                            && !elementChildren[0].querySelector('ul, ol, table, blockquote, pre, div, p')
                        ) {
                            unwrap(elementChildren[0]);
                        }

                        if (!hasMeaningfulText(li.textContent || '')) {
                            li.remove();
                        }
                    }
                }
            """)
        except Exception:
            pass
        page.wait_for_timeout(300)
        png_bytes = page.screenshot(full_page=True)

        img = Image.open(io.BytesIO(png_bytes))
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        img.save(output_jpg, format='JPEG', quality=85, optimize=True)


def _get_screenshot_renderer() -> _PlaywrightScreenshotRenderer:
    global _SCREENSHOT_RENDERER
    with _SCREENSHOT_RENDERER_LOCK:
        if _SCREENSHOT_RENDERER is None:
            _SCREENSHOT_RENDERER = _PlaywrightScreenshotRenderer()
        return _SCREENSHOT_RENDERER


def _launch_screenshot_browser(playwright):
    """截图统一使用系统正式版 Chrome，不再回退到 Testing/Playwright Chromium。"""
    browser_executable = resolve_system_browser_executable()
    if not browser_executable:
        raise RuntimeError(
            "未找到系统正式版 Chrome。请安装 Google Chrome，或设置 CHROME_EXECUTABLE_PATH。"
        )
    return playwright.chromium.launch(
        headless=True,
        executable_path=browser_executable,
    )
