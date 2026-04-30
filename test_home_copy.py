import unittest
from datetime import datetime

from backend_lib.home_copy import _build_dashboard_home_copy


class HomeCopyTests(unittest.TestCase):
    def test_failed_task_headline_uses_fallback_priority(self) -> None:
        result = _build_dashboard_home_copy(
            config={},
            mode_key="browser",
            monitoring_running=False,
            failed_task_count=2,
            now=datetime(2026, 1, 1, 9, 0, 0),
        )

        self.assertEqual(result["greeting"], "早上好")
        self.assertEqual(result["headline"], "今天有 2 个任务需要补跑或补发，建议优先处理。")

    def test_running_headline_uses_record_count(self) -> None:
        result = _build_dashboard_home_copy(
            config={},
            mode_key="browser",
            monitoring_running=True,
            enabled_task_count=3,
            today_record_count=5,
            hit_record_count=8,
            now=datetime(2026, 1, 1, 15, 0, 0),
        )

        self.assertEqual(result["greeting"], "下午好")
        self.assertEqual(result["headline"], "今天已启用 3 个品牌任务，当前已累计 8 条运行记录。")

    def test_home_copy_prefers_business_message_from_ui_module(self) -> None:
        result = _build_dashboard_home_copy(
            config={
                "tasks": [
                    {
                        "name": "品牌A",
                        "service_start_date": "2026-01-01",
                        "service_cycle_days": 2,
                    }
                ]
            },
            mode_key="api",
            monitoring_running=False,
            enabled_task_count=1,
            now=datetime(2026, 1, 2, 9, 0, 0),
        )

        self.assertIn("品牌A", result["headline"])
        self.assertNotIn("保险模式走平台 API", result["headline"])

    def test_greeting_boundaries(self) -> None:
        cases = [
            (datetime(2026, 1, 1, 5, 59), "早点休息"),
            (datetime(2026, 1, 1, 11, 0), "中午好"),
            (datetime(2026, 1, 1, 18, 0), "晚上好"),
        ]
        for current_time, expected in cases:
            with self.subTest(expected=expected):
                result = _build_dashboard_home_copy(
                    config={},
                    mode_key="browser",
                    monitoring_running=False,
                    now=current_time,
                )
                self.assertEqual(result["greeting"], expected)

    def test_ignores_expired_service_message(self) -> None:
        result = _build_dashboard_home_copy(
            config={
                "tasks": [
                    {
                        "name": "品牌A",
                        "enabled": False,
                        "service_start_date": "2026-01-01",
                        "service_cycle_days": 1,
                    }
                ]
            },
            mode_key="browser",
            monitoring_running=False,
            total_task_count=1,
            now=datetime(2026, 1, 1, 9, 0, 0),
        )

        self.assertEqual(result["headline"], "当前已录入 1 个品牌任务，开启监控后会按计划继续运行。")


if __name__ == "__main__":
    unittest.main()
