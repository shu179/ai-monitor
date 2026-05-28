from __future__ import annotations

import threading
import tempfile
import hashlib
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import core.daily_task_state as daily_task_state_module
import core.history as history_module
from core.cloud_agent_status_store import CloudAgentStatusStore
from core.cloud_client import CloudClientError
from core.cloud_content_state_store import CloudContentStateStore
from core.cloud_object_cache import CloudObjectCache
from core.cloud_object_transfer_store import CloudObjectTransferStore
from core.cloud_state_delta_inbox import CloudStateDeltaInbox
from core.cloud_sync_runtime import (
    AppCloudRuntimeSupport,
    create_in_process_cloud_sync_command_client,
    create_local_cloud_sync_runtime,
)


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
            pull_state_delta=lambda _payload=None: {"ok": True},
            process_state_delta_inbox=lambda _payload=None: {"ok": True},
            retry_object_downloads=lambda _payload=None: {"ok": True},
            retry_object_uploads=lambda _payload=None: {"ok": True},
            object_upload_retry_status=lambda _payload=None: {"ok": True, "available": True, "retry_ready_count": 0},
            logger=lambda _message: None,
        )

    assert runtime.manager is manager
    assert runtime.platform_auto_sync is auto_sync
    assert auto_sync_cls.call_args.kwargs["upload_burst_interval_seconds"] == 2.5
    assert auto_sync_cls.call_args.kwargs["upload_burst_pending_threshold"] == 321
    assert callable(auto_sync_cls.call_args.kwargs["pull_state_delta"])
    assert callable(auto_sync_cls.call_args.kwargs["process_state_delta_inbox"])
    assert callable(auto_sync_cls.call_args.kwargs["retry_object_downloads"])
    assert callable(auto_sync_cls.call_args.kwargs["retry_object_uploads"])
    assert callable(auto_sync_cls.call_args.kwargs["object_upload_retry_status"])


def test_create_in_process_cloud_sync_command_client_wraps_handler():
    seen: list[tuple[str, dict[str, object] | None]] = []
    client = create_in_process_cloud_sync_command_client(
        lambda command, payload: seen.append((command, payload)) or {"ok": True}
    )

    result = client.send_command("cloud.status", {"force": True})

    assert result == {"ok": True}
    assert seen == [("cloud.status", {"force": True})]


def _support_owner() -> MagicMock:
    owner = MagicMock()
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
    auto_sync_status = {"running": True}
    outbox = MagicMock()
    outbox.stats.return_value = {"pending": 2, "failed": 1}
    support = AppCloudRuntimeSupport(
        owner=owner,
        outbox_factory=lambda: outbox,
        auto_sync_status_getter=lambda: auto_sync_status,
        current_account_config_path_getter=lambda: "/tmp/config.yaml",
    )
    support._cloud_status_validation_error = "stale token"

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
    outbox.stats.assert_called_once_with(include_retry=True)
    assert result["cloud"]["autoSync"] == auto_sync_status
    assert result["cloud"]["validationError"] == "stale token"
    assert result["cloud"]["localProfile"]["configPath"] == "/tmp/config.yaml"


def test_app_cloud_runtime_support_fetches_and_caches_cloud_capabilities():
    owner = _support_owner()
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.example.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 2, "workspace_id": 3},
    }

    class FakeClient:
        calls = 0

        def __init__(self, _base_url: str) -> None:
            pass

        def capabilities(self, token: str):
            assert token == "access"
            FakeClient.calls += 1
            return {
                "capabilities": ["sync-v2", "object-v1"],
                "limits": {
                    "object_storage_total_quota_bytes": 10 * 1024 * 1024 * 1024,
                    "object_storage_workspace_quota_bytes": 5 * 1024 * 1024 * 1024,
                    "object_storage_max_file_bytes": 512 * 1024 * 1024,
                    "object_storage_min_free_bytes": 8 * 1024 * 1024 * 1024,
                    "inline_blob_max_bytes": 32 * 1024,
                    "single_put_max_bytes": 5 * 1024 * 1024,
                    "multipart_part_bytes": 8 * 1024 * 1024,
                },
                "ttl_seconds": {"upload_presigned_url": 900},
                "object_storage_backend": "local",
            }

    support = AppCloudRuntimeSupport(
        owner=owner,
        session_store_factory=lambda: session_store,
        request_client_factory=FakeClient,
        capability_cache_ttl_seconds=300,
    )

    first = support.handle_command("cloud.capabilities")
    second = support.handle_command("cloud.capabilities")
    forced = support.handle_command("cloud.capabilities", {"force": True})

    assert first["ok"] is True
    assert first["capabilities"]["cached"] is False
    assert second["ok"] is True
    assert second["capabilities"]["cached"] is True
    assert forced["ok"] is True
    assert forced["capabilities"]["cached"] is False
    assert FakeClient.calls == 2
    limits = first["capabilities"]["limits"]
    assert limits["object_storage_total_quota_bytes"] == 10 * 1024 * 1024 * 1024
    assert limits["object_storage_workspace_quota_bytes"] == 5 * 1024 * 1024 * 1024
    assert limits["object_storage_max_file_bytes"] == 512 * 1024 * 1024
    assert limits["object_storage_min_free_bytes"] == 8 * 1024 * 1024 * 1024
    assert first["capabilities"]["object_storage_backend"] == "local"


def test_app_cloud_runtime_support_capability_cache_is_scoped_to_cloud_identity():
    owner = _support_owner()
    session_store = MagicMock()
    sessions = [
        {
            "base_url": "https://api.example.com",
            "access_token": "access-a",
            "refresh_token": "refresh",
            "user": {"id": 2, "workspace_id": 3},
        },
        {
            "base_url": "https://api.example.com",
            "access_token": "access-b",
            "refresh_token": "refresh",
            "user": {"id": 2, "workspace_id": 4},
        },
    ]
    session_store.load.side_effect = lambda: sessions[0]

    class FakeClient:
        tokens: list[str] = []

        def __init__(self, _base_url: str) -> None:
            pass

        def capabilities(self, token: str):
            self.tokens.append(token)
            return {"capabilities": ["sync-v2"], "limits": {}, "ttl_seconds": {}, "object_storage_backend": "local"}

    support = AppCloudRuntimeSupport(
        owner=owner,
        session_store_factory=lambda: session_store,
        request_client_factory=FakeClient,
    )

    assert support.handle_command("cloud.capabilities")["ok"] is True
    sessions[0] = sessions[1]
    assert support.handle_command("cloud.capabilities")["ok"] is True

    assert FakeClient.tokens == ["access-a", "access-b"]


def test_app_cloud_runtime_support_routes_state_delta_commands():
    owner = _support_owner()
    session_store = MagicMock()
    session_store.load.return_value = {"base_url": "https://api.example.com", "user": {"workspace_id": 1, "id": 2}}
    support = AppCloudRuntimeSupport(owner=owner, session_store_factory=lambda: session_store)

    with (
        patch("core.cloud_sync_runtime.pull_cloud_state_delta", return_value={"ok": True, "changes": 3}) as pull_delta,
        patch("core.cloud_sync_runtime.process_state_delta_inbox", return_value={"ok": True, "applied": 1}) as process_inbox,
        patch("core.cloud_sync_runtime.CloudAgentStatusStore") as agent_status_store_cls,
        patch("core.cloud_sync_runtime.CloudContentStateStore") as content_state_store_cls,
        patch("core.cloud_sync_runtime.CloudStateDeltaStore") as store_cls,
        patch("core.cloud_sync_runtime.CloudStateDeltaInbox") as inbox_cls,
    ):
        store_cls.return_value.diagnostics.return_value = {"cursors": {"tasks": 2}}
        inbox_cls.return_value.diagnostics.return_value = {"by_status": {"pending": 1}}
        agent_status_store_cls.return_value.diagnostics.return_value = {"total": 0}
        content_state_store_cls.return_value.diagnostics.return_value = {"answers_total": 0, "assets_total": 0}

        pull_result = support.handle_command("cloud.pull_state_delta", {"limit": 50, "max_pages": 2})
        process_result = support.handle_command("cloud.process_state_delta_inbox", {"limit": 20, "streams": ["tasks"]})
        diagnostics = support.handle_command("cloud.state_delta_diagnostics")

    assert pull_result == {"ok": True, "state_delta": {"ok": True, "changes": 3}, "message": ""}
    pull_delta.assert_called_once_with(limit=50, max_pages=2)
    assert process_result == {"ok": True, "state_delta_inbox": {"ok": True, "applied": 1}}
    process_kwargs = process_inbox.call_args.kwargs
    assert callable(process_kwargs["appliers"]["profile"])
    assert callable(process_kwargs["appliers"]["tasks"])
    assert callable(process_kwargs["appliers"]["runs"])
    assert callable(process_kwargs["appliers"]["articles"])
    assert callable(process_kwargs["appliers"]["references"])
    assert callable(process_kwargs["appliers"]["answers"])
    assert callable(process_kwargs["appliers"]["assets"])
    assert callable(process_kwargs["appliers"]["agent_status"])
    assert process_kwargs["limit"] == 20
    assert process_kwargs["streams"] == ["tasks"]
    assert process_kwargs["include_failed"] is False
    assert diagnostics == {
        "ok": True,
        "state_delta": {"cursors": {"tasks": 2}},
        "inbox": {"by_status": {"pending": 1}},
        "agent_status": {"total": 0},
        "content_state": {"answers_total": 0, "assets_total": 0},
    }
    store_cls.return_value.diagnostics.assert_called_once_with(session_store.load.return_value)
    inbox_cls.return_value.diagnostics.assert_called_once_with(failed_limit=10)
    agent_status_store_cls.return_value.diagnostics.assert_called_once_with(session_store.load.return_value)
    content_state_store_cls.return_value.diagnostics.assert_called_once_with(session_store.load.return_value)


def test_app_cloud_runtime_support_applies_profile_state_delta_to_session():
    owner = _support_owner()
    with tempfile.TemporaryDirectory() as tmp:
        inbox = CloudStateDeltaInbox(Path(tmp) / "inbox.sqlite3")
        inbox.record_changes(
            identity_key="https://api.example.com|3|2",
            changes=[
                {
                    "stream": "profile",
                    "seq": 1,
                    "kind": "profile.update",
                    "ref_id": "profile:2",
                    "entity": {
                        "type": "profile",
                        "user_id": 2,
                        "workspace_id": 3,
                        "username": "operator@example.com",
                        "role": "operate",
                        "display_name": "新名字",
                        "email": "operator@example.com",
                        "email_verified": True,
                        "avatar": "https://cdn.example.com/a.png",
                        "birthday": "2000-01-02",
                        "hire_date": "2026-05-01",
                        "view_all_tasks": False,
                        "enabled": True,
                        "updated_at": "2026-05-28T00:00:00Z",
                    },
                }
            ],
        )
        session_store = MagicMock()
        session_store.load.return_value = {
            "base_url": "https://api.example.com",
            "access_token": "access",
            "refresh_token": "refresh",
            "user": {"id": 2, "workspace_id": 3, "username": "old", "role": "operate"},
        }
        support = AppCloudRuntimeSupport(owner=owner, session_store_factory=lambda: session_store)

        with patch("core.cloud_sync_runtime.CloudStateDeltaInbox", return_value=inbox):
            result = support.process_cloud_state_delta_inbox({"limit": 10, "streams": ["profile"]})

        assert result["ok"] is True
        assert result["state_delta_inbox"]["applied"] == 1
        saved_user = session_store.update_user.call_args.args[0]
        assert saved_user["display_name"] == "新名字"
        assert saved_user["email_verified"] is True
        assert saved_user["workspace_id"] == 3
        assert inbox.diagnostics()["by_status"] == {"applied": 1}


def test_app_cloud_runtime_support_rejects_profile_delta_for_other_user():
    owner = _support_owner()
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.example.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 2, "workspace_id": 3, "username": "old", "role": "operate"},
    }
    support = AppCloudRuntimeSupport(owner=owner, session_store_factory=lambda: session_store)

    with tempfile.TemporaryDirectory() as tmp:
        inbox = CloudStateDeltaInbox(Path(tmp) / "inbox.sqlite3")
        inbox.record_changes(
            identity_key="https://api.example.com|3|2",
            changes=[
                {
                    "stream": "profile",
                    "seq": 1,
                    "kind": "profile.update",
                    "ref_id": "profile:99",
                    "entity": {"type": "profile", "user_id": 99, "workspace_id": 3},
                }
            ],
        )

        with patch("core.cloud_sync_runtime.CloudStateDeltaInbox", return_value=inbox):
            result = support.process_cloud_state_delta_inbox({"limit": 10, "streams": ["profile"]})
            diagnostics = inbox.diagnostics()

    assert result["ok"] is False
    assert result["state_delta_inbox"]["failed"] == 1
    assert "不匹配" in diagnostics["failed"][0]["last_error"]
    session_store.update_user.assert_not_called()


def test_app_cloud_runtime_support_applies_task_state_delta_to_config():
    owner = _support_owner()
    config = {"tasks": []}
    owner.load_config.return_value = config
    owner.save_config = Mock()
    owner._invalidate_tasks_full_cache = Mock()
    owner._refresh_monitoring_runtime = Mock()
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.example.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 2, "workspace_id": 3, "role": "operator"},
    }
    support = AppCloudRuntimeSupport(owner=owner, session_store_factory=lambda: session_store)

    with tempfile.TemporaryDirectory() as tmp:
        inbox = CloudStateDeltaInbox(Path(tmp) / "inbox.sqlite3")
        inbox.record_changes(
            identity_key="https://api.example.com|3|2",
            changes=[
                {
                    "stream": "tasks",
                    "seq": 1,
                    "kind": "task.updated",
                    "ref_id": "42",
                    "entity": {
                        "type": "task",
                        "id": 42,
                        "workspace_id": 3,
                        "task_key": "brand-demo",
                        "name": "云端品牌",
                        "brand": "云端品牌",
                        "config_json": {"keywords": ["云端关键词"], "platforms": ["Kimi"]},
                        "config_version": 2,
                        "enabled": True,
                        "access_level": "operate",
                    },
                }
            ],
        )

        with patch("core.cloud_sync_runtime.CloudStateDeltaInbox", return_value=inbox):
            result = support.process_cloud_state_delta_inbox({"limit": 10, "streams": ["tasks"]})

    assert result["ok"] is True
    assert result["state_delta_inbox"]["applied"] == 1
    saved_config = owner.save_config.call_args.args[0]
    task = saved_config["tasks"][0]
    assert task["cloud_task_id"] == 42
    assert task["cloud_config_version"] == 2
    assert task["keywords"][0]["keyword"] == "云端关键词"
    owner._invalidate_tasks_full_cache.assert_called_once()
    owner._refresh_monitoring_runtime.assert_called_once_with(restart_scheduler=True)


def test_app_cloud_runtime_support_deletes_task_state_delta_via_existing_merge_rules():
    owner = _support_owner()
    config = {
        "tasks": [
            {
                "task_id": "cloud_42",
                "name": "旧云端品牌",
                "brand": "旧云端品牌",
                "enabled": True,
                "cloud_task_id": 42,
                "cloud_workspace_id": 3,
                "cloud_base_url": "https://api.example.com",
                "cloud_access_level": "operate",
            }
        ]
    }
    owner.load_config.return_value = config
    owner.save_config = Mock()
    owner._invalidate_tasks_full_cache = Mock()
    owner._refresh_monitoring_runtime = Mock()
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.example.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 2, "workspace_id": 3, "role": "operator"},
    }
    support = AppCloudRuntimeSupport(owner=owner, session_store_factory=lambda: session_store)

    with tempfile.TemporaryDirectory() as tmp:
        inbox = CloudStateDeltaInbox(Path(tmp) / "inbox.sqlite3")
        inbox.record_changes(
            identity_key="https://api.example.com|3|2",
            changes=[
                {
                    "stream": "tasks",
                    "seq": 2,
                    "kind": "task.deleted",
                    "ref_id": "42",
                    "entity": {
                        "type": "task",
                        "id": 42,
                        "workspace_id": 3,
                        "task_key": "brand-demo",
                        "name": "旧云端品牌",
                        "brand": "旧云端品牌",
                        "config_json": {"keywords": ["旧云端品牌"], "platforms": ["kimi"]},
                        "config_version": 2,
                        "enabled": True,
                        "access_level": "operate",
                        "deleted_at": "2026-05-28T00:00:00+08:00",
                        "delete_expires_at": "2026-06-04T00:00:00+08:00",
                    },
                }
            ],
        )

        with patch("core.cloud_sync_runtime.CloudStateDeltaInbox", return_value=inbox):
            result = support.process_cloud_state_delta_inbox({"limit": 10, "streams": ["tasks"]})
            diagnostics = inbox.diagnostics()

    assert result["ok"] is True
    saved_config = owner.save_config.call_args.args[0]
    assert saved_config["tasks"] == []
    assert saved_config["deleted_tasks"][0]["cloud_task_id"] == 42
    assert diagnostics["by_status"] == {"applied": 1}


def test_app_cloud_runtime_support_rejects_task_state_delta_for_other_workspace():
    owner = _support_owner()
    owner.load_config.return_value = {"tasks": []}
    owner.save_config = Mock()
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.example.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 2, "workspace_id": 3, "role": "operator"},
    }
    support = AppCloudRuntimeSupport(owner=owner, session_store_factory=lambda: session_store)

    with tempfile.TemporaryDirectory() as tmp:
        inbox = CloudStateDeltaInbox(Path(tmp) / "inbox.sqlite3")
        inbox.record_changes(
            identity_key="https://api.example.com|3|2",
            changes=[
                {
                    "stream": "tasks",
                    "seq": 1,
                    "kind": "task.updated",
                    "ref_id": "42",
                    "entity": {"type": "task", "id": 42, "workspace_id": 99},
                }
            ],
        )

        with patch("core.cloud_sync_runtime.CloudStateDeltaInbox", return_value=inbox):
            result = support.process_cloud_state_delta_inbox({"limit": 10, "streams": ["tasks"]})
            diagnostics = inbox.diagnostics()

    assert result["ok"] is False
    assert result["state_delta_inbox"]["failed"] == 1
    assert "工作区不匹配" in diagnostics["failed"][0]["last_error"]
    owner.save_config.assert_not_called()


def test_app_cloud_runtime_support_applies_run_record_state_delta_to_history():
    owner = _support_owner()
    config = {
        "tasks": [
            {
                "task_id": "cloud_9",
                "name": "趋势品牌",
                "brand": "趋势品牌",
                "cloud_task_id": 9,
                "cloud_workspace_id": 3,
                "cloud_base_url": "https://api.example.com",
                "keywords": [{"keyword": "趋势品牌", "brand": "趋势品牌", "platforms": ["doubao"]}],
            }
        ]
    }
    owner.load_config.return_value = config
    owner.save_config = Mock()
    owner._invalidate_tasks_full_cache = Mock()
    owner._refresh_monitoring_runtime = Mock()
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.example.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 2, "workspace_id": 3, "role": "operator"},
    }
    support = AppCloudRuntimeSupport(owner=owner, session_store_factory=lambda: session_store)

    with tempfile.TemporaryDirectory() as tmp:
        original_history_dir = history_module.HISTORY_DIR
        original_state_path = daily_task_state_module.STATE_PATH
        history_module.HISTORY_DIR = Path(tmp) / "logs" / "history"
        daily_task_state_module.STATE_PATH = Path(tmp) / "user_data" / "daily_task_status.json"
        try:
            inbox = CloudStateDeltaInbox(Path(tmp) / "inbox.sqlite3")
            inbox.record_changes(
                identity_key="https://api.example.com|3|2",
                changes=[
                    {
                        "stream": "runs",
                        "seq": 1,
                        "kind": "run_record.upsert",
                        "ref_id": "12",
                        "entity": {
                            "type": "run_record",
                            "id": 12,
                            "workspace_id": 3,
                            "task_id": 9,
                            "platform": "doubao",
                            "keyword": "趋势品牌",
                            "brand": "趋势品牌",
                            "mode": "browser",
                            "result_json": {"rank": 1, "success": True},
                            "idempotency_key": "run:cloud-record-12",
                            "executed_at": "2026-05-03T08:00:00Z",
                        },
                    }
                ],
            )

            with patch("core.cloud_sync_runtime.CloudStateDeltaInbox", return_value=inbox):
                result = support.process_cloud_state_delta_inbox({"limit": 10, "streams": ["runs"]})

            records = history_module.get_records("趋势品牌", task_id="cloud_9")
            status = daily_task_state_module.get_task_day_status(
                owner.save_config.call_args.args[0]["tasks"][0],
                target_date=date(2026, 5, 3),
            )
        finally:
            history_module.HISTORY_DIR = original_history_dir
            daily_task_state_module.STATE_PATH = original_state_path

    assert result["ok"] is True
    assert result["state_delta_inbox"]["applied"] == 1
    saved_config = owner.save_config.call_args.args[0]
    assert saved_config["tasks"][0]["cloud_last_run_record_synced_id"] == 12
    assert len(records) == 1
    assert records[0]["id"] == "cloud:run:cloud-record-12"
    assert status["brand_status"] == "success"
    owner._invalidate_tasks_full_cache.assert_called_once()
    owner._refresh_monitoring_runtime.assert_called_once_with(restart_scheduler=True)


def test_app_cloud_runtime_support_rejects_run_state_delta_for_other_workspace():
    owner = _support_owner()
    owner.load_config.return_value = {"tasks": []}
    owner.save_config = Mock()
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.example.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 2, "workspace_id": 3, "role": "operator"},
    }
    support = AppCloudRuntimeSupport(owner=owner, session_store_factory=lambda: session_store)

    with tempfile.TemporaryDirectory() as tmp:
        inbox = CloudStateDeltaInbox(Path(tmp) / "inbox.sqlite3")
        inbox.record_changes(
            identity_key="https://api.example.com|3|2",
            changes=[
                {
                    "stream": "runs",
                    "seq": 1,
                    "kind": "run_record.upsert",
                    "ref_id": "12",
                    "entity": {"type": "run_record", "id": 12, "workspace_id": 99, "task_id": 9},
                }
            ],
        )

        with patch("core.cloud_sync_runtime.CloudStateDeltaInbox", return_value=inbox):
            result = support.process_cloud_state_delta_inbox({"limit": 10, "streams": ["runs"]})
            diagnostics = inbox.diagnostics()

    assert result["ok"] is False
    assert result["state_delta_inbox"]["failed"] == 1
    assert "工作区不匹配" in diagnostics["failed"][0]["last_error"]
    owner.save_config.assert_not_called()


def test_app_cloud_runtime_support_applies_article_state_delta_to_store():
    owner = _support_owner()
    owner.load_config.return_value = {"tasks": [{"name": "即搜AI", "brand": "即搜AI", "cloud_task_id": 42}]}
    owner._invalidate_article_cache = Mock()
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.example.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 2, "workspace_id": 3, "role": "operator"},
    }
    support = AppCloudRuntimeSupport(owner=owner, session_store_factory=lambda: session_store)

    with tempfile.TemporaryDirectory() as tmp:
        import core.article_store as article_store

        original_paths = {
            "ARTICLES_FILE": article_store.ARTICLES_FILE,
            "DOMAIN_OVERRIDES_FILE": article_store.DOMAIN_OVERRIDES_FILE,
            "DOMAIN_MEDIA_NAMES_FILE": article_store.DOMAIN_MEDIA_NAMES_FILE,
            "EXCLUDED_ARTICLE_URLS_FILE": article_store.EXCLUDED_ARTICLE_URLS_FILE,
        }
        root = Path(tmp)
        article_store.ARTICLES_FILE = root / "logs" / "articles.json"
        article_store.DOMAIN_OVERRIDES_FILE = root / "logs" / "domain_overrides.json"
        article_store.DOMAIN_MEDIA_NAMES_FILE = root / "logs" / "domain_media_names.json"
        article_store.EXCLUDED_ARTICLE_URLS_FILE = root / "logs" / "excluded_article_urls.json"
        article_store.ARTICLES_FILE.parent.mkdir(parents=True, exist_ok=True)
        article_store.ARTICLES_FILE.write_text("[]", encoding="utf-8")
        try:
            inbox = CloudStateDeltaInbox(Path(tmp) / "inbox.sqlite3")
            inbox.record_changes(
                identity_key="https://api.example.com|3|2",
                changes=[
                    {
                        "stream": "articles",
                        "seq": 1,
                        "kind": "article.upsert",
                        "ref_id": "21",
                        "entity": {
                            "type": "article",
                            "id": 21,
                            "workspace_id": 3,
                            "canonical_url": "https://example.com/a",
                            "url_hash": "hash-a",
                            "title": "即搜AI 入选榜单",
                            "source": "武汉观察",
                            "media_type": "selfmedia",
                            "payload_json": {"excerpt": "摘要"},
                            "task_links": [{"task_id": 42, "reason_json": {"reasons": ["标题匹配"]}}],
                        },
                    }
                ],
            )

            with patch("core.cloud_sync_runtime.CloudStateDeltaInbox", return_value=inbox):
                result = support.process_cloud_state_delta_inbox({"limit": 10, "streams": ["articles"]})

            articles = article_store.get_articles()
        finally:
            article_store.ARTICLES_FILE = original_paths["ARTICLES_FILE"]
            article_store.DOMAIN_OVERRIDES_FILE = original_paths["DOMAIN_OVERRIDES_FILE"]
            article_store.DOMAIN_MEDIA_NAMES_FILE = original_paths["DOMAIN_MEDIA_NAMES_FILE"]
            article_store.EXCLUDED_ARTICLE_URLS_FILE = original_paths["EXCLUDED_ARTICLE_URLS_FILE"]

    assert result["ok"] is True
    assert result["state_delta_inbox"]["applied"] == 1
    assert articles[0]["cloud_article_id"] == 21
    assert articles[0]["matched_tasks"] == ["即搜AI"]
    owner._invalidate_article_cache.assert_called_once()


def test_app_cloud_runtime_support_applies_reference_state_delta_to_store():
    owner = _support_owner()
    owner.load_config.return_value = {"tasks": [{"name": "即搜AI", "brand": "即搜AI", "cloud_task_id": 42}]}
    owner._invalidate_article_cache = Mock()
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.example.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 2, "workspace_id": 3, "role": "operator"},
    }
    support = AppCloudRuntimeSupport(owner=owner, session_store_factory=lambda: session_store)

    with tempfile.TemporaryDirectory() as tmp:
        import core.article_store as article_store

        original_paths = {
            "ARTICLES_FILE": article_store.ARTICLES_FILE,
            "DOMAIN_OVERRIDES_FILE": article_store.DOMAIN_OVERRIDES_FILE,
            "DOMAIN_MEDIA_NAMES_FILE": article_store.DOMAIN_MEDIA_NAMES_FILE,
            "EXCLUDED_ARTICLE_URLS_FILE": article_store.EXCLUDED_ARTICLE_URLS_FILE,
        }
        root = Path(tmp)
        article_store.ARTICLES_FILE = root / "logs" / "articles.json"
        article_store.DOMAIN_OVERRIDES_FILE = root / "logs" / "domain_overrides.json"
        article_store.DOMAIN_MEDIA_NAMES_FILE = root / "logs" / "domain_media_names.json"
        article_store.EXCLUDED_ARTICLE_URLS_FILE = root / "logs" / "excluded_article_urls.json"
        article_store.ARTICLES_FILE.parent.mkdir(parents=True, exist_ok=True)
        article_store.ARTICLES_FILE.write_text("[]", encoding="utf-8")
        try:
            article_store.import_article_store_bundle(
                {
                    "articles": [
                        {
                            "id": "cloud-21",
                            "url": "https://example.com/a",
                            "title": "即搜AI 入选榜单",
                            "matched_tasks": ["即搜AI"],
                            "match_reasons": {"即搜AI": ["标题匹配"]},
                            "cloud_article_id": 21,
                            "cloud_task_ids": [42],
                        }
                    ]
                },
                mode="replace",
            )
            inbox = CloudStateDeltaInbox(Path(tmp) / "inbox.sqlite3")
            inbox.record_changes(
                identity_key="https://api.example.com|3|2",
                changes=[
                    {
                        "stream": "references",
                        "seq": 1,
                        "kind": "article.reference",
                        "ref_id": "31",
                        "entity": {
                            "type": "article_reference",
                            "id": 31,
                            "workspace_id": 3,
                            "task_id": 42,
                            "article_id": 21,
                            "normalized_url": "https://example.com/a",
                            "platform": "doubao",
                            "source_record_key": "run:abc",
                            "idempotency_key": "ref-abc",
                            "created_at": "2026-05-06T08:10:00+00:00",
                        },
                    }
                ],
            )

            with patch("core.cloud_sync_runtime.CloudStateDeltaInbox", return_value=inbox):
                result = support.process_cloud_state_delta_inbox({"limit": 10, "streams": ["references"]})

            article = article_store.get_articles()[0]
        finally:
            article_store.ARTICLES_FILE = original_paths["ARTICLES_FILE"]
            article_store.DOMAIN_OVERRIDES_FILE = original_paths["DOMAIN_OVERRIDES_FILE"]
            article_store.DOMAIN_MEDIA_NAMES_FILE = original_paths["DOMAIN_MEDIA_NAMES_FILE"]
            article_store.EXCLUDED_ARTICLE_URLS_FILE = original_paths["EXCLUDED_ARTICLE_URLS_FILE"]

    assert result["ok"] is True
    assert result["state_delta_inbox"]["applied"] == 1
    assert article["referenced_tasks"] == ["即搜AI"]
    assert article["reference_hits"]["即搜AI"]["events"][0]["event_id"] == "ref-abc"
    owner._invalidate_article_cache.assert_called_once()


def test_app_cloud_runtime_support_applies_agent_status_state_delta_to_store():
    owner = _support_owner()
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.example.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 2, "workspace_id": 3, "role": "operator"},
    }
    support = AppCloudRuntimeSupport(owner=owner, session_store_factory=lambda: session_store)

    with tempfile.TemporaryDirectory() as tmp:
        inbox = CloudStateDeltaInbox(Path(tmp) / "inbox.sqlite3")
        agent_store = CloudAgentStatusStore(Path(tmp) / "agent_status.sqlite3")
        inbox.record_changes(
            identity_key="https://api.example.com|3|2",
            changes=[
                {
                    "stream": "agent_status",
                    "seq": 1,
                    "kind": "agent.command.status",
                    "ref_id": "command-001",
                    "entity": {
                        "type": "agent_command_status",
                        "id": "command-001",
                        "workspace_id": 3,
                        "target_device_id": "mac-1",
                        "target_role": "desktop",
                        "status": "completed",
                        "idempotency_key": "agent-key-1",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "result_chunks": [
                            {
                                "command_id": "command-001",
                                "seq": 0,
                                "payload_json": {"text": "done"},
                                "is_final": True,
                            }
                        ],
                    },
                }
            ],
        )

        with (
            patch("core.cloud_sync_runtime.CloudStateDeltaInbox", return_value=inbox),
            patch("core.cloud_sync_runtime.CloudAgentStatusStore", return_value=agent_store),
        ):
            result = support.process_cloud_state_delta_inbox({"limit": 10, "streams": ["agent_status"]})

        diagnostics = agent_store.diagnostics(session_store.load.return_value)

    assert result["ok"] is True
    assert result["state_delta_inbox"]["applied"] == 1
    assert diagnostics["total"] == 1
    assert diagnostics["by_status"] == {"completed": 1}
    assert diagnostics["newest"][0]["chunk_count"] == 1


def test_app_cloud_runtime_support_rejects_agent_status_state_delta_for_other_workspace():
    owner = _support_owner()
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.example.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 2, "workspace_id": 3, "role": "operator"},
    }
    support = AppCloudRuntimeSupport(owner=owner, session_store_factory=lambda: session_store)

    with tempfile.TemporaryDirectory() as tmp:
        inbox = CloudStateDeltaInbox(Path(tmp) / "inbox.sqlite3")
        agent_store = CloudAgentStatusStore(Path(tmp) / "agent_status.sqlite3")
        inbox.record_changes(
            identity_key="https://api.example.com|3|2",
            changes=[
                {
                    "stream": "agent_status",
                    "seq": 1,
                    "kind": "agent.command.status",
                    "ref_id": "command-001",
                    "entity": {
                        "type": "agent_command_status",
                        "id": "command-001",
                        "workspace_id": 99,
                        "status": "completed",
                    },
                }
            ],
        )

        with (
            patch("core.cloud_sync_runtime.CloudStateDeltaInbox", return_value=inbox),
            patch("core.cloud_sync_runtime.CloudAgentStatusStore", return_value=agent_store),
        ):
            result = support.process_cloud_state_delta_inbox({"limit": 10, "streams": ["agent_status"]})
            diagnostics = inbox.diagnostics()

    assert result["ok"] is False
    assert result["state_delta_inbox"]["failed"] == 1
    assert "工作区不匹配" in diagnostics["failed"][0]["last_error"]
    assert agent_store.diagnostics(session_store.load.return_value)["total"] == 0


def test_app_cloud_runtime_support_applies_answer_state_delta_to_content_store():
    owner = _support_owner()
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.example.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 2, "workspace_id": 3, "role": "operator"},
    }
    data = b"answer object body"
    sha256 = hashlib.sha256(data).hexdigest()

    class FakeClient:
        def __init__(self, _base_url: str) -> None:
            pass

        def create_object_download(self, _token: str, object_id: str, *, trace_id: str = ""):
            assert object_id == "obj-1"
            return {
                "object_id": object_id,
                "download_url": "/api/v2/objects/obj-1/content",
                "size_bytes": len(data),
                "content_type": "text/plain",
            }

        def iter_object_content(self, _token: str, _download_url: str, *, trace_id: str = ""):
            return iter([data])

    with tempfile.TemporaryDirectory() as tmp:
        inbox = CloudStateDeltaInbox(Path(tmp) / "inbox.sqlite3")
        content_store = CloudContentStateStore(Path(tmp) / "content.sqlite3")
        object_cache = CloudObjectCache(Path(tmp) / "object_cache")
        support = AppCloudRuntimeSupport(
            owner=owner,
            session_store_factory=lambda: session_store,
            request_client_factory=FakeClient,
            object_cache_factory=lambda: object_cache,
        )
        inbox.record_changes(
            identity_key="https://api.example.com|3|2",
            object_refs=[
                {
                    "object_id": "obj-1",
                    "sha256": sha256,
                    "size_bytes": len(data),
                    "storage_size_bytes": len(data),
                    "content_type": "text/plain",
                    "compression": "none",
                    "storage_key": "3/aa/bb/object",
                }
            ],
            changes=[
                {
                    "stream": "answers",
                    "seq": 1,
                    "kind": "answer.upsert",
                    "ref_id": "answer-001",
                    "entity": {
                        "type": "answer",
                        "id": "answer-001",
                        "workspace_id": 3,
                        "run_record_id": "run-9",
                        "platform": "doubao",
                        "content_ref": {"kind": "object", "object_id": "obj-1"},
                    },
                }
            ],
        )

        with (
            patch("core.cloud_sync_runtime.CloudStateDeltaInbox", return_value=inbox),
            patch("core.cloud_sync_runtime.CloudContentStateStore", return_value=content_store),
        ):
            result = support.process_cloud_state_delta_inbox({"limit": 10, "streams": ["answers"]})

        diagnostics = content_store.diagnostics(session_store.load.return_value)
        cached_object = object_cache.cached_object({"sha256": sha256, "size_bytes": len(data)})

    assert result["ok"] is True
    assert result["state_delta_inbox"]["applied"] == 1
    assert result["state_delta_inbox"]["object_cache"]["cached"] == 1
    assert result["state_delta_inbox"]["object_cache"]["downloaded"] == 1
    assert diagnostics["answers_total"] == 1
    assert diagnostics["newest_answers"][0]["object_ref_count"] == 1
    assert cached_object["valid"] is True


def test_app_cloud_runtime_support_does_not_fail_inbox_when_object_cache_download_fails():
    owner = _support_owner()
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.example.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 2, "workspace_id": 3, "role": "operator"},
    }

    class FailingClient:
        def __init__(self, _base_url: str) -> None:
            pass

        def create_object_download(self, _token: str, _object_id: str, *, trace_id: str = ""):
            raise CloudClientError("temporary object failure", status_code=503)

    with tempfile.TemporaryDirectory() as tmp:
        inbox = CloudStateDeltaInbox(Path(tmp) / "inbox.sqlite3")
        content_store = CloudContentStateStore(Path(tmp) / "content.sqlite3")
        object_cache = CloudObjectCache(Path(tmp) / "object_cache")
        support = AppCloudRuntimeSupport(
            owner=owner,
            session_store_factory=lambda: session_store,
            request_client_factory=FailingClient,
            object_cache_factory=lambda: object_cache,
        )
        inbox.record_changes(
            identity_key="https://api.example.com|3|2",
            object_refs=[
                {
                    "object_id": "obj-1",
                    "sha256": "b" * 64,
                    "size_bytes": 1200,
                    "content_type": "text/plain",
                }
            ],
            changes=[
                {
                    "stream": "answers",
                    "seq": 1,
                    "kind": "answer.upsert",
                    "ref_id": "answer-001",
                    "entity": {
                        "type": "answer",
                        "id": "answer-001",
                        "workspace_id": 3,
                        "content_ref": {"kind": "object", "object_id": "obj-1"},
                    },
                }
            ],
        )

        with (
            patch("core.cloud_sync_runtime.CloudStateDeltaInbox", return_value=inbox),
            patch("core.cloud_sync_runtime.CloudContentStateStore", return_value=content_store),
        ):
            result = support.process_cloud_state_delta_inbox({"limit": 10, "streams": ["answers"]})

    assert result["ok"] is True
    assert result["state_delta_inbox"]["applied"] == 1
    assert result["state_delta_inbox"]["failed"] == 0
    assert result["state_delta_inbox"]["object_cache"]["failed"] == 1
    assert "temporary object failure" in result["state_delta_inbox"]["object_cache"]["failures"][0]["message"]


def test_app_cloud_runtime_support_applies_asset_state_delta_to_content_store():
    owner = _support_owner()
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.example.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 2, "workspace_id": 3, "role": "operator"},
    }
    support = AppCloudRuntimeSupport(owner=owner, session_store_factory=lambda: session_store)

    with tempfile.TemporaryDirectory() as tmp:
        inbox = CloudStateDeltaInbox(Path(tmp) / "inbox.sqlite3")
        content_store = CloudContentStateStore(Path(tmp) / "content.sqlite3")
        inbox.record_changes(
            identity_key="https://api.example.com|3|2",
            changes=[
                {
                    "stream": "assets",
                    "seq": 1,
                    "kind": "asset.upsert",
                    "ref_id": "obj-1",
                    "entity": {
                        "type": "asset",
                        "workspace_id": 3,
                        "object_id": "obj-1",
                        "sha256": "a" * 64,
                        "size_bytes": 1200,
                        "storage_size_bytes": 450,
                        "content_type": "image/png",
                        "status": "active",
                    },
                }
            ],
        )

        with (
            patch("core.cloud_sync_runtime.CloudStateDeltaInbox", return_value=inbox),
            patch("core.cloud_sync_runtime.CloudContentStateStore", return_value=content_store),
        ):
            result = support.process_cloud_state_delta_inbox({"limit": 10, "streams": ["assets"]})

        diagnostics = content_store.diagnostics(session_store.load.return_value)

    assert result["ok"] is True
    assert result["state_delta_inbox"]["applied"] == 1
    assert diagnostics["assets_total"] == 1
    assert diagnostics["newest_assets"][0]["object_id"] == "obj-1"


def test_app_cloud_runtime_support_rejects_asset_state_delta_for_other_workspace():
    owner = _support_owner()
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.example.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 2, "workspace_id": 3, "role": "operator"},
    }
    support = AppCloudRuntimeSupport(owner=owner, session_store_factory=lambda: session_store)

    with tempfile.TemporaryDirectory() as tmp:
        inbox = CloudStateDeltaInbox(Path(tmp) / "inbox.sqlite3")
        content_store = CloudContentStateStore(Path(tmp) / "content.sqlite3")
        inbox.record_changes(
            identity_key="https://api.example.com|3|2",
            changes=[
                {
                    "stream": "assets",
                    "seq": 1,
                    "kind": "asset.upsert",
                    "ref_id": "obj-1",
                    "entity": {"type": "asset", "workspace_id": 99, "object_id": "obj-1"},
                }
            ],
        )

        with (
            patch("core.cloud_sync_runtime.CloudStateDeltaInbox", return_value=inbox),
            patch("core.cloud_sync_runtime.CloudContentStateStore", return_value=content_store),
        ):
            result = support.process_cloud_state_delta_inbox({"limit": 10, "streams": ["assets"]})
            diagnostics = inbox.diagnostics()

    assert result["ok"] is False
    assert result["state_delta_inbox"]["failed"] == 1
    assert "工作区不匹配" in diagnostics["failed"][0]["last_error"]
    assert content_store.diagnostics(session_store.load.return_value)["assets_total"] == 0


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
    assert support._article_cloud_enqueue_retry_thread is not None
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


def test_app_cloud_runtime_support_command_returns_outbox_diagnostics():
    owner = _support_owner()
    session_store = MagicMock()
    session_store.load.return_value = {"base_url": "https://api.surfacedlab.com", "user": {"id": 3}}
    bound_outbox = MagicMock()
    bound_outbox.diagnostics.return_value = {
        "path": "/tmp/outbox.json",
        "stats": {"failed": 1, "upload_ready": 0},
        "failed": [{"idempotency_key": "evt-1"}],
        "dead_letter": [],
    }
    outbox = MagicMock()
    outbox.bind_to_session.return_value = bound_outbox
    support = AppCloudRuntimeSupport(
        owner=owner,
        session_store_factory=lambda: session_store,
        outbox_factory=lambda: outbox,
    )

    result = support.handle_command("cloud.outbox_diagnostics", {"failedLimit": "7"})

    assert result["ok"] is True
    assert result["outbox"]["stats"]["failed"] == 1
    outbox.bind_to_session.assert_called_once_with(session_store.load.return_value)
    bound_outbox.diagnostics.assert_called_once_with(failed_limit=7)


def test_app_cloud_runtime_support_command_returns_sync_health_snapshot():
    owner = _support_owner()
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.surfacedlab.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 3, "workspace_id": 4},
    }
    bound_outbox = MagicMock()
    bound_outbox.diagnostics.return_value = {
        "path": "/tmp/outbox.json",
        "stats": {
            "pending": 2,
            "failed": 1,
            "dead_letter": 0,
            "upload_ready": 3,
            "next_retry_after_seconds": 12,
        },
        "failed": [],
        "dead_letter": [],
    }
    outbox = MagicMock()
    outbox.bind_to_session.return_value = bound_outbox
    auto_sync_status = {
        "running": True,
        "event_stream_connected": True,
        "upload_backpressure_until": "2026-05-28T10:00:00",
        "upload_backpressure_retry_after_seconds": 8.5,
        "upload_backpressure_queue_depth_hint": 400,
        "upload_backpressure_bucket": "sync_metadata",
        "object_download_retry_backpressure_until": "2026-05-28T10:00:30",
        "object_download_retry_backpressure_retry_after_seconds": 6.0,
        "object_download_retry_backpressure_queue_depth_hint": 17,
        "object_download_retry_backpressure_bucket": "object_download",
        "object_upload_retry_backpressure_until": "2026-05-28T10:01:00",
        "object_upload_retry_backpressure_retry_after_seconds": 15.0,
        "object_upload_retry_backpressure_queue_depth_hint": 25,
        "object_upload_retry_backpressure_bucket": "object_upload",
        "object_upload_retry_ready_count": 0,
        "object_upload_retry_waiting_count": 1,
        "object_upload_retry_wait_reason": "waiting_retry_backoff",
        "next_object_upload_retry_after_seconds": 7,
        "outbox_wait_reason": "waiting_retry_backoff",
        "next_upload_attempt_after_seconds": 12,
        "last_upload_at": "2026-05-28T09:59:00",
        "last_pull_at": "2026-05-28T09:59:03",
        "last_state_delta_at": "2026-05-28T09:59:04",
    }
    class FakeClient:
        def __init__(self, _base_url: str) -> None:
            pass

        def capabilities(self, token: str):
            assert token == "access"
            return {
                "capabilities": ["sync-v2", "batch-v2", "object-v1", "state-delta-v1"],
                "limits": {
                    "object_storage_total_quota_bytes": 10 * 1024 * 1024 * 1024,
                    "object_storage_workspace_quota_bytes": 5 * 1024 * 1024 * 1024,
                    "object_storage_max_file_bytes": 512 * 1024 * 1024,
                    "object_storage_min_free_bytes": 8 * 1024 * 1024 * 1024,
                    "inline_blob_max_bytes": 32 * 1024,
                    "single_put_max_bytes": 5 * 1024 * 1024,
                    "multipart_part_bytes": 8 * 1024 * 1024,
                },
                "ttl_seconds": {"multipart_upload_session": 24 * 60 * 60},
                "object_storage_backend": "local",
            }

    support = AppCloudRuntimeSupport(
        owner=owner,
        session_store_factory=lambda: session_store,
        outbox_factory=lambda: outbox,
        auto_sync_status_getter=lambda: auto_sync_status,
        request_client_factory=FakeClient,
        object_cache_factory=lambda: MagicMock(diagnostics=lambda: {"objects": 4, "bytes": 250, "max_cache_bytes": 1000}),
        object_transfer_store_factory=lambda: MagicMock(
            diagnostics=lambda failed_limit=10: {
                "total": 5,
                "retryable_count": 2,
                "by_status": {"failed": 2, "running": 2, "completed": 1},
                "by_direction": {
                    "upload": {"failed": 1, "running": 1},
                    "download": {"failed": 1, "running": 1},
                },
            }
        ),
    )
    support._run_cloud_api_request = Mock(wraps=support._run_cloud_api_request)

    with (
        patch("core.cloud_sync_runtime.CloudStateDeltaStore") as store_cls,
        patch("core.cloud_sync_runtime.CloudStateDeltaInbox") as inbox_cls,
        patch("core.cloud_sync_runtime.CloudAgentStatusStore") as agent_status_store_cls,
        patch("core.cloud_sync_runtime.CloudContentStateStore") as content_state_store_cls,
    ):
        store_cls.return_value.diagnostics.return_value = {"cursors": {"tasks": 2}}
        inbox_cls.return_value.diagnostics.return_value = {"by_status": {"pending": 5, "applied": 8}}
        agent_status_store_cls.return_value.diagnostics.return_value = {"total": 2}
        content_state_store_cls.return_value.diagnostics.return_value = {"answers_total": 7, "assets_total": 9}

        result = support.handle_command("cloud.sync_health", {"failedLimit": "3"})

    assert result["ok"] is True
    health = result["sync_health"]
    assert health["summary"]["logged_in"] is True
    assert health["summary"]["upload_backpressure_active"] is True
    assert health["summary"]["upload_backpressure_queue_depth_hint"] == 400
    assert health["summary"]["object_download_retry_backpressure_active"] is True
    assert health["summary"]["object_download_retry_backpressure_retry_after_seconds"] == 6.0
    assert health["summary"]["object_download_retry_backpressure_queue_depth_hint"] == 17
    assert health["summary"]["object_download_retry_backpressure_bucket"] == "object_download"
    assert health["summary"]["object_upload_retry_backpressure_active"] is True
    assert health["summary"]["object_upload_retry_backpressure_retry_after_seconds"] == 15.0
    assert health["summary"]["object_upload_retry_backpressure_queue_depth_hint"] == 25
    assert health["summary"]["object_upload_retry_backpressure_bucket"] == "object_upload"
    assert health["summary"]["object_upload_retry_ready_count"] == 0
    assert health["summary"]["object_upload_retry_waiting_count"] == 1
    assert health["summary"]["object_upload_retry_wait_reason"] == "waiting_retry_backoff"
    assert health["summary"]["next_object_upload_retry_after_seconds"] == 7
    assert health["summary"]["outbox_pending"] == 2
    assert health["summary"]["outbox_failed"] == 1
    assert health["summary"]["outbox_upload_ready"] == 3
    assert health["summary"]["next_retry_after_seconds"] == 12
    assert health["summary"]["outbox_wait_reason"] == "waiting_retry_backoff"
    assert health["summary"]["next_upload_attempt_after_seconds"] == 12
    assert health["summary"]["inbox_pending"] == 5
    assert health["summary"]["agent_status_total"] == 2
    assert health["summary"]["answers_cached"] == 7
    assert health["summary"]["assets_cached"] == 9
    assert health["summary"]["object_cache_objects"] == 4
    assert health["summary"]["object_cache_bytes"] == 250
    assert health["summary"]["object_cache_max_bytes"] == 1000
    assert health["summary"]["object_cache_free_bytes"] == 750
    assert health["summary"]["object_cache_usage_ratio"] == 0.25
    assert health["summary"]["object_transfers_total"] == 5
    assert health["summary"]["object_transfers_retryable"] == 2
    assert health["summary"]["object_transfers_failed"] == 2
    assert health["summary"]["object_transfers_running"] == 2
    assert health["summary"]["object_upload_transfers_failed"] == 1
    assert health["summary"]["object_upload_transfers_running"] == 1
    assert health["summary"]["object_download_transfers_failed"] == 1
    assert health["summary"]["object_download_transfers_running"] == 1
    assert health["summary"]["capabilities_ok"] is True
    assert health["summary"]["capabilities_cached"] is False
    assert health["summary"]["object_storage_backend"] == "local"
    assert health["summary"]["object_storage_total_quota_bytes"] == 10 * 1024 * 1024 * 1024
    assert health["summary"]["object_storage_workspace_quota_bytes"] == 5 * 1024 * 1024 * 1024
    assert health["summary"]["object_storage_max_file_bytes"] == 512 * 1024 * 1024
    assert health["summary"]["object_storage_min_free_bytes"] == 8 * 1024 * 1024 * 1024
    assert health["summary"]["inline_blob_max_bytes"] == 32 * 1024
    assert health["summary"]["single_put_max_bytes"] == 5 * 1024 * 1024
    assert health["summary"]["multipart_part_bytes"] == 8 * 1024 * 1024
    assert health["summary"]["cloud_object_storage_checked"] is False
    assert health["summary"]["healthy"] is True
    assert health["auto_sync"] == auto_sync_status
    assert health["capabilities"]["limits"]["object_storage_total_quota_bytes"] == 10 * 1024 * 1024 * 1024
    assert health["state_delta"] == {"cursors": {"tasks": 2}}
    bound_outbox.diagnostics.assert_called_once_with(failed_limit=3)
    inbox_cls.return_value.diagnostics.assert_called_once_with(failed_limit=3)
    support._run_cloud_api_request.assert_not_called()


def test_app_cloud_runtime_support_sync_health_can_include_cloud_object_storage_report():
    owner = _support_owner()
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.surfacedlab.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 3, "workspace_id": 4},
    }
    bound_outbox = MagicMock()
    bound_outbox.diagnostics.return_value = {"stats": {"pending": 0, "failed": 0, "dead_letter": 0}}
    outbox = MagicMock()
    outbox.bind_to_session.return_value = bound_outbox
    support = AppCloudRuntimeSupport(
        owner=owner,
        session_store_factory=lambda: session_store,
        outbox_factory=lambda: outbox,
        auto_sync_status_getter=lambda: {"running": True},
        object_cache_factory=lambda: MagicMock(diagnostics=lambda: {}),
        object_transfer_store_factory=lambda: MagicMock(diagnostics=lambda failed_limit=10: {}),
    )
    support._cloud_capabilities_cached = Mock(return_value={"ok": True, "cached": True, "limits": {}})
    support._run_cloud_api_request = Mock(
        return_value={
            "ok": True,
            "payload": {
                "report": {
                    "status": "warn",
                    "disk": {"free_bytes": 9 * 1024 * 1024 * 1024},
                    "pressure": {"max_safe_upload_bytes": 256 * 1024 * 1024, "warnings": ["near_min_free"]},
                    "missing_files": [{"object_id": "missing-1"}],
                    "orphan_files": [{"path": "/opt/surfaced/object-data/orphan.bin"}],
                },
                "text": "Object storage doctor: status=warn",
            },
            "message": "",
        }
    )

    with (
        patch("core.cloud_sync_runtime.CloudStateDeltaStore") as store_cls,
        patch("core.cloud_sync_runtime.CloudStateDeltaInbox") as inbox_cls,
        patch("core.cloud_sync_runtime.CloudAgentStatusStore") as agent_status_store_cls,
        patch("core.cloud_sync_runtime.CloudContentStateStore") as content_state_store_cls,
    ):
        store_cls.return_value.diagnostics.return_value = {}
        inbox_cls.return_value.diagnostics.return_value = {"by_status": {}}
        agent_status_store_cls.return_value.diagnostics.return_value = {}
        content_state_store_cls.return_value.diagnostics.return_value = {}

        result = support.handle_command("cloud.sync_health", {"includeCloudObjectStorage": True})

    health = result["sync_health"]
    assert health["cloud_object_storage"]["payload"]["report"]["status"] == "warn"
    assert health["summary"]["cloud_object_storage_checked"] is True
    assert health["summary"]["cloud_object_storage_ok"] is True
    assert health["summary"]["cloud_object_storage_status"] == "warn"
    assert health["summary"]["cloud_object_storage_disk_free_bytes"] == 9 * 1024 * 1024 * 1024
    assert health["summary"]["cloud_object_storage_max_safe_upload_bytes"] == 256 * 1024 * 1024
    assert health["summary"]["cloud_object_storage_warning_count"] == 1
    assert health["summary"]["cloud_object_storage_missing_files"] == 1
    assert health["summary"]["cloud_object_storage_orphan_files"] == 1
    support._run_cloud_api_request.assert_called_once_with("admin_object_storage_report", {"includeCloudObjectStorage": True})


def test_app_cloud_runtime_support_cache_object_downloads_to_file_cache():
    owner = _support_owner()
    data = b"downloaded object bytes"
    sha256 = hashlib.sha256(data).hexdigest()
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.example.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 3, "workspace_id": 4},
    }

    class FakeClient:
        def __init__(self, base_url: str) -> None:
            self.base_url = base_url

        def create_object_download(self, token: str, object_id: str, *, trace_id: str = ""):
            assert token == "access"
            assert object_id == "object-1"
            assert trace_id == "trace-1"
            return {
                "object_id": object_id,
                "download_url": "/api/v2/objects/object-1/content",
                "size_bytes": len(data),
                "content_type": "text/plain",
                "compression": "none",
            }

        def iter_object_content(self, token: str, download_url: str, *, trace_id: str = ""):
            assert token == "access"
            assert download_url == "/api/v2/objects/object-1/content"
            assert trace_id == "trace-1"
            return iter([data[:10], data[10:]])

    with tempfile.TemporaryDirectory() as tmp:
        cache = CloudObjectCache(Path(tmp) / "cache")
        transfer_store = CloudObjectTransferStore(Path(tmp) / "transfers.sqlite3")
        support = AppCloudRuntimeSupport(
            owner=owner,
            session_store_factory=lambda: session_store,
            request_client_factory=FakeClient,
            object_cache_factory=lambda: cache,
            object_transfer_store_factory=lambda: transfer_store,
        )

        result = support.handle_command(
            "cloud.cache_object",
            {
                "trace_id": "trace-1",
                "object_ref": {
                    "object_id": "object-1",
                    "sha256": sha256,
                    "size_bytes": len(data),
                    "content_type": "text/plain",
                },
            },
        )

        assert result["ok"] is True
        assert result["downloaded"] is True
        assert Path(result["cached"]["path"]).read_bytes() == data
        transfer_diagnostics = transfer_store.diagnostics()
        assert transfer_diagnostics["by_status"] == {"completed": 1}
        assert transfer_diagnostics["newest"][0]["object_id"] == "object-1"
        assert transfer_diagnostics["newest"][0]["path"] == result["cached"]["path"]


def test_app_cloud_runtime_support_cache_object_reuses_valid_cached_file():
    owner = _support_owner()
    data = b"already cached"
    ref = {
        "object_id": "object-1",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
    }
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.example.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 3, "workspace_id": 4},
    }
    with tempfile.TemporaryDirectory() as tmp:
        cache = CloudObjectCache(Path(tmp) / "cache")
        cache.cache_bytes(ref, [data])
        transfer_store = CloudObjectTransferStore(Path(tmp) / "transfers.sqlite3")
        client_factory = MagicMock()
        support = AppCloudRuntimeSupport(
            owner=owner,
            session_store_factory=lambda: session_store,
            request_client_factory=client_factory,
            object_cache_factory=lambda: cache,
            object_transfer_store_factory=lambda: transfer_store,
        )

        result = support.handle_command("cloud.cache_object", {"object_ref": ref})

        assert result["ok"] is True
        assert result["downloaded"] is False
        client_factory.assert_not_called()
        assert transfer_store.diagnostics()["total"] == 0


def test_app_cloud_runtime_support_cache_object_records_failed_transfer():
    owner = _support_owner()
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.example.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 3, "workspace_id": 4},
    }

    class FailingClient:
        def __init__(self, _base_url: str) -> None:
            pass

        def create_object_download(self, _token: str, _object_id: str, *, trace_id: str = ""):
            raise CloudClientError("download unavailable", status_code=503)

    with tempfile.TemporaryDirectory() as tmp:
        cache = CloudObjectCache(Path(tmp) / "cache")
        transfer_store = CloudObjectTransferStore(Path(tmp) / "transfers.sqlite3")
        support = AppCloudRuntimeSupport(
            owner=owner,
            session_store_factory=lambda: session_store,
            request_client_factory=FailingClient,
            object_cache_factory=lambda: cache,
            object_transfer_store_factory=lambda: transfer_store,
        )

        result = support.handle_command(
            "cloud.cache_object",
            {
                "trace_id": "trace-fail",
                "object_ref": {
                    "object_id": "object-1",
                    "sha256": "c" * 64,
                    "size_bytes": 10,
                    "content_type": "text/plain",
                },
            },
        )
        diagnostics = transfer_store.diagnostics()

    assert result["ok"] is False
    assert "download unavailable" in result["message"]
    assert diagnostics["by_status"] == {"failed": 1}
    assert diagnostics["failed"][0]["transfer_id"] == "trace-fail"
    assert diagnostics["failed"][0]["last_error"] == "download unavailable"


def test_app_cloud_runtime_support_records_download_metadata_before_cache_failure():
    owner = _support_owner()
    data = b"bad compressed bytes"
    sha256 = "d" * 64
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.example.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 3, "workspace_id": 4},
    }

    class FakeClient:
        def __init__(self, _base_url: str) -> None:
            pass

        def create_object_download(self, _token: str, object_id: str, *, trace_id: str = ""):
            return {
                "object_id": object_id,
                "download_url": "/api/v2/objects/object-1/content",
                "size_bytes": 100,
                "storage_size_bytes": len(data),
                "content_type": "text/plain",
                "compression": "zstd",
            }

        def iter_object_content(self, _token: str, _download_url: str, *, trace_id: str = ""):
            return iter([data])

    with tempfile.TemporaryDirectory() as tmp:
        cache = CloudObjectCache(Path(tmp) / "cache")
        transfer_store = CloudObjectTransferStore(Path(tmp) / "transfers.sqlite3")
        support = AppCloudRuntimeSupport(
            owner=owner,
            session_store_factory=lambda: session_store,
            request_client_factory=FakeClient,
            object_cache_factory=lambda: cache,
            object_transfer_store_factory=lambda: transfer_store,
        )

        result = support.handle_command(
            "cloud.cache_object",
            {
                "trace_id": "download-zstd-fail",
                "object_ref": {
                    "object_id": "object-1",
                    "sha256": sha256,
                    "size_bytes": 100,
                    "content_type": "text/plain",
                },
            },
        )
        diagnostics = transfer_store.diagnostics()

    assert result["ok"] is False
    assert diagnostics["failed"][0]["compression"] == "zstd"
    assert diagnostics["failed"][0]["storage_size_bytes"] == len(data)


def test_app_cloud_runtime_support_lists_object_transfer_retry_candidates():
    owner = _support_owner()
    with tempfile.TemporaryDirectory() as tmp:
        transfer_store = CloudObjectTransferStore(Path(tmp) / "transfers.sqlite3")
        transfer_store.start_transfer(transfer_id="download-1", direction="download", object_id="object-1")
        transfer_store.fail_transfer("download-1", "temporary", retry_after_seconds=0)
        support = AppCloudRuntimeSupport(owner=owner, object_transfer_store_factory=lambda: transfer_store)

        result = support.handle_command(
            "cloud.object_transfer_retry_candidates",
            {"direction": "download", "limit": 5, "maxAttempts": 5},
        )

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["retryable_transfers"][0]["transfer_id"] == "download-1"


def test_app_cloud_runtime_support_reports_object_upload_retry_status():
    owner = _support_owner()
    with tempfile.TemporaryDirectory() as tmp:
        transfer_store = CloudObjectTransferStore(Path(tmp) / "transfers.sqlite3")
        transfer_store.start_transfer(transfer_id="upload-1", direction="upload", sha256="a" * 64)
        transfer_store.fail_transfer("upload-1", "busy", retry_after_seconds=7.5)
        support = AppCloudRuntimeSupport(owner=owner, object_transfer_store_factory=lambda: transfer_store)

        result = support.object_upload_retry_status({})

    assert result["ok"] is True
    assert result["available"] is True
    assert result["direction"] == "upload"
    assert result["retry_ready_count"] == 0
    assert result["retry_waiting_count"] == 1
    assert result["wait_reason"] == "waiting_retry_backoff"
    assert result["next_retry_after_seconds"] >= 7


def test_app_cloud_runtime_support_retries_failed_object_downloads():
    owner = _support_owner()
    data = b"retry object bytes"
    sha256 = hashlib.sha256(data).hexdigest()
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.example.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 3, "workspace_id": 4},
    }

    class FakeClient:
        def __init__(self, _base_url: str) -> None:
            pass

        def create_object_download(self, _token: str, object_id: str, *, trace_id: str = ""):
            assert object_id == "object-1"
            assert trace_id == "download-1"
            return {
                "object_id": object_id,
                "download_url": "/api/v2/objects/object-1/content",
                "size_bytes": len(data),
                "content_type": "text/plain",
                "compression": "none",
            }

        def iter_object_content(self, _token: str, _download_url: str, *, trace_id: str = ""):
            return iter([data])

    with tempfile.TemporaryDirectory() as tmp:
        cache = CloudObjectCache(Path(tmp) / "cache")
        transfer_store = CloudObjectTransferStore(Path(tmp) / "transfers.sqlite3")
        transfer_store.start_transfer(
            transfer_id="download-1",
            direction="download",
            object_id="object-1",
            sha256=sha256,
            size_bytes=len(data),
            content_type="text/plain",
        )
        transfer_store.fail_transfer("download-1", "temporary", retry_after_seconds=0)
        support = AppCloudRuntimeSupport(
            owner=owner,
            session_store_factory=lambda: session_store,
            request_client_factory=FakeClient,
            object_cache_factory=lambda: cache,
            object_transfer_store_factory=lambda: transfer_store,
        )

        result = support.handle_command("cloud.retry_object_downloads", {"limit": 5})
        diagnostics = transfer_store.diagnostics()
        cached = cache.cached_object({"sha256": sha256, "size_bytes": len(data)})

    assert result["ok"] is True
    assert result["attempted"] == 1
    assert result["recovered"] == 1
    assert diagnostics["by_status"] == {"completed": 1}
    assert cached["valid"] is True


def test_app_cloud_runtime_support_retries_failed_zstd_object_downloads_with_transfer_metadata():
    import compression.zstd as zstd

    owner = _support_owner()
    data = b"retry compressed object bytes" * 20
    compressed = zstd.compress(data)
    sha256 = hashlib.sha256(data).hexdigest()
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.example.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 3, "workspace_id": 4},
    }

    class FakeClient:
        def __init__(self, _base_url: str) -> None:
            pass

        def create_object_download(self, _token: str, object_id: str, *, trace_id: str = ""):
            assert object_id == "object-1"
            assert trace_id == "download-zstd-1"
            return {
                "object_id": object_id,
                "download_url": "/api/v2/objects/object-1/content",
                "size_bytes": len(data),
                "storage_size_bytes": len(compressed),
                "content_type": "text/plain",
                "compression": "zstd",
            }

        def iter_object_content(self, _token: str, _download_url: str, *, trace_id: str = ""):
            return iter([compressed[:5], compressed[5:]])

    with tempfile.TemporaryDirectory() as tmp:
        cache = CloudObjectCache(Path(tmp) / "cache")
        transfer_store = CloudObjectTransferStore(Path(tmp) / "transfers.sqlite3")
        transfer_store.start_transfer(
            transfer_id="download-zstd-1",
            direction="download",
            object_id="object-1",
            sha256=sha256,
            size_bytes=len(data),
            storage_size_bytes=len(compressed),
            content_type="text/plain",
            compression="zstd",
        )
        transfer_store.fail_transfer("download-zstd-1", "temporary", retry_after_seconds=0)
        support = AppCloudRuntimeSupport(
            owner=owner,
            session_store_factory=lambda: session_store,
            request_client_factory=FakeClient,
            object_cache_factory=lambda: cache,
            object_transfer_store_factory=lambda: transfer_store,
        )

        result = support.handle_command("cloud.retry_object_downloads", {"limit": 5})
        cached = cache.cached_object({"sha256": sha256, "size_bytes": len(data)})
        diagnostics = transfer_store.diagnostics()
        cached_bytes = Path(cached["path"]).read_bytes()

    assert result["ok"] is True
    assert result["recovered"] == 1
    assert cached["valid"] is True
    assert cached_bytes == data
    assert diagnostics["newest"][0]["compression"] == "zstd"
    assert diagnostics["newest"][0]["storage_size_bytes"] == len(compressed)


def test_app_cloud_runtime_support_retry_object_downloads_skips_uploads():
    owner = _support_owner()
    with tempfile.TemporaryDirectory() as tmp:
        transfer_store = CloudObjectTransferStore(Path(tmp) / "transfers.sqlite3")
        transfer_store.start_transfer(transfer_id="upload-1", direction="upload", sha256="a" * 64)
        transfer_store.fail_transfer("upload-1", "temporary", retry_after_seconds=0)
        client_factory = MagicMock()
        support = AppCloudRuntimeSupport(
            owner=owner,
            request_client_factory=client_factory,
            object_transfer_store_factory=lambda: transfer_store,
        )

        result = support.handle_command("cloud.retry_object_downloads", {"limit": 5})

    assert result["ok"] is True
    assert result["attempted"] == 0
    assert result["recovered"] == 0
    client_factory.assert_not_called()


def test_app_cloud_runtime_support_retry_object_downloads_returns_backpressure_metadata():
    owner = _support_owner()
    sha256 = hashlib.sha256(b"download me later").hexdigest()
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.example.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 3, "workspace_id": 4},
    }

    class BusyClient:
        def __init__(self, _base_url: str) -> None:
            pass

        def create_object_download(self, _token: str, object_id: str, *, trace_id: str = ""):
            assert object_id == "object-1"
            assert trace_id == "download-1"
            raise CloudClientError(
                "object download busy",
                status_code=429,
                retry_after_seconds=6.5,
                queue_depth_hint=17,
                throttle_bucket="object_download",
            )

    with tempfile.TemporaryDirectory() as tmp:
        cache = CloudObjectCache(Path(tmp) / "cache")
        transfer_store = CloudObjectTransferStore(Path(tmp) / "transfers.sqlite3")
        transfer_store.start_transfer(
            transfer_id="download-1",
            direction="download",
            object_id="object-1",
            sha256=sha256,
            size_bytes=17,
            content_type="text/plain",
        )
        transfer_store.fail_transfer("download-1", "temporary", retry_after_seconds=0)
        support = AppCloudRuntimeSupport(
            owner=owner,
            session_store_factory=lambda: session_store,
            request_client_factory=BusyClient,
            object_cache_factory=lambda: cache,
            object_transfer_store_factory=lambda: transfer_store,
        )

        result = support.handle_command("cloud.retry_object_downloads", {"limit": 5})

    assert result["ok"] is False
    assert result["attempted"] == 1
    assert result["failed"] == 1
    assert result["retry_after_seconds"] == 6.5
    assert result["queue_depth_hint"] == 17
    assert result["throttle_bucket"] == "object_download"
    assert result["failures"][0]["retry_after_seconds"] == 6.5


def test_app_cloud_runtime_support_retries_failed_object_uploads_when_source_matches():
    owner = _support_owner()
    body = b"retry upload bytes"
    sha256 = hashlib.sha256(body).hexdigest()
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.example.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 3, "workspace_id": 4},
    }

    class FakeClient:
        def __init__(self, _base_url: str) -> None:
            self.uploaded = b""

        def create_object_upload(self, _token: str, **kwargs):
            assert kwargs["sha256"] == sha256
            assert kwargs["size_bytes"] == len(body)
            return {
                "strategy": "single_put",
                "session_id": "session-1",
                "sha256": kwargs["sha256"],
                "size_bytes": kwargs["size_bytes"],
                "storage_size_bytes": kwargs["storage_size_bytes"],
                "content_type": kwargs["content_type"],
                "compression": "none",
                "upload": {"url": "/api/v2/objects/uploads/session-1/content", "headers": {}},
            }

        def upload_object_content(self, _token: str, _upload_url: str, chunks, **_kwargs):
            self.uploaded = b"".join(chunks)
            return {"ok": True}

        def complete_object_upload(self, _token: str, _session_id: str, **_kwargs):
            return {"object_id": "object-1", "status": "active"}

    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "answer.txt"
        source.write_bytes(body)
        transfer_store = CloudObjectTransferStore(Path(tmp) / "transfers.sqlite3")
        transfer_store.start_transfer(
            transfer_id="upload-1",
            direction="upload",
            sha256=sha256,
            size_bytes=len(body),
            path=str(source),
            content_type="text/plain",
        )
        transfer_store.fail_transfer("upload-1", "temporary", retry_after_seconds=0)
        support = AppCloudRuntimeSupport(
            owner=owner,
            session_store_factory=lambda: session_store,
            request_client_factory=FakeClient,
            object_transfer_store_factory=lambda: transfer_store,
        )

        result = support.handle_command("cloud.retry_object_uploads", {"limit": 5})
        diagnostics = transfer_store.diagnostics()

    assert result["ok"] is True
    assert result["attempted"] == 1
    assert result["recovered"] == 1
    assert diagnostics["by_status"] == {"active": 1}
    assert diagnostics["newest"][0]["object_id"] == "object-1"


def test_app_cloud_runtime_support_skips_object_upload_retry_when_file_exceeds_cloud_limit():
    owner = _support_owner()
    body = b"too large for configured cloud limit"
    sha256 = hashlib.sha256(body).hexdigest()
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.example.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 3, "workspace_id": 4},
    }

    class FakeClient:
        uploads = 0

        def __init__(self, _base_url: str) -> None:
            pass

        def capabilities(self, _token: str):
            return {
                "capabilities": ["sync-v2", "object-v1"],
                "limits": {"object_storage_max_file_bytes": len(body) - 1},
                "ttl_seconds": {},
                "object_storage_backend": "local",
            }

        def create_object_upload(self, _token: str, **_kwargs):
            FakeClient.uploads += 1
            raise AssertionError("oversized retry should not call create_object_upload")

    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "answer.txt"
        source.write_bytes(body)
        transfer_store = CloudObjectTransferStore(Path(tmp) / "transfers.sqlite3")
        transfer_store.start_transfer(
            transfer_id="upload-oversized",
            direction="upload",
            sha256=sha256,
            size_bytes=len(body),
            path=str(source),
            content_type="text/plain",
        )
        transfer_store.fail_transfer("upload-oversized", "temporary", retry_after_seconds=0)
        support = AppCloudRuntimeSupport(
            owner=owner,
            session_store_factory=lambda: session_store,
            request_client_factory=FakeClient,
            object_transfer_store_factory=lambda: transfer_store,
        )

        result = support.handle_command("cloud.retry_object_uploads", {"limit": 5})
        diagnostics = transfer_store.diagnostics()

    assert result["ok"] is False
    assert result["attempted"] == 0
    assert result["failed"] == 1
    assert "exceeds cloud max file size" in result["failures"][0]["message"]
    assert "exceeds cloud max file size" in diagnostics["failed"][0]["last_error"]
    assert FakeClient.uploads == 0


def test_app_cloud_runtime_support_skips_object_upload_retry_when_source_changed():
    owner = _support_owner()
    original = b"original bytes"
    sha256 = hashlib.sha256(original).hexdigest()
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "answer.txt"
        source.write_bytes(b"changed bytes")
        transfer_store = CloudObjectTransferStore(Path(tmp) / "transfers.sqlite3")
        transfer_store.start_transfer(
            transfer_id="upload-1",
            direction="upload",
            sha256=sha256,
            size_bytes=len(original),
            path=str(source),
            content_type="text/plain",
        )
        transfer_store.fail_transfer("upload-1", "temporary", retry_after_seconds=0)
        client_factory = MagicMock()
        support = AppCloudRuntimeSupport(
            owner=owner,
            request_client_factory=client_factory,
            object_transfer_store_factory=lambda: transfer_store,
        )

        result = support.handle_command("cloud.retry_object_uploads", {"limit": 5})
        diagnostics = transfer_store.diagnostics()

    assert result["ok"] is True
    assert result["attempted"] == 0
    assert result["skipped"] == 1
    assert "source changed" in diagnostics["failed"][0]["last_error"]
    client_factory.assert_not_called()


def test_app_cloud_runtime_support_retry_object_uploads_returns_backpressure_metadata():
    owner = _support_owner()
    body = b"retry upload bytes"
    sha256 = hashlib.sha256(body).hexdigest()
    session_store = MagicMock()
    session_store.load.return_value = {
        "base_url": "https://api.example.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "user": {"id": 3, "workspace_id": 4},
    }

    class BusyClient:
        def __init__(self, _base_url: str) -> None:
            pass

        def create_object_upload(self, _token: str, **_kwargs):
            raise CloudClientError(
                "object storage busy",
                status_code=429,
                retry_after_seconds=7.5,
                queue_depth_hint=42,
                throttle_bucket="object_upload",
            )

    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "answer.txt"
        source.write_bytes(body)
        transfer_store = CloudObjectTransferStore(Path(tmp) / "transfers.sqlite3")
        transfer_store.start_transfer(
            transfer_id="upload-1",
            direction="upload",
            sha256=sha256,
            size_bytes=len(body),
            path=str(source),
            content_type="text/plain",
        )
        transfer_store.fail_transfer("upload-1", "temporary", retry_after_seconds=0)
        support = AppCloudRuntimeSupport(
            owner=owner,
            session_store_factory=lambda: session_store,
            request_client_factory=BusyClient,
            object_transfer_store_factory=lambda: transfer_store,
        )

        result = support.handle_command("cloud.retry_object_uploads", {"limit": 5})

    assert result["ok"] is False
    assert result["attempted"] == 1
    assert result["failed"] == 1
    assert result["retry_after_seconds"] == 7.5
    assert result["queue_depth_hint"] == 42
    assert result["throttle_bucket"] == "object_upload"
    assert result["failures"][0]["retry_after_seconds"] == 7.5


def test_app_cloud_runtime_support_prunes_object_cache():
    owner = _support_owner()
    data_a = b"a" * 10
    data_b = b"b" * 10
    with tempfile.TemporaryDirectory() as tmp:
        cache = CloudObjectCache(Path(tmp) / "cache", max_cache_bytes=100)
        cache.cache_bytes(
            {"object_id": "a", "sha256": hashlib.sha256(data_a).hexdigest(), "size_bytes": len(data_a)},
            [data_a],
        )
        cache.cache_bytes(
            {"object_id": "b", "sha256": hashlib.sha256(data_b).hexdigest(), "size_bytes": len(data_b)},
            [data_b],
        )
        cache.max_cache_bytes = 12
        support = AppCloudRuntimeSupport(owner=owner, object_cache_factory=lambda: cache)

        result = support.handle_command("cloud.prune_object_cache", {"target_bytes": 10})
        diagnostics = cache.diagnostics()

    assert result["ok"] is True
    assert result["object_cache"]["pruned"] == 1
    assert diagnostics["objects"] == 1
    assert diagnostics["bytes"] == 10


def test_app_cloud_runtime_support_command_returns_current_status_variants():
    owner = _support_owner()
    support = AppCloudRuntimeSupport(owner=owner)
    support.current_cloud_status = Mock(return_value={"ok": True, "cloud": {"loggedIn": True}})
    support.cloud_status_from_session = Mock(return_value={"ok": True, "cloud": {"loggedIn": False}})

    current = support.handle_command("cloud.current_status")
    from_session = support.handle_command("cloud.status_from_session", {"session": {"access_token": "token"}})

    assert current == {"ok": True, "cloud": {"loggedIn": True}}
    assert from_session == {"ok": True, "cloud": {"loggedIn": False}}
    support.current_cloud_status.assert_called_once_with()
    support.cloud_status_from_session.assert_called_once_with({"access_token": "token"})


def test_app_cloud_runtime_support_command_lists_admin_users_via_cloud_request():
    owner = _support_owner()
    support = AppCloudRuntimeSupport(owner=owner)

    class FakeClient:
        def list_admin_users(self, _token: str):
            return [{"id": 1, "username": "operator"}]

    support.cloud_request_with_refresh = Mock(side_effect=lambda operation: (True, operation(FakeClient(), "access-token"), ""))

    result = support.handle_command("cloud.list_admin_users")

    assert result == {"ok": True, "payload": [{"id": 1, "username": "operator"}], "message": ""}


def test_app_cloud_runtime_support_command_reads_admin_object_storage_report():
    owner = _support_owner()
    support = AppCloudRuntimeSupport(owner=owner)

    class FakeClient:
        def admin_object_storage_report(self, _token: str):
            return {
                "report": {
                    "status": "warn",
                    "disk": {"free_bytes": 9 * 1024 * 1024 * 1024},
                    "limits": {"object_storage_max_file_bytes": 512 * 1024 * 1024},
                },
                "text": "Object storage doctor: status=warn",
            }

    support.cloud_request_with_refresh = Mock(side_effect=lambda operation: (True, operation(FakeClient(), "access-token"), ""))

    result = support.handle_command("cloud.admin_object_storage_report")

    assert result["ok"] is True
    assert result["payload"]["report"]["status"] == "warn"
    assert result["payload"]["report"]["limits"]["object_storage_max_file_bytes"] == 512 * 1024 * 1024


def test_app_cloud_runtime_support_daemon_handle_command_delegates_to_main_handler_when_needed():
    owner = _support_owner()
    support = AppCloudRuntimeSupport(owner=owner)
    support.handle_command_for_main = Mock(return_value={"ok": True, "message": "main"})  # type: ignore[attr-defined]

    result = support.daemon_handle_command("cloud.logout")

    assert result == {"ok": True, "message": "main"}
    support.handle_command_for_main.assert_called_once_with("cloud.logout", {})


def test_app_cloud_runtime_support_daemon_handles_outbox_diagnostics_locally():
    owner = _support_owner()
    support = AppCloudRuntimeSupport(owner=owner)
    support.cloud_outbox_diagnostics = Mock(return_value={"ok": True, "outbox": {"stats": {"failed": 1}}})
    support.handle_command_for_main = Mock(side_effect=AssertionError("main process should not be used"))  # type: ignore[attr-defined]

    result = support.daemon_handle_command("cloud.outbox_diagnostics", {"failed_limit": 3})

    assert result == {"ok": True, "outbox": {"stats": {"failed": 1}}}
    support.cloud_outbox_diagnostics.assert_called_once_with({"failed_limit": 3})


def test_app_cloud_runtime_support_command_resolves_article_classification_job():
    owner = _support_owner()
    support = AppCloudRuntimeSupport(owner=owner)

    class FakeClient:
        def resolve_admin_article_classification_job(self, _token: str, job_id: int, *, task_id: int, reason: str = ""):
            return {"job_id": job_id, "task_id": task_id, "reason": reason}

    support.cloud_request_with_refresh = Mock(side_effect=lambda operation: (True, operation(FakeClient(), "access-token"), ""))

    result = support.handle_command(
        "cloud.resolve_admin_article_classification_job",
        {"job_id": 11, "task_id": 3, "reason": "manual"},
    )

    assert result == {"ok": True, "payload": {"job_id": 11, "task_id": 3, "reason": "manual"}, "message": ""}


def test_app_cloud_runtime_support_command_updates_admin_user_with_payload():
    owner = _support_owner()
    support = AppCloudRuntimeSupport(owner=owner)

    class FakeClient:
        def update_admin_user(self, _token: str, user_id: int, payload: dict[str, Any]):
            return {"user_id": user_id, "payload": payload}

    support.cloud_request_with_refresh = Mock(side_effect=lambda operation: (True, operation(FakeClient(), "access-token"), ""))

    result = support.handle_command(
        "cloud.update_admin_user",
        {"user_id": 2, "payload": {"visible_task_ids": [8, 9]}},
    )

    assert result == {
        "ok": True,
        "payload": {"user_id": 2, "payload": {"visible_task_ids": [8, 9]}},
        "message": "",
    }


def test_app_cloud_runtime_support_command_runs_admin_task_sync_with_local_snapshots():
    owner = _support_owner()
    owner._ensure_local_admin_tasks_in_cloud = Mock(return_value={"tasks": [{"id": 1}], "local_updates": []})
    support = AppCloudRuntimeSupport(owner=owner)

    class FakeClient:
        pass

    support.cloud_request_with_refresh = Mock(side_effect=lambda operation: (True, operation(FakeClient(), "access-token"), ""))

    result = support.handle_command(
        "cloud.list_admin_tasks_with_local_sync",
        {"local_snapshots": [{"local_task_id": "task-1"}]},
    )

    assert result == {"ok": True, "payload": {"tasks": [{"id": 1}], "local_updates": []}, "message": ""}
    owner._ensure_local_admin_tasks_in_cloud.assert_called_once()


def test_app_cloud_runtime_support_command_syncs_admin_task_and_assigns_operator():
    owner = _support_owner()
    support = AppCloudRuntimeSupport(owner=owner)

    class FakeClient:
        def __init__(self) -> None:
            self.assigned: list[dict[str, int | str]] = []

        def create_admin_task(self, _token: str, payload: dict[str, Any]) -> dict[str, Any]:
            return {"id": 100, "task_key": payload["task_key"], "name": payload["name"]}

        def assign_admin_task_member(self, _token: str, task_id: int, *, user_id: int, access_level: str, note: str) -> dict[str, Any]:
            self.assigned.append({"task_id": task_id, "user_id": user_id, "access_level": access_level, "note": note})
            return {}

        def list_admin_tasks(self, _token: str) -> list[dict[str, Any]]:
            return [{"id": 100, "task_key": "task-1", "assigned_operator_username": "user-7"}]

    fake_client = FakeClient()
    support.cloud_request_with_refresh = Mock(side_effect=lambda operation: (True, operation(fake_client, "access-token"), ""))

    result = support.handle_command(
        "cloud.sync_admin_task",
        {
            "local_task_id": "task-1",
            "local_task": {"task_id": "task-1"},
            "existing_cloud_task_id": 0,
            "operator_user_id": 7,
            "cloud_payload": {"name": "Brand A", "brand": "Brand A", "config_json": {}, "enabled": True},
        },
    )

    assert result["ok"] is True
    assert result["payload"]["id"] == 100
    assert fake_client.assigned[0]["user_id"] == 7


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
    assert support.article_snapshot_requested is True
    assert len(started) == 1
    assert started[0].kwargs["name"] == "cloud-article-snapshot-enqueue"


def test_app_cloud_runtime_support_command_runs_article_snapshot_worker_variants():
    owner = _support_owner()
    support = AppCloudRuntimeSupport(owner=owner)
    support.run_article_snapshot_worker = Mock()
    support.run_article_snapshot_retry = Mock()

    worker_result = support.handle_command("cloud.run_article_snapshot_worker")
    retry_result = support.handle_command("cloud.run_article_snapshot_retry", {"delay_seconds": "1.5"})

    assert worker_result == {"ok": True, "message": "文章云端同步 worker 已退出"}
    assert retry_result == {"ok": True, "message": "文章云端同步重试 worker 已退出"}
    support.run_article_snapshot_worker.assert_called_once_with()
    support.run_article_snapshot_retry.assert_called_once_with(1.5)


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


def test_app_cloud_runtime_support_command_logs_into_account_space():
    owner = _support_owner()
    support = AppCloudRuntimeSupport(owner=owner)
    support.login_cloud_account_space = Mock(return_value={"base_url": "https://api.surfacedlab.com", "access_token": "a"})

    result = support.handle_command(
        "cloud.login_account_space",
        {
            "base_url": "https://api.surfacedlab.com",
            "token_pair": {"access_token": "a", "refresh_token": "r"},
        },
    )

    assert result == {
        "ok": True,
        "message": "云端账号空间已切换",
        "session": {"base_url": "https://api.surfacedlab.com", "access_token": "a"},
    }
    support.login_cloud_account_space.assert_called_once_with(
        base_url="https://api.surfacedlab.com",
        token_pair={"access_token": "a", "refresh_token": "r"},
    )


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
    )

    outbox.bind_to_session.assert_not_called()
    account_space_ensurer.assert_called_once_with(
        {
            "base_url": "https://api.surfacedlab.com",
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "token_type": "bearer",
            "user": {"id": 2, "workspace_id": 3, "role": "operator"},
        },
        copy_legacy=False,
    )


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
    )

    account_space_ensurer.assert_called_once_with(
        {
            "base_url": "https://api.surfacedlab.com",
            "access_token": "access",
            "refresh_token": "refresh",
            "token_type": "bearer",
            "user": {"id": 9, "workspace_id": 3, "role": "admin"},
        },
        copy_legacy=True,
    )
