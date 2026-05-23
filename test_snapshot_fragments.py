import unittest
from datetime import datetime

from backend_lib.snapshot_fragments import (
    _build_snapshot_assistant,
    _build_snapshot_branding,
    _build_snapshot_dashboard,
    _build_snapshot_monitoring,
    _build_snapshot_platform_items,
    _build_snapshot_profile,
    _build_snapshot_profile_summary,
    _build_snapshot_session,
    _build_snapshot_sidebar,
    _build_snapshot_source_breakdown,
    _build_snapshot_stats,
    _build_snapshot_task_items,
    _collect_snapshot_tag_options,
)


class SnapshotFragmentTests(unittest.TestCase):
    def test_shell_fragments_preserve_top_level_shapes(self) -> None:
        self.assertEqual(
            _build_snapshot_session("token-1", "X-Token"),
            {"token": "token-1", "header": "X-Token"},
        )
        self.assertEqual(
            _build_snapshot_branding("Surfaced"),
            {"appName": "Surfaced", "brandName": "Surfaced", "subtitle": ""},
        )
        self.assertEqual(
            _build_snapshot_stats(
                enabled_task_count=2,
                total_task_count=3,
                today_record_count=4,
                hit_record_count=5,
                error_record_count=6,
            ),
            {
                "enabledTasks": 2,
                "totalTasks": 3,
                "todayRecords": 4,
                "hitRecords": 5,
                "errorRecords": 6,
            },
        )
        self.assertEqual(
            _build_snapshot_monitoring(
                enabled=True,
                running=False,
                status_message="定时任务已开启",
            ),
            {
                "enabled": True,
                "running": False,
                "statusMessage": "定时任务已开启",
            },
        )

    def test_profile_summary_feeds_sidebar_and_profile_shapes(self) -> None:
        summary = _build_snapshot_profile_summary(
            {
                "profile": {
                    "name": "AI 运营",
                    "role": "管理员",
                    "avatar": "avatar.png",
                    "birthday": "2026-01-01",
                    "hire_date": "2026-02-01",
                }
            },
            avatar_url_builder=lambda value: f"/profile/{value}" if value else "",
        )

        self.assertEqual(
            summary,
            {
                "name": "AI 运营",
                "role": "管理员",
                "avatar": "avatar.png",
                "avatarUrl": "/profile/avatar.png",
                "birthday": "2026-01-01",
                "hireDate": "2026-02-01",
            },
        )
        self.assertEqual(
            _build_snapshot_sidebar(summary),
            {
                "userName": "AI 运营",
                "role": "管理员",
                "avatar": "/profile/avatar.png",
                "online": True,
            },
        )
        self.assertEqual(
            _build_snapshot_profile(summary),
            {
                "name": "AI 运营",
                "role": "管理员",
                "avatar": "avatar.png",
                "birthday": "2026-01-01",
                "hireDate": "2026-02-01",
            },
        )

    def test_assistant_status_tracks_monitoring_state(self) -> None:
        config = {"ai_assistant": {"platform": "DeepSeek", "model": "deepseek-chat"}}

        running = _build_snapshot_assistant(config, monitoring_enabled=True, monitoring_running=True)
        enabled = _build_snapshot_assistant(config, monitoring_enabled=True, monitoring_running=False)
        disabled = _build_snapshot_assistant({}, monitoring_enabled=False, monitoring_running=False)

        self.assertEqual(running["status"], "定时任务运行中")
        self.assertEqual(enabled["status"], "定时任务已开启")
        self.assertEqual(disabled["status"], "定时任务已关闭")
        self.assertEqual(disabled["platform"], "监控助手")
        self.assertEqual(disabled["model"], "running")

    def test_dashboard_fragment_preserves_existing_defaults(self) -> None:
        dashboard = _build_snapshot_dashboard(
            current_date=datetime(2026, 1, 2, 9, 30, 0),
            weekday_label="五",
            home_copy={},
            profile_name="",
            dashboard_today_summary={
                "todayTaskCount": 3,
                "completedCount": 1,
                "runningCount": 2,
            },
            failed_task_details=[{"taskName": "任务一"}],
            today_record_count=0,
            hit_record_count=5,
            dashboard_trend={"points": []},
            source_breakdown=[{"name": "科技互联网", "value": 1}],
            task_cards=[{"key": "capture"}],
            media_stats=[{"name": "权威媒体", "value": 2}],
            month_overview={"month": "2026-01"},
        )

        self.assertEqual(dashboard["dateLabel"], "02 JAN 2026")
        self.assertEqual(dashboard["weekdayLabel"], "五")
        self.assertEqual(dashboard["greeting"], "下午好")
        self.assertEqual(dashboard["userName"], "AI 运营")
        self.assertEqual(dashboard["headline"], "系统运行平稳，今日已为您自动拦截 5 项异常请求。")
        self.assertEqual(dashboard["todayIntercepted"], 12)
        self.assertEqual(dashboard["completedCount"], 1)
        self.assertEqual(dashboard["runningCount"], 2)
        self.assertEqual(dashboard["failedTaskCount"], 1)
        self.assertEqual(dashboard["monthOverview"], {"month": "2026-01"})

    def test_source_breakdown_counts_all_tasks_and_uses_fallback(self) -> None:
        tasks = [
            {"enabled": True, "industry_tags": ["科技互联网", "金融医疗"]},
            {"enabled": False, "industry_tags": [" 科技互联网 ", ""]},
        ]

        breakdown = _build_snapshot_source_breakdown(tasks)

        self.assertEqual(
            breakdown,
            [
                {"name": "科技互联网", "value": 2},
                {"name": "金融医疗", "value": 1},
            ],
        )
        self.assertEqual(
            _build_snapshot_source_breakdown([])[0],
            {"name": "科技互联网", "value": 45},
        )

    def test_platform_items_match_snapshot_shape(self) -> None:
        items = _build_snapshot_platform_items(
            {
                "deepseek": {
                    "enabled": True,
                    "api_model": "deepseek-chat",
                    "api_key": "sk-secret",
                    "user_data_dir": "/tmp/profile",
                },
                "kimi": {"api_key": "abcdefghijk"},
            }
        )

        self.assertEqual(
            items,
            [
                {
                    "id": "deepseek",
                    "name": "deepseek",
                    "enabled": True,
                    "model": "deepseek-chat",
                    "userDataDir": "/tmp/profile",
                },
                {
                    "id": "kimi",
                    "name": "kimi",
                    "enabled": False,
                    "model": "abcdefgh",
                    "userDataDir": "",
                },
            ],
        )

    def test_task_items_keep_existing_dashboard_fields(self) -> None:
        tasks = [
            {
                "task_id": "task-1",
                "name": "任务一",
                "brand": "品牌一",
                "keywords": [
                    {"platforms": ["deepseek", "kimi", "deepseek", ""]},
                ],
                "optimization_start_date": "2026-01-01",
                "optimization_end_date": "2026-02-01",
            }
        ]

        payload = _build_snapshot_task_items(
            tasks,
            active_mode="browser",
            scheduler_config={"enabled": True},
            platform_display_name=lambda value: {"deepseek": "DeepSeek", "kimi": "Kimi"}.get(value, value),
            day_status_loader=lambda _task: {
                "status": "running",
                "source": "formal",
                "test_status": "success",
                "test_source": "manual",
            },
            schedule_describer=lambda task, scheduler: f"{task['name']}:{bool(scheduler.get('enabled'))}",
            status_labeler=lambda status: f"label:{status}",
        )

        self.assertEqual(
            payload,
            [
                {
                    "id": "task-1",
                    "name": "任务一",
                    "brand": "品牌一",
                    "mode": "browser",
                    "schedule": "任务一:True",
                    "status": "running",
                    "statusLabel": "label:running",
                    "statusSource": "formal",
                    "testStatus": "success",
                    "testStatusSource": "manual",
                    "platforms": ["DeepSeek", "Kimi"],
                    "optimization_start_date": "2026-01-01",
                    "optimization_end_date": "2026-02-01",
                }
            ],
        )

    def test_tag_options_preserve_existing_sorted_values(self) -> None:
        tags = _collect_snapshot_tag_options(
            [
                {"industry_tags": ["消费零售", "科技互联网"], "region_tags": ["华东", "华北"]},
                {"industry_tags": ["科技互联网", ""], "region_tags": ["华东"]},
            ]
        )

        self.assertEqual(tags["industryTags"], ["消费零售", "科技互联网"])
        self.assertEqual(tags["regionTags"], ["华东", "华北"])


if __name__ == "__main__":
    unittest.main()
