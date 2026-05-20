import unittest

from platforms.yuanbao import YuanbaoPlatform


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


if __name__ == "__main__":
    unittest.main()
