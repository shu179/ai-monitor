import unittest

from platforms.tongyi import TongyiPlatform


class TongyiPlatformTests(unittest.TestCase):
    def test_deep_think_selector_targets_current_qianwen_button(self):
        selector = TongyiPlatform.deep_think_selector
        self.assertIn('data-input-capsule-login-gate^="deep-think"', selector)
        self.assertIn('button[aria-label="思考"][aria-pressed]', selector)
        self.assertIn('qwpcicon-deepThinking', selector)

    def test_think_content_selector_covers_new_qianwen_markers(self):
        selector = TongyiPlatform.think_content_selector
        self.assertIn("reasoning", selector)
        self.assertIn("data-testid*=\"think\"", selector)
        self.assertIn("deepThink", selector)
        self.assertIn("thinkingWrap", selector)
        self.assertIn("thinkingTitle", selector)
        self.assertIn("thinkingHeader", selector)
        self.assertIn("data-card_name=\"deep_think\"", selector)

    def test_text_stable_completion_requires_complete_non_thinking_answer(self):
        platform = TongyiPlatform("/tmp/tongyi-test")
        self.assertFalse(platform._allow_text_stable_completion({}))
        self.assertFalse(platform._allow_text_stable_completion({"send_ready": True}))
        self.assertFalse(
            platform._allow_text_stable_completion(
                {"last_block_complete": True, "active_thinking": True}
            )
        )
        self.assertFalse(
            platform._allow_text_stable_completion(
                {"last_block_complete": True, "active_printing": True}
            )
        )
        self.assertFalse(
            platform._allow_text_stable_completion(
                {"last_block_complete": True, "stop_visible": True}
            )
        )
        self.assertTrue(
            platform._allow_text_stable_completion(
                {"last_block_complete": True, "active_thinking": False, "stop_visible": False}
            )
        )


if __name__ == "__main__":
    unittest.main()
