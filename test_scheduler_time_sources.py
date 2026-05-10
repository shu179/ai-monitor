from datetime import datetime
import os
from pathlib import Path
import re
import tempfile
from unittest.mock import patch

import core.article_store as article_store
import core.history as history
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


def test_article_store_cooldown_uses_monotonic_after_wall_clock_jump():
    article_store.reset_article_store_backend_health_for_tests()
    try:
        with (
            patch.dict(os.environ, {
                "AIBRANDMONITOR_ARTICLE_STORE_SQLITE_ERROR_LIMIT": "1",
                "AIBRANDMONITOR_ARTICLE_STORE_SQLITE_COOLDOWN_SECONDS": "120",
            }),
            patch("core.article_store.local_now", return_value=datetime(2026, 5, 10, 10, 0, 0)),
            patch("core.article_store.time.monotonic", return_value=100.0),
        ):
            article_store._record_article_store_backend_error(  # noqa: SLF001
                "sqlite",
                "unit_test_error",
                detail="boom",
            )

        with (
            patch("core.article_store.time.time", side_effect=AssertionError("wall clock must not drive cooldown")),
            patch("core.article_store.time.monotonic", return_value=101.0),
        ):
            assert article_store._article_store_sqlite_cooldown_remaining() == 119.0  # noqa: SLF001
    finally:
        article_store.reset_article_store_backend_health_for_tests()


def test_article_store_migration_wait_uses_monotonic_deadline():
    class FakeThread:
        def __init__(self):
            self.alive = True
            self.joins: list[float] = []

        def is_alive(self):
            return self.alive

        def join(self, timeout=None):
            self.joins.append(float(timeout or 0.0))
            self.alive = False

    fake_thread = FakeThread()
    with article_store._article_store_migration_lock:  # noqa: SLF001
        article_store._article_store_migration_threads[:] = [fake_thread]  # noqa: SLF001
    try:
        with (
            patch("core.article_store.time.time", side_effect=AssertionError("wall clock must not drive migration waits")),
            patch("core.article_store.time.monotonic", side_effect=[100.0, 100.05]),
        ):
            article_store.wait_for_article_store_backend_migration(timeout=0.2)
    finally:
        with article_store._article_store_migration_lock:  # noqa: SLF001
            article_store._article_store_migration_threads.clear()  # noqa: SLF001

    assert fake_thread.joins == [0.05]


def test_history_shadow_rebuild_cooldown_uses_monotonic_after_wall_clock_jump():
    with history._structured_shadow_rebuild_lock:  # noqa: SLF001
        original = dict(history._structured_shadow_rebuild_state)  # noqa: SLF001
        history._structured_shadow_rebuild_state.clear()  # noqa: SLF001
        history._structured_shadow_rebuild_state.update({  # noqa: SLF001
            "running": False,
            "next_allowed_at": 160.0,
            "next_allowed_at_monotonic": 160.0,
        })
    try:
        with (
            patch("core.history.time.time", side_effect=AssertionError("wall clock must not drive rebuild cooldown")),
            patch("core.history.time.monotonic", return_value=100.0),
        ):
            snapshot = history._structured_shadow_rebuild_state_snapshot()  # noqa: SLF001
    finally:
        with history._structured_shadow_rebuild_lock:  # noqa: SLF001
            history._structured_shadow_rebuild_state.clear()  # noqa: SLF001
            history._structured_shadow_rebuild_state.update(original)  # noqa: SLF001

    assert snapshot["cooldown_remaining_seconds"] == 60.0


def test_runtime_code_does_not_import_main_module():
    root = Path(__file__).resolve().parent
    runtime_targets = [root / "core", root / "backend_lib", root / "web_backend.py"]
    import_pattern = re.compile(r"^\s*(from\s+main\s+import|import\s+main\b)", re.MULTILINE)
    offenders: list[str] = []

    for target in runtime_targets:
        paths = [target] if target.is_file() else sorted(target.rglob("*.py"))
        for path in paths:
            if path.name.startswith("test_"):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if import_pattern.search(text):
                offenders.append(str(path.relative_to(root)))

    assert offenders == []
