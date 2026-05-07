import unittest
from copy import deepcopy
from datetime import date
from unittest.mock import patch

from backend_lib.task_overview_service import TaskOverviewService


class FakeConfigProvider:
    def __init__(self, config: dict) -> None:
        self.config = deepcopy(config)
        self.load_count = 0

    def load(self) -> dict:
        self.load_count += 1
        return deepcopy(self.config)


class TaskOverviewServiceTests(unittest.TestCase):
    def _make_service(
        self,
        config_provider: FakeConfigProvider,
        *,
        cache_ttl_seconds: float = 3.0,
        test_failure_notice_getter=None,
    ) -> TaskOverviewService:
        return TaskOverviewService(
            config_provider=config_provider,
            synced_articles_loader=lambda _config: [
                {"matched_tasks": ["任务一"]},
                {"matched_tasks": ["任务一", "其他任务"]},
            ],
            prune_test_failure_notices=lambda: None,
            test_failure_notice_getter=test_failure_notice_getter or (lambda task_id: {
                "message": f"{task_id} 测试失败",
                "updatedAt": "2026-01-05 10:00:00",
                "expiresAt": "2026-01-05 10:05:00",
            }),
            platform_display_name=lambda platform: {"doubao": "豆包", "kimi": "Kimi"}.get(platform, platform),
            secret_masker=lambda value: f"masked:{value}" if value else "",
            normalize_string_list=lambda values: list(dict.fromkeys(str(item).strip() for item in values if str(item).strip())),
            cache_ttl_seconds=cache_ttl_seconds,
        )

    def test_get_tasks_full_builds_overview_payload(self) -> None:
        config_provider = FakeConfigProvider(
            {
                "detection_mode": "browser",
                "scheduler": {"weekly_times": {"0": "09:30"}},
                "tasks": [
                    {
                        "task_id": "task-1",
                        "name": "任务一",
                        "brand": "品牌A",
                        "enabled": True,
                        "webhook_url": "secret-webhook",
                        "weekdays": [0],
                        "keywords": [
                            {
                                "keyword": "关键词1",
                                "brand": "品牌A",
                                "platforms": ["doubao", "kimi"],
                                "mode": "browser",
                                "deep_think": {"doubao": True},
                            }
                        ],
                        "industry_tags": ["行业A"],
                        "region_tags": ["华东"],
                        "recognition_batch_size": 2,
                        "fixed_screenshot_enabled": True,
                        "fixed_screenshot_count": 1,
                    }
                ],
            }
        )
        service = self._make_service(config_provider)

        with patch("backend_lib.task_overview_service.local_today", return_value=date(2026, 1, 5)):
            with patch(
                "backend_lib.task_overview_service.get_task_day_status",
                return_value={
                    "status": "success",
                    "brand_status": "success",
                    "sent_today": True,
                    "sent_at": "2026-01-05 09:45:00",
                    "formal_started": True,
                    "formal_running": False,
                    "has_gap": False,
                    "gap_reasons": [],
                    "source": "formal",
                    "test_status": "pending",
                    "test_source": "",
                    "extra": {
                        "completed_keywords": ["关键词1"],
                        "detected_platforms": ["doubao"],
                        "actual_screenshot_count": 2,
                    },
                    "keyword_states": {
                        "关键词1": {
                            "brand": "品牌A",
                            "run_success": True,
                            "screenshot_saved": True,
                            "image_path": "/tmp/one.png",
                        }
                    },
                },
            ):
                with patch(
                    "backend_lib.task_overview_service.get_records",
                    return_value=[
                        {"success": True, "rank": 1},
                        {"success": False, "rank": 99},
                        {"success": False, "execution_source": "manual_test"},
                    ],
                ):
                    with patch(
                        "backend_lib.task_overview_service.get_brand_trend_series",
                        return_value={"actual": [50, 75.24, None, 88.8]},
                    ):
                        payload = service.get_tasks_full()

        self.assertEqual(config_provider.load_count, 1)
        self.assertEqual(len(payload["tasks"]), 1)
        task = payload["tasks"][0]
        self.assertEqual(task["id"], "task-1")
        self.assertEqual(task["platforms"], ["Kimi", "豆包"])
        self.assertEqual(task["keywords"][0]["platforms"], ["豆包", "Kimi"])
        self.assertEqual(task["keywords"][0]["deep_think"], {"豆包": True})
        self.assertEqual(task["webhook_url"], "masked:secret-webhook")
        self.assertEqual(task["total_records"], 2)
        self.assertEqual(task["success_records"], 1)
        self.assertEqual(task["success_rate"], 50.0)
        self.assertEqual(task["article_count"], 2)
        self.assertEqual(task["completed_keywords_today"], ["关键词1"])
        self.assertEqual(task["detected_platforms_today"], ["doubao"])
        self.assertEqual(task["actual_screenshot_count_today"], 2)
        self.assertEqual(task["fixed_screenshot_target_today"], 2)
        self.assertTrue(task["completed_by_quota_today"])
        self.assertEqual(task["optimization_trend"], [{"value": 50.0}, {"value": 75.2}, {"value": 88.8}])
        self.assertEqual(task["test_failure_notice"]["message"], "task-1 测试失败")

    def test_get_tasks_full_uses_deepcopy_cache_until_invalidated(self) -> None:
        config_provider = FakeConfigProvider({"tasks": [{"task_id": "task-1", "name": "任务一"}]})
        service = self._make_service(config_provider)

        with patch(
            "backend_lib.task_overview_service.get_task_day_status",
            return_value={"status": "running", "brand_status": "running"},
        ):
            with patch("backend_lib.task_overview_service.get_records", return_value=[]):
                with patch("backend_lib.task_overview_service.get_brand_trend_series", return_value={}):
                    first = service.get_tasks_full()
                    first["tasks"].append({"id": "mutated"})
                    second = service.get_tasks_full()
                    service.invalidate_cache()
                    third = service.get_tasks_full()

        self.assertEqual(config_provider.load_count, 2)
        self.assertEqual(len(second["tasks"]), 1)
        self.assertEqual(len(third["tasks"]), 1)

    def test_get_tasks_full_hides_revoked_cloud_tasks_but_keeps_disabled_visible_tasks(self) -> None:
        config_provider = FakeConfigProvider(
            {
                "tasks": [
                    {
                        "task_id": "cloud_5",
                        "name": "已转派任务",
                        "brand": "旧品牌",
                        "enabled": False,
                        "cloud_task_id": 5,
                        "cloud_access_level": "revoked",
                    },
                    {
                        "task_id": "cloud_6",
                        "name": "浏览任务",
                        "brand": "浏览品牌",
                        "enabled": False,
                        "cloud_task_id": 6,
                        "cloud_access_level": "view",
                    },
                    {
                        "task_id": "local-disabled",
                        "name": "本地停用任务",
                        "brand": "本地品牌",
                        "enabled": False,
                    },
                ]
            }
        )
        service = self._make_service(config_provider)

        with patch(
            "backend_lib.task_overview_service.get_task_day_status",
            return_value={"status": "pending", "brand_status": "pending"},
        ):
            with patch("backend_lib.task_overview_service.get_records", return_value=[]):
                with patch("backend_lib.task_overview_service.get_brand_trend_series", return_value={}):
                    payload = service.get_tasks_full()

        task_ids = [task["id"] for task in payload["tasks"]]
        self.assertEqual(task_ids, ["cloud_6", "local-disabled"])

    def test_get_tasks_full_counts_nested_platform_success_screenshots(self) -> None:
        config_provider = FakeConfigProvider(
            {
                "tasks": [
                    {
                        "task_id": "task-1",
                        "name": "任务一",
                        "brand": "品牌A",
                        "keywords": [{"keyword": "关键词1", "platforms": ["doubao", "kimi"]}],
                    }
                ]
            }
        )
        service = self._make_service(config_provider)

        with patch(
            "backend_lib.task_overview_service.get_task_day_status",
            return_value={
                "status": "query_failed",
                "brand_status": "gap",
                "formal_started": True,
                "formal_running": False,
                "has_gap": True,
                "extra": {},
                "keyword_states": {
                    "关键词1": {
                        "platform_states": {
                            "doubao": {
                                "run_success": True,
                                "screenshot_saved": True,
                                "image_path": "/tmp/doubao.png",
                            },
                            "kimi": {
                                "run_success": False,
                                "screenshot_saved": False,
                                "image_path": "",
                            },
                        }
                    }
                },
            },
        ):
            with patch("backend_lib.task_overview_service.get_records", return_value=[]):
                with patch("backend_lib.task_overview_service.get_brand_trend_series", return_value={}):
                    payload = service.get_tasks_full()

        self.assertEqual(payload["tasks"][0]["actual_screenshot_count_today"], 1)

    def test_get_tasks_full_merges_history_success_screenshots(self) -> None:
        config_provider = FakeConfigProvider(
            {
                "tasks": [
                    {
                        "task_id": "task-1",
                        "name": "任务一",
                        "brand": "品牌A",
                        "keywords": [{"keyword": "关键词1", "platforms": ["doubao", "kimi"]}],
                    }
                ]
            }
        )
        service = self._make_service(config_provider)
        history_results = {
            ("关键词1", "品牌A", "doubao"): {
                "keyword": "关键词1",
                "brand": "品牌A",
                "platform": "doubao",
                "screenshot": "/tmp/doubao.png",
            },
            ("关键词1", "品牌A", "kimi"): {
                "keyword": "关键词1",
                "brand": "品牌A",
                "platform": "kimi",
                "screenshot": "/tmp/kimi.png",
            },
        }

        with patch(
            "backend_lib.task_overview_service.get_task_day_status",
            return_value={
                "status": "query_failed",
                "brand_status": "gap",
                "extra": {"actual_screenshot_count": 1},
                "keyword_states": {},
            },
        ):
            with patch("backend_lib.dashboard_tasks.load_today_success_only_query_results", return_value=history_results):
                with patch("backend_lib.dashboard_tasks.screenshot_path_exists", side_effect=lambda path: bool(path)):
                    with patch("backend_lib.task_overview_service.get_records", return_value=[]):
                        with patch("backend_lib.task_overview_service.get_brand_trend_series", return_value={}):
                            payload = service.get_tasks_full()

        task = payload["tasks"][0]
        self.assertEqual(task["actual_screenshot_count_today"], 2)
        self.assertEqual(task["completed_keywords_today"], ["关键词1"])
        self.assertEqual(task["detected_platforms_today"], ["doubao", "kimi"])

    def test_get_tasks_full_restores_persisted_test_failure_notice(self) -> None:
        config_provider = FakeConfigProvider(
            {
                "tasks": [
                    {
                        "task_id": "task-1",
                        "name": "任务一",
                        "brand": "品牌A",
                    }
                ]
            }
        )
        service = self._make_service(config_provider, test_failure_notice_getter=lambda _task_id: None)

        with patch(
            "backend_lib.task_overview_service.get_task_day_status",
            return_value={
                "status": "pending",
                "brand_status": "gap",
                "test_status": "query_failed",
                "test_message": "任务组未补齐（5/6），未发送企业微信",
                "test_updated_at": "2026-01-05T10:00:00+08:00",
                "keyword_states": {},
                "extra": {},
            },
        ):
            with patch("backend_lib.task_overview_service.get_records", return_value=[]):
                with patch("backend_lib.task_overview_service.get_brand_trend_series", return_value={}):
                    payload = service.get_tasks_full()

        notice = payload["tasks"][0]["test_failure_notice"]
        self.assertEqual(notice["message"], "任务组未补齐（5/6），未发送企业微信")
        self.assertEqual(notice["runId"], "")

    def test_get_tasks_full_does_not_restore_test_failure_notice_after_success(self) -> None:
        config_provider = FakeConfigProvider(
            {
                "tasks": [
                    {
                        "task_id": "task-1",
                        "name": "任务一",
                        "brand": "品牌A",
                    }
                ]
            }
        )
        service = self._make_service(config_provider, test_failure_notice_getter=lambda _task_id: None)

        with patch(
            "backend_lib.task_overview_service.get_task_day_status",
            return_value={
                "status": "success",
                "brand_status": "sent",
                "test_status": "query_failed",
                "test_message": "旧的测试失败",
                "test_updated_at": "2026-01-05T10:00:00+08:00",
                "keyword_states": {},
                "extra": {},
            },
        ):
            with patch("backend_lib.task_overview_service.get_records", return_value=[]):
                with patch("backend_lib.task_overview_service.get_brand_trend_series", return_value={}):
                    payload = service.get_tasks_full()

        self.assertIsNone(payload["tasks"][0]["test_failure_notice"])


if __name__ == "__main__":
    unittest.main()
