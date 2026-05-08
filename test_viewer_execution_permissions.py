from __future__ import annotations

import unittest
from unittest.mock import Mock

from web_backend import AppRuntime


class ViewerExecutionPermissionTests(unittest.TestCase):
    def _viewer_runtime(self) -> AppRuntime:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime._current_cloud_role = Mock(return_value="viewer")  # type: ignore[method-assign]
        return runtime

    def test_viewer_account_blocks_local_execution_entrypoints(self) -> None:
        runtime = self._viewer_runtime()
        cases = [
            ("start_monitoring", lambda: runtime.start_monitoring()),
            ("set_monitoring_enabled", lambda: runtime.set_monitoring_enabled({"enabled": True})),
            ("execute_scheduled_task", lambda: runtime._execute_scheduled_task({})),
            ("trigger_run_all", lambda: runtime.trigger_run_all()),
            ("trigger_run_selected", lambda: runtime.trigger_run_selected({"task_ids": ["task-1"]})),
            ("test_run_task", lambda: runtime.test_run_task("task-1")),
            ("start_test_run_task", lambda: runtime.start_test_run_task("task-1")),
            ("start_batch_test", lambda: runtime.start_batch_test({"brand": "品牌", "keywords": ["关键词"]})),
            ("run_search_brand_rank", lambda: runtime.run_search_brand_rank({"file_id": "file-1", "brand": "品牌"})),
            ("run_account_article_crawl", lambda: runtime.run_account_article_crawl({})),
            ("force_send_successful_task_results", lambda: runtime.force_send_successful_task_results("task-1")),
            ("recognition_action", lambda: runtime.recognition_action({"action": "screenshot"})),
        ]

        for name, call in cases:
            with self.subTest(name=name):
                result = call()
                self.assertFalse(result.get("ok", result.get("queued", True)))
                self.assertIn("浏览账号仅可查看", str(result.get("message") or ""))

    def test_operator_account_does_not_hit_viewer_block(self) -> None:
        runtime = AppRuntime.__new__(AppRuntime)
        runtime._current_cloud_role = Mock(return_value="operator")  # type: ignore[method-assign]

        self.assertIsNone(runtime._viewer_execution_block_response())


if __name__ == "__main__":
    unittest.main()
