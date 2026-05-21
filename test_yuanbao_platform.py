import unittest

from platforms.yuanbao import YuanbaoPlatform


class _FakeReferencePanelPage:
    def __init__(self):
        self.phase = 0

    def evaluate(self, script: str, arg=None):
        if "data-yb-reference-scroll-root" in script and "scrollHeight" in script and "score" in script:
            if self.phase >= 2:
                return {
                    "x": 100,
                    "y": 120,
                    "width": 360,
                    "height": 420,
                    "scrollTop": 800,
                    "scrollHeight": 1200,
                    "clientHeight": 400,
                    "score": 400,
                }
            return {
                "x": 100,
                "y": 120,
                "width": 360,
                "height": 420,
                "scrollTop": self.phase * 400,
                "scrollHeight": 1200,
                "clientHeight": 400,
                "score": 400,
            }
        if "data-yb-reference-scroll-root" in script and "before" in script and "after" in script:
            if self.phase >= 2:
                return {"ok": False}
            self.phase += 1
            return {"ok": True, "before": (self.phase - 1) * 400, "after": self.phase * 400, "maxTop": 800}
        raise AssertionError(f"unexpected evaluate script: {script[:120]}")


class _FakeReferenceClickLocator:
    def __init__(self, *, click_raises: bool = False):
        self.click_raises = click_raises
        self.hover_calls = 0
        self.click_calls = 0
        self.last_click_kwargs: dict | None = None

    @property
    def first(self):
        return self

    def hover(self, timeout=None):
        self.hover_calls += 1

    def click(self, timeout=None, **kwargs):
        self.click_calls += 1
        self.last_click_kwargs = kwargs
        if self.click_raises:
            raise RuntimeError("simulated playwright click failure")


class _FakeReferenceOpenPage:
    def __init__(self, *, click_raises: bool = False):
        self.marked = False
        self.dispatched = False
        self.locator_selector: str | None = None
        self.last_locator: _FakeReferenceClickLocator | None = None
        self._click_raises = click_raises

    def evaluate(self, script: str, arg=None):
        if "data-yb-source-click" in script and "search-guide-tool" in script and "scoreCandidate" in script:
            self.marked = True
            return {"found": True, "score": 2400, "text": "源 search-guide-tool citation"}
        if "data-yb-source-click" in script and "dispatchEvent" in script:
            self.dispatched = True
            return True
        raise AssertionError(f"unexpected evaluate script: {script[:120]}")

    def locator(self, selector: str):
        assert selector == '[data-yb-source-click="1"]', (
            f"locator selector mismatch: {selector}"
        )
        self.locator_selector = selector
        self.last_locator = _FakeReferenceClickLocator(click_raises=self._click_raises)
        return self.last_locator


class _FakeLatestToolbarScrollPage:
    def __init__(self):
        self.scrolled = False

    def evaluate(self, script: str, arg=None):
        if "window.scrollTo" in script or "scrollIntoView" in script:
            raise AssertionError("latest toolbar scroll must stay inside the chat container")
        if "maxTop" in script and "containerSel" in script:
            self.scrolled = True
            return {"found": True, "scrolled": True, "before": 120, "after": 960, "maxTop": 960}
        raise AssertionError(f"unexpected evaluate script: {script[:120]}")


class YuanbaoPlatformTests(unittest.TestCase):
    def test_reference_open_selector_has_semantic_fallbacks(self):
        selector = YuanbaoPlatform.reference_open_selector
        self.assertIn('#search-guide-tool[data-toolbar-type="citation"]', selector)
        self.assertIn('[data-toolbar-type="citation"]', selector)
        self.assertIn("ToolbarSearchGuid_searchGuidTool", selector)
        self.assertIn("ToolbarSearchGuid_source", selector)
        self.assertIn('button:has-text("来源")', selector)
        self.assertIn('[role="button"]:has-text("引用")', selector)
        self.assertIn('button:has-text("源")', selector)

    def test_reference_scroll_sampling_merges_lazy_loaded_cards(self):
        platform = YuanbaoPlatform("/tmp/yuanbao-test")
        platform.page = _FakeReferencePanelPage()
        platform._cooperative_sleep_jittered = lambda *args, **kwargs: None

        def collect():
            phase = platform.page.phase
            return [
                {
                    "index": phase + 1,
                    "title": f"引用 {phase + 1}",
                    "url": f"https://example.com/{phase + 1}",
                    "source": "Example",
                }
            ]

        platform._collect_reference_cards = collect

        references = platform._collect_reference_cards_with_scroll_sampling(
            [{"index": 1, "title": "引用 1", "url": "https://example.com/1", "source": "Example"}],
            max_passes=5,
        )

        self.assertEqual([item["url"] for item in references], [
            "https://example.com/1",
            "https://example.com/2",
            "https://example.com/3",
        ])
        self.assertEqual([item["index"] for item in references], [1, 2, 3])

    def test_reference_open_prefers_playwright_real_click(self):
        platform = YuanbaoPlatform("/tmp/yuanbao-test")
        platform.page = _FakeReferenceOpenPage()
        platform._cooperative_sleep = lambda *args, **kwargs: None

        self.assertTrue(platform._click_reference_open_button())
        self.assertTrue(
            platform.page.marked,
            "JS scoring 必须给目标打 data-yb-source-click 标记",
        )
        self.assertIsNotNone(
            platform.page.last_locator,
            "必须用 Playwright locator 真实点击",
        )
        self.assertGreaterEqual(
            platform.page.last_locator.click_calls,
            1,
            "Playwright click 必须被调用",
        )
        self.assertFalse(
            platform.page.dispatched,
            "Playwright 点击成功后不应回退到 JS dispatchEvent",
        )

    def test_reference_open_falls_back_to_js_when_playwright_fails(self):
        platform = YuanbaoPlatform("/tmp/yuanbao-test")
        platform.page = _FakeReferenceOpenPage(click_raises=True)
        platform._cooperative_sleep = lambda *args, **kwargs: None

        self.assertTrue(platform._click_reference_open_button())
        self.assertTrue(
            platform.page.dispatched,
            "Playwright click 失败后必须回退到 JS dispatchEvent",
        )

    def test_latest_toolbar_scroll_uses_chat_container_only(self):
        platform = YuanbaoPlatform("/tmp/yuanbao-test")
        platform.page = _FakeLatestToolbarScrollPage()

        self.assertTrue(platform._scroll_latest_answer_toolbar_into_view())
        self.assertTrue(platform.page.scrolled)


if __name__ == "__main__":
    unittest.main()
