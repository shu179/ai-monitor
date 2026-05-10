from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from web_backend import AppRuntime


def test_save_settings_persists_selector_agent_section():
    runtime = AppRuntime()
    base_config = {
        "scheduler": {},
        "search": {},
        "cloud_sync": {},
        "query_execution": {},
        "browser_automation": {},
        "selector_agent": {"enabled": False, "platform": "doubao", "model": "old"},
    }
    saved_configs = []

    runtime.load_config = Mock(return_value=deepcopy(base_config))
    runtime.save_config = Mock(side_effect=lambda config: saved_configs.append(deepcopy(config)) or Path("/tmp/config.yaml"))
    runtime._refresh_monitoring_runtime = Mock()
    runtime._sync_recognition_mode = Mock()

    with (
        patch("web_backend.CloudSessionStore") as session_store_mock,
        patch("web_backend.get_local_model_manager") as local_model_manager_mock,
        patch("web_backend.configure_structured_history_storage"),
        patch("web_backend.maybe_schedule_structured_history_auto_rebuild"),
    ):
        session_store_mock.return_value.load.return_value = {}
        local_model_manager_mock.return_value = Mock(sync_config=Mock())
        result = runtime.save_settings({
            "selector_agent": {
                "enabled": True,
                "platform": "DeepSeek",
                "model": "deepseek-chat",
            },
        })

    assert result["ok"] is True
    assert saved_configs[0]["selector_agent"] == {
        "enabled": True,
        "platform": "deepseek",
        "model": "deepseek-chat",
    }
    assert saved_configs[0]["storage"] == {
        "history_read_backend": "auto",
        "history_write_backend": "auto",
        "history_shadow_writes_enabled": True,
    }


def test_save_settings_persists_normalized_storage_section():
    runtime = AppRuntime()
    base_config = {
        "scheduler": {},
        "search": {},
        "cloud_sync": {},
        "query_execution": {},
        "browser_automation": {},
        "storage": {"history_read_backend": "", "history_shadow_writes_enabled": False},
    }
    saved_configs = []

    runtime.load_config = Mock(return_value=deepcopy(base_config))
    runtime.save_config = Mock(side_effect=lambda config: saved_configs.append(deepcopy(config)) or Path("/tmp/config.yaml"))
    runtime._refresh_monitoring_runtime = Mock()
    runtime._sync_recognition_mode = Mock()

    with (
        patch("web_backend.CloudSessionStore") as session_store_mock,
        patch("web_backend.get_local_model_manager") as local_model_manager_mock,
        patch("web_backend.configure_structured_history_storage") as configure_storage_mock,
        patch("web_backend.maybe_schedule_structured_history_auto_rebuild"),
    ):
        session_store_mock.return_value.load.return_value = {}
        local_model_manager_mock.return_value = Mock(sync_config=Mock())
        result = runtime.save_settings({
            "storage": {
                "history_read_backend": "sqlite_shadow_auto",
                "history_shadow_writes_enabled": 1,
            },
        })

    assert result["ok"] is True
    assert saved_configs[0]["storage"] == {
        "history_read_backend": "auto",
        "history_write_backend": "auto",
        "history_shadow_writes_enabled": True,
    }
    configure_storage_mock.assert_called_once_with(saved_configs[0])


def test_save_settings_preserves_guarded_storage_defaults_without_storage_payload():
    runtime = AppRuntime()
    base_config = {
        "scheduler": {},
        "search": {},
        "cloud_sync": {},
        "query_execution": {},
        "browser_automation": {},
    }
    saved_configs = []

    runtime.load_config = Mock(return_value=deepcopy(base_config))
    runtime.save_config = Mock(side_effect=lambda config: saved_configs.append(deepcopy(config)) or Path("/tmp/config.yaml"))
    runtime._refresh_monitoring_runtime = Mock()
    runtime._sync_recognition_mode = Mock()

    with (
        patch("web_backend.CloudSessionStore") as session_store_mock,
        patch("web_backend.get_local_model_manager") as local_model_manager_mock,
        patch("web_backend.configure_structured_history_storage") as configure_storage_mock,
        patch("web_backend.maybe_schedule_structured_history_auto_rebuild") as rebuild_mock,
    ):
        session_store_mock.return_value.load.return_value = {}
        local_model_manager_mock.return_value = Mock(sync_config=Mock())
        result = runtime.save_settings({"selector_agent": {"enabled": True}})

    assert result["ok"] is True
    assert saved_configs[0]["storage"] == {
        "history_read_backend": "auto",
        "history_write_backend": "auto",
        "history_shadow_writes_enabled": True,
    }
    configure_storage_mock.assert_called_once_with(saved_configs[0])
    rebuild_mock.assert_called_once_with("settings_save")


def test_save_settings_does_not_enable_storage_for_ordinary_cloud_session():
    runtime = AppRuntime()
    base_config = {
        "scheduler": {},
        "search": {},
        "cloud_sync": {},
        "query_execution": {},
        "browser_automation": {},
        "storage": {
            "history_read_backend": "auto",
            "history_write_backend": "auto",
            "history_shadow_writes_enabled": True,
        },
    }
    saved_configs = []

    runtime.load_config = Mock(return_value=deepcopy(base_config))
    runtime.save_config = Mock(side_effect=lambda config: saved_configs.append(deepcopy(config)) or Path("/tmp/config.yaml"))
    runtime._refresh_monitoring_runtime = Mock()
    runtime._sync_recognition_mode = Mock()

    with (
        patch("web_backend.CloudSessionStore") as session_store_mock,
        patch("web_backend.get_local_model_manager") as local_model_manager_mock,
        patch("web_backend.configure_structured_history_storage") as configure_storage_mock,
        patch("web_backend.maybe_schedule_structured_history_auto_rebuild") as rebuild_mock,
    ):
        session_store_mock.return_value.load.return_value = {"access_token": "token", "user": {"role": "viewer"}}
        local_model_manager_mock.return_value = Mock(sync_config=Mock())
        result = runtime.save_settings({
            "storage": {
                "history_read_backend": "sqlite_shadow_auto",
                "history_shadow_writes_enabled": 1,
            },
        })

    assert result["ok"] is True
    assert saved_configs[0]["storage"] == {
        "history_read_backend": "",
        "history_write_backend": "",
        "history_shadow_writes_enabled": False,
    }
    configure_storage_mock.assert_called_once_with(saved_configs[0])
    rebuild_mock.assert_not_called()
