"""
将 API 返回的 markdown 文本渲染为 HTML，用 playwright 截长图。
playwright 只渲染本地 HTML 文件，不打开任何 AI 平台网页。
"""

import base64
import html
import io
import mimetypes
import os
import queue
import re
import threading
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Optional

from PIL import Image

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
    html_body = _md_to_html(text)
    return render_html_to_screenshot(
        html_body,
        platform,
        keyword=keyword,
        brand=brand,
        output_path=output_path,
        include_badges=include_badges,
    )


def render_image_to_screenshot(
    image_path: str,
    platform: str,
    keyword: str = "",
    brand: str = "",
    output_path: str = None,
    include_badges: bool = True,
) -> str:
    """
    本地截图文件 -> 同一套 Surfaced HTML 模版 -> playwright 截图 -> JPEG
    """
    src = Path(str(image_path or "").strip())
    if not src.exists() or not src.is_file():
        return ""

    data_uri = _path_to_data_uri(src)
    if not data_uri:
        return ""

    alt_text = html.escape(str(keyword or brand or platform or "截图").strip() or "截图")
    html_body = (
        '<div class="embedded-shot">'
        f'<img class="embedded-shot-image" src="{data_uri}" alt="{alt_text}">'
        '</div>'
    )
    return render_html_to_screenshot(
        html_body,
        platform,
        keyword=keyword,
        brand=brand,
        output_path=output_path,
        include_badges=include_badges,
        allow_answer_images=True,
    )


def render_html_to_screenshot(
    html_body: str,
    platform: str,
    keyword: str = "",
    brand: str = "",
    output_path: Optional[str] = None,
    include_badges: bool = True,
    allow_answer_images: bool = False,
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
    if allow_answer_images:
        style_block += "\n" + _build_answer_image_style_block()
    full_html = (
        _load_html_template()
        .replace('{style_block}', style_block)
        .replace('{keyword_section}', keyword_section)
        .replace('{logo_html}', _build_platform_logo_html(platform))
        .replace('{html_body}', rendered_html_body)
        .replace('{timestamp}', timestamp)
    )

    if output_path is None:
        ts = datetime.now().strftime("%m%d_%H%M%S")
        output_path = str(resolve_app_dir("screenshots") / f"{platform}_api_{ts}.jpg")

    try:
        _playwright_screenshot(full_html, output_path)
    except Exception as e:
        print(f"[html_renderer] 截图失败: {e}")
        return ""

    if os.path.exists(output_path):
        print(f"[html_renderer] 截图已保存: {output_path}")
        return output_path
    return ""


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


def _build_answer_image_style_block() -> str:
    return """
.answer-body img,
.answer-body picture,
.answer-body figure,
.answer-body figure img {
  display: block !important;
}

.answer-body .embedded-shot {
  width: 100%;
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid rgba(226, 232, 240, 0.9);
  border-radius: 24px;
  padding: 14px;
  box-shadow: 0 18px 36px -18px rgba(15, 23, 42, 0.24);
}

.answer-body .embedded-shot-image {
  width: 100%;
  height: auto;
  border-radius: 18px;
  object-fit: contain;
  background: #ffffff;
}
"""


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
