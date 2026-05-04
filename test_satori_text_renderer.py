import tempfile
import unittest
import shutil
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops

from platforms import html_renderer


class FakeSatoriRenderer:
    def __init__(self):
        self.payloads = []

    def render_text(self, payload: dict) -> dict:
        self.payloads.append(dict(payload))
        output_path = Path(payload["outputPath"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (24, 24), (250, 250, 250, 255)).save(output_path, format="PNG")
        return {"outputPath": str(output_path), "width": 24, "height": 24}


class SatoriTextRendererTests(unittest.TestCase):
    def test_text_render_uses_satori_fast_path_when_available(self):
        fake_renderer = FakeSatoriRenderer()
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "rendered.jpg"
            with (
                patch.object(html_renderer, "_should_use_satori_renderer", return_value=True),
                patch.object(html_renderer, "_get_satori_renderer", return_value=fake_renderer),
            ):
                result = html_renderer.render_text_to_screenshot(
                    text="DeepSeek 命中。",
                    platform="deepseek",
                    keyword="测试关键词",
                    brand="DeepSeek",
                    output_path=str(output_path),
                    include_badges=True,
                )

            self.assertEqual(result, str(output_path))
            self.assertTrue(output_path.exists())
            self.assertEqual(fake_renderer.payloads[0]["platform"], "deepseek")
            self.assertEqual(fake_renderer.payloads[0]["text"], "DeepSeek 命中。")
            self.assertNotIn("htmlBody", fake_renderer.payloads[0])
            self.assertEqual(fake_renderer.payloads[0]["brand"], "DeepSeek")
            self.assertIn("logoDataUri", fake_renderer.payloads[0])

    def test_text_render_falls_back_to_playwright_when_satori_fails(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "fallback.jpg"
            with (
                patch.object(html_renderer, "_should_use_satori_renderer", return_value=True),
                patch.object(html_renderer, "_get_satori_renderer", side_effect=RuntimeError("missing deps")),
                patch.object(html_renderer, "render_html_to_screenshot", return_value=str(output_path)) as fallback,
            ):
                result = html_renderer.render_text_to_screenshot(
                    text="DeepSeek 命中。",
                    platform="deepseek",
                    keyword="测试关键词",
                    brand="DeepSeek",
                    output_path=str(output_path),
                    include_badges=True,
                )

            self.assertEqual(result, str(output_path))
            fallback.assert_called_once()


class SatoriTemplateCaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        worker_path = html_renderer._satori_worker_path()
        missing = []
        if not shutil.which("node"):
            missing.append("node")
        for package_name in ("satori", "sharp", "markdown-it", "react", "emoji-datasource-twitter"):
            if not (worker_path.parent / "node_modules" / package_name).exists():
                missing.append(package_name)
        if missing:
            raise unittest.SkipTest(f"Satori runtime not available: {', '.join(missing)}")

    @classmethod
    def tearDownClass(cls):
        html_renderer._close_satori_renderer()

    def test_template_cases_render_without_browser_fallback(self):
        cases = {
            "long_markdown": """### 广州 GEO 公司怎么选

DeepSeek、豆包、Kimi 都提到了品牌。

这是一段用于测试的长段落内容，包含中文、英文 BrandName 和一些数字 12345。

第二段包含 **加粗文字**、`inline code` 和 [链接文本](https://example.com)。
""",
            "simple_html_and_stripped_images": """
<article>
  <h2>HTML 原始排版</h2>
  <p><strong>DeepSeek</strong>、<span>豆包</span> 和 Kimi 都在同一段里。</p>
  <div>第一段 div 内容<br>第二行内容</div>
  <img src="https://example.com/image.png" alt="替代文字">
</article>
""",
            "long_tokens_and_symbols": """### 长词与符号

DeepSeek 的链接：https://example.com/a/very/long/path/that/should/not/stretch/the/card?keyword=DeepSeek&region=guangzhou&campaign=geo-monitoring

符号测试：★ ✓ ① ② ③
""",
            "lists_with_emoji": """### 主要GEO优化公司

1. 即搜AI（武汉即搜网络信息技术有限公司）
   - 成立时间：2011年11月 🔗
   - 核心定位：全链路GEO优化服务商 ✅
   - 服务特点：自主研发系统 📝
""",
            "empty_text": "   \n\n",
            "very_long_plain": "\n\n".join(
                ["### 很长的文本"]
                + [f"第 {index} 段：DeepSeek 和豆包在这段文字里反复出现，用于测试长图高度估算。" for index in range(1, 45)]
            ),
            "long_wrapping_table": """### 牛肉品牌推荐总览

| 类别 | 品牌名称 | 核心特点 | 牛种 | 主打产品 / 部位 | 价格参考 |
| --- | --- | --- | --- | --- | --- |
| 高端进口牛排 | 蜂铭牛 | 中国高端雪花牛头部品牌，纯血和牛，国际知名 | 纯血和牛 | A5级雪花牛肉 | 约980/kg |
| 高端进口牛排 | 龙江和牛 | 中国本土培育的纯种和牛，达到日本标准 | 纯种和牛 | A3级眼肉牛排 | 较高 |
| 高端进口牛排 | 澳格牛 | 专注澳洲进口和牛与安格斯牛肉，产品线丰富 | 纯血和牛、安格斯牛 | 石斧纯血和牛、玛格丽特和牛系列 | 偏高 |
| 国产精品 | 熊月 | 培育出沃金和牛自主品种，填补国产高端空白 | 沃金和牛、延边黄牛 | 各部位分割肉 | 中端 |
| 国产精品 | 科尔沁 | 内蒙古草原散养，肉质干香有嚼劲，24个月自然生长 | 科尔沁牛、褐牛 | 家庭款风干牛肉条 | 约399/5kg |
| 新零售电商 | 叮咚买菜昆山牛 | 48小时从牧场到餐桌，主打鲜切适合中式烹饪 | 泛源黄牛、思南黄牛 | 吊龙、里脊、肉丝等鲜切肉 | 中等 |
| 新零售电商 | 京东自有品牌 | 旗下多个自有品牌可选，覆盖不同家庭需求 | 南美牛、澳洲牛 | 牛腱子、谷饲牛排 | 约25-28/斤 |
| 收尾验证 | 最后一行品牌 | 这一行必须完整出现在长图底部之前，不能被画布截断 | 安格斯牛 | 测试用尾行内容 | 可见 |
""",
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            for name, text in cases.items():
                with self.subTest(name=name):
                    output_path = Path(tmp_dir) / f"{name}.jpg"
                    result = html_renderer._render_text_to_screenshot_with_satori(
                        text=text,
                        platform="deepseek",
                        keyword="测试关键词",
                        brand="DeepSeek",
                        output_path=str(output_path),
                        include_badges=name != "empty_text",
                    )

                    self.assertEqual(result, str(output_path))
                    self.assertTrue(output_path.exists())
                    self._assert_rendered_image_is_usable(output_path)
                    if name in {"very_long_plain", "long_wrapping_table"}:
                        self._assert_footer_is_not_cropped_at_bottom(output_path)
                        self._assert_template_background_tint_is_present(output_path)

    def test_target_emoji_renders_from_local_asset(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "target_emoji.jpg"
            result = html_renderer._render_text_to_screenshot_with_satori(
                text="🎯",
                platform="recognition",
                keyword="",
                brand="",
                output_path=str(output_path),
                include_badges=False,
            )

            self.assertEqual(result, str(output_path))
            self.assertTrue(output_path.exists())
            self._assert_rendered_image_has_red_target_pixels(output_path)

    def test_first_heading_does_not_leave_large_top_gap(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "first_heading.jpg"
            result = html_renderer._render_text_to_screenshot_with_satori(
                text="# 首行标题\n\n正文内容",
                platform="recognition",
                keyword="",
                brand="",
                output_path=str(output_path),
                include_badges=False,
            )

            self.assertEqual(result, str(output_path))
            self.assertTrue(output_path.exists())
            self._assert_first_answer_text_is_near_message_top(output_path)

    def _assert_rendered_image_is_usable(self, path: Path) -> None:
        with Image.open(path) as img:
            self.assertEqual(img.mode, "RGB")
            self.assertGreaterEqual(img.width, 900)
            self.assertGreater(img.height, 300)
            self.assertLess(img.height, 28000)
            background = Image.new("RGB", img.size, (250, 250, 250))
            diff = ImageChops.difference(img, background)
            self.assertIsNotNone(diff.getbbox(), f"{path} appears blank")

    def _assert_rendered_image_has_red_target_pixels(self, path: Path) -> None:
        with Image.open(path) as img:
            rgb = img.convert("RGB")
            red_pixels = 0
            pixels = rgb.load()
            for y in range(rgb.height):
                for x in range(rgb.width):
                    r, g, b = pixels[x, y]
                    if r > 150 and g < 130 and b < 130:
                        red_pixels += 1
            self.assertGreater(red_pixels, 20, f"{path} does not appear to contain the 🎯 emoji asset")

    def _assert_first_answer_text_is_near_message_top(self, path: Path) -> None:
        with Image.open(path) as img:
            rgb = img.convert("RGB")
            scale = rgb.width / 900
            x_start = round(178 * scale)
            x_end = round(780 * scale)
            y_start = round(220 * scale)
            y_end = round(330 * scale)
            first_dark_y = None
            for y in range(y_start, min(y_end, rgb.height)):
                for x in range(x_start, min(x_end, rgb.width)):
                    r, g, b = rgb.getpixel((x, y))
                    if r < 80 and g < 100 and b < 130:
                        first_dark_y = y / scale
                        break
                if first_dark_y is not None:
                    break
            self.assertIsNotNone(first_dark_y, f"{path} does not contain answer title pixels in the expected area")
            self.assertLess(first_dark_y, 252, f"{path} leaves too much top gap before the first answer heading")

    def _assert_footer_is_not_cropped_at_bottom(self, path: Path) -> None:
        with Image.open(path) as img:
            rgb = img.convert("RGB")
            scale = rgb.width / 900
            band_height = max(1, round(55 * scale))
            text_like_samples = 0
            sample_step = max(1, rgb.width // 200)
            for y in range(max(0, rgb.height - band_height), rgb.height):
                for x in range(0, rgb.width, sample_step):
                    r, g, b = rgb.getpixel((x, y))
                    if r < 185 and g < 195 and b < 215:
                        text_like_samples += 1
            self.assertEqual(text_like_samples, 0, f"{path} appears to have footer/text pixels cropped into the bottom edge")

            gaps = []
            for fraction in (0.1, 0.5, 0.9):
                x = min(rgb.width - 1, max(0, round(rgb.width * fraction)))
                bg = rgb.getpixel((x, rgb.height - 5))
                last_content_y = None
                for y in range(rgb.height - 1, -1, -1):
                    pixel = rgb.getpixel((x, y))
                    if sum(abs(int(a) - int(b)) for a, b in zip(pixel, bg)) > 9:
                        last_content_y = y
                        break
                if last_content_y is not None:
                    gaps.append((rgb.height - last_content_y - 1) / scale)
            self.assertTrue(gaps, f"{path} could not locate the template bottom edge")
            self.assertGreater(min(gaps), 22, f"{path} leaves too little bottom whitespace")
            self.assertLess(max(gaps), 55, f"{path} leaves excessive bottom whitespace")

    def _assert_template_background_tint_is_present(self, path: Path) -> None:
        with Image.open(path) as img:
            rgb = img.convert("RGB")
            scale = rgb.width / 900
            r, _g, b = rgb.getpixel((round(15 * scale), round(15 * scale)))
            self.assertGreater(b, r + 2, f"{path} is missing the template's blue background tint")


if __name__ == "__main__":
    unittest.main()
