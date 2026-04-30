import unittest
from unittest.mock import patch

from core.batch_test_runner import BatchTestRunner


class FakeBrowserPlatform:
    def __init__(self):
        self.page = object()
        self.closed = False
        self.last_error = ""
        self.last_references = []
        self.last_body_references = []
        self.deep_think = False
        self.extract_references_enabled = False
        self.screenshot_on_mention = True

    def search(self, keyword, brand, max_retries=1, deep_think=True):
        return 1, None

    def close(self):
        self.closed = True


class BatchHeadedModeTests(unittest.TestCase):
    def test_single_query_passes_inspect_to_browser_factory(self):
        captured = {}
        created = {}

        def fake_create_browser_platform(*, platform_name, config, inspect, stop_checker):
            captured["platform_name"] = platform_name
            captured["inspect"] = inspect
            captured["stop_checker"] = stop_checker
            created["platform"] = FakeBrowserPlatform()
            return created["platform"]

        runner = BatchTestRunner({}, None)
        try:
            with patch(
                "core.browser_platform_factory.create_browser_platform",
                side_effect=fake_create_browser_platform,
            ):
                result = runner._execute_single_query(
                    keyword="杭州geo优化公司",
                    brand="即搜",
                    platform="yuanbao",
                    deep_think=True,
                    inspect=True,
                    batch_id="batch_headed_mode",
                    query_index=1,
                )
        finally:
            runner.close()

        self.assertEqual(captured["platform_name"], "yuanbao")
        self.assertIs(captured["inspect"], True)
        self.assertIsNone(captured["stop_checker"])
        self.assertTrue(created["platform"].force_reclaim_profile_processes_on_start)
        self.assertTrue(result["success"])
        self.assertTrue(result["inspect"])


if __name__ == "__main__":
    unittest.main()
