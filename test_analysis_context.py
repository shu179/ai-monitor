import unittest
from datetime import datetime

from backend_lib.analysis_context import (
    _build_monitoring_analysis_payload,
    _format_assistant_analysis_context,
    _rank_failure_buckets,
    _should_attach_assistant_analysis,
)


class AnalysisContextTests(unittest.TestCase):
    def test_rank_failure_buckets_prefers_qualified_items(self) -> None:
        ranked = _rank_failure_buckets(
            {
                "low_sample": {"name": "low_sample", "attempts": 1, "failed": 1},
                "high_rate": {"name": "high_rate", "attempts": 4, "failed": 3},
                "low_rate": {"name": "low_rate", "attempts": 6, "failed": 1},
            },
            limit=2,
            min_attempts=2,
        )

        self.assertEqual([item["name"] for item in ranked], ["high_rate", "low_rate"])
        self.assertEqual(ranked[0]["failureRate"], 75.0)
        self.assertEqual(ranked[0]["success"], 1)

    def test_should_attach_assistant_analysis_respects_mode_markers(self) -> None:
        self.assertTrue(
            _should_attach_assistant_analysis([
                {"role": "system", "content": "[SURFACED_CHAT_MODE:analysis]"},
                {"role": "user", "content": "普通聊天"},
            ])
        )
        self.assertFalse(
            _should_attach_assistant_analysis([
                {"role": "system", "content": "[SURFACED_CHAT_MODE:plain]"},
                {"role": "user", "content": "帮我查看数据"},
            ])
        )
        self.assertTrue(
            _should_attach_assistant_analysis([
                {"role": "user", "content": "帮我分析一下系统失败率"},
            ])
        )

    def test_build_monitoring_analysis_payload_aggregates_records_and_articles(self) -> None:
        config = {
            "tasks": [
                {
                    "name": "任务一",
                    "task_id": "task-1",
                    "brand": "品牌A",
                    "industry_tags": ["行业A", "行业A", "行业B"],
                }
            ]
        }
        records_by_task = {
            "任务一": [
                {
                    "ts": "2026-01-10 08:00:00",
                    "brand": "品牌A",
                    "keyword": "关键词1",
                    "platform": "doubao",
                    "ok": False,
                },
                {
                    "ts": "2026-01-10 09:00:00",
                    "brand": "品牌A",
                    "keyword": "关键词1",
                    "platform": "doubao",
                    "ok": True,
                },
                {
                    "ts": "2026-01-10 10:00:00",
                    "brand": "品牌A",
                    "keyword": "关键词1",
                    "platform": "doubao",
                    "execution_source": "manual_test",
                    "ok": False,
                },
            ]
        }
        articles = [
            {"ts": "2026-01-10 11:00:00", "media_type": "authority", "matched_tasks": ["任务一"]},
            {"ts": "2026-01-09 11:00:00", "media_type": "self", "matched_tasks": []},
            {"ts": "2025-12-01 11:00:00", "media_type": "authority", "matched_tasks": ["任务一"]},
        ]

        payload = _build_monitoring_analysis_payload(
            config,
            articles=articles,
            now=datetime(2026, 1, 10, 12, 0, 0),
            records_loader=lambda task_name, **_: records_by_task.get(task_name, []),
            task_names_loader=lambda: [],
            success_checker=lambda record: bool(record.get("ok")),
            platform_display_name=lambda platform: {"doubao": "豆包"}.get(platform, platform),
        )

        overall = payload["monitoring"]["overall"]
        self.assertEqual(overall["attempts"], 2)
        self.assertEqual(overall["failed"], 1)
        self.assertEqual(overall["failureRate"], 50.0)
        self.assertEqual(payload["monitoring"]["topPlatforms"][0]["name"], "豆包")

        article_overall = payload["articles"]["overall"]
        self.assertEqual(article_overall["total"], 2)
        self.assertEqual(article_overall["authority"], 1)
        self.assertEqual(article_overall["selfmedia"], 1)
        self.assertEqual(article_overall["unmatched"], 1)

        brand_trend = payload["articles"]["brandTrends"][0]
        self.assertEqual(brand_trend["brand"], "品牌A")
        self.assertEqual(brand_trend["industry"], "行业A / 行业B")
        self.assertEqual(brand_trend["last7Days"], 1)

        context_text = _format_assistant_analysis_context(payload)
        self.assertIn("监测总览", context_text)
        self.assertIn("豆包", context_text)
        self.assertIn("品牌A", context_text)


if __name__ == "__main__":
    unittest.main()
