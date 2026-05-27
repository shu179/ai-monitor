from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from core.cloud_client import CloudClientError
from core.cloud_sync_runtime import AppCloudRuntimeSupport, create_local_cloud_sync_runtime


def test_create_local_cloud_sync_runtime_wires_burst_env(monkeypatch):
    monkeypatch.setenv("AIBRANDMONITOR_CLOUD_UPLOAD_BURST_INTERVAL_SECONDS", "2.5")
    monkeypatch.setenv("AIBRANDMONITOR_CLOUD_UPLOAD_BURST_PENDING_THRESHOLD", "321")

    with (
        patch("core.cloud_sync_runtime.CloudSyncManager") as manager_cls,
        patch("core.cloud_sync_runtime.CloudPlatformAutoSync") as auto_sync_cls,
    ):
        manager = MagicMock()
        auto_sync = MagicMock()
        manager_cls.return_value = manager
        auto_sync_cls.return_value = auto_sync

        runtime = create_local_cloud_sync_runtime(
            config_getter=lambda: {},
            bundle_applier=lambda *_args, **_kwargs: None,
            config_updater=lambda _config: None,
            pull_tasks=lambda **_kwargs: {"ok": True},
            recover_upload_candidates=lambda: {"ok": True},
            logger=lambda _message: None,
        )

    assert runtime.manager is manager
    assert runtime.platform_auto_sync is auto_sync
    assert auto_sync_cls.call_args.kwargs["upload_burst_interval_seconds"] == 2.5
    assert auto_sync_cls.call_args.kwargs["upload_burst_pending_threshold"] == 321


def _support_owner() -> MagicMock:
    owner = MagicMock()
    owner._article_cloud_enqueue_lock = threading.RLock()
    owner._last_article_cloud_enqueue_key = None
    owner._article_cloud_enqueue_requested = False
    owner._article_cloud_enqueue_thread = None
    owner._article_cloud_enqueue_retry_thread = None
    owner._cloud_status_validation_lock = threading.RLock()
    owner._cloud_status_validated_identity = ""
    owner._cloud_status_validated_at = 0.0
    owner._cloud_status_validation_error = ""
    owner._article_store_version_key.return_value = ("articles.json", 1, 10)
    owner._article_cloud_task_map_key.return_value = "task-map"
    owner._get_cloud_article_upload_snapshot_with_refresh_state.return_value = ([{"title": "A"}], False)
    return owner


def test_app_cloud_runtime_support_uses_injected_thread_factory_for_snapshot_worker():
    owner = _support_owner()
    created_threads: list[dict[str, object]] = []

    class FakeThread:
        def __init__(self, *args, **kwargs) -> None:
            created_threads.append({"args": args, "kwargs": kwargs})
            self.kwargs = kwargs
            self.started = False

        def start(self) -> None:
            self.started = True

        def is_alive(self) -> bool:
            return self.started

    support = AppCloudRuntimeSupport(owner=owner, thread_factory=lambda *args, **kwargs: FakeThread(*args, **kwargs))
    support.schedule_article_snapshot()

    assert len(created_threads) == 1
    assert created_threads[0]["kwargs"]["name"] == "cloud-article-snapshot-enqueue"
    assert created_threads[0]["kwargs"]["daemon"] is True


def test_app_cloud_runtime_support_cloud_status_uses_injected_status_and_outbox():
    owner = _support_owner()
    owner._cloud_status_validation_error = "stale token"
    auto_sync_status = {"running": True}
    outbox = MagicMock()
    outbox.stats.return_value = {"pending": 2, "failed": 1}
    support = AppCloudRuntimeSupport(
        owner=owner,
        outbox_factory=lambda: outbox,
        auto_sync_status_getter=lambda: auto_sync_status,
        current_account_config_path_getter=lambda: "/tmp/config.yaml",
    )

    result = support.cloud_status_from_session(
        {
            "base_url": "https://api.surfacedlab.com",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "saved_at": "2026-05-27T00:00:00",
            "user": {"id": 7, "workspace_id": 9, "username": "operator", "role": "operator"},
        }
    )

    assert result["cloud"]["loggedIn"] is True
    assert result["cloud"]["outbox"] == {"pending": 2, "failed": 1}
    assert result["cloud"]["autoSync"] == auto_sync_status
    assert result["cloud"]["validationError"] == "stale token"
    assert result["cloud"]["localProfile"]["configPath"] == "/tmp/config.yaml"


def test_app_cloud_runtime_support_retries_request_after_refresh():
    owner = _support_owner()
    session_store = MagicMock()
    session_store.load.side_effect = [
        {
            "base_url": "https://api.surfacedlab.com",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "user": {"id": 2, "workspace_id": 3},
        },
        {
            "base_url": "https://api.surfacedlab.com",
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "user": {"id": 2, "workspace_id": 3},
        },
    ]
    session_store.refresh_login_if_current.return_value = {
        "base_url": "https://api.surfacedlab.com",
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "user": {"id": 2, "workspace_id": 3},
    }
    client = MagicMock()
    operation = Mock(side_effect=[CloudClientError("expired", status_code=401), {"ok": True}])
    support = AppCloudRuntimeSupport(
        owner=owner,
        session_store_factory=lambda: session_store,
        request_client_factory=lambda _base_url: client,
    )

    ok, payload, message = support.cloud_request_with_refresh(operation)

    assert ok is True
    assert payload == {"ok": True}
    assert message == ""
    session_store.refresh_login_if_current.assert_called_once()
    assert operation.call_args_list[0].args == (client, "old-access")
    assert operation.call_args_list[1].args == (client, "new-access")


def test_app_cloud_runtime_support_recovers_run_history_and_articles_into_session_outbox():
    owner = _support_owner()
    owner.load_config.return_value = {"tasks": [{"cloud_task_id": 42}]}
    owner._get_cloud_article_upload_snapshot_with_refresh_state.return_value = ([{"title": "A"}], False)
    session = {
        "base_url": "https://api.surfacedlab.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 2, "workspace_id": 3},
    }
    session_store = MagicMock()
    session_store.load.return_value = session
    outbox = MagicMock()
    bound_outbox = MagicMock()
    outbox.bind_to_session.return_value = bound_outbox
    article_enqueue = Mock(return_value={"articles": 1, "queued": 1})

    with patch(
        "core.cloud_sync_runtime.enqueue_recent_cloud_run_records_from_history",
        return_value={"records": 2, "queued": 2},
    ) as run_enqueue:
        support = AppCloudRuntimeSupport(
            owner=owner,
            session_store_factory=lambda: session_store,
            outbox_factory=lambda: outbox,
            article_enqueue_fn=article_enqueue,
        )

        result = support.recover_cloud_run_history_uploads()

    assert result["ok"] is True
    assert result["run_records"] == {"records": 2, "queued": 2}
    assert result["articles_sync"] == {"articles": 1, "queued": 1}
    outbox.bind_to_session.assert_called_once_with(session)
    run_enqueue.assert_called_once_with(owner.load_config.return_value, outbox=bound_outbox, days=7)
    article_enqueue.assert_called_once_with([{"title": "A"}], owner.load_config.return_value, outbox=bound_outbox)


def test_app_cloud_runtime_support_schedules_retry_when_recovery_article_snapshot_is_deferred():
    owner = _support_owner()
    owner.load_config.return_value = {"tasks": []}
    owner._get_cloud_article_upload_snapshot_with_refresh_state.return_value = ([{"title": "stale"}], True)
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.surfacedlab.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 2, "workspace_id": 3},
    }
    outbox = MagicMock()
    outbox.bind_to_session.return_value = MagicMock()
    article_enqueue = Mock()
    support = AppCloudRuntimeSupport(
        owner=owner,
        session_store_factory=lambda: session_store,
        outbox_factory=lambda: outbox,
        article_enqueue_fn=article_enqueue,
        thread_factory=lambda *args, **kwargs: MagicMock(start=lambda: None, is_alive=lambda: False),
    )

    with patch(
        "core.cloud_sync_runtime.enqueue_recent_cloud_run_records_from_history",
        return_value={"records": 0, "queued": 0},
    ):
        result = support.recover_cloud_run_history_uploads()

    assert result["ok"] is True
    assert result["articles_sync"]["deferred"] is True
    assert result["articles_sync"]["queued"] == 0
    owner._schedule_cloud_articles_snapshot_retry.assert_called_once()
    article_enqueue.assert_not_called()


def test_app_cloud_runtime_support_logout_flushes_pending_outbox_and_clears_session():
    owner = _support_owner()
    owner._activate_current_account_space = Mock()
    session = {
        "base_url": "https://api.surfacedlab.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 2, "workspace_id": 3},
    }
    session_store = MagicMock()
    session_store.load.return_value = session
    outbox = MagicMock()
    bound_outbox = MagicMock()
    bound_outbox.stats.return_value = {"pending": 2, "failed": 0}
    outbox.bind_to_session.return_value = bound_outbox
    flush_outbox = Mock(return_value={"ok": True})
    client = MagicMock()
    support = AppCloudRuntimeSupport(
        owner=owner,
        session_store_factory=lambda: session_store,
        outbox_factory=lambda: outbox,
        flush_outbox_fn=flush_outbox,
        request_client_factory=lambda _base_url: client,
    )

    result = support.logout_cloud_account()

    assert result["ok"] is True
    outbox.bind_to_session.assert_called_once_with(session)
    flush_outbox.assert_called_once_with(outbox=bound_outbox)
    client.logout.assert_called_once_with("refresh")
    session_store.clear.assert_called_once()
    owner._activate_current_account_space.assert_called_once_with(copy_legacy=False)


def test_app_cloud_runtime_support_logout_continues_when_flush_or_remote_logout_fails():
    owner = _support_owner()
    owner._activate_current_account_space = Mock()
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.surfacedlab.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 2, "workspace_id": 3},
    }
    outbox = MagicMock()
    bound_outbox = MagicMock()
    bound_outbox.stats.return_value = {"pending": 1, "failed": 1}
    outbox.bind_to_session.return_value = bound_outbox
    client = MagicMock()
    client.logout.side_effect = CloudClientError("remote logout failed", status_code=503)
    support = AppCloudRuntimeSupport(
        owner=owner,
        session_store_factory=lambda: session_store,
        outbox_factory=lambda: outbox,
        flush_outbox_fn=Mock(side_effect=RuntimeError("flush failed")),
        request_client_factory=lambda _base_url: client,
    )

    result = support.logout_cloud_account()

    assert result["ok"] is True
    session_store.clear.assert_called_once()
    owner._activate_current_account_space.assert_called_once_with(copy_legacy=False)


def test_app_cloud_runtime_support_flush_cloud_outbox_uses_payload_limit():
    owner = _support_owner()
    flush_outbox = Mock(return_value={"ok": True, "metrics": {"event_count": 3}})
    support = AppCloudRuntimeSupport(owner=owner, flush_outbox_fn=flush_outbox)

    result = support.flush_cloud_outbox({"limit": "250"})

    assert result == {"ok": True, "metrics": {"event_count": 3}}
    flush_outbox.assert_called_once_with(limit=250)


def test_app_cloud_runtime_support_flush_cloud_outbox_defaults_bad_limit():
    owner = _support_owner()
    flush_outbox = Mock(return_value={"ok": True})
    support = AppCloudRuntimeSupport(owner=owner, flush_outbox_fn=flush_outbox)

    support.flush_cloud_outbox({"limit": "not-a-number"})

    flush_outbox.assert_called_once_with(limit=100)


def test_app_cloud_runtime_support_command_flushes_outbox_with_payload_limit():
    owner = _support_owner()
    flush_outbox = Mock(return_value={"ok": True, "metrics": {"event_count": 4}})
    support = AppCloudRuntimeSupport(owner=owner, flush_outbox_fn=flush_outbox)

    result = support.handle_command("cloud.flush_outbox", {"limit": "333"})

    assert result == {"ok": True, "metrics": {"event_count": 4}}
    flush_outbox.assert_called_once_with(limit=333)


def test_app_cloud_runtime_support_command_schedules_article_snapshot():
    owner = _support_owner()
    started: list[object] = []

    class FakeThread:
        def __init__(self, *args, **kwargs) -> None:
            self.kwargs = kwargs
            self.started = False

        def start(self) -> None:
            self.started = True
            started.append(self)

        def is_alive(self) -> bool:
            return self.started

    support = AppCloudRuntimeSupport(owner=owner, thread_factory=lambda *args, **kwargs: FakeThread(*args, **kwargs))

    result = support.handle_command("cloud.schedule_article_snapshot")

    assert result == {"ok": True, "message": "文章云端同步已调度"}
    assert owner._article_cloud_enqueue_requested is True
    assert len(started) == 1
    assert started[0].kwargs["name"] == "cloud-article-snapshot-enqueue"


def test_app_cloud_runtime_support_command_enqueues_article_snapshot_with_config():
    owner = _support_owner()
    support = AppCloudRuntimeSupport(owner=owner)
    support.enqueue_article_snapshot = Mock()

    result = support.handle_command("cloud.enqueue_article_snapshot", {"config": {"tasks": [{"name": "A"}]}})

    assert result == {"ok": True, "message": "文章云端同步快照已触发"}
    support.enqueue_article_snapshot.assert_called_once_with({"tasks": [{"name": "A"}]})


def test_app_cloud_runtime_support_command_schedules_article_snapshot_retry():
    owner = _support_owner()
    support = AppCloudRuntimeSupport(owner=owner)
    support.schedule_article_snapshot_retry = Mock()

    result = support.handle_command("cloud.schedule_article_snapshot_retry", {"delay_seconds": 1.25})

    assert result == {"ok": True, "message": "文章云端同步重试已调度"}
    support.schedule_article_snapshot_retry.assert_called_once_with(delay_seconds=1.25)


def test_app_cloud_runtime_support_command_validates_session_with_force():
    owner = _support_owner()
    support = AppCloudRuntimeSupport(owner=owner)
    support.validate_cloud_session_if_needed = Mock()
    support.current_cloud_status = Mock(return_value={"ok": True, "cloud": {"loggedIn": True}})

    result = support.handle_command("cloud.validate_session", {"force": True})

    assert result == {"ok": True, "cloud": {"loggedIn": True}}
    support.validate_cloud_session_if_needed.assert_called_once_with(force=True)


def test_app_cloud_runtime_support_command_reports_unknown_command():
    owner = _support_owner()
    support = AppCloudRuntimeSupport(owner=owner)

    result = support.handle_command("cloud.nope", {"limit": 10})

    assert result["ok"] is False
    assert "cloud.nope" in result["message"]


def test_app_cloud_runtime_support_login_flushes_previous_account_outbox_before_switch():
    owner = _support_owner()
    owner._activate_current_account_space = Mock()
    owner._isolate_ordinary_cloud_account_config = Mock()
    owner._close_execution_runtime_for_viewer = Mock()
    previous_session = {
        "base_url": "https://api.surfacedlab.com",
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "user": {"id": 1, "workspace_id": 3, "role": "operator"},
    }
    next_token_pair = {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "token_type": "bearer",
        "user": {"id": 2, "workspace_id": 3, "role": "admin"},
    }
    next_session = {
        "base_url": "https://api.surfacedlab.com",
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "token_type": "bearer",
        "user": {"id": 2, "workspace_id": 3, "role": "admin"},
    }
    session_store = MagicMock()
    session_store.load.return_value = previous_session
    session_store.save_login.return_value = next_session
    outbox = MagicMock()
    bound_outbox = MagicMock()
    bound_outbox.stats.return_value = {"pending": 1, "failed": 0}
    outbox.bind_to_session.return_value = bound_outbox
    flush_outbox = Mock(return_value={"ok": True})
    support = AppCloudRuntimeSupport(
        owner=owner,
        session_store_factory=lambda: session_store,
        outbox_factory=lambda: outbox,
        flush_outbox_fn=flush_outbox,
        account_profile_dir_getter=lambda _session: Path("/tmp/new-profile"),
        account_space_ensurer=Mock(),
    )

    saved = support.login_cloud_account_space(
        base_url="https://api.surfacedlab.com",
        token_pair=next_token_pair,
        session_preview_builder=lambda *, base_url, token_pair: next_session,
        cloud_role_getter=lambda session: session["user"]["role"],
    )

    assert saved == next_session
    outbox.bind_to_session.assert_called_once_with(previous_session)
    flush_outbox.assert_called_once_with(outbox=bound_outbox)
    session_store.save_login.assert_called_once_with(base_url="https://api.surfacedlab.com", token_pair=next_token_pair)
    owner._activate_current_account_space.assert_called_once_with(copy_legacy=False)
    owner._isolate_ordinary_cloud_account_config.assert_called_once_with(next_session)
    owner._close_execution_runtime_for_viewer.assert_called_once()


def test_app_cloud_runtime_support_login_does_not_flush_same_account_outbox():
    owner = _support_owner()
    same_session = {
        "base_url": "https://api.surfacedlab.com",
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "user": {"id": 2, "workspace_id": 3, "role": "operator"},
    }
    token_pair = {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "user": {"id": 2, "workspace_id": 3, "role": "operator"},
    }
    session_store = MagicMock()
    session_store.load.return_value = same_session
    session_store.save_login.return_value = dict(same_session, access_token="new-access")
    outbox = MagicMock()
    account_space_ensurer = Mock()
    support = AppCloudRuntimeSupport(
        owner=owner,
        session_store_factory=lambda: session_store,
        outbox_factory=lambda: outbox,
        flush_outbox_fn=Mock(),
        account_profile_dir_getter=lambda _session: Path("/tmp/existing-profile"),
        account_space_ensurer=account_space_ensurer,
    )

    support.login_cloud_account_space(
        base_url="https://api.surfacedlab.com",
        token_pair=token_pair,
        session_preview_builder=lambda *, base_url, token_pair: same_session,
        cloud_role_getter=lambda session: session["user"]["role"],
    )

    outbox.bind_to_session.assert_not_called()
    account_space_ensurer.assert_called_once_with(same_session, copy_legacy=False)


def test_app_cloud_runtime_support_login_copies_legacy_for_new_admin_profile():
    owner = _support_owner()
    next_session = {
        "base_url": "https://api.surfacedlab.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 9, "workspace_id": 3, "role": "admin"},
    }
    session_store = MagicMock()
    session_store.load.return_value = {}
    session_store.save_login.return_value = next_session
    account_space_ensurer = Mock()
    support = AppCloudRuntimeSupport(
        owner=owner,
        session_store_factory=lambda: session_store,
        account_profile_dir_getter=lambda _session: Path("/tmp/profile-without-marker"),
        account_space_ensurer=account_space_ensurer,
    )

    support.login_cloud_account_space(
        base_url="https://api.surfacedlab.com",
        token_pair={"access_token": "access", "refresh_token": "refresh", "user": next_session["user"]},
        session_preview_builder=lambda *, base_url, token_pair: next_session,
        cloud_role_getter=lambda session: session["user"]["role"],
    )

    account_space_ensurer.assert_called_once_with(next_session, copy_legacy=True)
