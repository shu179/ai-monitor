from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest

import core.history as history


class HistoryTrendSeriesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_history_dir = history.HISTORY_DIR
        history.HISTORY_DIR = Path(self._tmpdir.name) / "logs" / "history"

    def tearDown(self) -> None:
        history.HISTORY_DIR = self._original_history_dir
        self._tmpdir.cleanup()

    def test_no_run_days_hold_previous_zone_and_do_not_advance_failure_count(self) -> None:
        task_name = "趋势区间测试"
        brand = "品牌T"
        today = date.today()

        def make_record(day_offset: int, success: bool) -> dict:
            run_date = today + timedelta(days=day_offset)
            return {
                "id": f"{day_offset}-{success}",
                "ts": f"{run_date.isoformat()} 09:00",
                "task_name": task_name,
                "platform": "doubao",
                "keyword": "测试关键词",
                "brand": brand,
                "rank": 1 if success else 99,
                "success": success,
                "review_status": "",
                "error_message": "" if success else "未识别到品牌名",
            }

        records = [
            make_record(-16, True),
            make_record(-14, False),
            make_record(-12, False),
            make_record(-10, False),
            make_record(-8, False),
            make_record(-6, False),
            make_record(-4, False),
            make_record(-2, False),
        ]
        history._save(history._task_file(task_name), records)

        series = history.get_brand_trend_series(task_name, [brand], 21)
        self.assertIsNotNone(series)
        values = {
            day.isoformat(): value
            for day, value in zip(series["dates"], series["actual"])
        }

        def assert_between(day_offset: int, low: float, high: float) -> None:
            ds = (today + timedelta(days=day_offset)).isoformat()
            value = values.get(ds)
            self.assertIsNotNone(value, ds)
            self.assertGreaterEqual(value, low, ds)
            self.assertLessEqual(value, high, ds)

        assert_between(-15, 80.0, 100.0)  # 未运行：沿用成功后的区间
        assert_between(-9, 80.0, 100.0)   # 第 3 次失败后的未运行日仍不降档
        assert_between(-8, 60.0, 80.0)    # 第 4 次实际失败才进入 60-80
        assert_between(-7, 60.0, 80.0)    # 未运行：沿用 60-80 区间
        assert_between(-2, 40.0, 60.0)    # 第 7 次实际失败进入 40-60
        assert_between(-1, 40.0, 60.0)    # 未运行：沿用 40-60 区间
        assert_between(0, 40.0, 60.0)     # 今天未运行，不继续累计失败

    def test_dashboard_summary_prefers_actual_run_days(self) -> None:
        from web_backend import _summarize_trend_points

        summary = _summarize_trend_points(
            [
                {"date": "2026-01-01", "value": 92},
                {"date": "2026-01-02", "value": 60},
                {"date": "2026-01-03", "value": 40},
            ],
            recorded_dates=["2026-01-01"],
            range_key="week",
        )

        self.assertEqual(summary["current"], 92)
        self.assertEqual(summary["avg"], 92)
        self.assertEqual(summary["peak"], 92)

    def test_success_once_wins_over_later_manual_test_failure(self) -> None:
        task_name = "测试成功锁定"
        brand = "品牌S"
        today = date.today()

        records = [
            {
                "id": "formal-success",
                "ts": f"{today.isoformat()} 09:00",
                "task_name": task_name,
                "platform": "doubao",
                "keyword": "关键词S",
                "brand": brand,
                "rank": 1,
                "success": True,
                "review_status": "",
                "execution_source": "auto",
            },
            {
                "id": "manual-test-failure",
                "ts": f"{today.isoformat()} 10:00",
                "task_name": task_name,
                "platform": "doubao",
                "keyword": "关键词S",
                "brand": brand,
                "rank": 99,
                "success": False,
                "review_status": "",
                "error_message": "未识别到品牌名",
                "execution_source": "manual_test",
            },
        ]
        history._save(history._task_file(task_name), records)

        bundle = history.get_task_daily_query_state_bundle(task_name, target_date=today)

        self.assertEqual(len(bundle["success_query_results"]), 1)
        self.assertEqual(bundle["failed_query_details"], [])

    def test_manual_test_failure_does_not_advance_trend_or_get_recorded_from_runner(self) -> None:
        from main import _record_result_history

        task_name = "测试失败不计入"
        brand = "品牌M"
        today = date.today()

        history._save(history._task_file(task_name), [
            {
                "id": "formal-success-before",
                "ts": f"{(today - timedelta(days=4)).isoformat()} 09:00",
                "task_name": task_name,
                "platform": "doubao",
                "keyword": "关键词M",
                "brand": brand,
                "rank": 1,
                "success": True,
                "review_status": "",
                "execution_source": "auto",
            },
            *[
                {
                    "id": f"manual-test-failure-{offset}",
                    "ts": f"{(today - timedelta(days=offset)).isoformat()} 10:00",
                    "task_name": task_name,
                    "platform": "doubao",
                    "keyword": "关键词M",
                    "brand": brand,
                    "rank": 99,
                    "success": False,
                    "review_status": "",
                    "error_message": "未识别到品牌名",
                    "execution_source": "manual_test",
                }
                for offset in (3, 2, 1, 0)
            ],
        ])

        series = history.get_brand_trend_series(task_name, [brand], 7)
        self.assertIsNotNone(series)
        self.assertEqual(series["recorded_dates"], [(today - timedelta(days=4)).isoformat()])
        values = {
            day.isoformat(): value
            for day, value in zip(series["dates"], series["actual"])
        }
        for offset in (3, 2, 1, 0):
            value = values[(today - timedelta(days=offset)).isoformat()]
            self.assertIsNotNone(value)
            self.assertGreaterEqual(value, 80.0)
            self.assertLessEqual(value, 100.0)

        runner_task = "测试运行写历史"
        _record_result_history(
            runner_task,
            {
                "platform": "doubao",
                "keyword": "关键词R",
                "brand": brand,
                "rank": 99,
                "mode": "browser",
                "error_message": "未识别到品牌名",
            },
            execution_source="manual_test",
            task_id="runner_task",
        )
        self.assertEqual(history.get_records(runner_task, task_id="runner_task"), [])

        _record_result_history(
            runner_task,
            {
                "platform": "doubao",
                "keyword": "关键词R",
                "brand": brand,
                "rank": 1,
                "mode": "browser",
            },
            execution_source="manual_test",
            task_id="runner_task",
        )
        self.assertEqual(len(history.get_records(runner_task, task_id="runner_task")), 1)

    def test_legacy_test_failure_source_is_excluded_from_rates(self) -> None:
        task_name = "旧测试失败兼容"
        brand = "品牌L"
        today = date.today()

        history._save(history._task_file(task_name), [
            {
                "id": "success-before",
                "ts": f"{(today - timedelta(days=2)).isoformat()} 09:00",
                "task_name": task_name,
                "platform": "doubao",
                "keyword": "关键词L",
                "brand": brand,
                "rank": 1,
                "success": True,
                "review_status": "",
            },
            {
                "id": "legacy-test-failure",
                "ts": f"{(today - timedelta(days=1)).isoformat()} 09:00",
                "task_name": task_name,
                "platform": "doubao",
                "keyword": "关键词L",
                "brand": brand,
                "rank": 99,
                "success": False,
                "review_status": "",
                "error_message": "未识别到品牌名",
                "source": "test",
            },
        ])

        rates = history.get_daily_rates(task_name)
        by_date = {item["date"]: item for item in rates}
        held = by_date[(today - timedelta(days=1)).isoformat()]
        self.assertFalse(held["missing"])
        self.assertGreaterEqual(held["rate"], 80.0)
        self.assertLessEqual(held["rate"], 100.0)

    def test_brand_trend_can_be_scoped_by_task_id(self) -> None:
        task_name = "重名任务趋势"
        brand = "品牌N"
        today = date.today()

        history._save(history._task_file("task-low"), [
            {
                "id": "low-failure",
                "ts": f"{(today - timedelta(days=3)).isoformat()} 09:00",
                "task_id": "task-low",
                "task_name": task_name,
                "platform": "doubao",
                "keyword": "关键词N",
                "brand": brand,
                "rank": 99,
                "success": False,
                "review_status": "",
                "error_message": "未识别到品牌名",
                "execution_source": "auto",
            },
        ])
        history._save(history._task_file("task-high"), [
            {
                "id": "high-success",
                "ts": f"{(today - timedelta(days=2)).isoformat()} 09:00",
                "task_id": "task-high",
                "task_name": task_name,
                "platform": "doubao",
                "keyword": "关键词N",
                "brand": brand,
                "rank": 1,
                "success": True,
                "review_status": "",
                "execution_source": "auto",
            },
        ])

        series = history.get_brand_trend_series(task_name, [brand], 7, task_id="task-high")
        self.assertIsNotNone(series)
        values = [value for value in series["actual"] if value is not None]
        self.assertTrue(values)
        self.assertGreaterEqual(values[0], 80.0)

    def test_period_report_excludes_manual_test_failures(self) -> None:
        import core.reports as reports

        task_name = "报表测试失败过滤"
        brand = "品牌R"
        today = date.today()
        original_report_dir = reports.REPORT_DIR
        original_render = reports.render_report_card
        reports.REPORT_DIR = Path(self._tmpdir.name) / "reports"
        reports.render_report_card = lambda title, lines, output_path: output_path
        self.addCleanup(setattr, reports, "REPORT_DIR", original_report_dir)
        self.addCleanup(setattr, reports, "render_report_card", original_render)

        history._save(history._task_file("report-task"), [
            {
                "id": "formal-success",
                "ts": f"{today.isoformat()} 09:00",
                "task_id": "report-task",
                "task_name": task_name,
                "platform": "doubao",
                "keyword": "关键词R",
                "brand": brand,
                "rank": 1,
                "success": True,
                "review_status": "",
                "execution_source": "auto",
            },
            {
                "id": "manual-test-failure",
                "ts": f"{today.isoformat()} 10:00",
                "task_id": "report-task",
                "task_name": task_name,
                "platform": "doubao",
                "keyword": "关键词R",
                "brand": brand,
                "rank": 99,
                "success": False,
                "review_status": "",
                "error_message": "未识别到品牌名",
                "execution_source": "manual_test",
            },
        ])

        report = reports.generate_period_report({
            "tasks": [{
                "name": task_name,
                "task_id": "report-task",
                "brand": brand,
                "keywords": [{"keyword": "关键词R", "brand": brand, "platforms": ["doubao"]}],
            }]
        })

        self.assertEqual(report["overall_total"], 1)
        self.assertEqual(report["overall_success"], 1)
        self.assertEqual(report["overall_rate"], 100.0)


if __name__ == "__main__":
    unittest.main()
