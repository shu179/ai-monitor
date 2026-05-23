import unittest
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
import tempfile

import backend_lib.dashboard_trends as dashboard_trends
from backend_lib.dashboard import (
    _build_dashboard_failed_tasks,
    _build_dashboard_media_stats,
    _build_dashboard_media_stats_monthly,
    _build_dashboard_month_overview,
    _build_dashboard_today_task_summary,
    _build_media_stats,
    _build_task_failure_summary,
    _build_trend_payload,
    _compute_fixed_screenshot_target,
    _collect_today_successful_task_payload,
    _format_trend_points,
    _range_to_days,
    _task_is_scheduled_for_day,
    _weekday_name_from_iso,
)


class DashboardHelperTests(unittest.TestCase):
    def test_build_media_stats_splits_auth_and_self_counts(self) -> None:
        stats = _build_media_stats(Counter({"doubao": 10, "unknown": 3}))

        self.assertEqual(
            stats[:2],
            [
                {"label": "豆包", "auth": 7, "self": 3},
                {"label": "unknown", "auth": 2, "self": 1},
            ],
        )

    def test_build_media_stats_empty_defaults(self) -> None:
        stats = _build_media_stats(Counter())

        self.assertEqual(stats[0], {"label": "豆包", "auth": 0, "self": 0})
        self.assertEqual(len(stats), 6)

    def test_task_is_scheduled_for_day_checks_dates_weekdays_and_times(self) -> None:
        monday = date(2026, 1, 5)
        scheduler = {"weekly_times": {"0": "09:30"}}

        self.assertTrue(
            _task_is_scheduled_for_day({"enabled": True, "weekdays": [0]}, scheduler, monday)
        )
        self.assertFalse(
            _task_is_scheduled_for_day({"enabled": False, "weekdays": [0]}, scheduler, monday)
        )
        self.assertFalse(
            _task_is_scheduled_for_day(
                {"enabled": True, "weekdays": [0], "optimization_start_date": "2026-01-06"},
                scheduler,
                monday,
            )
        )
        self.assertFalse(
            _task_is_scheduled_for_day({"enabled": True, "weekdays": [1]}, scheduler, monday)
        )

    def test_dashboard_today_summary_counts_scheduled_and_started_tasks(self) -> None:
        monday = date(2026, 1, 5)
        scheduler = {"weekly_times": {"0": "09:30"}}
        tasks = [
            {"task_id": "scheduled", "name": "已排程", "weekdays": [0]},
            {"task_id": "done", "name": "已完成", "weekdays": [0]},
            {"task_id": "started", "name": "已开始", "weekdays": [2]},
            {"task_id": "excluded", "name": "未排程", "weekdays": [2]},
        ]
        statuses = {
            "scheduled": {"brand_status": "pending", "formal_started": False},
            "done": {"brand_status": "success", "formal_started": True},
            "started": {"brand_status": "gap", "formal_started": True},
            "excluded": {"brand_status": "pending", "formal_started": False},
        }

        summary = _build_dashboard_today_task_summary(
            tasks,
            scheduler,
            monday,
            day_status_loader=lambda task, _target_date=None: statuses[task["task_id"]],
        )

        self.assertEqual(summary["todayTaskCount"], 3)
        self.assertEqual(summary["completedCount"], 1)
        self.assertEqual(summary["runningCount"], 1)
        self.assertEqual([task["task_id"] for task in summary["tasks"]], ["scheduled", "done", "started"])

    def test_dashboard_failed_tasks_builds_query_details(self) -> None:
        task = {"task_id": "task-1", "name": "任务一", "brand": "品牌A"}
        status = {
            "status": "query_failed",
            "brand_status": "gap",
            "formal_started": True,
            "formal_running": False,
            "has_gap": True,
            "updated_at": "2026-01-05 10:00:00",
            "message": "",
            "extra": {
                "gap_details": [
                    {
                        "keyword": "关键词1",
                        "platform": "doubao",
                        "reason": "run_failed",
                        "updated_at": "2026-01-05 09:59:00",
                    }
                ],
            },
            "keyword_states": {
                "关键词2": {
                    "run_success": True,
                    "screenshot_saved": True,
                    "image_path": "/tmp/success.png",
                }
            },
        }

        failed = _build_dashboard_failed_tasks(
            [task],
            day_status_loader=lambda _task, _target_date=None: status,
            scheduler_state_loader=lambda unit_id: {
                "last_auto_run_date": "2026-01-05",
                "last_round_status": "failed" if unit_id.endswith("::browser") else "success",
                "last_auto_fail_at": "2026-01-05 10:01:00",
                "last_failure_kind": "query",
            },
            platform_display_name=lambda platform: {"doubao": "豆包"}.get(platform, platform),
            today_text="2026-01-05",
        )

        self.assertEqual(len(failed), 1)
        item = failed[0]
        self.assertEqual(item["failedModes"], ["抓取模式"])
        self.assertEqual(item["issueType"], "query")
        self.assertEqual(item["failedQueries"][0]["platform"], "豆包")
        self.assertEqual(item["failedQueries"][0]["error_message"], "关键词执行失败")
        self.assertEqual(item["sendableSuccessCount"], 1)
        self.assertTrue(item["canForceSendSuccess"])

    def test_task_failure_summary_returns_empty_shape_when_no_failure(self) -> None:
        task = {"task_id": "task-1", "name": "任务一", "brand": "品牌A"}
        summary = _build_task_failure_summary(
            task,
            day_status_loader=lambda _task, _target_date=None: {
                "status": "running",
                "brand_status": "running",
                "message": "",
                "updated_at": "2026-01-05 10:00:00",
            },
            scheduler_state_loader=lambda _unit_id: {},
            today_text="2026-01-05",
        )

        self.assertFalse(summary["failedToday"])
        self.assertEqual(summary["failedQueries"], [])

    def test_collect_today_successful_task_payload_reads_progress_fields(self) -> None:
        task = {"task_id": "task-1", "name": "任务一", "brand": "品牌A"}
        status = {
            "status": "success",
            "source": "formal",
            "brand_status": "success",
            "extra": {
                "completed_keywords": ["关键词1", "关键词2"],
                "detected_platforms": ["doubao", "kimi"],
                "actual_screenshot_count": 4,
            },
            "keyword_states": {
                "关键词1": {
                    "brand": "品牌A",
                    "run_success": True,
                    "screenshot_saved": True,
                    "image_path": "/tmp/one.png",
                },
                "关键词2": {
                    "brand": "品牌A",
                    "run_success": True,
                    "screenshot_saved": False,
                    "image_path": "/tmp/two.png",
                },
                "关键词3": {
                    "brand": "品牌B",
                    "run_success": True,
                    "screenshot_saved": True,
                    "image_path": "",
                },
            },
        }

        payload = _collect_today_successful_task_payload(
            task,
            day_status_loader=lambda _task, _target_date=None: status,
        )

        self.assertEqual(payload["taskId"], "task-1")
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["statusSource"], "formal")
        self.assertEqual(payload["brands"], ["品牌A", "品牌B"])
        self.assertEqual(payload["completedKeywords"], ["关键词1", "关键词2"])
        self.assertEqual(payload["detectedPlatforms"], ["doubao", "kimi"])
        self.assertEqual(payload["screenshotPaths"], ["/tmp/one.png"])
        self.assertEqual(payload["actualScreenshotCount"], 4)

    def test_collect_today_successful_task_payload_falls_back_to_required_keywords(self) -> None:
        task = {"task_id": "task-1", "name": "任务一", "brand": "品牌A"}
        base_status = {
            "status": "success",
            "brand_status": "success",
            "required_keywords": ["关键词1", "关键词2"],
            "extra": {},
            "keyword_states": {},
        }

        payload = _collect_today_successful_task_payload(
            task,
            day_status_loader=lambda _task, _target_date=None: base_status,
        )
        self.assertEqual(payload["completedKeywords"], ["关键词1", "关键词2"])
        self.assertEqual(payload["brands"], ["品牌A"])

        quota_payload = _collect_today_successful_task_payload(
            task,
            day_status_loader=lambda _task, _target_date=None: {
                **base_status,
                "extra": {"completed_by_quota": True},
            },
        )
        self.assertEqual(quota_payload["completedKeywords"], [])

    def test_collect_today_successful_task_payload_reads_screenshot_paths_from_failure_progress(self) -> None:
        task = {"task_id": "task-1", "name": "任务一", "brand": "品牌A"}
        with tempfile.TemporaryDirectory() as tmpdir:
            screenshot_path = Path(tmpdir) / "retained.png"
            screenshot_path.write_bytes(b"fake")
            status = {
                "status": "send_failed",
                "brand_status": "send_failed",
                "extra": {
                    "completed_keywords": ["关键词1"],
                    "detected_platforms": ["doubao"],
                    "screenshot_paths": [str(screenshot_path)],
                    "actual_screenshot_count": 1,
                },
                "official_extra": {
                    "completed_keywords": ["关键词1"],
                    "detected_platforms": ["doubao"],
                    "screenshot_paths": [str(screenshot_path)],
                    "actual_screenshot_count": 1,
                },
                "keyword_states": {},
            }

            payload = _collect_today_successful_task_payload(
                task,
                day_status_loader=lambda _task, _target_date=None: status,
            )

        self.assertEqual(payload["completedKeywords"], ["关键词1"])
        self.assertEqual(payload["detectedPlatforms"], ["doubao"])
        self.assertEqual(payload["screenshotPaths"], [str(screenshot_path)])
        self.assertEqual(payload["actualScreenshotCount"], 1)

    def test_compute_fixed_screenshot_target_respects_flag_count_and_platform_floor(self) -> None:
        self.assertEqual(
            _compute_fixed_screenshot_target(
                {
                    "fixed_screenshot_enabled": False,
                    "fixed_screenshot_count": 5,
                    "keywords": [{"platforms": ["doubao", "kimi"]}],
                }
            ),
            0,
        )
        self.assertEqual(
            _compute_fixed_screenshot_target(
                {
                    "fixed_screenshot_enabled": True,
                    "fixed_screenshot_count": 1,
                    "keywords": [{"platforms": ["doubao", "kimi", "doubao", ""]}],
                }
            ),
            2,
        )
        self.assertEqual(
            _compute_fixed_screenshot_target(
                {
                    "fixed_screenshot_enabled": True,
                    "fixed_screenshot_count": "bad",
                    "recognition_batch_size": 4,
                    "keywords": [{"platforms": ["doubao"]}],
                }
            ),
            1,
        )
        self.assertEqual(
            _compute_fixed_screenshot_target(
                {
                    "fixed_screenshot_enabled": True,
                    "recognition_batch_size": 3,
                    "keywords": [{"platforms": ["doubao"]}],
                }
            ),
            3,
        )

    def test_range_and_weekday_labels(self) -> None:
        self.assertEqual(_range_to_days("week"), 7)
        self.assertEqual(_range_to_days("month"), 30)
        self.assertEqual(_range_to_days("year"), 365)
        self.assertEqual(_weekday_name_from_iso("2026-01-05"), "周一")
        self.assertEqual(_weekday_name_from_iso("not-a-date"), "not-a-date")

    def test_year_trend_points_are_aggregated_by_month(self) -> None:
        points = _format_trend_points(
            {
                "dates": [date(2026, 1, 1), date(2026, 1, 2), date(2026, 2, 1)],
                "actual": [10, 30, 50],
                "predicted": [20, 40, 60],
            },
            "year",
        )

        self.assertEqual(
            points,
            [
                {"date": "2026-01", "name": "1月", "value": 20.0, "predict": 30.0},
                {"date": "2026-02", "name": "2月", "value": 50.0, "predict": 60.0},
            ],
        )

    def test_trend_summary_prefers_recorded_dates(self) -> None:
        payload = _build_trend_payload(
            {
                "dates": [date(2026, 1, 1), date(2026, 1, 2)],
                "actual": [91, 30],
                "predicted": [91, 30],
                "recorded_dates": ["2026-01-01"],
            },
            "week",
        )

        self.assertEqual(payload["current"], 91)
        self.assertEqual(payload["avg"], 91)
        self.assertEqual(payload["peak"], 91)
        self.assertEqual(len(payload["data"]), 2)

    def test_dashboard_trend_batches_history_reads(self) -> None:
        original_get_records_many = dashboard_trends.get_records_many
        original_series_from_records = dashboard_trends.get_brand_trend_series_from_records
        read_calls = []
        series_calls = []

        def fake_get_records_many(specs):
            read_calls.append(list(specs))
            return [
                [{"id": "a", "brand": "品牌A"}],
                [{"id": "b", "brand": "品牌B"}],
            ]

        def fake_series_from_records(
            task_name,
            brands,
            days,
            records,
            *,
            task_id="",
            task_created_at="",
        ):
            series_calls.append((task_name, brands, days, records, task_id, task_created_at))
            return {
                "dates": [date(2026, 1, 1)],
                "actual": [90 if task_id == "task-a" else 70],
                "predicted": [90 if task_id == "task-a" else 70],
                "recorded_dates": ["2026-01-01"],
            }

        dashboard_trends.get_records_many = fake_get_records_many
        dashboard_trends.get_brand_trend_series_from_records = fake_series_from_records
        self.addCleanup(setattr, dashboard_trends, "get_records_many", original_get_records_many)
        self.addCleanup(
            setattr,
            dashboard_trends,
            "get_brand_trend_series_from_records",
            original_series_from_records,
        )

        payload = dashboard_trends._build_dashboard_trend(
            [
                {
                    "task_id": "task-a",
                    "name": "任务A",
                    "keywords": [{"brand": "品牌A"}],
                    "created_at": "2025-12-01",
                },
                {
                    "task_id": "task-b",
                    "name": "任务B",
                    "keywords": [{"brand": "品牌B"}],
                },
                {"task_id": "disabled", "name": "禁用", "enabled": False},
            ],
            "week",
        )

        self.assertEqual(read_calls, [[("任务A", "task-a"), ("任务B", "task-b")]])
        self.assertEqual([call[0] for call in series_calls], ["任务A", "任务B"])
        self.assertEqual([call[3] for call in series_calls], [
            [{"id": "a", "brand": "品牌A"}],
            [{"id": "b", "brand": "品牌B"}],
        ])
        self.assertEqual(payload["current"], 80)

    def test_media_stats_count_authority_and_self_articles(self) -> None:
        today = date.today()
        yesterday = today - timedelta(days=1)

        stats = _build_dashboard_media_stats(
            [
                {"ts": yesterday.isoformat(), "media_type": "authority"},
                {"ts": today.isoformat(), "media_type": "self"},
                {"ts": today.isoformat(), "media_type": ""},
                {"ts": (today - timedelta(days=3)).isoformat(), "media_type": "authority"},
            ],
            days=2,
        )

        self.assertEqual(stats[0]["auth"], 1)
        self.assertEqual(stats[0]["self"], 0)
        self.assertEqual(stats[1]["auth"], 0)
        self.assertEqual(stats[1]["self"], 2)

    def test_monthly_media_stats_uses_recent_30_day_window(self) -> None:
        today = date.today()
        stats = _build_dashboard_media_stats_monthly([
            {"ts": (today - timedelta(days=30)).isoformat(), "media_type": "self"},
            {"ts": (today - timedelta(days=29)).isoformat(), "media_type": "authority"},
            {"ts": today.isoformat(), "media_type": "authority"},
            {"ts": today.isoformat(), "media_type": "self"},
        ])

        self.assertEqual(len(stats), 30)
        self.assertEqual(stats[0]["date"], (today - timedelta(days=29)).isoformat())
        self.assertEqual(stats[0]["auth"], 1)
        self.assertEqual(stats[0]["self"], 0)
        self.assertEqual(stats[-1]["self"], 1)

    def test_dashboard_month_overview_summarizes_articles_tasks_references_and_platforms(self) -> None:
        today = date(2026, 5, 15)
        scheduler = {"weekly_times": {"0": "09:30", "4": "09:30"}}
        tasks = [
            {
                "task_id": "task-a",
                "name": "品牌A",
                "brand": "品牌A",
                "enabled": True,
                "weekdays": [0, 4],
                "optimization_start_date": "2026-05-01",
                "optimization_end_date": "2026-05-31",
            },
            {
                "task_id": "task-b",
                "name": "品牌B",
                "brand": "品牌B",
                "enabled": False,
                "weekdays": [0, 4],
            },
        ]
        articles = [
            {
                "id": "article-a",
                "ts": "2026-05-05 10:00",
                "media_type": "authority",
                "platform": "doubao",
                "price": 1200,
                "reference_hits": {
                    "品牌A": {
                        "count": 2,
                        "events": [
                            {"referenced_at": "2026-05-05 11:00", "platform": "kimi"},
                            {"referenced_at": "2026-05-05 12:00", "platform": "kimi"},
                        ],
                    }
                },
            },
            {"id": "article-b", "published_at": "2026-05-15", "media_type": "selfmedia", "platform": "wechat", "spend": 300},
            {"id": "article-old", "ts": "2026-04-30", "price": 900},
        ]
        history_batches = [[
            {"ts": "2026-05-05 09:00", "platform": "doubao", "success": True, "rank": 1, "review_status": ""},
            {"ts": "2026-05-05 10:00", "platform": "deepseek", "success": False, "rank": 99, "error_message": ""},
            {"ts": "2026-05-15 09:00", "platform": "kimi", "success": True, "rank": 2, "review_status": "pending"},
        ]]

        overview = _build_dashboard_month_overview(
            tasks,
            scheduler,
            articles,
            today=today,
            history_records_loader=lambda specs: history_batches,
            day_status_loader=lambda task, target_date=None: {
                "brand_status": "success" if target_date == date(2026, 5, 5) else "pending",
                "formal_started": target_date == date(2026, 5, 15),
            },
        )

        self.assertEqual(overview["month"], "2026-05")
        self.assertEqual(overview["totals"]["articlePublishedTotal"], 2)
        self.assertEqual(overview["totals"]["totalSpend"], 1500)
        self.assertEqual(overview["totals"]["brandTaskCount"], 1)
        self.assertEqual(overview["totals"]["brandDisplayTotal"], 2)
        self.assertEqual(overview["totals"]["brandPendingOptimizationCount"], 1)
        self.assertEqual(overview["totals"]["referenceTotal"], 2)
        self.assertEqual(overview["totals"]["platformCoverage"], 4)
        may_fifth = next(day for day in overview["days"] if day["date"] == "2026-05-05")
        self.assertEqual(may_fifth["articleCount"], 1)
        self.assertEqual(may_fifth["spend"], 1200)
        self.assertEqual(may_fifth["displayCount"], 1)
        self.assertEqual(may_fifth["referenceCount"], 2)
        self.assertEqual(may_fifth["platformCount"], 3)


if __name__ == "__main__":
    unittest.main()
