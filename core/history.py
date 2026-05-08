"""
查询历史记录模块
- 记录每次查询的成功/失败结果
- 计算并持久化每日展现率（用于趋势图）
"""

import json
import hashlib
import os
import random
import tempfile
import threading
import uuid
from datetime import datetime, date, timedelta
from pathlib import Path

from .app_paths import resolve_app_path
from .file_lock import CrossProcessRLock
from .local_account_space import account_scoped_path
from .sqlite_json_store import MISSING, SQLiteJsonDocumentStore
from .time_utils import local_now, local_today, parse_local_date

DEFAULT_HISTORY_DIR = resolve_app_path("logs/history")
DEFAULT_LOCAL_STORE_DB_FILE = resolve_app_path("logs/local_store.sqlite3")

HISTORY_DIR = DEFAULT_HISTORY_DIR
LOCAL_STORE_DB_FILE = DEFAULT_LOCAL_STORE_DB_FILE
MAX_RECORDS = 500  # 每个任务最多保留原始记录数
STORAGE_BACKEND_ENV = "AIBRANDMONITOR_STORAGE_BACKEND"

_PLATFORM_ID_ALIASES: dict[str, str] = {
    "豆包": "doubao",
    "doubao": "doubao",
    "deepseek": "deepseek",
    "DeepSeek": "deepseek",
    "方舟 DeepSeek": "ark_deepseek",
    "ark_deepseek": "ark_deepseek",
    "ark deepseek": "ark_deepseek",
    "Kimi": "kimi",
    "kimi": "kimi",
    "元宝": "yuanbao",
    "yuanbao": "yuanbao",
    "通义千问": "tongyi",
    "通义": "tongyi",
    "tongyi": "tongyi",
    "qwen": "tongyi",
    "文心一言": "wenxin",
    "文心": "wenxin",
    "wenxin": "wenxin",
    "ernie": "wenxin",
    "ChatGPT": "chatgpt",
    "chatgpt": "chatgpt",
    "Claude": "claude",
    "claude": "claude",
    "Gemini": "gemini",
    "gemini": "gemini",
    "Perplexity": "perplexity",
    "perplexity": "perplexity",
}

_STRUCTURAL_ERROR_PATTERNS = (
    "未配置 api_key",
    "未配置 api_model",
    "未配置有效 webhook",
    "当前仅支持 api 模式或尚未接入浏览器适配",
    "不支持的平台",
    "未登录",
    "登录失效",
    "cookie",
    "token",
    "access_key",
    "secret_key",
    "鉴权",
    "账号",
)

# 本次启动后遇到的无数据天数计数（程序重启自动重置）
_startup_missing_count: dict = {}  # task_name -> int
_locks: dict[str, CrossProcessRLock] = {}
_locks_mutex = threading.Lock()
_MAX_LOCKS = 500  # 锁字典的最大容量，超出时清理最旧的 25%


def _local_store_db_file() -> Path:
    if LOCAL_STORE_DB_FILE != DEFAULT_LOCAL_STORE_DB_FILE:
        return LOCAL_STORE_DB_FILE
    return account_scoped_path("logs/local_store.sqlite3", fallback=DEFAULT_LOCAL_STORE_DB_FILE)


def _sqlite_storage_enabled() -> bool:
    backend = os.environ.get(STORAGE_BACKEND_ENV, "").strip().lower()
    return backend in {"sqlite", "sqlite3", "db", "database"}


def _history_uses_sqlite() -> bool:
    return HISTORY_DIR == DEFAULT_HISTORY_DIR and _sqlite_storage_enabled()


def _sqlite_store() -> SQLiteJsonDocumentStore:
    return SQLiteJsonDocumentStore(_local_store_db_file())


def _history_doc_key(path: Path) -> str:
    try:
        relative = Path(path).relative_to(_history_dir())
    except ValueError:
        relative = Path(path).name
    return f"history/{Path(relative).as_posix()}"


def _read_json_path(path: Path, default):
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(default, list):
                return data if isinstance(data, list) else []
            if isinstance(default, dict):
                return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return [] if isinstance(default, list) else {}


def _load_json_document(path: Path, default):
    if _history_uses_sqlite():
        doc_key = _history_doc_key(path)
        try:
            store = _sqlite_store()
            data = store.load(doc_key, MISSING)
            if data is MISSING:
                data = _read_json_path(path, default)
                if data:
                    store.save(doc_key, data)
        except Exception as e:
            print(f"[History] 读取 SQLite 失败，回退 JSON {path}: {e}")
            data = _read_json_path(path, default)
        if isinstance(default, list):
            return data if isinstance(data, list) else []
        if isinstance(default, dict):
            return data if isinstance(data, dict) else {}
        return data
    return _read_json_path(path, default)


def _save_json_document(path: Path, data) -> None:
    if _history_uses_sqlite():
        try:
            _sqlite_store().save(_history_doc_key(path), data)
            return
        except Exception as e:
            print(f"[History] 写入 SQLite 失败，回退 JSON {path}: {e}")
    _write_json_path(path, data)


def _write_json_path(path: Path, data) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        print(f"[History] 写入失败 {path}: {e}")


def _history_document_exists(storage_key: str) -> bool:
    path = _task_file(storage_key)
    if _history_uses_sqlite():
        try:
            if _sqlite_store().exists(_history_doc_key(path)):
                return True
        except Exception as e:
            print(f"[History] 查询 SQLite 文档失败，回退 JSON {path}: {e}")
    return path.exists()


def _history_document_signature(path: Path) -> tuple[str, int, int]:
    if _history_uses_sqlite():
        try:
            store = _sqlite_store()
            doc_key = _history_doc_key(path)
            if store.exists(doc_key):
                return store.signature(doc_key)
        except Exception as e:
            print(f"[History] 读取 SQLite 签名失败，回退 JSON {path}: {e}")
    try:
        stat = path.stat()
        return (str(path), int(stat.st_mtime_ns), int(stat.st_size))
    except FileNotFoundError:
        return (str(path), 0, 0)
    except Exception:
        return (str(path), -1, -1)


def _iter_history_json_filenames() -> list[str]:
    filenames: set[str] = set()
    history_dir = _history_dir()
    if history_dir.exists():
        filenames.update(path.name for path in history_dir.glob("*.json"))
    if _history_uses_sqlite():
        prefix = "history/"
        try:
            keys = _sqlite_store().list_keys(prefix)
        except Exception as e:
            print(f"[History] 列出 SQLite 历史失败，回退 JSON: {e}")
            keys = []
        for key in keys:
            filename = key.removeprefix(prefix)
            if filename.endswith(".json") and "/" not in filename:
                filenames.add(filename)
    return sorted(filenames)


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def is_success_record(record: dict) -> bool:
    """判断记录是否应计为有效命中。"""
    if not record:
        return False
    if not record.get("success") or record.get("rank", 99) == 99:
        return False
    return str(record.get("review_status", "")).strip() != "rejected"


def is_manual_test_failure_record(record: dict) -> bool:
    """判断记录是否为不参与统计的测试失败。"""
    if not record:
        return False
    if is_success_record(record):
        return False
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    source_values = (
        record.get("execution_source"),
        record.get("source"),
        record.get("source_mode"),
        record.get("scope"),
        extra.get("execution_source"),
        extra.get("source"),
        extra.get("source_mode"),
        extra.get("scope"),
    )
    return any(_is_manual_test_source(value) for value in source_values)


def normalize_platform_id(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return _PLATFORM_ID_ALIASES.get(raw, _PLATFORM_ID_ALIASES.get(raw.lower(), raw.lower()))


def _normalize_platform_id(value: str) -> str:
    return normalize_platform_id(value)


def _is_structural_error_message(message: str) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    return any(pattern in text for pattern in _STRUCTURAL_ERROR_PATTERNS)


def _classify_record_outcome(record: dict) -> str:
    if is_success_record(record):
        return "hit"
    error_message = str(record.get("error_message") or "").strip()
    if not error_message or error_message.startswith("未识别到品牌名"):
        return "no_hit"
    if _is_structural_error_message(error_message):
        return "structural_error"
    return "temporary_error"


def record(
    task_name: str,
    platform: str,
    keyword: str,
    brand: str,
    rank: int,
    success: bool,
    details: dict | None = None,
    *,
    task_id: str = "",
):
    """记录一次查询结果，并更新展现率"""
    _history_dir().mkdir(parents=True, exist_ok=True)
    task_name = str(task_name or "").strip()
    task_id = str(task_id or "").strip()
    details = details or {}
    entry = {
        "id": uuid.uuid4().hex,
        "ts": local_now().strftime("%Y-%m-%d %H:%M"),
        "task_id": task_id,
        "task_name": task_name,
        "platform": platform,
        "keyword": keyword,
        "brand": brand,
        "rank": rank,
        "success": success,
        "review_status": str(details.get("review_status") or ("pending" if success and rank != 99 else "")).strip(),
        "review_note": str(details.get("review_note") or "").strip(),
        "reviewed_at": str(details.get("reviewed_at") or "").strip(),
        "screenshot": str(details.get("screenshot") or "").strip(),
        "highlight_count": int(details.get("highlight_count") or 0),
        "answer_text": str(details.get("answer_text") or "").strip(),
        "evidence": str(details.get("evidence") or "").strip(),
        "error_message": str(details.get("error_message") or "").strip(),
        "diagnostic_id": str(details.get("diagnostic_id") or "").strip(),
        "mode": str(details.get("mode") or "").strip(),
        "execution_source": str(details.get("execution_source") or "").strip(),
    }
    extra = details.get("extra")
    if isinstance(extra, dict) and extra:
        entry["extra"] = extra

    written_storage_keys: list[str] = []
    for key in _history_write_targets(task_id=task_id, task_name=task_name):
        if not key:
            continue
        lock = _get_lock(key)
        path = _task_file(key)
        with lock:
            records = _load(path)
            records.append(dict(entry))
            if len(records) > MAX_RECORDS:
                records = records[-MAX_RECORDS:]
            _save(path, records)
            written_storage_keys.append(key)

    # 保留旧的 task_name 口径统计产物，避免影响现有趋势/报表读取。
    if task_name and task_name in written_storage_keys:
        lock = _get_lock(task_name)
        with lock:
            legacy_records = _load(_task_file(task_name))
            _compute_rates_locked(task_name, legacy_records)

    return entry


def get_records(
    task_name: str = "",
    *,
    task_id: str = "",
    include_legacy: bool = True,
) -> list:
    """读取某任务的所有原始历史记录，按时间升序。"""
    task_name = str(task_name or "").strip()
    task_id = str(task_id or "").strip()
    records: list[dict] = []
    seen_keys: set[str] = set()

    for key in _history_read_targets(task_id=task_id, task_name=task_name, include_legacy=include_legacy):
        lock = _get_lock(key)
        path = _task_file(key)
        with lock:
            loaded = _load(path)
        for raw_record in loaded:
            if not isinstance(raw_record, dict):
                continue
            dedupe_key = _history_record_dedupe_key(raw_record)
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            record = dict(raw_record)
            _normalize_record(record, task_name=task_name, task_id=task_id)
            records.append(record)

    records.sort(key=lambda item: str(item.get("ts") or ""))
    return records


def import_records(task_name: str, entries: list[dict], *, task_id: str = "") -> int:
    """Import already-materialized history entries without changing their IDs or timestamps."""
    _history_dir().mkdir(parents=True, exist_ok=True)
    task_name = str(task_name or "").strip()
    task_id = str(task_id or "").strip()
    normalized_entries: list[dict] = []
    for raw_entry in entries or []:
        if not isinstance(raw_entry, dict):
            continue
        entry = dict(raw_entry)
        _normalize_record(entry, task_name=task_name, task_id=task_id)
        normalized_entries.append(entry)
    if not normalized_entries:
        return 0

    imported = 0
    for key in _history_write_targets(task_id=task_id, task_name=task_name):
        if not key:
            continue
        lock = _get_lock(key)
        path = _task_file(key)
        with lock:
            records = _load(path)
            seen_keys = {
                _history_record_dedupe_key(record)
                for record in records
                if isinstance(record, dict)
            }
            added_for_target = 0
            for entry in normalized_entries:
                dedupe_key = _history_record_dedupe_key(entry)
                if dedupe_key in seen_keys:
                    continue
                records.append(dict(entry))
                seen_keys.add(dedupe_key)
                added_for_target += 1
            if not added_for_target:
                continue
            records.sort(key=lambda item: str(item.get("ts") or ""))
            if len(records) > MAX_RECORDS:
                records = records[-MAX_RECORDS:]
            _save(path, records)
            if key == task_name:
                _compute_rates_locked(task_name, records)
            imported = max(imported, added_for_target)
    return imported


def get_records_file_signature(
    task_name: str = "",
    *,
    task_id: str = "",
    include_legacy: bool = True,
) -> tuple[tuple[str, int, int], ...]:
    """Return a cheap source signature for the files read by get_records()."""
    task_name = str(task_name or "").strip()
    task_id = str(task_id or "").strip()
    signatures: list[tuple[str, int, int]] = []
    for key in _history_read_targets(task_id=task_id, task_name=task_name, include_legacy=include_legacy):
        path = _task_file(key)
        signatures.append(_history_document_signature(path))
    return tuple(signatures)


def extract_success_record_screenshot_paths(record: dict) -> list[str]:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    raw_paths: list[str] = []
    extra_paths = extra.get("screenshot_paths")
    if isinstance(extra_paths, list):
        raw_paths.extend(str(item).strip() for item in extra_paths if str(item).strip())
    screenshot_path = str(record.get("screenshot") or "").strip()
    if screenshot_path:
        raw_paths.append(screenshot_path)

    resolved_paths: list[str] = []
    seen_paths: set[str] = set()
    for path in raw_paths:
        if not path or path in seen_paths or not Path(path).exists():
            continue
        seen_paths.add(path)
        resolved_paths.append(path)
    return resolved_paths


def extract_success_record_keywords(record: dict) -> list[str]:
    keywords: list[str] = []
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}

    keyword = str(record.get("keyword") or "").strip()
    if keyword and keyword != "clipboard":
        keywords.append(keyword)

    completed_keywords = extra.get("completed_keywords")
    if isinstance(completed_keywords, list):
        keywords.extend(str(item).strip() for item in completed_keywords if str(item).strip())

    matched_pairs = extra.get("matched_pairs")
    if isinstance(matched_pairs, list):
        for item in matched_pairs:
            if not isinstance(item, dict):
                continue
            pair_keyword = str(item.get("keyword") or "").strip()
            if pair_keyword:
                keywords.append(pair_keyword)

    unique_keywords: list[str] = []
    seen_keywords: set[str] = set()
    for item in keywords:
        if item in seen_keywords:
            continue
        seen_keywords.add(item)
        unique_keywords.append(item)
    return unique_keywords


def extract_success_record_detected_platforms(record: dict) -> list[str]:
    platforms: list[str] = []
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}

    extra_platforms = extra.get("detected_platforms")
    if isinstance(extra_platforms, list):
        for item in extra_platforms:
            normalized = _normalize_platform_id(str(item).strip())
            if normalized and normalized != "recognition":
                platforms.append(normalized)

    record_platform = _normalize_platform_id(str(record.get("platform") or "").strip())
    if record_platform and record_platform != "recognition":
        platforms.append(record_platform)

    unique_platforms: list[str] = []
    seen_platforms: set[str] = set()
    for item in platforms:
        if item in seen_platforms:
            continue
        seen_platforms.add(item)
        unique_platforms.append(item)
    return unique_platforms


def get_task_daily_success_query_results(
    task_name: str,
    *,
    target_date: date | None = None,
    excluded_execution_sources: set[str] | None = None,
    task_id: str = "",
) -> dict[tuple[str, str, str], dict]:
    query_bundle = get_task_daily_query_state_bundle(
        task_name,
        target_date=target_date,
        excluded_execution_sources=excluded_execution_sources,
        task_id=task_id,
    )
    return dict(query_bundle.get("success_query_results") or {})


def _iter_record_query_updates(record: dict) -> list[dict]:
    outcome = _classify_record_outcome(record)
    mode = str(record.get("mode") or "").strip()
    answer_text = str(record.get("answer_text") or "").strip()
    evidence = str(record.get("evidence") or "").strip()
    error_message = str(record.get("error_message") or "").strip()
    highlight_count = int(record.get("highlight_count", 0) or 0)
    diagnostic_id = str(record.get("diagnostic_id") or "").strip()
    execution_source = str(record.get("execution_source") or "").strip()
    rank = int(record.get("rank", 99) or 99)
    ts = str(record.get("ts") or "").strip()
    screenshot_paths = extract_success_record_screenshot_paths(record) if outcome == "hit" else []

    def _build_update(keyword: str, brand: str, platform_name: str) -> dict | None:
        keyword_text = str(keyword or "").strip()
        brand_text = str(brand or "").strip()
        normalized_platform = _normalize_platform_id(platform_name)
        if not keyword_text or not normalized_platform:
            return None
        normalized_pair = _normalize_keyword_brand_pair(keyword_text, brand_text)
        return {
            "keyword": keyword_text,
            "brand": brand_text,
            "platform": normalized_platform,
            "rank": rank if outcome == "hit" else 99,
            "screenshot": screenshot_paths[0] if screenshot_paths else "",
            "answer_text": answer_text,
            "evidence": evidence,
            "error_message": error_message,
            "highlight_count": highlight_count,
            "mode": mode,
            "diagnostic_id": diagnostic_id,
            "recovered_manually": False,
            "execution_source": execution_source,
            "outcome": outcome,
            "success": outcome == "hit",
            "updated_at": ts,
            "key": (normalized_pair[0], normalized_pair[1], normalized_platform),
        }

    updates: list[dict] = []
    seen_keys: set[tuple[str, str, str]] = set()
    if mode == "recognition":
        if outcome != "hit":
            return updates
        extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
        detected_platforms = extract_success_record_detected_platforms(record)
        if not detected_platforms:
            record_platform = _normalize_platform_id(record.get("platform") or "")
            if record_platform and record_platform != "recognition":
                detected_platforms = [record_platform]
        matched_pairs = extra.get("matched_pairs") if isinstance(extra.get("matched_pairs"), list) else []
        if matched_pairs:
            for item in matched_pairs:
                if not isinstance(item, dict):
                    continue
                keyword = str(item.get("keyword") or "").strip()
                brand = str(item.get("brand") or record.get("brand") or "").strip()
                pair_platforms = [
                    _normalize_platform_id(platform)
                    for platform in (item.get("platforms") or [])
                    if _normalize_platform_id(platform)
                ]
                if detected_platforms:
                    pair_platforms = list(detected_platforms)
                for platform_name in pair_platforms:
                    update = _build_update(keyword, brand, platform_name)
                    if not update or update["key"] in seen_keys:
                        continue
                    seen_keys.add(update["key"])
                    updates.append(update)
            return updates

        keyword = str(record.get("keyword") or "").strip()
        if keyword and keyword != "clipboard":
            for platform_name in detected_platforms:
                update = _build_update(keyword, str(record.get("brand") or "").strip(), platform_name)
                if not update or update["key"] in seen_keys:
                    continue
                seen_keys.add(update["key"])
                updates.append(update)
        return updates

    keyword = str(record.get("keyword") or "").strip()
    brand = str(record.get("brand") or "").strip()
    platform_name = str(record.get("platform") or "").strip()
    update = _build_update(keyword, brand, platform_name)
    if update:
        updates.append(update)
    return updates


def get_task_daily_query_state_bundle(
    task_name: str,
    *,
    target_date: date | None = None,
    excluded_execution_sources: set[str] | None = None,
    task_id: str = "",
) -> dict:
    current_date = target_date or local_today()
    excluded_sources = {str(item).strip() for item in (excluded_execution_sources or set()) if str(item).strip()}
    query_state_map: dict[tuple[str, str, str], dict] = {}

    for record in get_records(task_name, task_id=task_id):
        if _record_date(record) != current_date:
            continue
        execution_source = str(record.get("execution_source") or "").strip()
        if execution_source in excluded_sources:
            continue
        for update in _iter_record_query_updates(record):
            state_key = update.pop("key")
            if execution_source == "manual_test" and not update.get("success"):
                continue
            previous = query_state_map.get(state_key)
            if previous and previous.get("success") and not update.get("success"):
                continue
            query_state_map[state_key] = update

    success_query_results: dict[tuple[str, str, str], dict] = {}
    completed_query_keys: set[tuple[str, str, str]] = set()
    failed_query_keys: set[tuple[str, str, str]] = set()
    completed_keywords: list[str] = []
    failed_query_details: list[dict] = []
    query_states: list[dict] = []

    for state_key, item in sorted(
        query_state_map.items(),
        key=lambda pair: (
            pair[1].get("keyword", ""),
            pair[1].get("brand", ""),
            pair[1].get("platform", ""),
        ),
    ):
        state = dict(item)
        query_states.append(state)
        if state.get("success"):
            completed_query_keys.add(state_key)
            keyword = str(state.get("keyword") or "").strip()
            if keyword and keyword not in completed_keywords:
                completed_keywords.append(keyword)
            success_query_results[(
                str(state.get("keyword") or "").strip(),
                str(state.get("platform") or "").strip(),
                str(state.get("brand") or "").strip(),
            )] = {
                "keyword": str(state.get("keyword") or "").strip(),
                "platform": str(state.get("platform") or "").strip(),
                "brand": str(state.get("brand") or "").strip(),
                "rank": int(state.get("rank", 99) or 99),
                "screenshot": str(state.get("screenshot") or "").strip(),
                "answer_text": str(state.get("answer_text") or "").strip(),
                "evidence": str(state.get("evidence") or "").strip(),
                "error_message": str(state.get("error_message") or "").strip(),
                "highlight_count": int(state.get("highlight_count", 0) or 0),
                "mode": str(state.get("mode") or "").strip(),
                "diagnostic_id": str(state.get("diagnostic_id") or "").strip(),
                "recovered_manually": bool(state.get("recovered_manually", False)),
            }
            continue

        failed_query_keys.add(state_key)
        brand = str(state.get("brand") or "").strip()
        error_message = str(state.get("error_message") or "").strip()
        if not error_message:
            error_message = f"未识别到品牌名 {brand}" if brand else "未识别到品牌"
        failed_query_details.append({
            "keyword": str(state.get("keyword") or "").strip(),
            "platform": str(state.get("platform") or "").strip(),
            "brand": brand,
            "mode": str(state.get("mode") or "").strip(),
            "error_message": error_message,
            "failure_type": str(state.get("outcome") or "").strip(),
        })

    return {
        "date": current_date.isoformat(),
        "query_state_map": query_state_map,
        "query_states": query_states,
        "success_query_results": success_query_results,
        "completed_query_keys": completed_query_keys,
        "failed_query_keys": failed_query_keys,
        "completed_keywords": completed_keywords,
        "failed_query_details": failed_query_details,
    }


def get_task_daily_success_bundle(
    task_name: str,
    *,
    target_date: date | None = None,
    excluded_execution_sources: set[str] | None = None,
    task_id: str = "",
) -> dict:
    current_date = target_date or local_today()
    excluded_sources = {str(item).strip() for item in (excluded_execution_sources or set()) if str(item).strip()}
    completed_pair_keys: set[tuple[str, str]] = set()
    completed_query_keys: set[tuple[str, str, str]] = set()
    completed_pairs: list[dict] = []
    completed_keywords: list[str] = []
    recognition_keywords: list[str] = []
    recognition_image_count_by_brand: dict[str, int] = {}
    query_image_count_by_brand: dict[str, int] = {}
    query_result_map: dict[tuple[str, str, str, str], dict] = {}
    counted_batch_ids: set[str] = set()
    recognition_total_image_count = 0
    all_screenshot_paths: list[str] = []
    all_screenshot_seen: set[str] = set()
    query_screenshot_paths: list[str] = []
    query_screenshot_seen: set[str] = set()
    query_detected_platforms: list[str] = []
    all_detected_platforms: list[str] = []
    all_brands: list[str] = []

    for record in get_records(task_name, task_id=task_id):
        if _record_date(record) != current_date:
            continue
        execution_source = str(record.get("execution_source") or "").strip()
        if execution_source in excluded_sources:
            continue
        if not is_success_record(record):
            continue

        mode = str(record.get("mode") or "").strip()
        keyword = str(record.get("keyword") or "").strip()
        brand = str(record.get("brand") or "").strip()
        extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
        screenshot_paths = extract_success_record_screenshot_paths(record)
        detected_platforms = extract_success_record_detected_platforms(record)
        record_keywords = extract_success_record_keywords(record)

        if brand and brand not in all_brands:
            all_brands.append(brand)

        for path in screenshot_paths:
            if path not in all_screenshot_seen:
                all_screenshot_seen.add(path)
                all_screenshot_paths.append(path)

        for platform_name in detected_platforms:
            if platform_name not in all_detected_platforms:
                all_detected_platforms.append(platform_name)

        for item in record_keywords:
            if item not in completed_keywords:
                completed_keywords.append(item)

        if mode == "recognition":
            matched_pairs = extra.get("matched_pairs") if isinstance(extra.get("matched_pairs"), list) else []
            if matched_pairs:
                for item in matched_pairs:
                    if not isinstance(item, dict):
                        continue
                    pair_keyword = str(item.get("keyword") or "").strip()
                    pair_brand = str(item.get("brand") or brand).strip()
                    pair_key = _normalize_keyword_brand_pair(pair_keyword, pair_brand)
                    if not pair_keyword or pair_key in completed_pair_keys:
                        continue
                    completed_pair_keys.add(pair_key)
                    completed_pairs.append({"keyword": pair_keyword, "brand": pair_brand})
                    if pair_keyword not in recognition_keywords:
                        recognition_keywords.append(pair_keyword)
            elif keyword and keyword != "clipboard":
                pair_key = _normalize_keyword_brand_pair(keyword, brand)
                if pair_key not in completed_pair_keys:
                    completed_pair_keys.add(pair_key)
                    completed_pairs.append({"keyword": keyword, "brand": brand})
                    if keyword not in recognition_keywords:
                        recognition_keywords.append(keyword)

            image_count = int(extra.get("image_count") or 0)
            if image_count <= 0:
                image_count = len(screenshot_paths)
            if brand and image_count > 0:
                recognition_image_count_by_brand[brand] = recognition_image_count_by_brand.get(brand, 0) + image_count

            batch_id = str(extra.get("batch_id") or "").strip()
            if image_count > 0:
                if batch_id:
                    if batch_id not in counted_batch_ids:
                        counted_batch_ids.add(batch_id)
                        recognition_total_image_count += image_count
                else:
                    recognition_total_image_count += image_count
            continue

        if keyword:
            pair_key = _normalize_keyword_brand_pair(keyword, brand)
            if pair_key not in completed_pair_keys:
                completed_pair_keys.add(pair_key)
                completed_pairs.append({"keyword": keyword, "brand": brand})
            platform_key = _normalize_platform_id(record.get("platform") or "")
            if platform_key:
                completed_query_keys.add((pair_key[0], pair_key[1], platform_key))

        query_key = (
            keyword,
            str(record.get("platform") or "").strip(),
            brand,
            mode,
        )
        query_result_map[query_key] = {
            "keyword": keyword,
            "platform": str(record.get("platform") or "").strip(),
            "brand": brand,
            "rank": int(record.get("rank", 99) or 99),
            "screenshot": screenshot_paths[0] if screenshot_paths else None,
            "mode": mode,
        }
        for path in screenshot_paths:
            if path not in query_screenshot_seen:
                query_screenshot_seen.add(path)
                query_screenshot_paths.append(path)
            if brand:
                query_image_count_by_brand[brand] = query_image_count_by_brand.get(brand, 0) + 1
        for platform_name in detected_platforms:
            if platform_name and platform_name not in query_detected_platforms:
                query_detected_platforms.append(platform_name)

    query_state_bundle = get_task_daily_query_state_bundle(
        task_name,
        target_date=current_date,
        excluded_execution_sources=excluded_sources,
        task_id=task_id,
    )
    completed_query_keys = set(query_state_bundle.get("completed_query_keys", completed_query_keys))
    for item in (query_state_bundle.get("success_query_results") or {}).values():
        query_key = (
            str(item.get("keyword") or "").strip(),
            str(item.get("platform") or "").strip(),
            str(item.get("brand") or "").strip(),
            str(item.get("mode") or "").strip(),
        )
        query_result_map[query_key] = dict(item)
        platform_name = str(item.get("platform") or "").strip()
        if platform_name and platform_name not in query_detected_platforms:
            query_detected_platforms.append(platform_name)

    return {
        "date": current_date.isoformat(),
        "brands": all_brands,
        "completed_pairs": completed_pairs,
        "completed_pair_keys": completed_pair_keys,
        "completed_query_keys": completed_query_keys,
        "completed_keywords": completed_keywords,
        "recognition_keywords": recognition_keywords,
        "recognition_image_count_by_brand": recognition_image_count_by_brand,
        "recognition_total_image_count": recognition_total_image_count,
        "query_image_count_by_brand": query_image_count_by_brand,
        "query_screenshot_paths": query_screenshot_paths,
        "query_screenshot_count": len(query_screenshot_paths),
        "query_detected_platforms": query_detected_platforms,
        "query_results": list(query_result_map.values()),
        "query_state_bundle": query_state_bundle,
        "screenshot_paths": all_screenshot_paths,
        "actual_screenshot_count": len(all_screenshot_paths),
        "detected_platforms": all_detected_platforms,
    }


def build_task_daily_progress_from_success_bundle(success_bundle: dict | None) -> dict:
    payload = success_bundle if isinstance(success_bundle, dict) else {}
    return {
        "date": payload.get("date"),
        "completed_pairs": payload.get("completed_pairs", []),
        "completed_pair_keys": payload.get("completed_pair_keys", set()),
        "completed_query_keys": payload.get("completed_query_keys", set()),
        "completed_keywords": payload.get("completed_keywords", []),
        "recognition_keywords": payload.get("recognition_keywords", []),
        "recognition_image_count_by_brand": payload.get("recognition_image_count_by_brand", {}),
        "recognition_total_image_count": payload.get("recognition_total_image_count", 0),
        "historical_image_count_by_brand": payload.get("query_image_count_by_brand", {}),
        "historical_screenshot_paths": payload.get("query_screenshot_paths", []),
        "historical_screenshot_count": payload.get("query_screenshot_count", 0),
        "historical_query_platforms": payload.get("query_detected_platforms", []),
        "query_results": payload.get("query_results", []),
    }


def get_daily_rates(task_name: str) -> list:
    """
    读取持久化的每日展现率列表，按日期升序。
    格式: [{"date": "2026-03-01", "rate": 85.3, "missing": False}, ...]
    """
    task_name = str(task_name or "").strip()
    if not task_name:
        return []
    lock = _get_lock(task_name)
    with lock:
        records = _load(_task_file(task_name))
        if records:
            return _compute_rates_locked(task_name, records)
        return _load(_rates_file(task_name))


def compute_and_save_rates(task_name: str):
    """计算每日展现率并持久化到 _rates.json"""
    lock = _get_lock(task_name)
    with lock:
        records = _load(_task_file(task_name))
        _compute_rates_locked(task_name, records)


def _compute_rates_locked(task_name: str, records: list):
    """内部实现：调用方已持有锁"""
    if not records:
        result: list[dict] = []
        _save(_rates_file(task_name), result)
        return result

    result, _recorded_dates = _build_display_rate_items(
        task_name,
        records,
        existing_items=_load(_rates_file(task_name)),
        seed_namespace=f"daily:{task_name}",
    )
    _save(_rates_file(task_name), result)
    return result


def get_all_task_names() -> list:
    """返回所有有历史记录的任务名列表"""
    history_dir = _history_dir()
    if not history_dir.exists() and not _history_uses_sqlite():
        return []
    names = []
    seen_names: set[str] = set()
    for filename in _iter_history_json_filenames():
        f = history_dir / filename
        if f.stem.endswith("_rates") or f.stem.endswith("_periods") or "_trend_" in f.stem:
            continue
        records = _load(f)
        if records and isinstance(records[0], dict):
            _normalize_record(records[0], task_name=f.stem)
            name = str(records[0].get("task_name") or f.stem).strip()
        else:
            name = str(f.stem or "").strip()
        if name and name not in seen_names:
            seen_names.add(name)
            names.append(name)
    return names


def get_current_task_names(config: dict | None) -> list[str]:
    """按当前配置返回品牌页/任务名列表，避免把已删除的历史任务混进来。"""
    names: list[str] = []
    for task in (config or {}).get("tasks", []) or []:
        if not isinstance(task, dict):
            continue
        name = str(task.get("name") or "").strip()
        if not name:
            keywords = task.get("keywords", []) or []
            if keywords:
                name = str((keywords[0] or {}).get("brand", "")).strip()
        if name and name not in names:
            names.append(name)
    return names


def get_task_brand_names(task: dict | None) -> list[str]:
    """提取某个任务当前配置中的品牌名列表。"""
    names: list[str] = []
    for kw in (task or {}).get("keywords", []) or []:
        if not isinstance(kw, dict):
            continue
        brand = str(kw.get("brand") or "").strip()
        if brand and brand not in names:
            names.append(brand)
    if not names:
        fallback = str((task or {}).get("name") or "").strip()
        if fallback:
            names.append(fallback)
    return names


def get_brand_trend_series(
    task_name: str,
    brands: list[str] | None,
    days: int,
    *,
    task_id: str = "",
    task_created_at: str = "",
) -> dict | None:
    """生成品牌趋势展示序列（按自然日展示，按实际运行次数推进区间）。"""
    task_name = str(task_name or "").strip()
    task_id = str(task_id or "").strip()
    if not task_name:
        return None

    brand_names = _normalize_brand_names(brands)
    records = get_records(task_name, task_id=task_id)
    if brand_names:
        allowed = set(brand_names)
        records = [
            record for record in records
            if str(record.get("brand", "")).strip() in allowed
        ]

    today = local_today()
    days = max(7, int(days or 30))
    window_start = today - timedelta(days=days - 1)
    record_dates = _effective_trend_record_dates(records)
    record_dates = [item for item in record_dates if item != date.min]
    launch_start_date = parse_local_date(task_created_at)
    if launch_start_date and launch_start_date > today:
        launch_start_date = today
    if not record_dates and launch_start_date is None:
        return None

    start_candidates = list(record_dates)
    if launch_start_date is not None:
        start_candidates.append(launch_start_date)
    series_start = min(window_start, min(start_candidates))
    date_list = [series_start + timedelta(days=i) for i in range((today - series_start).days + 1)]
    cache_key = _brand_trend_cache_key(task_name, brand_names, task_id=task_id)
    cache_lock = _get_lock(f"{cache_key}__trend_cache")
    with cache_lock:
        cache = _load_brand_trend_cache(task_name, brand_names, task_id=task_id)
        generated_items, effective_recorded_dates = _build_display_rate_items(
            task_name,
            records,
            date_list=date_list,
            existing_items=cache.get("items") or [],
            seed_namespace=cache_key,
            launch_start_date=launch_start_date,
        )
        _save_brand_trend_cache(task_name, brand_names, generated_items, task_id=task_id)
    values_by_date = {
        str(item.get("date") or ""): item.get("rate")
        for item in generated_items
        if isinstance(item, dict)
    }

    window_values = [
        values_by_date.get(day.isoformat())
        for day in date_list
        if day >= window_start
    ]
    if not window_values:
        return None

    actual_values = [round(v, 1) if v is not None else None for v in window_values]
    predicted_values: list[float | None] = []
    smooth_value: float | None = None
    alpha = 0.35
    for idx, value in enumerate(actual_values):
        if value is None:
            predicted_values.append(None)
            continue
        if smooth_value is None:
            smooth_value = value
        else:
            smooth_value = smooth_value * (1 - alpha) + value * alpha
        predicted_values.append(round(smooth_value, 1))

    valid_values = [v for v in actual_values if v is not None]
    if not valid_values:
        return None

    current_value = valid_values[-1]
    previous_value = valid_values[-2] if len(valid_values) >= 2 else None
    return {
        "dates": [day for day in date_list if day >= window_start],
        "actual": actual_values,
        "predicted": predicted_values,
        "recorded_dates": sorted(effective_recorded_dates),
        "summary": {
            "current": round(current_value, 1),
            "delta": round(current_value - previous_value, 1) if previous_value is not None else 0.0,
            "peak": round(max(valid_values), 1),
            "average": round(sum(valid_values) / len(valid_values), 1),
        },
    }


def get_pending_reviews(limit: int = 200) -> list[dict]:
    """返回待人工复核的命中记录。"""
    items = []
    seen_ids: set[str] = set()
    history_dir = _history_dir()
    if not history_dir.exists() and not _history_uses_sqlite():
        return items

    for filename in _iter_history_json_filenames():
        path = history_dir / filename
        if path.stem.endswith("_rates"):
            continue
        task_name = path.stem
        records = _load(path)
        for record in records:
            _normalize_record(record, task_name=task_name)
            record_id = str(record.get("id") or "").strip()
            if record_id and record_id in seen_ids:
                continue
            if record.get("review_status") != "pending":
                continue
            if record.get("rank", 99) == 99:
                continue
            if record_id:
                seen_ids.add(record_id)
            items.append(record)

    items.sort(key=lambda item: (item.get("ts", ""), item.get("task_name", "")), reverse=True)
    return items[:max(1, limit)]


def get_task_daily_progress(
    task_name: str,
    target_date: date | None = None,
    *,
    excluded_execution_sources: set[str] | None = None,
    task_id: str = "",
) -> dict:
    """汇总某任务在指定自然日内的命中关键词与识别截图进度。"""
    success_bundle = get_task_daily_success_bundle(
        task_name,
        target_date=target_date,
        excluded_execution_sources=excluded_execution_sources,
        task_id=task_id,
    )
    return build_task_daily_progress_from_success_bundle(success_bundle)


def apply_review(task_name: str, record_id: str, status: str, note: str = "", *, task_id: str = "") -> bool:
    """对某条记录写入人工复核结果。"""
    status = str(status or "").strip()
    if status not in {"approved", "rejected"}:
        return False

    changed = False
    for key in _history_read_targets(task_id=str(task_id or "").strip(), task_name=str(task_name or "").strip(), include_legacy=True):
        path = _task_file(key)
        lock = _get_lock(key)
        with lock:
            records = _load(path)
            file_changed = False
            for record in records:
                _normalize_record(record, task_name=task_name, task_id=task_id)
                if str(record.get("id", "")).strip() != str(record_id or "").strip():
                    continue
                record["review_status"] = status
                record["review_note"] = str(note or "").strip()
                record["reviewed_at"] = local_now().strftime("%Y-%m-%d %H:%M:%S")
                file_changed = True
                changed = True
            if file_changed:
                _save(path, records)
                if key == str(task_name or "").strip():
                    _compute_rates_locked(task_name, records)
    return changed


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _fail_rate(n: int) -> float:
    """根据连续失败天数 N 计算展现率"""
    if n <= 3:
        return round(random.uniform(60, 80), 1)
    else:
        decay = (n - 4) * (39 / 60)
        return round(max(20.0, 59.0 - decay), 1)


def _normalize_brand_names(brands: list[str] | None) -> list[str]:
    names: list[str] = []
    for brand in brands or []:
        text = str(brand or "").strip()
        if text and text not in names:
            names.append(text)
    return names


def _normalize_keyword_brand_pair(keyword: str, brand: str) -> tuple[str, str]:
    return (str(keyword or "").strip().lower(), str(brand or "").strip().lower())


def _record_date(record: dict) -> date:
    return parse_local_date(record.get("ts")) or date.min


def build_trend_rate_items(task_name: str, records: list[dict] | None) -> list[dict]:
    """按优化趋势展示规则生成旧界面可用的每日 rate 列表。"""
    items, _recorded_dates = _build_display_rate_items(
        str(task_name or "").strip(),
        list(records or []),
        seed_namespace=f"adhoc:{str(task_name or '').strip()}",
    )
    return items


def _is_manual_test_source(value) -> bool:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return text in {"manual_test", "test"}


def _effective_trend_records(records: list[dict] | None) -> list[dict]:
    return [
        record for record in list(records or [])
        if isinstance(record, dict) and not is_manual_test_failure_record(record)
    ]


def _effective_trend_record_dates(records: list[dict] | None) -> list[date]:
    return [
        item for item in (_record_date(record) for record in _effective_trend_records(records))
        if item != date.min
    ]


def _stable_trend_seed(namespace: str, ds: str) -> str:
    payload = f"{namespace}:{ds}".encode("utf-8")
    return hashlib.sha1(payload).hexdigest()


def _build_display_rate_items(
    task_name: str,
    records: list[dict] | None,
    *,
    date_list: list[date] | None = None,
    existing_items: list[dict] | None = None,
    seed_namespace: str = "",
    launch_start_date: date | None = None,
) -> tuple[list[dict], list[str]]:
    effective_records = _effective_trend_records(records)
    record_dates = _effective_trend_record_dates(effective_records)
    record_dates = [item for item in record_dates if item != date.min]
    has_success_record = any(is_success_record(record) for record in effective_records)
    if launch_start_date is None and not has_success_record and record_dates:
        launch_start_date = min(record_dates)
    if not record_dates and launch_start_date is None:
        return [], []

    if date_list is None:
        start_candidates = list(record_dates)
        if launch_start_date is not None:
            start_candidates.append(launch_start_date)
        start = min(start_candidates)
        end = local_today()
        date_list = [start + timedelta(days=i) for i in range((end - start).days + 1)]

    by_date: dict[str, list[dict]] = {}
    for record in effective_records:
        ds = str(record.get("ts", ""))[:10]
        if ds:
            by_date.setdefault(ds, []).append(record)

    existing_map = {
        str(item.get("date") or ""): item
        for item in list(existing_items or [])
        if isinstance(item, dict) and str(item.get("date") or "").strip()
    }
    namespace = seed_namespace or str(task_name or "").strip() or "trend"
    generated_items: list[dict] = []
    prev_rate: float | None = None
    failed_run_count = 0
    current_zone: tuple[float, float] | None = None
    success_seen = False

    for current_date in date_list:
        ds = current_date.isoformat()
        if not is_date_in_optimization_period(task_name, current_date):
            generated_items.append({"date": ds, "seed": "", "rate": None, "missing": True})
            prev_rate = None
            failed_run_count = 0
            current_zone = None
            continue

        seed = str((existing_map.get(ds) or {}).get("seed") or "").strip()
        if not seed:
            seed = _stable_trend_seed(namespace, ds)
        rng = random.Random(f"{seed}:{ds}")

        day_records = by_date.get(ds, [])
        success_today = any(is_success_record(record) for record in day_records)
        failed_run_today = any(not is_success_record(record) for record in day_records)
        if success_today:
            rate = _trend_success_rate(prev_rate, rng)
            failed_run_count = 0
            current_zone = (80.0, 100.0)
            success_seen = True
        elif not success_seen and launch_start_date is not None and current_date >= launch_start_date:
            rate = _trend_launch_rate(prev_rate, (current_date - launch_start_date).days, rng)
            current_zone = None
            failed_run_count = 0
        elif failed_run_today:
            failed_run_count += 1
            current_zone = _trend_failure_zone(failed_run_count)
            rate = _trend_gap_rate(prev_rate, current_zone[0], current_zone[1], rng)
        else:
            if current_zone is None:
                generated_items.append({"date": ds, "seed": seed, "rate": None, "missing": True})
                continue
            rate = _trend_gap_rate(prev_rate, current_zone[0], current_zone[1], rng)

        prev_rate = rate
        generated_items.append({"date": ds, "seed": seed, "rate": rate, "missing": False})

    recorded_dates = sorted(by_date.keys()) if has_success_record else []
    return generated_items, recorded_dates


def _trend_launch_band(days_since_start: int) -> tuple[float, float]:
    """首次成功前的新任务启动带宽：第 5 个自然日起稳定在 50-60。"""
    elapsed = max(0, int(days_since_start or 0))
    if elapsed <= 0:
        return 1.2, 4.8
    if elapsed == 1:
        return 7.0, 13.0
    if elapsed == 2:
        return 17.0, 26.0
    if elapsed == 3:
        return 33.0, 45.0
    return 50.0, 60.0


def _trend_launch_rate(previous_rate: float | None, days_since_start: int, rng: random.Random) -> float:
    band_low, band_high = _trend_launch_band(days_since_start)
    if previous_rate is None:
        return round(rng.uniform(band_low, band_high), 1)

    band_mid = band_low + (band_high - band_low) / 2
    if previous_rate < band_low:
        gap = band_mid - previous_rate
        step = max(2.0, min(13.0, gap * rng.uniform(0.58, 0.78)))
        candidate = previous_rate + step + rng.uniform(-0.8, 0.8)
    elif previous_rate > band_high:
        gap = previous_rate - band_mid
        step = max(1.0, min(6.0, gap * rng.uniform(0.25, 0.42)))
        candidate = previous_rate - step + rng.uniform(-0.5, 0.5)
    else:
        drift = (band_mid - previous_rate) * rng.uniform(0.10, 0.24)
        candidate = previous_rate + drift + rng.uniform(-1.2, 1.2)

    rounded = round(max(band_low, min(band_high, candidate)), 1)
    if rounded == round(previous_rate, 1):
        nudge = 0.7 if previous_rate <= band_low + 0.7 else -0.7 if previous_rate >= band_high - 0.7 else (0.7 if rng.random() >= 0.5 else -0.7)
        rounded = round(max(band_low, min(band_high, previous_rate + nudge)), 1)
    return rounded


def _trend_success_rate(previous_rate: float | None, rng: random.Random) -> float:
    """成功后向成功区间靠拢，但从前序曲线自然接续。"""
    if previous_rate is None:
        return round(rng.uniform(80.0, 86.0), 1)
    if previous_rate >= 80.0:
        return _trend_gap_rate(previous_rate, 80.0, 100.0, rng)

    candidate = 80.0 + min(
        6.0,
        max(0.0, (previous_rate - 50.0) * 0.25 + rng.uniform(0.0, 3.0)),
    )
    return round(max(80.0, min(86.0, candidate)), 1)


def _brand_trend_cache_key(task_name: str, brands: list[str] | None, *, task_id: str = "") -> str:
    storage_key = _history_storage_key(task_id=task_id, task_name=task_name)
    scope = "|".join(sorted(_normalize_brand_names(brands))) or "__all__"
    return f"{storage_key}:{scope}"


def _brand_trend_cache_file(task_name: str, brands: list[str] | None, *, task_id: str = "") -> Path:
    safe = _safe_name(_history_storage_key(task_id=task_id, task_name=task_name))
    scope = "|".join(sorted(_normalize_brand_names(brands))) or "__all__"
    scope_hash = hashlib.sha1(scope.encode("utf-8")).hexdigest()[:12]
    return _history_dir() / f"{safe}_trend_{scope_hash}.json"


def _load_brand_trend_cache(task_name: str, brands: list[str] | None, *, task_id: str = "") -> dict:
    path = _brand_trend_cache_file(task_name, brands, task_id=task_id)
    if _history_uses_sqlite():
        data = _load_json_document(path, {})
        if isinstance(data, dict):
            return data or {
                "version": 1,
                "task_id": str(task_id or "").strip(),
                "task_name": task_name,
                "brands": _normalize_brand_names(brands),
                "items": [],
            }
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {
        "version": 1,
        "task_id": str(task_id or "").strip(),
        "task_name": task_name,
        "brands": _normalize_brand_names(brands),
        "items": [],
    }


def _save_brand_trend_cache(task_name: str, brands: list[str] | None, items: list[dict], *, task_id: str = "") -> None:
    path = _brand_trend_cache_file(task_name, brands, task_id=task_id)
    payload = {
        "version": 1,
        "task_id": str(task_id or "").strip(),
        "task_name": str(task_name or "").strip(),
        "brands": _normalize_brand_names(brands),
        "items": items,
    }
    if _history_uses_sqlite():
        _save_json_document(path, payload)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        print(f"[History] 写入品牌趋势缓存失败 {path}: {e}")


def _trend_gap_rate(previous_rate: float | None, zone_low: float, zone_high: float, rng: random.Random) -> float:
    """生成无命中/未运行日的展示值；跨区间时做限幅过渡，避免折线突跳。"""
    if zone_high <= zone_low:
        return round(zone_low, 1)

    zone_width = zone_high - zone_low
    zone_mid = zone_low + zone_width / 2
    if previous_rate is None:
        return round(rng.uniform(zone_low, zone_high), 1)

    if previous_rate < zone_low:
        gap = zone_low - previous_rate
        step_limit = max(4.0, min(14.0, gap * 0.55 + 2.0))
        candidate = previous_rate + step_limit + rng.uniform(-1.0, 1.0)
        candidate = max(previous_rate + 1.0, min(zone_high, candidate))
        return round(candidate, 1)

    if previous_rate > zone_high:
        gap = previous_rate - zone_high
        step_limit = max(4.0, min(16.0, gap * 0.55 + 2.0))
        candidate = previous_rate - step_limit + rng.uniform(-1.0, 1.0)
        candidate = max(zone_low, min(zone_high, candidate))
        return round(candidate, 1)

    step_limit = max(1.2, min(4.0, zone_width * 0.18))
    drift = (zone_mid - previous_rate) * rng.uniform(0.08, 0.22)
    candidate = previous_rate + drift + rng.uniform(-step_limit, step_limit)

    if candidate < zone_low:
        candidate = zone_low + (zone_low - candidate) * rng.uniform(0.35, 0.85)
    elif candidate > zone_high:
        candidate = zone_high - (candidate - zone_high) * rng.uniform(0.35, 0.85)
    candidate = max(zone_low, min(zone_high, candidate))

    rounded = round(candidate, 1)
    if rounded == round(previous_rate, 1):
        nudge = max(0.3, min(1.2, zone_width * 0.04))
        if previous_rate <= zone_low + nudge:
            candidate = previous_rate + nudge
        elif previous_rate >= zone_high - nudge:
            candidate = previous_rate - nudge
        else:
            candidate = previous_rate + (nudge if rng.random() >= 0.5 else -nudge)
        rounded = round(max(zone_low, min(zone_high, candidate)), 1)
    return rounded


def _trend_failure_zone(failed_run_count: int) -> tuple[float, float]:
    """按连续失败运行次数返回趋势展示区间。"""
    count = max(1, int(failed_run_count or 1))
    if count <= 3:
        return 80.0, 100.0
    if count <= 6:
        return 60.0, 80.0
    return 40.0, 60.0


def _history_storage_key(*, task_id: str = "", task_name: str = "") -> str:
    return str(task_id or "").strip() or str(task_name or "").strip()


def _history_write_targets(*, task_id: str = "", task_name: str = "") -> list[str]:
    targets: list[str] = []
    primary = _history_storage_key(task_id=task_id, task_name=task_name)
    if primary:
        targets.append(primary)
    legacy = str(task_name or "").strip()
    if legacy and legacy not in targets:
        targets.append(legacy)
    return targets


def _history_read_targets(*, task_id: str = "", task_name: str = "", include_legacy: bool = True) -> list[str]:
    targets: list[str] = []
    normalized_task_id = str(task_id or "").strip()
    primary = _history_storage_key(task_id=normalized_task_id, task_name=task_name)
    if primary:
        targets.append(primary)
    if normalized_task_id and _history_document_exists(normalized_task_id):
        return targets
    legacy = str(task_name or "").strip()
    if include_legacy and legacy and legacy not in targets:
        targets.append(legacy)
    return targets


def _history_record_dedupe_key(record: dict) -> str:
    record_id = str((record or {}).get("id") or "").strip()
    if record_id:
        return f"id:{record_id}"
    payload = {
        "task_id": str((record or {}).get("task_id") or "").strip(),
        "task_name": str((record or {}).get("task_name") or "").strip(),
        "ts": str((record or {}).get("ts") or "").strip(),
        "platform": str((record or {}).get("platform") or "").strip(),
        "keyword": str((record or {}).get("keyword") or "").strip(),
        "brand": str((record or {}).get("brand") or "").strip(),
        "rank": int((record or {}).get("rank", 99) or 99),
        "mode": str((record or {}).get("mode") or "").strip(),
        "execution_source": str((record or {}).get("execution_source") or "").strip(),
    }
    return "raw:" + json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _task_file(task_name: str) -> Path:
    safe = _safe_name(task_name)
    return _history_dir() / f"{safe}.json"


def _rates_file(task_name: str) -> Path:
    safe = _safe_name(task_name)
    return _history_dir() / f"{safe}_rates.json"


def _periods_file(task_name: str) -> Path:
    safe = _safe_name(task_name)
    return _history_dir() / f"{safe}_periods.json"


def get_optimization_periods(task_name: str) -> list[dict]:
    """读取该任务所有历史优化周期列表。"""
    path = _periods_file(task_name)
    data = _load(path)
    return [item for item in data if isinstance(item, dict) and item.get("start") and item.get("end")]


def save_optimization_period(task_name: str, start_date: str, end_date: str) -> None:
    """追加或更新优化周期到 periods 历史文件。"""
    start_date = str(start_date or "").strip()
    end_date = str(end_date or "").strip()
    if not start_date or not end_date:
        return

    path = _periods_file(task_name)
    lock = _get_lock(f"{task_name}__periods")
    with lock:
        periods = _load(path)
        periods = [item for item in periods if isinstance(item, dict) and item.get("start") and item.get("end")]

        # 智能追加/更新
        updated = False
        for period in periods:
            if period["start"] == start_date and period["end"] == end_date:
                # 完全相同，跳过
                return
            if period["start"] == start_date:
                # start 相同，更新 end（用户调整了同一周期的结束时间）
                period["end"] = end_date
                updated = True
                break

        if not updated:
            periods.append({"start": start_date, "end": end_date})

        # 按 start 排序
        periods.sort(key=lambda p: p.get("start", ""))
        _save(path, periods)


def is_date_in_optimization_period(task_name: str, check_date: date) -> bool:
    """判断某日期是否在任一历史优化周期内。
    periods 文件不存在或为空时返回 True（向后兼容老任务）。"""
    periods = get_optimization_periods(task_name)
    if not periods:
        return True

    ds = check_date.isoformat() if isinstance(check_date, date) else str(check_date)
    for period in periods:
        if period.get("start", "") <= ds <= period.get("end", ""):
            return True
    return False


def _safe_name(task_name: str) -> str:
    safe = task_name.replace("/", "_").replace("\\", "_").replace(" ", "_")
    # 替换 Windows 文件名非法字符，保证跨平台兼容
    for ch in ':*?"<>|':
        safe = safe.replace(ch, "_")
    return safe


def _history_dir() -> Path:
    if HISTORY_DIR != DEFAULT_HISTORY_DIR:
        return HISTORY_DIR
    return account_scoped_path("logs/history", fallback=DEFAULT_HISTORY_DIR)


def get_history_dir() -> Path:
    return _history_dir()


def _history_lock_file(lock_key: str) -> Path:
    return _history_dir() / ".locks" / f"{_safe_name(lock_key)}.lock"


def _get_lock(task_name: str) -> CrossProcessRLock:
    lock_key = str(task_name or "__default__").strip() or "__default__"
    with _locks_mutex:
        lock = _locks.pop(lock_key, None)
        if lock is None:
            lock = CrossProcessRLock(lambda key=lock_key: _history_lock_file(key))
        _locks[lock_key] = lock
        _prune_lock_cache_locked()
        return lock


def _prune_lock_cache_locked() -> None:
    capacity = max(1, int(_MAX_LOCKS or 1))
    if len(_locks) <= capacity:
        return

    target_size = max(1, int(capacity * 0.75))
    for key in list(_locks.keys()):
        if len(_locks) <= target_size:
            break
        lock = _locks.get(key)
        if lock is not None and getattr(lock, "_depth", 0) > 0:
            continue
        _locks.pop(key, None)


def _load(path: Path) -> list:
    return _load_json_document(path, [])


def _save(path: Path, data: list):
    _save_json_document(path, data)


def _normalize_record(record: dict, task_name: str = "", task_id: str = "") -> dict:
    if not isinstance(record, dict):
        return {}
    if not record.get("id"):
        record["id"] = uuid.uuid4().hex
    if task_id and not record.get("task_id"):
        record["task_id"] = task_id
    if task_name and not record.get("task_name"):
        record["task_name"] = task_name
    record.setdefault("review_status", "pending" if record.get("success") and record.get("rank", 99) != 99 else "")
    record.setdefault("review_note", "")
    record.setdefault("reviewed_at", "")
    record.setdefault("screenshot", "")
    record.setdefault("highlight_count", 0)
    record.setdefault("answer_text", "")
    record.setdefault("evidence", "")
    record.setdefault("error_message", "")
    record.setdefault("diagnostic_id", "")
    record.setdefault("mode", "")
    record.setdefault("execution_source", "")
    return record
