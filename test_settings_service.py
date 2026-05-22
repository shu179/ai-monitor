from backend_lib import settings_service as settings_module
from backend_lib.settings_service import SettingsService


class _FakeLocalModelManager:
    def get_status(self):
        return {"ready": True}


def test_settings_service_projects_masked_settings(monkeypatch):
    monkeypatch.setattr(settings_module, "get_version_payload", lambda: {"version": "test"})
    monkeypatch.setattr(settings_module, "get_local_model_manager", lambda: _FakeLocalModelManager())
    monkeypatch.setattr(settings_module, "build_update_status", lambda config, include_check=False: {"ok": True})
    monkeypatch.setattr(settings_module, "get_app_update_settings", lambda config: {"channel": "stable"})

    config = {
        "scheduler": {"notification_webhook_url": "scheduler-secret"},
        "search": {"tavily_api_key": "tavily-secret"},
        "cloud_sync": {"api_token": "cloud-secret"},
        "recognition": {
            "safe_mode_ocr_enabled": True,
            "ai_fallback_enabled": True,
            "platform": "doubao",
            "model": "doubao-seed",
            "dom_render_mode": True,
            "floating_window_resident_enabled": True,
        },
        "query_execution": {
            "browser": {"strategy": "bad-value", "session_pool_dispatch": "bad-value"},
        },
        "screenshot": {
            "browser_answer_mode": "dom",
            "decoration": {
                "enabled": True,
                "title": "标题",
                "subtitle": "副标题",
                "footer": "页脚",
                "show_timestamp": False,
                "background": {"start": "#111111", "end": "#222222"},
                "header": {"start": "#333333", "end": "#444444"},
                "layout": {"outer_padding": 12, "header_height": 88, "radius": 10, "image_radius": 6},
            },
        },
        "article_export": {"show_keyword_category": True, "show_selfmedia_account": False},
        "storage": {"history_read_backend": "auto", "history_shadow_writes_enabled": True},
    }

    service = SettingsService(
        context_snapshot_loader=lambda: (config, {}),
        browser_auth_loader=lambda: {"platforms": {"doubao": {"authenticated": True}}},
        public_profile_builder=lambda current_config: {"name": "AI 运营"},
        cloud_sync_status_getter=lambda: {"connected": False},
        secret_masker=lambda value: value if str(value or "").startswith("MASK(") else (f"MASK({value})" if value else ""),
    )

    result = service.get_settings()

    assert result["version"] == {"version": "test"}
    assert result["scheduler"]["notification_webhook_url"] == "MASK(scheduler-secret)"
    assert result["search"]["tavily_api_key"] == "MASK(tavily-secret)"
    assert result["tavily"]["api_key"] == "MASK(tavily-secret)"
    assert result["cloud_sync"]["api_token"] == "MASK(cloud-secret)"
    assert result["browser_auth"] == {"doubao": {"authenticated": True}}
    assert result["profile"] == {"name": "AI 运营"}
    assert result["cloud_sync_status"] == {"connected": False}
    assert result["local_model_status"] == {"ready": True}
    assert result["app_update"] == {"channel": "stable"}
    assert result["update_status"] == {"ok": True}
    assert result["article_export"] == {
        "show_keyword_category": True,
        "show_selfmedia_account": False,
    }
    assert result["recognition"] == {
        "safe_mode_ocr_enabled": True,
        "dom_render_mode": True,
        "floating_window_resident_enabled": True,
    }
    assert result["storage"] == {
        "history_read_backend": "auto",
        "history_shadow_writes_enabled": True,
    }
    assert "context_snapshots" not in result
    assert "weather_snapshot" not in result
    assert "calendar_snapshot" not in result
    assert result["query_execution"]["browser"]["strategy"] == "session_pool"
    assert result["query_execution"]["browser"]["session_pool_dispatch"] == "platform_batch"
    assert result["screenshot_template"]["title"] == "{platform}"
    assert result["screenshot_template"]["show_time"] is True
    assert result["screenshot_template"]["background_start"] == "#FCFDFF"
    assert result["screenshot"]["browser_answer_mode"] == "dom"
