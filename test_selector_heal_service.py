import unittest
from unittest.mock import patch

from backend_lib.selector_heal_service import SelectorHealService
from core.selector_heal.verifier import verify_selector_candidates


class _FakePage:
    def __init__(self):
        self.clicked_selectors = []

    def evaluate(self, script, arg=None):
        text = str(script or "")
        if "querySelectorAll(selector)" in text:
            return {"selector": arg, "count": 0, "visible": False, "error": ""}
        if "selectorHints" in text:
            return [
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
                    "bbox": {"x": 10, "y": 20, "width": 80, "height": 32},
                }
            ]
        raise AssertionError(f"unexpected script: {text[:80]}")

    def locator(self, selector):
        return _FakeLocator(self, selector)


class _FakeLocator:
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

    def click(self, timeout=None):
        self._page.clicked_selectors.append(self.selector)


class _FakePlatform:
    def __init__(self):
        self.page = None
        self.inspect = False
        self.prefer_headed_runtime = False
        self.new_chat_selector = "button.old"
        self.closed = False
        self.started = False
        self.ready_after_click = True

    def start(self):
        self.started = True
        self.page = _FakePage()
        return self

    def close(self):
        self.closed = True

    def _conversation_snapshot(self):
        return {"answerCount": 1, "answerLength": 100, "inputValue": "", "path": "/a", "href": "https://chat.deepseek.com/a"}

    def _wait_for_locator(self, locator, timeout_ms):
        locator.wait_for(timeout=timeout_ms)

    def _click_locator(self, locator, timeout_ms=5000, **kwargs):
        locator.click(timeout=timeout_ms)

    def _cooperative_sleep(self, seconds, interval=0.1):
        return None

    def _wait_for_page_selector(self, selector, timeout_ms):
        if selector != "textarea":
            raise AssertionError(selector)

    def _wait_until_new_chat_ready(self, before, timeout=6.0):
        return bool(self.ready_after_click)


class _PauseStateFakePlatform(_FakePlatform):
    def __init__(self):
        super().__init__()
        self.page = object()
        self.signal_states = [
            {
                "is_generating": True,
                "stop_visible_count": 0,
                "stop_path_count": 1,
                "send_visible_count": 0,
                "input_length": 0,
                "answer_count": 0,
                "answer_length": 0,
                "stop_controls": [],
                "stop_paths": [{"selectorHints": ['path[d^="M2 4.88C2 3.68009"]'], "dPrefix": "M2 4.88C2 3.68009"}],
                "stop_selector_hints": ['div:has(path[d^="M2 4.88C2 3.68009"])'],
            },
            {
                "is_generating": True,
                "stop_visible_count": 0,
                "stop_path_count": 1,
                "send_visible_count": 0,
                "input_length": 0,
                "answer_count": 0,
                "answer_length": 0,
                "stop_controls": [],
                "stop_paths": [{"selectorHints": ['path[d^="M2 4.88C2 3.68009"]'], "dPrefix": "M2 4.88C2 3.68009"}],
                "stop_selector_hints": ['div:has(path[d^="M2 4.88C2 3.68009"])'],
            },
            {
                "is_generating": False,
                "stop_visible_count": 0,
                "stop_path_count": 0,
                "send_visible_count": 1,
                "input_length": 0,
                "answer_count": 1,
                "answer_length": 120,
                "stop_controls": [],
                "stop_paths": [],
                "stop_selector_hints": [],
            },
        ]
        self.typed_prompts = []
        self.logged_in = False
        self.new_chat_started = False
        self.submitted = False

    def ensure_logged_in(self, timeout=20):
        self.logged_in = True

    def start_new_chat(self):
        self.new_chat_started = True

    def type_like_human(self, prompt):
        self.typed_prompts.append(prompt)

    def submit_prompt(self):
        self.submitted = True

    def _get_generation_signal_state(self):
        if self.signal_states:
            return self.signal_states.pop(0)
        return {
            "is_generating": False,
            "stop_visible_count": 0,
            "stop_path_count": 0,
            "send_visible_count": 1,
            "input_length": 0,
            "answer_count": 1,
            "answer_length": 120,
            "stop_controls": [],
            "stop_paths": [],
            "stop_selector_hints": [],
        }


class SelectorHealServiceTests(unittest.TestCase):
    def test_diagnose_deepseek_new_chat_is_read_only_and_closes_platform(self):
        created = []

        def factory(platform_name, *, config=None, inspect=False, stop_checker=None):
            self.assertEqual(platform_name, "deepseek")
            self.assertTrue(inspect)
            self.assertIsNone(stop_checker)
            platform = _FakePlatform()
            created.append(platform)
            return platform

        service = SelectorHealService(
            config_loader=lambda: {"browser_automation": {"deepseek": {}}},
            platform_factory=factory,
        )

        result = service.diagnose({"platform": "deepseek", "fields": ["new_chat_selector"]})

        self.assertTrue(result["ok"])
        self.assertEqual(result["platform"], "deepseek")
        self.assertFalse(result["verify"])
        self.assertFalse(result["vision"])
        self.assertEqual(result["results"][0]["current_status"], "missing")
        self.assertEqual(result["results"][0]["candidates"][0]["selector"], 'text="新对话"')
        self.assertTrue(created[0].started)
        self.assertTrue(created[0].closed)
        self.assertTrue(created[0].inspect)
        self.assertTrue(created[0].prefer_headed_runtime)

    def test_verify_true_verifies_deepseek_new_chat_in_temporary_platform(self):
        created = []
        service = SelectorHealService(
            config_loader=lambda: {},
            platform_factory=lambda *args, **kwargs: created.append(_FakePlatform()) or created[-1],
        )

        result = service.diagnose({"platform": "deepseek", "fields": ["new_chat_selector"], "verify": True})

        self.assertTrue(result["ok"])
        self.assertTrue(result["verify"])
        self.assertEqual(result["results"][0]["verify_status"], "passed")
        self.assertTrue(result["results"][0]["candidates"][0]["verified"])
        self.assertEqual(created[0].page.clicked_selectors, ['text="新对话"'])
        self.assertTrue(created[0].closed)

    def test_diagnose_auto_apply_saves_verified_new_chat_selector(self):
        config = {"browser_automation": {"deepseek": {"new_chat_selector": "button.old"}}}
        saved = []
        closed = []

        def writer(platform, field, selector):
            previous = str(config["browser_automation"].setdefault(platform, {}).get(field) or "").strip()
            config["browser_automation"][platform][field] = selector
            saved.append((platform, field, selector))
            return previous

        service = SelectorHealService(
            config_loader=lambda: config,
            platform_factory=lambda *args, **kwargs: _FakePlatform(),
            selector_config_writer=writer,
            platform_session_closer=lambda platform, reason: closed.append((platform, reason)),
        )

        result = service.diagnose({
            "platform": "deepseek",
            "fields": ["new_chat_selector"],
            "verify": True,
            "auto_apply": True,
        })

        field_result = result["results"][0]
        self.assertTrue(result["ok"])
        self.assertTrue(field_result["saved"])
        self.assertEqual(field_result["selector"], 'text="新对话"')
        self.assertEqual(field_result["current_selector"], 'text="新对话"')
        self.assertEqual(field_result["current_status"], "healthy")
        self.assertEqual(config["browser_automation"]["deepseek"]["new_chat_selector"], 'text="新对话"')
        self.assertEqual(saved, [("deepseek", "new_chat_selector", 'text="新对话"')])
        self.assertEqual(closed, [("deepseek", "selector 配置已更新")])

    def test_verifier_skips_low_semantic_score_candidates(self):
        platform = _FakePlatform().start()
        diagnosis = {
            "ok": True,
            "platform": "deepseek",
            "field": "new_chat_selector",
            "current_status": "missing",
            "candidates": [
                {
                    "selector": 'text="智能搜索"',
                    "score": 0.16,
                    "verified": False,
                }
            ],
        }

        result = verify_selector_candidates(platform, diagnosis)

        self.assertEqual(result["verify_status"], "failed")
        self.assertFalse(result["candidates"][0]["verified"])
        self.assertIn("置信度过低", result["candidates"][0]["verify_reason"])
        self.assertEqual(platform.page.clicked_selectors, [])

    def test_unsupported_field_is_rejected_before_browser_start(self):
        calls = []
        service = SelectorHealService(
            config_loader=lambda: {},
            platform_factory=lambda *args, **kwargs: calls.append(args) or _FakePlatform(),
        )

        result = service.diagnose({"platform": "deepseek", "fields": ["result_selector"]})

        self.assertFalse(result["ok"])
        self.assertEqual(calls, [])
        self.assertIn("白名单", result["message"])

    def test_apply_revalidates_candidate_saves_config_and_closes_session(self):
        config = {"browser_automation": {"deepseek": {"new_chat_selector": "button.old"}}}
        saved = []
        closed = []
        service = SelectorHealService(
            config_loader=lambda: config,
            platform_factory=lambda *args, **kwargs: _FakePlatform(),
            config_saver=lambda next_config: saved.append(next_config.copy()),
            platform_session_closer=lambda platform, reason: closed.append((platform, reason)),
        )

        result = service.apply(
            {
                "platform": "deepseek",
                "field": "new_chat_selector",
                "candidate_selector": 'text="新对话"',
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(config["browser_automation"]["deepseek"]["new_chat_selector"], 'text="新对话"')
        self.assertEqual(result["previous_selector"], "button.old")
        self.assertEqual(len(saved), 1)
        self.assertEqual(closed, [("deepseek", "selector 配置已更新")])

    def test_apply_does_not_save_unverified_candidate(self):
        config = {"browser_automation": {"deepseek": {"new_chat_selector": "button.old"}}}
        saved = []
        service = SelectorHealService(
            config_loader=lambda: config,
            platform_factory=lambda *args, **kwargs: _FakePlatform(),
            config_saver=lambda next_config: saved.append(next_config.copy()),
        )

        result = service.apply(
            {
                "platform": "deepseek",
                "field": "new_chat_selector",
                "candidate_selector": 'text="智能搜索"',
            }
        )

        self.assertFalse(result["ok"])
        self.assertEqual(config["browser_automation"]["deepseek"]["new_chat_selector"], "button.old")
        self.assertEqual(saved, [])
        self.assertIn("未通过", result["message"])

    def test_apply_is_blocked_when_runtime_is_not_safe(self):
        config = {"browser_automation": {"deepseek": {"new_chat_selector": "button.old"}}}
        saved = []
        service = SelectorHealService(
            config_loader=lambda: config,
            platform_factory=lambda *args, **kwargs: _FakePlatform(),
            config_saver=lambda next_config: saved.append(next_config.copy()),
            runtime_safety_checker=lambda: {
                "runtime_safe": False,
                "blocking_reason": "正式抓取任务正在运行",
                "checks": {"monitoring_running": True},
            },
        )

        result = service.apply(
            {
                "platform": "deepseek",
                "field": "new_chat_selector",
                "candidate_selector": 'text="新对话"',
            }
        )

        self.assertFalse(result["ok"])
        self.assertFalse(result["runtime_safe"])
        self.assertIn("正式抓取", result["message"])
        self.assertEqual(config["browser_automation"]["deepseek"]["new_chat_selector"], "button.old")
        self.assertEqual(saved, [])

    def test_diagnose_pause_state_saves_verified_selector(self):
        config = {"browser_automation": {"deepseek": {"generation_pause_selector": 'path[d^="M2 4.88"]'}}}
        saved = []
        closed = []

        expected_selector = 'div:has(path[d^="M2 4.88C2 3.68009"])'

        def writer(platform, field, selector):
            previous = str(config["browser_automation"].setdefault(platform, {}).get(field) or "").strip()
            config["browser_automation"][platform][field] = selector
            saved.append((platform, field, selector))
            return previous

        service = SelectorHealService(
            config_loader=lambda: config,
            platform_factory=lambda *args, **kwargs: _PauseStateFakePlatform(),
            selector_config_writer=writer,
            platform_session_closer=lambda platform, reason: closed.append((platform, reason)),
        )

        with patch("backend_lib.selector_heal_service.time.time", return_value=0.0), patch(
            "backend_lib.selector_heal_service.time.sleep", return_value=None
        ):
            result = service.diagnose_pause_state({"platform": "deepseek", "timeout": 5, "interval": 0.1})

        self.assertTrue(result["ok"])
        self.assertTrue(result["saved"])
        self.assertEqual(result["selector"], expected_selector)
        self.assertEqual(config["browser_automation"]["deepseek"]["generation_pause_selector"], expected_selector)
        self.assertEqual(saved, [("deepseek", "generation_pause_selector", expected_selector)])
        self.assertEqual(closed, [("deepseek", "selector 配置已更新")])


if __name__ == "__main__":
    unittest.main()
