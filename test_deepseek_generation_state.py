import unittest

from platforms.deepseek import DeepSeekPlatform


class _FakePage:
    def __init__(self, snapshot: dict) -> None:
        self.snapshot = dict(snapshot)

    def evaluate(self, script, payload=None):
        del script, payload
        return dict(self.snapshot)


class _GenerationProbe(DeepSeekPlatform):
    def __init__(self, snapshot: dict) -> None:
        self.name = "DeepSeek"
        self.page = _FakePage(snapshot)
        self.input_selector = "textarea"
        self.result_selector = ".ds-message .ds-markdown"
        self.think_content_selector = ".ds-think-content"

    def _raise_if_stop_requested(self) -> None:
        return None

    def _reraise_stop_requested(self, exc: Exception) -> None:
        return None


class DeepSeekGenerationStateTests(unittest.TestCase):
    def test_stop_text_signal_keeps_generation_incomplete(self) -> None:
        platform = _GenerationProbe({
            "stop_visible_count": 1,
            "stop_path_count": 0,
            "send_visible_count": 0,
            "input_length": 0,
            "answer_count": 1,
            "answer_length": 120,
        })

        state = platform._get_generation_signal_state()

        self.assertTrue(state["is_generating"])
        self.assertFalse(platform.is_generation_complete("", 0))

    def test_stop_icon_path_signal_keeps_generation_incomplete(self) -> None:
        platform = _GenerationProbe({
            "stop_visible_count": 0,
            "stop_path_count": 1,
            "send_visible_count": 0,
            "input_length": 0,
            "answer_count": 1,
            "answer_length": 120,
        })

        state = platform._get_generation_signal_state()

        self.assertTrue(state["is_generating"])
        self.assertFalse(platform.is_generation_complete("", 0))

    def test_generation_state_uses_configured_pause_selector(self) -> None:
        class RecordingPage(_FakePage):
            def __init__(self) -> None:
                super().__init__({
                    "stop_visible_count": 0,
                    "stop_path_count": 1,
                    "send_visible_count": 0,
                    "input_length": 0,
                    "answer_count": 1,
                    "answer_length": 120,
                })
                self.payload = None

            def evaluate(self, script, payload=None):
                self.payload = payload
                return super().evaluate(script, payload)

        page = RecordingPage()
        platform = _GenerationProbe({})
        platform.page = page
        platform.generation_pause_selector = 'div:has(path[d^="M2 4.88C2 3.68009"])'

        state = platform._get_generation_signal_state()

        self.assertTrue(state["is_generating"])
        self.assertEqual(page.payload["pauseSel"], 'div:has(path[d^="M2 4.88C2 3.68009"])')

    def test_send_ready_without_stop_signal_marks_generation_complete(self) -> None:
        platform = _GenerationProbe({
            "stop_visible_count": 0,
            "stop_path_count": 0,
            "send_visible_count": 1,
            "input_length": 0,
            "answer_count": 1,
            "answer_length": 120,
        })

        state = platform._get_generation_signal_state()

        self.assertFalse(state["is_generating"])
        self.assertTrue(state["send_ready"])
        self.assertTrue(platform.is_generation_complete("", 0))

    def test_answer_without_stop_signal_can_complete_even_if_send_button_is_not_detected(self) -> None:
        platform = _GenerationProbe({
            "stop_visible_count": 0,
            "stop_path_count": 0,
            "send_visible_count": 0,
            "input_length": 0,
            "answer_count": 1,
            "answer_length": 120,
        })

        self.assertTrue(platform.is_generation_complete("", 0))

    def test_no_stop_send_or_answer_signal_is_not_complete(self) -> None:
        platform = _GenerationProbe({
            "stop_visible_count": 0,
            "stop_path_count": 0,
            "send_visible_count": 0,
            "input_length": 0,
            "answer_count": 0,
            "answer_length": 0,
        })

        state = platform._get_generation_signal_state()

        self.assertFalse(state["is_generating"])
        self.assertFalse(state["send_ready"])
        self.assertFalse(platform.is_generation_complete("", 0))


if __name__ == "__main__":
    unittest.main()
