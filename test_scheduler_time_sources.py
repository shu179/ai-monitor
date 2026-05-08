from datetime import datetime
from pathlib import Path
import tempfile
from unittest.mock import patch

from core.file_lock import CrossProcessRLock
from core.scheduler import SmartScheduler
import core.scheduler_state as scheduler_state


def test_should_run_task_today_uses_local_now_when_now_not_provided():
    scheduler = SmartScheduler({"weekly_times": {"4": "09:30"}})
    task = {
        "enabled": True,
        "name": "品牌A",
        "weekdays": [4],
        "keywords": [{"keyword": "词A", "platforms": ["doubao"], "mode": "browser"}],
    }

    with patch("core.scheduler.local_now", return_value=datetime(2026, 5, 8, 9, 31)):
        assert scheduler.should_run_task_today(task)
        run_at = scheduler.get_task_run_datetime(task)

    assert run_at == datetime(2026, 5, 8, 9, 30)


def test_get_status_uses_local_now_for_business_date():
    scheduler = SmartScheduler({"weekly_times": {"4": "09:30"}})

    with patch("core.scheduler.local_now", return_value=datetime(2026, 5, 8, 9, 31)):
        status = scheduler.get_status()

    assert status["current_weekday"] == 4
    assert status["current_date"] == "2026-05-08"


def test_get_running_tasks_uses_monotonic_elapsed_time():
    scheduler = SmartScheduler({})
    scheduler._task_start_times["品牌A [抓取模式]"] = (100.0, {"task_id": "task-a"})

    with patch("core.scheduler.time.monotonic", return_value=160.5):
        running = scheduler.get_running_tasks()

    assert running == {"品牌A [抓取模式]": 60.5}


def test_get_running_tasks_never_reports_negative_elapsed_time():
    scheduler = SmartScheduler({})
    scheduler._task_start_times["品牌A [抓取模式]"] = (200.0, {"task_id": "task-a"})

    with patch("core.scheduler.time.monotonic", return_value=160.5):
        running = scheduler.get_running_tasks()

    assert running == {"品牌A [抓取模式]": 0.0}


def test_scheduler_state_uses_cross_process_lock_next_to_state_file():
    original_state_path = scheduler_state.STATE_PATH
    with tempfile.TemporaryDirectory() as tmpdir:
        scheduler_state.STATE_PATH = Path(tmpdir) / "scheduler_state.json"
        try:
            assert isinstance(scheduler_state._LOCK, CrossProcessRLock)
            with scheduler_state._LOCK:
                assert scheduler_state.STATE_PATH.with_name("scheduler_state.json.lock").exists()
        finally:
            scheduler_state.STATE_PATH = original_state_path


def test_scheduler_state_success_decays_structural_failures():
    original_state_path = scheduler_state.STATE_PATH
    with tempfile.TemporaryDirectory() as tmpdir:
        scheduler_state.STATE_PATH = Path(tmpdir) / "scheduler_state.json"
        try:
            scheduler_state.update_after_run(
                "unit-a",
                task_name="品牌A",
                mode="browser",
                round_status="failed",
                failure_kind="structural",
            )
            scheduler_state.update_after_run(
                "unit-a",
                task_name="品牌A",
                mode="browser",
                round_status="failed",
                failure_kind="structural",
            )

            entry = scheduler_state.update_after_run(
                "unit-a",
                task_name="品牌A",
                mode="browser",
                round_status="success",
                recovered_manually=False,
            )

            assert entry["structural_failures"] == 1
            assert entry["consecutive_auto_failures"] == 0
        finally:
            scheduler_state.STATE_PATH = original_state_path
