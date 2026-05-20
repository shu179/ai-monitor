import unittest

from platforms.doubao import DoubaoPlatform


class _FakeReferenceButtonLocator:
    def __init__(self, text: str):
        self._text = text
        self.click_count = 0

    def filter(self, **kwargs):
        return self

    @property
    def last(self):
        return self

    def count(self):
        return 1

    def inner_text(self):
        return self._text

    def click(self, timeout=0):
        self.click_count += 1


class _FakeReferencePage:
    def __init__(
        self,
        button_text: str,
        phase_results: dict[str, list[dict]],
        *,
        phase_order=None,
    ):
        self.button_locator = _FakeReferenceButtonLocator(button_text)
        self.phase_results = phase_results
        self.phase_order = list(phase_order or ["top", "middle", "bottom"])
        self.phase_index = 0
        self.phase = self.phase_order[self.phase_index]
        self.wheel_calls = []

    def locator(self, selector: str):
        if selector != "div, span":
            raise AssertionError(f"unexpected locator selector: {selector}")
        return self.button_locator

    def evaluate(self, script: str, arg=None):
        if "scrollHeight" in script and "clientHeight" in script and "scrollTop" in script:
            max_index = max(1, len(self.phase_order) - 1)
            return {
                "x": 100,
                "y": 100,
                "width": 400,
                "height": 600,
                "scrollTop": min(self.phase_index, max_index) * 100,
                "scrollHeight": max_index * 100 + 100,
                "clientHeight": 100,
            }
        if "document.querySelectorAll('a[href]')" in script:
            return list(self.phase_results.get(self.phase, []))
        raise AssertionError(f"unexpected evaluate script: {script[:120]}")

    def advance_wheel(self, **kwargs):
        self.wheel_calls.append(kwargs)
        if self.phase_index + 1 < len(self.phase_order):
            self.phase_index += 1
            self.phase = self.phase_order[self.phase_index]
        return True


class DoubaoPlatformTests(unittest.TestCase):
    def test_selector_looks_xpath(self):
        self.assertTrue(DoubaoPlatform._selector_looks_xpath("xpath=//div[@role='button']"))
        self.assertTrue(DoubaoPlatform._selector_looks_xpath("//div[@role='button']"))
        self.assertFalse(DoubaoPlatform._selector_looks_xpath("button[aria-label='发送']"))

    def test_classify_status_button_state_supports_text_fallbacks(self):
        platform = DoubaoPlatform("/tmp/doubao-test")
        self.assertEqual(
            platform._classify_status_button_state([], text="停止生成", data_state="", outer_html=""),
            "pause",
        )
        self.assertEqual(
            platform._classify_status_button_state([], text="发送消息", data_state="", outer_html=""),
            "send",
        )
        self.assertEqual(
            platform._classify_status_button_state([], text="语音输入", data_state="", outer_html=""),
            "voice",
        )

    def test_classify_status_button_state_prefers_svg_paths_when_present(self):
        platform = DoubaoPlatform("/tmp/doubao-test")
        self.assertEqual(platform._classify_status_button_state([platform._status_pause_path_prefix]), "pause")
        self.assertEqual(platform._classify_status_button_state([platform._status_send_path_prefix]), "send")
        self.assertEqual(platform._classify_status_button_state([platform._status_voice_path_prefix]), "voice")

    def test_parse_reference_expected_count(self):
        self.assertEqual(DoubaoPlatform._parse_reference_expected_count("参考 10 篇资料"), 10)
        self.assertEqual(DoubaoPlatform._parse_reference_expected_count("参考8篇资料"), 8)
        self.assertEqual(DoubaoPlatform._parse_reference_expected_count("没有引用"), 0)

    def test_reference_wheel_max_passes_is_bounded(self):
        self.assertEqual(DoubaoPlatform._reference_wheel_max_passes(0), 8)
        self.assertEqual(DoubaoPlatform._reference_wheel_max_passes(10), 9)
        self.assertEqual(DoubaoPlatform._reference_wheel_max_passes(200), 36)

    def test_extract_answer_references_stops_after_bottom_when_expected_count_reached(self):
        platform = DoubaoPlatform("/tmp/doubao-test")
        platform._cooperative_sleep_jittered = lambda *args, **kwargs: None
        platform._perform_auxiliary_wheel_pass = lambda **kwargs: platform.page.advance_wheel(**kwargs)
        platform.page = _FakeReferencePage(
            "参考 10 篇资料",
            {
                "top": [
                    {"index": i, "title": f"top-{i}", "url": f"https://example.com/{i}", "source": "Example"}
                    for i in range(1, 7)
                ],
                "bottom": [
                    {"index": i, "title": f"bottom-{i}", "url": f"https://example.com/{i}", "source": "Example"}
                    for i in range(5, 11)
                ],
                "middle": [],
            },
            phase_order=["top", "bottom", "middle"],
        )

        references = platform.extract_answer_references()

        self.assertEqual(len(references), 10)
        self.assertEqual(len(platform.page.wheel_calls), 1)
        self.assertEqual({ref["url"] for ref in references}, {f"https://example.com/{i}" for i in range(1, 11)})

    def test_extract_answer_references_uses_wheel_sampling_without_looping(self):
        platform = DoubaoPlatform("/tmp/doubao-test")
        platform._cooperative_sleep_jittered = lambda *args, **kwargs: None
        platform._perform_auxiliary_wheel_pass = lambda **kwargs: platform.page.advance_wheel(**kwargs)
        platform.page = _FakeReferencePage(
            "参考 10 篇资料",
            {
                "top": [
                    {"index": i, "title": f"top-{i}", "url": f"https://example.com/{i}", "source": "Example"}
                    for i in range(1, 5)
                ],
                "bottom": [
                    {"index": i, "title": f"bottom-{i}", "url": f"https://example.com/{i}", "source": "Example"}
                    for i in range(7, 11)
                ],
                "middle": [
                    {"index": i, "title": f"middle-{i}", "url": f"https://example.com/{i}", "source": "Example"}
                    for i in range(4, 8)
                ],
            },
            phase_order=["top", "middle", "bottom"],
        )

        references = platform.extract_answer_references()

        self.assertEqual(len(references), 10)
        self.assertEqual(len(platform.page.wheel_calls), 2)
        self.assertEqual({ref["url"] for ref in references}, {f"https://example.com/{i}" for i in range(1, 11)})
        self.assertEqual([ref["index"] for ref in references], list(range(1, 11)))


if __name__ == "__main__":
    unittest.main()
