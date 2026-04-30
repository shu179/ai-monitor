import tempfile
import unittest

from platforms.base import BasePlatform


class NoHitReferencePlatform(BasePlatform):
    target_url = "https://example.com/chat"
    input_selector = "textarea"
    result_selector = "body"

    def __init__(self, user_data_dir: str):
        super().__init__(user_data_dir)
        self.page = object()
        self.context = object()
        self.answer_text = (
            "这是一段足够长的回答内容，用于模拟平台已经生成了有效答案，"
            "但答案里没有包含目标品牌名称，同时仍然带有外部引用来源。"
        )
        self.references_extracted = False

    def ensure_logged_in(self, timeout=15):
        return None

    def start_new_chat(self):
        return None

    def type_like_human(self, text):
        return None

    def submit_prompt(self):
        return None

    def _get_answer_text(self):
        return ""

    def _poll_until_complete(self, brand, on_rank, get_text=None, keyword: str = ""):
        self.last_answer_text = self.answer_text
        on_rank(99, self.answer_text)

    def _should_simulate_human_behavior(self):
        return False

    def _maybe_extract_answer_reference_metadata(self):
        super()._maybe_extract_answer_reference_metadata()
        self.references_extracted = True

    def extract_answer_references(self):
        return [{"index": 1, "title": "来源标题", "url": "https://example.com/a", "source": "Example"}]

    def extract_body_references(self):
        return [{"index": 2, "title": "正文来源", "url": "https://example.com/b", "source": "Body"}]

    def _apply_retry_backoff(self, attempt, max_retries, reason):
        return None


class ReferenceExtractionOnNoHitTests(unittest.TestCase):
    def test_search_extracts_references_even_when_brand_is_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            platform = NoHitReferencePlatform(tmpdir)
            platform.extract_references_enabled = True

            rank, screenshot = platform.search(
                keyword="杭州geo优化公司",
                brand="即搜",
                max_retries=1,
                deep_think=False,
            )

            self.assertEqual(rank, 99)
            self.assertIsNone(screenshot)
            self.assertTrue(platform.references_extracted)
            self.assertEqual([item["url"] for item in platform.last_references], ["https://example.com/a"])
            self.assertEqual([item["url"] for item in platform.last_body_references], ["https://example.com/b"])


if __name__ == "__main__":
    unittest.main()
