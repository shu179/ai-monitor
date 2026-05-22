from core import context_snapshots as context_module
from core.context_snapshots import ensure_context_snapshots


def test_context_snapshots_skip_stale_network_refresh(monkeypatch):
    def fail_fetch(*_args, **_kwargs):
        raise AssertionError("network refresh should not run")

    monkeypatch.setattr(context_module, "_fetch_open_meteo_weather", fail_fetch)
    monkeypatch.setattr(context_module, "_fetch_timor_calendar", fail_fetch)

    config = {
        "context_snapshots": {
            "weather": {
                "enabled": True,
                "city": "武汉",
                "ttl_minutes": 15,
            },
            "calendar": {
                "enabled": True,
                "ttl_minutes": 30,
            },
        },
        "weather_snapshot": {
            "city": "武汉",
            "summary": "旧天气",
            "updated_at": "2000-01-01T00:00:00",
        },
        "calendar_snapshot": {
            "holiday_name": "旧节日",
            "updated_at": "2000-01-01T00:00:00",
        },
    }

    result = ensure_context_snapshots(config, refresh_stale=False)

    assert result["weather"]["snapshot"]["summary"] == "旧天气"
    assert result["weather"]["message"] == "天气快照待后台刷新"
    assert result["calendar"]["snapshot"]["holiday_name"] == "旧节日"
    assert result["calendar"]["message"] == "节日快照待后台刷新"


def test_context_snapshots_normalize_to_managed_defaults():
    config = {
        "context_snapshots": {
            "weather": {
                "enabled": False,
                "provider": "caiyun",
                "city": "上海",
                "token": "legacy-token",
                "auto_locate_by_ip": False,
                "last_auto_located_on": "2026-05-01",
            },
            "calendar": {
                "enabled": False,
                "provider": "custom",
            },
        }
    }

    normalized = context_module.get_context_snapshots_config(config)

    assert normalized["weather"]["enabled"] is True
    assert normalized["weather"]["provider"] == "open-meteo"
    assert normalized["weather"]["city"] == "上海"
    assert "token" not in normalized["weather"]
    assert normalized["weather"]["auto_locate_by_ip"] is True
    assert normalized["weather"]["last_auto_located_on"] == "2026-05-01"
    assert normalized["calendar"]["enabled"] is True
    assert normalized["calendar"]["provider"] == "timor"
