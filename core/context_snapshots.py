from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import requests

from .config_watcher import save_config
from .time_utils import local_now, local_today


_BROWSER_LIKE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


DEFAULT_CONTEXT_SNAPSHOTS_CONFIG: dict[str, Any] = {
    "weather": {
        "enabled": True,
        "provider": "caiyun",
        "city": "",
        "token": "",
        "ttl_minutes": 180,
        "timeout_seconds": 4,
    },
    "calendar": {
        "enabled": True,
        "provider": "timor",
        "ttl_minutes": 720,
        "timeout_seconds": 4,
    },
}

_CAIYUN_SKYCON_LABELS = {
    "CLEAR_DAY": "晴",
    "CLEAR_NIGHT": "晴",
    "PARTLY_CLOUDY_DAY": "多云",
    "PARTLY_CLOUDY_NIGHT": "多云",
    "CLOUDY": "阴",
    "LIGHT_HAZE": "轻度雾霾",
    "MODERATE_HAZE": "中度雾霾",
    "HEAVY_HAZE": "重度雾霾",
    "LIGHT_RAIN": "小雨",
    "MODERATE_RAIN": "中雨",
    "HEAVY_RAIN": "大雨",
    "STORM_RAIN": "暴雨",
    "FOG": "雾",
    "LIGHT_SNOW": "小雪",
    "MODERATE_SNOW": "中雪",
    "HEAVY_SNOW": "大雪",
    "STORM_SNOW": "暴雪",
    "DUST": "浮尘",
    "SAND": "沙尘",
    "WIND": "大风",
}


def get_context_snapshots_config(config: dict[str, Any]) -> dict[str, Any]:
    current = dict(config.get("context_snapshots", {}) or {})
    normalized = _deep_merge(DEFAULT_CONTEXT_SNAPSHOTS_CONFIG, current)
    normalized["weather"]["enabled"] = bool(normalized["weather"].get("enabled", True))
    normalized["weather"]["provider"] = str(normalized["weather"].get("provider", "caiyun") or "caiyun").strip() or "caiyun"
    normalized["weather"]["city"] = str(normalized["weather"].get("city", "") or "").strip()
    normalized["weather"]["token"] = str(normalized["weather"].get("token", "") or "").strip()
    normalized["weather"]["ttl_minutes"] = max(15, _to_int(normalized["weather"].get("ttl_minutes"), 180))
    normalized["weather"]["timeout_seconds"] = max(2, _to_int(normalized["weather"].get("timeout_seconds"), 4))

    normalized["calendar"]["enabled"] = bool(normalized["calendar"].get("enabled", True))
    normalized["calendar"]["provider"] = str(normalized["calendar"].get("provider", "timor") or "timor").strip() or "timor"
    normalized["calendar"]["ttl_minutes"] = max(30, _to_int(normalized["calendar"].get("ttl_minutes"), 720))
    normalized["calendar"]["timeout_seconds"] = max(2, _to_int(normalized["calendar"].get("timeout_seconds"), 4))
    return normalized


def ensure_context_snapshots(
    config: dict[str, Any],
    *,
    config_path: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    context_cfg = get_context_snapshots_config(config)
    changed = config.get("context_snapshots") != context_cfg
    config["context_snapshots"] = context_cfg

    weather_result, weather_changed = _ensure_weather_snapshot(config, context_cfg, force=force)
    calendar_result, calendar_changed = _ensure_calendar_snapshot(config, context_cfg, force=force)
    changed = changed or weather_changed or calendar_changed

    if changed and config_path:
        save_config(config, config_path)

    return {
        "ok": True,
        "config": config,
        "changed": changed,
        "weather": weather_result,
        "calendar": calendar_result,
    }


def _ensure_weather_snapshot(
    config: dict[str, Any],
    context_cfg: dict[str, Any],
    *,
    force: bool = False,
) -> tuple[dict[str, Any], bool]:
    weather_cfg = dict(context_cfg.get("weather", {}) or {})
    changed = False
    existing_snapshot = dict(config.get("weather_snapshot", {}) or {})
    existing_cfg = dict(config.get("context_snapshots", {}) or {}).get("weather", {}) or {}
    city = _resolve_weather_city(config, weather_cfg)
    token = _normalize_caiyun_token(weather_cfg.get("token"))

    if not weather_cfg.get("enabled"):
        if config.get("weather_snapshot"):
            config["weather_snapshot"] = {}
            changed = True
        weather_cfg["last_error"] = ""
        context_cfg["weather"] = weather_cfg
        return {
            "ok": True,
            "enabled": False,
            "message": "天气提醒已关闭",
            "snapshot": {},
        }, changed or existing_cfg != weather_cfg

    if not city:
        if config.get("weather_snapshot"):
            config["weather_snapshot"] = {}
            changed = True
        weather_cfg["last_error"] = "未设置天气城市"
        context_cfg["weather"] = weather_cfg
        return {
            "ok": False,
            "enabled": True,
            "message": "未设置天气城市",
            "snapshot": {},
        }, changed or existing_cfg != weather_cfg

    if not token:
        weather_cfg["city"] = city
        weather_cfg["last_error"] = "未填写彩云天气 Token"
        context_cfg["weather"] = weather_cfg
        return {
            "ok": False,
            "enabled": True,
            "message": "未填写彩云天气 Token",
            "snapshot": dict(config.get("weather_snapshot", {}) or {}),
        }, changed or existing_cfg != weather_cfg

    snapshot_city = str(existing_snapshot.get("city") or "").strip()
    should_refresh = force or snapshot_city != city or _is_snapshot_stale(existing_snapshot, weather_cfg.get("ttl_minutes"))
    if should_refresh:
        try:
            snapshot = _fetch_caiyun_weather(
                city,
                token=token,
                timeout_seconds=weather_cfg.get("timeout_seconds", 4),
            )
            config["weather_snapshot"] = snapshot
            weather_cfg["city"] = city
            weather_cfg["token"] = token
            weather_cfg["last_error"] = ""
            weather_cfg["last_updated_at"] = str(snapshot.get("updated_at") or "")
            context_cfg["weather"] = weather_cfg
            return {
                "ok": True,
                "enabled": True,
                "message": "天气快照已更新",
                "snapshot": snapshot,
            }, True
        except Exception as exc:
            weather_cfg["city"] = city
            weather_cfg["token"] = token
            weather_cfg["last_error"] = str(exc)
            context_cfg["weather"] = weather_cfg
            if snapshot_city != city and config.get("weather_snapshot"):
                config["weather_snapshot"] = {}
                changed = True
            return {
                "ok": False,
                "enabled": True,
                "message": str(exc),
                "snapshot": dict(config.get("weather_snapshot", {}) or {}),
            }, changed or existing_cfg != weather_cfg

    weather_cfg["city"] = city
    weather_cfg["token"] = token
    weather_cfg["last_error"] = ""
    weather_cfg["last_updated_at"] = str(existing_snapshot.get("updated_at") or "")
    context_cfg["weather"] = weather_cfg
    return {
        "ok": True,
        "enabled": True,
        "message": "天气快照可继续复用",
        "snapshot": existing_snapshot,
    }, existing_cfg != weather_cfg


def _ensure_calendar_snapshot(
    config: dict[str, Any],
    context_cfg: dict[str, Any],
    *,
    force: bool = False,
) -> tuple[dict[str, Any], bool]:
    calendar_cfg = dict(context_cfg.get("calendar", {}) or {})
    changed = False
    existing_snapshot = dict(config.get("calendar_snapshot", {}) or {})
    existing_cfg = dict(config.get("context_snapshots", {}) or {}).get("calendar", {}) or {}

    if not calendar_cfg.get("enabled"):
        if config.get("calendar_snapshot"):
            config["calendar_snapshot"] = {}
            changed = True
        calendar_cfg["last_error"] = ""
        context_cfg["calendar"] = calendar_cfg
        return {
            "ok": True,
            "enabled": False,
            "message": "节日提醒已关闭",
            "snapshot": {},
        }, changed or existing_cfg != calendar_cfg

    should_refresh = force or _is_snapshot_stale(existing_snapshot, calendar_cfg.get("ttl_minutes"))
    if should_refresh:
        try:
            snapshot = _fetch_timor_calendar(timeout_seconds=calendar_cfg.get("timeout_seconds", 4))
            config["calendar_snapshot"] = snapshot
            calendar_cfg["last_error"] = ""
            calendar_cfg["last_updated_at"] = str(snapshot.get("updated_at") or "")
            context_cfg["calendar"] = calendar_cfg
            return {
                "ok": True,
                "enabled": True,
                "message": "节日快照已更新",
                "snapshot": snapshot,
            }, True
        except Exception as exc:
            calendar_cfg["last_error"] = str(exc)
            context_cfg["calendar"] = calendar_cfg
            return {
                "ok": False,
                "enabled": True,
                "message": str(exc),
                "snapshot": existing_snapshot,
            }, existing_cfg != calendar_cfg

    calendar_cfg["last_error"] = ""
    calendar_cfg["last_updated_at"] = str(existing_snapshot.get("updated_at") or "")
    context_cfg["calendar"] = calendar_cfg
    return {
        "ok": True,
        "enabled": True,
        "message": "节日快照可继续复用",
        "snapshot": existing_snapshot,
    }, existing_cfg != calendar_cfg


def _resolve_weather_city(config: dict[str, Any], weather_cfg: dict[str, Any]) -> str:
    city = str(weather_cfg.get("city") or "").strip()
    if city:
        return city
    profile = config.get("profile", {}) or {}
    city = str(profile.get("city") or "").strip()
    if city:
        return city
    region_tags = []
    for task in (config.get("tasks", []) or []):
        for tag in (task.get("region_tags") or []):
            text = str(tag or "").strip()
            if text and text not in region_tags:
                region_tags.append(text)
    if len(region_tags) == 1:
        return region_tags[0]
    return ""


def _fetch_caiyun_weather(city: str, *, token: str, timeout_seconds: int = 4) -> dict[str, Any]:
    latitude, longitude, resolved_city = _geocode_city_with_open_meteo(city, timeout_seconds=timeout_seconds)
    forecast_resp = requests.get(
        f"https://api.caiyunapp.com/v2.6/{token}/{longitude},{latitude}/weather",
        params={
            "alert": "true",
            "dailysteps": 1,
            "hourlysteps": 24,
        },
        timeout=timeout_seconds,
    )
    forecast_resp.raise_for_status()
    forecast = forecast_resp.json() or {}
    if str(forecast.get("status") or "").strip().lower() != "ok":
        raise ValueError("彩云天气接口返回异常")

    result = forecast.get("result", {}) or {}
    realtime = result.get("realtime", {}) or {}
    daily = result.get("daily", {}) or {}
    daily_temperature = ((daily.get("temperature") or [None]) or [None])[0] or {}
    daily_skycon = ((daily.get("skycon") or [None]) or [None])[0] or {}

    temp_c = _to_float(realtime.get("temperature"))
    low_c = _to_float(daily_temperature.get("min"))
    high_c = _to_float(daily_temperature.get("max"))
    skycon_code = str(realtime.get("skycon") or daily_skycon.get("value") or "").strip()
    condition = _skycon_to_label(skycon_code)
    forecast_keypoint = str(result.get("forecast_keypoint") or "").strip()

    temp_bits = []
    if temp_c is not None:
        temp_bits.append(f"{round(temp_c)}°C")
    if low_c is not None and high_c is not None:
        temp_bits.append(f"{round(low_c)}~{round(high_c)}°C")
    elif high_c is not None:
        temp_bits.append(f"最高 {round(high_c)}°C")
    elif low_c is not None:
        temp_bits.append(f"最低 {round(low_c)}°C")
    summary = condition
    if temp_bits:
        summary = f"{condition} {' '.join(temp_bits)}".strip()
    if forecast_keypoint:
        summary = f"{summary} · {forecast_keypoint}".strip()

    return {
        "provider": "caiyun",
        "city": resolved_city,
        "summary": summary,
        "condition": condition,
        "temp_c": temp_c,
        "low_c": low_c,
        "high_c": high_c,
        "skycon": skycon_code,
        "forecast_keypoint": forecast_keypoint,
        "latitude": latitude,
        "longitude": longitude,
        "timezone": str(forecast.get("timezone") or "").strip(),
        "updated_at": local_now().isoformat(timespec="seconds"),
    }


def _normalize_caiyun_token(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "caiyunapp.com" not in text and "/" not in text and "?" not in text:
        return text

    match = re.search(r"/v2(?:\.\d+)?/([^/]+)/", text)
    if match:
        candidate = str(match.group(1) or "").strip()
        if candidate:
            return candidate

    try:
        parsed = urlsplit(text)
        parts = [part for part in parsed.path.split("/") if part]
        for index, part in enumerate(parts):
            if re.fullmatch(r"v2(?:\.\d+)?", part) and index + 1 < len(parts):
                candidate = str(parts[index + 1] or "").strip()
                if candidate:
                    return candidate
    except Exception:
        pass

    return text


def _geocode_city_with_open_meteo(city: str, *, timeout_seconds: int = 4) -> tuple[float, float, str]:
    geocode_resp = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={
            "name": city,
            "count": 1,
            "language": "zh",
            "format": "json",
        },
        timeout=timeout_seconds,
    )
    geocode_resp.raise_for_status()
    geocode_data = geocode_resp.json()
    result = ((geocode_data or {}).get("results") or [None])[0]
    if not isinstance(result, dict):
        raise ValueError(f"未找到天气城市：{city}")

    latitude = result.get("latitude")
    longitude = result.get("longitude")
    resolved_city = str(result.get("name") or city).strip() or city
    if latitude is None or longitude is None:
        raise ValueError(f"天气城市缺少经纬度：{city}")
    return float(latitude), float(longitude), resolved_city


def _fetch_timor_calendar(*, timeout_seconds: int = 4) -> dict[str, Any]:
    today = local_today().isoformat()
    info_data = _fetch_timor_json(
        f"/api/holiday/info/{today}",
        params={"type": "Y"},
        timeout_seconds=timeout_seconds,
    )
    if _to_int(info_data.get("code"), -1) != 0:
        raise ValueError("节日接口返回异常")

    holiday = info_data.get("holiday") or {}
    workday = info_data.get("workday") or {}

    holiday_name = ""
    is_holiday = False
    is_makeup_workday = False
    days_until: int | None = None
    holiday_type = ""

    if isinstance(holiday, dict) and holiday.get("holiday"):
        holiday_name = str(holiday.get("name") or "").strip()
        is_holiday = True
        days_until = 0
        holiday_type = "holiday"
    elif isinstance(workday, dict) and workday.get("holiday") is False and ("调休" in str(workday.get("name") or "") or str(workday.get("target") or "").strip()):
        is_makeup_workday = True
        holiday_name = str(workday.get("target") or workday.get("name") or "").strip()
        days_until = 0
        holiday_type = "makeup_workday"
    else:
        next_data = _fetch_timor_json(
            f"/api/holiday/next/{today}",
            params={"type": "Y"},
            timeout_seconds=timeout_seconds,
        )
        if _to_int(next_data.get("code"), -1) != 0:
            raise ValueError("节日接口返回异常")
        next_holiday = next_data.get("holiday") or {}
        if isinstance(next_holiday, dict) and next_holiday.get("date"):
            holiday_name = str(next_holiday.get("name") or "").strip()
            days_until = _to_int(next_holiday.get("rest"))
            holiday_type = "holiday"

    return {
        "provider": "timor",
        "date": today,
        "holiday_name": holiday_name,
        "days_until": days_until,
        "is_holiday": is_holiday,
        "is_makeup_workday": is_makeup_workday,
        "holiday_type": holiday_type,
        "updated_at": local_now().isoformat(timespec="seconds"),
    }


def _fetch_timor_json(path: str, *, params: dict[str, Any], timeout_seconds: int = 4) -> dict[str, Any]:
    last_error: Exception | None = None
    for base_url in ("http://timor.tech", "https://timor.tech"):
        try:
            response = requests.get(
                f"{base_url}{path}",
                params=params,
                headers=_BROWSER_LIKE_HEADERS,
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            return response.json() or {}
        except Exception as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    raise ValueError("节日接口请求失败")


def _is_snapshot_stale(snapshot: dict[str, Any], ttl_minutes: Any) -> bool:
    ttl = max(1, _to_int(ttl_minutes, 60))
    updated_at = _parse_datetime(snapshot.get("updated_at"))
    if not updated_at:
        return True
    return updated_at <= local_now() - timedelta(minutes=ttl)


def _skycon_to_label(code: str) -> str:
    text = str(code or "").strip().upper()
    if not text:
        return "天气未知"
    return _CAIYUN_SKYCON_LABELS.get(text, text)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _to_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default
