import tempfile
import unittest
from unittest.mock import patch

from platforms.base import BasePlatform


class FakePage:
    def __init__(self, url: str):
        self.url = url
        self.closed = False

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, pages):
        self.pages = pages

    def new_page(self):
        page = FakePage("about:blank")
        self.pages.append(page)
        return page


class TestPlatform(BasePlatform):
    target_url = "https://yuanbao.tencent.com/chat"


class PlatformPrimaryPageTests(unittest.TestCase):
    def test_prefers_target_page_over_initial_blank_page(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            platform = TestPlatform(tmpdir)
            blank_page = FakePage("about:blank")
            target_page = FakePage("https://yuanbao.tencent.com/chat/abc")
            platform.context = FakeContext([blank_page, target_page])

            selected = platform._ensure_primary_page(retries=0)

            self.assertIs(selected, target_page)
            self.assertIs(platform.page, target_page)
            self.assertTrue(blank_page.closed)
            self.assertFalse(target_page.closed)

    def test_keeps_non_blank_fallback_when_no_target_page_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            platform = TestPlatform(tmpdir)
            blank_page = FakePage("about:blank")
            other_page = FakePage("https://example.com/")
            platform.context = FakeContext([blank_page, other_page])

            selected = platform._ensure_primary_page(retries=0)

            self.assertIs(selected, other_page)
            self.assertTrue(blank_page.closed)
            self.assertFalse(other_page.closed)

    def test_can_keep_extra_pages_during_startup_selection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            platform = TestPlatform(tmpdir)
            blank_page = FakePage("about:blank")
            target_page = FakePage("https://yuanbao.tencent.com/chat/abc")
            platform.context = FakeContext([blank_page, target_page])

            selected = platform._ensure_primary_page(retries=0, close_extra_pages=False)

            self.assertIs(selected, target_page)
            self.assertFalse(blank_page.closed)
            self.assertFalse(target_page.closed)

    def test_inspect_mode_only_reclaims_profile_processes_when_forced(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            platform = TestPlatform(tmpdir)
            platform.inspect = True

            with patch("platforms.base.browser_profile_owner_pids", return_value=[123]), patch(
                "platforms.base.browser_profile_process_tree_is_orphaned",
                return_value=False,
            ), patch(
                "platforms.base.terminate_browser_profile_processes",
                return_value=True,
            ) as terminate:
                self.assertFalse(platform._reclaim_orphaned_profile_processes())
                terminate.assert_not_called()

                self.assertTrue(platform._reclaim_orphaned_profile_processes(force=True))
                terminate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
