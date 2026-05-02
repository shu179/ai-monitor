import unittest

from core.reference_urls import extract_reference_urls_from_text, normalize_reference_url


class ReferenceUrlTests(unittest.TestCase):
    def test_normalize_reference_url_strips_fragment_tracking_www_and_upgrades_http(self):
        self.assertEqual(
            normalize_reference_url("http://www.example.com/news/a?utm_source=feed&utm_campaign=x#section"),
            "https://example.com/news/a",
        )

    def test_extract_reference_urls_unwraps_redirect_and_deduplicates(self):
        text = (
            "参考 https://short.example/redirect?"
            "target=http%3A%2F%2Fwww.example.com%2Fnews%2Fa%3Futm_source%3Dfoo%23frag"
            "，以及 www.example.com/news/a?utm_source=foo#frag"
        )

        self.assertEqual(extract_reference_urls_from_text(text), ["https://example.com/news/a"])


if __name__ == "__main__":
    unittest.main()
