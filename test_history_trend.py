from datetime import date, timedelta
from pathlib import Path
import random
import tempfile
import unittest

from core.file_lock import CrossProcessRLock
import core.history as history


class HistoryTrendSeriesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_history_dir = history.HISTORY_DIR
        history.HISTORY_DIR = Path(self._tmpdir.name) / "logs" / "history"

    def tearDown(self) -> None:
        history.HISTORY_DIR = self._original_history_dir
        self._tmpdir.cleanup()

    def test_history_lock_uses_cross_process_lock_in_history_dir(self) -> None:
        lock_key = "任务/锁"
        lock = history._get_lock(lock_key)

        self.assertIsInstance(lock, CrossProcessRLock)
        with lock:
            lock_path = history._history_lock_file(lock_key)
            self.assertTrue(lock_path.exists())
            self.assertTrue(str(lock_path).startswith(str(history.HISTORY_DIR)))

    def test_history_lock_cache_prunes_old_unheld_locks(self) -> None:
        original_locks = history._locks
        original_max_locks = history._MAX_LOCKS
        history._locks = {}
        history._MAX_LOCKS = 4
        self.addCleanup(setattr, history, "_locks", original_locks)
        self.addCleanup(setattr, history, "_MAX_LOCKS", original_max_locks)

        held_lock = history._get_lock("held-lock")
        with held_lock:
            for index in range(8):
                history._get_lock(f"dynamic-task-{index}")

            self.assertIn("held-lock", history._locks)
            self.assertIn("dynamic-task-7", history._locks)
            self.assertLessEqual(len(history._locks), history._MAX_LOCKS)

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

    def test_gap_rate_does_not_flatline_at_zone_floor(self) -> None:
        for low, high in ((80.0, 100.0), (60.0, 80.0), (40.0, 60.0)):
            with self.subTest(zone=(low, high)):
                value = low
                values = []
                for index in range(6):
                    value = history._trend_gap_rate(value, low, high, random.Random(f"{low}-{index}"))
                    values.append(value)

                self.assertGreater(len(set(values)), 1)
                self.assertTrue(any(item > low for item in values))
                self.assertTrue(all(low <= item <= high for item in values))

    def test_new_task_without_success_starts_low_and_climbs_smoothly(self) -> None:
        task_name = "新任务启动曲线"
        brand = "品牌N"
        today = date.today()
        created_at = (today - timedelta(days=4)).isoformat()

        records = [
            {
                "id": f"failure-{offset}",
                "ts": f"{(today - timedelta(days=offset)).isoformat()} 09:00",
                "task_name": task_name,
                "platform": "doubao",
                "keyword": "关键词N",
                "brand": brand,
                "rank": 99,
                "success": False,
                "review_status": "",
                "error_message": "未识别到品牌名",
                "execution_source": "auto",
            }
            for offset in range(4, -1, -1)
        ]
        history._save(history._task_file(task_name), records)

        series = history.get_brand_trend_series(task_name, [brand], 7, task_created_at=created_at)
        self.assertIsNotNone(series)
        values = {
            day.isoformat(): value
            for day, value in zip(series["dates"], series["actual"])
            if value is not None
        }
        path = [
            values[(today - timedelta(days=offset)).isoformat()]
            for offset in range(4, -1, -1)
        ]

        self.assertLess(path[0], 5.0)
        self.assertGreaterEqual(path[1], 7.0)
        self.assertLessEqual(path[1], 13.0)
        self.assertGreaterEqual(path[2], 17.0)
        self.assertLessEqual(path[2], 26.0)
        self.assertGreaterEqual(path[3], 33.0)
        self.assertLessEqual(path[3], 45.0)
        self.assertGreaterEqual(path[-1], 50.0)
        self.assertLessEqual(path[-1], 60.0)
        for previous, current in zip(path, path[1:]):
            self.assertGreater(current, previous)
            self.assertLessEqual(current - previous, 18.0)
        self.assertEqual(series["recorded_dates"], [])

    def test_new_task_without_success_stays_in_day_five_band_afterwards(self) -> None:
        task_name = "新任务第五天后波动"
        brand = "品牌F"
        today = date.today()
        created_at = (today - timedelta(days=8)).isoformat()

        records = [
            {
                "id": f"failure-{offset}",
                "ts": f"{(today - timedelta(days=offset)).isoformat()} 09:00",
                "task_name": task_name,
                "platform": "doubao",
                "keyword": "关键词F",
                "brand": brand,
                "rank": 99,
                "success": False,
                "review_status": "",
                "error_message": "未识别到品牌名",
                "execution_source": "auto",
            }
            for offset in range(8, -1, -1)
        ]
        history._save(history._task_file(task_name), records)

        series = history.get_brand_trend_series(task_name, [brand], 14, task_created_at=created_at)
        self.assertIsNotNone(series)
        values = {
            day.isoformat(): value
            for day, value in zip(series["dates"], series["actual"])
            if value is not None
        }

        for offset in range(4, -1, -1):
            ds = (today - timedelta(days=offset)).isoformat()
            self.assertGreaterEqual(values[ds], 50.0, ds)
            self.assertLessEqual(values[ds], 60.0, ds)
        tail = [
            values[(today - timedelta(days=offset)).isoformat()]
            for offset in range(4, -1, -1)
        ]
        self.assertGreater(len(set(tail)), 1)

    def test_success_connects_to_launch_curve_without_sudden_jump(self) -> None:
        task_name = "新任务成功接续"
        brand = "品牌J"
        today = date.today()
        created_at = (today - timedelta(days=3)).isoformat()

        records = [
            {
                "id": "failure-created",
                "ts": f"{(today - timedelta(days=3)).isoformat()} 09:00",
                "task_name": task_name,
                "platform": "doubao",
                "keyword": "关键词J",
                "brand": brand,
                "rank": 99,
                "success": False,
                "review_status": "",
                "error_message": "未识别到品牌名",
                "execution_source": "auto",
            },
            {
                "id": "failure-next",
                "ts": f"{(today - timedelta(days=2)).isoformat()} 09:00",
                "task_name": task_name,
                "platform": "doubao",
                "keyword": "关键词J",
                "brand": brand,
                "rank": 99,
                "success": False,
                "review_status": "",
                "error_message": "未识别到品牌名",
                "execution_source": "auto",
            },
            {
                "id": "success",
                "ts": f"{(today - timedelta(days=1)).isoformat()} 09:00",
                "task_name": task_name,
                "platform": "doubao",
                "keyword": "关键词J",
                "brand": brand,
                "rank": 1,
                "success": True,
                "review_status": "",
                "execution_source": "auto",
            },
        ]
        history._save(history._task_file(task_name), records)

        series = history.get_brand_trend_series(task_name, [brand], 7, task_created_at=created_at)
        self.assertIsNotNone(series)
        values = {
            day.isoformat(): value
            for day, value in zip(series["dates"], series["actual"])
            if value is not None
        }
        launch_day = values[(today - timedelta(days=3)).isoformat()]
        before_success = values[(today - timedelta(days=2)).isoformat()]
        success_day = values[(today - timedelta(days=1)).isoformat()]
        after_success = values[today.isoformat()]

        self.assertLess(launch_day, 5.0)
        self.assertLess(before_success, 80.0)
        self.assertGreaterEqual(success_day, 80.0)
        self.assertLessEqual(success_day, 86.0)
        self.assertGreaterEqual(after_success, 80.0)
        self.assertLessEqual(after_success, 100.0)

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

    def test_task_id_records_keep_legacy_pre_id_success_without_mixing_other_same_name_tasks(self) -> None:
        task_name = "共享任务趋势"
        task_id = "task-current"
        other_task_id = "task-other"
        brand = "品牌Legacy"
        today = date.today()

        history._save(history._task_file(task_name), [
            {
                "id": "legacy-success",
                "ts": f"{(today - timedelta(days=6)).isoformat()} 09:00",
                "task_name": task_name,
                "platform": "doubao",
                "keyword": "关键词Legacy",
                "brand": brand,
                "rank": 1,
                "success": True,
                "review_status": "",
                "execution_source": "auto",
            },
            {
                "id": "other-task-success",
                "ts": f"{(today - timedelta(days=4)).isoformat()} 09:00",
                "task_id": other_task_id,
                "task_name": task_name,
                "platform": "doubao",
                "keyword": "关键词Legacy",
                "brand": brand,
                "rank": 1,
                "success": True,
                "review_status": "",
                "execution_source": "auto",
            },
        ])
        history._save(history._task_file(task_id), [
            {
                "id": "current-failure",
                "ts": f"{(today - timedelta(days=1)).isoformat()} 09:00",
                "task_id": task_id,
                "task_name": task_name,
                "platform": "doubao",
                "keyword": "关键词Legacy",
                "brand": brand,
                "rank": 99,
                "success": False,
                "review_status": "",
                "error_message": "未识别到品牌名",
                "execution_source": "auto",
            },
        ])

        records = history.get_records(task_name, task_id=task_id)
        self.assertEqual([record["id"] for record in records], ["current-failure"])

        series = history.get_brand_trend_series(task_name, [brand], 7, task_id=task_id)
        self.assertIsNotNone(series)
        self.assertEqual(
            series["recorded_dates"],
            [
                (today - timedelta(days=6)).isoformat(),
                (today - timedelta(days=1)).isoformat(),
            ],
        )
        values = {
            day.isoformat(): value
            for day, value in zip(series["dates"], series["actual"])
            if value is not None
        }
        current_failure_value = values[(today - timedelta(days=1)).isoformat()]
        today_value = values[today.isoformat()]
        self.assertGreaterEqual(current_failure_value, 80.0)
        self.assertLessEqual(current_failure_value, 100.0)
        self.assertGreaterEqual(today_value, 80.0)
        self.assertLessEqual(today_value, 100.0)

    def test_trend_supplement_merges_legacy_pre_id_success_without_other_same_name_tasks(self) -> None:
        task_name = "shared-task"
        task_id = "task-current"
        history._save(history._task_file(task_name), [
            {
                "id": "legacy-success",
                "ts": "2026-01-01 09:00",
                "task_name": task_name,
                "platform": "doubao",
                "keyword": "关键词S",
                "brand": "品牌S",
                "rank": 1,
                "success": True,
                "review_status": "",
                "execution_source": "auto",
            },
            {
                "id": "other-task-success",
                "ts": "2026-01-02 09:00",
                "task_id": "task-other",
                "task_name": task_name,
                "platform": "doubao",
                "keyword": "关键词S",
                "brand": "品牌S",
                "rank": 1,
                "success": True,
                "review_status": "",
                "execution_source": "auto",
            },
        ])
        supplemented = history._supplement_trend_records_with_legacy_success(
            task_name,
            [
                {
                    "id": "current-failure",
                    "ts": "2026-01-03 09:00",
                    "task_id": task_id,
                    "task_name": task_name,
                    "platform": "doubao",
                    "keyword": "关键词S",
                    "brand": "品牌S",
                    "rank": 99,
                    "success": False,
                    "review_status": "",
                    "execution_source": "auto",
                },
            ],
            task_id=task_id,
            brands=["品牌S"],
        )

        self.assertEqual([record["id"] for record in supplemented], ["legacy-success", "current-failure"])

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
