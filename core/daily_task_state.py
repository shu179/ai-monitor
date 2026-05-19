"""
每日任务状态

统一维护“当天任务池”：
- 兼容旧的 official/test 任务状态写法
- 新增品牌级 / 关键词级共享池，供正式任务与测试任务复用
"""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import tempfile
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .app_paths import resolve_app_path
from .file_lock import CrossProcessRLock
from .local_account_space import account_scoped_path
from .time_utils import local_now, local_today


STATE_PATH = resolve_app_path("user_data/daily_task_status.json")
_LOCK = CrossProcessRLock(lambda: _state_lock_file())

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_QUERY_FAILED = "query_failed"
STATUS_SEND_FAILED = "send_failed"
STATUS_SENT = "sent"
_VISIBLE_STATUSES = {
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    STATUS_QUERY_FAILED,
    STATUS_SEND_FAILED,
    STATUS_SENT,
}
_FAILURE_STATUSES = {
    STATUS_QUERY_FAILED,
    STATUS_SEND_FAILED,
}
_PROGRESS_EXTRA_KEYS = (
    "brands",
    "completed_keywords",
    "detected_platforms",
    "screenshot_paths",
    "actual_screenshot_count",
    "found_results",
    "image_count",
    "selected_screenshot_results",
    "fixed_screenshot_target",
    "completed_by_quota",
    "forced_ignore_failure",
)
_STATUS_LABELS = {
    STATUS_PENDING: "未运行",
    STATUS_RUNNING: "运行中",
    STATUS_SUCCESS: "成功",
    STATUS_QUERY_FAILED: "失败待补齐",
    STATUS_SEND_FAILED: "待补发",
    STATUS_SENT: "今日已发送",
}
_RUNNING_STALE_SECONDS = 1800

POOL_VERSION = 2
SOURCE_MODE_FORMAL = "formal"
SOURCE_MODE_TEST = "test"
KEYWORD_REASON_RUN_FAILED = "run_failed"
KEYWORD_REASON_SCREENSHOT_SAVE_FAILED = "screenshot_save_failed"
KEYWORD_REASON_NOT_RUN = "not_run"
BRAND_STATUS_PENDING = "pending"
BRAND_STATUS_RUNNING = "running"
BRAND_STATUS_GAP = "gap"
BRAND_STATUS_SUCCESS = "success"
BRAND_STATUS_SEND_FAILED = "send_failed"
BRAND_STATUS_SENT = "sent"


def _dedupe_text_list(values: list | tuple | set | None) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
    return items


def _today_text(target_date: date | None = None) -> str:
    return (target_date or local_today()).isoformat()


def _normalize_task(task: dict) -> dict:
    normalized = deepcopy(task or {})
    if "keyword" in normalized and "keywords" not in normalized:
        normalized["keywords"] = [{
            "keyword": normalized.get("keyword", ""),
            "brand": normalized.get("brand", ""),
            "platforms": [normalized.get("platform", "")],
            "mode": normalized.get("mode", "browser"),
        }]
    return normalized


def derive_task_id(task: dict) -> str:
    normalized = _normalize_task(task)
    explicit = str(normalized.get("task_id") or "").strip()
    if explicit:
        return explicit

    signature = {
        "name": str(normalized.get("name") or "").strip(),
        "keywords": [],
    }
    keyword_entries = list(normalized.get("keywords", []) or [])
    if not keyword_entries:
        keyword_entries = list(normalized.get("guide_keywords", []) or [])
    for kw in keyword_entries:
        signature["keywords"].append({
            "keyword": str(kw.get("keyword") or "").strip(),
            "brand": str(kw.get("brand") or "").strip(),
            "platforms": sorted(str(p).strip() for p in (kw.get("platforms") or []) if str(p).strip()),
            "mode": str(kw.get("mode") or "browser").strip(),
        })

    raw = json.dumps(signature, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"legacy_{digest}"


def assign_task_id(task: dict) -> str:
    task_id = str((task or {}).get("task_id") or "").strip()
    if task_id:
        return task_id

    task_id = derive_task_id(task)
    task["task_id"] = task_id
    return task_id


def ensure_config_task_ids(config: dict) -> bool:
    changed = False
    for task in (config or {}).get("tasks", []) or []:
        if not str(task.get("task_id") or "").strip():
            assign_task_id(task)
            changed = True
    return changed


def _load_state() -> dict:
    state_path = _state_path()
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[DailyState] 读取状态失败: {e}")
        return {}


def _save_state(data: dict) -> None:
    state_path = _state_path()
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(state_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, state_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        print(f"[DailyState] 保存状态失败: {e}")


def _state_path() -> Path:
    resolved_default = resolve_app_path("user_data/daily_task_status.json")
    if STATE_PATH != resolved_default:
        return STATE_PATH
    return account_scoped_path("user_data/daily_task_status.json", fallback=resolved_default)


def _state_lock_file() -> Path:
    state_path = _state_path()
    return state_path.with_name(f"{state_path.name}.lock")


def get_state_path() -> Path:
    return _state_path()


def _make_day_key(task: dict, target_date: date | None = None) -> str:
    return f"{derive_task_id(task)}#{_today_text(target_date)}"


def _legacy_single_mode_day_key(task: dict, target_date: date | None = None) -> str | None:
    normalized = _normalize_task(task)
    modes: list[str] = []
    keyword_entries = list(normalized.get("keywords", []) or [])
    if not keyword_entries:
        keyword_entries = list(normalized.get("guide_keywords", []) or [])
    for kw in keyword_entries:
        mode = str(kw.get("mode") or normalized.get("mode") or "browser").strip() or "browser"
        if mode == "recognition":
            continue
        if mode not in modes:
            modes.append(mode)
    if len(modes) != 1:
        return None
    return f"{derive_task_id(task)}::{modes[0]}#{_today_text(target_date)}"


def _prune_old_entries(data: dict, keep_days: int = 45) -> dict:
    cutoff = local_today().toordinal() - max(keep_days, 7)
    pruned = {}
    for key, value in data.items():
        date_text = str(key).rsplit("#", 1)[-1]
        try:
            day_value = date.fromisoformat(date_text).toordinal()
        except Exception:
            pruned[key] = value
            continue
        if day_value >= cutoff:
            pruned[key] = value
    return pruned


def _default_status_payload() -> dict:
    return {
        "status": STATUS_PENDING,
        "source": "",
        "updated_at": "",
        "message": "",
    }


def _parse_local_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except Exception:
        return None
    local_tz = local_now().tzinfo
    if dt.tzinfo is None:
        return dt.replace(tzinfo=local_tz) if local_tz is not None else dt
    return dt.astimezone(local_tz) if local_tz is not None else dt.astimezone()


def _elapsed_since_local_datetime(value: Any) -> float | None:
    parsed = _parse_local_datetime(value)
    if parsed is None:
        return None
    try:
        return (local_now() - parsed).total_seconds()
    except Exception:
        return None


def _is_stale_running_timestamp(value: Any) -> bool:
    elapsed_seconds = _elapsed_since_local_datetime(value)
    return elapsed_seconds is not None and elapsed_seconds >= _RUNNING_STALE_SECONDS


def _normalize_status_payload(entry: dict | None, *, allow_manual_test_failure: bool = False) -> dict:
    if not isinstance(entry, dict):
        return _default_status_payload()

    normalized = dict(entry)
    source = str(normalized.get("source") or "").strip()
    status = str(normalized.get("status") or "").strip()
    if source == "manual_test" and status != STATUS_SUCCESS and not allow_manual_test_failure:
        return _default_status_payload()

    extra = normalized.get("extra") if isinstance(normalized.get("extra"), dict) else {}
    if status == "failed":
        status = STATUS_SEND_FAILED if str(extra.get("task_failure_kind") or "").strip() == "notification" else STATUS_QUERY_FAILED
        normalized["status"] = status
    elif status == STATUS_RUNNING:
        if _is_stale_running_timestamp(normalized.get("updated_at")):
            normalized["status"] = STATUS_QUERY_FAILED
            normalized["message"] = str(normalized.get("message") or "").strip() or "任务执行超时，等待后续补跑"
            extra = dict(extra)
            if not str(extra.get("task_failure_kind") or "").strip():
                extra["task_failure_kind"] = "temporary"
            normalized["extra"] = extra
    elif status not in _VISIBLE_STATUSES:
        normalized["status"] = status or STATUS_PENDING
    return normalized


def _split_status_entry(entry: dict | None) -> tuple[dict, dict]:
    if not isinstance(entry, dict):
        return _default_status_payload(), _default_status_payload()
    if "official" in entry or "test" in entry:
        official = _normalize_status_payload(entry.get("official"))
        test = _normalize_status_payload(entry.get("test"), allow_manual_test_failure=True)
        return official, test

    normalized = _normalize_status_payload(entry, allow_manual_test_failure=True)
    source = str(normalized.get("source") or "").strip()
    if source == "manual_test":
        official = _normalize_status_payload(entry)
        return official, normalized
    return _normalize_status_payload(entry), _default_status_payload()


def _extract_retained_failure(entry: dict | None) -> dict:
    if not isinstance(entry, dict):
        return {}

    status = str(entry.get("status") or "").strip()
    extra = entry.get("extra") if isinstance(entry.get("extra"), dict) else {}
    if status in _FAILURE_STATUSES:
        return {
            "status": status,
            "message": str(entry.get("message") or "").strip(),
            "updated_at": str(entry.get("updated_at") or "").strip(),
            "failure_kind": str(extra.get("task_failure_kind") or "").strip(),
        }

    retained_status = str(extra.get("previous_failure_status") or "").strip()
    if retained_status in _FAILURE_STATUSES:
        return {
            "status": retained_status,
            "message": str(extra.get("previous_failure_message") or "").strip(),
            "updated_at": str(extra.get("previous_failure_updated_at") or "").strip(),
            "failure_kind": str(extra.get("previous_failure_kind") or "").strip(),
        }
    return {}


def _merge_retained_failure(extra: dict | None, retained_failure: dict | None) -> dict | None:
    payload = dict(extra or {})
    for key in (
        "previous_failure_status",
        "previous_failure_message",
        "previous_failure_updated_at",
        "previous_failure_kind",
    ):
        payload.pop(key, None)

    retained = dict(retained_failure or {})
    retained_status = str(retained.get("status") or "").strip()
    if retained_status in _FAILURE_STATUSES:
        payload["previous_failure_status"] = retained_status
        payload["previous_failure_message"] = str(retained.get("message") or "").strip()
        payload["previous_failure_updated_at"] = str(retained.get("updated_at") or "").strip()
        payload["previous_failure_kind"] = str(retained.get("failure_kind") or "").strip()

    return payload or None


def _merge_retained_progress(extra: dict | None, existing_extra: dict | None) -> dict | None:
    payload = dict(extra or {})
    source = dict(existing_extra or {})
    for key in _PROGRESS_EXTRA_KEYS:
        if key in payload:
            continue
        if key in source:
            payload[key] = source[key]
    return payload or None


def _normalize_keyword_key(value: Any) -> str:
    return str(value or "").strip()


def _infer_primary_brand(task: dict | None, keyword_entries: list[dict] | None = None) -> str:
    task_payload = dict(task or {})
    brand = str(task_payload.get("brand") or "").strip()
    if brand:
        return brand
    for item in keyword_entries or []:
        candidate = str((item or {}).get("brand") or "").strip()
        if candidate:
            return candidate
    return str(task_payload.get("name") or "").strip()


def _extract_required_keyword_entries(task: dict | None) -> list[dict]:
    normalized = _normalize_task(task or {})
    items: list[dict] = []
    seen: set[str] = set()
    keyword_entries = list(normalized.get("keywords", []) or [])
    if not keyword_entries:
        keyword_entries = list(normalized.get("guide_keywords", []) or [])
    for kw in keyword_entries:
        keyword = _normalize_keyword_key(kw.get("keyword"))
        if not keyword or keyword in seen:
            continue
        seen.add(keyword)
        items.append({
            "keyword": keyword,
            "brand": str(kw.get("brand") or normalized.get("brand") or normalized.get("name") or "").strip(),
            "platforms": [str(p).strip() for p in (kw.get("platforms") or []) if str(p).strip()],
            "mode": str(kw.get("mode") or normalized.get("mode") or "browser").strip() or "browser",
        })
    return items


def _default_keyword_state(keyword: str, brand: str = "", platforms: list[str] | None = None) -> dict:
    return {
        "keyword": str(keyword or "").strip(),
        "brand": str(brand or "").strip(),
        "run_success": False,
        "screenshot_saved": False,
        "failure_reason": KEYWORD_REASON_NOT_RUN,
        "source_mode": "",
        "image_path": "",
        "platform": "",
        "required_platforms": _dedupe_text_list(platforms or []),
        "platform_states": {},
        "updated_at": "",
    }


def _default_task_pool(task: dict | None = None, target_date: date | None = None) -> dict:
    required_entries = _extract_required_keyword_entries(task)
    required_keywords = [item["keyword"] for item in required_entries]
    primary_brand = _infer_primary_brand(task, required_entries)
    keyword_states = {
        item["keyword"]: _default_keyword_state(
            item["keyword"],
            item.get("brand") or primary_brand,
            item.get("platforms") or [],
        )
        for item in required_entries
    }
    return {
        "version": POOL_VERSION,
        "task_id": str((task or {}).get("task_id") or derive_task_id(task or {})).strip() if task else "",
        "task_name": str((task or {}).get("name") or primary_brand or "").strip(),
        "brand_name": primary_brand,
        "date": _today_text(target_date),
        "brand": {
            "required_keywords": list(required_keywords),
            "formal_started": False,
            "formal_running": False,
            "has_gap": bool(required_keywords),
            "gap_reasons": [],
            "sent_today": False,
            "sent_at": "",
            "status_message": "",
            "source_mode": "",
            "updated_at": "",
            "last_send_error": "",
        },
        "keywords": keyword_states,
    }


def _coerce_bool(value: Any) -> bool:
    return bool(value)


def _coerce_keyword_state(raw: Any, *, keyword: str, brand: str = "", platforms: list[str] | None = None) -> dict:
    state = _default_keyword_state(keyword, brand, platforms)
    if not isinstance(raw, dict):
        return state
    required_platforms = _dedupe_text_list(platforms if platforms is not None else raw.get("required_platforms") or [])
    platform_states = {}
    raw_platform_states = raw.get("platform_states") if isinstance(raw.get("platform_states"), dict) else {}
    for platform_name, platform_state in dict(raw_platform_states or {}).items():
        platform_text = str(platform_name or "").strip()
        if not platform_text:
            continue
        platform_states[platform_text] = _coerce_keyword_state(
            platform_state,
            keyword=keyword,
            brand=brand,
            platforms=[],
        )
        platform_states[platform_text]["platform"] = platform_text
        platform_states[platform_text]["required_platforms"] = []
        platform_states[platform_text]["platform_states"] = {}
    run_success = _coerce_bool(raw.get("run_success"))
    screenshot_saved = _coerce_bool(raw.get("screenshot_saved"))
    failure_reason = str(raw.get("failure_reason") or "").strip()
    if run_success and screenshot_saved:
        failure_reason = ""
    elif run_success and not failure_reason:
        failure_reason = KEYWORD_REASON_SCREENSHOT_SAVE_FAILED
    elif not run_success and not failure_reason:
        failure_reason = KEYWORD_REASON_NOT_RUN
    state.update({
        "keyword": _normalize_keyword_key(raw.get("keyword") or keyword),
        "brand": str(raw.get("brand") or brand).strip(),
        "run_success": run_success,
        "screenshot_saved": screenshot_saved,
        "failure_reason": failure_reason,
        "source_mode": str(raw.get("source_mode") or "").strip(),
        "image_path": str(raw.get("image_path") or "").strip(),
        "platform": str(raw.get("platform") or "").strip(),
        "required_platforms": required_platforms,
        "platform_states": platform_states,
        "updated_at": str(raw.get("updated_at") or "").strip(),
    })
    return state


def _is_keyword_complete(keyword_state: dict | None) -> bool:
    if not isinstance(keyword_state, dict):
        return False
    required_platforms = _dedupe_text_list(keyword_state.get("required_platforms") or [])
    if required_platforms:
        platform_states = dict(keyword_state.get("platform_states") or {})
        return all(_is_keyword_complete(platform_states.get(platform)) for platform in required_platforms)
    return bool(keyword_state.get("run_success")) and bool(keyword_state.get("screenshot_saved"))


def _normalize_gap_details(pool: dict) -> list[dict]:
    brand = dict(pool.get("brand") or {})
    keywords = dict(pool.get("keywords") or {})
    details: list[dict] = []
    for keyword in list(brand.get("required_keywords") or []):
        state = _coerce_keyword_state(
            keywords.get(keyword),
            keyword=keyword,
            brand=_infer_primary_brand({"brand": pool.get("brand_name", "")}),
        )
        required_platforms = _dedupe_text_list(state.get("required_platforms") or [])
        if required_platforms:
            platform_states = dict(state.get("platform_states") or {})
            for platform_name in required_platforms:
                platform_state = _coerce_keyword_state(
                    platform_states.get(platform_name),
                    keyword=keyword,
                    brand=str(state.get("brand") or pool.get("brand_name") or "").strip(),
                    platforms=[],
                )
                if _is_keyword_complete(platform_state):
                    continue
                reason = str(platform_state.get("failure_reason") or KEYWORD_REASON_NOT_RUN).strip() or KEYWORD_REASON_NOT_RUN
                details.append({
                    "keyword": keyword,
                    "brand": str(platform_state.get("brand") or state.get("brand") or pool.get("brand_name") or "").strip(),
                    "reason": reason,
                    "source_mode": str(platform_state.get("source_mode") or state.get("source_mode") or "").strip(),
                    "image_path": str(platform_state.get("image_path") or "").strip(),
                    "platform": platform_name,
                    "updated_at": str(platform_state.get("updated_at") or state.get("updated_at") or "").strip(),
                })
            continue
        if _is_keyword_complete(state):
            continue
        reason = str(state.get("failure_reason") or KEYWORD_REASON_NOT_RUN).strip() or KEYWORD_REASON_NOT_RUN
        details.append({
            "keyword": keyword,
            "brand": str(state.get("brand") or pool.get("brand_name") or "").strip(),
            "reason": reason,
            "source_mode": str(state.get("source_mode") or "").strip(),
            "image_path": str(state.get("image_path") or "").strip(),
            "platform": str(state.get("platform") or "").strip(),
            "updated_at": str(state.get("updated_at") or "").strip(),
        })
    return details


def _format_gap_reasons(gap_details: list[dict]) -> list[str]:
    labels = {
        KEYWORD_REASON_NOT_RUN: "未运行",
        KEYWORD_REASON_RUN_FAILED: "执行失败",
        KEYWORD_REASON_SCREENSHOT_SAVE_FAILED: "截图保存失败",
    }
    return [
        f"{str(item.get('keyword') or '').strip()}：{labels.get(str(item.get('reason') or '').strip(), '未完成')}"
        for item in gap_details
        if str(item.get("keyword") or "").strip()
    ]


def _derive_brand_status(pool: dict) -> str:
    brand = dict(pool.get("brand") or {})
    if _coerce_bool(brand.get("sent_today")):
        return BRAND_STATUS_SENT
    if _coerce_bool(brand.get("formal_running")):
        return BRAND_STATUS_RUNNING
    if str(brand.get("last_send_error") or "").strip():
        return BRAND_STATUS_SEND_FAILED
    if _coerce_bool(brand.get("formal_started")) and _coerce_bool(brand.get("has_gap")):
        return BRAND_STATUS_GAP
    required_keywords = list(brand.get("required_keywords") or [])
    if required_keywords and all(
        _is_keyword_complete((pool.get("keywords") or {}).get(keyword))
        for keyword in required_keywords
    ):
        return BRAND_STATUS_SUCCESS
    return BRAND_STATUS_PENDING


def _legacy_status_from_brand_status(brand_status: str) -> str:
    normalized = str(brand_status or "").strip()
    if normalized == BRAND_STATUS_RUNNING:
        return STATUS_RUNNING
    if normalized == BRAND_STATUS_GAP:
        return STATUS_QUERY_FAILED
    if normalized == BRAND_STATUS_SUCCESS:
        return STATUS_SUCCESS
    if normalized == BRAND_STATUS_SEND_FAILED:
        return STATUS_SEND_FAILED
    if normalized == BRAND_STATUS_SENT:
        return STATUS_SENT
    return STATUS_PENDING


def _pool_extra(pool: dict, *, brand_status: str) -> dict:
    brand = dict(pool.get("brand") or {})
    keyword_map = dict(pool.get("keywords") or {})
    required_keywords = list(brand.get("required_keywords") or [])
    completed_keywords = [
        keyword for keyword in required_keywords
        if _is_keyword_complete(keyword_map.get(keyword))
    ]
    detected_platform_values = []
    screenshot_path_values = []
    for keyword in required_keywords:
        state = dict(keyword_map.get(keyword) or {})
        platform_states = dict(state.get("platform_states") or {})
        if platform_states:
            for platform_name, platform_state in platform_states.items():
                if not _is_keyword_complete(platform_state):
                    continue
                detected_platform_values.append(str(platform_name or (platform_state or {}).get("platform") or "").strip())
                screenshot_path_values.append(str((platform_state or {}).get("image_path") or "").strip())
            continue
        if _is_keyword_complete(state):
            detected_platform_values.append(str(state.get("platform") or "").strip())
            screenshot_path_values.append(str(state.get("image_path") or "").strip())
    detected_platforms = _dedupe_text_list(detected_platform_values)
    screenshot_paths = _dedupe_text_list(screenshot_path_values)
    return {
        "brand_status": brand_status,
        "required_keywords": list(required_keywords),
        "completed_keywords": list(completed_keywords),
        "detected_platforms": list(detected_platforms),
        "actual_screenshot_count": len(screenshot_paths),
        "sent_today": bool(brand.get("sent_today")),
        "sent_at": str(brand.get("sent_at") or "").strip(),
        "formal_started": bool(brand.get("formal_started")),
        "formal_running": bool(brand.get("formal_running")),
        "has_gap": bool(brand.get("has_gap")),
        "gap_reasons": list(brand.get("gap_reasons") or []),
        "gap_details": _normalize_gap_details(pool),
        "status_message": str(brand.get("status_message") or "").strip(),
        "last_send_error": str(brand.get("last_send_error") or "").strip(),
        "keyword_states": deepcopy(keyword_map),
    }


def _ensure_pool_shape(pool: dict | None, task: dict | None, target_date: date | None = None) -> dict:
    normalized = deepcopy(pool) if isinstance(pool, dict) else _default_task_pool(task, target_date)
    default_pool = _default_task_pool(task, target_date)

    if int(normalized.get("version") or 0) < POOL_VERSION:
        normalized["version"] = POOL_VERSION
    normalized["task_id"] = str(normalized.get("task_id") or default_pool.get("task_id") or "").strip()
    normalized["task_name"] = str(normalized.get("task_name") or default_pool.get("task_name") or "").strip()
    normalized["brand_name"] = str(normalized.get("brand_name") or default_pool.get("brand_name") or "").strip()
    normalized["date"] = str(normalized.get("date") or default_pool.get("date") or _today_text(target_date)).strip()

    brand = dict(default_pool.get("brand") or {})
    brand.update(dict(normalized.get("brand") or {}))
    required_entries = _extract_required_keyword_entries(task)
    if required_entries:
        brand["required_keywords"] = [item["keyword"] for item in required_entries]
    else:
        brand["required_keywords"] = _dedupe_text_list(brand.get("required_keywords"))
    if _coerce_bool(brand.get("formal_running")) and _is_stale_running_timestamp(brand.get("updated_at")):
        brand["formal_running"] = False
        brand["status_message"] = str(brand.get("status_message") or "").strip() or "任务执行超时，等待后续补跑"

    keyword_states = {}
    existing_keywords = dict(normalized.get("keywords") or {})
    required_brand_map = {item["keyword"]: item.get("brand") or normalized.get("brand_name") or "" for item in required_entries}
    required_platform_map = {item["keyword"]: list(item.get("platforms") or []) for item in required_entries}
    for keyword in list(brand.get("required_keywords") or []):
        keyword_states[keyword] = _coerce_keyword_state(
            existing_keywords.get(keyword),
            keyword=keyword,
            brand=required_brand_map.get(keyword, normalized.get("brand_name", "")),
            platforms=required_platform_map.get(keyword, []),
        )
    for keyword, raw_state in existing_keywords.items():
        normalized_keyword = _normalize_keyword_key(keyword)
        if not normalized_keyword or normalized_keyword in keyword_states:
            continue
        keyword_states[normalized_keyword] = _coerce_keyword_state(
            raw_state,
            keyword=normalized_keyword,
            brand=str((raw_state or {}).get("brand") or normalized.get("brand_name") or "").strip(),
        )

    brand["has_gap"] = bool(_normalize_gap_details({
        "brand": brand,
        "brand_name": normalized.get("brand_name", ""),
        "keywords": keyword_states,
    }))
    brand["gap_reasons"] = _format_gap_reasons(_normalize_gap_details({
        "brand": brand,
        "brand_name": normalized.get("brand_name", ""),
        "keywords": keyword_states,
    }))
    normalized["brand"] = brand
    normalized["keywords"] = keyword_states
    return normalized


def _build_pool_from_legacy_entry(entry: dict | None, task: dict | None, target_date: date | None = None) -> dict:
    pool = _default_task_pool(task, target_date)
    official, test = _split_status_entry(entry)
    legacy_payload = official
    if str(official.get("status") or "").strip() == STATUS_PENDING and str(test.get("status") or "").strip() != STATUS_PENDING:
        legacy_payload = test

    extra = dict(legacy_payload.get("extra") or {}) if isinstance(legacy_payload.get("extra"), dict) else {}
    source = str(legacy_payload.get("source") or "").strip()
    message = str(legacy_payload.get("message") or "").strip()
    updated_at = str(legacy_payload.get("updated_at") or "").strip()
    brand = dict(pool.get("brand") or {})
    brand["source_mode"] = SOURCE_MODE_TEST if source == "manual_test" else SOURCE_MODE_FORMAL
    brand["updated_at"] = updated_at
    brand["status_message"] = message
    brand["last_send_error"] = message if str(extra.get("task_failure_kind") or "").strip() == "notification" else ""

    completed_keywords = _dedupe_text_list(extra.get("completed_keywords"))
    for keyword in completed_keywords:
        if keyword not in pool["keywords"]:
            pool["keywords"][keyword] = _default_keyword_state(keyword, pool.get("brand_name", ""))
        pool["keywords"][keyword].update({
            "run_success": True,
            "screenshot_saved": True,
            "failure_reason": "",
            "source_mode": brand["source_mode"],
            "updated_at": updated_at,
        })

    legacy_status = str(legacy_payload.get("status") or "").strip()
    if legacy_status in {STATUS_RUNNING}:
        brand["formal_started"] = brand["source_mode"] != SOURCE_MODE_TEST
        brand["formal_running"] = brand["source_mode"] != SOURCE_MODE_TEST
    elif legacy_status in {STATUS_SUCCESS, STATUS_SENT}:
        for keyword in list(brand.get("required_keywords") or []):
            if keyword not in pool["keywords"]:
                pool["keywords"][keyword] = _default_keyword_state(keyword, pool.get("brand_name", ""))
            pool["keywords"][keyword].update({
                "run_success": True,
                "screenshot_saved": True,
                "failure_reason": "",
                "source_mode": brand["source_mode"],
                "updated_at": updated_at,
            })
        brand["formal_started"] = True
        brand["formal_running"] = False
        brand["sent_today"] = bool(extra.get("notification_success", True))
        brand["sent_at"] = updated_at if brand["sent_today"] else ""
    elif legacy_status in {STATUS_QUERY_FAILED, STATUS_SEND_FAILED}:
        brand["formal_started"] = brand["source_mode"] != SOURCE_MODE_TEST
        brand["formal_running"] = False

    pool["brand"] = brand
    return _ensure_pool_shape(pool, task, target_date)


def _default_scoped_entry(task: dict | None = None, target_date: date | None = None) -> dict:
    return {
        "official": _default_status_payload(),
        "test": _default_status_payload(),
        "pool": _default_task_pool(task, target_date),
        "test_pool": _default_task_pool(task, target_date),
    }


def _extract_pool(entry: dict | None, task: dict | None, target_date: date | None = None) -> dict:
    if isinstance(entry, dict) and isinstance(entry.get("pool"), dict):
        return _ensure_pool_shape(entry.get("pool"), task, target_date)
    if isinstance(entry, dict):
        return _build_pool_from_legacy_entry(entry, task, target_date)
    return _default_task_pool(task, target_date)


def _extract_test_pool(entry: dict | None, task: dict | None, target_date: date | None = None) -> dict:
    if isinstance(entry, dict) and isinstance(entry.get("test_pool"), dict):
        return _ensure_pool_shape(entry.get("test_pool"), task, target_date)
    return _default_task_pool(task, target_date)


def _load_entry(task: dict, target_date: date | None = None) -> dict | None:
    with _LOCK:
        state = _load_state()
    entry = state.get(_make_day_key(task, target_date))
    if entry is None:
        legacy_key = _legacy_single_mode_day_key(task, target_date)
        if legacy_key:
            entry = state.get(legacy_key)
    return entry


def get_task_day_pool(task: dict, target_date: date | None = None) -> dict:
    return _extract_pool(_load_entry(task, target_date), task, target_date)


def get_task_test_day_pool(task: dict, target_date: date | None = None) -> dict:
    return _extract_test_pool(_load_entry(task, target_date), task, target_date)


def get_task_day_status(task: dict, target_date: date | None = None) -> dict:
    entry = _load_entry(task, target_date)
    official, test = _split_status_entry(entry)
    pool = _extract_pool(entry, task, target_date)
    test_pool = _extract_test_pool(entry, task, target_date)
    brand = dict(pool.get("brand") or {})
    brand_status = _derive_brand_status(pool)
    legacy_status = _legacy_status_from_brand_status(brand_status)
    extra = _pool_extra(pool, brand_status=brand_status)
    test_brand = dict(test_pool.get("brand") or {})
    test_brand_status = _derive_brand_status(test_pool)
    test_legacy_status = _legacy_status_from_brand_status(test_brand_status)
    test_extra = _pool_extra(test_pool, brand_status=test_brand_status)

    message = str(brand.get("status_message") or "").strip()
    if not message and legacy_status == STATUS_QUERY_FAILED:
        message = "当前仍有关键词缺口待补齐"
    if not message and legacy_status == STATUS_SENT:
        message = "今日已发送"

    test_message = str(test.get("message") or test_brand.get("status_message") or "").strip()
    if not test_message and test_legacy_status == STATUS_QUERY_FAILED:
        test_message = "当前测试任务仍有关键词缺口待补齐"
    if not test_message and test_legacy_status == STATUS_SENT:
        test_message = "测试任务今日已发送"
    test_has_started = bool(
        str(test.get("updated_at") or test_brand.get("updated_at") or "").strip()
        or str(test.get("source") or test_brand.get("source_mode") or "").strip()
        or list(test_extra.get("completed_keywords") or [])
        or int(test_extra.get("actual_screenshot_count") or 0) > 0
    )

    payload = {
        "status": legacy_status,
        "source": str(brand.get("source_mode") or "").strip(),
        "updated_at": str(brand.get("updated_at") or "").strip(),
        "message": message,
        "extra": extra,
        "official_status": str(official.get("status") or legacy_status).strip() or legacy_status,
        "official_source": str(official.get("source") or brand.get("source_mode") or "").strip(),
        "official_updated_at": str(official.get("updated_at") or brand.get("updated_at") or "").strip(),
        "official_message": str(official.get("message") or message).strip(),
        "official_extra": dict(official.get("extra") or extra) if isinstance(official.get("extra"), dict) else dict(extra),
        "test_status": str(test.get("status") or test_legacy_status).strip() or test_legacy_status,
        "test_source": str(test.get("source") or test_brand.get("source_mode") or "").strip(),
        "test_updated_at": str(test.get("updated_at") or test_brand.get("updated_at") or "").strip(),
        "test_message": test_message,
        "test_extra": dict(test.get("extra") or test_extra) if isinstance(test.get("extra"), dict) else dict(test_extra),
        "brand_status": brand_status,
        "brand_status_label": _STATUS_LABELS.get(_legacy_status_from_brand_status(brand_status), "未知状态"),
        "sent_today": bool(brand.get("sent_today")),
        "sent_at": str(brand.get("sent_at") or "").strip(),
        "formal_started": bool(brand.get("formal_started")),
        "formal_running": bool(brand.get("formal_running")),
        "has_gap": bool(brand.get("has_gap")),
        "gap_reasons": list(brand.get("gap_reasons") or []),
        "required_keywords": list(brand.get("required_keywords") or []),
        "completed_keywords": list(extra.get("completed_keywords") or []),
        "detected_platforms": list(extra.get("detected_platforms") or []),
        "actual_screenshot_count": int(extra.get("actual_screenshot_count") or 0),
        "keyword_states": deepcopy(pool.get("keywords") or {}),
        "pool": deepcopy(pool),
        "test_brand_status": test_brand_status,
        "test_brand_status_label": _STATUS_LABELS.get(test_legacy_status, "未知状态"),
        "test_has_gap": bool(test_brand.get("has_gap")) if test_has_started else False,
        "test_gap_reasons": list(test_brand.get("gap_reasons") or []) if test_has_started else [],
        "test_completed_keywords": list(test_extra.get("completed_keywords") or []),
        "test_detected_platforms": list(test_extra.get("detected_platforms") or []),
        "test_actual_screenshot_count": int(test_extra.get("actual_screenshot_count") or 0),
        "test_keyword_states": deepcopy(test_pool.get("keywords") or {}),
        "test_pool": deepcopy(test_pool),
    }
    return payload


def get_task_test_status(task: dict, target_date: date | None = None) -> dict:
    status = get_task_day_status(task, target_date)
    return {
        "status": str(status.get("test_status") or "").strip(),
        "source": str(status.get("test_source") or "").strip(),
        "updated_at": str(status.get("test_updated_at") or "").strip(),
        "message": str(status.get("test_message") or "").strip(),
        "extra": dict(status.get("test_extra") or {}) if isinstance(status.get("test_extra"), dict) else {},
    }


def has_task_success(task: dict, target_date: date | None = None) -> bool:
    brand_status = str(get_task_day_status(task, target_date).get("brand_status") or "").strip()
    return brand_status in {BRAND_STATUS_SUCCESS, BRAND_STATUS_SENT}


def is_task_sent_today(task: dict, target_date: date | None = None) -> bool:
    status = get_task_day_status(task, target_date)
    return bool(status.get("sent_today"))


def get_task_status_label(status: str) -> str:
    return _STATUS_LABELS.get(str(status or "").strip(), "未知状态")


def build_task_state_extra(
    *,
    brands: list[str] | None = None,
    completed_keywords: list[str] | None = None,
    detected_platforms: list[str] | None = None,
    supplemented_keywords: list[str] | None = None,
    found_results: int | None = None,
    image_count: int | None = None,
    selected_screenshot_results: int | None = None,
    fixed_screenshot_target: int | None = None,
    completed_by_quota: bool | None = None,
    failed_queries: int | None = None,
    query_round_status: str | None = None,
    task_status: str | None = None,
    task_failure_kind: str | None = None,
    notification_success: bool | None = None,
    forced_ignore_failure: bool | None = None,
    extra: dict | None = None,
) -> dict:
    payload = dict(extra or {})

    if brands is not None:
        payload["brands"] = _dedupe_text_list(brands)
    if completed_keywords is not None:
        payload["completed_keywords"] = _dedupe_text_list(completed_keywords)
    if detected_platforms is not None:
        payload["detected_platforms"] = _dedupe_text_list(detected_platforms)
    if supplemented_keywords is not None:
        payload["supplemented_keywords"] = _dedupe_text_list(supplemented_keywords)
    if found_results is not None:
        payload["found_results"] = max(0, int(found_results or 0))
    if image_count is not None:
        payload["image_count"] = max(0, int(image_count or 0))
    if selected_screenshot_results is not None:
        payload["selected_screenshot_results"] = max(0, int(selected_screenshot_results or 0))
    if fixed_screenshot_target is not None:
        payload["fixed_screenshot_target"] = max(0, int(fixed_screenshot_target or 0))
    if completed_by_quota is not None:
        payload["completed_by_quota"] = bool(completed_by_quota)
    if failed_queries is not None:
        payload["failed_queries"] = max(0, int(failed_queries or 0))
    if query_round_status is not None:
        payload["query_round_status"] = str(query_round_status or "").strip()
    if task_status is not None:
        payload["task_status"] = str(task_status or "").strip()
    if task_failure_kind is not None:
        payload["task_failure_kind"] = str(task_failure_kind or "").strip()
    if notification_success is not None:
        payload["notification_success"] = bool(notification_success)
    if forced_ignore_failure is not None:
        payload["forced_ignore_failure"] = bool(forced_ignore_failure)
    return payload


def _move_file_if_needed(source_path: str, target_path: str) -> str:
    source = Path(str(source_path or "").strip())
    target = Path(str(target_path or "").strip())
    if not source.exists() or not target:
        return str(source_path or "").strip()
    if source.resolve() == target.resolve():
        return str(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            if target.samefile(source):
                return str(target)
        except Exception:
            pass
        try:
            os.replace(str(source), str(target))
            return str(target)
        except Exception:
            try:
                shutil.copy2(str(source), str(target))
                source.unlink()
                return str(target)
            except Exception:
                return str(source)
    try:
        shutil.move(str(source), str(target))
        return str(target)
    except Exception:
        return str(source)


def build_canonical_screenshot_path(
    task: dict,
    *,
    keyword: str,
    brand: str = "",
    platform: str = "",
    source_mode: str = SOURCE_MODE_FORMAL,
    original_path: str = "",
) -> str:
    original = Path(str(original_path or "").strip()) if str(original_path or "").strip() else None
    suffix = original.suffix if original and original.suffix else ".jpg"
    date_text = _today_text().replace("-", "")
    safe_brand = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(brand or task.get("brand") or task.get("name") or "").strip())
    safe_keyword = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(keyword or "").strip())
    safe_platform = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(platform or "unknown").strip())
    mode_suffix = "_test" if str(source_mode or "").strip() == SOURCE_MODE_TEST else ""
    filename = f"{date_text}_{safe_brand}_{safe_keyword}_{safe_platform}{mode_suffix}{suffix}"
    return str(resolve_app_path("screenshots") / filename)


def normalize_task_screenshot_path(
    task: dict,
    *,
    keyword: str,
    brand: str = "",
    platform: str = "",
    source_mode: str = SOURCE_MODE_FORMAL,
    original_path: str = "",
) -> str:
    target_path = build_canonical_screenshot_path(
        task,
        keyword=keyword,
        brand=brand,
        platform=platform,
        source_mode=source_mode,
        original_path=original_path,
    )
    return _move_file_if_needed(original_path, target_path)


def _with_pool_lock(
    task: dict,
    *,
    target_date: date | None = None,
) -> tuple[dict, str, dict]:
    state = _prune_old_entries(_load_state())
    key = _make_day_key(task, target_date)
    entry = state.get(key)
    if entry is None:
        legacy_key = _legacy_single_mode_day_key(task, target_date)
        if legacy_key and legacy_key in state:
            entry = state.pop(legacy_key)
    scoped_entry = _default_scoped_entry(task, target_date)
    if isinstance(entry, dict):
        scoped_entry["official"], scoped_entry["test"] = _split_status_entry(entry)
        scoped_entry["pool"] = _extract_pool(entry, task, target_date)
        scoped_entry["test_pool"] = _extract_test_pool(entry, task, target_date)
    state[key] = scoped_entry
    return state, key, scoped_entry


def reset_manual_test_session_state(
    task: dict,
    *,
    target_date: date | None = None,
) -> dict:
    with _LOCK:
        state, key, scoped_entry = _with_pool_lock(task, target_date=target_date)
        scoped_entry["test"] = _default_status_payload()
        test_pool = _default_task_pool(task, target_date)
        test_pool["brand"]["has_gap"] = False
        test_pool["brand"]["gap_reasons"] = []
        scoped_entry["test_pool"] = test_pool
        state[key] = scoped_entry
        _save_state(state)
        return deepcopy(scoped_entry["test_pool"])


def start_formal_task_run(
    task: dict,
    *,
    source: str = "",
    message: str = "",
    target_date: date | None = None,
) -> dict:
    now_text = local_now().isoformat(timespec="seconds")
    with _LOCK:
        state, key, scoped_entry = _with_pool_lock(task, target_date=target_date)
        pool = _ensure_pool_shape(scoped_entry.get("pool"), task, target_date)
        pool["brand"]["formal_started"] = True
        pool["brand"]["formal_running"] = True
        pool["brand"]["source_mode"] = SOURCE_MODE_FORMAL
        pool["brand"]["updated_at"] = now_text
        if message:
            pool["brand"]["status_message"] = str(message or "").strip()
        scoped_entry["pool"] = pool
        state[key] = scoped_entry
        _save_state(state)
    return deepcopy(pool)


def finish_formal_task_run(
    task: dict,
    *,
    message: str = "",
    send_error: str = "",
    target_date: date | None = None,
) -> dict:
    now_text = local_now().isoformat(timespec="seconds")
    with _LOCK:
        state, key, scoped_entry = _with_pool_lock(task, target_date=target_date)
        pool = _ensure_pool_shape(scoped_entry.get("pool"), task, target_date)
        pool["brand"]["formal_started"] = True
        pool["brand"]["formal_running"] = False
        pool["brand"]["source_mode"] = SOURCE_MODE_FORMAL
        pool["brand"]["updated_at"] = now_text
        if message:
            pool["brand"]["status_message"] = str(message or "").strip()
        if send_error:
            pool["brand"]["last_send_error"] = str(send_error or "").strip()
        gap_details = _normalize_gap_details(pool)
        pool["brand"]["has_gap"] = bool(gap_details)
        pool["brand"]["gap_reasons"] = _format_gap_reasons(gap_details)
        scoped_entry["pool"] = pool
        state[key] = scoped_entry
        _save_state(state)
    return deepcopy(pool)


def _apply_keyword_update_to_pool(
    pool: dict,
    *,
    task: dict,
    update: dict,
    normalized_source: str,
    now_text: str,
) -> tuple[dict, bool]:
    keyword_states = dict(pool.get("keywords") or {})
    keyword = _normalize_keyword_key(update.get("keyword"))
    if not keyword:
        return pool, False

    brand = str(update.get("brand") or keyword_states.get(keyword, {}).get("brand") or pool.get("brand_name") or "").strip()
    current_state = _coerce_keyword_state(keyword_states.get(keyword), keyword=keyword, brand=brand)
    run_success = _coerce_bool(update.get("run_success"))
    screenshot_saved = _coerce_bool(update.get("screenshot_saved"))
    failure_reason = str(update.get("failure_reason") or "").strip()
    if normalized_source == SOURCE_MODE_TEST and not run_success:
        return pool, False

    image_path = str(update.get("image_path") or "").strip()
    platform = str(update.get("platform") or current_state.get("platform") or "").strip()
    if run_success and screenshot_saved and image_path:
        target_path = build_canonical_screenshot_path(
            task,
            keyword=keyword,
            brand=brand,
            platform=platform,
            source_mode=SOURCE_MODE_FORMAL if normalized_source == SOURCE_MODE_TEST else normalized_source,
            original_path=image_path,
        )
        image_path = _move_file_if_needed(image_path, target_path)
    update["image_path"] = image_path

    if run_success and screenshot_saved:
        failure_reason = ""
    elif run_success and not failure_reason:
        failure_reason = KEYWORD_REASON_SCREENSHOT_SAVE_FAILED
    elif not run_success and not failure_reason:
        failure_reason = KEYWORD_REASON_RUN_FAILED

    current_state.update({
        "keyword": keyword,
        "brand": brand,
        "run_success": run_success,
        "screenshot_saved": screenshot_saved,
        "failure_reason": failure_reason or ("" if run_success and screenshot_saved else KEYWORD_REASON_NOT_RUN),
        "source_mode": normalized_source,
        "image_path": image_path,
        "platform": platform,
        "updated_at": str(update.get("updated_at") or now_text).strip() or now_text,
    })
    if platform and current_state.get("required_platforms"):
        platform_states = dict(current_state.get("platform_states") or {})
        platform_state = _coerce_keyword_state(
            platform_states.get(platform),
            keyword=keyword,
            brand=brand,
            platforms=[],
        )
        platform_state.update({
            "keyword": keyword,
            "brand": brand,
            "run_success": run_success,
            "screenshot_saved": screenshot_saved,
            "failure_reason": failure_reason or ("" if run_success and screenshot_saved else KEYWORD_REASON_NOT_RUN),
            "source_mode": normalized_source,
            "image_path": image_path,
            "platform": platform,
            "required_platforms": [],
            "platform_states": {},
            "updated_at": str(update.get("updated_at") or now_text).strip() or now_text,
        })
        platform_states[platform] = platform_state
        current_state["platform_states"] = platform_states
        current_state["run_success"] = any(_coerce_bool((item or {}).get("run_success")) for item in platform_states.values())
        current_state["screenshot_saved"] = _is_keyword_complete(current_state)
        if current_state["screenshot_saved"]:
            current_state["failure_reason"] = ""
    keyword_states[keyword] = current_state
    pool["keywords"] = keyword_states
    return pool, True


def apply_task_keyword_updates(
    task: dict,
    keyword_updates: list[dict],
    *,
    source_mode: str,
    target_date: date | None = None,
) -> dict:
    now_text = local_now().isoformat(timespec="seconds")
    normalized_source = SOURCE_MODE_TEST if str(source_mode or "").strip() == SOURCE_MODE_TEST else SOURCE_MODE_FORMAL
    with _LOCK:
        state, key, scoped_entry = _with_pool_lock(task, target_date=target_date)
        pool_key = "test_pool" if normalized_source == SOURCE_MODE_TEST else "pool"
        pool = _ensure_pool_shape(scoped_entry.get(pool_key), task, target_date)
        shared_pool = _ensure_pool_shape(scoped_entry.get("pool"), task, target_date) if normalized_source == SOURCE_MODE_TEST else None
        for raw_update in keyword_updates or []:
            update = dict(raw_update or {})
            pool, applied = _apply_keyword_update_to_pool(
                pool,
                task=task,
                update=update,
                normalized_source=normalized_source,
                now_text=now_text,
            )
            if normalized_source == SOURCE_MODE_TEST and applied and shared_pool is not None:
                shared_pool, _ = _apply_keyword_update_to_pool(
                    shared_pool,
                    task=task,
                    update=update,
                    normalized_source=normalized_source,
                    now_text=now_text,
                )
        pool["brand"]["source_mode"] = normalized_source
        pool["brand"]["updated_at"] = now_text
        gap_details = _normalize_gap_details(pool)
        pool["brand"]["has_gap"] = bool(gap_details)
        pool["brand"]["gap_reasons"] = _format_gap_reasons(gap_details)
        scoped_entry[pool_key] = pool
        if normalized_source == SOURCE_MODE_TEST and shared_pool is not None:
            shared_pool["brand"]["source_mode"] = normalized_source
            shared_pool["brand"]["updated_at"] = now_text
            shared_gap_details = _normalize_gap_details(shared_pool)
            shared_pool["brand"]["has_gap"] = bool(shared_gap_details)
            shared_pool["brand"]["gap_reasons"] = _format_gap_reasons(shared_gap_details)
            scoped_entry["pool"] = shared_pool
        state[key] = scoped_entry
        _save_state(state)
    return deepcopy(pool)


def mark_task_sent(
    task: dict,
    *,
    source_mode: str,
    message: str = "",
    target_date: date | None = None,
) -> dict:
    now_text = local_now().isoformat(timespec="seconds")
    normalized_source = SOURCE_MODE_TEST if str(source_mode or "").strip() == SOURCE_MODE_TEST else SOURCE_MODE_FORMAL
    with _LOCK:
        state, key, scoped_entry = _with_pool_lock(task, target_date=target_date)
        pool_key = "test_pool" if normalized_source == SOURCE_MODE_TEST else "pool"
        pool = _ensure_pool_shape(scoped_entry.get(pool_key), task, target_date)
        pool["brand"]["sent_today"] = True
        pool["brand"]["sent_at"] = now_text
        pool["brand"]["source_mode"] = normalized_source
        pool["brand"]["updated_at"] = now_text
        pool["brand"]["last_send_error"] = ""
        if normalized_source == SOURCE_MODE_FORMAL:
            pool["brand"]["formal_started"] = True
            pool["brand"]["formal_running"] = False
        if message:
            pool["brand"]["status_message"] = str(message or "").strip()
        gap_details = _normalize_gap_details(pool)
        pool["brand"]["has_gap"] = bool(gap_details)
        pool["brand"]["gap_reasons"] = _format_gap_reasons(gap_details)
        scoped_entry[pool_key] = pool
        if normalized_source == SOURCE_MODE_TEST:
            shared_pool = _ensure_pool_shape(scoped_entry.get("pool"), task, target_date)
            shared_pool["brand"]["sent_today"] = True
            shared_pool["brand"]["sent_at"] = now_text
            shared_pool["brand"]["source_mode"] = normalized_source
            shared_pool["brand"]["updated_at"] = now_text
            if message:
                shared_pool["brand"]["status_message"] = str(message or "").strip()
            test_keywords = dict(pool.get("keywords") or {})
            merged_keywords = dict(shared_pool.get("keywords") or {})
            for keyword, raw_state in test_keywords.items():
                test_state = _coerce_keyword_state(
                    raw_state,
                    keyword=keyword,
                    brand=str((raw_state or {}).get("brand") or shared_pool.get("brand_name") or "").strip(),
                )
                if not _is_keyword_complete(test_state):
                    continue
                merged_keywords[keyword] = deepcopy(test_state)
            shared_pool["keywords"] = merged_keywords
            shared_gap_details = _normalize_gap_details(shared_pool)
            shared_pool["brand"]["has_gap"] = bool(shared_gap_details)
            shared_pool["brand"]["gap_reasons"] = _format_gap_reasons(shared_gap_details)
            scoped_entry["pool"] = shared_pool
        state[key] = scoped_entry
        _save_state(state)
    return deepcopy(pool)


def mark_task_send_failure(
    task: dict,
    *,
    source_mode: str,
    message: str = "",
    target_date: date | None = None,
) -> dict:
    now_text = local_now().isoformat(timespec="seconds")
    normalized_source = SOURCE_MODE_TEST if str(source_mode or "").strip() == SOURCE_MODE_TEST else SOURCE_MODE_FORMAL
    with _LOCK:
        state, key, scoped_entry = _with_pool_lock(task, target_date=target_date)
        pool = _ensure_pool_shape(scoped_entry.get("pool"), task, target_date)
        if normalized_source == SOURCE_MODE_TEST:
            return deepcopy(pool)
        pool["brand"]["formal_started"] = True
        pool["brand"]["formal_running"] = False
        pool["brand"]["source_mode"] = normalized_source
        pool["brand"]["updated_at"] = now_text
        pool["brand"]["last_send_error"] = str(message or "").strip()
        if message:
            pool["brand"]["status_message"] = str(message or "").strip()
        gap_details = _normalize_gap_details(pool)
        pool["brand"]["has_gap"] = bool(gap_details)
        pool["brand"]["gap_reasons"] = _format_gap_reasons(gap_details)
        scoped_entry["pool"] = pool
        state[key] = scoped_entry
        _save_state(state)
    return deepcopy(pool)


def _apply_legacy_status_to_pool(
    task: dict,
    *,
    status: str,
    source: str,
    message: str = "",
    extra: dict | None = None,
    target_date: date | None = None,
) -> None:
    normalized_status = str(status or "").strip()
    normalized_source = SOURCE_MODE_TEST if str(source or "").strip() == "manual_test" else SOURCE_MODE_FORMAL
    pool_key = "test_pool" if normalized_source == SOURCE_MODE_TEST else "pool"
    extra_payload = dict(extra or {})
    completed_keywords = _dedupe_text_list(extra_payload.get("completed_keywords"))
    detected_platforms = _dedupe_text_list(extra_payload.get("detected_platforms"))
    image_count = int(extra_payload.get("image_count") or 0)
    required_entries = _extract_required_keyword_entries(task)
    required_platforms_by_keyword = {
        item["keyword"]: list(item.get("platforms") or [])
        for item in required_entries
        if str(item.get("keyword") or "").strip()
    }
    completed_all_required = False
    if (
        not completed_keywords
        and normalized_status in {STATUS_SUCCESS, STATUS_SENT}
        and bool(extra_payload.get("notification_success"))
        and not bool(extra_payload.get("forced_ignore_failure"))
        and not bool(extra_payload.get("completed_by_quota"))
    ):
        completed_keywords = [item["keyword"] for item in required_entries if str(item.get("keyword") or "").strip()]
        completed_all_required = True

    keyword_updates: list[dict] = []
    if completed_keywords:
        for index, keyword in enumerate(completed_keywords):
            if completed_all_required:
                update_platforms = list(required_platforms_by_keyword.get(keyword) or [])
            else:
                update_platforms = []
            if not update_platforms:
                update_platforms = [detected_platforms[index] if index < len(detected_platforms) else ""]
            for platform in update_platforms:
                keyword_updates.append({
                    "keyword": keyword,
                    "run_success": True,
                    "screenshot_saved": True,
                    "failure_reason": "",
                    "platform": platform,
                    "image_path": "",
                })
    if keyword_updates:
        apply_task_keyword_updates(task, keyword_updates, source_mode=normalized_source, target_date=target_date)

    if normalized_status == STATUS_RUNNING and normalized_source == SOURCE_MODE_FORMAL:
        start_formal_task_run(task, source=source, message=message, target_date=target_date)
        return

    if normalized_status in {STATUS_SUCCESS, STATUS_SENT} and bool(extra_payload.get("notification_success")):
        mark_task_sent(task, source_mode=normalized_source, message=message, target_date=target_date)
        return

    if normalized_source == SOURCE_MODE_TEST:
        if normalized_status == STATUS_SUCCESS:
            now_text = local_now().isoformat(timespec="seconds")
            with _LOCK:
                state, key, scoped_entry = _with_pool_lock(task, target_date=target_date)
                pool = _ensure_pool_shape(scoped_entry.get(pool_key), task, target_date)
                pool["brand"]["source_mode"] = normalized_source
                pool["brand"]["updated_at"] = now_text
                if message:
                    pool["brand"]["status_message"] = str(message or "").strip()
                gap_details = _normalize_gap_details(pool)
                pool["brand"]["has_gap"] = bool(gap_details)
                pool["brand"]["gap_reasons"] = _format_gap_reasons(gap_details)
                scoped_entry[pool_key] = pool
                if not gap_details:
                    shared_pool = _ensure_pool_shape(scoped_entry.get("pool"), task, target_date)
                    shared_pool["brand"]["source_mode"] = normalized_source
                    shared_pool["brand"]["updated_at"] = now_text
                    if message:
                        shared_pool["brand"]["status_message"] = str(message or "").strip()
                    shared_pool["brand"]["formal_started"] = True
                    shared_gap_details = _normalize_gap_details(shared_pool)
                    shared_pool["brand"]["has_gap"] = bool(shared_gap_details)
                    shared_pool["brand"]["gap_reasons"] = _format_gap_reasons(shared_gap_details)
                    scoped_entry["pool"] = shared_pool
                state[key] = scoped_entry
                _save_state(state)
            return
        return

    if normalized_status == STATUS_SEND_FAILED:
        mark_task_send_failure(task, source_mode=normalized_source, message=message, target_date=target_date)
        return

    if normalized_source == SOURCE_MODE_FORMAL:
        finish_formal_task_run(
            task,
            message=message,
            send_error=(message if str(extra_payload.get("task_failure_kind") or "").strip() == "notification" else ""),
            target_date=target_date,
        )


def _write_status(
    task: dict,
    status: str,
    *,
    source: str,
    scope: str = "official",
    message: str = "",
    extra: dict | None = None,
    target_date: date | None = None,
) -> dict:
    now_text = local_now().isoformat(timespec="seconds")
    key = _make_day_key(task, target_date)
    normalized_scope = "test" if str(scope or "").strip() == "test" else "official"
    with _LOCK:
        state = _prune_old_entries(_load_state())
        existing_entry = state.get(key)
        if existing_entry is None:
            legacy_key = _legacy_single_mode_day_key(task, target_date)
            if legacy_key and legacy_key in state:
                existing_entry = state.pop(legacy_key)
        existing_official, existing_test = _split_status_entry(existing_entry)
        pool = _extract_pool(existing_entry, task, target_date)
        existing_scoped = existing_test if normalized_scope == "test" else existing_official
        retained_failure = _extract_retained_failure(existing_scoped)
        existing_extra = existing_scoped.get("extra") if isinstance(existing_scoped.get("extra"), dict) else {}
        merged_extra = dict(extra or {})
        normalized_status = str(status or "").strip()
        if normalized_status in _FAILURE_STATUSES:
            if (
                normalized_scope == "test"
                and str(existing_test.get("status") or "").strip() == STATUS_SUCCESS
            ):
                return dict(existing_test)
            if (
                normalized_scope == "official"
                and str(existing_official.get("status") or "").strip() in {STATUS_SUCCESS, STATUS_SENT}
            ):
                return dict(existing_official)
        if normalized_status == STATUS_SUCCESS:
            merged_extra = _merge_retained_failure(merged_extra, {})
            merged_extra = _merge_retained_progress(merged_extra, existing_extra)
        elif normalized_status in {STATUS_RUNNING, STATUS_PENDING}:
            merged_extra = _merge_retained_failure(merged_extra, retained_failure)
            merged_extra = _merge_retained_progress(merged_extra, existing_extra)
        elif normalized_status in _FAILURE_STATUSES:
            retained_failure = {
                "status": normalized_status,
                "message": str(message or "").strip(),
                "updated_at": now_text,
                "failure_kind": str(merged_extra.get("task_failure_kind") or "").strip(),
            }
            merged_extra = _merge_retained_failure(merged_extra, retained_failure)
            merged_extra = _merge_retained_progress(merged_extra, existing_extra)
        else:
            merged_extra = _merge_retained_failure(merged_extra, retained_failure)
            merged_extra = _merge_retained_progress(merged_extra, existing_extra)

        payload = {
            "status": status,
            "source": str(source or "").strip(),
            "updated_at": now_text,
            "message": str(message or "").strip(),
        }
        if merged_extra:
            payload["extra"] = dict(merged_extra)
        scoped_entry = _default_scoped_entry(task, target_date)
        scoped_entry["official"] = dict(existing_official)
        scoped_entry["test"] = dict(existing_test)
        scoped_entry["pool"] = pool
        scoped_entry["test_pool"] = _extract_test_pool(existing_entry, task, target_date)
        scoped_entry[normalized_scope] = payload
        state[key] = scoped_entry
        _save_state(state)

    _apply_legacy_status_to_pool(
        task,
        status=status,
        source=source,
        message=message,
        extra=merged_extra,
        target_date=target_date,
    )
    return payload


def mark_task_success(
    task: dict,
    *,
    source: str,
    scope: str = "official",
    message: str = "",
    extra: dict | None = None,
    target_date: date | None = None,
) -> dict:
    return _write_status(
        task,
        STATUS_SUCCESS,
        source=source,
        scope=scope,
        message=message,
        extra=extra,
        target_date=target_date,
    )


def mark_task_query_failed(
    task: dict,
    *,
    source: str,
    scope: str = "official",
    message: str = "",
    extra: dict | None = None,
    target_date: date | None = None,
) -> dict:
    return _write_status(
        task,
        STATUS_QUERY_FAILED,
        source=source,
        scope=scope,
        message=message,
        extra=extra,
        target_date=target_date,
    )


def mark_task_running(
    task: dict,
    *,
    source: str,
    scope: str = "official",
    message: str = "",
    extra: dict | None = None,
    target_date: date | None = None,
) -> dict:
    return _write_status(
        task,
        STATUS_RUNNING,
        source=source,
        scope=scope,
        message=message,
        extra=extra,
        target_date=target_date,
    )


def mark_task_send_failed(
    task: dict,
    *,
    source: str,
    scope: str = "official",
    message: str = "",
    extra: dict | None = None,
    target_date: date | None = None,
) -> dict:
    return _write_status(
        task,
        STATUS_SEND_FAILED,
        source=source,
        scope=scope,
        message=message,
        extra=extra,
        target_date=target_date,
    )


def mark_task_failed(
    task: dict,
    *,
    source: str,
    scope: str = "official",
    message: str = "",
    extra: dict | None = None,
    target_date: date | None = None,
) -> dict:
    failure_kind = str(((extra or {}).get("task_failure_kind") or "")).strip()
    if failure_kind == "notification":
        return mark_task_send_failed(
            task,
            source=source,
            scope=scope,
            message=message,
            extra=extra,
            target_date=target_date,
        )
    return mark_task_query_failed(
        task,
        source=source,
        scope=scope,
        message=message,
        extra=extra,
        target_date=target_date,
    )


def write_task_status(
    task: dict,
    *,
    status: str,
    source: str,
    scope: str = "official",
    message: str = "",
    extra: dict | None = None,
    target_date: date | None = None,
) -> dict:
    normalized_status = str(status or "").strip()
    normalized_extra = build_task_state_extra(extra=extra)
    if normalized_status in {STATUS_RUNNING, "running"}:
        return mark_task_running(
            task,
            source=source,
            scope=scope,
            message=message,
            extra=normalized_extra,
            target_date=target_date,
        )
    if normalized_status in {STATUS_SUCCESS, "success", STATUS_SENT, "sent"}:
        return mark_task_success(
            task,
            source=source,
            scope=scope,
            message=message,
            extra=normalized_extra,
            target_date=target_date,
        )
    if normalized_status in {STATUS_SEND_FAILED, "send_failed"}:
        return mark_task_send_failed(
            task,
            source=source,
            scope=scope,
            message=message,
            extra=normalized_extra,
            target_date=target_date,
        )
    if normalized_status in {STATUS_QUERY_FAILED, "query_failed"}:
        return mark_task_query_failed(
            task,
            source=source,
            scope=scope,
            message=message,
            extra=normalized_extra,
            target_date=target_date,
        )
    return mark_task_failed(
        task,
        source=source,
        scope=scope,
        message=message,
        extra=normalized_extra,
        target_date=target_date,
    )
