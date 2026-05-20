import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from core.diagnostics import _default_suggestion
from core.selector_cache import get_learned_selector, load_selector_cache, set_learned_selector
from core.selector_heal.fingerprint import browser_automation_hash, platform_browser_automation_hash
from core.selector_heal.ranker import rank_candidates
from core.selector_heal.registry import get_field_intent
from core.selector_heal.repair import diagnose_selector_field
from platforms.base import BasePlatform


class _FakeSelectorPage:
    def __init__(self, *, current=None, candidates=None):
        self.current = current or {"selector": "", "count": 0, "visible": False, "error": ""}
        self.candidates = candidates or []

    def evaluate(self, script, arg=None):
        text = str(script or "")
        if "querySelectorAll(selector)" in text:
            return {**self.current, "selector": arg}
        if "selectorHints" in text:
            return self.candidates
        raise AssertionError(f"unexpected evaluate script: {text[:80]}")


class _RuntimeFallbackPage:
    def locator(self, selector):
        raise RuntimeError(f"selector miss: {selector}")

    def wait_for_selector(self, selector, timeout=None):
        return None


class _LearnedCachePage:
    def __init__(self):
        self.clicked_selectors = []

    def locator(self, selector):
        return _LearnedCacheLocator(self, selector)


class _LearnedCacheLocator:
    def __init__(self, page, selector):
        self._page = page
        self.selector = selector
        self.first = self

    def count(self):
        return 1

    def wait_for(self, timeout=None):
        return None

    def scroll_into_view_if_needed(self, timeout=None):
        return None

    def click(self, timeout=None, **kwargs):
        self._page.clicked_selectors.append(self.selector)


class SelectorHealTests(unittest.TestCase):
    def test_platform_browser_automation_hash_is_stable_for_key_order(self):
        first = {
            "browser_automation": {
                "deepseek": {
                    "new_chat_selector": "button",
                    "deep_think_selector": "button:has-text('深度思考')",
                    "generation_pause_selector": 'path[d^="M2 4.88"]',
                }
            }
        }
        second = {
            "browser_automation": {
                "deepseek": {
                    "deep_think_selector": "button:has-text('深度思考')",
                    "generation_pause_selector": 'path[d^="M2 4.88"]',
                    "new_chat_selector": "button",
                }
            }
        }

        self.assertEqual(
            platform_browser_automation_hash(first, "deepseek"),
            platform_browser_automation_hash(second, "deepseek"),
        )
        self.assertEqual(browser_automation_hash(first), browser_automation_hash(second))

    def test_platform_browser_automation_hash_changes_for_selector_update(self):
        before = {"browser_automation": {"deepseek": {"new_chat_selector": "button.old"}}}
        after = {"browser_automation": {"deepseek": {"new_chat_selector": "button.new"}}}

        self.assertNotEqual(
            platform_browser_automation_hash(before, "deepseek"),
            platform_browser_automation_hash(after, "deepseek"),
        )

    def test_selector_miss_default_suggestion_points_to_selector_detection(self):
        suggestion = _default_suggestion("selector_miss", "new_chat_selector 未命中")

        self.assertIn("selector", suggestion)
        self.assertIn("重新检测", suggestion)

    def test_ranks_deepseek_new_chat_candidate_by_semantics(self):
        intent = get_field_intent("deepseek", "new_chat_selector")
        self.assertIsNotNone(intent)
        candidates = [
            {
                "tag": "button",
                "role": "",
                "text": "设置",
                "ariaLabel": "",
                "title": "",
                "testId": "",
                "className": "settings",
                "selectorHints": ["button:has-text(\"设置\")"],
                "svgPathPrefixes": [],
                "bbox": {"x": 10, "y": 10, "width": 80, "height": 32},
            },
            {
                "tag": "button",
                "role": "",
                "text": "",
                "ariaLabel": "新建对话",
                "title": "",
                "testId": "",
                "className": "new-chat-entry",
                "selectorHints": ['button[aria-label="新建对话"]'],
                "svgPathPrefixes": ["M8 0.599609L12 4"],
                "bbox": {"x": 20, "y": 80, "width": 40, "height": 40},
            },
        ]

        ranked = rank_candidates(candidates, intent)

        self.assertEqual(ranked[0]["selector"], 'button[aria-label="新建对话"]')
        self.assertGreater(ranked[0]["score"], ranked[1]["score"])
        self.assertIn("aria/title", ranked[0]["reason"])

    def test_diagnose_deepseek_new_chat_returns_current_status_and_candidates(self):
        page = _FakeSelectorPage(
            current={"count": 0, "visible": False, "error": ""},
            candidates=[
                {
                    "tag": "button",
                    "role": "",
                    "text": "新对话",
                    "ariaLabel": "",
                    "title": "",
                    "testId": "",
                    "className": "",
                "selectorHints": ['text="新对话"'],
                    "svgPathPrefixes": [],
                    "bbox": {"x": 12, "y": 48, "width": 100, "height": 36},
                }
            ],
        )

        result = diagnose_selector_field(
            page,
            platform="deepseek",
            field_name="new_chat_selector",
            current_selector="button.old",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["current_status"], "missing")
        self.assertEqual(result["candidates"][0]["selector"], 'text="新对话"')
        self.assertFalse(result["candidates"][0]["verified"])

    def test_diagnose_rejects_unsupported_field(self):
        result = diagnose_selector_field(
            _FakeSelectorPage(),
            platform="deepseek",
            field_name="result_selector",
            current_selector=".ds-markdown",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["current_status"], "unsupported")

    def test_ranker_prefers_text_selector_over_hash_class_hint(self):
        intent = get_field_intent("deepseek", "new_chat_selector")
        candidate = {
            "tag": "div",
            "role": "",
            "text": "开启新对话",
            "ariaLabel": "",
            "title": "",
            "testId": "",
            "className": "_5a8ac7a",
            "selectorHints": [
                'text="开启新对话"',
                'div:text-is("开启新对话")',
                'div:has-text("开启新对话")',
                'div:has(path[d^="M8 0.599609"]):has-text("开启新对话")',
                "div._5a8ac7a",
            ],
            "svgPathPrefixes": ["M8 0.599609L12 4"],
            "bbox": {"x": 12, "y": 70, "width": 236, "height": 40},
        }

        ranked = rank_candidates([candidate], intent)

        self.assertEqual(ranked[0]["selector"], 'text="开启新对话"')

    def test_selector_cache_round_trip_is_profile_scoped(self):
        with TemporaryDirectory() as tempdir:
            self.assertEqual(get_learned_selector(tempdir, "new_chat_selector"), "")
            previous = set_learned_selector(tempdir, "new_chat_selector", "button.learned", source="vision")
            self.assertEqual(previous, "")
            self.assertEqual(get_learned_selector(tempdir, "new_chat_selector"), "button.learned")

            cache = load_selector_cache(tempdir)
            self.assertEqual(cache["new_chat_selector"]["selector"], "button.learned")
            self.assertEqual(cache["new_chat_selector"]["source"], "vision")
            self.assertEqual(int(cache["new_chat_selector"]["hit_count"]), 1)
            self.assertTrue((Path(tempdir) / ".selectors_learned.yaml.lock").exists())

            set_learned_selector(tempdir, "new_chat_selector", "button.learned", source="cache_verified")
            cache = load_selector_cache(tempdir)
            self.assertEqual(cache["new_chat_selector"]["selector"], "button.learned")
            self.assertEqual(cache["new_chat_selector"]["source"], "cache_verified")
            self.assertEqual(int(cache["new_chat_selector"]["hit_count"]), 2)

    def test_learned_selector_fallback_clicks_cached_selector(self):
        with TemporaryDirectory() as tempdir:
            set_learned_selector(tempdir, "new_chat_selector", "button.learned", source="vision")
            platform = BasePlatform.__new__(BasePlatform)
            platform.name = "deepseek"
            platform.user_data_dir = tempdir
            platform.page = _LearnedCachePage()
            platform.input_selector = "textarea"
            platform._stop_requested = False
            platform.stop_checker = None
            platform._reraise_stop_requested = lambda exc: None
            platform._raise_if_stop_requested = lambda: None
            platform._conversation_snapshot = lambda: {
                "answerCount": 0,
                "answerLength": 0,
                "inputValue": "",
                "path": "",
                "href": "",
            }
            platform._wait_for_locator = lambda locator, timeout_ms: locator.wait_for(timeout=timeout_ms)
            platform._click_locator = lambda locator, timeout_ms=5000, **kwargs: locator.click(timeout=timeout_ms, **kwargs)
            platform._cooperative_sleep = lambda *args, **kwargs: None
            platform._wait_for_page_selector = lambda selector, timeout_ms: None
            platform._wait_until_new_chat_ready = lambda before, timeout=6.0: True
            platform._remember_learned_selector = BasePlatform._remember_learned_selector.__get__(platform, BasePlatform)

            result = BasePlatform._attempt_learned_selector_heal(platform, "new_chat_selector", label="新对话")

            self.assertTrue(result)
            self.assertEqual(platform.page.clicked_selectors, ["button.learned"])
            self.assertEqual(get_learned_selector(tempdir, "new_chat_selector"), "button.learned")

    def test_new_chat_transition_allows_persisted_draft_text(self):
        platform = BasePlatform.__new__(BasePlatform)
        before = {
            "answerCount": 1,
            "answerLength": 120,
            "inputValue": "还没发送的草稿",
            "path": "/chat/old",
            "href": "https://chat.deepseek.com/chat/old",
        }
        after = {
            "answerCount": 0,
            "answerLength": 0,
            "inputValue": "还没发送的草稿",
            "path": "/chat/new",
            "href": "https://chat.deepseek.com/chat/new",
        }

        self.assertTrue(BasePlatform._new_chat_transition_ready(platform, before, after))

    def test_start_new_chat_falls_back_to_learned_selector_after_selector_miss(self):
        platform = BasePlatform.__new__(BasePlatform)
        platform.name = "deepseek"
        platform.new_chat_selector = "button.old"
        platform.input_selector = "textarea"
        platform.page = _RuntimeFallbackPage()
        platform._stop_requested = False
        platform.stop_checker = None
        platform.last_error = ""
        platform._attempt_learned_selector_heal = Mock(return_value=True)

        BasePlatform.start_new_chat(platform)

        platform._attempt_learned_selector_heal.assert_called_once_with("new_chat_selector", label="新对话")


if __name__ == "__main__":
    unittest.main()
