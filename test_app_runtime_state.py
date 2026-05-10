from datetime import datetime, timedelta
from threading import Event

from core.app_runtime import BrowserAuthSessionStore, TestRunStateStore


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def test_test_run_state_prunes_terminal_runs_but_keeps_active_runs():
    now = datetime(2026, 5, 10, 12, 0, 0)
    old = now - timedelta(hours=7)
    store = TestRunStateStore(
        terminal_ttl_seconds=6 * 60 * 60,
        now_func=lambda: now,
        iso_now_func=lambda: _iso(now),
    )
    expired_cancel = Event()
    active_cancel = Event()

    store.create(
        "expired",
        {"runId": "expired", "status": "success", "finishedAt": _iso(old)},
        cancel_event=expired_cancel,
    )
    store.create(
        "active",
        {"runId": "active", "status": "running", "updatedAt": _iso(old)},
        cancel_event=active_cancel,
    )
    store.create("fresh", {"runId": "fresh", "status": "failed", "finishedAt": _iso(now)})

    assert store.prune_terminal() == ["expired"]
    assert store.get("expired") == {}
    assert store.get_cancel_event("expired") is None
    assert store.get("fresh")["status"] == "failed"
    assert store.active_run_ids() == ["active"]
    assert store.get_cancel_event("active") is active_cancel


def test_test_run_state_active_list_update_and_instance_isolation():
    now = datetime(2026, 5, 10, 12, 0, 0)
    first = TestRunStateStore(
        terminal_ttl_seconds=60,
        now_func=lambda: now,
        iso_now_func=lambda: _iso(now),
    )
    second = TestRunStateStore(
        terminal_ttl_seconds=60,
        now_func=lambda: now,
        iso_now_func=lambda: _iso(now),
    )

    first.create("run-1", {"runId": "run-1", "status": "queued", "pollDiagnostics": []})
    first.update("run-1", {"status": "running"})
    first.append_poll_diagnostic("run-1", {"platform": "doubao"})

    assert first.active_run_ids() == ["run-1"]
    assert first.get("run-1")["updatedAt"] == _iso(now)
    assert first.get("run-1")["pollDiagnostics"] == [{"platform": "doubao"}]
    assert second.active_run_ids() == []
    assert second.get("run-1") == {}


def test_browser_auth_session_store_ttl_get_pop_and_prune_candidates():
    now = datetime(2026, 5, 10, 12, 0, 0)
    old = now - timedelta(hours=7)
    store = BrowserAuthSessionStore(
        ttl_seconds=6 * 60 * 60,
        normalize_platform=lambda value: str(value or "").strip().lower(),
        now_func=lambda: now,
    )

    store.set("Doubao", {"opened_at": _iso(old), "profile_id": "old"})
    store.set("kimi", {"opened_at": _iso(now), "profile_id": "fresh"})

    assert store.get("doubao")["profile_id"] == "old"
    assert store.expired_platforms() == ["doubao"]
    assert store.pop("DOUBAO")["profile_id"] == "old"
    assert store.get("doubao") == {}
    assert store.expired_platforms() == []
    assert store.list_platforms() == ["kimi"]


def test_browser_auth_session_store_instances_do_not_share_sessions():
    first = BrowserAuthSessionStore(ttl_seconds=60)
    second = BrowserAuthSessionStore(ttl_seconds=60)

    first.set("doubao", {"opened_at": "2026-05-10T12:00:00"})

    assert first.get("doubao")["opened_at"] == "2026-05-10T12:00:00"
    assert second.get("doubao") == {}
